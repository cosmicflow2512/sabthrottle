"""sabthrottle — throttle SABnzbd while Jellyfin is streaming.

Listens for Jellyfin Webhook events and dynamically adjusts the SABnzbd
speed limit so that active streams (e.g. via NZBDav) always get enough
bandwidth.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

import requests
from flask import Flask, jsonify, request

# ---------- Config ----------------------------------------------------------

def _env(name: str, default: str | None = None, required: bool = False) -> str:
    val = os.environ.get(name, default)
    if required and not val:
        raise SystemExit(f"missing required env var: {name}")
    return val  # type: ignore[return-value]

SABNZBD_URL          = _env("SABNZBD_URL", required=True).rstrip("/")
SABNZBD_API_KEY      = _env("SABNZBD_API_KEY", required=True)
THROTTLE_PERCENT     = int(_env("THROTTLE_PERCENT", "50"))
FULL_PERCENT         = int(_env("FULL_PERCENT", "100"))
WEBHOOK_TOKEN        = _env("WEBHOOK_TOKEN", "")            # optional shared secret
NZBDAV_PATH_PREFIX   = _env("NZBDAV_PATH_PREFIX", "")       # optional filter
SESSION_TIMEOUT_SEC  = int(_env("SESSION_TIMEOUT_SEC", "90"))
GC_INTERVAL_SEC      = int(_env("GC_INTERVAL_SEC", "30"))
LISTEN_PORT          = int(_env("LISTEN_PORT", "6811"))
LOG_LEVEL            = _env("LOG_LEVEL", "INFO").upper()

# Optional polling mode — if both are set, sabthrottle polls Jellyfin
# directly and no Webhook plugin is required.
JELLYFIN_URL              = _env("JELLYFIN_URL", "").rstrip("/")
JELLYFIN_API_KEY          = _env("JELLYFIN_API_KEY", "")
JELLYFIN_POLL_INTERVAL    = int(_env("JELLYFIN_POLL_INTERVAL_SEC", "15"))
POLLING_ENABLED           = bool(JELLYFIN_URL and JELLYFIN_API_KEY)

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("sabthrottle")

# ---------- State -----------------------------------------------------------

app = Flask(__name__)
_active: dict[str, float] = {}   # session_id -> last_seen_ts
_lock = threading.Lock()
_current_limit = [FULL_PERCENT]

# ---------- SABnzbd ---------------------------------------------------------

def set_sabnzbd_speed(percent: int) -> None:
    try:
        r = requests.get(
            f"{SABNZBD_URL}/api",
            params={
                "mode": "config",
                "name": "speedlimit",
                "value": str(percent),
                "apikey": SABNZBD_API_KEY,
                "output": "json",
            },
            timeout=5,
        )
        r.raise_for_status()
        log.info("SABnzbd speed limit set to %d%%", percent)
    except Exception as e:
        log.error("failed to update SABnzbd speed limit: %s", e)

def _apply_target() -> None:
    target = THROTTLE_PERCENT if _active else FULL_PERCENT
    if target != _current_limit[0]:
        set_sabnzbd_speed(target)
        _current_limit[0] = target

# ---------- Webhook helpers -------------------------------------------------

def _matches_filter(payload: dict[str, Any]) -> bool:
    if not NZBDAV_PATH_PREFIX:
        return True
    path = payload.get("ItemPath") or payload.get("Path") or ""
    return NZBDAV_PATH_PREFIX in path

def _session_key(payload: dict[str, Any]) -> str:
    return (
        payload.get("SessionId")
        or f"{payload.get('UserId','?')}::{payload.get('ItemId','?')}"
    )

# ---------- Routes ----------------------------------------------------------

@app.route("/jellyfin", methods=["POST"])
def jellyfin_webhook():
    if WEBHOOK_TOKEN:
        provided = request.args.get("token") or request.headers.get("X-Webhook-Token")
        if provided != WEBHOOK_TOKEN:
            return ("unauthorized", 401)

    payload = request.get_json(force=True, silent=True) or {}
    event   = payload.get("NotificationType", "")
    paused  = bool(payload.get("IsPaused", False))
    sid     = _session_key(payload)

    if not _matches_filter(payload):
        log.debug("ignoring event for non-matching path: %s", payload.get("ItemPath"))
        return ("", 204)

    with _lock:
        if event == "PlaybackStop" or paused:
            _active.pop(sid, None)
            log.info("session %s -> inactive (%s, paused=%s)", sid, event, paused)
        elif event in ("PlaybackStart", "PlaybackProgress"):
            _active[sid] = time.time()
            log.debug("session %s heartbeat (%s)", sid, event)
        else:
            log.debug("ignoring event type: %s", event)
        _apply_target()

    return ("", 204)

@app.route("/health")
def health():
    return jsonify(status="ok")

@app.route("/status")
def status():
    with _lock:
        return jsonify(
            mode="polling" if POLLING_ENABLED else "webhook",
            active_sessions=len(_active),
            sessions=list(_active.keys()),
            current_sabnzbd_limit_percent=_current_limit[0],
        )

# ---------- Background: GC for webhook mode --------------------------------

def _gc_loop() -> None:
    while True:
        time.sleep(GC_INTERVAL_SEC)
        with _lock:
            now = time.time()
            stale = [k for k, v in _active.items() if now - v > SESSION_TIMEOUT_SEC]
            for k in stale:
                del _active[k]
                log.info("session %s expired (no heartbeat)", k)
            _apply_target()

# ---------- Background: Jellyfin polling -----------------------------------

def _jellyfin_auth_header() -> str:
    # Per Jellyfin API guidelines: prefer the Authorization header with the
    # MediaBrowser scheme over the deprecated X-Emby-Token header.
    # https://gist.github.com/nielsvanvelzen/ea047d9028f676185832e51ffaf12a6f
    return (
        f'MediaBrowser Token="{JELLYFIN_API_KEY}", '
        f'Client="sabthrottle", Device="sabthrottle", '
        f'DeviceId="sabthrottle", Version="1.0"'
    )

def _fetch_jellyfin_sessions() -> list[dict[str, Any]] | None:
    try:
        r = requests.get(
            f"{JELLYFIN_URL}/Sessions",
            headers={"Authorization": _jellyfin_auth_header()},
            timeout=10,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("Jellyfin poll failed: %s", e)
        return None

def _poll_loop() -> None:
    log.info("Jellyfin polling enabled (every %ds)", JELLYFIN_POLL_INTERVAL)
    while True:
        sessions = _fetch_jellyfin_sessions()
        if sessions is not None:
            new_active: dict[str, float] = {}
            now = time.time()
            for s in sessions:
                npi = s.get("NowPlayingItem")
                if not npi:
                    continue
                play_state = s.get("PlayState") or {}
                if play_state.get("IsPaused"):
                    continue
                pseudo_payload = {
                    "ItemPath": npi.get("Path", ""),
                    "Path": npi.get("Path", ""),
                }
                if not _matches_filter(pseudo_payload):
                    continue
                sid = s.get("Id") or f"{s.get('UserId','?')}::{npi.get('Id','?')}"
                new_active[sid] = now
            with _lock:
                _active.clear()
                _active.update(new_active)
                _apply_target()
        time.sleep(JELLYFIN_POLL_INTERVAL)

if POLLING_ENABLED:
    threading.Thread(target=_poll_loop, daemon=True).start()
else:
    threading.Thread(target=_gc_loop, daemon=True).start()

# ---------- Entrypoint ------------------------------------------------------

if __name__ == "__main__":
    log.info("sabthrottle starting on port %d", LISTEN_PORT)
    log.info("SABnzbd target: %s, throttle=%d%%, full=%d%%",
             SABNZBD_URL, THROTTLE_PERCENT, FULL_PERCENT)
    log.info("mode: %s", "polling" if POLLING_ENABLED else "webhook-only")
    if NZBDAV_PATH_PREFIX:
        log.info("path filter active: %s", NZBDAV_PATH_PREFIX)
    set_sabnzbd_speed(FULL_PERCENT)
    app.run(host="0.0.0.0", port=LISTEN_PORT)
