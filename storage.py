"""Persistent storage for rules, settings and runtime state.

Files inside CONFIG_DIR:

    rules.json     — per-target time-based speed rules
    settings.json  — global settings (flat key/value)
    state.json     — last applied limit per target (survives restarts so
                     we never leave a target paused after a container
                     restart, and never resume a pause we didn't cause)

Writes are atomic (write to .tmp, then rename) so a crash mid-write
never leaves the user with a corrupted file.

Schema v2 (this version) keeps SEPARATE rule sets per target:

    rules.json:   {"version": 2, "targets": {"sab": [...], "jd": [...]}}

A v1 rules.json (a bare list) is migrated on first load: all existing
rules become SABnzbd rules, which preserves v1 behaviour exactly.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from typing import Any

CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")

TARGETS = ("sab", "jd")

_lock = threading.Lock()


# ---------- Atomic JSON helpers --------------------------------------------

def _path(name: str) -> str:
    return os.path.join(CONFIG_DIR, name)

def _read_json(name: str, default: Any) -> Any:
    p = _path(name)
    if not os.path.exists(p):
        return default
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default

def _write_json(name: str, data: Any) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    p = _path(name)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, p)


# ---------- Settings --------------------------------------------------------

DEFAULT_SETTINGS = {
    # Real line speed in Mbit/s. 0 = unknown (fall back to the value
    # detected from SABnzbd's bandwidth_max, then to 1 Gbit/s).
    "line_speed_mbit": 0,

    # ---- Stream throttling (global bandwidth budget) ----
    # Total download budget (percent of line speed) shared by ALL
    # downloaders while at least one Jellyfin stream is playing.
    "stream_budget_percent": 50,
    # How the budget is split when several downloaders are active:
    #   "demand" — proportional to each downloader's measured speed
    #   "equal"  — simple 1/n split
    "split_mode": "demand",
    # Demand split: a downloader's share is based on its smoothed measured
    # speed multiplied by this factor, so it always has room to grow.
    "headroom_factor": 1.5,
    # Demand split: minimum assumed demand per downloader (KB/s), so a
    # downloader that is just starting up is never starved.
    "floor_kbps": 5120,
    # Demand split: EMA smoothing factor for measured speeds (0..1).
    # Higher = reacts faster, lower = smoother.
    "ema_alpha": 0.4,
    # Burst protection: while a stream is playing, park IDLE downloaders
    # at floor_kbps so a download that suddenly starts mid-tick cannot
    # briefly saturate the line before the next evaluation.
    "burst_protection": False,

    # ---- Per-target defaults (baseline when no rule matches) ----
    "sab_enabled": True,
    "sab_default_percent": 100,
    "jd_enabled": True,
    "jd_default_percent": 100,
}

# v1 -> v2 key renames
_SETTINGS_MIGRATIONS = {
    "throttle_percent": "stream_budget_percent",
    "default_percent":  "sab_default_percent",
}

# Only these keys are accepted from the API / UI (plus runtime-injected
# keys prefixed with "_", which are never persisted).
ALLOWED_SETTINGS_KEYS = set(DEFAULT_SETTINGS)


def _migrate_settings(data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    changed = False
    for old, new in _SETTINGS_MIGRATIONS.items():
        if old in data:
            if new not in data:
                data[new] = data.pop(old)
            else:
                data.pop(old)
            changed = True
    return data, changed


def load_settings() -> dict[str, Any]:
    with _lock:
        data = _read_json("settings.json", {})
        data, changed = _migrate_settings(data)
        if changed:
            _write_json("settings.json", data)
    return {**DEFAULT_SETTINGS, **data}


def save_settings(settings: dict[str, Any]) -> None:
    clean = {k: v for k, v in settings.items() if k in ALLOWED_SETTINGS_KEYS}
    if not clean:
        return
    with _lock:
        existing = _read_json("settings.json", {})
        existing, _ = _migrate_settings(existing)
        existing.update(clean)
        _write_json("settings.json", existing)


# ---------- Rules ------------------------------------------------------------

def _empty_rules() -> dict[str, Any]:
    return {"version": 2, "targets": {t: [] for t in TARGETS}}


def _migrate_rules(data: Any) -> tuple[dict[str, Any], bool]:
    """v1 (bare list) -> v2 (per-target dict). v1 rules become SAB rules."""
    if isinstance(data, list):
        out = _empty_rules()
        out["targets"]["sab"] = data
        return out, True
    if not isinstance(data, dict):
        return _empty_rules(), True
    targets = data.get("targets") or {}
    changed = False
    for t in TARGETS:
        if t not in targets:
            targets[t] = []
            changed = True
    data["targets"] = targets
    if data.get("version") != 2:
        data["version"] = 2
        changed = True
    return data, changed


def _load_rules_locked() -> dict[str, Any]:
    data = _read_json("rules.json", _empty_rules())
    data, changed = _migrate_rules(data)
    if changed:
        _write_json("rules.json", data)
    return data


def load_rules(target: str | None = None) -> dict[str, list] | list:
    """Without `target`: {"sab": [...], "jd": [...]}. With: that list."""
    with _lock:
        data = _load_rules_locked()
    if target is not None:
        return data["targets"].get(target, [])
    return data["targets"]


def add_rule(target: str, rule: dict[str, Any]) -> dict[str, Any]:
    if target not in TARGETS:
        raise ValueError(f"unknown target: {target}")
    rule = dict(rule)
    rule.setdefault("id", uuid.uuid4().hex[:8])
    rule.setdefault("created_at", int(time.time()))
    with _lock:
        data = _load_rules_locked()
        data["targets"][target].append(rule)
        _write_json("rules.json", data)
    return rule


def find_rule(rule_id: str) -> tuple[str, dict[str, Any]] | None:
    """Return (target, rule) for an id, searching all targets."""
    with _lock:
        data = _load_rules_locked()
    for t in TARGETS:
        for r in data["targets"][t]:
            if r.get("id") == rule_id:
                return t, r
    return None


def update_rule(rule_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
    with _lock:
        data = _load_rules_locked()
        for t in TARGETS:
            for r in data["targets"][t]:
                if r.get("id") == rule_id:
                    r.update(patch)
                    _write_json("rules.json", data)
                    return r
    return None


def delete_rule(rule_id: str) -> bool:
    with _lock:
        data = _load_rules_locked()
        for t in TARGETS:
            rules = data["targets"][t]
            new_rules = [r for r in rules if r.get("id") != rule_id]
            if len(new_rules) != len(rules):
                data["targets"][t] = new_rules
                _write_json("rules.json", data)
                return True
    return False


# ---------- Runtime state (last applied limit per target) -------------------

def load_state() -> dict[str, Any]:
    with _lock:
        return _read_json("state.json", {})


def save_state(state: dict[str, Any]) -> None:
    with _lock:
        _write_json("state.json", state)
