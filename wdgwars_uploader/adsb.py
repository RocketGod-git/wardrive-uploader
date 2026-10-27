"""ADS-B source: produce a dump1090-shape ``aircraft.json`` object from whichever
radio is available.

``adsb_source`` selects between two radios that yield the same shape, so nothing
downstream depends on which one is in use:

* ``rtlsdr`` - decoded on the PC by dump1090 (this module).
* ``mayhem`` - a PortaPack/HackRF decodes on-device and its log is read back
  over the CDC console (:mod:`wdgwars_uploader.mayhem`).

For the RTL-SDR path this module finds, installs on demand and launches the
all-in-one **gvanem/Dump1090** Windows build. That build is HTTP-serve-only (no
``--write-json``), so aircraft are polled from ``/data/aircraft.json`` on :8080
rather than read from a file. It also requires the RTL-SDR to be bound to the
WinUSB driver (see :mod:`wdgwars_uploader.prereq`); without it dump1090 runs,
reports no device, and the aircraft list stays empty.
"""

from __future__ import annotations

import hashlib
import io
import json
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

log = logging.getLogger("wdgwars.adsb")
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# All-in-one Windows dump1090 (librtlsdr statically linked; serves tar1090 JSON).
DUMP1090_ZIP_URL = "https://github.com/gvanem/Dump1090/releases/download/v1.0.0/Dump1090-for-Windows-1.0.0-x64.zip"
# SHA-256 of that exact release asset. The binary is downloaded and then
# executed, so it is verified before extraction.
DUMP1090_ZIP_SHA256 = "75610a54c89584337faec624cc052744fa58c471bb8665cd9cad5ea005ced236"
DEFAULT_JSON_URL = "http://localhost:8080/data/aircraft.json"

_proc: subprocess.Popen | None = None
_last_launch_attempt: float = 0.0
_RELAUNCH_BACKOFF = 60.0
_launch_lock = threading.Lock()   # serialize the launch decision across threads


def tools_dir() -> Path:
    d = config_dir() / "tools"
    d.mkdir(parents=True, exist_ok=True)
    return d


def find_dump1090(configured_exe: str = "") -> str | None:
    """Configured path, then the app's tools dir, then a PyInstaller bundle."""
    import shutil
    if configured_exe and Path(configured_exe).exists():
        return configured_exe
    search = [tools_dir()]
    if getattr(sys, "frozen", False):
        search.append(Path(getattr(sys, "_MEIPASS", ".")) / "tools")
    else:
        search.append(Path(__file__).resolve().parent.parent / "tools")
    for root in search:
        for cand in root.rglob("dump1090.exe"):
            return str(cand)
    p = shutil.which("dump1090") or shutil.which("dump1090.exe")
    return p


def install_dump1090(progress: Callable[[str], None] | None = None) -> str:
    """Download + extract the gvanem dump1090 build into the tools dir. Returns
    the path to dump1090.exe. Raises on failure."""
    def say(m):
        log.info("dump1090 install: %s", m)
        if progress:
            progress(m)

    existing = find_dump1090()
    if existing:
        say("already installed")
        return existing

    say("downloading dump1090 (~25 MB)…")
    resp = requests.get(DUMP1090_ZIP_URL, timeout=180)
    resp.raise_for_status()
    digest = hashlib.sha256(resp.content).hexdigest()
    if digest != DUMP1090_ZIP_SHA256:
        raise RuntimeError(f"dump1090 download failed integrity check (got {digest[:12]}…)")
    say("verified; extracting…")
    dest = tools_dir() / "dump1090"
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
        z.extractall(dest)
    exe = find_dump1090()
    if not exe:
        raise RuntimeError("dump1090.exe not found after extraction")
    say("installed")
    return exe


def is_running() -> bool:
    return _proc is not None and _proc.poll() is None


def _url_serving(url: str) -> bool:
    return from_url(url, timeout=3) is not None


def _kill_stray_decoders() -> None:
    """Kill any dump1090.exe (e.g. an orphan from a previous app run) so a fresh
    launch doesn't fight it over the RTL-SDR / port 8080."""
    global _proc
    if sys.platform == "win32":
        try:
            subprocess.run(["taskkill", "/F", "/IM", "dump1090.exe"],
                           creationflags=_NO_WINDOW, capture_output=True, timeout=10)
        except Exception:
            pass
    _proc = None


def launch_dump1090(exe: str, args: str = "--net") -> bool:
    """Launch dump1090 headless. With --net it serves aircraft.json on :8080."""
    global _proc
    if not exe or not Path(exe).exists():
        return False
    if is_running():
        return True
    cmd = [exe, *args.split()]
    try:
        log.info("launching dump1090: %s", " ".join(cmd))
        _proc = subprocess.Popen(cmd, cwd=str(Path(exe).parent), creationflags=_NO_WINDOW,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 stdin=subprocess.DEVNULL)
        return True
    except Exception as e:
        log.warning("dump1090 launch failed: %s", e)
        return False


def stop_dump1090() -> None:
    """Terminate only the decoder this app launched, never a dump1090 started
    outside it. Orphans from a hard-killed prior run are handled at launch time by
    ensure_decoder, which only clears strays when nothing is serving.
    """
    global _proc
    if is_running():
        try:
            _proc.terminate()
        except Exception:
            pass
    _proc = None


# A single ground receiver only sees line-of-sight traffic, and WDGWars
# rejects an upload whose positions span further than one receiver could
# hear ("Geographic span too large"), so a single glitched Mode-S position
# or a 0,0 fix fails the entire batch.
_MAX_KM_FROM_MEDIAN = 500.0   # generous: real LOS is ~450 km at altitude


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    import math
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def receiver_home(cfg: dict[str, Any]) -> tuple[float, float] | None:
    """The configured receiver location, or None if unset/invalid."""
    lat, lon = cfg.get("adsb_home_lat"), cfg.get("adsb_home_lon")
    if lat is None or lon is None:
        return None
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    return lat, lon


def _valid_gain(gain: Any) -> float | None:
    try:
        g = float(gain)
    except (TypeError, ValueError):
        return None
    return g if 0.0 <= g <= 49.6 else None


# The bundled decoder traces EVERY Mode-S frame (CRC, RSSI, registration
# lookup) to its own log, about 5 GB a day, and never rotates or caps it.
# Nothing in this app reads that file and only the last day or so has any
# diagnostic value, so keep it small and recent.
_DECODER_LOG_MAX_BYTES = 512 * 1024 * 1024
_DECODER_LOG_KEEP_DAYS = 2.0


def _prune_decoder_logs(folder: Path) -> None:
    """Delete decoder logs that are oversized or stale. Called just before a
    launch, when no decoder holds them open."""
    cutoff = time.time() - _DECODER_LOG_KEEP_DAYS * 86400
    for p in folder.glob("dump1090*.log"):
        try:
            st = p.stat()
            if st.st_size < _DECODER_LOG_MAX_BYTES and st.st_mtime >= cutoff:
                continue
            size = st.st_size
            p.unlink()
            log.info("removed decoder log %s (%.1f GB)", p.name, size / 1e9)
        except OSError as e:
            log.warning("could not remove decoder log %s: %s", p.name, e)


def _apply_decoder_cfg(exe: str, home: tuple[float, float] | None, gain: Any = None) -> None:
    """Pin the receiver location and tuner gain in the bundled build's dump1090.cfg.

    This build takes neither on the command line; it reads them from the cfg beside
    the exe, which ships with ``homepos = -1,-1`` (so single-frame positions decode
    relative to a point off West Africa) and ``gain = 31``. Rewritten on every
    launch so a dump1090 reinstall cannot silently reset them. latin-1 round-trips
    the file byte-for-byte.
    """
    import re
    cfg = Path(exe).with_name("dump1090.cfg")
    try:
        text = cfg.read_text(encoding="latin-1")
    except OSError:
        return
    new, changes = text, []
    if home is not None:
        lat, lon = home
        new = re.sub(r"(?m)^(homepos\s*=\s*)[^\s#]+", lambda m: f"{m.group(1)}{lat:.7f},{lon:.7f}", new)
        # location=yes asks the Windows Location API, which returns nothing when
        # location services are off, leaving homepos unusable.
        new = re.sub(r"(?m)^(location\s*=\s*)[^\s#]+", lambda m: f"{m.group(1)}no", new)
        changes.append(f"homepos {lat:.5f},{lon:.5f}")
    g = _valid_gain(gain)
    if g is not None:
        new = re.sub(r"(?m)^(gain\s*=\s*)[^\s#]+", lambda m: f"{m.group(1)}{g:g}", new)
        changes.append(f"gain {g:g} dB")
    # One file per day so _prune_decoder_logs can retire old ones, and no
    # per-CPR-event tracing on top of the per-frame trace.
    if re.search(r"(?m)^logfile-daily\s*=\s*0", new):
        new = re.sub(r"(?m)^(logfile-daily\s*=\s*)[^\s#]+", lambda m: f"{m.group(1)}1", new)
        changes.append("logfile-daily 1")
    if re.search(r"(?m)^cpr-trace\s*=\s*true", new):
        new = re.sub(r"(?m)^(cpr-trace\s*=\s*)[^\s#]+", lambda m: f"{m.group(1)}false", new)
        changes.append("cpr-trace off")
    # Single-bit CRC repair recovers frames that arrive with one bad bit; keep it on.
    if re.search(r"(?m)^error-correct1\s*=\s*false", new):
        new = re.sub(r"(?m)^(error-correct1\s*=\s*)[^\s#]+", lambda m: f"{m.group(1)}true", new)
        changes.append("error-correct1 on")
    if new != text:
        try:
            cfg.write_text(new, encoding="latin-1")
            log.info("dump1090.cfg: %s", ", ".join(changes))
        except OSError as e:
            log.warning("could not write dump1090.cfg: %s", e)

def filter_plausible_positions(items: list[dict], max_km: float = _MAX_KM_FROM_MEDIAN,
                               home: tuple[float, float] | None = None) -> list[dict]:
    """Drop aircraft with implausible coordinates so one bad fix cannot fail the
    whole WDGWars upload: null-island and out-of-range points always go, and points
    more than ``max_km`` from the batch median (a robust stand-in for the receiver
    location) are treated as decode glitches and removed.
    """
    import statistics
    pts: list[tuple[float, float, dict]] = []
    for a in items or []:
        try:
            lat = float(a.get("lat"))
            lon = float(a.get("lon"))
        except (TypeError, ValueError):
            continue
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            continue
        if abs(lat) < 1e-6 and abs(lon) < 1e-6:   # null island
            continue
        pts.append((lat, lon, a))
    if home is not None:
        # A known receiver location is the right reference. The batch median is
        # only robust while MOST fixes are good: with dump1090 unconfigured every
        # single-frame fix lands near its default homepos, the median lands there
        # too, and the filter keeps the garbage while dropping real traffic. This
        # path also works for a batch of one or two.
        med_lat, med_lon = home
    else:
        if len(pts) < 3:
            # Too few to tell signal from outlier: keep the (already null-island-
            # filtered) points rather than risk dropping the only real aircraft.
            return [a for _, _, a in pts]
        med_lat = statistics.median(p[0] for p in pts)
        med_lon = statistics.median(p[1] for p in pts)
    kept = [a for lat, lon, a in pts if _haversine_km(lat, lon, med_lat, med_lon) <= max_km]
    dropped = len(pts) - len(kept)
    if dropped:
        log.warning("dropped %d aircraft with outlier positions (>%.0f km from median)",
                    dropped, max_km)
    return kept


def _normalise(obj: Any) -> dict[str, Any] | None:
    if isinstance(obj, dict) and isinstance(obj.get("aircraft"), list):
        obj.setdefault("now", time.time())
        return obj
    if isinstance(obj, dict) and isinstance(obj.get("acList"), list):
        return obj
    if isinstance(obj, list):
        return {"now": time.time(), "aircraft": obj}
    return None


def from_url(url: str, timeout: int = 10) -> dict[str, Any] | None:
    try:
        resp = requests.get(url, timeout=timeout)
        if resp.status_code == 200:
            return _normalise(resp.json())
    except (requests.RequestException, ValueError) as e:
        log.debug("aircraft url read failed: %s", e)
    return None


def from_file(path: str) -> dict[str, Any] | None:
    try:
        p = Path(path)
        if p.exists():
            return _normalise(json.loads(p.read_text(encoding="utf-8")))
    except (OSError, ValueError) as e:
        log.debug("aircraft file read failed: %s", e)
    return None


# Which radio ensure_decoder() last selected. collect() must stay fast, so it
# reads this rather than re-running device detection (which shells out).
_active_source: str = ""


def resolve_source(cfg: dict[str, Any], det: Any = None) -> tuple[str, str]:
    """Pick the ADS-B radio. Returns ``(source, port)`` where source is
    ``"rtlsdr"``, ``"mayhem"`` or ``""`` (none available), and ``port`` is the
    PortaPack console port when source is ``"mayhem"``."""
    from . import detect as _detect
    choice = str(cfg.get("adsb_source") or "auto").strip().lower()
    if choice == "rtlsdr":
        return "rtlsdr", ""
    if choice == "mayhem":
        return "mayhem", _detect.pick_portapack_port(cfg.get("mayhem_port", "auto"), det) or ""
    # auto: prefer a dedicated RTL-SDR receiver; fall back to a PortaPack.
    det = det or _detect.detect()
    if det.rtlsdr_present:
        return "rtlsdr", ""
    port = _detect.pick_portapack_port(cfg.get("mayhem_port", "auto"), det) or ""
    return ("mayhem", port) if port else ("", "")


def active_source() -> str:
    return _active_source


def ensure_decoder(cfg: dict[str, Any], driver_ok: bool | None = None,
                   det: Any = None, sdr_present: bool | None = None) -> bool:
    """Non-blocking: if configured and the RTL-SDR is on WinUSB, launch an
    installed dump1090 (with backoff) so it starts serving the JSON. Returns
    True if a decoder process is (now) running. Never downloads and never waits
    on the URL — callers just read the URL on their normal cadence.

    ``driver_ok=False`` skips the launch entirely: without the WinUSB driver
    dump1090 finds no device and exits immediately, so spawning it is pointless.
    """
    global _last_launch_attempt, _active_source
    if not cfg.get("adsb_enabled", True):
        _active_source = ""
        return is_running()

    from . import mayhem
    source, port = resolve_source(cfg, det)
    _active_source = source
    if source == "mayhem":
        # Never leave a dump1090 running while a different radio is being read: it
        # would sit there holding a device and serving a stale JSON.
        stop_dump1090()
        return mayhem.ensure_reader(port, cfg)
    mayhem.stop()

    if not cfg.get("dump1090_autostart", True):
        return is_running()
    url = (cfg.get("adsb_json_url") or DEFAULT_JSON_URL).strip()
    # Something already serving aircraft (this decoder, an orphan, or a
    # separately started dump1090) is reused. A duplicate would fight for the
    # RTL-SDR and port 8080.
    if _url_serving(url):
        return True
    if is_running():
        return True
    # No RTL-SDR plugged in: nothing for dump1090 to open. It is launched again
    # the moment one shows up.
    if sdr_present is False:
        return False
    # Launched regardless of the driver check, which can be flaky: without
    # WinUSB dump1090 exits immediately and the next backoff window retries, so
    # it starts working the moment the driver is fixed. Double-checked under a
    # lock so two threads cannot both launch.
    with _launch_lock:
        if is_running():
            return True
        now = time.time()
        if now - _last_launch_attempt < _RELAUNCH_BACKOFF:
            return False
        exe = find_dump1090(cfg.get("dump1090_exe", ""))
        if not exe:
            return False
        _last_launch_attempt = now
        _kill_stray_decoders()   # clear any stuck/orphan instance first
        _prune_decoder_logs(Path(exe).parent)
        _apply_decoder_cfg(exe, receiver_home(cfg), cfg.get("dump1090_gain"))
        return launch_dump1090(exe, cfg.get("dump1090_args", "--net"))


def collect(cfg: dict[str, Any]) -> dict[str, Any] | None:
    """Read a dump1090-shape aircraft object from the active radio. Fast and
    side-effect-free; :func:`ensure_decoder` selects the radio and starts a
    decoder or reader.
    """
    if _active_source == "mayhem":
        from . import mayhem
        return mayhem.collect()
    url = (cfg.get("adsb_json_url") or "").strip()
    file = (cfg.get("adsb_json_file") or "").strip()
    if url:
        obj = from_url(url)
        if obj is not None:
            return obj
    if file:
        obj = from_file(file)
        if obj is not None:
            return obj
    return None
