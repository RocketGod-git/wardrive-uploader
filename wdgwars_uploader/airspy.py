from __future__ import annotations

import hashlib
import io
import logging
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Callable

import requests

from .config import config_dir

log = logging.getLogger("wdgwars.airspy")
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

ADSB_SPY_URL = "https://airspy.com/?ddownload=6057"
ADSB_SPY_ZIP_SHA256 = "e195bcf3b8bcc1e335e45a9265304808e411694848d0d516d2b4c10f30dac282"
ADSB_SPY_EXE = "airspy_adsb.exe"

DUMP1090_RAW_IN_PORT = 30001
_PUSH_FORMAT = "avr-strict"

_RELAUNCH_BACKOFF = 15.0
_RECONNECT_GRACE_LAUNCH = 90.0
_RECONNECT_GRACE_LOST = 15.0
_LOG_MAX_BYTES = 20 * 1024 * 1024

_proc: subprocess.Popen | None = None
_log_fp: Any = None
_launch_lock = threading.Lock()
_last_launch_attempt = 0.0
_launched_ts = 0.0
_last_connected_ts = 0.0
_last_error = ""
_restarts = 0
_connected = False


def tools_dir() -> Path:
    d = config_dir() / "tools" / "airspy_adsb"
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_path() -> Path:
    return tools_dir() / "airspy_adsb.log"


def find_airspy_adsb(configured_exe: str = "") -> str | None:
    import shutil
    if configured_exe and Path(configured_exe).exists():
        return configured_exe
    search = [tools_dir(), config_dir() / "tools"]
    if getattr(sys, "frozen", False):
        search.append(Path(getattr(sys, "_MEIPASS", ".")) / "tools")
    else:
        search.append(Path(__file__).resolve().parent.parent / "tools")
    for root in search:
        for cand in root.rglob(ADSB_SPY_EXE):
            return str(cand)
    return shutil.which("airspy_adsb") or shutil.which(ADSB_SPY_EXE)


def install_airspy_adsb(progress: Callable[[str], None] | None = None) -> str:
    def say(m: str) -> None:
        log.info("ADS-B SPY install: %s", m)
        if progress:
            progress(m)

    existing = find_airspy_adsb()
    if existing:
        say("already installed")
        return existing
    say("downloading ADS-B SPY (~0.7 MB)...")
    resp = requests.get(ADSB_SPY_URL, timeout=120,
                        headers={"User-Agent": "Mozilla/5.0 (WDGWars Uploader)"})
    resp.raise_for_status()
    digest = hashlib.sha256(resp.content).hexdigest()
    if digest != ADSB_SPY_ZIP_SHA256:
        raise RuntimeError(
            f"ADS-B SPY download failed integrity check (got {digest[:12]}..., expected "
            f"{ADSB_SPY_ZIP_SHA256[:12]}...). The vendor may have published a new build; "
            "verify it, then update ADSB_SPY_ZIP_SHA256 in airspy.py.")
    say("verified; extracting...")
    dest = tools_dir()
    with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
        names = z.namelist()
        if ADSB_SPY_EXE not in names:
            raise RuntimeError(f"{ADSB_SPY_EXE} not in the downloaded zip ({names})")
        z.extractall(dest)
    exe = find_airspy_adsb()
    if not exe:
        raise RuntimeError(f"{ADSB_SPY_EXE} not found after extraction")
    say("installed")
    return exe


def is_running() -> bool:
    return _proc is not None and _proc.poll() is None


def _valid_gain(gain: Any) -> int | None:
    if gain is None or str(gain).strip().lower() in ("", "auto"):
        return None
    try:
        g = int(float(gain))
    except (TypeError, ValueError):
        return None
    return g if 0 <= g <= 21 else None


def build_args(cfg: dict[str, Any], raw_in_port: int = DUMP1090_RAW_IN_PORT) -> list[str]:
    args = ["-c", f"localhost:{int(raw_in_port)}:{_PUSH_FORMAT}"]
    extra = str(cfg.get("airspy_adsb_args") or "").split()
    args += extra
    g = _valid_gain(cfg.get("airspy_gain"))
    if g is not None and "-g" not in extra:
        args += ["-g", str(g)]
    if cfg.get("airspy_bias_tee") and "-b" not in extra:
        args.append("-b")
    return args


def _kill_strays() -> None:
    global _proc
    if sys.platform == "win32":
        try:
            subprocess.run(["taskkill", "/F", "/IM", ADSB_SPY_EXE],
                           creationflags=_NO_WINDOW, capture_output=True, timeout=10)
        except Exception:
            pass
    _proc = None


def _netstat_tcp() -> list[tuple[str, str, str, int]]:
    if sys.platform != "win32":
        return []
    try:
        res = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True,
                             timeout=15, creationflags=_NO_WINDOW)
    except Exception:
        return []
    rows: list[tuple[str, str, str, int]] = []
    for line in (res.stdout or "").splitlines():
        parts = line.split()
        if len(parts) == 5 and parts[0].upper() == "TCP":
            try:
                rows.append((parts[1], parts[2], parts[3].upper(), int(parts[4])))
            except ValueError:
                continue
    return rows


def _connection_state(raw_in_port: int) -> tuple[bool, bool]:
    pid = _proc.pid if is_running() else -1
    suffix = f":{int(raw_in_port)}"
    listening = False
    connected = False
    for local, foreign, state, owner in _netstat_tcp():
        if state == "LISTENING" and local.endswith(suffix):
            listening = True
        elif state == "ESTABLISHED" and foreign.endswith(suffix) and owner == pid:
            connected = True
    return listening, connected


def _launch(exe: str, args: list[str]) -> bool:
    global _proc, _log_fp, _launched_ts, _last_error
    try:
        if _log_fp:
            try:
                _log_fp.close()
            except Exception:
                pass
        _log_fp = open(log_path(), "w", encoding="utf-8", errors="replace")
        cmd = [exe, *args]
        log.info("launching ADS-B SPY: %s", " ".join(cmd))
        _proc = subprocess.Popen(cmd, cwd=str(Path(exe).parent), creationflags=_NO_WINDOW,
                                 stdin=subprocess.DEVNULL, stdout=_log_fp, stderr=subprocess.STDOUT)
        _launched_ts = time.time()
        _last_error = ""
        return True
    except Exception as e:
        _last_error = f"launch failed: {e}"
        log.warning("ADS-B SPY %s", _last_error)
        return False


def stop() -> None:
    global _proc, _log_fp, _connected
    if is_running():
        try:
            _proc.terminate()
            try:
                _proc.wait(5)
            except subprocess.TimeoutExpired:
                _proc.kill()
        except Exception:
            pass
    _proc = None
    _connected = False
    if _log_fp:
        try:
            _log_fp.close()
        except Exception:
            pass
        _log_fp = None


def ensure_decoder(cfg: dict[str, Any], raw_in_port: int = DUMP1090_RAW_IN_PORT) -> bool:
    global _last_launch_attempt, _last_connected_ts, _last_error, _restarts, _connected
    with _launch_lock:
        now = time.time()
        listening, connected = _connection_state(raw_in_port)
        if is_running():
            if connected:
                _connected = True
                _last_connected_ts = now
                return True
            _connected = False
            if _last_connected_ts:
                since, grace, why = _last_connected_ts, _RECONNECT_GRACE_LOST, "lost its connection to"
            else:
                since, grace, why = _launched_ts, _RECONNECT_GRACE_LAUNCH, "never connected to"
            try:
                big = log_path().stat().st_size > _LOG_MAX_BYTES
            except OSError:
                big = False
            if big:
                log.info("ADS-B SPY log over %d MB; restarting the decoder", _LOG_MAX_BYTES // (1024 * 1024))
            elif not (listening and now - since >= grace):
                return True
            else:
                log.warning("ADS-B SPY %s dump1090 raw-in :%d (%.0f s); restarting it",
                            why, raw_in_port, now - since)
                _last_error = "not connected to dump1090; restarted"
            _restarts += 1
            stop()
            _last_launch_attempt = 0.0
        else:
            if _proc is not None and _proc.poll() is not None:
                _last_error = f"decoder exited with code {_proc.poll()}"
                log.warning("ADS-B SPY %s", _last_error)
        if not listening:
            _last_error = f"waiting for dump1090 to listen on :{raw_in_port}"
            return False
        if now - _last_launch_attempt < _RELAUNCH_BACKOFF:
            return False
        exe = find_airspy_adsb(str(cfg.get("airspy_adsb_exe") or ""))
        if not exe:
            _last_error = "ADS-B SPY not installed"
            return False
        _last_launch_attempt = now
        _kill_strays()
        _last_connected_ts = 0.0
        return _launch(exe, build_args(cfg, raw_in_port))


def status(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = cfg or {}
    exe = find_airspy_adsb(str(cfg.get("airspy_adsb_exe") or ""))
    return {
        "installed": bool(exe),
        "exe": exe or "",
        "running": is_running(),
        "pid": _proc.pid if is_running() else None,
        "connected": bool(_connected and is_running()),
        "error": _last_error,
        "restarts": _restarts,
        "log": str(log_path()),
    }
