"""sabthrottle — automatic SABnzbd throttling driven by Jellyfin
playback + user-configured time-based speed rules.

Entry point. Wires together storage, jellyfin polling, the rule
resolver, the SABnzbd client and the WebUI.
"""
from __future__ import annotations

import logging
import os
import threading
import time

from flask import Flask, jsonify, redirect, render_template, request, url_for

import jellyfin
import resolver
import sabnzbd
import storage
import units

# ---------- Config ---------------------------------------------------------

LISTEN_PORT          = int(os.environ.get("LISTEN_PORT", "6811"))
LOG_LEVEL            = os.environ.get("LOG_LEVEL", "INFO").upper()
JELLYFIN_POLL_INTERVAL = int(os.environ.get("JELLYFIN_POLL_INTERVAL_SEC", "15"))
WEBHOOK_TOKEN        = os.environ.get("WEBHOOK_TOKEN", "")
NZBDAV_PATH_PREFIX   = os.environ.get("NZBDAV_PATH_PREFIX", "")

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("sabthrottle")

# Seed settings file from legacy env vars (THROTTLE_PERCENT, FULL_PERCENT)
# on first start so existing users have continuity.
_seed = {}
if "THROTTLE_PERCENT" in os.environ:
    _seed["throttle_percent"] = int(os.environ["THROTTLE_PERCENT"])
if "FULL_PERCENT" in os.environ:
    _seed["default_percent"] = int(os.environ["FULL_PERCENT"])
if "LINE_SPEED_MBIT" in os.environ:
    _seed["line_speed_mbit"] = int(os.environ["LINE_SPEED_MBIT"])
if _seed:
    storage.save_settings(_seed)

# ---------- State ----------------------------------------------------------

app = Flask(__name__)
_state_lock = threading.Lock()
_state = {
    "active_streams": 0,
    "stream_sessions": [],
    "decision": None,
    "last_applied": None,           # dict snapshot of last applied limit
    "last_change_ts": None,
}


# ---------- Apply loop -----------------------------------------------------

def _apply_decision(decision) -> None:
    """Push the resolver's decision to SABnzbd, but only if it changed."""
    snapshot = {
        "mode": decision.mode,
        "value": decision.value,
        "source": decision.source,
        "pause": decision.pause,
    }
    last = _state.get("last_applied")
    if last == snapshot:
        return

    if decision.pause:
        sabnzbd.pause()
    else:
        # If we were previously paused, make sure to resume before setting
        # a new limit, otherwise SABnzbd stays paused.
        if last and last.get("pause"):
            sabnzbd.resume()
        sabnzbd.set_speed(decision.mode, decision.value)

    _state["last_applied"] = snapshot
    _state["last_change_ts"] = time.time()
    log.info("apply -> %s (source: %s)",
             units.format_limit(decision.mode, decision.value, None),
             decision.source)


def _tick() -> None:
    """One resolution cycle: poll Jellyfin, recompute, apply if changed."""
    sessions = jellyfin.active_sessions(NZBDAV_PATH_PREFIX) if jellyfin.ENABLED else []
    settings = storage.load_settings()
    # If the user did not configure a line speed, fall back to whatever
    # SABnzbd has set for bandwidth_max so percent ↔ absolute math still
    # works out of the box.
    if not settings.get("line_speed_mbit"):
        detected = sabnzbd.get_max_speed_kbps()
        if detected:
            settings = {**settings, "_detected_line_speed_kbps": detected}
    rules    = storage.load_rules()
    decision = resolver.resolve(settings, rules, jellyfin_streams=len(sessions))

    with _state_lock:
        _state["active_streams"]  = len(sessions)
        _state["stream_sessions"] = [s.get("Id") or "?" for s in sessions]
        _state["decision"]        = decision

    _apply_decision(decision)


def _loop() -> None:
    while True:
        try:
            _tick()
        except Exception:
            log.exception("tick failed")
        time.sleep(JELLYFIN_POLL_INTERVAL)


# ---------- Webhook (legacy / optional) ------------------------------------

@app.route("/jellyfin", methods=["POST"])
def jellyfin_webhook():
    if WEBHOOK_TOKEN:
        provided = request.args.get("token") or request.headers.get("X-Webhook-Token")
        if provided != WEBHOOK_TOKEN:
            return ("unauthorized", 401)
    # Webhooks just trigger an immediate resolution cycle; we still use
    # polling as the source of truth.
    try:
        _tick()
    except Exception:
        log.exception("webhook-triggered tick failed")
    return ("", 204)


# ---------- API ------------------------------------------------------------

@app.route("/health")
def health():
    return jsonify(status="ok")


@app.route("/api/status")
def api_status():
    with _state_lock:
        decision = _state["decision"]
        out = {
            "active_streams":  _state["active_streams"],
            "stream_sessions": _state["stream_sessions"],
            "polling_enabled": jellyfin.ENABLED,
            "poll_interval_sec": JELLYFIN_POLL_INTERVAL,
        }
    settings = storage.load_settings()
    line_kbps = _effective_line_speed_kbps(settings)
    out["line_speed_kbps"]      = line_kbps
    out["line_speed_source"]    = "user" if settings.get("line_speed_mbit") else ("sabnzbd" if line_kbps else "fallback")
    if decision:
        out.update(
            current_mode=decision.mode,
            current_value=decision.value,
            current_source=decision.source,
            current_human=units.format_limit(decision.mode, decision.value, line_kbps),
        )
    return jsonify(out)


@app.route("/api/rules", methods=["GET", "POST"])
def api_rules():
    if request.method == "POST":
        rule = storage.add_rule(request.get_json(force=True))
        _tick()
        return jsonify(rule), 201
    return jsonify(storage.load_rules())


@app.route("/api/rules/<rid>", methods=["PUT", "DELETE"])
def api_rule(rid: str):
    if request.method == "DELETE":
        ok = storage.delete_rule(rid)
        _tick()
        return ("", 204 if ok else 404)
    rule = storage.update_rule(rid, request.get_json(force=True))
    _tick()
    return jsonify(rule) if rule else ("", 404)


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "POST":
        storage.save_settings(request.get_json(force=True))
        _tick()
    return jsonify(storage.load_settings())


# ---------- WebUI ----------------------------------------------------------

WEEKDAYS = [
    (1, "Mo"), (2, "Di"), (3, "Mi"), (4, "Do"),
    (5, "Fr"), (6, "Sa"), (7, "So"),
]

def _form_rule_from_request() -> dict:
    days = [int(d) for d in request.form.getlist("weekdays")]
    return {
        "name":     request.form.get("name", "").strip(),
        "enabled":  request.form.get("enabled") == "on",
        "weekdays": days,
        "start":    request.form.get("start", "00:00"),
        "end":      request.form.get("end", "23:59"),
        "mode":     request.form.get("mode", units.MODE_PERCENT),
        "value":    float(request.form.get("value", "100") or 100),
    }


def _effective_line_speed_kbps(settings: dict) -> float | None:
    """Compute the line speed that the resolver would use, for display."""
    if settings.get("line_speed_mbit"):
        return float(settings["line_speed_mbit"]) * 125.0
    return sabnzbd.get_max_speed_kbps()


@app.route("/")
def ui_index():
    settings = storage.load_settings()
    line_kbps = _effective_line_speed_kbps(settings)
    with _state_lock:
        decision = _state["decision"]
        active_streams = _state["active_streams"]
    return render_template(
        "status.html",
        decision=decision,
        active_streams=active_streams,
        settings=settings,
        line_kbps=line_kbps,
        units=units,
        polling_enabled=jellyfin.ENABLED,
    )


@app.route("/rules")
def ui_rules():
    return render_template(
        "rules.html",
        rules=storage.load_rules(),
        weekdays=WEEKDAYS,
        units=units,
    )


@app.route("/rules/new", methods=["GET", "POST"])
def ui_rule_new():
    if request.method == "POST":
        storage.add_rule(_form_rule_from_request())
        _tick()
        return redirect(url_for("ui_rules"))
    return render_template(
        "rule_edit.html",
        rule={"enabled": True, "weekdays": [1,2,3,4,5,6,7],
              "start": "00:00", "end": "23:59",
              "mode": units.MODE_PERCENT, "value": 100},
        weekdays=WEEKDAYS, units=units, is_new=True,
    )


@app.route("/rules/<rid>/edit", methods=["GET", "POST"])
def ui_rule_edit(rid: str):
    rules = {r["id"]: r for r in storage.load_rules()}
    rule = rules.get(rid)
    if not rule:
        return ("not found", 404)
    if request.method == "POST":
        storage.update_rule(rid, _form_rule_from_request())
        _tick()
        return redirect(url_for("ui_rules"))
    return render_template(
        "rule_edit.html", rule=rule,
        weekdays=WEEKDAYS, units=units, is_new=False,
    )


@app.route("/rules/<rid>/delete", methods=["POST"])
def ui_rule_delete(rid: str):
    storage.delete_rule(rid)
    _tick()
    return redirect(url_for("ui_rules"))


@app.route("/settings", methods=["GET", "POST"])
def ui_settings():
    if request.method == "POST":
        storage.save_settings({
            "line_speed_mbit":  int(request.form.get("line_speed_mbit", 0) or 0),
            "default_percent":  int(request.form.get("default_percent", 100) or 100),
            "throttle_percent": int(request.form.get("throttle_percent", 50) or 50),
        })
        _tick()
        return redirect(url_for("ui_settings"))
    settings = storage.load_settings()
    detected_kbps = sabnzbd.get_max_speed_kbps()
    detected_mbit = (detected_kbps * 8 / 1000) if detected_kbps else None
    return render_template(
        "settings.html",
        settings=settings,
        detected_mbit=detected_mbit,
        detected_kbps=detected_kbps,
    )


# ---------- Entrypoint -----------------------------------------------------

if __name__ == "__main__":
    log.info("sabthrottle starting on port %d", LISTEN_PORT)
    log.info("SABnzbd target: %s", sabnzbd.SABNZBD_URL)
    log.info("mode: %s", "polling" if jellyfin.ENABLED else "webhook-only")
    if NZBDAV_PATH_PREFIX:
        log.info("path filter active: %s", NZBDAV_PATH_PREFIX)

    threading.Thread(target=_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=LISTEN_PORT)
