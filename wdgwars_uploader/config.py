"""Configuration storage.

Config lives in ``%APPDATA%\\WardriveUploader\\config.json``. The API keys are the
only secrets; they are stored in plain text in the roaming profile, at the same trust
level as the rest of the account, and is never returned by the dashboard API.
"""

from __future__ import annotations

import json
import os
import re
import socket
import tempfile
import threading
from pathlib import Path
from typing import Any


# Interval choices offered in the UI, in hours.
INTERVAL_CHOICES_HOURS = [1, 4, 12, 24]

DEFAULT_CONFIG: dict[str, Any] = {
    "api_key": "",
    "wardrift_session": "",          # set by Wardrift sign-in; the password is never stored
    "wardrift_session_expires": "",
    "wardrift_username": "",
    # Wi-Fi + Bluetooth wardriving: every adapter, Bluetooth radio and location
    # source found is used, except the ones switched off here by id
    # ("wifi:<adapter>", "mon:<usb port>", "bt", "gps:windows", "gps:phone", "gps:serial:<COM>").
    "hw_off": [],
    "gps_net_port": 10110,       # NMEA from a phone, TCP or UDP
    "wigle_api_name": "",
    "wigle_api_token": "",
    "wpasec_key": "",            # wpa-sec.stanev.org account key; captures upload when set
    "wardrift_api_key": "",   # optional; aircraft and mesh nodes also go to wardrift.net
    # Name of this PC in uploaded file names, so uploads from several machines
    # can be told apart. Blank = the computer name.
    "upload_device_label": "",
    "interval_hours": 4,
    # ADS-B --------------------------------------------------------------------
    "adsb_enabled": True,
    # Which radio to read aircraft from:
    #   "auto"    - RTL-SDR if present, else a PortaPack/Mayhem console
    #   "rtlsdr"  - dump1090 + RTL-SDR only
    #   "mayhem"  - PortaPack/HackRF (Mayhem) console only
    "adsb_source": "auto",
    # PortaPack (Mayhem) source. "" / "auto" = the detected PortaPack console.
    "mayhem_port": "auto",
    "mayhem_log_path": "/LOGS/ADSB.TXT",
    # Seconds between log polls. Mayhem writes the same file being read, and
    # tight polling is the suspected trigger for a firmware wedge.
    "mayhem_poll_secs": 3.5,
    # Issue "appstart adsbrx" on connect so the receiver is running.
    "mayhem_autostart_app": True,
    # Receiver location (decimal degrees). dump1090 decodes single-frame
    # positions RELATIVE to this - left unset, the bundled build uses -1,-1 and
    # aircraft appear off the coast of Africa instead of overhead. Also the
    # reference point for dropping implausible fixes before upload.
    "adsb_home_lat": None,
    "adsb_home_lon": None,
    # Where to read decoded aircraft from. Priority: url first, then file.
    # A running dump1090/tar1090/readsb serves this by default.
    "adsb_json_url": "http://localhost:8080/data/aircraft.json",
    "adsb_json_file": "",
    # Optional: a dump1090.exe the app can launch itself if nothing is serving
    # aircraft yet. Blank = auto-find one installed in the app's tools dir.
    "dump1090_exe": "",
    "dump1090_args": "--net",
    # If the aircraft URL isn't answering, auto-launch an installed dump1090.
    "dump1090_autostart": True,
    # RTL-SDR tuner gain in dB, written into the bundled dump1090.cfg on every
    # launch (0-49.6; the R820T2 steps to the nearest supported value).
    # None = leave the cfg's own value alone.
    "dump1090_gain": None,
    # MeshCore (Heltec) --------------------------------------------------------
    "mesh_enabled": True,
    # "serial" (USB companion) or "ble" (Bluetooth LE companion).
    "mesh_transport": "serial",
    # "" / "auto" = auto-pick the first MeshCore-looking serial port.
    "mesh_port": "auto",
    "mesh_baud": 115200,
    # BLE transport: "" / "auto" = first MeshCore-looking BLE device, else a
    # name or address to match.
    "mesh_ble_address": "auto",
    # Periodically send a flood advert to announce this node mesh-wide and
    # encourage discovery/propagation. Minutes between adverts; 0 = never.
    # Flooding too often is poor mesh etiquette (and LoRa duty cycle), so keep
    # this >= ~30.
    "mesh_advert_minutes": 60,
    # One or more mesh devices, each with its own protocol, read every cycle and
    # uploaded together (MeshCore + Meshtastic side by side). Seeded from the
    # legacy single-device fields on first load; then this list is authoritative.
    # Each entry: {"port": "auto"|"COMx", "protocol": "meshcore"|"meshtastic",
    #              "baud": 115200, "enabled": true, "transport": "serial"|"ble",
    #              "ble_address": "address or device UUID"}
    "mesh_devices": [],
    # Meshtastic only: raise the device's LoRa hop limit (1-7) for more reach.
    "mesh_hop_limit": 7,
    # System -------------------------------------------------------------------
    "start_on_boot": False,
    "upload_on_start": True,
    "last_upload_ts": 0,
}


def mesh_devices(values: Any, *, transport: str = "serial", ble_address: str = "auto") -> list[dict[str, Any]]:
    """Add per-device transport defaults without changing existing radio settings."""
    out = []
    for dev in values if isinstance(values, list) else []:
        if not isinstance(dev, dict):
            continue
        protocol = str(dev.get("protocol") or "meshcore").lower()
        conn = str(dev.get("transport") or (transport if protocol == "meshcore" else "serial")).lower()
        out.append({**dev, "protocol": protocol, "transport": "serial" if conn == "usb_serial" else conn,
                    "ble_address": str(dev.get("ble_address") or
                                       (ble_address if protocol == "meshcore" else "")).strip()})
    return out


def device_label(custom: Any = "") -> str:
    """The upload label: the user-set name, else the computer name. Letters and
    digits only, at most 24 characters."""
    def clean(v: Any) -> str:
        return re.sub(r"[^A-Za-z0-9]", "", str(v or ""))[:24]
    return (clean(custom) or clean(os.environ.get("COMPUTERNAME"))
            or clean(socket.gethostname()) or "PC")


def upload_file_name(label: str, name: str) -> str:
    """``aircraft.json.gz`` -> ``WardriveUploader-<label>-aircraft.json.gz``."""
    return f"WardriveUploader-{device_label(label)}-{name}"


_DIR: Path | None = None
MOVE_ERROR = ""            # why the old data folder could not be moved, for the log


def _stop_programs_in(folder: Path) -> None:
    if os.name != "nt":
        return
    import subprocess
    import time
    ps = ("Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and "
          "$_.ExecutablePath.StartsWith($env:WU_DIR, 'OrdinalIgnoreCase') } | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], timeout=20,
                       capture_output=True, env={**os.environ, "WU_DIR": str(folder) + os.sep},
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        time.sleep(1)
    except Exception:
        pass


def config_dir() -> Path:
    """%APPDATA%\\WardriveUploader. Builds from before the rename kept everything
    in %APPDATA%\\WDGWarsUploader; that folder is moved over the first time. If
    something still holds a file in it, the old folder keeps being used."""
    global _DIR, MOVE_ERROR
    if _DIR is not None:
        return _DIR
    base = Path(os.environ.get("APPDATA") or str(Path.home()))
    d, old = base / "WardriveUploader", base / "WDGWarsUploader"
    if old.is_dir() and not d.exists():
        try:
            old.rename(d)
        except OSError:
            # Usually the ADS-B decoder the old build started, still running from
            # its tools folder. Stop whatever runs from in there and try again.
            _stop_programs_in(old)
            try:
                old.rename(d)
            except OSError as e:
                MOVE_ERROR = f"kept using {old}: could not move it ({e})"
                d = old
    d.mkdir(parents=True, exist_ok=True)
    _DIR = d
    return d


def config_path() -> Path:
    return config_dir() / "config.json"


def log_path() -> Path:
    return config_dir() / "uploader.log"


class Config:
    """Thread-safe view over the JSON config file."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._data: dict[str, Any] = dict(DEFAULT_CONFIG)
        self.load()

    def load(self) -> None:
        path = config_path()
        with self._lock:
            self._data = dict(DEFAULT_CONFIG)
            if path.exists():
                try:
                    loaded = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        # Only keep known keys so a stale file cannot break the load.
                        for k in DEFAULT_CONFIG:
                            if k in loaded:
                                self._data[k] = loaded[k]
                except (json.JSONDecodeError, OSError):
                    # Corrupt config: fall back to defaults rather than crash.
                    pass
            # Migrate stale dump1090 args from older versions (pre-gvanem, which
            # used --write-json/--device-type flags the bundled build rejects).
            args = self._data.get("dump1090_args", "")
            if "--write-json" in args or "--device-type" in args:
                self._data["dump1090_args"] = "--net"
            # Seed the mesh-device list from the legacy single-device fields.
            if not self._data.get("mesh_devices"):
                self._data["mesh_devices"] = [{
                    "port": self._data.get("mesh_port", "auto") or "auto",
                    "protocol": "meshcore",
                    "baud": int(self._data.get("mesh_baud", 115200) or 115200),
                    "enabled": True,
                }]
            self._data["mesh_devices"] = mesh_devices(
                self._data["mesh_devices"], transport=self._data["mesh_transport"],
                ble_address=self._data["mesh_ble_address"])

    def save(self) -> None:
        path = config_path()
        with self._lock:
            data = json.dumps(dict(self._data), indent=2)
            # Unique temp file + atomic replace, all under the lock, so
            # concurrent writers can't collide on a shared temp name.
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(data)
                os.replace(tmp, path)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value

    def update(self, values: dict[str, Any]) -> None:
        with self._lock:
            self._data.update(values)

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data)
