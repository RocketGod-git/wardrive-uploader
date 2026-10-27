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
from typing import Any

log = logging.getLogger("wdgwars.ble")


class BleScanner:
    def __init__(self) -> None:
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
            mfgr = next(iter(adv.manufacturer_data), None) if adv.manufacturer_data else None
            rec = {"mac": dev.address.lower(), "name": adv.local_name or "", "rssi": int(adv.rssi),
                   "mfgr": mfgr, "at": time.monotonic(), "utc": time.time()}
            with self._lock:
                prev = self._seen.get(rec["mac"])
                if prev and not rec["name"]:
                    rec["name"] = prev["name"]       # names come in scan responses only
                self._seen[rec["mac"]] = rec

        async with BleakScanner(on_adv, scanning_mode="active"):
            self.state, self.detail = "scanning", ""
            while not self._stop.is_set():
                await asyncio.sleep(1)

    def fresh(self, max_age: float = 6.0) -> list[dict[str, Any]]:
        """Devices heard within ``max_age`` seconds; older ones are dropped."""
        now = time.monotonic()
        with self._lock:
            for k in [k for k, v in self._seen.items() if now - v["at"] > 120]:
                del self._seen[k]
            return [dict(v) for v in self._seen.values() if now - v["at"] <= max_age]
