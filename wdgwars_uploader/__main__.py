"""Entry point.

    python -m wdgwars_uploader            # launch the tray app
    python -m wdgwars_uploader --once     # run one upload cycle, print result, exit
    python -m wdgwars_uploader --detect   # list detected devices and exit
    python -m wdgwars_uploader --check    # validate the API key and exit
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
    print(f"RTL-SDR : {'present - ' + d.rtlsdr_label + ' (driver: ' + (d.rtlsdr_service or 'none') + ')' if d.rtlsdr_present else 'not found'}")
    print(f"Airspy  : {'present - ' + d.airspy_label + ' SN ' + d.airspy_serial + ' (driver: ' + (d.airspy_service or 'none') + ')' if d.airspy_present else 'not found'}")
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


def _cmd_check(core: UploaderCore) -> int:
    ok, msg = core.check_key()
    print(msg)
    return 0 if ok else 1


def _cmd_once(core: UploaderCore) -> int:
    rep = core.run_cycle()
    print("ADS-B:", rep.adsb)
    print("Mesh :", rep.mesh)
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
