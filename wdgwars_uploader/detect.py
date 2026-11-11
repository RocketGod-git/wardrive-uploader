"""USB device detection for the ADS-B radios and the mesh serial devices.

* Serial detection (MeshCore/Meshtastic boards, and the PortaPack console) uses
  pyserial, which enumerates COM ports with their VID/PID - reliable and
  dependency-light.
* An RTL-SDR is *not* a serial device, so it is detected by querying Windows PnP
  through PowerShell for the Realtek DVB VID/PID. This reads the same data as
  ``Get-PnpDevice`` and needs no native driver access.

Two ADS-B radios are possible: an RTL-SDR (decoded on the PC by dump1090) and a
PortaPack/HackRF running Mayhem (decoded on-device and read back over its CDC
console - see :mod:`wdgwars_uploader.mayhem`).
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field

from .mayhem import MAYHEM_IDS

try:
    from serial.tools import list_ports
except Exception:  # pragma: no cover - pyserial always present at runtime
    list_ports = None  # type: ignore


# --- RTL-SDR ----------------------------------------------------------------
# RTL2832U-based dongles, including the RTL-SDR Blog V3/V4.
RTLSDR_IDS = {
    (0x0BDA, 0x2838),  # RTL2832U (most RTL-SDRs, incl. Blog V3/V4)
    (0x0BDA, 0x2832),  # older RTL2832U
    (0x0BDA, 0x2839),
    (0x1D50, 0x60A1),  # osmocom rtl-sdr
}

# --- MeshCore / Heltec serial bridges ---------------------------------------
# VID/PIDs of the USB-serial chips these boards use. A match here is a *strong*
# hint; the app also lets the user pick a port manually.
MESH_SERIAL_IDS = {
    (0x10C4, 0xEA60): "CP210x (Heltec V3 / T-Beam)",
    (0x10C4, 0xEA70): "CP210x dual",
    (0x1A86, 0x7523): "CH340 (Heltec / generic ESP32)",
    (0x1A86, 0x5523): "CH341",
    (0x1A86, 0x55D4): "CH9102 (Station G1/G2, Heltec V3, LilyGo)",
    (0x303A, 0x1001): "ESP32-S3 native USB (Heltec V3)",
    (0x303A, 0x4001): "ESP32-S2 native USB",
    (0x239A, 0x8029): "nRF52840 native (Heltec T114 / RAK)",
    (0x239A, 0x4405): "nRF52840 Meshtastic (Heltec T114 / RAK)",
    (0x2886, 0x0045): "nRF52840 (Seeed)",
    (0x1915, 0x520F): "nRF52840 (Nordic)",
}

# Textual hints in the port description/manufacturer that suggest a mesh board
# even when the VID/PID is unknown.
MESH_TEXT_HINTS = ("heltec", "meshcore", "meshtastic", "silicon labs", "cp210",
                   "ch340", "ch910", "espressif", "nordic", "rak")

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


@dataclass
class SerialDevice:
    port: str
    vid: int | None
    pid: int | None
    description: str
    manufacturer: str
    is_mesh_candidate: bool = False
    chip_label: str = ""
    strong: bool = False   # True = matched a known mesh VID/PID (not just text)
    is_portapack: bool = False


@dataclass
class DetectResult:
    rtlsdr_present: bool = False
    rtlsdr_label: str = ""
    hackrf_present: bool = False
    hackrf_label: str = ""
    serial_ports: list[SerialDevice] = field(default_factory=list)

    @property
    def portapack(self) -> SerialDevice | None:
        return next((d for d in self.serial_ports if d.is_portapack), None)

    @property
    def portapack_present(self) -> bool:
        return self.portapack is not None

    @property
    def portapack_port(self) -> str:
        d = self.portapack
        return d.port if d else ""

    @property
    def portapack_label(self) -> str:
        d = self.portapack
        return d.chip_label if d else ""

    @property
    def adsb_radio_present(self) -> bool:
        """Any radio this app can get ADS-B from."""
        return self.rtlsdr_present or self.hackrf_present or self.portapack_present

    @property
    def mesh_candidates(self) -> list[SerialDevice]:
        # Confirmed VID/PID matches rank ahead of text-hint guesses, so
        # pick_mesh_port() prefers a real mesh board over an unrelated CP210x.
        return sorted((d for d in self.serial_ports if d.is_mesh_candidate),
                      key=lambda d: not d.strong)

    @property
    def mesh_present(self) -> bool:
        return bool(self.mesh_candidates)


def _list_serial() -> list[SerialDevice]:
    out: list[SerialDevice] = []
    if list_ports is None:
        return out
    for p in list_ports.comports():
        vid = getattr(p, "vid", None)
        pid = getattr(p, "pid", None)
        desc = (p.description or "").strip()
        manu = (getattr(p, "manufacturer", None) or "").strip()
        chip = ""
        is_mesh = False
        strong = False
        is_pp = vid is not None and pid is not None and (vid, pid) in MAYHEM_IDS
        if is_pp:
            # The PortaPack console is an ADS-B source, never a mesh node. Flag it
            # explicitly so "mesh_port: auto" can never grab it and hang on the
            # Mayhem shell waiting for MeshCore framing that will never come.
            chip = "PortaPack / HackRF (Mayhem console)"
        elif vid is not None and pid is not None and (vid, pid) in MESH_SERIAL_IDS:
            chip = MESH_SERIAL_IDS[(vid, pid)]
            is_mesh = True
            strong = True
        else:
            blob = f"{desc} {manu}".lower()
            if any(h in blob for h in MESH_TEXT_HINTS):
                is_mesh = True
                chip = desc or manu
        out.append(SerialDevice(
            port=p.device, vid=vid, pid=pid, description=desc,
            manufacturer=manu, is_mesh_candidate=is_mesh, chip_label=chip, strong=strong,
            is_portapack=is_pp,
        ))
    return out


def _rtlsdr_label(reported: str) -> str:
    """Turn the dongle's own USB product string into a readable label.

    Windows' FriendlyName is NOT reliable here: RTL-SDR dongles share the same
    VID/PID and usually the serial 00000001, so a dongle plugged into a port
    inherits the previous dongle's device node and its cached name. The product
    string comes from the dongle itself: a Blog V4 reports "Blog V4", a V3 or
    generic RTL2832U reports "RTL2838UHIDIR".
    """
    low = reported.lower()
    if "blog v4" in low:
        return "RTL-SDR Blog V4"
    if "rtl2838" in low or "rtl2832" in low:
        return "RTL2832U (V3 / generic)"
    return reported


def _rtlsdr_present() -> tuple[bool, str]:
    """Detect an RTL-SDR via Windows PnP. Returns (present, label)."""
    if sys.platform != "win32":
        return False, ""
    # Build a regex of VID_xxxx&PID_xxxx patterns for the known IDs.
    patterns = "|".join(f"VID_{v:04X}&PID_{p:04X}" for v, p in RTLSDR_IDS)
    # Prefer the bus-reported product string (from the dongle) over FriendlyName
    # (Windows' possibly stale cached name) - see _rtlsdr_label.
    ps = (
        "$d = Get-PnpDevice -PresentOnly | "
        f"Where-Object {{ $_.InstanceId -match '{patterns}' }} | Select-Object -First 1; "
        "if ($d) { $b = (Get-PnpDeviceProperty -InstanceId $d.InstanceId "
        "-KeyName 'DEVPKEY_Device_BusReportedDeviceDesc' -ErrorAction SilentlyContinue).Data; "
        "if ($b) { $b } else { $d.FriendlyName } }"
    )
    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=15,
            creationflags=_NO_WINDOW,
        )
        label = (res.stdout or "").strip().splitlines()
        if label and label[0].strip():
            return True, _rtlsdr_label(label[0].strip())
    except Exception:
        pass
    return False, ""


def detect() -> DetectResult:
    r = DetectResult()
    r.serial_ports = _list_serial()
    r.rtlsdr_present, r.rtlsdr_label = _rtlsdr_present()
    from . import hackrf
    hit = hackrf.find_usb()
    if hit:
        r.hackrf_present = True
        # One and Pro share a USB id: the bridge reads the board id once it opens it.
        r.hackrf_label = (hackrf.bridge_status() or {}).get("board") or "HackRF"
    return r


def pick_portapack_port(configured: str, result: DetectResult | None = None) -> str | None:
    """Resolve the PortaPack console port. "" / "auto" -> the detected one."""
    if configured and configured.lower() != "auto":
        return configured
    result = result or detect()
    return result.portapack_port or None


def pick_mesh_port(configured: str, result: DetectResult | None = None) -> str | None:
    """Resolve the mesh serial port from config.

    ``configured`` == "" / "auto" -> first mesh candidate. Otherwise the exact
    COM port requested, returned as-is (so the user can force a port even if the
    VID/PID heuristic didn't flag it).
    """
    if configured and configured.lower() != "auto":
        return configured
    result = result or detect()
    cands = result.mesh_candidates
    return cands[0].port if cands else None


if __name__ == "__main__":  # manual smoke test
    d = detect()
    print(f"RTL-SDR present: {d.rtlsdr_present} ({d.rtlsdr_label})")
    print(f"PortaPack present: {d.portapack_present} ({d.portapack_port})")
    for s in d.serial_ports:
        tag = "  <-- MESH" if s.is_mesh_candidate else ""
        print(f"  {s.port}  vid={s.vid} pid={s.pid}  {s.description}{tag}")
