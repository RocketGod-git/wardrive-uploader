"""Thread-safe live state shared between the poller, scheduler, uploader and the
dashboard's JSON API. One instance is created in app startup and passed around.
"""

from __future__ import annotations

import collections
import copy
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


def _utc_day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


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
        # Wardrift queue: aircraft are staged separately because Wardrift credits
        # once per aircraft per UTC observation day. _wardrift_sent maps hex to
        # the observation day last delivered.
        self.wardrift_enabled = False
        self._wardrift_acc: dict[str, dict] = {}
        self._wardrift_sent: dict[str, str] = {}
        self._SENT_TTL = 24 * 3600.0                  # re-confirm at most once/day
        self._SENT_CAP = 100000
        self.aircraft_seen_ts: float = 0.0
        self.mesh_seen_ts: float = 0.0
        self._ACC_CAP = 20000                          # safety cap if uploads fail
        self._persist_ts = 0.0
        self._persist_every = 15.0                     # throttle disk writes
        self._load_acc()                               # restore un-uploaded queue
        # Dashboard history: WDGWars totals per day (for the trend graph) and the
        # Wardrift credits this uploader earned per UTC day (from upload receipts).
        self.wdgwars_days: dict[str, dict] = {}
        self.wigle_days: dict[str, dict] = {}
        self.wardrift_days: dict[str, dict] = {}
        self._load_history()
        self.wardrift: dict[str, Any] = {"session": "signed_out", "account": {},
                                         "leaderboard": {}, "error": ""}

        # Counters
        self.session_uploaded = {"aircraft": 0, "mesh": 0}   # NEW to account (imported)
        self._seen_aircraft: set[str] = set()   # unique positioned targets since the app started
        self._seen_mesh: set[str] = set()       # mesh nodes heard since the app started
        # Every unique target this PC has collected, kept across restarts.
        self._all_aircraft: set[str] = set()
        self._all_mesh: set[str] = set()
        self._all_dirty = False
        self._all_saved = 0.0
        self._load_collected()

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
        self.wigle: dict[str, Any] = {}       # WiGLE /stats/user for the YOU card
        self._receipts: dict[str, list[dict[str, Any]]] = {}
        # MeshReporter owns the durable receipts; these are the selected account's display rows.
        self._mesh_receipts: list[dict[str, Any]] = []
        self.wardrift_mesh: dict[str, Any] = {}

        # Prerequisites
        self.prereqs: dict[str, Any] = {}
        self.update_status: dict[str, Any] = {}
        self.wardrive_snapshot = None   # set by the core; returns the wardriving panel

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
                if h not in self._all_aircraft:
                    self._all_aircraft.add(h)
                    self._all_dirty = True
                if (self.wardrift_enabled and isinstance(r.get("_obs_ts"), (int, float))
                        and self._wardrift_sent.get(h) != _utc_day(r["_obs_ts"])):
                    self._wardrift_acc[h] = r
                if self._recently_sent(self._sent_aircraft, h):
                    continue   # already uploaded and still confirmed — don't re-stage
                self._aircraft_acc[h] = r
            self._cap(self._aircraft_acc)
            self._cap(self._wardrift_acc)
        self.persist()
        self.save_collected()

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
                    # The node store carries nodes over from earlier runs; only
                    # ones heard since this start count toward "since start".
                    if float(n.get("heard_ts") or 0) >= self.started:
                        self._seen_mesh.add(nid)
                    if nid not in self._all_mesh:
                        self._all_mesh.add(nid)
                        self._all_dirty = True
                    if self._recently_sent(self._sent_mesh, nid):
                        continue   # already uploaded and still confirmed — don't re-stage
                    self._mesh_acc[nid] = {k: n[k] for k in self._MESH_UPLOAD_FIELDS if k in n}
            self._cap(self._mesh_acc)
        self.persist()
        self.save_collected()

    def _cap(self, d: dict) -> None:
        if len(d) > self._ACC_CAP:
            for k in list(d.keys())[: len(d) - self._ACC_CAP]:
                d.pop(k, None)

    def pending(self) -> dict[str, int]:
        """Count staged for the next upload (accumulated since last upload)."""
        with self._lock:
            return {"aircraft": len(self._aircraft_acc), "mesh": len(self._mesh_acc),
                    "wardrift": len(self._wardrift_acc)}

    def acc_aircraft_raw(self) -> list[dict]:
        with self._lock:
            return list(self._aircraft_acc.values())

    def acc_wardrift_raw(self) -> list[dict]:
        with self._lock:
            return list(self._wardrift_acc.values())

    def mark_wardrift_uploaded(self, records) -> None:
        """Drop delivered aircraft from the Wardrift queue and remember their
        observation day, so the same aircraft is not re-sent that day."""
        with self._lock:
            for r in records:
                h = (r.get("hex") or "").upper()
                if not h:
                    continue
                if isinstance(r.get("_obs_ts"), (int, float)):
                    self._wardrift_sent[h] = _utc_day(r["_obs_ts"])
                if self._wardrift_acc.get(h) is r:
                    self._wardrift_acc.pop(h, None)
            cutoff = _utc_day(time.time() - 3 * 86400)
            for h in [h for h, d in self._wardrift_sent.items() if d < cutoff]:
                self._wardrift_sent.pop(h, None)
        self.persist(force=True)

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
            wd, ws = data.get("wardrift", {}), data.get("wardrift_sent", {})
            if isinstance(wd, dict):
                self._wardrift_acc = {k: v for k, v in wd.items() if isinstance(v, dict)}
            if isinstance(ws, dict):
                self._wardrift_sent = {k: v for k, v in ws.items() if isinstance(v, str)}
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
                                   "sent_aircraft": self._sent_aircraft, "sent_mesh": self._sent_mesh,
                                   "wardrift": self._wardrift_acc,
                                   "wardrift_sent": self._wardrift_sent})
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

    # -- all-time collected ------------------------------------------------
    def _collected_path(self):
        return config_dir() / "collected.json"

    def _load_collected(self) -> None:
        try:
            data = json.loads(self._collected_path().read_text(encoding="utf-8"))
            self._all_aircraft = {str(x) for x in data.get("aircraft") or []}
            self._all_mesh = {str(x) for x in data.get("mesh") or []}
        except (OSError, ValueError, AttributeError):
            pass

    def save_collected(self, force: bool = False) -> None:
        now = time.time()
        with self._lock:
            if not self._all_dirty or (not force and now - self._all_saved < 60):
                return
            self._all_dirty, self._all_saved = False, now
            payload = json.dumps({"aircraft": sorted(self._all_aircraft), "mesh": sorted(self._all_mesh)})
        try:
            path = self._collected_path()
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp, path)
        except OSError:
            pass

    # -- dashboard history -------------------------------------------------
    def _history_path(self):
        return config_dir() / "history.json"

    def _load_history(self) -> None:
        try:
            data = json.loads(self._history_path().read_text(encoding="utf-8"))
            self.wdgwars_days = {k: v for k, v in (data.get("wdgwars") or {}).items() if isinstance(v, dict)}
            self.wigle_days = {k: v for k, v in (data.get("wigle") or {}).items() if isinstance(v, dict)}
            self.wardrift_days = {k: v for k, v in (data.get("wardrift") or {}).items() if isinstance(v, dict)}
        except (OSError, ValueError, AttributeError):
            pass

    def _save_history(self) -> None:
        cutoff = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 120 * 86400))
        with self._lock:
            for d in (self.wdgwars_days, self.wardrift_days, self.wigle_days):
                for k in [k for k in d if k < cutoff]:
                    d.pop(k, None)
            payload = json.dumps({"wdgwars": self.wdgwars_days, "wardrift": self.wardrift_days,
                                  "wigle": self.wigle_days})
        try:
            path = self._history_path()
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp, path)
        except OSError:
            pass

    def record_wdgwars_totals(self, stats: dict) -> None:
        """Keep the latest WDGWars totals for each local day."""
        day = time.strftime("%Y-%m-%d")
        row = {k: int(stats.get(k) or 0) for k in ("wifi", "ble", "aircraft", "mesh", "total")}
        with self._lock:
            if self.wdgwars_days.get(day) == row:
                return
            self.wdgwars_days[day] = row
        self._save_history()

    def record_wigle_totals(self, me: dict) -> None:
        """Keep the latest WiGLE totals for each local day."""
        day = time.strftime("%Y-%m-%d")
        row = {k: int(me.get(k) or 0) for k in ("wifi_gps", "bt", "cell", "rank")}
        with self._lock:
            if self.wigle_days.get(day) == row:
                return
            self.wigle_days[day] = row
        self._save_history()

    def add_wardrift_earned(self, aircraft: int, xp: int, influence: int, currency: int) -> None:
        """Add one upload receipt's credits to today's (UTC) tally."""
        day = time.strftime("%Y-%m-%d", time.gmtime())
        with self._lock:
            row = self.wardrift_days.setdefault(day, {"aircraft": 0, "xp": 0, "influence": 0, "currency": 0})
            for k, v in (("aircraft", aircraft), ("xp", xp), ("influence", influence), ("currency", currency)):
                row[k] = int(row.get(k) or 0) + int(v or 0)
        self._save_history()

    def reconcile_mesh(self, status: dict[str, Any], receipts: list[dict[str, Any]]) -> None:
        """Show acknowledged mesh receipts for the selected account without replaying
        awards: one row per upload, as before, and none for the periodic radio report."""
        rows, seen, uploads = [], set(), {}
        account = status.get("account_id")
        for record in receipts:
            receipt = record.get("receipt") or {}
            rid = receipt.get("receipt_id")
            if not account or record.get("account_id") != account or not rid or rid in seen:
                continue
            seen.add(rid)
            if record.get("report"):
                continue
            samples = receipt.get("samples") or []
            row = uploads.get(record.get("upload_id") or rid)
            if row is None:
                row = {"t": record["accepted_at"], "kind": "mesh", "ok": True,
                       "receipt_id": rid, "batch_id": receipt.get("batch_id"),
                       "account_id": account, "sent": 0, "telemetry": 0, "currency": 0, "influence": 0,
                       "availability_seconds": 0, "_samples": []}
                uploads[record.get("upload_id") or rid] = row
                rows.append(row)
            row["t"] = max(row["t"], record["accepted_at"])
            row["sent"] += record.get("nodes", 0)
            row["telemetry"] += receipt.get("reports_accepted", 0)
            row["currency"] += receipt.get("currency", 0)
            row["influence"] += receipt.get("influence", 0)
            row["availability_seconds"] += sum(s.get("availability_seconds") or 0 for s in samples)
            row["_samples"] += samples
        for row in rows:
            samples = row.pop("_samples")
            for key in ("forwarded", "suppressed", "attempted", "failed"):
                known = [s[key] for s in samples if s.get(key) is not None]
                row[key] = sum(known) if known else None
            row["summary"] = (f"{row['sent']} nodes acknowledged, {row['telemetry']} health samples, "
                              f"+{row['currency']} currency, +{row['influence']:g} influence")
        with self._lock:
            self.wardrift_mesh = copy.deepcopy(status)
            self._mesh_receipts = sorted(rows, key=lambda r: r["t"])[-500:]

    # -- events ------------------------------------------------------------
    def event(self, msg: str) -> None:
        with self._lock:
            self._events.appendleft({"t": time.time(), "msg": msg})

    # -- snapshot for the API ---------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        try:
            wardrive = self.wardrive_snapshot() if self.wardrive_snapshot else {}
        except Exception:   # never let the wardriving panel break the dashboard
            wardrive = {}
        with self._lock:
            mt_live = [n for n in self._mesh_live if n.get("network") == "meshtastic"]
            mc_live = [n for n in self._mesh_live if n.get("network") == "meshcore"]
            return {
                "now": time.time(),
                "uptime": time.time() - self.started,
                "devices": self.devices,
                "pending": {"aircraft": len(self._aircraft_acc), "mesh": len(self._mesh_acc),
                            "wardrift": len(self._wardrift_acc)},
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
                "wardrift_mesh": copy.deepcopy(self.wardrift_mesh),
                "adsb_source_ok": self.adsb_source_ok,
                "session_uploaded": self.session_uploaded,
                "collected": {"aircraft": len(self._seen_aircraft), "mesh": len(self._seen_mesh),
                              "aircraft_all": len(self._all_aircraft), "mesh_all": len(self._all_mesh)},
                "upload_in_progress": self.upload_in_progress,
                "last_cycle": self.last_cycle,
                "next_run_ts": self.next_run_ts,
                "interval_hours": self.interval_hours,
                "stats": self.stats,
                "stats_ts": self.stats_ts,
                "leaderboard": self.leaderboard,
                "wdgwars_days": dict(sorted(self.wdgwars_days.items())[-30:]),
                "wigle_days": dict(sorted(self.wigle_days.items())[-30:]),
                "wardrift": {**self.wardrift,
                             "days": dict(sorted(self.wardrift_days.items())[-30:]),
                             "earned_total": {k: sum(int(d.get(k) or 0) for d in self.wardrift_days.values())
                                              for k in ("aircraft", "xp", "influence", "currency")}},
                "leaderboard_ts": self.leaderboard_ts,
                "wigle": self.wigle,
                "prereqs": self.prereqs,
                "update": self.update_status,
                "wardrive": wardrive,
                "config": self.config_summary,
                "serial_ports": self.serial_ports,
                "events": list(self._events)[:100],
            }


    # -- upload receipts (sites without a readable upload history) -------------
    def _receipts_file(self, site: str):
        return config_dir() / f"receipts_{site}.jsonl"

    def add_receipt(self, site: str, kind: str, ok: bool, summary: str, **nums: Any) -> None:
        rec = {"t": round(time.time(), 1), "kind": kind, "ok": bool(ok), "summary": summary,
               **{k: v for k, v in nums.items() if v}}
        with self._lock:
            self.receipts(site)            # load what is on disk first
            self._receipts[site].append(rec)
            del self._receipts[site][:-500]
        try:
            with open(self._receipts_file(site), "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
        except OSError:
            pass

    def receipts(self, site: str) -> list[dict[str, Any]]:
        """Newest first, up to 500."""
        with self._lock:
            if site not in self._receipts:
                rows: list[dict[str, Any]] = []
                try:
                    with open(self._receipts_file(site), encoding="utf-8") as f:
                        for line in f:
                            try:
                                rows.append(json.loads(line))
                            except ValueError:
                                pass
                except OSError:
                    pass
                self._receipts[site] = rows[-500:]
            if site == "wardrift":
                rows = [r for r in self._receipts[site] if r.get("kind") != "mesh"]
                if not self._mesh_receipts:
                    return list(reversed(rows))
                rows += self._mesh_receipts
                return copy.deepcopy(sorted(rows, key=lambda r: r.get("t") or 0, reverse=True)[:500])
            return list(reversed(self._receipts[site]))
