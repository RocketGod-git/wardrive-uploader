"""Monitor-mode USB Wi-Fi adapters, found and started as they are plugged in.

Each supported adapter runs its own driver thread. Beacons and probe responses
become access points (BSSID, SSID, security, channel, strongest signal, and the
moment they were heard, so GPS can be matched to that moment); data frames go
to handshake and PMKID capture.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from .capture import Capture, Hit
from .wifi import auth_mode
from . import ar9271, mt7612u

log = logging.getLogger("wdgwars.monitor")

# (vid, pid) -> (driver class, chipset name)
SUPPORTED = {vp: (mt7612u.Mt7612u, "MediaTek MT7612U") for vp in mt7612u.VID_PIDS}
SUPPORTED.update({vp: (ar9271.Ar9271, "Atheros AR9271") for vp in ar9271.VID_PIDS})

# Monitor-capable chips Wardrive Go knows that have no desktop driver yet. They
# are listed as found so a plugged-in adapter never just vanishes.
KNOWN = {
    (0x0BDA, 0x0811): "Realtek RTL8821AU", (0x0BDA, 0x8812): "Realtek RTL8812AU",
    (0x0BDA, 0x881A): "Realtek RTL8812AU", (0x0BDA, 0x8813): "Realtek RTL8814AU",
    (0x0BDA, 0x8187): "Realtek RTL8187", (0x148F, 0x3070): "Ralink RT3070",
    (0x148F, 0x5370): "Ralink RT5370", (0x148F, 0x5372): "Ralink RT5372",
    (0x0E8D, 0x7961): "MediaTek MT7921AU",
}


class _Adapter:
    def __init__(self, key: str, name: str) -> None:
        self.key, self.name = key, name
        self.state = "starting"
        self.detail = ""
        self.channel = 0
        self.frames = 0
        self.thread: threading.Thread | None = None
        self.driver = None

    def status(self) -> dict[str, Any]:
        return {"id": f"mon:{self.key}", "name": self.name, "state": self.state, "detail": self.detail, "channel": self.channel,
                "frames": self.driver.frames if self.driver else self.frames, "monitor": True}


class MonitorManager:
    def __init__(self, on_hit: Callable[[Hit], None]) -> None:
        self._lock = threading.Lock()
        self._aps: dict[str, dict[str, Any]] = {}
        self.adapters: dict[str, _Adapter] = {}
        self.capture = Capture(on_hit)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.error = ""
        self.off: set[str] = set()        # USB port keys switched off

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._watch, name="monitor-watch", daemon=True)
        self._thread.start()

    def stop(self, wait: float = 4.0) -> None:
        """Stop every adapter and wait for its driver to cancel its USB transfers
        and close, so nothing is left in flight when the app exits."""
        self._stop.set()
        adapters = list(self.adapters.values())
        for a in adapters:
            if a.driver:
                a.driver.stop_evt.set()
        end = time.monotonic() + wait
        for a in adapters:
            if a.thread and a.thread.is_alive():
                a.thread.join(max(0.0, end - time.monotonic()))

    # -- hotplug -----------------------------------------------------------------
    def _watch(self) -> None:
        try:
            import usb1
        except ImportError:
            self.error = "USB support missing (libusb1)"
            return
        while not self._stop.is_set():
            try:
                present = set()
                with usb1.USBContext() as ctx:
                    for dev in ctx.getDeviceIterator(skip_on_error=True):
                        vp = (dev.getVendorID(), dev.getProductID())
                        if vp not in SUPPORTED and vp not in KNOWN:
                            continue
                        key = f"{dev.getBusNumber()}-{'.'.join(map(str, dev.getPortNumberList() or []))}"
                        present.add(key)
                        if vp not in SUPPORTED:
                            with self._lock:
                                a = self.adapters.setdefault(key, _Adapter(key, KNOWN[vp]))
                                a.state, a.detail = "no desktop driver yet", "Wardrive Go supports this chip; its desktop driver is on the way"
                            continue
                        with self._lock:
                            a = self.adapters.get(key)
                            if key in self.off:
                                a = a or _Adapter(key, SUPPORTED[vp][1])
                                self.adapters[key] = a
                                if a.thread and a.thread.is_alive():
                                    if a.driver:
                                        a.driver.stop_evt.set()
                                else:
                                    a.state, a.detail = "off", ""
                                continue
                            if a and a.thread and a.thread.is_alive():
                                continue
                            if a and a.state == "needs WinUSB" and time.monotonic() - getattr(a, "tried", 0) < 30:
                                continue
                            if a and a.state == "needs replug":
                                continue      # retried once it has been unplugged
                            if a:
                                a.driver = None
                            a = a or _Adapter(key, SUPPORTED[vp][1])
                            a.tried = time.monotonic()
                            self.adapters[key] = a
                        a.thread = threading.Thread(target=self._run, args=(a, vp), name=f"monitor-{key}", daemon=True)
                        a.thread.start()
                with self._lock:
                    for key, a in self.adapters.items():
                        if key not in present and not (a.thread and a.thread.is_alive()):
                            a.state, a.detail = "unplugged", ""
            except Exception as e:  # noqa: BLE001
                self.error = f"USB scan failed: {e}"
                log.debug("USB scan failed", exc_info=True)
            self._stop.wait(4)

    def _run(self, a: _Adapter, vp: tuple[int, int]) -> None:
        import usb1
        driver_cls = SUPPORTED[vp][0]
        try:
            with usb1.USBContext() as ctx:
                dev = None
                for d in ctx.getDeviceIterator(skip_on_error=True):
                    k = f"{d.getBusNumber()}-{'.'.join(map(str, d.getPortNumberList() or []))}"
                    if k == a.key:
                        dev = d
                        break
                if dev is None:
                    a.state = "unplugged"
                    return

                def status(s: str) -> None:
                    a.detail = s

                drv = driver_cls(ctx, dev, lambda f, r, c: self._frame(f, r, c, a.name), status)
                a.driver = drv
                try:
                    drv.open()
                except usb1.USBError as e:
                    if e.value in (usb1.ERROR_ACCESS, usb1.ERROR_NOT_SUPPORTED, usb1.ERROR_NOT_FOUND):
                        a.state, a.detail = "needs WinUSB", "switch this adapter to the WinUSB driver to use monitor mode"
                        return
                    raise
                a.state = "loading firmware"
                try:
                    drv.load_firmware()
                except usb1.USBErrorTimeout:
                    a.state, a.detail = "needs replug", "the adapter stopped responding: unplug it and plug it back in"
                    return
                a.state = "starting radio"
                try:
                    drv.bring_up()
                except usb1.USBErrorTimeout:
                    a.state, a.detail = "needs replug", "the adapter stopped responding: unplug it and plug it back in"
                    return
                a.state = "live"
                a.watch = threading.Thread(target=self._channel_watch, args=(a, drv), daemon=True)
                a.watch.start()
                drv.run()
        except Exception as e:  # noqa: BLE001 - unplugged mid-run, USB error
            a.state, a.detail = "stopped", str(e)[:140]
            log.info("monitor adapter %s stopped: %s", a.name, e)
        finally:
            if a.driver:
                a.frames = a.driver.frames
                # The liveness check talks to the adapter from its own thread: let it
                # finish before the handle is closed, or it uses a freed handle.
                a.driver.stop_evt.set()
                w = getattr(a, "watch", None)
                if w is not None and w.is_alive():
                    w.join(5)
                try:
                    a.driver.close()
                except Exception:
                    pass
            if a.state == "live":
                a.state = "stopped"

    def _channel_watch(self, a: _Adapter, drv) -> None:
        while not drv.stop_evt.is_set() and a.state == "live":
            a.channel = drv.channel
            try:
                alive = drv.is_alive() if hasattr(drv, "is_alive") else drv.rd(mt7612u.MAC_CSR0) not in (0, 0xFFFFFFFF)
            except Exception:  # noqa: BLE001 - the adapter is going away; the run loop reports why
                return
            if not alive:
                a.state, a.detail = "stopped", "adapter unplugged"
                drv.stop_evt.set()
                return
            drv.stop_evt.wait(1)

    # -- frames ------------------------------------------------------------------
    def _frame(self, f: bytes, rssi: int, ch: int, adapter: str) -> None:
        ftype, sub = (f[0] >> 2) & 3, (f[0] >> 4) & 0xF
        if ftype == 0 and sub in (8, 5) and len(f) >= 36:
            bssid = f[16:22].hex(":")
            if f[16] & 1 or bssid == "00:00:00:00:00:00":
                return
            ies = f[36:]
            ssid = ""
            i = 0
            while i + 2 <= len(ies):
                tag, ln = ies[i], ies[i + 1]
                if tag == 0:
                    raw = ies[i + 2:i + 2 + ln]
                    ssid = "" if not any(raw) else raw.decode("utf-8", "replace")
                    break
                i += 2 + ln
            cap = f[34] | (f[35] << 8)
            now, utc = time.monotonic(), time.time()
            freq = mt7612u.freq_of(ch)
            with self._lock:
                prev = self._aps.get(bssid)
                # Keep the strongest reading of the last few seconds (and when it was heard,
                # for the GPS match); a weaker repeat only marks the network as still in view.
                if prev is None or now - prev["at"] > 4 or rssi >= prev["rssi"]:
                    self._aps[bssid] = {"mac": bssid, "ssid": ssid or (prev or {}).get("ssid", ""), "rssi": rssi,
                                        "freq": freq, "channel": ch, "auth": auth_mode(ies, cap),
                                        "adapter": adapter, "at": now, "utc": utc, "seen": now}
                else:
                    prev["seen"] = now
                    if ssid and not prev["ssid"]:
                        prev["ssid"] = ssid
        elif ftype == 2:
            self.capture.scan(f, ch, rssi, self._ssid_for)

    def _ssid_for(self, bssid: str) -> str | None:
        with self._lock:
            ap = self._aps.get(bssid)
            return ap["ssid"] if ap and ap["ssid"] else None

    def fresh(self, max_age: float) -> list[dict[str, Any]]:
        now = time.monotonic()
        with self._lock:
            for k in [k for k, v in self._aps.items() if now - v["seen"] > 300]:
                del self._aps[k]
            return [dict(v) for v in self._aps.values() if now - v["seen"] <= max_age]

    def status(self) -> list[dict[str, Any]]:
        with self._lock:
            return [a.status() for a in self.adapters.values()]
