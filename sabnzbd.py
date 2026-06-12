"""SABnzbd API client.

    set_speed(mode, value)        — set speed limit
    pause() / resume()            — control downloads
    get_max_speed_kbps()          — fetch the user-configured line speed
                                    (bandwidth_max), cached for 5 minutes
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

import requests

import units

log = logging.getLogger("sabthrottle.sabnzbd")

SABNZBD_URL     = os.environ["SABNZBD_URL"].rstrip("/")
SABNZBD_API_KEY = os.environ["SABNZBD_API_KEY"]

_MAX_SPEED_TTL = 300   # seconds
_max_speed_cache: dict[str, Any] = {"value": None, "ts": 0.0}


def _call(mode: str, **extra: str) -> bool:
    params = {"mode": mode, "apikey": SABNZBD_API_KEY, "output": "json", **extra}
    try:
        r = requests.get(f"{SABNZBD_URL}/api", params=params, timeout=5)
        r.raise_for_status()
        return True
    except Exception as e:
        log.error("SABnzbd %s call failed: %s", mode, e)
        return False


def _fetch_json(mode: str, **extra: str) -> dict[str, Any] | None:
    params = {"mode": mode, "apikey": SABNZBD_API_KEY, "output": "json", **extra}
    try:
        r = requests.get(f"{SABNZBD_URL}/api", params=params, timeout=5)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("SABnzbd %s call failed: %s", mode, e)
        return None


def get_max_speed_kbps(force: bool = False) -> float | None:
    """Return SABnzbd's configured maximum line speed in KB/s.

    Cached for _MAX_SPEED_TTL seconds since this rarely changes.
    Returns None if the value isn't set in SABnzbd or the API call fails.
    """
    now = time.time()
    if not force and (now - _max_speed_cache["ts"]) < _MAX_SPEED_TTL:
        return _max_speed_cache["value"]

    data = _fetch_json("get_config", section="misc", keyword="bandwidth_max")
    raw = None
    if data:
        # The API can return shapes like {"config":{"misc":{"bandwidth_max":"100M"}}}
        # or {"config":{"misc":[{"bandwidth_max":"100M",...}]}}.
        misc = (data.get("config") or {}).get("misc")
        if isinstance(misc, list) and misc:
            raw = misc[0].get("bandwidth_max")
        elif isinstance(misc, dict):
            raw = misc.get("bandwidth_max")

    kbps = units.parse_sabnzbd_speed(raw)
    _max_speed_cache.update(value=kbps, ts=now)
    if kbps:
        log.debug("SABnzbd bandwidth_max = %r -> %.0f KB/s", raw, kbps)
    return kbps


def set_speed(mode: str, value: float) -> bool:
    """Set the SABnzbd speed limit. Returns True on success."""
    sab_value = units.sabnzbd_value(mode, value)
    ok = _call("config", name="speedlimit", value=sab_value)
    if ok:
        log.info("SABnzbd speed limit -> %s", sab_value)
    return ok


def pause() -> bool:
    ok = _call("pause")
    if ok:
        log.info("SABnzbd paused")
    return ok


def resume() -> bool:
    ok = _call("resume")
    if ok:
        log.info("SABnzbd resumed")
    return ok


def get_activity() -> tuple[bool, float]:
    """Return (is_active, current_speed_kbps) from the queue endpoint.

    Active means there is at least one item in the queue and the queue is
    not paused. Speed is SABnzbd's reported kbpersec, used by the
    demand-based budget split. Returns (False, 0.0) on API failure; the
    caller's hysteresis bridges single failed polls.
    """
    data = _fetch_json("queue", limit="1")
    if not data:
        return False, 0.0
    q = data.get("queue") or {}
    try:
        slots = int(q.get("noofslots_total") or q.get("noofslots") or 0)
    except (TypeError, ValueError):
        slots = 0
    paused = bool(q.get("paused"))
    try:
        speed_kbps = float(q.get("kbpersec") or 0.0)
    except (TypeError, ValueError):
        speed_kbps = 0.0
    return (slots > 0 and not paused), speed_kbps
