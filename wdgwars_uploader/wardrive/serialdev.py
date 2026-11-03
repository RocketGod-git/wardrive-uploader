"""ESP32 Marauder, GhostESP and Flipper Zero boards on a USB serial port, run the
way Wardrive Go runs them.

A port that isn't a WardriveESP board is asked who it is, never reset:

* GhostESP (Revival, Poltergeist, classic) runs its own full sweep (`scanap`,
  then `list -a`); its SD card's WiGLE CSVs come in on their own.
* ESP32 Marauder is driven channel by channel (`channel -s`, `sniffbeacon`),
  2.4 GHz, plus 5 GHz on an ESP32-C5.
* A Flipper Zero shows its name, firmware and battery, and the WiGLE CSVs its
  apps saved come in on their own. With its USB-UART Bridge running, the board
  on its GPIO is identified and scanned like any other.
* Unknown firmware gets the same passive commands; it is let go if a few sweeps
  bring nothing.

Only passive commands are sent. Access points go into the run with the boards'
own, located by this PC's GPS.
"""

from __future__ import annotations

import base64
import io
import logging
import re
import threading
import time
from typing import Any, Callable

from .sdimport import RemoteFile, SdImport

log = logging.getLogger("wdgwars.serialdev")

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def clean(s: str) -> str:
    return ANSI.sub("", s).replace("\r", "")


class Gone(Exception):
    """The port went away (unplugged)."""


class Link:
    """A pyserial port with the reads Wardrive Go's drivers use."""

    def __init__(self, ser, stop: Callable[[], bool]) -> None:
        self.ser = ser
        self.stop = stop

    def read(self, ms: int) -> bytes:
        end = time.monotonic() + ms / 1000
        while True:
            try:
                n = self.ser.in_waiting
                data = self.ser.read(n if n else 1)
            except Exception as e:  # noqa: BLE001 - unplugged
                raise Gone(str(e)) from e
            if data or time.monotonic() >= end or self.stop():
                return data

    def text(self, ms: int) -> str:
        return self.read(ms).decode("utf-8", "replace")

    def write(self, s: str) -> None:
        try:
            self.ser.write(s.encode("utf-8"))
        except Exception as e:  # noqa: BLE001
            raise Gone(str(e)) from e

    def drain(self) -> None:
        try:
            self.ser.reset_input_buffer()
        except Exception as e:  # noqa: BLE001
            raise Gone(str(e)) from e

    def read_for(self, ms: int, tick: Callable[[], None] | None = None, until: str | None = None) -> str:
        sb = []
        end = time.monotonic() + ms / 1000
        while not self.stop() and time.monotonic() < end:
            sb.append(self.text(300))
            if tick:
                tick()
            if until:
                t = "".join(sb)
                i = t.find(until)
                if i >= 0 and t.find("\n", i) >= 0:
                    break
        return "".join(sb)


def excerpt(raw: str, n: int = 300) -> str:
    c = re.sub(" +", " ", "".join(ch if ch.isprintable() else " " for ch in clean(raw))).strip()
    return f'"{c[:n]}{"…" if len(c) > n else ""}"' if c else "<nothing>"


def ch_to_freq(ch: int | None, band: str | None = None) -> int | None:
    if not ch:
        return None
    b = (band or "").lower()
    if "2.4" in b or (not b and 1 <= ch <= 14):
        return 2484 if ch == 14 else 2407 + ch * 5
    if "6" in b and "5" not in b:
        return 5950 + ch * 5
    if "5" in b or 32 <= ch <= 177:
        return 5000 + ch * 5
    return None


def chip_from_text(text: str | None) -> tuple[str, bool] | None:
    """Chip and 5 GHz from free text ("Hardware: ESP32-C5 DevKit", "Model: ESP32-C6").
    Only an explicit ESP32 variant counts; only the C5 does 5 GHz."""
    m = re.search(r"\bESP32[-_ ]?(C5|C6|C3|C2|S3|S2|H2)\b", text or "", re.I)
    if not m:
        return None
    v = m.group(1).upper()
    return f"ESP32-{v}", v == "C5"


def looks_like_ghost(t: str) -> bool:
    lo = t.lower()
    return "ghost esp" in lo or "ghostesp" in lo or "ghost_esp" in lo or "ghost> " in t or "[CHIPINFO_START]" in t


def looks_like_marauder(banner: str) -> bool:
    if looks_like_ghost(banner):
        return False
    lo = banner.lower()
    if "sniffbeacon" in lo or "sniffprobe" in lo:
        return True
    # "Build Name: Marauder V8" names the board (GhostESP Revival logs it), never the firmware.
    return any("marauder" in ln.lower() and "Build Name:" not in ln and "Build Config:" not in ln
               for ln in banner.splitlines())


def looks_like_wardriveesp(t: str) -> bool:
    return "WD:" in t or "WardriveESP" in t or "iosESP" in t


def looks_like_debug_stream(t: str) -> bool:
    """Mostly one line repeated (another device's own debug log) or Bluetooth write
    traces, and no CLI prompt."""
    if len(re.findall(r"BLE:\s*writeBytes", t)) >= 3:
        return True
    lines = [ln.strip() for ln in clean(t).splitlines() if len(ln.strip()) > 3]
    if len(lines) < 8 or any(ln.endswith((">", "#")) for ln in lines):
        return False
    top = max(lines.count(x) for x in set(lines))
    return top * 10 >= len(lines) * 6


class Device:
    """What a driver reports to the dashboard and feeds into the run."""

    def __init__(self, bridge, oid: str, port: str, sd: SdImport | None) -> None:
        self.bridge = bridge
        self.oid = oid
        self.port = port
        self.sd = sd
        self.name = "USB serial"
        self.kind = ""

    def show(self, state: str, detail: str = "", **kw: Any) -> None:
        self.bridge.set_other(self.oid, name=self.name, kind=self.kind, transport="USB", port=self.port,
                              state=state, detail=detail, **kw)

    def ap(self, bssid: str, ssid: str | None, rssi: int, ch: int | None, caps: str | None) -> None:
        self.bridge.feed_ap(bssid, ssid or "", max(-120, min(-20, rssi)), ch, caps, self.name, self.oid)

    @property
    def off(self) -> bool:
        return self.oid in self.bridge.off_other


# --------------------------------------------------------------------------- Marauder

AP_RE = re.compile(r"^(?:>\s*)?(?:RSSI:\s*)?(-?\d+)\s+Ch:\s*(\d+)\s+(?:BSSID:\s*)?([0-9a-fA-F:]{17})\s+"
                   r"(?:ESSID Len:\s*\d+\s+)?ESSID:\s?(.*?)(?:\s+\d+\s+\d+)?\s*$")
HOP_24 = list(range(1, 14))
HOP_5 = [36, 40, 44, 48, 52, 56, 60, 64, 100, 104, 108, 112, 116, 120, 124, 128, 132, 136, 140, 144,
         149, 153, 157, 161, 165]
DWELL_S = 0.35


def _marauder_info(link: Link, seen: str) -> dict[str, str | None]:
    """`info` fields ("Firmware: Marauder", "Version: v1.14.0", "Hardware: Marauder Mini v3")."""
    text = seen
    if "Hardware:" not in seen:
        link.drain()
        link.write("info\n")
        sb = ""
        end = time.monotonic() + 1.5
        while not link.stop() and time.monotonic() < end:
            sb += link.text(150)
            hw = sb.find("Hardware:")
            if hw >= 0 and sb.find("\n>", hw) >= 0:
                break
        text = sb

    def field(k: str) -> str | None:
        m = re.search(rf"(?m)^\s*{k}:\s*(.+?)\s*$", text)
        return m.group(1) if m and m.group(1).strip() else None
    return {"hardware": field("Hardware"), "version": field("Version"), "firmware": field("Firmware")}


def drive_marauder(link: Link, dev: Device, boot: str) -> bool:
    """The per-channel Marauder scan. True when the board rejects Marauder's commands
    (it isn't Marauder), so the caller can try GhostESP."""
    try:
        warm_end = time.monotonic() + 2.5
        info = _marauder_info(link, boot)
        chip, dual = chip_from_text(boot) or chip_from_text(info["hardware"]) or ("ESP32", False)
        hw = info["hardware"]
        model = (hw if "marauder" in hw.lower() else f"Marauder · {hw}") if hw else None
        dev.kind = "Marauder"
        dev.name = " · ".join(x for x in (model, info["version"]) if x) if model else f"{chip} Marauder"
        log.info("Marauder on %s: %s · %s · %s", dev.port, dev.name, chip,
                 "2.4 + 5 GHz" if dual else "2.4 GHz")
        dev.show("live", "starting", chip=chip)
        time.sleep(max(0.0, warm_end - time.monotonic()))
        link.write("led -p rainbow\n")
        time.sleep(0.15)
        link.write("stopscan -f\n")
        time.sleep(0.25)
        link.drain()
        hop = HOP_24 + (HOP_5 if dual else [])
        return _sniff_hop(link, dev, hop)
    finally:
        try:
            link.write("stopscan -f\n")
        except Gone:
            pass


def _sniff_hop(link: Link, dev: Device, hop: list[int]) -> bool:
    fresh_s = len(hop) * DWELL_S * 2
    aps: dict[str, dict[str, Any]] = {}
    line = ""
    i = 0
    rows = rejected = 0
    link.write("stopscan -f\n")
    time.sleep(0.18)
    link.drain()
    link.write(f"channel -s {hop[i]}\n")
    time.sleep(0.06)
    link.write("sniffbeacon\n")
    dwell = last_feed = last_diag = time.monotonic()
    while not link.stop() and not dev.off:
        for c in link.text(150):
            if c in "\r\n":
                ln = line.strip()
                line = ""
                if "Unsupported command:" in ln:
                    rejected += 1
                m = AP_RE.match(ln)
                if m:
                    rows += 1
                    rssi, ch, bssid, ssid = int(m.group(1)), int(m.group(2)), m.group(3).lower(), m.group(4)
                    a = aps.get(bssid)
                    if a is None:
                        aps[bssid] = {"ssid": ssid or None, "rssi": rssi, "ch": ch, "at": time.monotonic()}
                    else:
                        a.update(rssi=rssi, ch=ch, at=time.monotonic())
                        if ssid:
                            a["ssid"] = ssid
            else:
                line += c
        if rejected >= 6 and rows == 0:
            log.info("Marauder on %s: %d commands rejected, 0 AP rows, not a Marauder", dev.port, rejected)
            return True
        now = time.monotonic()
        if now - dwell >= DWELL_S:
            i = (i + 1) % len(hop)
            link.write(f"channel -s {hop[i]}\n")
            dwell = now
        if now - last_feed >= 2:
            last_feed = now
            live = {b: a for b, a in aps.items() if now - a["at"] < fresh_s}
            for b in [b for b, a in aps.items() if now - a["at"] > fresh_s * 3]:
                del aps[b]
            for b, a in live.items():
                dev.ap(b, a["ssid"], a["rssi"], a["ch"], None)
            dev.show("live", f"ch {hop[i]}", aps=len(live))
        if now - last_diag >= 12:
            last_diag = now
            log.info("Marauder[%s · %d ch]: aps=%d live=%d", dev.name, len(hop), rows,
                     sum(1 for a in aps.values() if now - a["at"] < fresh_s))
    return False


# --------------------------------------------------------------------------- GhostESP

HEAD_RE = re.compile(r"\[(\d+)]\s*SSID:\s*(.*)$")
LBL = {
    "bssid": re.compile(r"BSSID:\s*([0-9A-Fa-f:]{17})"),
    "rssi": re.compile(r"RSSI:\s*(-?\d+)"),
    "ch": re.compile(r"Channel:\s*(\d+)"),
    "band": re.compile(r"Band:\s*([^,]+)"),
    "maker": re.compile(r"(?:Company|Vendor):\s*([^,\r\n]+)"),
    "sec": re.compile(r"Security:\s*([^,\r\n]+)"),
}
NAMED = [("red", 0xFF0000), ("green", 0x00FF00), ("blue", 0x0000FF), ("yellow", 0xFFFF00), ("purple", 0x800080),
         ("cyan", 0x00FFFF), ("orange", 0xFFA500), ("white", 0xFFFFFF), ("pink", 0xFFC0CB)]
ACCENT = 0x00E5FF
LED_COLOR = min(NAMED, key=lambda n: sum((((ACCENT >> s) & 255) - ((n[1] >> s) & 255)) ** 2 for s in (16, 8, 0)))[0]


def ghost_auth(sec: str | None) -> str | None:
    """GhostESP's `Security:` value as a WiGLE capabilities string; unknown stays None."""
    parts = [p for p in re.split(r"[/ \-]", (sec or "").upper()) if p]
    if not parts:
        return None
    eap = "ENTERPRISE" in parts or "EAP" in parts
    tags = []
    for p in parts:
        if p == "WEP":
            tags.append("[WEP]")
        elif p == "WPA":
            tags.append("[WPA-EAP-CCMP]" if eap else "[WPA-PSK-CCMP]")
        elif p == "WPA2":
            tags.append("[WPA2-EAP-CCMP]" if eap else "[WPA2-PSK-CCMP]")
        elif p == "WPA3":
            tags.append("[WPA3-EAP-CCMP]" if eap else "[WPA3-SAE-CCMP]")
    if tags:
        return "".join(tags) + "[ESS]"
    return "[ESS]" if "OPEN" in parts else None


def _clean_ssid(s: str) -> str | None:
    s = s.strip().rstrip(",").strip()
    return None if not s or s.lower() == "(hidden)" else s


def parse_ap_list(text: str) -> list[dict[str, Any]]:
    """A `list -a` dump, both the multi-line block and the older one-line format."""
    out: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None

    def apply(rec: dict[str, Any], src: str) -> None:
        if m := LBL["bssid"].search(src):
            rec["bssid"] = m.group(1).lower()
        if m := LBL["rssi"].search(src):
            rec["rssi"] = int(m.group(1))
        if m := LBL["ch"].search(src):
            rec["ch"] = int(m.group(1))
        if m := LBL["band"].search(src):
            rec["band"] = m.group(1).strip().rstrip(",").strip()
        if m := LBL["maker"].search(src):
            v = m.group(1).strip().rstrip(",").strip()
            if v and v.lower() != "unknown":
                rec["maker"] = v
        if m := LBL["sec"].search(src):
            a = ghost_auth(m.group(1))
            if a:
                rec["auth"] = a

    def flush() -> None:
        if cur and cur.get("bssid"):
            out.append(cur)
    for raw in text.splitlines():
        line = ANSI.sub("", raw).strip()
        head = HEAD_RE.search(line)
        if head:
            flush()
            rest = head.group(2)
            if LBL["bssid"].search(rest):
                ssid = rest.split(", BSSID:")[0] if ", BSSID:" in rest else rest.split("BSSID:")[0]
                rec = {"ssid": _clean_ssid(ssid), "rssi": -100}
                apply(rec, rest)
                if rec.get("bssid"):
                    out.append(rec)
                cur = None
            else:
                cur = {"ssid": _clean_ssid(rest), "rssi": -100}
            continue
        if cur is not None and line.lower().startswith(("bssid:", "rssi:", "channel:", "band:", "company:", "vendor:",
                                                         "security:")):
            apply(cur, line)
    flush()
    return out


def drive_ghost(link: Link, dev: Device, usb_serial: str | None, known_ghost: bool = False,
                host: str | None = None) -> str:
    """Identify an ESP board (GhostESP, Marauder or unknown firmware) and scan it.
    Returns "done", or "none" when it isn't a Wi-Fi scanner, or "esp" for a WardriveESP board."""
    banner = ""
    identified = False
    try:
        if known_ghost:
            t0 = time.monotonic()
            while not link.stop() and time.monotonic() - t0 < 9 and "ghost> " not in clean(banner):
                banner += link.read_for(300)
            while not link.stop() and time.monotonic() - t0 < 9 and "ghost> " in clean(banner):
                more = link.read_for(500)
                if not more:
                    break
                banner += more
            identified = True
        for _ in range(3):
            if link.stop() or identified:
                break
            link.write("stopscan\r\n")
            time.sleep(0.15)
            link.drain()
            link.write("help\r\n")
            banner += link.read_for(2500)
            if looks_like_wardriveesp(banner):
                return "esp"
            if looks_like_ghost(banner):
                identified = True
                break
            if looks_like_marauder(banner):
                log.info("%s: running Marauder firmware", dev.port)
                if not drive_marauder(link, dev, banner) or link.stop() or dev.off:
                    return "done"
                identified = True
                break
        if not identified:
            log.info("%s: help reply %s", dev.port, excerpt(banner))
        link.drain()
        link.write("chipinfo\r\n")
        info = clean(link.read_for(1800, until="[CHIPINFO_END]"))
        m = re.search(r"Model:\s*(ESP32\S*)", info)
        chip = m.group(1) if m else None
        m = re.search(r"Build Config:\s*(\S+)", info)
        build = m.group(1) if m else None
        if not identified and looks_like_marauder(info):
            if not drive_marauder(link, dev, banner + "\n" + info) or link.stop() or dev.off:
                return "done"
            identified = True
        if not identified:
            heard = banner + info
            if looks_like_wardriveesp(heard):
                return "esp"
            if not heard.strip():
                log.info("%s: the board never answered over USB", dev.port)
                return "none"
            if looks_like_debug_stream(heard):
                log.info("%s: another device's debug output, not a Wi-Fi scanner", dev.port)
                return "none"
        brand = ("Unknown firmware" if not identified else
                 "Poltergeist" if "poltergeist" in (build or "").lower() or "poltergeist" in banner.lower()
                 else "Ghost ESP")
        m = re.search(r"(?m)^\s*Build Config:\s*(.+?)\s*$", info)
        board = m.group(1) if m and m.group(1).strip() else None
        m = re.search(r"(?m)^\s*Firmware:.*?\b(v\d+(?:\.\d+)+)", info)
        ver = m.group(1) if m else None
        named = identified and (board or ver)
        label = " · ".join(x for x in ((brand, board, ver) if named else (brand, chip)) if x)
        dev.kind = "Ghost ESP" if identified else "ESP serial"
        dev.name = f"{host} · {label}" if host else label
        poll = chip is not None or "poltergeist" in (build or "").lower() or not identified
        log.info("%s: %s %s (build=%s, %s)", dev.port, "identified as" if identified else "unknown firmware, trying as",
                 dev.name, build or "?", "poll" if poll else "classic stopscan")
        dev.show("live", "scanning…", chip=chip)
        v8 = "marauder v8" in info.lower()
        if identified:
            hexc = f"{ACCENT:06X}"
            for cmd in (f"settings set accent_color {hexc}", f"settings set terminal_color {hexc}",
                        "statusidle set hud", f"rgbmode {LED_COLOR}"):
                link.write(cmd + "\r\n")
                time.sleep(0.04)
            if not v8:
                link.write("rgbmode police\r\n")
                time.sleep(0.6)
                link.write(f"rgbmode {LED_COLOR}\r\n")
                time.sleep(0.04)
            link.drain()
        # The SD card's WiGLE CSVs (GhostESP's own wardriving).
        sd_src = sd_pending = None
        if identified and dev.sd is not None:
            sid = (usb_serial or "").strip()
            key = sid if len(sid) >= 6 and len(set(sid)) > 1 else f"{dev.port}:{chip or 'esp32'}"
            sd_src = GhostSdSource(link, "ghost:" + key.lower(), dev.name)
            sd_src.count_as = dev.oid
            try:
                sd_pending = dev.sd.find_new(sd_src, link.stop)
            except Gone:
                raise
            except Exception as e:  # noqa: BLE001
                log.info("GhostESP SD: check failed: %s", e)
                sd_pending = []
            if sd_pending:
                dev.sd.offer(sd_src, sd_pending)
            link.drain()
        return _ghost_sweeps(link, dev, identified, poll, v8, sd_src, sd_pending)
    finally:
        try:
            link.write("stopscan\r\n")
            link.write("rgbmode off\r\n")
        except Gone:
            pass


def _ghost_sweeps(link: Link, dev: Device, identified: bool, poll: bool, v8: bool, sd_src, sd_pending) -> str:
    fresh_s = 60.0
    aps: dict[str, dict[str, Any]] = {}
    cycles = 0
    last_emit = [0.0]

    def emit() -> None:
        t = time.monotonic()
        for b, a in aps.items():
            if t - a["at"] < fresh_s:
                dev.ap(b, a.get("ssid"), a["rssi"], a.get("ch"), a.get("auth"))
        for b in [b for b, a in aps.items() if t - a["at"] > fresh_s * 3]:
            del aps[b]
        last_emit[0] = t

    def keep_live() -> None:
        if time.monotonic() - last_emit[0] >= 1.5:
            emit()
    try:
        while not link.stop() and not dev.off:
            if sd_src is not None and sd_pending and not dev.sd.busy:
                link.write("stopscan\r\n")
                time.sleep(0.25)
                link.drain()
                dev.show("live", "importing from the SD card…")
                if dev.sd.try_transfer(sd_src, sd_pending, link.stop):
                    sd_pending = []
                link.drain()
            link.drain()
            link.write("scanap\r\n")
            recs: list[dict[str, Any]] = []
            if poll:
                end = time.monotonic() + 2.5
                while not link.stop() and time.monotonic() < end:
                    link.read(300)
                    keep_live()
                hard = time.monotonic() + 34
                while not link.stop() and time.monotonic() < hard:
                    link.drain()
                    link.write("list -a\r\n")
                    recs = parse_ap_list(link.read_for(4000, keep_live))
                    if recs:
                        break
            else:
                end = time.monotonic() + 6
                while not link.stop() and time.monotonic() < end:
                    link.read(300)
                    keep_live()
                link.write("stopscan\r\n")
                time.sleep(0.25)
                link.drain()
                link.write("list -a\r\n")
                recs = parse_ap_list(link.read_for(4000, keep_live))
            if link.stop():
                break
            now = time.monotonic()
            new = 0
            for r in recs:
                a = aps.get(r["bssid"])
                if a is None:
                    aps[r["bssid"]] = dict(r, at=now)
                    new += 1
                else:
                    for k in ("ssid", "maker", "auth", "ch", "band"):
                        if r.get(k) is not None:
                            a[k] = r[k]
                    a.update(rssi=r["rssi"], at=now)
            cycles += 1
            emit()
            live = sum(1 for a in aps.values() if now - a["at"] < fresh_s)
            log.info("Ghost ESP %s: sweep %d parsed %d APs, %d live, +%d new", dev.port, cycles, len(recs), live, new)
            if not identified and not aps and cycles >= 3:
                log.info("%s: no APs after %d sweeps, not a Wi-Fi scanner", dev.port, cycles)
                return "none"
            dev.show("live", f"sweep {cycles}", aps=live)
            if identified and new and not v8:
                link.write("rgbmode police\r\n")
                until = time.monotonic() + 2
                while not link.stop() and time.monotonic() < until:
                    time.sleep(0.1)
                    keep_live()
                link.write(f"rgbmode {LED_COLOR}\r\n")
                link.drain()
        return "done"
    finally:
        if sd_src is not None and dev.sd is not None:
            dev.sd.withdraw(sd_src)


class GhostSdSource:
    """A GhostESP Revival board's SD card over its CLI (`sd status`, `sd list`,
    `sd read <path> <off> <len> --base64`). Its wardriving writes WiGLE CSVs to
    /mnt/ghostesp/gps. Read-only."""

    GPS_DIR = "/mnt/ghostesp/gps"
    CHUNK = 4096
    TRIES = 3
    next_time = "next plug-in"

    def __init__(self, link: Link, device_key: str, device_label: str) -> None:
        self.link = link
        self.device_key = device_key
        self.device_label = device_label

    def _command(self, cmd: str, timeout_ms: int, end_on_ok: bool = True) -> str:
        if cmd:
            self.link.drain()
            self.link.write(cmd + "\r\n")
        sb = ""
        end = time.monotonic() + timeout_ms / 1000
        last = 0.0
        while not self.link.stop() and time.monotonic() < end:
            got = self.link.text(200)
            now = time.monotonic()
            if got:
                last = now
                sb += got
                if end_on_ok:
                    mark = max(sb.rfind("SD:OK"), sb.rfind("SD:ERR:"))
                    if mark >= 0 and sb.find("\n", mark) >= 0:
                        break
                else:
                    i = sb.find("SD:STATUS:mounted=false")
                    if i >= 0 and sb.find("\n", i) >= 0:
                        break
            elif last and now - last > 0.7:
                break
        return sb

    @staticmethod
    def _list(text: str) -> tuple[list[tuple[str, bool, int]], bool, str | None]:
        out = []
        ok, err = False, None
        for raw in clean(text).splitlines():
            if m := re.search(r"SD:FILE:\[(\d+)]\s+(.+)\s+(\d+)\s*$", raw):
                out.append((m.group(2), False, int(m.group(3))))
                continue
            if m := re.search(r"SD:DIR:\[(\d+)]\s+(.+?)\s*$", raw):
                out.append((m.group(2), True, 0))
                continue
            if m := re.search(r"SD:ERR:(\S.*?)\s*$", raw):
                err = m.group(1)
            if "SD:OK:listed" in raw or "SD:EMPTY" in raw:
                ok = True
        if not ok and err is None:
            err = "no SD:OK reply"
        return out, ok and err is None, err

    def list(self) -> list[RemoteFile]:
        st = clean(self._command("sd status", 4000, end_on_ok=False))
        m = re.search(r"SD:STATUS:mounted=(true|false)", st)
        if not m or m.group(1) != "true":
            log.info("GhostESP SD: %s", "no card mounted" if m else f"no SD:STATUS line {excerpt(st)}")
            return []
        out: list[RemoteFile] = []
        entries, ok, err = self._list(self._command(f"sd list {self.GPS_DIR}", 6000))
        if not ok:
            log.info("GhostESP SD: sd list %s: %s", self.GPS_DIR, err)
            return []

        def add(d: str, es) -> None:
            for name, is_dir, size in es:
                if not is_dir and size > 0 and " " not in name:
                    out.append(RemoteFile(f"{d}/{name}", size))
        add(self.GPS_DIR, entries)
        for name, is_dir, _ in [e for e in entries if e[1] and " " not in e[0]][:8]:
            if self.link.stop():
                break
            sub, ok, err = self._list(self._command(f"sd list {self.GPS_DIR}/{name}", 6000))
            if ok:
                add(f"{self.GPS_DIR}/{name}", sub)
        log.info("GhostESP SD: %d file(s)", len(out))
        return out

    def _chunk(self, path: str, off: int, n: int) -> bytes:
        last = "no reply"
        for _ in range(self.TRIES):
            if self.link.stop():
                raise OSError("stopped")
            t = clean(self._command(f"sd read {path} {off} {n} --base64", 8000 + n))
            data = bytearray()
            bad = False
            for line in t.splitlines():
                if m := re.search(r"SD:READ:DATA:([A-Za-z0-9+/=]+)", line):
                    try:
                        data += base64.b64decode(m.group(1))
                    except ValueError:
                        bad = True
                        break
            m = re.search(r"SD:READ:END:bytes=(\d+)", t)
            if not bad and m and int(m.group(1)) == len(data) and "SD:OK" in t:
                return bytes(data)
            last = "bad base64 line" if bad else ("no SD:READ:END" if not m else f"got {len(data)} of {m.group(1)}")
        raise OSError(f"GhostESP: {last} reading {path} at {off}")

    def head(self, f: RemoteFile, n: int) -> bytes | None:
        return self._chunk(f.path, 0, min(n, f.size))

    def read(self, f: RemoteFile, out: io.BufferedIOBase, on_bytes: Callable[[int], None]) -> int:
        off = 0
        while off < f.size:
            n = min(self.CHUNK, f.size - off)
            data = self._chunk(f.path, off, n)
            out.write(data)
            off += len(data)
            on_bytes(off)
            if len(data) < n:
                break
        return off

    def taken(self, f: RemoteFile) -> bool:
        return True


# --------------------------------------------------------------------------- Flipper Zero

def flipper_name(serial: str | None) -> str | None:
    s = (serial or "").strip()
    if not s.lower().startswith("flip_") or len(s) <= 5:
        return None
    return s[5:].lower().capitalize()


def looks_like_flipper_cli(t: str) -> bool:
    t = clean(t)
    return (t.startswith(">: ") or "\n>: " in t or t.rstrip().endswith("\n>:") or t.strip() == ">:"
            or "Flipper Zero Command Line" in t or "hardware.model" in t or "hardware_model" in t)


def flipper_kv(text: str) -> dict[str, str]:
    out = {}
    for line in clean(text).splitlines():
        if m := re.match(r"^\s*([A-Za-z0-9_.]+)\s+:\s*(.*?)\s*$", line):
            out[m.group(1)] = m.group(2)
    return out


def flipper_firmware(kv: dict[str, str]) -> str | None:
    fork = kv.get("firmware.origin.fork") or kv.get("firmware_origin_fork") or None
    ver = kv.get("firmware.version") or kv.get("firmware_version") or None
    if fork and ver:
        return ver if fork.lower() in ver.lower() else f"{fork} {ver}"
    return ver or fork


def flipper_battery(kv: dict[str, str]) -> tuple[int, str] | None:
    try:
        level = int(kv["charge.level"])
    except (KeyError, ValueError):
        return None
    state = kv.get("charge.state")
    return level, ("" if not state or state.lower() == "discharging" else state)


def cli_command(link: Link, cmd: str, timeout_ms: int) -> str:
    """One Flipper CLI command: what follows its echo, up to the `>: ` prompt."""
    link.drain()
    link.write(cmd + "\r")
    sb = ""
    end = time.monotonic() + timeout_ms / 1000

    def body() -> str | None:
        t = clean(sb)
        if not cmd:
            return t
        at = t.rfind(cmd)
        if at < 0:
            return None
        nl = t.find("\n", at + len(cmd))
        return None if nl < 0 else t[nl + 1:]
    while not link.stop() and time.monotonic() < end:
        got = link.text(200)
        if not got:
            continue
        sb += got
        b = body()
        if b is not None and b.rstrip().endswith(">:"):
            break
    b = body()
    return b if b is not None else clean(sb)


def drive_flipper(link: Link, dev: Device, usb_serial: str | None) -> str:
    name = flipper_name(usb_serial)
    host = "Flipper Zero" + (f" {name}" if name else "")
    dev.kind, dev.name = "Flipper Zero", host
    dev.show("identifying", "")
    link.write("\r")
    heard = link.read_for(1500)
    cli = looks_like_flipper_cli(heard)
    if not cli:
        link.write("info device\r")
        heard += link.read_for(2500)
        cli = looks_like_flipper_cli(heard)
    if cli:
        _flipper_cli(link, dev, name)
        return "done"
    if name:
        log.info("Flipper %s: USB-UART Bridge, identifying the board on its GPIO", name)
        dev.show("live", "bridge · identifying the board…")
        return drive_ghost(link, dev, usb_serial, host=host)
    log.info("USB 0483:5740: no Flipper CLI and not named FLIP_ (a generic STM32 serial device), released")
    return "none"


def _flipper_cli(link: Link, dev: Device, usb_name: str | None) -> None:
    kv = flipper_kv(cli_command(link, "info device", 4000))
    name = kv.get("hardware.name") or usb_name
    fw = flipper_firmware(kv)
    bat = flipper_battery(flipper_kv(cli_command(link, "info power", 3000)))
    sd_text = clean(cli_command(link, "storage info /ext", 4000))
    has_sd = "Storage error" not in sd_text
    dev.name = "Flipper Zero" + (f" {name}" if name else "")
    detail = " · ".join(x for x in (fw, "no SD card" if not has_sd else "") if x)
    log.info("Flipper: CLI · %s · firmware %s · battery %s · SD %s", dev.name, fw, bat, has_sd)
    dev.show("live", detail, battery=bat[0] if bat else None)
    src = FlipperSdSource(link, "flipper:" + (name or usb_name or "unknown").lower(), dev.name)
    src.count_as = dev.oid
    pending: list[RemoteFile] = []
    if has_sd and dev.sd is not None:
        dev.show("live", "checking the SD card…", battery=bat[0] if bat else None)
        try:
            pending = dev.sd.find_new(src, link.stop)
        except Gone:
            raise
        except Exception as e:  # noqa: BLE001
            log.info("Flipper SD: check failed: %s", e)
        if pending:
            dev.sd.offer(src, pending)
        dev.show("live", detail, battery=bat[0] if bat else None)
    next_power = time.monotonic() + 60
    try:
        while not link.stop() and not dev.off:
            if pending and not dev.sd.busy:
                dev.show("live", "importing from the SD card…", battery=bat[0] if bat else None)
                if dev.sd.try_transfer(src, pending, link.stop):
                    pending = []
                    src.cache.clear()
                dev.show("live", detail, battery=bat[0] if bat else None)
            if time.monotonic() >= next_power:
                next_power = time.monotonic() + 60
                b = flipper_battery(flipper_kv(cli_command(link, "info power", 3000)))
                if b and b != bat:
                    bat = b
                    dev.show("live", detail, battery=bat[0])
            link.read(300)
    finally:
        if dev.sd is not None:
            dev.sd.withdraw(src)


class FlipperSdSource:
    """The Flipper's SD card over its CLI (`storage list`, `storage read`). Read-only.
    Every .csv/.log/.txt under /ext/apps_data (and a few likely folders) is a
    candidate; the WiGLE header decides."""

    ROOTS = ("/ext/apps_data", "/ext/wardriver", "/ext/wardrive", "/ext/wigle", "/ext/gps", "/ext/wifi_marauder")
    SKIP = {"subghz", "nfc", "infrared", "lfrfid", "badusb", "bad_kb", "ibutton", "js_app", "unit_tests", "dolphin",
            "u2f", "totp", "music_player", "wav_player", "metroflip", "swd", "esp_flasher", "assets", "plugins",
            "subghz_remote", "rfid_raw", "nrf24batch", "nrf24scan", "pcaps", "cli"}
    EXT = {"csv", "log", "txt"}
    SMALL = 512 * 1024
    HEAD_STREAM_MAX = 16 * 1024 * 1024
    next_time = "next plug-in"

    def __init__(self, link: Link, device_key: str, device_label: str) -> None:
        self.link = link
        self.device_key = device_key
        self.device_label = device_label
        self.cache: dict[str, bytes] = {}

    @staticmethod
    def _q(p: str) -> str:
        return f'"{p}"' if " " in p else p

    def list(self) -> list[RemoteFile]:
        out: list[RemoteFile] = []
        lists = [0]

        def walk(d: str, depth: int) -> None:
            if self.link.stop() or lists[0] >= 60:
                return
            lists[0] += 1
            t = clean(cli_command(self.link, f"storage list {self._q(d)}", 5000))
            if "Storage error" in t:
                return
            for line in t.splitlines():
                if m := re.match(r"^\s*\[F]\s+(.+?)\s+(\d+)b\s*$", line):
                    nm, size = m.group(1), int(m.group(2))
                    if size > 0 and nm.rsplit(".", 1)[-1].lower() in self.EXT:
                        out.append(RemoteFile(f"{d}/{nm}", size))
                elif m := re.match(r"^\s*\[D]\s+(.+?)\s*$", line):
                    nm = m.group(1)
                    if depth < 3 and nm.lower() not in self.SKIP and not nm.startswith("."):
                        walk(f"{d}/{nm}", depth + 1)
        for r in self.ROOTS:
            walk(r, 1)
        log.info("Flipper SD: looked in %d folders, %d candidate file(s)", lists[0], len(out))
        return out

    def head(self, f: RemoteFile, n: int) -> bytes | None:
        if f.size <= self.SMALL:
            buf = io.BytesIO()
            self._stream(f.path, buf, lambda _: None)
            self.cache[f.path] = buf.getvalue()
            return buf.getvalue()[:n]
        if f.name.lower().endswith(".csv"):
            return None
        if f.size <= self.HEAD_STREAM_MAX:
            buf = io.BytesIO()
            self._stream(f.path, buf, lambda _: None)
            return buf.getvalue()[:n]
        return b""

    def read(self, f: RemoteFile, out: io.BufferedIOBase, on_bytes: Callable[[int], None]) -> int:
        b = self.cache.pop(f.path, None)
        if b is not None:
            out.write(b)
            on_bytes(len(b))
            return len(b)
        return self._stream(f.path, out, on_bytes)

    def taken(self, f: RemoteFile) -> bool:
        return True

    def _stream(self, path: str, out, on_bytes: Callable[[int], None]) -> int:
        """`storage read <path>`: `Size: N`, then exactly N bytes, then the prompt."""
        link = self.link
        link.drain()
        link.write(f"storage read {self._q(path)}\r")
        header = bytearray()
        size, got = -1, 0
        last = time.monotonic()
        while size < 0 or got < size:
            if link.stop():
                raise OSError("stopped")
            if time.monotonic() - last > 8:
                raise OSError(f"Flipper stopped sending {path} at {got} bytes")
            data = link.read(300)
            if not data:
                continue
            last = time.monotonic()
            if size >= 0:
                take = data[: size - got]
                out.write(take)
                got += len(take)
                on_bytes(got)
                continue
            header += data
            h = header.decode("latin-1")
            if "Storage error" in h:
                cli_command(link, "", 1000)
                raise OSError(f"Flipper: storage error ({path})")
            m = re.search(r"Size: (\d+)\r?\n", h)
            if not m:
                if len(header) > 8192:
                    raise OSError(f"Flipper: no Size line for {path}")
                continue
            size = int(m.group(1))
            rest = bytes(header[m.end():])[:size]
            out.write(rest)
            got = len(rest)
            on_bytes(got)
        tail = ""
        end = time.monotonic() + 1.5
        while not link.stop() and time.monotonic() < end and not tail.rstrip().endswith(">:"):
            tail += link.read(200).decode("latin-1")
        return got


# --------------------------------------------------------------------------- entry

FLIPPER = (0x0483, 0x5740)


def device_id(device: str, usb_serial: str | None) -> str:
    s = (usb_serial or "").strip()
    return "ser:" + (s if len(s) >= 6 and len(set(s)) > 1 else device)


def run(ser, bridge, device: str, vp: tuple, usb_serial: str | None, stop: Callable[[], bool]) -> str:
    """Drive a port that isn't a WardriveESP board. Returns the port's next state for
    the USB watcher: "none" (not a scanner, leave it), "esp" (a WardriveESP board
    while the bridge is off), "off:<id>" (switched off here) or None (unplugged / done)."""
    oid = device_id(device, usb_serial)
    dev = Device(bridge, oid, device, bridge.sd)
    if dev.off:
        return f"offdev:{oid}"
    link = Link(ser, lambda: stop() or dev.off)
    dev.show("identifying", "")
    result = "none"
    try:
        if vp == FLIPPER:
            result = drive_flipper(link, dev, usb_serial)
        else:
            result = drive_ghost(link, dev, usb_serial)
    except Gone as e:
        log.info("%s left the USB bus (%s)", device, e)
        result = "gone"
    except Exception:  # noqa: BLE001
        log.exception("%s: serial device failed", device)
        result = "none"
    finally:
        bridge.drop_other(oid)
    if dev.off and result != "gone":
        return f"offdev:{oid}"
    return {"done": None, "gone": None}.get(result, result)
