"""Jellyfin polling client."""
from __future__ import annotations

import logging
import os
from typing import Any

import requests

log = logging.getLogger("sabthrottle.jellyfin")

JELLYFIN_URL     = os.environ.get("JELLYFIN_URL", "").rstrip("/")
JELLYFIN_API_KEY = os.environ.get("JELLYFIN_API_KEY", "")
ENABLED          = bool(JELLYFIN_URL and JELLYFIN_API_KEY)


def _auth_header() -> str:
    return (
        f'MediaBrowser Token="{JELLYFIN_API_KEY}", '
        f'Client="sabthrottle", Device="sabthrottle", '
        f'DeviceId="sabthrottle", Version="1.0"'
    )


def fetch_sessions() -> list[dict[str, Any]] | None:
    if not ENABLED:
        return []
    try:
        r = requests.get(
            f"{JELLYFIN_URL}/Sessions",
            headers={"Authorization": _auth_header()},
            timeout=10,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("Jellyfin poll failed: %s", e)
        return None


def filter_active(sessions: list[dict[str, Any]], path_prefix: str = "") -> list[dict[str, Any]]:
    """Return only sessions that are actively playing (have NowPlayingItem,
    not paused). Optionally filter by path prefix on the item path."""
    out = []
    for s in sessions or []:
        npi = s.get("NowPlayingItem")
        if not npi:
            continue
        play_state = s.get("PlayState") or {}
        if play_state.get("IsPaused"):
            continue
        if path_prefix and path_prefix not in (npi.get("Path") or ""):
            continue
        out.append(s)
    return out


def active_sessions(path_prefix: str = "") -> list[dict[str, Any]]:
    """Legacy helper: fetch + filter, treating fetch errors as no streams."""
    return filter_active(fetch_sessions() or [], path_prefix)
