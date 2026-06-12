"""JDownloader 2 client via the MyJDownloader remote API (myjdapi).

    set_limit_kbps(kbps)     — enable the speed limiter at an absolute KB/s
    clear_limit()            — disable the speed limiter (unlimited)
    pause(True/False)        — pause / resume downloads
    get_activity()           — (is_active, current_speed_kbps) for the
                               demand-based budget split

Implementation notes (verified against the official JDownloader source,
org.jdownloader.settings.GeneralSettings):

    * `DownloadSpeedLimit`        — config key, unit is BYTES per second
    * `DownloadSpeedLimitEnabled` — config key, the limiter on/off switch

Both are set through the remote `/config/set` endpoint. Setting only the
value without the enabled flag has no effect, which is why both keys are
always written together.

Pausing in JDownloader does NOT drop connections — it limits the speed to
the configured PauseSpeed (10 KB/s by default) so downloads resume
instantly. That behaviour is ideal for a throttling tool.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

log = logging.getLogger("sabthrottle.jdownloader")

MYJD_EMAIL    = os.environ.get("MYJD_EMAIL", "")
MYJD_PASSWORD = os.environ.get("MYJD_PASSWORD", "")
MYJD_DEVICE   = os.environ.get("MYJD_DEVICE", "")
MYJD_APP_KEY  = os.environ.get("MYJD_APP_KEY", "sabthrottle")

ENABLED = bool(MYJD_EMAIL and MYJD_PASSWORD and MYJD_DEVICE)

_GENERAL_SETTINGS = "org.jdownloader.settings.GeneralSettings"
_STORAGE          = "null"   # "null" = default storage (per myjdapi docs)

_RECONNECT_COOLDOWN = 60     # seconds to wait after a failed connect

_lock = threading.Lock()
_conn: dict[str, Any] = {"jd": None, "device": None, "failed_at": 0.0}


def _connect_locked():
    """(Re)connect to MyJDownloader. Must be called with _lock held.
    Raises on failure."""
    import myjdapi

    jd = myjdapi.Myjdapi()
    jd.set_app_key(MYJD_APP_KEY)
    jd.connect(MYJD_EMAIL, MYJD_PASSWORD)
    jd.update_devices()
    device = jd.get_device(MYJD_DEVICE)
    _conn["jd"] = jd
    _conn["device"] = device
    log.info("connected to MyJDownloader device %r", MYJD_DEVICE)


def _device():
    """Return a connected device or None (with cooldown after failures)."""
    if not ENABLED:
        return None
    with _lock:
        if _conn["device"] is not None:
            return _conn["device"]
        if time.time() - _conn["failed_at"] < _RECONNECT_COOLDOWN:
            return None
        try:
            _connect_locked()
            return _conn["device"]
        except Exception as e:
            _conn["failed_at"] = time.time()
            _conn["jd"] = None
            _conn["device"] = None
            log.error("MyJDownloader connect failed: %s", e)
            return None


def _drop_connection() -> None:
    with _lock:
        _conn["jd"] = None
        _conn["device"] = None
        _conn["failed_at"] = time.time()


def _call(fn, *args, **kwargs):
    """Run a device call; on failure drop the session so the next tick
    reconnects (MyJD session tokens expire). Returns None on failure."""
    device = _device()
    if device is None:
        return None
    try:
        return fn(device, *args, **kwargs)
    except Exception as e:
        log.warning("JDownloader call failed (%s) — dropping session", e)
        _drop_connection()
        return None


# ---------- Public API ------------------------------------------------------

def set_limit_kbps(kbps: float) -> bool:
    """Enable the speed limiter at `kbps` (decimal KB/s)."""
    bps = max(1, int(kbps * 1000))

    def _do(device):
        device.config.set(_GENERAL_SETTINGS, _STORAGE, "DownloadSpeedLimit", bps)
        device.config.set(_GENERAL_SETTINGS, _STORAGE, "DownloadSpeedLimitEnabled", True)
        return True

    ok = bool(_call(_do))
    if ok:
        log.info("JDownloader speed limit -> %d B/s", bps)
    return ok


def clear_limit() -> bool:
    """Disable the speed limiter (unlimited)."""
    def _do(device):
        device.config.set(_GENERAL_SETTINGS, _STORAGE, "DownloadSpeedLimitEnabled", False)
        return True

    ok = bool(_call(_do))
    if ok:
        log.info("JDownloader speed limit disabled (unlimited)")
    return ok


def pause(value: bool) -> bool:
    """Pause (True) or resume (False) downloads. JDownloader's pause keeps
    connections alive at PauseSpeed, so resuming is instant."""
    def _do(device):
        device.downloadcontroller.pause_downloads(value)
        return True

    ok = bool(_call(_do))
    if ok:
        log.info("JDownloader %s", "paused" if value else "resumed")
    return ok


def get_activity() -> tuple[bool, float]:
    """Return (is_active, current_speed_kbps).

    Active means the download controller reports RUNNING — i.e. JDownloader
    has work and is downloading (or about to, e.g. waiting on a reconnect).
    Speed is the aggregated current download rate, used by the demand-based
    budget split.

    Returns (False, 0.0) when JDownloader is unreachable; the caller's
    hysteresis keeps a recently-active target in the split for a couple of
    ticks, so a single failed poll does not cause a limit jump.
    """
    def _do(device):
        state = device.downloadcontroller.get_current_state()
        speed_bps = device.downloadcontroller.get_speed_in_bytes()
        return state, speed_bps

    result = _call(_do)
    if result is None:
        return False, 0.0
    state, speed_bps = result
    state_str = str(state or "").upper()
    speed_kbps = float(speed_bps or 0) / 1000.0
    active = "RUNNING" in state_str or speed_kbps > 50
    return active, speed_kbps


def is_reachable() -> bool:
    return _device() is not None
