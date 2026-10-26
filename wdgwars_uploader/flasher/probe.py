"""Talk to the firmware a board is running, over plain serial at 115200.

None of this enters the ROM loader. Ports are opened with DTR and RTS both
deasserted (RTS released first): on native USB, RTS asserted with DTR low resets the
chip, and on bridge boards RTS drives EN.

* :func:`probe_firmware` listens for a moment, then sends ``wdusb hello`` about once a
  second. A WardriveESP 1.6+ board answers ``WD:CAPS ... usb=1`` (firmware, chip,
  board, battery gauge, second radio) and the session is closed with ``wdusb bye`` so
  the board goes straight back to Bluetooth. Mesh firmware and the stock Biscuit
  firmware are recognized from what they print.
* :func:`probe_wroom` resets a Biscuit's WROOM into its firmware (EN pulse, IO0 high)
  and reads its boot log: the stock banner names the model, our co-processor says
  ``WDCP hello``.
* :func:`finish_coproc` waits for the new co-processor's hello and stores the model
  with ``WDCP setboard`` when the hello doesn't carry it.
* :func:`verify_wardrive` reads ``WD:CAPS`` after a flash, plus the battery level from
  ``WD:HEALTH`` on boards with a gauge.
* :func:`plain_reset` is an RTS pulse with DTR low: boots a board out of the ROM loader
  into its firmware.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable

import serial

log = logging.getLogger("wdgwars.flasher")

BISCUIT_BOARDS = ("biscuit_ultra", "biscuit_pro", "biscuit_dual")
MODEL_NAMES = {"biscuit_ultra": "Biscuit Ultra", "biscuit_pro": "Biscuit Pro",
               "biscuit_dual": "Biscuit Pro / Ultra", "crumb": "Biscuit Crumb",
               "poltergeist": "Rabbit Labs Poltergeist"}

# Mesh firmware fingerprints (docs/device-research, Heltec V3 bench): MeshCore's
# Bluetooth companion build, Meshtastic's log and banner.
MESH_RE = re.compile(r"SerialBLEInterface|BLE: (?:readBytes|writeBytes|onConnect)|\[PowerFSM\]|"
                     r"\[SerialConsole\]|E S H T|meshtastic|meshcore", re.I)
# Stock Biscuit C5 firmware: the Crumb's health line, the Ultra / Pro build banner.
STOCK_C5_RE = re.compile(r"\[WD-HEALTH\]|NEW FIRMWARE BUILD|Board: Biscuit|Ready for commands from WROOM")


class PortBusy(Exception):
    """The port exists but another program has it open."""


class PortGone(Exception):
    """The port isn't there any more."""


def open_quiet(device: str, baud: int = 115200) -> serial.SerialBase:
    s = serial.serial_for_url(device, do_not_open=True, exclusive=True)
    s.baudrate = baud
    s.timeout = 0.1
    s.write_timeout = 2
    s.rts = False
    s.dtr = False
    try:
        s.open()
    except serial.SerialException as e:
        msg = str(e)
        if re.search(r"Access is denied|PermissionError|busy|Errno 13|Errno 16", msg, re.I):
            raise PortBusy(device) from None
        if re.search(r"FileNotFound|cannot find|not found|Errno 2", msg, re.I):
            raise PortGone(device) from None
        raise
    try:
        s.rts = False      # RTS first, then DTR
        s.dtr = False
    except Exception:  # noqa: BLE001
        pass
    return s


def port_free(device: str) -> bool | None:
    """True = it can be opened now, False = another program holds it, None = gone."""
    try:
        open_quiet(device).close()
        return True
    except PortBusy:
        return False
    except PortGone:
        return None
    except Exception:  # noqa: BLE001
        return False


class Reader:
    """Accumulates text from a port (latin-1, boot logs are not always UTF-8)."""

    def __init__(self, s: serial.SerialBase) -> None:
        self.s = s
        self.text = ""

    def read(self, secs: float, done: Callable[[str], bool] | None = None) -> str:
        end = time.time() + secs
        while time.time() < end and len(self.text) < 64_000:
            try:
                b = self.s.read(512)
            except serial.SerialException:
                break
            if b:
                self.text += b.decode("latin-1", "replace")
                if done and done(self.text):
                    break
        return self.text

    def write(self, line: str) -> None:
        try:
            self.s.write(line.encode("ascii", "replace"))
        except (serial.SerialException, serial.SerialTimeoutException):
            pass


# -- parsers -------------------------------------------------------------------

def key_values(line: str) -> dict[str, str]:
    out = {}
    for tok in line.split()[1:]:
        k, sep, v = tok.partition("=")
        if sep and k:
            out[k] = v
    return out


def _complete_lines(text: str) -> list[str]:
    """Lines that already ended in a newline (the last piece may still be arriving)."""
    return [ln.rstrip("\r") for ln in text.split("\n")[:-1]]


def caps_of(text: str) -> dict[str, str] | None:
    """The last complete ``WD:CAPS ... usb=1`` line (a log fragment can precede it)."""
    found = None
    for line in _complete_lines(text):
        at = line.find("WD:CAPS")
        if at < 0:
            continue
        kv = key_values(line[at:])
        if kv.get("usb") == "1" and kv.get("fw"):
            found = kv
    return found


def health_of(text: str) -> dict[str, str] | None:
    found = None
    for line in _complete_lines(text):
        at = line.find("WD:HEALTH")
        if at >= 0:
            kv = key_values(line[at:])
            if "batt" in kv:
                found = kv
    return found


def hello_of(text: str) -> dict[str, str] | None:
    """The last ``WDCP hello`` from a Biscuit co-processor, or None."""
    m = None
    for m2 in re.finditer(r"WDCP hello [^\r\n]*[\r\n]", text):
        m = m2
    if not m:
        return None
    kv = key_values(m.group(0).strip())
    if kv.get("board") not in BISCUIT_BOARDS:
        return None
    kv.setdefault("fw", "?")
    return kv


def parse_wroom_log(text: str) -> dict | None:
    """A Biscuit WROOM from its boot log: {board, coproc_fw (None = stock), mode}, or None.

    Stock (bench 2026-10-25): "BISCUIT V1 - WROOM BLE Gateway", "Device name: Biscuit Ultra".
    """
    h = hello_of(text)
    if h:
        return {"board": h["board"], "coproc_fw": h.get("fw") or "?", "mode": h.get("mode")}
    if ("WROOM BLE Gateway" not in text and "Device name: Biscuit" not in text
            and "firmware.biscuitshop.us" not in text):
        return None
    if "Device name: Biscuit Ultra" in text or "Biscuit_Ultra" in text:
        board = "biscuit_ultra"
    elif "Device name: Biscuit Pro" in text or "Biscuit_V1" in text:
        board = "biscuit_pro"
    else:
        board = "biscuit_dual"
    m = re.search(r"WROOM BLE Gateway\s+Version:\s*(v?[\d.]+)", text)
    return {"board": board, "coproc_fw": None, "stock_version": m.group(1) if m else None, "mode": None}


def stock_c5_of(text: str) -> str | None:
    """Stock Biscuit firmware on a C5: the model it names, "Biscuit" when unnamed."""
    if not STOCK_C5_RE.search(text):
        return None
    m = re.search(r"Board: (Biscuit [A-Za-z]+)", text)
    if m:
        return m.group(1)
    return "Biscuit Crumb" if "[WD-HEALTH]" in text else "Biscuit"


# -- probes --------------------------------------------------------------------

@dataclass
class Firmware:
    kind: str = "unknown"       # wardrive | mesh | biscuit_stock | silent | other | unknown
    version: str = ""
    chip: str = ""
    board: str = ""             # WD:CAPS board id (crumb, biscuit_ultra, ...)
    board_name: str = ""
    name: str = ""              # Bluetooth name, WardriveESP-12FA
    caps: dict = field(default_factory=dict)
    detail: str = ""

    def as_dict(self) -> dict:
        return {"kind": self.kind, "version": self.version, "chip": self.chip, "board": self.board,
                "board_name": self.board_name, "name": self.name, "detail": self.detail,
                "batt": self.caps.get("batt") == "1", "coproc": self.caps.get("coproc", "")}


def firmware_from_caps(caps: dict, name: str = "") -> Firmware:
    from .firmware import chip_label
    bn = (caps.get("board_name") or "").replace("_", " ")
    return Firmware(kind="wardrive", version=caps.get("fw", ""), chip=chip_label(caps.get("chip")),
                    board=caps.get("board", ""), board_name=bn or MODEL_NAMES.get(caps.get("board", ""), ""),
                    name=name, caps=dict(caps))


def probe_firmware(device: str, listen: float = 1.2, hellos: int = 3) -> Firmware:
    """What runs on this board, without resetting it."""
    s = open_quiet(device)
    try:
        r = Reader(s)
        heard = r.read(listen)
        caps = None
        for _ in range(hellos):
            r.write("wdusb hello\n")
            r.read(1.0, lambda t: caps_of(t) is not None)
            caps = caps_of(r.text)
            if caps:
                break
        if caps:
            name = ""
            r.write("info\n")
            r.read(1.5, lambda t: re.search(r"(WardriveESP|iosESP)-[0-9A-Fa-f]{4}", t) is not None)
            m = re.search(r"(WardriveESP|iosESP)-([0-9A-Fa-f]{4})", r.text)
            if m:
                name = f"{m.group(1)}-{m.group(2).upper()}"
            r.write("wdusb bye\n")
            r.read(0.2)
            return firmware_from_caps(caps, name)
        text = r.text
        if MESH_RE.search(text):
            kind = "Meshtastic" if re.search(r"meshtastic|E S H T|\[PowerFSM\]|\[SerialConsole\]", text, re.I) else "MeshCore"
            return Firmware(kind="mesh", detail=kind)
        stock = stock_c5_of(text)
        if stock:
            return Firmware(kind="biscuit_stock", board_name=stock, detail=f"Stock {stock} firmware")
        if not heard.strip() and not text.strip():
            return Firmware(kind="silent", detail="No answer over USB")
        return Firmware(kind="other", detail="Another firmware")
    finally:
        s.close()


def probe_wroom(device: str) -> dict | None:
    """Reset the WROOM into its firmware and read its boot log (EN pulse on RTS)."""
    s = open_quiet(device)
    try:
        s.rts = True
        time.sleep(0.12)
        s.rts = False
        r = Reader(s)
        r.read(4.5, lambda t: hello_of(t) is not None)
        w = parse_wroom_log(r.text)
        log.info("flasher: CH340 boot log says %s", w or "not a Biscuit WROOM")
        return w
    finally:
        s.close()


def finish_coproc(device: str, board: str) -> dict | None:
    """After the co-processor flash and its reset: wait for the hello, store the model."""
    s = open_quiet(device)
    try:
        r = Reader(s)
        r.read(6.0, lambda t: hello_of(t) is not None)
        hello = hello_of(r.text)
        if hello is None:              # booted before the port opened: ask
            r.write("\r\nWDCP hello?\r\n")
            r.read(3.0, lambda t: hello_of(t) is not None)
            hello = hello_of(r.text)
        if board != "biscuit_dual" and (hello or {}).get("board") != board:
            log.info("flasher: co-processor says board %s, setting %s",
                     (hello or {}).get("board", "nothing"), board)
            r.text = ""
            r.write(f"\r\nWDCP setboard {board}\r\n")
            r.read(3.0, lambda t: (hello_of(t) or {}).get("board") == board)
            again = hello_of(r.text)
            if again:
                hello = again
        return hello
    finally:
        s.close()


def verify_wardrive(device: str, wait: float = 15.0, battery_wait: float = 0.0) -> dict | None:
    """``WD:CAPS`` from the freshly flashed board (and its ``WD:HEALTH`` battery when
    ``battery_wait`` > 0 and it has a gauge). Native USB re-enumerates on reset, so
    the port is retried until ``wait`` runs out."""
    end = time.time() + wait
    while time.time() < end:
        try:
            s = open_quiet(device)
        except (PortBusy, PortGone, serial.SerialException):
            time.sleep(0.5)
            continue
        try:
            r = Reader(s)
            caps = None
            while time.time() < end and caps is None:
                r.write("wdusb hello\n")
                r.read(1.0, lambda t: caps_of(t) is not None)
                caps = caps_of(r.text)
            if caps is None:
                return None
            out = {"caps": caps, "health": None}
            if battery_wait > 0 and caps.get("batt") == "1":
                r.read(battery_wait, lambda t: health_of(t) is not None)
                out["health"] = health_of(r.text)
            r.write("wdusb bye\n")
            r.read(0.2)
            return out
        except serial.SerialException:
            time.sleep(0.5)
        finally:
            try:
                s.close()
            except Exception:  # noqa: BLE001
                pass
    return None


def plain_reset(device: str, native: bool = True) -> None:
    """RTS pulse with DTR low: the chip boots its firmware (out of the ROM loader)."""
    try:
        s = open_quiet(device)
    except Exception:  # noqa: BLE001
        return
    try:
        s.rts = True
        time.sleep(0.2 if native else 0.1)
        s.rts = False
        time.sleep(0.1)
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            s.close()
        except Exception:  # noqa: BLE001
            pass
