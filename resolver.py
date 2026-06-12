"""Decide the speed limit for every download target at the current moment.

Per target, the resolution has two stages:

    1. BASE — from that target's own time rules. If any rule matches the
       current time it replaces the target's default (a rule can therefore
       both lower AND raise the default in its window). If several rules
       match at once, the lowest wins for safety. Rules that resolve to
       0 KB/s without being pause rules are ignored as invalid (a literal
       0 would mean "unlimited" on the SABnzbd API).

    2. STREAM BUDGET — while Jellyfin is streaming, all targets together
       share one global bandwidth budget (stream_budget_percent of the
       line speed). The budget is divided among the targets that are
       actively downloading:

           equal  — budget / n
           demand — proportional to each target's smoothed measured
                    speed (with a headroom factor so a target can grow,
                    and a floor so a freshly started target is never
                    starved). Shares are normalised so they always sum
                    to exactly the budget.

       A target's share only ever *tightens* its base limit (min of the
       two): a stricter time rule still wins, and the budget never
       weakens a more aggressive rule.

       Targets whose base is "pause" are excluded from the split — we are
       pausing them ourselves, so they must not consume budget.

       Idle targets keep their base limit, or — with burst protection
       enabled — are parked at floor_kbps so a download that starts
       mid-tick cannot briefly saturate the line.

Single-target degenerate case: with one active target the demand split
gives it the entire budget, which is exactly the pre-v2 behaviour.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

import units


@dataclass
class Candidate:
    mode: str
    value: float
    source: str
    kbps: float


@dataclass
class TargetInput:
    """Everything the resolver needs to know about one target this tick."""
    key: str                      # "sab" | "jd"
    rules: list[dict[str, Any]]
    default_percent: float
    active: bool                  # hysteresis-adjusted download activity
    ema_kbps: float               # smoothed measured speed


@dataclass
class TargetDecision:
    key: str
    mode: str
    value: float
    source: str
    kbps: float
    pause: bool
    base: Candidate
    stream_share: Candidate | None      # set when the budget share won
    rule_candidates: list[Candidate] = field(default_factory=list)
    in_split: bool = False              # took part in the budget split
    parked: bool = False                # idle + burst protection floor


def line_speed_kbps(settings: dict[str, Any]) -> float:
    """Return line speed in KB/s.

    Priority:
      1. `line_speed_mbit` user override from settings.json
      2. `_detected_line_speed_kbps` filled in by the caller from
         SABnzbd's own `bandwidth_max` config
      3. 1 Gbit/s fallback so percent rules always have a reference.
    """
    mbit = float(settings.get("line_speed_mbit") or 0)
    if mbit > 0:
        return units.mbit_to_kbps(mbit)
    detected = settings.get("_detected_line_speed_kbps")
    if detected:
        return float(detected)
    return units.mbit_to_kbps(1000.0)


def _rule_applies(rule: dict[str, Any], now: dt.datetime) -> bool:
    if not rule.get("enabled", True):
        return False
    weekdays = rule.get("weekdays") or []
    if weekdays and now.isoweekday() not in weekdays:
        return False
    start = rule.get("start", "00:00")
    end   = rule.get("end", "23:59")
    now_hm = now.strftime("%H:%M")
    if start <= end:
        return start <= now_hm <= end
    # Overnight window (e.g. 22:00 -> 06:00)
    return now_hm >= start or now_hm <= end


def _make(mode: str, value: float, source: str, line: float) -> Candidate:
    return Candidate(
        mode=mode, value=value, source=source,
        kbps=units.to_kbps(mode, value, line),
    )


def _resolve_base(t: TargetInput, line: float, now: dt.datetime) -> tuple[Candidate, list[Candidate]]:
    candidates: list[Candidate] = []
    for r in t.rules:
        if not _rule_applies(r, now):
            continue
        c = _make(
            mode=r.get("mode", units.MODE_PERCENT),
            value=float(r.get("value", 100)),
            source=f"Rule: {r.get('name') or r.get('id', '')}",
            line=line,
        )
        # A non-pause rule resolving to 0 KB/s would mean "unlimited" on
        # the SABnzbd API — treat it as invalid and skip it.
        if c.mode != units.MODE_PAUSE and c.kbps <= 0:
            continue
        candidates.append(c)

    if candidates:
        base = min(candidates, key=lambda c: c.kbps)
    else:
        base = _make(units.MODE_PERCENT, t.default_percent, "Default", line)
    return base, candidates


def _split_budget(
    actives: list[TargetInput],
    budget_kbps: float,
    settings: dict[str, Any],
) -> dict[str, float]:
    """Return {target_key: share_kbps}; shares sum to exactly the budget."""
    if not actives:
        return {}
    if len(actives) == 1 or settings.get("split_mode", "demand") == "equal":
        share = budget_kbps / len(actives)
        return {t.key: share for t in actives}

    headroom = max(1.0, float(settings.get("headroom_factor", 1.5)))
    floor    = max(1.0, float(settings.get("floor_kbps", 5120)))
    demands  = {t.key: max(t.ema_kbps * headroom, floor) for t in actives}
    total    = sum(demands.values())
    return {k: budget_kbps * d / total for k, d in demands.items()}


def resolve_all(
    settings: dict[str, Any],
    targets: list[TargetInput],
    jellyfin_streams: int,
    now: dt.datetime | None = None,
) -> dict[str, TargetDecision]:
    now  = now or dt.datetime.now()
    line = line_speed_kbps(settings)

    # ---- Stage 1: per-target base from its own rules ----
    bases: dict[str, tuple[Candidate, list[Candidate]]] = {
        t.key: _resolve_base(t, line, now) for t in targets
    }

    # ---- Stage 2: stream budget split ----
    shares: dict[str, float] = {}
    streaming = jellyfin_streams > 0
    if streaming:
        budget_pct  = float(settings.get("stream_budget_percent", 50))
        budget_kbps = line * budget_pct / 100.0
        actives = [
            t for t in targets
            if t.active and bases[t.key][0].mode != units.MODE_PAUSE
        ]
        shares = _split_budget(actives, budget_kbps, settings)

    burst_protection = bool(settings.get("burst_protection", False))
    floor = max(1.0, float(settings.get("floor_kbps", 5120)))

    decisions: dict[str, TargetDecision] = {}
    for t in targets:
        base, candidates = bases[t.key]
        winner       = base
        stream_share = None
        in_split     = False
        parked       = False

        if streaming and base.mode != units.MODE_PAUSE:
            if t.key in shares:
                in_split = True
                # Quantise to whole MB/s (floor) — acts as a deadband so
                # small EMA drift doesn't fire an API call every tick, and
                # flooring guarantees the shares never exceed the budget.
                share_mb = max(1.0, float(int(units.kbps_to_mb(shares[t.key]))))
                share = _make(
                    units.MODE_MB,
                    share_mb,
                    f"Stream budget share ({jellyfin_streams} stream"
                    f"{'s' if jellyfin_streams != 1 else ''})",
                    line,
                )
                if share.kbps < base.kbps:
                    winner, stream_share = share, share
            elif burst_protection:
                park = _make(
                    units.MODE_MB,
                    units.kbps_to_mb(floor),
                    "Burst protection (idle while streaming)",
                    line,
                )
                if park.kbps < base.kbps:
                    winner, stream_share, parked = park, park, True

        decisions[t.key] = TargetDecision(
            key=t.key,
            mode=winner.mode,
            value=winner.value,
            source=winner.source,
            kbps=winner.kbps,
            pause=(winner.mode == units.MODE_PAUSE),
            base=base,
            stream_share=stream_share,
            rule_candidates=candidates,
            in_split=in_split,
            parked=parked,
        )
    return decisions
