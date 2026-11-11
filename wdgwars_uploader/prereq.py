"""Prerequisite checks surfaced on the dashboard so a new user knows exactly what
is missing and how to fix it: Python deps, a dump1090 decoder, and — the common
gotcha — whether the RTL-SDR is bound to the WinUSB driver (needed by rtl-sdr /
dump1090) rather than the Realtek DVB-T driver.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

ZADIG_URL = "https://zadig.akeo.ie/"


def _check(name: str, ok: bool, status: str, detail: str = "", fix: str = "", url: str = "",
           action: str = "") -> dict:
    return {"name": name, "ok": ok, "status": status, "detail": detail, "fix": fix, "url": url,
            "action": action}


def check_python_deps() -> dict:
    required = ["requests", "serial", "meshcore", "pystray", "PIL"]
    missing = [m for m in required if importlib.util.find_spec(m) is None]
    if missing:
        return _check("Python packages", False, "missing: " + ", ".join(missing),
                      fix="pip install -r requirements.txt")
    return _check("Python packages", True, "all installed")


def check_dump1090(configured_exe: str = "") -> dict:
    from . import adsb
    exe = adsb.find_dump1090(configured_exe)
    name = "ADS-B decoder (dump1090)"
    if exe:
        return _check(name, True, "installed", detail=exe)
    st = adsb.install_status()
    if st["state"] == "downloading":
        return _check(name, False, "downloading on its own…", detail=st["msg"])
    pinned = ("The download is checked against a pinned SHA-256 of the gvanem/Dump1090 "
              "v1.0.0 release.")
    if st["state"] == "failed":
        return _check(name, False, "download failed", detail=st["msg"],
                      fix="It tries again on its own every 15 minutes, or press Install now. "
                          "If it keeps disappearing, restore it from antivirus quarantine and "
                          "allow it: Microsoft Defender flags this unsigned decoder as "
                          "Trojan:Win32/Bearfoos.A!ml, a machine-learning false positive. "
                          + pinned, action="install-dump1090")
    return _check(name, False, "not installed",
                  fix="It downloads on its own (about 25 MB) as soon as an SDR that needs it is "
                      "plugged in. Press Install now to get it right away. " + pinned,
                  action="install-dump1090")


def rtlsdr_driver_state(rtlsdr_present: bool) -> dict:
    """Determine whether a present RTL-SDR is on WinUSB (usable by rtl-sdr) or
    still on the Realtek DVB-T driver (not usable until re-bound via Zadig)."""
    if not rtlsdr_present:
        return _check("RTL-SDR driver", True, "n/a (no RTL-SDR)")
    if sys.platform != "win32":
        return _check("RTL-SDR driver", True, "n/a (non-Windows)")
    ps = (
        "$d = Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -match 'VID_0BDA&PID_283[289]' } | Select-Object -First 1;"
        "if ($d) { $svc = (Get-PnpDeviceProperty -InstanceId $d.InstanceId -KeyName 'DEVPKEY_Device_Service' -ErrorAction SilentlyContinue).Data;"
        "Write-Output ($d.Class + '|' + $svc) }"
    )
    try:
        res = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                             capture_output=True, text=True, timeout=15, creationflags=_NO_WINDOW)
        out = (res.stdout or "").strip()
    except Exception:
        out = ""
    cls, _, svc = out.partition("|")
    svc_l = svc.strip().lower()
    if svc_l in ("winusb", "libusbk", "libusb0", "winusbcompat"):
        return _check("RTL-SDR driver", True, f"WinUSB ready ({svc.strip()})")
    # Bound to DVB/Realtek (MEDIA class, RTL2832UUSB service) or unknown.
    return _check(
        "RTL-SDR driver", False,
        f"needs WinUSB (currently: {cls or 'DVB'} / {svc.strip() or 'Realtek'})",
        fix="Run Zadig → Options → List All Devices → select 'Bulk-In, Interface (Interface 0)' "
            "(RTL2832U) → replace driver with WinUSB. Requires admin. This disables DVB-T TV use.",
        url=ZADIG_URL,
    )


def hackrf_state(portapack_port: str = "") -> dict:
    from . import adsb, hackrf
    name = "HackRF"
    st = hackrf.bridge_status()
    if portapack_port and not hackrf.find_usb():
        sw = adsb.portapack_switch_state(portapack_port)
        return _check(name, False, "switching the PortaPack to HackRF mode…" if sw.get("sent")
                      else "PortaPack found, switching it to HackRF mode")
    if not st:
        return _check(name, False, "starting…")
    board = st.get("board") or "HackRF"
    if st.get("state") == "streaming":
        if st.get("no_signal"):
            return _check(name, False, f"{board} · no signal",
                          fix="The HackRF is sending empty samples. Check the antenna and use a "
                              "data USB cable, or a powered hub if the port is weak.")
        if not st.get("client"):
            return _check(name, True, f"{board} · waiting for the decoder")
        return _check(name, True, f"{board} · {st.get('msps', 0):.1f} Msps to the decoder",
                      detail=f"firmware {st['firmware']}" if st.get("firmware") else "")
    if st.get("error"):
        return _check(name, False, "could not open it", detail=st["error"],
                      fix="Close any other SDR program using the HackRF (SDR#, SDR++, GQRX, "
                          "hackrf_transfer), then unplug it and plug it back in.")
    return _check(name, False, "waiting for the HackRF")


def portapack_state(port: str) -> dict:
    """Health of the PortaPack/Mayhem ADS-B source."""
    from . import mayhem
    if not port:
        return _check("PortaPack (Mayhem)", False, "not detected",
                      fix="Connect the PortaPack over USB. It must be running Mayhem "
                          "firmware, which exposes a console on USB ID 1d50:6018.")
    st = mayhem.status() or {}
    if st.get("connected"):
        return _check("PortaPack (Mayhem)", True,
                      f"reading on {port} ({int(st.get('aircraft', 0) or 0)} aircraft)",
                      detail=str(st.get("device", "")))
    return _check("PortaPack (Mayhem)", False, f"detected on {port}, not reading yet",
                  detail=str(st.get("error", "")),
                  fix="Insert the microSD (adsbrx logs to /LOGS/ADSB.TXT), attach the "
                      "1090 MHz antenna, and close anything else holding the COM port.")


def check_all(devices, cfg: dict) -> list[dict]:
    from . import adsb
    source, port = adsb.resolve_source(cfg, devices)
    checks = [check_python_deps()]
    if source == "mayhem":
        # dump1090 and the WinUSB driver are irrelevant when the PortaPack does the
        # decoding on-device, so they are not reported as missing.
        checks.append(portapack_state(port))
    elif source == "hackrf":
        # The HackRF firmware asks Windows for WinUSB itself: no driver step.
        checks.append(check_dump1090(cfg.get("dump1090_exe", "")))
        checks.append(hackrf_state(port))
    else:
        checks.append(check_dump1090(cfg.get("dump1090_exe", "")))
        checks.append(rtlsdr_driver_state(bool(devices and devices.rtlsdr_present)))
    return checks
