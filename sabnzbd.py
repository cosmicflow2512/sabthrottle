"""SABnzbd API client.

Three primitives:

    set_speed(mode, value)  — set speed limit in percent/Mbit/MB
    pause()                 — pause all downloads
    resume()                — resume downloads
"""
from __future__ import annotations

import logging
import os

import requests

import units

log = logging.getLogger("sabthrottle.sabnzbd")

SABNZBD_URL     = os.environ["SABNZBD_URL"].rstrip("/")
SABNZBD_API_KEY = os.environ["SABNZBD_API_KEY"]


def _call(mode: str, **extra: str) -> bool:
    params = {"mode": mode, "apikey": SABNZBD_API_KEY, "output": "json", **extra}
    try:
        r = requests.get(f"{SABNZBD_URL}/api", params=params, timeout=5)
        r.raise_for_status()
        return True
    except Exception as e:
        log.error("SABnzbd %s call failed: %s", mode, e)
        return False


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
