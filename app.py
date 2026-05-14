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

SAB_URL              = _env("SAB_URL", required=True).rstrip("/")
SAB_API_KEY          = _env("SAB_API_KEY", required=True)
THROTTLE_PERCENT     = int(_env("THROTTLE_PERCENT", "50"))
FULL_PERCENT         = int(_env("FULL_PERCENT", "100"))
WEBHOOK_TOKEN        = _env("WEBHOOK_TOKEN", "")            # optional shared secret
NZBDAV_PATH_PREFIX   = _env("NZBDAV_PATH_PREFIX", "")       # optional filter
SESSION_TIMEOUT_SEC  = int(_env("SESSION_TIMEOUT_SEC", "90"))
GC_INTERVAL_SEC      = int(_env("GC_INTERVAL_SEC", "30"))
LISTEN_PORT          = int(_env("LISTEN_PORT", "9999"))
LOG_LEVEL            = _env("LOG_LEVEL", "INFO").upper()

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

def set_sab_speed(percent: int) -> None:
    try:
        r = requests.get(
            f"{SAB_URL}/sabnzbd/api",
            params={
                "mode": "config",
                "name": "speedlimit",
                "value": str(percent),
                "apikey": SAB_API_KEY,
                "output": "json",
            },
            timeout=5,
        )
        r.raise_for_status()
        log.info("SAB speed limit set to %d%%", percent)
    except Exception as e:
        log.error("failed to update SAB speed limit: %s", e)

def _apply_target() -> None:
    target = THROTTLE_PERCENT if _active else FULL_PERCENT
    if target != _current_limit[0]:
        set_sab_speed(target)
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
            active_sessions=len(_active),
            sessions=list(_active.keys()),
            current_sab_limit_percent=_current_limit[0],
        )

# ---------- Background GC ---------------------------------------------------

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

threading.Thread(target=_gc_loop, daemon=True).start()

# ---------- Entrypoint ------------------------------------------------------

if __name__ == "__main__":
    log.info("sabthrottle starting on port %d", LISTEN_PORT)
    log.info("SAB target: %s, throttle=%d%%, full=%d%%",
             SAB_URL, THROTTLE_PERCENT, FULL_PERCENT)
    if NZBDAV_PATH_PREFIX:
        log.info("path filter active: %s", NZBDAV_PATH_PREFIX)
    set_sab_speed(FULL_PERCENT)
    app.run(host="0.0.0.0", port=LISTEN_PORT)
