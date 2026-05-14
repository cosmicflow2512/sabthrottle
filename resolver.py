"""Decide the SABnzbd speed limit for the current moment.

Two-stage resolution:

    1. Determine the *base* limit from active time rules. If any rule
       matches the current time it wins over the default (a rule can
       therefore both lower AND raise the default in its window). If
       several rules match at once, the lowest of them wins for safety.
    2. If Jellyfin is actively streaming, apply the stream throttle —
       but only if it is actually stricter than the current base. A
       100 % stream throttle never weakens a more aggressive rule.

This gives users an intuitive model — "in this time window use X" —
while still guaranteeing that an active stream is never starved.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

import units


@dataclass
class Candidate:
    mode: str
    value: float
    source: str
    kbps: float


@dataclass
class Decision:
    mode: str
    value: float
    source: str
    kbps: float
    pause: bool
    base: Candidate                  # what the resolver picked as the base
    stream_override: Candidate | None  # set when the stream throttle won
    rule_candidates: list[Candidate]   # all rules considered for the base


def _line_speed_kbps(settings: dict[str, Any]) -> float:
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
    # Overnight window (e.g. 22:00 → 06:00)
    return now_hm >= start or now_hm <= end


def _make(mode: str, value: float, source: str, line: float) -> Candidate:
    return Candidate(
        mode=mode, value=value, source=source,
        kbps=units.to_kbps(mode, value, line),
    )


def resolve(
    settings: dict[str, Any],
    rules: list[dict[str, Any]],
    jellyfin_streams: int,
    now: dt.datetime | None = None,
) -> Decision:
    now  = now or dt.datetime.now()
    line = _line_speed_kbps(settings)

    # ---- Step 1: base limit (time rule wins over default) ----
    active_rules: list[Candidate] = []
    for r in rules:
        if not _rule_applies(r, now):
            continue
        active_rules.append(_make(
            mode=r.get("mode", units.MODE_PERCENT),
            value=float(r.get("value", 100)),
            source=f"Zeitregel: {r.get('name') or r.get('id','')}",
            line=line,
        ))

    if active_rules:
        base = min(active_rules, key=lambda c: c.kbps)
    else:
        base = _make(
            units.MODE_PERCENT,
            float(settings.get("default_percent", 100)),
            "Standardmodus",
            line,
        )

    # ---- Step 2: stream override (only if stricter than base) ----
    stream_override: Candidate | None = None
    if jellyfin_streams > 0:
        stream = _make(
            units.MODE_PERCENT,
            float(settings.get("throttle_percent", 50)),
            f"Jellyfin-Stream ({jellyfin_streams})",
            line,
        )
        if stream.kbps < base.kbps:
            stream_override = stream

    winner = stream_override or base
    return Decision(
        mode=winner.mode,
        value=winner.value,
        source=winner.source,
        kbps=winner.kbps,
        pause=(winner.mode == units.MODE_PAUSE),
        base=base,
        stream_override=stream_override,
        rule_candidates=active_rules,
    )
