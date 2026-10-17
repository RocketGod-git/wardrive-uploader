"""Thread-safe live state shared between the poller, scheduler, uploader and the
dashboard's JSON API. One instance is created in app startup and passed around.
"""

from __future__ import annotations

import collections
import json
import os
import tempfile
import threading
import time
from typing import Any

from .config import config_dir


def _map_node(n: dict) -> dict:
    """Minimal fields the live map needs for a mesh marker + popup."""
    return {"node_id": n.get("node_id"), "name": n.get("name"),
            "node_type": n.get("node_type"), "lat": n.get("lat"),
            "lon": n.get("lon"), "network": n.get("network")}


class LiveState:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.started = time.time()

        # Device presence / health
        self.devices: dict[str, Any] = {
            "rtlsdr": {"present": False, "label": "", "driver_ok": None},
            "portapack": {"present": False, "port": "", "label": "", "connected": False,
                          "device": "", "error": "", "aircraft": 0},
            "mesh": {"present": False, "port": "", "connected": False,
                     "name": "", "contacts": 0, "transport": "serial"},
        }

        # Two distinct notions:
        #  * _*_live  = what's visible right now (replaced every poll) -> feed/map
        #  * _*_acc   = everything seen since the last successful upload, so an
        #              aircraft that flew away is still uploaded -> queued count
        self._aircraft_live: list[dict] = []
        self._aircraft_acc: dict[str, dict] = {}      # raw dump1090 dicts by HEX
        self._mesh_live: list[dict] = []
        self._mesh_acc: dict[str, dict] = {}          # node dicts by node_id
        # Persistent record of what has already been uploaded, keyed by hex/node_id
        # -> last-sent ts. A still-visible item that was already sent is not
        # re-staged: the server dedupes it as "already_seen", so re-sending gains
        # nothing and just makes "staged to send" jump back up right after an
        # upload.
        self._sent_aircraft: dict[str, float] = {}
        self._sent_mesh: dict[str, float] = {}
        self._SENT_TTL = 24 * 3600.0                  # re-confirm at most once/day
        self._SENT_CAP = 100000
        self.aircraft_seen_ts: float = 0.0
        self.mesh_seen_ts: float = 0.0
        self._ACC_CAP = 20000                          # safety cap if uploads fail
        self._persist_ts = 0.0
        self._persist_every = 15.0                     # throttle disk writes
        self._load_acc()                               # restore un-uploaded queue

        # Counters
        self.session_uploaded = {"aircraft": 0, "mesh": 0}   # NEW to account (imported)
        self._seen_aircraft: set[str] = set()   # unique collected this session
        self._seen_mesh: set[str] = set()

        # Is an ADS-B source (dump1090) actually serving? None=unknown until first
        # poll, True=serving (even if the sky is empty), False=not reachable.
        self.adsb_source_ok: bool | None = None

        # Upload status
        self.upload_in_progress = False
        self.last_cycle: dict[str, Any] = {}
        self.next_run_ts: float = 0.0
        self.interval_hours: int = 4

        # Cached WDGWars data
        self.stats: dict[str, Any] = {}
        self.stats_ts: float = 0.0
        self.leaderboard: dict[str, Any] = {}
        self.leaderboard_ts: float = 0.0

        # Prerequisites
        self.prereqs: dict[str, Any] = {}

        # Config summary (never contains the key itself)
        self.config_summary: dict[str, Any] = {}

        # Serial ports offered in the mesh device picker: {port, label, is_mesh}.
        self.serial_ports: list[dict[str, Any]] = []

        # Activity log (kept longer so the dashboard panel scrolls through history)
        self._events: collections.deque = collections.deque(maxlen=200)

    # -- devices -----------------------------------------------------------
    def set_devices(self, **kw) -> None:
        with self._lock:
            for k, v in kw.items():
                self.devices[k] = v

    def update_device(self, name: str, **fields) -> None:
        """Atomically merge fields into one device card (no lost updates between
        the poller and the device-poll threads)."""
        with self._lock:
            d = dict(self.devices.get(name, {}))
            d.update(fields)
            self.devices[name] = d

    # -- feeds -------------------------------------------------------------
    def update_aircraft(self, display: list[dict], raw_positioned: list[dict]) -> None:
        """display = current-view items (for feed/map); raw_positioned = raw
        dump1090 dicts to accumulate for upload."""
        with self._lock:
            self.aircraft_seen_ts = time.time()
            self._aircraft_live = display
            for r in raw_positioned:
                h = (r.get("hex") or "").upper()
                if not h:
                    continue
                self._seen_aircraft.add(h)
                if self._recently_sent(self._sent_aircraft, h):
                    continue   # already uploaded and still confirmed — don't re-stage
                self._aircraft_acc[h] = r
            self._cap(self._aircraft_acc)
        self.persist()

    # Exactly the fields WDGWars accepts for a mesh node — anything else on a
    # display node (uploadable flag, telemetry) is dropped before upload.
    _MESH_UPLOAD_FIELDS = ("node_id", "node_type", "name", "lat", "lon", "rssi",
                           "first_seen", "type", "network", "public_key")

    def update_mesh(self, nodes: list[dict]) -> None:
        """``nodes`` is EVERYTHING heard (for the live feed). Only nodes flagged
        ``uploadable`` (valid id + real coords) are staged for the WDGWars upload, and
        only their whitelisted fields are stored, so display-only telemetry never
        reaches the payload.
        """
        with self._lock:
            self.mesh_seen_ts = time.time()
            self._mesh_live = nodes
            for n in nodes:
                nid = n.get("node_id")
                if nid and n.get("uploadable") and n.get("lat") is not None and n.get("lon") is not None:
                    self._seen_mesh.add(nid)
                    if self._recently_sent(self._sent_mesh, nid):
                        continue   # already uploaded and still confirmed — don't re-stage
                    self._mesh_acc[nid] = {k: n[k] for k in self._MESH_UPLOAD_FIELDS if k in n}
            self._cap(self._mesh_acc)
        self.persist()

    def _cap(self, d: dict) -> None:
        if len(d) > self._ACC_CAP:
            for k in list(d.keys())[: len(d) - self._ACC_CAP]:
                d.pop(k, None)

    def pending(self) -> dict[str, int]:
        """Count staged for the next upload (accumulated since last upload)."""
        with self._lock:
            return {"aircraft": len(self._aircraft_acc), "mesh": len(self._mesh_acc)}

    def acc_aircraft_raw(self) -> list[dict]:
        with self._lock:
            return list(self._aircraft_acc.values())

    def acc_mesh_nodes(self) -> list[dict]:
        with self._lock:
            return list(self._mesh_acc.values())

    def _recently_sent(self, sent: dict, key: str) -> bool:
        ts = sent.get(key)
        return ts is not None and (time.time() - ts) < self._SENT_TTL

    def _prune_sent(self, sent: dict) -> None:
        now = time.time()
        for k in [k for k, ts in sent.items() if now - ts >= self._SENT_TTL]:
            sent.pop(k, None)
        if len(sent) > self._SENT_CAP:   # keep the most recently sent
            for k in sorted(sent, key=sent.get)[: len(sent) - self._SENT_CAP]:
                sent.pop(k, None)

    def mark_aircraft_uploaded(self, hexes) -> None:
        """Record these as sent (so they aren't re-staged) and drop them from the
        pending batch. Called after a SUCCESSFUL upload with the exact keys sent."""
        now = time.time()
        with self._lock:
            for h in hexes:
                h = (h or "").upper()
                if h:
                    self._sent_aircraft[h] = now
                    self._aircraft_acc.pop(h, None)
            self._prune_sent(self._sent_aircraft)
        self.persist(force=True)

    def mark_mesh_uploaded(self, ids) -> None:
        now = time.time()
        with self._lock:
            for i in ids:
                if i:
                    self._sent_mesh[i] = now
                    self._mesh_acc.pop(i, None)
            self._prune_sent(self._sent_mesh)
        self.persist(force=True)

    # -- persistence: the un-uploaded queue survives restarts/crashes ------
    def _queue_path(self):
        return config_dir() / "queue.json"

    def _load_acc(self) -> None:
        try:
            p = self._queue_path()
            if not p.exists():
                return
            data = json.loads(p.read_text(encoding="utf-8"))
            air = data.get("aircraft", {})
            mesh = data.get("mesh", {})
            if isinstance(air, dict):
                self._aircraft_acc = {k: v for k, v in air.items() if isinstance(v, dict)}
            if isinstance(mesh, dict):
                self._mesh_acc = {k: v for k, v in mesh.items() if isinstance(v, dict)}
            sa, sm = data.get("sent_aircraft", {}), data.get("sent_mesh", {})
            if isinstance(sa, dict):
                self._sent_aircraft = {k: float(v) for k, v in sa.items() if isinstance(v, (int, float))}
            if isinstance(sm, dict):
                self._sent_mesh = {k: float(v) for k, v in sm.items() if isinstance(v, (int, float))}
            self._prune_sent(self._sent_aircraft)
            self._prune_sent(self._sent_mesh)
        except (OSError, ValueError):
            pass

    def persist(self, force: bool = False) -> None:
        """Write the accumulated (un-uploaded) queue to disk atomically. Throttled
        unless ``force`` — call force=True on shutdown and after clearing."""
        now = time.time()
        with self._lock:
            if not force and (now - self._persist_ts) < self._persist_every:
                return
            self._persist_ts = now
            payload = json.dumps({"aircraft": self._aircraft_acc, "mesh": self._mesh_acc,
                                   "sent_aircraft": self._sent_aircraft, "sent_mesh": self._sent_mesh})
        try:
            path = self._queue_path()
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(payload)
                os.replace(tmp, path)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        except OSError:
            pass

    # -- events ------------------------------------------------------------
    def event(self, msg: str) -> None:
        with self._lock:
            self._events.appendleft({"t": time.time(), "msg": msg})

    # -- snapshot for the API ---------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            mt_live = [n for n in self._mesh_live if n.get("network") == "meshtastic"]
            mc_live = [n for n in self._mesh_live if n.get("network") == "meshcore"]
            return {
                "now": time.time(),
                "uptime": time.time() - self.started,
                "devices": self.devices,
                "pending": {"aircraft": len(self._aircraft_acc), "mesh": len(self._mesh_acc)},
                "live": {"aircraft": len(self._aircraft_live), "mesh": len(self._mesh_live),
                         "meshtastic": len(mt_live), "meshcore": len(mc_live),
                         # how many of the heard nodes actually have a GPS fix (the
                         # uploadable subset) — makes "heard vs collected" legible.
                         "meshtastic_gps": sum(1 for n in mt_live if n.get("lat") is not None),
                         "meshcore_gps": sum(1 for n in mc_live if n.get("lat") is not None)},
                "feed": {
                    "aircraft": self._aircraft_live[:60],
                    "mesh": self._mesh_live[:60],           # kept for back-compat
                    "meshtastic": mt_live[:80],             # each panel gets its own list so
                    "meshcore": mc_live[:80],               # one protocol can't crowd out the other
                },
                # Uncapped positioned nodes for the live map (lean fields) — every
                # located node plots, not just the feed's capped window.
                "map": {
                    "aircraft": self._aircraft_live,
                    "meshtastic": [_map_node(n) for n in mt_live if n.get("lat") is not None][:3000],
                    "meshcore": [_map_node(n) for n in mc_live if n.get("lat") is not None][:3000],
                },
                "aircraft_seen_ts": self.aircraft_seen_ts,
                "mesh_seen_ts": self.mesh_seen_ts,
                "adsb_source_ok": self.adsb_source_ok,
                "session_uploaded": self.session_uploaded,
                "collected": {"aircraft": len(self._seen_aircraft), "mesh": len(self._seen_mesh)},
                "upload_in_progress": self.upload_in_progress,
                "last_cycle": self.last_cycle,
                "next_run_ts": self.next_run_ts,
                "interval_hours": self.interval_hours,
                "stats": self.stats,
                "stats_ts": self.stats_ts,
                "leaderboard": self.leaderboard,
                "leaderboard_ts": self.leaderboard_ts,
                "prereqs": self.prereqs,
                "config": self.config_summary,
                "serial_ports": self.serial_ports,
                "events": list(self._events)[:100],
            }
