"""Wi-Fi scanning through the Windows Native Wifi API, on every Wi-Fi adapter
Windows has a driver for (built-in cards, and USB adapters such as an Alfa).

Each pass asks every adapter to scan, then reads its BSS list. Windows keeps
networks in that list for up to a couple of minutes after they were last heard,
so only entries heard within the last few seconds are returned; an older entry
would otherwise be logged at the current GPS position.
"""

from __future__ import annotations

import ctypes
import logging
import time
from ctypes import wintypes as W
from typing import Any

log = logging.getLogger("wdgwars.wifi")

FRESH_SECONDS = 6.0


class _GUID(ctypes.Structure):
    _fields_ = [("d1", ctypes.c_ulong), ("d2", ctypes.c_ushort), ("d3", ctypes.c_ushort),
                ("d4", ctypes.c_ubyte * 8)]


class _IfInfo(ctypes.Structure):
    _fields_ = [("guid", _GUID), ("desc", ctypes.c_wchar * 256), ("state", ctypes.c_int)]


class _IfList(ctypes.Structure):
    _fields_ = [("n", W.DWORD), ("i", W.DWORD), ("items", _IfInfo * 1)]


class _Ssid(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ulong), ("ssid", ctypes.c_char * 32)]


class _BssEntry(ctypes.Structure):
    _fields_ = [("ssid", _Ssid), ("phyId", ctypes.c_ulong), ("bssid", ctypes.c_ubyte * 6),
                ("bssType", ctypes.c_int), ("phyType", ctypes.c_int), ("rssi", ctypes.c_long),
                ("linkQuality", ctypes.c_ulong), ("inRegDomain", ctypes.c_ubyte),
                ("beaconPeriod", ctypes.c_ushort), ("timestamp", ctypes.c_ulonglong),
                ("hostTimestamp", ctypes.c_ulonglong), ("capInfo", ctypes.c_ushort),
                ("chCenterFreq", ctypes.c_ulong), ("rateSet", ctypes.c_ubyte * (4 + 126 * 2)),
                ("ieOffset", ctypes.c_ulong), ("ieSize", ctypes.c_ulong)]


class _BssList(ctypes.Structure):
    _fields_ = [("size", W.DWORD), ("n", W.DWORD), ("e", _BssEntry * 1)]


_IF_STATES = {0: "not ready", 1: "connected", 2: "ad hoc", 3: "disconnecting", 4: "disconnected",
              5: "associating", 6: "discovering", 7: "authenticating"}
_CIPHERS = {1: "WEP40", 2: "TKIP", 4: "CCMP", 5: "WEP104", 8: "GCMP", 9: "GCMP-256", 10: "CCMP-256"}
_AKMS = {1: "EAP", 2: "PSK", 3: "FT/EAP", 4: "FT/PSK", 5: "EAP-SHA256", 6: "PSK-SHA256",
         8: "SAE", 9: "FT/SAE", 11: "EAP-SUITE-B", 12: "EAP-SUITE-B-192", 18: "OWE", 24: "SAE-EXT-KEY"}


def channel(freq_mhz: int) -> int | None:
    if freq_mhz == 2484:
        return 14
    if 2412 <= freq_mhz <= 2472:
        return (freq_mhz - 2407) // 5
    if 5150 <= freq_mhz <= 5925:
        return (freq_mhz - 5000) // 5
    if 5955 <= freq_mhz <= 7115:
        return (freq_mhz - 5950) // 5
    return None


def _suites(body: bytes, pos: int) -> tuple[list[int], int]:
    """A suite list: 2-byte count, then 4-byte OUI+type entries."""
    if pos + 2 > len(body):
        return [], pos
    n = int.from_bytes(body[pos:pos + 2], "little")
    pos += 2
    out = [body[pos + 4 * i + 3] for i in range(n) if pos + 4 * i + 4 <= len(body)]
    return out, pos + 4 * n


def _rsn(body: bytes, wpa1: bool = False) -> tuple[list[str], str]:
    """(AKM names, pairwise cipher) from an RSN body or a WPA1 vendor body."""
    pos = 2 + 4                      # version + group cipher
    pairwise, pos = _suites(body, pos)
    akms, pos = _suites(body, pos)
    names = [_AKMS.get(a, "?") for a in akms] or (["PSK"] if wpa1 else [])
    ciphers = [_CIPHERS.get(c, "?") for c in pairwise]
    cipher = "+".join(dict.fromkeys(ciphers)) or ("TKIP" if wpa1 else "CCMP")
    return names, cipher


def auth_mode(ies: bytes, cap_info: int) -> str:
    """Capabilities in the Android ScanResult style the WiGLE app writes, e.g.
    ``[WPA2-PSK-CCMP][RSN-PSK-CCMP][ESS]``."""
    parts: list[str] = []
    rsn = wpa = None
    wps = False
    i = 0
    while i + 2 <= len(ies):
        eid, ln = ies[i], ies[i + 1]
        body = ies[i + 2:i + 2 + ln]
        if eid == 48 and len(body) >= 8:
            rsn = body
        elif eid == 221 and len(body) >= 4 and body[:3] == b"\x00\x50\xf2":
            if body[3] == 1 and len(body) >= 10:
                wpa = body[4:]
            elif body[3] == 4:
                wps = True
        i += 2 + ln
    if wpa is not None:
        akms, cipher = _rsn(wpa, wpa1=True)
        parts.append(f"[WPA-{'+'.join(akms)}-{cipher}]")
    if rsn is not None:
        akms, cipher = _rsn(rsn)
        joined = "+".join(akms) or "PSK"
        if any(a in ("PSK", "EAP", "FT/PSK", "FT/EAP", "PSK-SHA256", "EAP-SHA256") for a in akms):
            parts.append(f"[WPA2-{joined}-{cipher}]")
        parts.append(f"[RSN-{joined}-{cipher}]")
    if not parts and cap_info & 0x0010:
        parts.append("[WEP]")
    if wps:
        parts.append("[WPS]")
    parts.append("[IBSS]" if cap_info & 0x0002 else "[ESS]")
    return "".join(parts)


def _filetime_now() -> int:
    ft = ctypes.c_ulonglong()
    ctypes.windll.kernel32.GetSystemTimePreciseAsFileTime(ctypes.byref(ft))
    return ft.value


class WifiScanner:
    def __init__(self) -> None:
        self.api = ctypes.windll.wlanapi
        self.handle = W.HANDLE()
        ver = W.DWORD()
        rc = self.api.WlanOpenHandle(2, None, ctypes.byref(ver), ctypes.byref(self.handle))
        if rc:
            raise OSError(f"Wi-Fi service unavailable (error {rc})")
        self.adapters: list[dict[str, Any]] = []
        self.off: set[str] = set()       # adapter names switched off

    def close(self) -> None:
        if self.handle:
            self.api.WlanCloseHandle(self.handle, None)
            self.handle = W.HANDLE()

    def _interfaces(self) -> list[_IfInfo]:
        pl = ctypes.POINTER(_IfList)()
        if self.api.WlanEnumInterfaces(self.handle, None, ctypes.byref(pl)):
            return []
        try:
            n = pl.contents.n
            arr = ctypes.cast(ctypes.addressof(pl.contents.items), ctypes.POINTER(_IfInfo * n)).contents
            return [_IfInfo.from_buffer_copy(bytes(it)) for it in arr]
        finally:
            self.api.WlanFreeMemory(pl)

    def trigger(self) -> list[_IfInfo]:
        """Ask every adapter to scan. Returns the adapters."""
        ifs = self._interfaces()
        for it in ifs:
            if it.desc in self.off:
                continue
            self.api.WlanScan(self.handle, ctypes.byref(it.guid), None, None, None)
        return ifs

    def read(self, ifs: list[_IfInfo]) -> list[dict[str, Any]]:
        """Fresh networks across all adapters; the strongest reading wins when
        two adapters hear the same network."""
        best: dict[str, dict[str, Any]] = {}
        now_ft, now_mono, now_utc = _filetime_now(), time.monotonic(), time.time()
        adapters = []
        for it in ifs:
            seen = 0
            if it.desc in self.off:
                adapters.append({"name": it.desc, "state": "off", "networks": 0})
                continue
            bl = ctypes.POINTER(_BssList)()
            if self.api.WlanGetNetworkBssList(self.handle, ctypes.byref(it.guid), None, 0, False, None,
                                              ctypes.byref(bl)):
                adapters.append({"name": it.desc, "state": _IF_STATES.get(it.state, "?"), "networks": 0})
                continue
            try:
                n = bl.contents.n
                ents = ctypes.cast(ctypes.addressof(bl.contents.e), ctypes.POINTER(_BssEntry * n)).contents if n else []
                for e in ents:
                    age = max(0.0, (now_ft - e.hostTimestamp) / 1e7)
                    if age > FRESH_SECONDS:
                        continue
                    mac = ":".join(f"{b:02x}" for b in e.bssid)
                    if mac == "00:00:00:00:00:00":
                        continue
                    seen += 1
                    rssi = int(e.rssi)
                    if mac in best and best[mac]["rssi"] >= rssi:
                        continue
                    ies = bytes((ctypes.c_ubyte * e.ieSize).from_address(ctypes.addressof(e) + e.ieOffset))
                    freq = int(e.chCenterFreq // 1000)
                    best[mac] = {
                        "mac": mac,
                        "ssid": e.ssid.ssid[:min(32, e.ssid.len)].decode("utf-8", "replace"),
                        "rssi": rssi, "freq": freq, "channel": channel(freq),
                        "auth": auth_mode(ies, e.capInfo), "adapter": it.desc,
                        "at": now_mono - age, "utc": now_utc - age,   # when it was actually heard
                    }
            finally:
                self.api.WlanFreeMemory(bl)
            adapters.append({"name": it.desc, "state": _IF_STATES.get(it.state, "?"), "networks": seen})
        self.adapters = adapters
        return list(best.values())
