"""Unit conversion helpers.

All internal speed math happens in KB/s (kilobytes per second), matching
the SABnzbd API. This module converts between the user-facing units
(percent, Mbit/s, MB/s) and KB/s, both directions.

Conventions (decimal, not binary):
    1 Mbit/s = 1000 / 8     KB/s = 125 KB/s
    1 MB/s   = 1000         KB/s
"""
from __future__ import annotations

MODE_PERCENT = "percent"
MODE_MBIT    = "mbit"
MODE_MB      = "mb"
MODE_PAUSE   = "pause"
VALID_MODES  = {MODE_PERCENT, MODE_MBIT, MODE_MB, MODE_PAUSE}


def to_kbps(mode: str, value: float, line_speed_kbps: float) -> float:
    """Convert a (mode, value) pair to absolute KB/s.

    For percent mode the line speed is required to resolve to an
    absolute value. Pause mode returns 0 (effectively the lowest possible
    limit, so it wins comparisons).
    """
    if mode == MODE_PAUSE:
        return 0.0
    if mode == MODE_PERCENT:
        return line_speed_kbps * float(value) / 100.0
    if mode == MODE_MBIT:
        return float(value) * 125.0
    if mode == MODE_MB:
        return float(value) * 1000.0
    raise ValueError(f"unknown speed mode: {mode}")


def kbps_to_mb(kbps: float) -> float:
    return kbps / 1000.0


def kbps_to_mbit(kbps: float) -> float:
    return kbps * 8.0 / 1000.0


def mbit_to_kbps(mbit: float) -> float:
    return float(mbit) * 125.0


def format_kbps(kbps: float) -> str:
    """Format a KB/s value as a human-friendly speed string."""
    if kbps <= 0:
        return "paused"
    mb = kbps_to_mb(kbps)
    if mb >= 1:
        return f"{mb:.1f} MB/s"
    return f"{kbps:.0f} KB/s"


def format_limit(mode: str, value: float, line_speed_kbps: float | None) -> str:
    """User-friendly description of a (mode, value) limit, optionally
    enriched with an absolute MB/s estimate when line speed is known."""
    if mode == MODE_PAUSE:
        return "Pause"
    if mode == MODE_PERCENT:
        if line_speed_kbps:
            mb = kbps_to_mb(line_speed_kbps * float(value) / 100.0)
            return f"{value:.0f} % (~{mb:.1f} MB/s)"
        return f"{value:.0f} %"
    if mode == MODE_MBIT:
        return f"{value:.0f} Mbit/s"
    if mode == MODE_MB:
        return f"{value:.0f} MB/s"
    return f"{value} {mode}"


def sabnzbd_value(mode: str, value: float) -> str:
    """Translate a (mode, value) pair to the string SABnzbd's speedlimit
    API expects.

    SABnzbd natively understands percent ("50"), KB/s suffix ("K"), and
    MB/s suffix ("M"). We pass percent and MB/s through verbatim, and
    convert Mbit/s to KB/s.
    """
    if mode == MODE_PERCENT:
        return str(int(round(float(value))))
    if mode == MODE_MB:
        return f"{value:g}M"
    if mode == MODE_MBIT:
        return f"{int(round(mbit_to_kbps(value)))}K"
    raise ValueError(f"cannot translate {mode} to SABnzbd value")
