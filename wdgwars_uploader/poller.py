"""Background poller: keeps the dashboard's live feed, counters, device status
and WDGWars stats fresh without uploading anything. Each task runs on its own
cadence so the cheap ones (aircraft HTTP read) stay snappy while the expensive
one (mesh serial read) runs less often."""

from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger("wdgwars.poller")


class Poller:
    def __init__(self, core, *, device_every=15.0, aircraft_every=10.0,
                 mesh_every=60.0, stats_every=120.0, wardrift_mesh_every=900.0) -> None:
        self.core = core
        self.intervals = {
            "device": device_every,
            "aircraft": aircraft_every,
            "mesh": mesh_every,
            "stats": stats_every,
            "wardrift_mesh": wardrift_mesh_every,
        }
        self._next = {k: 0.0 for k in self.intervals}
        self._next["wardrift_mesh"] = time.time() + 120   # let the radios connect first
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="wdgw-poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self, name: str) -> None:
        try:
            if name == "device":
                self.core.poll_devices()
            elif name == "aircraft":
                self.core.poll_aircraft()
            elif name == "mesh":
                self.core.poll_mesh()
            elif name == "stats":
                self.core.refresh_stats()
            elif name == "wardrift_mesh":
                self.core.wardrift_mesh_tick()
        except Exception:
            log.debug("poll task %s failed", name, exc_info=True)

    def _loop(self) -> None:
        # Kick device + stats immediately on startup.
        self._run("device")
        self._run("stats")
        while not self._stop.is_set():
            now = time.time()
            for name, every in self.intervals.items():
                if now >= self._next[name]:
                    # Don't fight an in-progress upload for the serial port.
                    if name == "mesh" and self.core.state.upload_in_progress:
                        self._next[name] = now + 5
                        continue
                    self._run(name)
                    self._next[name] = time.time() + every
            self._stop.wait(2.0)
