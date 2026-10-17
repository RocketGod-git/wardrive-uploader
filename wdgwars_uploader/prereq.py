from __future__ import annotations

import importlib.util

from .detect import WINUSB_SERVICES

ZADIG_URL = "https://zadig.akeo.ie/"


def _check(name: str, ok: bool, status: str, detail: str = "", fix: str = "", url: str = "",
           action: dict | None = None) -> dict:
    return {"name": name, "ok": ok, "status": status, "detail": detail, "fix": fix, "url": url,
            "action": action or None}


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
    if exe:
        return _check("ADS-B decoder (dump1090)", True, "installed", detail=exe)
    return _check("ADS-B decoder (dump1090)", False, "not installed",
                  fix="Needed for an RTL-SDR and for an Airspy (ADS-B SPY feeds it). Click "
                      "'Install dump1090' to fetch the Windows build automatically, or point "
                      "Settings at a running dump1090 URL. If it was installed and has "
                      "disappeared, check antivirus quarantine: Microsoft Defender flags this "
                      "unsigned decoder as Trojan:Win32/Bearfoos.A!ml, a machine-learning false "
                      "positive. The download is checked against a pinned SHA-256 of the "
                      "gvanem/Dump1090 v1.0.0 release.",
                  action={"label": "Install dump1090", "endpoint": "/api/install-dump1090"})


def _driver_check(name: str, radio: str, present: bool, service: str, auto_binds: bool) -> dict:
    if not present:
        return _check(name, True, f"n/a (no {radio})")
    svc = (service or "").strip()
    if svc.lower() in WINUSB_SERVICES:
        return _check(name, True, f"WinUSB ready ({svc})")
    if auto_binds:
        fix = (f"Windows normally binds the inbox WinUSB driver to the {radio} by itself. "
               "Unplug and replug it; if it still shows another driver, run Zadig -> Options -> "
               f"List All Devices -> select the {radio} -> replace driver with WinUSB (admin).")
    else:
        fix = ("Run Zadig -> Options -> List All Devices -> select 'Bulk-In, Interface (Interface 0)' "
               "(RTL2832U) -> replace driver with WinUSB. Requires admin. This disables DVB-T TV use.")
    return _check(name, False, f"needs WinUSB (currently: {svc or 'no driver'})", fix=fix, url=ZADIG_URL)


def rtlsdr_driver_state(devices) -> dict:
    present = bool(devices and devices.rtlsdr_present)
    return _driver_check("RTL-SDR driver", "RTL-SDR", present,
                         devices.rtlsdr_service if present else "", auto_binds=False)


def airspy_driver_state(devices) -> dict:
    present = bool(devices and devices.airspy_present)
    return _driver_check("Airspy driver", "Airspy", present,
                         devices.airspy_service if present else "", auto_binds=True)


def airspy_decoder_state(cfg: dict) -> dict:
    from . import airspy
    st = airspy.status(cfg)
    if not st["installed"]:
        return _check("Airspy decoder (ADS-B SPY)", False, "not installed",
                      fix="An Airspy is decoded by Airspy's own ADS-B SPY, which feeds dump1090. "
                          "Click 'Install ADS-B SPY' to fetch the Windows build from airspy.com "
                          "(checked against a pinned SHA-256).",
                      action={"label": "Install ADS-B SPY", "endpoint": "/api/install-airspy-adsb"})
    if st["connected"]:
        return _check("Airspy decoder (ADS-B SPY)", True, "running, feeding dump1090", detail=st["exe"])
    if st["running"]:
        return _check("Airspy decoder (ADS-B SPY)", False, "running, not yet connected to dump1090",
                      detail=st["error"] or st["exe"],
                      fix="Normal for the first seconds after start. If it persists: is the Airspy "
                          "antenna/USB seated, and is dump1090 running (it must listen on port "
                          "30001)? The decoder is restarted automatically after 90 s without a "
                          "connection following a launch, or 15 s after a connection was lost. "
                          f"Log: {st['log']}")
    return _check("Airspy decoder (ADS-B SPY)", False, "installed, not running",
                  detail=st["error"] or st["exe"],
                  fix=f"It is launched automatically while an Airspy is present. Log: {st['log']}")


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


def no_radio_state() -> dict:
    return _check("ADS-B radio", False, "no RTL-SDR, Airspy or PortaPack detected",
                  fix="Plug one in. An RTL-SDR must be on WinUSB (Zadig); an Airspy binds WinUSB "
                      "by itself; a PortaPack must run Mayhem. Or point Settings at an "
                      "aircraft.json URL served by another machine.")


def check_all(devices, cfg: dict) -> list[dict]:
    from . import adsb
    source, port = adsb.resolve_source(cfg, devices)
    checks = [check_python_deps()]
    if source == "mayhem":
        checks.append(portapack_state(port))
        return checks
    if not (devices and (devices.rtlsdr_present or devices.airspy_present)):
        checks.append(no_radio_state())
    checks.append(check_dump1090(cfg.get("dump1090_exe", "")))
    if devices and devices.rtlsdr_present:
        checks.append(rtlsdr_driver_state(devices))
    if devices and devices.airspy_present:
        checks.append(airspy_driver_state(devices))
        checks.append(airspy_decoder_state(cfg))
    return checks
