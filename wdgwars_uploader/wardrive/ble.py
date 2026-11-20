"""Bluetooth Low Energy scanning (bleak, Windows advertisement watcher).

Windows runs one Bluetooth radio at a time, so a plugged-in dongle is used when
Windows makes it the active adapter. Scanning restarts by itself if Bluetooth is
turned off or the adapter changes.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Callable

log = logging.getLogger("wdgwars.ble")

# While a WardriveESP board copies its SD runs over Bluetooth, the PC's active scans stop: on Windows an active scan
# takes the radio's time from every link, and a copy that ran at 55-160 KB/s with no scan crawled at 2-8 KB/s with one
# (bench 2026-11-20). Counted, so two pauses need two resumes. The board's own scan is paused for the copy the same way.
_pause_lock = threading.Lock()
_pauses = 0


def pause_scans() -> None:
    global _pauses
    with _pause_lock:
        _pauses += 1


def resume_scans() -> None:
    global _pauses
    with _pause_lock:
        _pauses = max(0, _pauses - 1)


def scans_paused() -> bool:
    return _pauses > 0


class BleScanner:
    def __init__(self, on_advert: Callable[[Any, Any], None] | None = None) -> None:
        self.on_advert = on_advert        # every advert, for the WardriveESP bridge
        self._lock = threading.Lock()
        self._seen: dict[str, dict[str, Any]] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.state = "starting"
        self.detail = ""

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="wardrive-ble", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                asyncio.run(self._scan())
            except Exception as e:  # noqa: BLE001 - Bluetooth off, no adapter, stack hiccup
                self.state, self.detail = "unavailable", str(e)[:120] or type(e).__name__
                log.info("BLE scan stopped: %s", self.detail)
            self._stop.wait(15)

    async def _scan(self) -> None:
        from bleak import BleakScanner

        def on_adv(dev, adv) -> None:
            if self.on_advert:
                try:
                    self.on_advert(dev, adv)
                except Exception:  # noqa: BLE001
                    log.debug("board advert hook failed", exc_info=True)
            mfgr = next(iter(adv.manufacturer_data), None) if adv.manufacturer_data else None
            rec = {"mac": dev.address.lower(), "name": adv.local_name or "", "rssi": int(adv.rssi),
                   "mfgr": mfgr, "at": time.monotonic(), "utc": time.time()}
            with self._lock:
                prev = self._seen.get(rec["mac"])
                if prev and not rec["name"]:
                    rec["name"] = prev["name"]       # names come in scan responses only
                self._seen[rec["mac"]] = rec

        while not self._stop.is_set():
            if scans_paused():   # a board is copying its SD runs (see pause_scans)
                self.state, self.detail = "paused", "a board is sending its SD card"
                await asyncio.sleep(0.5)
                continue
            async with BleakScanner(on_adv, scanning_mode="active"):
                self.state, self.detail = "scanning", ""
                while not self._stop.is_set() and not scans_paused():
                    await asyncio.sleep(0.5)

    def fresh(self, max_age: float = 6.0) -> list[dict[str, Any]]:
        """Devices heard within ``max_age`` seconds; older ones are dropped."""
        now = time.monotonic()
        with self._lock:
            for k in [k for k, v in self._seen.items() if now - v["at"] > 120]:
                del self._seen[k]
            return [dict(v) for v in self._seen.values() if now - v["at"] <= max_age]
