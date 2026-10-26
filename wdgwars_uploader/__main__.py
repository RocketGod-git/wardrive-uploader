"""Entry point.

    python -m wdgwars_uploader            # launch the tray app
    python -m wdgwars_uploader --once     # run one upload cycle, print result, exit
    python -m wdgwars_uploader --detect   # list detected devices and exit
    python -m wdgwars_uploader --check    # validate the API key and exit
    python -m wdgwars_uploader --boards   # list ESP32 boards for the flasher and exit
"""

from __future__ import annotations

import argparse
import logging
import sys

from .config import Config
from .core import UploaderCore
from .logutil import setup_logging


def _cmd_detect() -> int:
    from . import detect
    d = detect.detect()
    print(f"RTL-SDR : {'present - ' + d.rtlsdr_label if d.rtlsdr_present else 'not found'}")
    print(f"PortaPack: {'present on ' + d.portapack_port if d.portapack_present else 'not found'}")
    print("Serial ports:")
    if not d.serial_ports:
        print("  (none)")
    for s in d.serial_ports:
        if s.is_portapack:
            tag = "  <-- PortaPack ADS-B source (Mayhem console)"
        elif s.is_mesh_candidate:
            # A USB-serial chip says nothing about the firmware: the same CH9102
            # carries Meshtastic on a Station G1 and MeshCore on other boards.
            tag = f"  <-- mesh radio candidate ({s.chip_label})"
        else:
            tag = ""
        vid = f"{s.vid:04x}" if s.vid is not None else "----"
        pid = f"{s.pid:04x}" if s.pid is not None else "----"
        print(f"  {s.port:6}  {vid}:{pid}  {s.description}{tag}")
    return 0


def _cmd_boards() -> int:
    """ESP32 boards the WardriveESP flasher sees and what they run (no ROM resets)."""
    from .flasher import ports, probe
    devs = ports.group(ports.scan_ports())
    if not devs:
        print("No ESP32 boards found.")
        return 0
    for d in devs:
        main = d.main
        try:
            fw = probe.probe_firmware(main.device)
            what = (f"WardriveESP {fw.version} ({fw.chip}{', ' + fw.board_name if fw.board_name else ''})"
                    if fw.kind == "wardrive" else (fw.detail or fw.kind))
        except probe.PortBusy:
            what = "in use by another program"
        except Exception as e:  # noqa: BLE001
            what = f"couldn't read it ({e})"
        print(f"{main.label:28} {main.vid:04x}:{main.pid:04x}  {what}")
        if d.wroom is not None:
            try:
                w = probe.probe_wroom(d.wroom.device)
            except Exception:  # noqa: BLE001
                w = None
            if w:
                fw2 = f"co-processor {w['coproc_fw']}" if w.get("coproc_fw") else "stock Biscuit firmware"
                print(f"  + {d.wroom.label:24} CH340      {probe.MODEL_NAMES.get(w['board'], 'Biscuit')} WROOM, {fw2}")
            else:
                print(f"  + {d.wroom.label:24} CH340      not a Biscuit WROOM")
    return 0


def _cmd_check(core: UploaderCore) -> int:
    ok, msg = core.check_key()
    print(msg)
    return 0 if ok else 1


def _cmd_once(core: UploaderCore) -> int:
    rep = core.run_cycle()
    print("ADS-B:", rep.adsb)
    print("Mesh :", rep.mesh)
    if rep.wardrift != "skipped" or rep.wardrift_mesh != "skipped":
        print("Wardrift      :", rep.wardrift)
        print("Wardrift mesh :", rep.wardrift_mesh)
    if rep.errors:
        print("Errors:")
        for e in rep.errors:
            print("  -", e)
    return 0 if not rep.errors else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="wdgwars_uploader")
    ap.add_argument("--once", action="store_true", help="run one upload cycle and exit")
    ap.add_argument("--detect", action="store_true", help="list detected devices and exit")
    ap.add_argument("--check", action="store_true", help="validate the API key and exit")
    ap.add_argument("--boards", action="store_true", help="list ESP32 boards for the WardriveESP flasher and exit")
    ap.add_argument("--window", metavar="URL", help="open the dashboard as a native window (internal)")
    ap.add_argument("--watchdog", type=int, metavar="PID", help=argparse.SUPPRESS)
    ap.add_argument("--boot", action="store_true",
                    help="launched at Windows logon: start in the tray without opening the dashboard")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    setup_logging(logging.DEBUG if args.verbose else logging.INFO)

    if args.watchdog:
        from .watchdog import run as watchdog_run
        watchdog_run(args.watchdog)
        return 0

    if args.window:
        from .app import open_window
        open_window(args.window)
        return 0

    if args.detect:
        return _cmd_detect()
    if args.boards:
        return _cmd_boards()

    core = UploaderCore(Config())
    if args.check:
        return _cmd_check(core)
    if args.once:
        return _cmd_once(core)

    # Default: launch the tray GUI. On a boot launch, don't pop the dashboard,
    # and take over from any hung predecessor (watchdog restart / logon start).
    from .app import main as app_main
    app_main(open_dash=not args.boot, takeover=args.boot)
    return 0


if __name__ == "__main__":
    sys.exit(main())
