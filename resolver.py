"""Decide the SABnzbd speed limit for the current moment.

The resolver collects all candidate limits that currently apply
(Jellyfin override + matching time rules + default) and picks the
lowest one in KB/s. The chosen limit is reported together with its
source so the WebUI/logs can explain *why* a given limit is active.

Priority follows directly from the "lowest wins" rule:

    PAUSE  (KB/s == 0)  >  any throttle  >  100%

so an active Jellyfin stream that maps to 50% always wins over a
"100% all night" rule, without any special-case priority logic.
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
    source: str       # human-readable explanation (e.g. "Jellyfin-Stream")
    kbps: float       # normalized for comparison


@dataclass
class Decision:
    mode: str
    value: float
    source: str
    kbps: float
    pause: bool
    candidates: list[Candidate]


def _line_speed_kbps(settings: dict[str, Any]) -> float:
    """Return line speed in KB/s.

    Priority:
      1. User override `line_speed_mbit` (from WebUI settings)
      2. Auto-detected `_detected_line_speed_kbps` (filled by caller from
         SABnzbd's own `bandwidth_max` config)
      3. 1 Gbit/s fallback — a generous default so percent rules still
         have a finite reference even without any input.
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


def resolve(
    settings: dict[str, Any],
    rules: list[dict[str, Any]],
    jellyfin_streams: int,
    now: dt.datetime | None = None,
) -> Decision:
    now  = now or dt.datetime.now()
    line = _line_speed_kbps(settings)

    cands: list[Candidate] = []

    # 1. Default baseline.
    default_pct = float(settings.get("default_percent", 100))
    cands.append(Candidate(
        mode=units.MODE_PERCENT,
        value=default_pct,
        source="Standardmodus",
        kbps=units.to_kbps(units.MODE_PERCENT, default_pct, line),
    ))

    # 2. Jellyfin override.
    if jellyfin_streams > 0:
        thr = float(settings.get("throttle_percent", 50))
        cands.append(Candidate(
            mode=units.MODE_PERCENT,
            value=thr,
            source=f"Jellyfin-Stream ({jellyfin_streams})",
            kbps=units.to_kbps(units.MODE_PERCENT, thr, line),
        ))

    # 3. Time rules.
    for r in rules:
        if not _rule_applies(r, now):
            continue
        mode  = r.get("mode", units.MODE_PERCENT)
        value = float(r.get("value", 100))
        name  = r.get("name") or f"Regel {r.get('id','')}"
        cands.append(Candidate(
            mode=mode,
            value=value,
            source=f"Zeitregel: {name}",
            kbps=units.to_kbps(mode, value, line),
        ))

    winner = min(cands, key=lambda c: c.kbps)
    return Decision(
        mode=winner.mode,
        value=winner.value,
        source=winner.source,
        kbps=winner.kbps,
        pause=(winner.mode == units.MODE_PAUSE),
        candidates=cands,
    )
