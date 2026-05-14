"""Persistent storage for rules and settings.

Two JSON files inside CONFIG_DIR:

    rules.json     — list of time-based speed rules
    settings.json  — global settings (line speed, default percent, ...)

Writes are atomic (write to .tmp, then rename) so a crash mid-write
never leaves the user with a corrupted file.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from typing import Any

CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")

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


# ---------- Settings -------------------------------------------------------

DEFAULT_SETTINGS = {
    "line_speed_mbit": 0,        # 0 = unknown; absolute rules still work
    "default_percent": 100,      # baseline limit when no rule and no stream
    "throttle_percent": 50,      # what to apply during a Jellyfin stream
}

def load_settings() -> dict[str, Any]:
    with _lock:
        data = _read_json("settings.json", {})
    return {**DEFAULT_SETTINGS, **data}

def save_settings(settings: dict[str, Any]) -> None:
    with _lock:
        existing = _read_json("settings.json", {})
        existing.update(settings)
        _write_json("settings.json", existing)


# ---------- Rules ----------------------------------------------------------

def load_rules() -> list[dict[str, Any]]:
    with _lock:
        return _read_json("rules.json", [])

def save_rules(rules: list[dict[str, Any]]) -> None:
    with _lock:
        _write_json("rules.json", rules)

def add_rule(rule: dict[str, Any]) -> dict[str, Any]:
    rule = dict(rule)
    rule.setdefault("id", uuid.uuid4().hex[:8])
    rule.setdefault("created_at", int(time.time()))
    with _lock:
        rules = _read_json("rules.json", [])
        rules.append(rule)
        _write_json("rules.json", rules)
    return rule

def update_rule(rule_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
    with _lock:
        rules = _read_json("rules.json", [])
        for r in rules:
            if r.get("id") == rule_id:
                r.update(patch)
                _write_json("rules.json", rules)
                return r
    return None

def delete_rule(rule_id: str) -> bool:
    with _lock:
        rules = _read_json("rules.json", [])
        new_rules = [r for r in rules if r.get("id") != rule_id]
        if len(new_rules) == len(rules):
            return False
        _write_json("rules.json", new_rules)
        return True
