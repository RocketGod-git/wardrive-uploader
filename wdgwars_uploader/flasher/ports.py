"""Find the ESP32 boards the flasher can talk to and group them into devices.

* Every COM port whose USB id is native USB-Serial-JTAG (303a:1001) or a UART bridge
  WardriveESP boards use (CP210x, CH34x, CH9102 / CH343), with its serial number and
  its USB location (the hub path).
* A 303a:1001 bound to WinUSB has no COM port. It is found over libusb and opened as
  ``usbjtag://<serial>`` (see :mod:`.protocol_usbjtag`).
* A Biscuit Pro / Ultra has two chips behind its own internal hub: the C5 on native
  USB and the classic ESP32 WROOM behind a CH340 (1a86:7523). A C5 and a CH340 that
  are the only two of their kind on one hub are a candidate pair, and when the hub
  path can't be read, the only C5 and the only CH340 on the PC are. The WROOM's own
  boot log confirms it (stock Biscuit banner or our co-processor's hello) before
  anything is treated as a Biscuit.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import serial

log = logging.getLogger("wdgwars.flasher")

if "wdgwars_uploader.flasher" not in serial.protocol_handler_packages:
    serial.protocol_handler_packages.append("wdgwars_uploader.flasher")

USB_JTAG = (0x303A, 0x1001)
CH340 = (0x1A86, 0x7523)

# (vid, pid) -> transport
FLASHABLE = {
    USB_JTAG: "usb_jtag",
    (0x10C4, 0xEA60): "cp210x",
    CH340: "ch34x",
    (0x1A86, 0x5523): "ch34x",
    (0x1A86, 0x55D4): "cdc_uart",     # CH9102
    (0x1A86, 0x55D3): "cdc_uart",     # CH343
}

TRANSPORT_LABEL = {
    "usb_jtag": "Native USB",
    "cp210x": "CP210x",
    "ch34x": "CH340",
    "cdc_uart": "CH9102",
}


@dataclass
class Port:
    device: str                 # "COM7" or "usbjtag://38:44:BE:31:5F:20"
    vid: int
    pid: int
    serial: str = ""
    location: str = ""          # pyserial location ("1-3.2:x.0"), or "3.2" from libusb
    description: str = ""
    winusb: bool = False

    @property
    def transport(self) -> str:
        return FLASHABLE.get((self.vid, self.pid), "")

    @property
    def key(self) -> str:
        """Stable id across replugs: the USB serial number when there is one."""
        if self.serial:
            return f"{self.vid:04x}:{self.pid:04x}:{self.serial.lower()}"
        return self.device.lower()

    @property
    def label(self) -> str:
        return "No COM port (WinUSB)" if self.winusb else self.device

    @property
    def native(self) -> bool:
        return (self.vid, self.pid) == USB_JTAG

    @property
    def chain(self) -> list[str]:
        """USB port chain below the root, bus number left out (pyserial and libusb
        number buses differently on Windows, the port chain is the same)."""
        loc = self.location.split(":", 1)[0]
        if "-" in loc:
            loc = loc.split("-", 1)[1]
        return [p for p in loc.split(".") if p]

    @property
    def hub(self) -> str | None:
        """The hub this port hangs off, or None when it sits on a root port."""
        c = self.chain
        return ".".join(c[:-1]) if len(c) > 1 else None


@dataclass
class Device:
    """What the dashboard shows as one card: one board, or a Biscuit candidate pair."""
    ports: list[Port] = field(default_factory=list)
    pair: bool = False          # C5 + CH340 that may be a Biscuit Pro / Ultra

    @property
    def id(self) -> str:
        return "+".join(sorted(p.key for p in self.ports))

    @property
    def main(self) -> Port:
        return next((p for p in self.ports if p.native), self.ports[0])

    @property
    def wroom(self) -> Port | None:
        return next((p for p in self.ports if (p.vid, p.pid) == CH340), None) if self.pair else None


def list_com_ports() -> list[Port]:
    try:
        from serial.tools import list_ports
        found = list_ports.comports()
    except Exception:  # noqa: BLE001
        log.debug("flasher: listing COM ports failed", exc_info=True)
        return []
    out = []
    for p in found:
        vid, pid = getattr(p, "vid", None), getattr(p, "pid", None)
        if vid is None or pid is None or (vid, pid) not in FLASHABLE:
            continue
        out.append(Port(device=p.device, vid=vid, pid=pid, serial=(p.serial_number or "").strip(),
                        location=(p.location or "").strip(), description=(p.description or "").strip()))
    return out


_WINUSB_SEEN: dict[tuple, str] = {}     # (bus, port chain) -> serial of a WinUSB C5 seen there


def list_winusb_c5(com_serials: set[str]) -> list[Port]:
    """Native USB C5 boards with no COM port (WinUSB). Silent when libusb isn't there."""
    try:
        import usb.core
        import usb.util
        from .protocol_usbjtag import backend
        be = backend()
        devs = list(usb.core.find(find_all=True, idVendor=USB_JTAG[0], idProduct=USB_JTAG[1], backend=be) or [])
    except Exception:  # noqa: BLE001
        return []
    out = []
    for d in devs:
        where = (getattr(d, "bus", None), tuple(getattr(d, "port_numbers", None) or ()))
        try:
            sn = (usb.util.get_string(d, d.iSerialNumber) or "").strip() if d.iSerialNumber else ""
            if sn:
                _WINUSB_SEEN[where] = sn
        except Exception:  # noqa: BLE001
            # Bound to usbser (it has a COM port), or our own session holds it right now.
            sn = _WINUSB_SEEN.get(where, "")
            if not sn:
                continue
        if not sn or sn.lower() in com_serials:
            continue
        chain = ".".join(str(x) for x in (getattr(d, "port_numbers", None) or ()))
        out.append(Port(device=f"usbjtag://{sn}", vid=USB_JTAG[0], pid=USB_JTAG[1], serial=sn,
                        location=chain, description="USB JTAG/serial debug unit (WinUSB)", winusb=True))
    return out


def scan_ports() -> list[Port]:
    com = list_com_ports()
    serials = {p.serial.lower() for p in com if p.serial}
    return com + list_winusb_c5(serials)


def group(ports: list[Port]) -> list[Device]:
    """One Device per board, a C5 + CH340 candidate pair as one Device."""
    c5s = [p for p in ports if p.native]
    chs = [p for p in ports if (p.vid, p.pid) == CH340]
    paired: dict[str, str] = {}
    by_hub: dict[str, list[Port]] = {}
    for p in c5s + chs:
        if p.hub is not None:
            by_hub.setdefault(p.hub, []).append(p)
    for hub, members in by_hub.items():
        a = [p for p in members if p.native]
        b = [p for p in members if (p.vid, p.pid) == CH340]
        if len(a) == 1 and len(b) == 1:
            paired[a[0].key] = b[0].key
    if not paired and len(c5s) == 1 and len(chs) == 1:
        c5, ch = c5s[0], chs[0]
        # Only when the hub paths can't tell (no location, or a WinUSB C5 read over
        # libusb); two known, different hubs are two boards.
        if c5.hub is None or ch.hub is None or c5.winusb:
            paired[c5.key] = ch.key
    out: list[Device] = []
    taken: set[str] = set()
    by_key = {p.key: p for p in ports}
    for p in sorted(ports, key=lambda p: (not p.native, _natural(p.device))):
        if p.key in taken:
            continue
        if p.key in paired:
            ch = by_key[paired[p.key]]
            out.append(Device(ports=[p, ch], pair=True))
            taken |= {p.key, ch.key}
        else:
            out.append(Device(ports=[p]))
            taken.add(p.key)
    return out


def _natural(s: str) -> list:
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]
