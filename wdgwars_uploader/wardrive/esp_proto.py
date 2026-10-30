"""The WardriveESP wire protocol, the same one Wardrive Go speaks: GATT UUIDs,
the 12-byte "GB" frame, the status beacon and advert extras, parsers for the
``WD:`` output lines, the channel region and the channel split between boards.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

SERVICE = "47686f73-7445-5350-4272-696467655376"
RX = "0147686f-7374-4553-5042-726964675852"        # host -> board (write)
TX = "0247686f-7374-4553-5042-726964675854"        # board -> host (notify)
# A linked board advertises this instead of SERVICE, so every host nearby sees
# who holds it. Never connect to it: the board is taken.
STATUS = "0547686f-7374-4553-5042-726964675354"
BEACON_COMPANY = 0xFFFF
EXTRAS = "00005744-0000-1000-8000-00805f9b34fb"
BYE_MIN_FW = "1.7.3"     # first firmware that takes `bye` and sends the advert extras
IAM_MIN_FW = "1.6.5"     # first firmware that reads `iam` and sends the status beacon
HOLDER_MAX_BYTES = 18

NAME_PREFIX = "WardriveESP"
LEGACY_NAME_PREFIX = "iosESP"
_PREFIXES = (NAME_PREFIX, LEGACY_NAME_PREFIX)

START = "wdstream start -wifi"

MAGIC0, MAGIC1, VERSION, HEADER = 0x47, 0x42, 0x01, 12
T_CMD, T_ACK, T_DATA, T_END, T_ERR = 1, 2, 3, 4, 5
FLAG_FIRST = 0x01

LINK_END_NONE, LINK_END_RELEASED, LINK_END_LOST = 0, 1, 2

EXTRAS_CHANNELS = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14,
                   36, 40, 44, 48, 52, 56, 60, 64, 100, 104, 108, 112, 116, 120, 124, 128,
                   132, 136, 140, 144, 149, 153, 157, 161, 165, 169, 173, 177)
_BOARD_NAMES = (None, "Biscuit Crumb", "Rabbit Labs Ghost Board", "Rabbit Labs Poltergeist",
                "Biscuit Pro", "Biscuit Ultra", "Biscuit Pro or Ultra", "Koko Marauder V8", "Marauder Mini v3", "AWOK Dual C5")
COPROC_MODES = ("btc", "wifi", "off")


def is_board_name(name: str | None) -> bool:
    return bool(name) and any(name.lower().startswith(p.lower()) for p in _PREFIXES)


def board_suffix(name: str | None) -> str | None:
    """"12FA" from "WardriveESP-12FA" or "iosESP-12FA"; None for a bare prefix."""
    if not name:
        return None
    for p in _PREFIXES:
        if name.lower().startswith(p.lower() + "-"):
            return name[len(p) + 1:].strip().upper() or None
    return None


@dataclass
class Beacon:
    linked: bool
    scanning: bool
    mode: int
    usb: bool
    gps: bool
    updating: bool
    holder_iphone: bool
    temp_c: int | None
    count: int
    fw: str | None
    name: str
    holder: str


def parse_beacon(m: bytes | None) -> Beacon | None:
    """Manufacturer data under BEACON_COMPANY (id bytes stripped): [0] 'W', [1] flags,
    [2] temp (int8, -128 unknown), [3-4] count LE, [5] major<<4|minor, [6] patch,
    [7-8] name bytes, [9..] holder nickname (UTF-8, linked boards only)."""
    if not m or len(m) < 9 or m[0] != 0x57:
        return None
    f = m[1]
    temp = m[2] - 256 if m[2] > 127 else m[2]
    count = m[3] | (m[4] << 8)
    v, patch = m[5], m[6]
    fw = None if v == 0 and patch == 0 else f"{v >> 4}.{v & 0x0F}.{patch}"
    name = f"{NAME_PREFIX}-{m[7]:02X}{m[8]:02X}"
    holder = ""
    if len(m) > 9:
        raw = bytes(m[9:9 + HOLDER_MAX_BYTES])
        end = raw.find(b"\x00")
        holder = raw[:end if end >= 0 else len(raw)].decode("utf-8", "replace").rstrip("�").strip()
    return Beacon(linked=bool(f & 0x01), scanning=bool(f & 0x02), mode=(f >> 2) & 0x03, usb=bool(f & 0x10),
                  gps=bool(f & 0x20), updating=bool(f & 0x40), holder_iphone=bool(f & 0x80),
                  temp_c=None if temp == -128 else temp, count=count, fw=fw, name=name, holder=holder)


@dataclass
class Extras:
    frame_b: bool
    link_end: int
    screen: bool
    coproc: str | None
    board: int | None
    battery: int | None
    chan_bits: int
    chan_mask: int


def parse_extras(d: bytes | None) -> Extras | None:
    """Service data under EXTRAS (6 bytes). [0] b0 frame, b1-2 how the last link
    ended, b3 screen, b4-5 co-processor. Frame A: [1] board id, [2] battery %,
    [3-5] channel bits 0-23. Frame B: [1-3] channel bits 24-47."""
    if not d or len(d) < 6:
        return None
    h = d[0]
    b = bool(h & 0x01)
    coproc = {1: "off", 2: "btc", 3: "wifi"}.get((h >> 4) & 0x03)
    return Extras(
        frame_b=b, link_end=(h >> 1) & 0x03, screen=bool(h & 0x08), coproc=coproc,
        board=None if b else d[1], battery=None if b or d[2] > 100 else d[2],
        chan_bits=((d[1] | (d[2] << 8) | (d[3] << 16)) << 24) if b else (d[3] | (d[4] << 8) | (d[5] << 16)),
        chan_mask=(0xFFFFFF << 24) if b else 0xFFFFFF,
    )


def extras_board_name(i: int) -> str | None:
    return _BOARD_NAMES[i] if 0 <= i < len(_BOARD_NAMES) else None


def extras_channels(bits: int) -> list[int]:
    return [c for i, c in enumerate(EXTRAS_CHANNELS) if bits & (1 << i)]


def holder_nickname(raw: str) -> str:
    """One line, no control characters, whole UTF-8 characters up to 18 bytes."""
    clean = "".join(c for c in raw if not (ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F)).strip()
    out, n = [], 0
    for c in clean:
        k = len(c.encode("utf-8"))
        if n + k > HOLDER_MAX_BYTES:
            break
        out.append(c)
        n += k
    return "".join(out).strip()


def cmd_frame(command: str, cid: int) -> bytes:
    p = command.encode("utf-8")
    return bytes([MAGIC0, MAGIC1, VERSION, T_CMD, 0, FLAG_FIRST]) + (cid & 0xFFFFFFFF).to_bytes(4, "little") \
        + len(p).to_bytes(2, "little") + p


def key_values(line: str, tag: str) -> dict[str, str]:
    body = line[len(tag):] if line.startswith(tag) else line
    out = {}
    for tok in body.strip().split(" "):
        eq = tok.find("=")
        if eq > 0:
            out[tok[:eq]] = tok[eq + 1:]
    return out


def channel_list(s: str | None) -> list[int]:
    out = []
    for x in (s or "").split(","):
        try:
            v = int(x.strip())
        except ValueError:
            continue
        if v > 0:
            out.append(v)
    return out


def decode_hex(h: str) -> str:
    """Hex -> UTF-8 text; the raw hex when it isn't valid UTF-8; "" when malformed."""
    if not h or len(h) % 2:
        return ""
    try:
        b = bytes.fromhex(h)
    except ValueError:
        return ""
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return h


def frequency(ch: int) -> int:
    if ch <= 0:
        return 0
    if ch == 14:
        return 2484
    return 2407 + ch * 5 if ch < 14 else 5000 + ch * 5


def wigle_auth(token: str) -> str:
    return {"OPEN": "[ESS]", "WEP": "[WEP][ESS]", "WPA": "[WPA-PSK-CCMP][ESS]", "WPA2": "[WPA2-PSK-CCMP][ESS]",
            "WPA3": "[WPA3-SAE-CCMP][ESS]", "WPA2_WPA3": "[WPA2-PSK-CCMP][WPA3-SAE-CCMP][ESS]"}.get(token.upper(), "")


def is_older(a: str, b: str) -> bool:
    """Dotted-number compare: "1.1" < "1.1.1" < "1.2"."""
    def parts(s: str) -> list[int]:
        out = []
        for x in s.split("."):
            try:
                out.append(int(x))
            except ValueError:
                out.append(0)
        return out
    pa, pb = parts(a), parts(b)
    for i in range(max(len(pa), len(pb))):
        x = pa[i] if i < len(pa) else 0
        y = pb[i] if i < len(pb) else 0
        if x != y:
            return x < y
    return False


def _mac17(s: str | None) -> bool:
    return s is not None and len(s) == 17


@dataclass
class Ap:
    bssid: str
    ssid: str
    rssi: int
    channel: int
    caps: str
    hidden: bool
    coproc: bool = False


@dataclass
class Bt:
    mac: str
    random: bool
    name: str
    company_id: int | None
    rssi: int
    connectable: bool
    classic_cod: int | None = None
    coproc: bool = False


@dataclass
class Gps:
    lat: float
    lon: float
    alt: float | None
    sats: int | None


@dataclass
class CaptureLine:
    kind: int
    bssid: str
    sta: str
    ssid: str
    channel: int
    rssi: int
    key: str
    hc22000: str | None


def _int(s: str | None, default: int = 0) -> int:
    try:
        return int(s) if s is not None else default
    except ValueError:
        return default


def parse_ap(line: str) -> Ap | None:
    kv = key_values(line, "WD:AP")
    bssid = kv.get("bssid")
    if not _mac17(bssid):
        return None
    hidden = kv.get("hidden") == "1"
    return Ap(bssid.lower(), "" if hidden else decode_hex(kv.get("ssid_hex", "")), _int(kv.get("rssi")),
              _int(kv.get("ch")), wigle_auth(kv.get("auth", "")), hidden, coproc=kv.get("src") == "coproc")


def parse_bt(line: str) -> Bt | None:
    kv = key_values(line, "WD:BT")
    mac = kv.get("mac")
    if not _mac17(mac):
        return None
    try:
        company = int(kv["mfg"], 16) if kv.get("mfg") else None
    except ValueError:
        company = None
    cod = None
    if kv.get("kind") == "classic":
        try:
            cod = int(kv.get("cod") or "0", 16)
        except ValueError:
            cod = 0
    return Bt(mac.upper(), kv.get("type") == "random", decode_hex(kv.get("name_hex", "")), company,
              _int(kv.get("rssi")), kv.get("conn") == "1", classic_cod=cod, coproc=kv.get("src") == "coproc")


def parse_gps(line: str) -> Gps | None:
    kv = key_values(line, "WD:GPS")
    try:
        lat, lon = float(kv["lat"]), float(kv["lon"])
    except (KeyError, ValueError):
        return None
    if lat != lat or lon != lon or abs(lat) > 90 or abs(lon) > 180 or (lat == 0 and lon == 0) or kv.get("fix") == "0":
        return None
    try:
        alt = float(kv["alt"]) if "alt" in kv else None
    except ValueError:
        alt = None
    return Gps(lat, lon, alt, _int(kv.get("sats"), -1) if "sats" in kv else None)


def parse_capture(line: str) -> CaptureLine | None:
    """`WD:HS ... hc22000=WPA*02*...` or `WD:PMKID ... pmkid=<32 hex>`. The key is the
    MIC (handshake) or the PMKID."""
    hs = line.startswith("WD:HS ")
    kv = key_values(line, "WD:HS" if hs else "WD:PMKID")
    bssid = kv.get("bssid")
    if not _mac17(bssid):
        return None
    hc = kv.get("hc22000")
    if hs:
        f = (hc or "").split("*")
        if len(f) < 3:
            return None
        key = f[2]
    else:
        key = kv.get("pmkid")
        if key is None:
            return None
    if not key:
        return None
    return CaptureLine(2 if hs else 1, bssid.lower(), (kv.get("sta") or "").lower(), decode_hex(kv.get("ssid_hex", "")),
                       _int(kv.get("ch")), _int(kv.get("rssi")), key.lower(), hc if hs else None)


# WiGLE's Bluetooth device-class legend, keyed major << 8 | minor << 2.
_BT_CLASS_LEGEND = {
    0x0000: "Misc", 0x1F00: "Uncategorized",
    0x0400: "A/V", 0x0408: "Handsfree", 0x0410: "Mic", 0x0414: "Speaker", 0x0418: "Headphones",
    0x041C: "Portable Audio", 0x0420: "Car Audio", 0x0424: "Settop", 0x0428: "HiFi", 0x042C: "VCR",
    0x0430: "Camera", 0x0434: "Camcorder", 0x0438: "Monitor", 0x043C: "Display/Speaker",
    0x0440: "Videoconf", 0x0448: "AV Toy",
    0x0100: "Computer", 0x0104: "Desktop", 0x0108: "Server", 0x010C: "Laptop", 0x0110: "PDA",
    0x0114: "Palm", 0x0118: "Wearable Computer",
    0x0900: "Health", 0x0904: "Blood Pressure", 0x0908: "Thermometer", 0x090C: "Scale", 0x0910: "Glucose",
    0x0914: "PulseOxy", 0x0918: "Pulse", 0x091C: "Health Display",
    0x0500: "Keyboard !p", 0x0540: "Keyboard", 0x0580: "Pointer", 0x05C0: "Keyboard+p",
    0x0200: "Phone", 0x0204: "Cellphone", 0x0208: "Cordless Phone", 0x020C: "Smartphone",
    0x0210: "Modem/GW", 0x0214: "ISDN",
    0x0800: "Toy", 0x0804: "Robot", 0x0808: "Vehicle", 0x080C: "Doll", 0x0810: "Controller", 0x0814: "Game",
    0x0700: "Wearable", 0x0704: "Watch", 0x0708: "Pager", 0x070C: "Jacket", 0x0710: "Helmet", 0x0714: "Glasses",
}


def classic_auth_mode(cod: int) -> str:
    """The WiGLE AuthMode of a Bluetooth Classic device: its class, else its major
    class, else "Misc", then ";10" (never bonded)."""
    return (_BT_CLASS_LEGEND.get(cod & 0x1FFC) or _BT_CLASS_LEGEND.get(cod & 0x1F00) or "Misc") + ";10"


# --- channel region ---------------------------------------------------------
US_CHANNELS = frozenset(list(range(1, 12)) + list(range(36, 65, 4)) + list(range(100, 145, 4)) + list(range(149, 166, 4)))
EU_CHANNELS = frozenset(list(range(1, 14)) + list(range(36, 65, 4)) + list(range(100, 141, 4)))
_US_COUNTRIES = {"US", "CA", "MX", "PR", "GU", "VI", "AS", "MP", "UM"}
_EU_COUNTRIES = {
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV", "LT", "LU",
    "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE", "IS", "LI", "NO", "GB", "CH", "GG", "JE", "IM", "GI",
    "MC", "SM", "VA", "AD",
}


class Region:
    """The channels every board's plan is limited to: US (FCC), EU (CEPT/ETSI) or
    every channel the board supports."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.channels = {"US": US_CHANNELS, "EU": EU_CHANNELS}.get(name)

    def limit(self, supported: list[int]) -> list[int]:
        if self.channels is None:
            return list(supported)
        return [c for c in supported if c in self.channels] or list(supported)

    @staticmethod
    def for_country(country: str | None) -> "Region":
        c = (country or "").strip().upper()
        return Region("US" if c in _US_COUNTRIES else "EU" if c in _EU_COUNTRIES else "ALL")


# --- channel split ----------------------------------------------------------
PRIORITY = (1, 6, 11, 36, 40, 44, 48, 149, 153, 157, 161,
            52, 56, 60, 64, 100, 104, 108, 112, 116, 120, 124, 128, 132, 136, 140, 144,
            165, 169, 173, 177, 2, 3, 4, 5, 7, 8, 9, 10, 12, 13, 14)


def compact(chans) -> str:
    """"1, 6, 11, 36–64": runs of 3+ adjacent channels collapse to a range."""
    s = sorted(set(chans))
    parts = []
    i = 0
    while i < len(s):
        j = i
        while j + 1 < len(s) and (s[j] <= 14) == (s[j + 1] <= 14) and s[j + 1] - s[j] == (1 if s[j] <= 14 else 4):
            j += 1
        if j - i >= 2:
            parts.append(f"{s[i]}–{s[j]}")
        else:
            parts.extend(str(s[k]) for k in range(i, j + 1))
        i = j + 1
    return ", ".join(parts)


@dataclass
class Plan:
    shares: dict[str, list[int]] = field(default_factory=dict)
    sweep_all: list[str] = field(default_factory=list)


def plan(boards: list[tuple[str, list[int]]], exclude: set[int] | None = None,
         previous: dict[str, list[int]] | None = None) -> Plan:
    """Splits the channels ``boards`` (in standing order) can tune, minus ``exclude``.
    A board joins the split only while it and every board before it can each hold a
    channel no other board has; the rest sweep everything. Each board keeps what it
    had in ``previous`` where it still can, the rest go in priority order to the
    capable board with the fewest, then shares are evened out. Deterministic."""
    exclude = exclude or set()
    previous = previous or {}
    order = list(PRIORITY)
    for _, s in boards:
        for c in s:
            if c not in order:
                order.append(c)
    rank = {c: i for i, c in enumerate(order)}
    pool = {c for _, s in boards for c in s} - exclude
    sup = {bid: [c for c in order if c in pool and c in s] for bid, s in boards}

    matched: dict[int, str] = {}

    def claim(bid: str, seen: set[int]) -> bool:
        for c in sup[bid]:
            if c in seen:
                continue
            seen.add(c)
            other = matched.get(c)
            if other is not None and not claim(other, seen):
                continue
            matched[c] = bid
            return True
        return False

    in_split = [bid for bid, _ in boards if claim(bid, set())]
    slot = {bid: i for i, bid in enumerate(in_split)}
    load: dict[str, list[int]] = {bid: [] for bid in in_split}
    holder: dict[int, str] = {}

    def give(c: int, to: str) -> None:
        frm = holder.get(c)
        if frm is not None:
            load[frm].remove(c)
        load[to].append(c)
        holder[c] = to

    def size(bid: str) -> int:
        return len(load[bid])

    for bid in in_split:                                   # 1. each board keeps what it had
        for c in previous.get(bid, []):
            if c in sup[bid] and c not in holder:
                give(c, bid)
    for c in order:                                        # 2. the rest, to the capable board with the fewest
        if c not in pool or c in holder:
            continue
        cands = [b for b in in_split if c in sup[b]]
        if cands:
            give(c, min(cands, key=lambda b: (size(b), slot[b])))
    for bid in in_split:                                   # 3. an empty board takes one through a chain of hand-overs
        if size(bid):
            continue
        via: dict[str, tuple[str, int]] = {}
        queue = [bid]
        head = 0
        seen = {bid}
        end = None
        while end is None and head < len(queue):
            x = queue[head]
            head += 1
            for c in sup[x]:
                y = holder.get(c)
                if y is None or y in seen:
                    continue
                seen.add(y)
                via[y] = (x, c)
                if size(y) >= 2:
                    end = y
                    break
                queue.append(y)
        if end is None:
            continue
        cur = end
        while cur != bid:
            to, c = via[cur]
            give(c, to)
            cur = to
    rounds = 0                                             # 4. even out
    while rounds < 10_000:
        rounds += 1
        receivers = sorted(in_split, key=lambda b: (size(b), slot[b]))
        donors = sorted(in_split, key=lambda b: (-size(b), slot[b]))
        move = None
        for r in receivers:
            for d in donors:
                if size(d) - size(r) < 2:
                    break
                rs = sup[r]
                cands = [c for c in load[d] if c in rs]
                if not cands:
                    continue
                move = (max(cands, key=lambda c: rank[c]), r)
                break
            if move:
                break
        if move is None:
            break
        give(*move)
    return Plan({b: sorted(v) for b, v in load.items()}, [b for b, _ in boards if b not in slot])


_NAME_RE = re.compile(r"(WardriveESP|iosESP)-([0-9A-Fa-f]{4})", re.I)


def name_in(text: str) -> str | None:
    """The board's Bluetooth name in an `info` line."""
    m = _NAME_RE.search(text)
    if not m:
        return None
    prefix = LEGACY_NAME_PREFIX if m.group(1).lower() == LEGACY_NAME_PREFIX.lower() else NAME_PREFIX
    return f"{prefix}-{m.group(2).upper()}"
