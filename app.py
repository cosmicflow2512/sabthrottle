"""sabthrottle — automatic download throttling driven by Jellyfin
playback + per-downloader time-based speed rules.

Targets:
    sab — SABnzbd (always required)
    jd  — JDownloader 2 via MyJDownloader (optional, enabled when
          MYJD_EMAIL / MYJD_PASSWORD / MYJD_DEVICE are configured)

Entry point. Wires together storage, Jellyfin polling, the rule
resolver, the per-target clients and the WebUI.
"""
from __future__ import annotations

import logging
import os
import threading
import time

from flask import Flask, jsonify, redirect, render_template, request, url_for

import jdownloader
import jellyfin
import resolver
import sabnzbd
import storage
import units

# ---------- Config ----------------------------------------------------------

LISTEN_PORT            = int(os.environ.get("LISTEN_PORT", "6811"))
LOG_LEVEL              = os.environ.get("LOG_LEVEL", "INFO").upper()
JELLYFIN_POLL_INTERVAL = int(os.environ.get("JELLYFIN_POLL_INTERVAL_SEC", "15"))
WEBHOOK_TOKEN          = os.environ.get("WEBHOOK_TOKEN", "")
NZBDAV_PATH_PREFIX     = os.environ.get("NZBDAV_PATH_PREFIX", "")

# How many consecutive idle ticks before a target leaves the budget split.
# Bridges short gaps (captcha waits, reconnects, single failed polls).
INACTIVE_TICKS_THRESHOLD = 2

# If Jellyfin polling fails, keep the last known stream count for this many
# ticks before assuming 0 — a transient Jellyfin error mid-stream must not
# briefly unthrottle the downloaders.
STALE_STREAM_TICKS = 4

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("sabthrottle")

# Seed settings file from env vars on first start so existing users have
# continuity. Legacy names (THROTTLE_PERCENT/FULL_PERCENT) map onto the
# v2 keys via the storage migration table.
_seed = {}
if "THROTTLE_PERCENT" in os.environ:
    _seed["stream_budget_percent"] = int(os.environ["THROTTLE_PERCENT"])
if "STREAM_BUDGET_PERCENT" in os.environ:
    _seed["stream_budget_percent"] = int(os.environ["STREAM_BUDGET_PERCENT"])
if "FULL_PERCENT" in os.environ:
    _seed["sab_default_percent"] = int(os.environ["FULL_PERCENT"])
if "LINE_SPEED_MBIT" in os.environ:
    _seed["line_speed_mbit"] = int(os.environ["LINE_SPEED_MBIT"])
if _seed:
    storage.save_settings(_seed)

TARGET_LABELS = {"sab": "SABnzbd", "jd": "JDownloader"}

# ---------- State ------------------------------------------------------------

app = Flask(__name__)

_tick_lock  = threading.Lock()   # serialises ticks (loop / webhook / UI)
_state_lock = threading.Lock()   # guards the snapshot below

_state: dict = {
    "active_streams": 0,
    "stream_sessions": [],
    "stale_stream_ticks": 0,
    "decisions": {},                 # target -> TargetDecision
    "last_change_ts": None,
}

# Per-target runtime tracking for the demand split (not persisted).
_runtime: dict[str, dict] = {
    t: {"ema_kbps": 0.0, "active": False, "inactive_ticks": 999,
        "raw_kbps": 0.0, "reachable": None}
    for t in storage.TARGETS
}

# Last applied limit per target — persisted to /config/state.json so a
# container restart never leaves a target paused, and we never "resume"
# a pause we did not cause ourselves.
_last_applied: dict[str, dict] = storage.load_state()


def _target_enabled(settings: dict, key: str) -> bool:
    if key == "jd" and not jdownloader.ENABLED:
        return False
    return bool(settings.get(f"{key}_enabled", True))


# ---------- Apply -------------------------------------------------------------

def _apply_decision(key: str, decision) -> None:
    """Push one target's decision to its client, but only if it changed."""
    snapshot = {
        "mode": decision.mode,
        "value": decision.value,
        "source": decision.source,
        "pause": decision.pause,
    }
    if _last_applied.get(key) == snapshot:
        return

    was_paused_by_us = bool((_last_applied.get(key) or {}).get("pause"))

    ok = True
    if key == "sab":
        if decision.pause:
            ok = sabnzbd.pause()
        else:
            if was_paused_by_us:
                ok = sabnzbd.resume() and ok
            ok = sabnzbd.set_speed(decision.mode, decision.value) and ok
    elif key == "jd":
        if decision.pause:
            ok = jdownloader.pause(True)
        else:
            if was_paused_by_us:
                ok = jdownloader.pause(False) and ok
            if decision.mode == units.MODE_PERCENT and decision.value >= 100:
                # 100 % of an (possibly unknown) line = no artificial cap:
                # disable JD's limiter entirely instead of writing a huge number.
                ok = jdownloader.clear_limit() and ok
            else:
                ok = jdownloader.set_limit_kbps(decision.kbps) and ok

    if not ok:
        # Do not record the snapshot — retry on the next tick.
        log.warning("[%s] apply failed, will retry next tick", key)
        return

    _last_applied[key] = snapshot
    storage.save_state(_last_applied)
    with _state_lock:
        _state["last_change_ts"] = time.time()
    log.info("[%s] apply -> %s (source: %s)",
             key,
             units.format_limit(decision.mode, decision.value, None),
             decision.source)


# ---------- Tick --------------------------------------------------------------

def _update_runtime(key: str, settings: dict) -> None:
    """Measure activity + speed for one target and update EMA/hysteresis."""
    rt = _runtime[key]
    if key == "sab":
        active_raw, speed = sabnzbd.get_activity()
        rt["reachable"] = True   # failures already yield (False, 0.0)
    else:
        active_raw, speed = jdownloader.get_activity()
        rt["reachable"] = jdownloader.is_reachable()

    alpha = min(1.0, max(0.05, float(settings.get("ema_alpha", 0.4))))
    rt["raw_kbps"] = speed
    rt["ema_kbps"] = alpha * speed + (1 - alpha) * rt["ema_kbps"]

    if active_raw:
        rt["active"] = True
        rt["inactive_ticks"] = 0
    else:
        rt["inactive_ticks"] += 1
        if rt["inactive_ticks"] >= INACTIVE_TICKS_THRESHOLD:
            rt["active"] = False


def _poll_streams() -> int:
    """Poll Jellyfin; on transient failure keep the last known count for a
    few ticks instead of unthrottling mid-stream."""
    if not jellyfin.ENABLED:
        return _state["active_streams"]   # webhook mode: keep last known

    sessions = jellyfin.fetch_sessions()
    if sessions is None:
        with _state_lock:
            _state["stale_stream_ticks"] += 1
            if _state["stale_stream_ticks"] <= STALE_STREAM_TICKS:
                log.warning("Jellyfin poll failed — keeping last known "
                            "stream count (%d)", _state["active_streams"])
                return _state["active_streams"]
            _state["active_streams"] = 0
            _state["stream_sessions"] = []
        return 0

    filtered = jellyfin.filter_active(sessions, NZBDAV_PATH_PREFIX)
    with _state_lock:
        _state["stale_stream_ticks"] = 0
        _state["active_streams"]  = len(filtered)
        _state["stream_sessions"] = [s.get("Id") or "?" for s in filtered]
    return len(filtered)


def _tick() -> None:
    """One resolution cycle: poll Jellyfin + targets, resolve, apply."""
    with _tick_lock:
        settings = storage.load_settings()
        # If the user did not configure a line speed, fall back to whatever
        # SABnzbd has set for bandwidth_max so percent math works out of
        # the box.
        if not settings.get("line_speed_mbit"):
            detected = sabnzbd.get_max_speed_kbps()
            if detected:
                settings = {**settings, "_detected_line_speed_kbps": detected}

        streams = _poll_streams()
        rules   = storage.load_rules()

        inputs = []
        for key in storage.TARGETS:
            if not _target_enabled(settings, key):
                continue
            _update_runtime(key, settings)
            rt = _runtime[key]
            inputs.append(resolver.TargetInput(
                key=key,
                rules=rules.get(key, []),
                default_percent=float(settings.get(f"{key}_default_percent", 100)),
                active=rt["active"],
                ema_kbps=rt["ema_kbps"],
            ))

        decisions = resolver.resolve_all(settings, inputs, jellyfin_streams=streams)

        with _state_lock:
            _state["decisions"] = decisions

        for key, decision in decisions.items():
            _apply_decision(key, decision)


def _loop() -> None:
    while True:
        try:
            _tick()
        except Exception:
            log.exception("tick failed")
        time.sleep(JELLYFIN_POLL_INTERVAL)


# ---------- Webhook (optional, instant reaction) ------------------------------

@app.route("/jellyfin", methods=["POST"])
def jellyfin_webhook():
    if WEBHOOK_TOKEN:
        provided = request.args.get("token") or request.headers.get("X-Webhook-Token")
        if provided != WEBHOOK_TOKEN:
            return ("unauthorized", 401)
    # Webhooks just trigger an immediate resolution cycle; polling stays
    # the source of truth.
    try:
        _tick()
    except Exception:
        log.exception("webhook-triggered tick failed")
    return ("", 204)


# ---------- API ----------------------------------------------------------------

@app.route("/health")
def health():
    return jsonify(status="ok")


def _decision_dict(d, line_kbps) -> dict:
    return {
        "mode": d.mode,
        "value": d.value,
        "source": d.source,
        "kbps": round(d.kbps, 1),
        "human": units.format_limit(d.mode, d.value, line_kbps),
        "pause": d.pause,
        "in_split": d.in_split,
        "parked": d.parked,
        "base": {
            "mode": d.base.mode, "value": d.base.value,
            "source": d.base.source, "kbps": round(d.base.kbps, 1),
        },
        "stream_share_kbps": round(d.stream_share.kbps, 1) if d.stream_share else None,
    }


@app.route("/api/status")
def api_status():
    settings  = storage.load_settings()
    line_kbps = _effective_line_speed_kbps(settings)
    with _state_lock:
        decisions = dict(_state["decisions"])
        out = {
            "active_streams":   _state["active_streams"],
            "stream_sessions":  _state["stream_sessions"],
            "polling_enabled":  jellyfin.ENABLED,
            "poll_interval_sec": JELLYFIN_POLL_INTERVAL,
            "line_speed_kbps":  line_kbps,
            "line_speed_source": ("user" if settings.get("line_speed_mbit")
                                  else ("sabnzbd" if line_kbps else "fallback")),
            "targets": {},
        }
    for key, d in decisions.items():
        rt = _runtime[key]
        out["targets"][key] = {
            **_decision_dict(d, line_kbps),
            "label": TARGET_LABELS[key],
            "active": rt["active"],
            "current_speed_kbps": round(rt["raw_kbps"], 1),
            "smoothed_speed_kbps": round(rt["ema_kbps"], 1),
            "reachable": rt["reachable"],
        }
    return jsonify(out)


@app.route("/api/rules", methods=["GET", "POST"])
def api_rules():
    target = request.args.get("target", "sab")
    if target not in storage.TARGETS:
        return jsonify(error=f"unknown target {target!r}"), 400
    if request.method == "POST":
        body = request.get_json(force=True)
        # target may also be supplied in the body; query param wins
        body.pop("target", None)
        rule = storage.add_rule(target, body)
        _tick()
        return jsonify(rule), 201
    return jsonify(storage.load_rules(target))


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


# ---------- WebUI ---------------------------------------------------------------

WEEKDAYS = [
    (1, "Mon"), (2, "Tue"), (3, "Wed"), (4, "Thu"),
    (5, "Fri"), (6, "Sat"), (7, "Sun"),
]


def _form_rule_from_request() -> dict:
    days = [int(d) for d in request.form.getlist("weekdays")]
    mode = request.form.get("mode", units.MODE_PERCENT)
    value = float(request.form.get("value", "100") or 100)
    if mode == units.MODE_PERCENT:
        value = min(100.0, max(1.0, value))
    elif mode != units.MODE_PAUSE:
        value = max(1.0, value)
    return {
        "name":     request.form.get("name", "").strip(),
        "enabled":  request.form.get("enabled") == "on",
        "weekdays": days,
        "start":    request.form.get("start", "00:00"),
        "end":      request.form.get("end", "23:59"),
        "mode":     mode,
        "value":    value,
    }


def _effective_line_speed_kbps(settings: dict) -> float | None:
    """Compute the line speed that the resolver would use, for display."""
    if settings.get("line_speed_mbit"):
        return float(settings["line_speed_mbit"]) * 125.0
    return sabnzbd.get_max_speed_kbps()


def _ui_target() -> str:
    t = request.args.get("target", "sab")
    return t if t in storage.TARGETS else "sab"


@app.route("/")
def ui_index():
    settings  = storage.load_settings()
    line_kbps = _effective_line_speed_kbps(settings)
    with _state_lock:
        decisions      = dict(_state["decisions"])
        active_streams = _state["active_streams"]
    runtime = {k: dict(v) for k, v in _runtime.items()}
    return render_template(
        "status.html",
        decisions=decisions,
        runtime=runtime,
        labels=TARGET_LABELS,
        active_streams=active_streams,
        settings=settings,
        line_kbps=line_kbps,
        units=units,
        polling_enabled=jellyfin.ENABLED,
        jd_configured=jdownloader.ENABLED,
    )


@app.route("/rules")
def ui_rules():
    target = _ui_target()
    return render_template(
        "rules.html",
        rules=storage.load_rules(target),
        target=target,
        labels=TARGET_LABELS,
        weekdays=WEEKDAYS,
        units=units,
        jd_configured=jdownloader.ENABLED,
    )


@app.route("/rules/new", methods=["GET", "POST"])
def ui_rule_new():
    target = _ui_target()
    if request.method == "POST":
        storage.add_rule(target, _form_rule_from_request())
        _tick()
        return redirect(url_for("ui_rules", target=target))
    return render_template(
        "rule_edit.html",
        rule={"enabled": True, "weekdays": [1, 2, 3, 4, 5, 6, 7],
              "start": "00:00", "end": "23:59",
              "mode": units.MODE_PERCENT, "value": 100},
        target=target, labels=TARGET_LABELS,
        weekdays=WEEKDAYS, units=units, is_new=True,
    )


@app.route("/rules/<rid>/edit", methods=["GET", "POST"])
def ui_rule_edit(rid: str):
    found = storage.find_rule(rid)
    if not found:
        return ("not found", 404)
    target, rule = found
    if request.method == "POST":
        storage.update_rule(rid, _form_rule_from_request())
        _tick()
        return redirect(url_for("ui_rules", target=target))
    return render_template(
        "rule_edit.html", rule=rule,
        target=target, labels=TARGET_LABELS,
        weekdays=WEEKDAYS, units=units, is_new=False,
    )


@app.route("/rules/<rid>/delete", methods=["POST"])
def ui_rule_delete(rid: str):
    found = storage.find_rule(rid)
    target = found[0] if found else "sab"
    storage.delete_rule(rid)
    _tick()
    return redirect(url_for("ui_rules", target=target))


@app.route("/settings", methods=["GET", "POST"])
def ui_settings():
    if request.method == "POST":
        storage.save_settings({
            "line_speed_mbit":       int(request.form.get("line_speed_mbit", 0) or 0),
            "stream_budget_percent": min(100, max(1, int(request.form.get("stream_budget_percent", 50) or 50))),
            "split_mode":            ("equal" if request.form.get("split_mode") == "equal" else "demand"),
            "burst_protection":      request.form.get("burst_protection") == "on",
            "headroom_factor":       max(1.0, float(request.form.get("headroom_factor", 1.5) or 1.5)),
            "floor_kbps":            max(64, int(request.form.get("floor_kbps", 5120) or 5120)),
            "ema_alpha":             min(1.0, max(0.05, float(request.form.get("ema_alpha", 0.4) or 0.4))),
            "sab_enabled":           request.form.get("sab_enabled") == "on",
            "sab_default_percent":   min(100, max(1, int(request.form.get("sab_default_percent", 100) or 100))),
            "jd_enabled":            request.form.get("jd_enabled") == "on",
            "jd_default_percent":    min(100, max(1, int(request.form.get("jd_default_percent", 100) or 100))),
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
        labels=TARGET_LABELS,
        jd_configured=jdownloader.ENABLED,
    )


# ---------- Entrypoint ------------------------------------------------------------

if __name__ == "__main__":
    log.info("sabthrottle starting on port %d", LISTEN_PORT)
    log.info("SABnzbd target: %s", sabnzbd.SABNZBD_URL)
    log.info("JDownloader target: %s",
             f"device {os.environ.get('MYJD_DEVICE')!r} via MyJDownloader"
             if jdownloader.ENABLED else "not configured")
    log.info("mode: %s", "polling" if jellyfin.ENABLED else "webhook-only")
    if NZBDAV_PATH_PREFIX:
        log.info("path filter active: %s", NZBDAV_PATH_PREFIX)

    threading.Thread(target=_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=LISTEN_PORT)
