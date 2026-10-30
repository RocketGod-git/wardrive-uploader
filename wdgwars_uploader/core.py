"""Device-agnostic upload core plus display-only polling.

* ``run_cycle()`` reads both sources and uploads (used by the scheduler and the
  manual "Upload now").
* ``poll_*`` methods read for the live dashboard WITHOUT uploading, so counters
  and the incoming-data feed stay live between uploads.

All of it feeds a shared :class:`~wdgwars_uploader.state.LiveState`.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from . import adsb, detect, mayhem, mesh, mesh_capture, prereq, watchdog
from . import wardrift, wigle
from .wardrive.service import Wardriver
from .config import Config, config_dir, device_label
from .state import LiveState
from .uploader import WDGWarsClient

log = logging.getLogger("wdgwars.core")


@dataclass
class CycleReport:
    started: float = field(default_factory=time.time)
    adsb: str = "skipped"
    mesh: str = "skipped"
    wardrift: str = "skipped"
    wardrift_mesh: str = "skipped"
    wardrive: str = "skipped"
    key_ok: bool = False
    aircraft_sent: int = 0
    mesh_sent: int = 0
    errors: list[str] = field(default_factory=list)

    def line(self) -> str:
        return (f"ADS-B: {self.adsb} | Mesh: {self.mesh} | Wardrift: {self.wardrift}"
                f" | Wardrift mesh: {self.wardrift_mesh} | Wardrive: {self.wardrive}")

    def as_dict(self) -> dict[str, Any]:
        return {"ts": self.started, "adsb": self.adsb, "mesh": self.mesh,
                "wardrift": self.wardrift, "wardrift_mesh": self.wardrift_mesh, "wardrive": self.wardrive,
                "aircraft_sent": self.aircraft_sent, "mesh_sent": self.mesh_sent,
                "errors": self.errors}


def _aircraft_items(obj: dict[str, Any] | None) -> list[dict]:
    if not obj:
        return []
    out = []
    for a in obj.get("aircraft", []):
        if not isinstance(a, dict):
            continue
        if a.get("lat") is None or a.get("lon") is None:
            continue
        out.append({
            "hex": (a.get("hex") or "").upper(),
            "flight": (a.get("flight") or a.get("callsign") or "").strip(),
            "alt": a.get("alt_baro", a.get("altitude", a.get("alt_ft"))),
            "gs": a.get("gs", a.get("speed", a.get("speed_kt"))),
            "track": a.get("track", a.get("heading")),
            "lat": a.get("lat"), "lon": a.get("lon"),
            "seen": a.get("seen", 0),
            "squawk": a.get("squawk"),
            "category": a.get("category"),
            "vert_rate": a.get("vert_rate", a.get("baro_rate")),
            "rssi": a.get("rssi"),
            "messages": a.get("messages"),
        })
    return out


def _snapshot_time(obj: dict[str, Any] | None) -> float:
    """The decoder's clock for this snapshot; ``seen`` ages are relative to it."""
    now = time.time()
    try:
        snap = float((obj or {}).get("now"))
    except (TypeError, ValueError):
        return now
    return snap if 0 < snap <= now + 300 else now


def _stamp(a: dict[str, Any], snap: float) -> dict[str, Any]:
    """Copy of a decoder record with absolute observation times, so a batch
    sent hours later still carries when each aircraft was actually heard."""
    r = dict(a)
    try:
        r["_obs_ts"] = snap - max(0.0, float(a["seen"]))
    except (KeyError, TypeError, ValueError):
        r["_obs_ts"] = snap
    try:
        r["_pos_ts"] = snap - max(0.0, float(a["seen_pos"]))
    except (KeyError, TypeError, ValueError):
        r.pop("_pos_ts", None)
    return r


class UploaderCore:
    def __init__(self, cfg: Config | None = None, state: LiveState | None = None) -> None:
        self.cfg = cfg or Config()
        self.state = state or LiveState()
        self._last_detect: detect.DetectResult | None = None
        self._last_report: CycleReport | None = None
        self._last_advert_ts: float = 0.0
        self._mesh_suppressed_until: float = 0.0   # set after an un-recoverable serial hang
        # Persistent live-stream capture. The store persists to disk so captures
        # ACCUMULATE across restarts instead of re-seeding from the device's capped
        # node DB on every launch.
        self.capture = mesh_capture.MeshCaptureService(
            persist_path=str(config_dir() / "mesh_store.json"))
        self.wardrift_mesh = wardrift.MeshReporter(config_dir() / "wardrift_mesh.json")
        self._wardrift_mesh_lock = threading.RLock()   # cycle and radio report share sequence numbers
        self._mesh_stopping = threading.Event()
        self._mesh_schedule_lock = threading.Lock()
        self._mesh_upload_thread: threading.Thread | None = None
        self._wardrift_report_error = ""
        self.capture.set_credential_fingerprint(wardrift.credential_fingerprint(self.cfg.get("wardrift_api_key")))
        self._refresh_mesh_delivery()
        self._wd_boards: dict[tuple[str, str], dict] = {}
        self._wigle_cache: dict[str, tuple[float, dict]] = {}
        self.wardriver = Wardriver(on_event=self.state.event)
        self.state.wardrive_snapshot = lambda: self.wardriver.snapshot(self.cfg.as_dict())

    def client(self) -> WDGWarsClient:
        return WDGWarsClient(self.cfg.get("api_key", ""),
                             label=self.cfg.get("upload_device_label", ""))

    @property
    def last_detect(self):
        return self._last_detect

    @property
    def last_report(self):
        return self._last_report

    # -- config summary (no secrets) --------------------------------------
    def _push_config_summary(self) -> None:
        c = self.cfg.as_dict()
        self.state.config_summary = {
            "has_api_key": bool((c.get("api_key") or "").strip()),
            "has_wardrift_key": bool((c.get("wardrift_api_key") or "").strip()),
            "has_wardrift_session": bool(c.get("wardrift_session")),
            "wardrift_username": c.get("wardrift_username", "") if c.get("wardrift_session") else "",
            "upload_device_label": c.get("upload_device_label", ""),
            "upload_device_default": device_label(),
            "interval_hours": c.get("interval_hours", 4),
            "adsb_enabled": c.get("adsb_enabled", True),
            "mesh_enabled": c.get("mesh_enabled", True),
            "mesh_transport": c.get("mesh_transport", "serial"),
            "mesh_advert_minutes": c.get("mesh_advert_minutes", 60),
            "mesh_devices": c.get("mesh_devices", []),
            "mesh_hop_limit": c.get("mesh_hop_limit", 7),
            "adsb_json_url": c.get("adsb_json_url", ""),
            "adsb_source": c.get("adsb_source", "auto"),
            "adsb_active_source": adsb.active_source(),
            "mayhem_port": c.get("mayhem_port", "auto"),
            "dump1090_gain": c.get("dump1090_gain"),
            "start_on_boot": c.get("start_on_boot", False),
            "hw_off": list(c.get("hw_off") or []),
            "gps_net_port": int(c.get("gps_net_port") or 10110),
            "has_wigle": bool((c.get("wigle_api_name") or "").strip() and (c.get("wigle_api_token") or "").strip()),
            "wigle_api_name": (c.get("wigle_api_name") or "").strip(),
            "has_wpasec": bool((c.get("wpasec_key") or "").strip()),
        }
        self.state.interval_hours = int(c.get("interval_hours", 4))
        self.state.wardrift_enabled = bool((c.get("wardrift_api_key") or "").strip())

    # -- checks ------------------------------------------------------------
    def check_key(self) -> tuple[bool, str]:
        res = self.client().check_key()
        if res.ok:
            return True, f"key OK ({res.detail.get('username','?')})"
        return False, res.error or "key check failed"

    # -- device / prereq polling ------------------------------------------
    def poll_devices(self) -> detect.DetectResult:
        det = detect.detect()
        self._last_detect = det
        # Ports for the mesh device picker, likely mesh boards first. The
        # PortaPack is left out because the Mayhem reader owns its port.
        self.state.serial_ports = [
            {"port": d.port,
             "label": (d.chip_label or d.description or d.manufacturer or "").strip(),
             "is_mesh": bool(d.is_mesh_candidate)}
            for d in sorted(det.serial_ports, key=lambda d: (not d.is_mesh_candidate, d.port))
            if not d.is_portapack
        ]
        port = detect.pick_mesh_port(self.cfg.get("mesh_port", "auto"), det)
        # Merge (not replace) so driver_ok survives a flaky prereq check.
        self.state.update_device("rtlsdr", present=det.rtlsdr_present, label=det.rtlsdr_label)
        self.state.update_device("portapack", present=det.portapack_present,
                                 port=det.portapack_port, label=det.portapack_label)
        self.state.update_device("mesh", present=det.mesh_present, port=port or "",
                                 transport=self.cfg.get("mesh_transport", "serial"))
        try:
            self.state.prereqs = {"checks": prereq.check_all(det, self.cfg.as_dict())}
            for c in self.state.prereqs["checks"]:
                if c["name"] == "RTL-SDR driver":
                    self.state.update_device("rtlsdr", driver_ok=c["ok"])
        except Exception:
            log.exception("prereq check failed")
        self._push_config_summary()
        self.apply_wardrive(det)
        return det

    def apply_wardrive(self, det=None) -> None:
        """Start, stop or reconfigure wardriving to match the settings. Ports that
        belong to mesh radios or the PortaPack are never opened as a GPS."""
        det = det or self._last_detect
        busy = {d.get("port") for d in (self.cfg.get("mesh_devices") or [])
                if d.get("port") and d.get("transport", "serial") == "serial"}
        # WardriveESP boards share the mesh radios' USB serial chips, so they are told
        # apart by asking. Configured mesh radios and the PortaPack are never asked.
        skip = set()
        for d in (self.cfg.get("mesh_devices") or []):
            if d.get("transport", "serial") == "serial":
                skip.add(detect.pick_mesh_port(d.get("port", "auto"), det) if det else d.get("port"))
        mayhem_port = self.cfg.get("mayhem_port", "auto")
        if mayhem_port and mayhem_port.lower() != "auto":
            skip.add(mayhem_port)
        if det:
            busy |= {d.port for d in det.serial_ports if d.is_mesh_candidate or d.is_portapack}
            skip |= {d.port for d in det.serial_ports if d.is_portapack}
        skip.discard(None)
        try:
            self.wardriver.apply(self.cfg.as_dict(), busy, skip)
        except Exception:
            log.exception("wardriving setup failed")

    def poll_aircraft(self) -> int:
        if not self.cfg.get("adsb_enabled", True):
            self.state.adsb_source_ok = None
            return 0
        cfg = self.cfg.as_dict()
        sdr = self.state.devices.get("rtlsdr", {})
        adsb.ensure_decoder(cfg, sdr.get("driver_ok"), self._last_detect, sdr.get("present"))   # non-blocking
        obj = adsb.collect(cfg)
        if adsb.active_source() == "mayhem":
            st = mayhem.status() or {}
            self.state.update_device("portapack", connected=bool(st.get("connected")),
                                     device=st.get("device", ""), error=st.get("error", ""),
                                     aircraft=int(st.get("aircraft", 0) or 0),
                                     stalled_for=st.get("stalled_for"))
        # obj is not None means a decoder is serving (even with an empty sky).
        self.state.adsb_source_ok = obj is not None
        display = _aircraft_items(obj)
        raw_pos = [a for a in (obj.get("aircraft", []) if obj else [])
                   if isinstance(a, dict) and a.get("lat") is not None and a.get("lon") is not None]
        # Keep glitched/null-island fixes out of the accumulator (and queue.json)
        # entirely, so a bad fix cannot persist and break a later upload's span
        # check.
        raw_pos = adsb.filter_plausible_positions(raw_pos, home=adsb.receiver_home(cfg))
        snap = _snapshot_time(obj)
        self.state.update_aircraft(display, [_stamp(a, snap) for a in raw_pos])
        return len(display)

    def poll_mesh(self) -> int:
        """Live-capture from every configured mesh device over persistent connections
        (see :mod:`wdgwars_uploader.mesh_capture`) and stage what was heard. The local
        store is unbounded, so nodes the radio evicts at its firmware cap (Meshtastic
        80, MeshCore ~350) are kept anyway: every node whose advert or position
        crossed the antenna, including far multi-hop ones.
        """
        if self._mesh_stopping.is_set():
            return 0
        if not self.cfg.get("mesh_enabled", True):
            self.capture.stop()
            self._stage_mesh_samples()
            return 0
        cfg = self.cfg.as_dict()
        devices = []
        for dev in (cfg.get("mesh_devices") or []):
            d = dict(dev)
            if d.get("transport", "serial") == "serial":
                d["port"] = detect.pick_mesh_port(dev.get("port", "auto"), self._last_detect) \
                    or dev.get("port", "auto")
            devices.append(d)
        # Reconcile is idempotent: starts a persistent worker per enabled device,
        # stops workers for removed/disabled ones. The workers hold the
        # connection open and capture the live stream continuously.
        self.capture.reconcile(
            devices,
            hop_limit=int(cfg.get("mesh_hop_limit", 7) or 0),
            advert_minutes=int(cfg.get("mesh_advert_minutes", 60) or 0),
            transport=cfg.get("mesh_transport", "serial"),
            ble_address=cfg.get("mesh_ble_address", "auto"),
            credential_fingerprint=wardrift.credential_fingerprint(cfg.get("wardrift_api_key")),
        )
        self.capture.persist()          # save the growing store (throttled)
        self._stage_mesh_samples()
        # Drop garbage positions (corrupt Position packets land nodes mid-ocean)
        # before they skew the map or upload with a bad location.
        nodes = mesh.filter_outlier_positions(self.capture.snapshot())
        self.state.update_mesh(nodes)
        status = self.capture.status()
        any_conn = any(s.get("connected") for s in status)
        self.state.update_device("mesh", connected=any_conn,
                                 contacts=len(nodes), devices=status)
        return len(nodes)

    def _refresh_mesh_delivery(self, error: str = "") -> None:
        fingerprint = wardrift.credential_fingerprint(self.cfg.get("wardrift_api_key"))
        status = self.wardrift_mesh.status(fingerprint)
        receipts = self.wardrift_mesh.receipts(fingerprint)
        # Settings can change while the reporter is finishing an old-account
        # request. Never publish a snapshot selected with the former key.
        if fingerprint != wardrift.credential_fingerprint(self.cfg.get("wardrift_api_key")):
            return
        if error:
            status["error"] = error
        self.state.reconcile_mesh(status, receipts)

    def _stage_mesh_samples(self, *, raise_errors: bool = False) -> None:
        # A request may be waiting on HTTP. Leave observations in the capture
        # queue for the next poll instead of blocking live discovery behind it.
        if not self._wardrift_mesh_lock.acquire(blocking=False):
            return
        try:
            samples = self.capture.pending_samples()
            if samples:
                ids = self.wardrift_mesh.stage(samples)
                self.capture.acknowledge_samples(ids)
            self._refresh_mesh_delivery()
        except Exception as e:
            self._refresh_mesh_delivery(str(e))
            log.error("Could not preserve Wardrift mesh measurements: %s", e)
            if raise_errors:
                raise
        finally:
            self._wardrift_mesh_lock.release()

    def mesh_config_changed(self) -> None:
        """Stamp future observations with the newly selected credential only."""
        self.capture.set_credential_fingerprint(wardrift.credential_fingerprint(self.cfg.get("wardrift_api_key")))
        self._refresh_mesh_delivery()

    def stop_mesh(self) -> None:
        self._mesh_stopping.set()
        self.capture.stop()
        # Quitting does not wait out an upload in progress: the ledger guards its own
        # state, each request is saved before it is sent, and Wardrift answers a
        # resend with the same receipt.
        try:
            samples = self.capture.pending_samples()
            if samples:
                self.capture.acknowledge_samples(self.wardrift_mesh.stage(samples))
            self.wardrift_mesh.save()
        except Exception as e:
            log.error("Could not preserve Wardrift mesh measurements: %s", e)
            raise

    def refresh_stats(self) -> None:
        self.refresh_wardrift()
        self.refresh_wigle()
        client = self.client()
        if not self.cfg.get("api_key"):
            return
        stats = client.get_stats()
        if stats:
            self.state.stats = stats
            self.state.stats_ts = time.time()
            self.state.record_wdgwars_totals(stats)
        lb = client.get_leaderboard()
        if lb:
            self.state.leaderboard = lb
            self.state.leaderboard_ts = time.time()

    # -- WiGLE stats, boards and upload history ---------------------------------
    def _wigle_key(self) -> tuple[str, str]:
        return ((self.cfg.get("wigle_api_name") or "").strip(), (self.cfg.get("wigle_api_token") or "").strip())

    def refresh_wigle(self) -> None:
        name, token = self._wigle_key()
        if not (name and token):
            self.state.wigle = {}
            return
        me = wigle.user_stats(name, token)
        if me.get("ok"):
            me["race"] = wigle.race("discovered", me.get("rank", 0), name, token)
            me["race_month"] = wigle.race("monthcount", me.get("month_rank", 0), name, token)
            me["recent"] = wigle.transactions(name, token, max_rows=6).get("list", [])[:6]
            self.state.record_wigle_totals(me)
        if me.get("ok") or not self.state.wigle.get("ok"):
            self.state.wigle = {**me, "fetched": time.time()}

    def _wigle_cached(self, key: str, ttl: float, fetch) -> dict:
        hit = self._wigle_cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        data = fetch()
        if data.get("ok") or not hit:
            self._wigle_cache[key] = (time.time(), {**data, "fetched": time.time()})
        return self._wigle_cache[key][1]

    def wigle_board(self, kind: str, group_id: str = "") -> dict:
        """kind: alltime | month | teams | team (with ``group_id``). Boards are
        public; the key, when set, marks your own row."""
        name, token = self._wigle_key()
        me = (self.state.wigle or {}).get("name", "")
        if kind == "teams":
            return self._wigle_cached("teams", 600, lambda: wigle.groups(name, token))
        if kind == "team" and group_id:
            return self._wigle_cached(f"team:{group_id}", 600, lambda: wigle.group_members(group_id, me, name, token))
        sort = "monthcount" if kind == "month" else "discovered"
        return self._wigle_cached(f"board:{sort}", 600, lambda: wigle.standings(sort, 0, 100, name, token))

    def uploads(self, source: str) -> dict:
        """Upload history for the Uploads section: WiGLE and WDGWars from their own
        servers, Wardrift from the receipts kept here (it has no key-readable history)."""
        if source == "wigle":
            name, token = self._wigle_key()
            if not (name and token):
                return {"ok": False, "error": "Add your WiGLE API Name and Token in Settings.", "list": []}
            return self._wigle_cached("uploads", 30, lambda: wigle.transactions(name, token))
        if source == "wdgwars":
            if not (self.cfg.get("api_key") or "").strip():
                return {"ok": False, "error": "Add your WDGWars API key in Settings.", "list": []}
            return self._wigle_cached("wdg-uploads", 30, lambda: self.client().upload_history(50))
        return {"ok": True, "list": self.state.receipts("wardrift")}

    # -- the upload cycle --------------------------------------------------
    def run_cycle(self) -> CycleReport:
        rep = CycleReport()
        self.state.upload_in_progress = True
        try:
            rep = self._run_cycle_inner(rep)
        finally:
            self.state.upload_in_progress = False
            self.state.last_cycle = rep.as_dict()
            self._last_report = rep
        return rep

    # -- Wardrift account (optional sign-in) ---------------------------------
    def refresh_wardrift(self) -> None:
        """Fetch the signed-in player's stats and the all-time leaderboard."""
        token = self.cfg.get("wardrift_session") or ""
        if not token:
            self.state.wardrift.update(session="signed_out", account={}, leaderboard={}, error="")
            return
        acct = self.state.wardrift.get("account") or {}
        if self.state.wardrift.get("session") == "ok" and time.time() - acct.get("fetched", 0) < 290:
            return
        try:
            acct = wardrift.account(token)
            lb = wardrift.leaderboard(token)
            self.state.wardrift.update(session="ok", account=acct, leaderboard=lb, error="")
            self._wd_boards[("all_time", "global")] = lb
        except wardrift.SessionExpired:
            self.state.wardrift.update(session="expired", error="")
        except Exception as e:  # noqa: BLE001 - keep the last good stats on a network blip
            self.state.wardrift["error"] = str(e)
            log.debug("Wardrift stats refresh failed: %s", e)

    def wardrift_board(self, timeframe: str, scope: str = "global") -> dict:
        """One Wardrift leaderboard, cached for a minute."""
        token = self.cfg.get("wardrift_session") or ""
        if not token:
            return {"error": "signed_out"}
        cached = self._wd_boards.get((timeframe, scope))
        if cached and time.time() - cached.get("fetched", 0) < 60:
            return cached
        try:
            lb = wardrift.leaderboard(token, timeframe, scope)
        except wardrift.SessionExpired:
            self.state.wardrift.update(session="expired")
            return {"error": "expired"}
        except Exception as e:  # noqa: BLE001
            return cached or {"error": str(e)}
        self._wd_boards[(lb["timeframe"], lb["scope"])] = lb
        return lb

    def _upload_wardrive(self, cfg: dict, rep: CycleReport) -> None:
        """Wi-Fi and Bluetooth rows to every site that has a key."""
        label = cfg.get("upload_device_label", "")
        sends = {}
        name, token = (cfg.get("wigle_api_name") or "").strip(), (cfg.get("wigle_api_token") or "").strip()
        if name and token:
            sends["wigle"] = ("WiGLE", lambda gz, fn: wigle.upload(name, token, gz, fn))
        api_key = (cfg.get("api_key") or "").strip()
        if api_key:
            sends["wdgwars"] = ("WDGWars", self.client().upload_wardrive_csv)
        wd_key = (cfg.get("wardrift_api_key") or "").strip()
        if wd_key:
            wc = wardrift.WardriftClient(wd_key, label=label)
            sends["wardrift"] = ("Wardrift", wc.upload_csv)
        lines = []
        for target, (title, send) in sends.items():
            try:
                summary = self.wardriver.upload(target, label, send)
            except Exception as e:  # noqa: BLE001
                log.exception("wardrive upload to %s failed", title)
                summary = f"failed: {e}"
            lines.append(f"{title}: {summary}")
            if summary != "nothing new":
                self.state.event(f"Wardrive to {title}: {summary}")
                if target == "wardrift":
                    sent = summary.split(" ", 1)[0]
                    xp = re.search(r"\+([\d.]+) XP", summary)
                    inf = re.search(r"([\d.]+) influence", summary)
                    self.state.add_receipt("wardrift", "wifi", "failed" not in summary, summary,
                                           sent=int(sent) if sent.isdigit() else 0,
                                           xp=float(xp.group(1)) if xp else 0,
                                           influence=float(inf.group(1)) if inf else 0)
            if "failed" in summary:
                rep.errors.append(f"wardrive {title}: {summary}")
        wpk = (cfg.get("wpasec_key") or "").strip()
        if wpk:
            try:
                summary = self.wardriver.upload_wpasec(wpk)
            except Exception as e:  # noqa: BLE001
                summary = f"failed: {e}"
            lines.append(f"wpa-sec: {summary}")
            if summary != "nothing new":
                self.state.event(f"wpa-sec: {summary}")
        rep.wardrive = "; ".join(lines) or "no upload site set up"

    def _upload_wardrift(self, key: str, rep: CycleReport) -> None:
        """Send aircraft staged for Wardrift (independent of the WDGWars key)."""
        try:
            self.poll_aircraft()
            recs = self.state.acc_wardrift_raw()
            if not recs:
                rep.wardrift = "0 aircraft logged"
                return
            res = wardrift.WardriftClient(
                key, label=self.cfg.get("upload_device_label", "")).upload_aircraft(recs)
            if res.sent:
                self.state.mark_wardrift_uploaded(res.sent)
                self.state.add_wardrift_earned(res.credited, res.exp, res.influence, res.currency)
            rep.wardrift = res.summary()
            self.state.add_receipt("wardrift", "aircraft", not res.error, res.summary(), sent=res.sent,
                                   credited=res.credited, xp=res.exp, influence=res.influence, currency=res.currency)
            if res.error:
                rep.errors.append(f"wardrift: {res.error}")
                self.state.event(("Wardrift upload partial: " if res.sent
                                  else "Wardrift upload failed: ") + res.summary())
            else:
                self.state.event(f"Wardrift uploaded: {res.summary()}")
        except Exception as e:
            rep.wardrift = f"error: {e}"
            rep.errors.append(f"wardrift: {e}")
            log.exception("Wardrift upload failed")

    def _upload_wardrift_mesh(self, key: str, rep: CycleReport, *, poll: bool = True,
                              quiet: bool = False) -> None:
        """Send mesh nodes and the local radios' telemetry to Wardrift. Runs on
        its own, so a failure here never affects the WDGWars mesh upload.
        ``quiet`` is the periodic radio report: it logs to the activity feed only
        when it fails, and only once per distinct error."""
        if self._mesh_stopping.is_set() or not self._wardrift_mesh_lock.acquire(blocking=False):
            rep.wardrift_mesh = "mesh upload already running" if not self._mesh_stopping.is_set() else "stopping"
            return
        try:
            try:
                if poll:
                    self.poll_mesh()
                else:
                    self._stage_mesh_samples()
                nodes = mesh.filter_outlier_positions(self.capture.snapshot())
                res = wardrift.WardriftClient(key, timeout=wardrift.MESH_TIMEOUT).upload_mesh(
                    nodes, [], self.wardrift_mesh, report=quiet,
                    binding_valid=lambda: not self._mesh_stopping.is_set()
                    and self.cfg.get("mesh_enabled", True)
                    and (self.cfg.get("wardrift_api_key") or "").strip() == key)
                rep.wardrift_mesh = res.summary()
                # Every tick reconciles authoritative receipts, even when its
                # activity-feed entry is suppressed. The current key selects the
                # display account, so an old in-flight response cannot relabel it.
                self._refresh_mesh_delivery(res.error if (self.cfg.get("wardrift_api_key") or "").strip() == key else "")
                if res.error:
                    rep.errors.append(f"wardrift mesh: {res.error}")
                    msg = ("Wardrift mesh partial: " if res.nodes
                           else "Wardrift mesh upload failed: ") + res.summary()
                    if not quiet or msg != self._wardrift_report_error:
                        self.state.event(msg)
                    self._wardrift_report_error = msg
                else:
                    self._wardrift_report_error = ""
                    if res.nodes and not quiet:
                        self.state.event(f"Wardrift mesh uploaded: {res.summary()}")
                    elif res.nodes:
                        log.info("Wardrift radio report: %s", res.summary())
            except Exception as e:
                rep.wardrift_mesh = f"error: {e}"
                rep.errors.append(f"wardrift mesh: {e}")
                self._refresh_mesh_delivery(str(e))
                log.exception("Wardrift mesh upload failed")
        finally:
            self._wardrift_mesh_lock.release()

    def wardrift_mesh_tick(self) -> None:
        """Send a radio report between uploads. Wardrift counts uptime only from
        reports at most an hour apart, so the poller calls this every 15 minutes.
        It runs on its own thread so a slow request never stalls live polling."""
        key = (self.cfg.get("wardrift_api_key") or "").strip()
        if not key or not self.cfg.get("mesh_enabled", True) or self._mesh_stopping.is_set():
            return
        with self._mesh_schedule_lock:
            if self._mesh_upload_thread and self._mesh_upload_thread.is_alive():
                return
            self._mesh_upload_thread = threading.Thread(
                target=self._upload_wardrift_mesh, args=(key, CycleReport()),
                kwargs={"poll": False, "quiet": True}, name="wdgw-wardrift-mesh", daemon=True)
            self._mesh_upload_thread.start()

    def _run_cycle_inner(self, rep: CycleReport) -> CycleReport:
        cfg = self.cfg.as_dict()
        wd_key = (cfg.get("wardrift_api_key") or "").strip()
        if wd_key and cfg.get("adsb_enabled", True):
            self._upload_wardrift(wd_key, rep)
        if wd_key and cfg.get("mesh_enabled", True):
            self._upload_wardrift_mesh(wd_key, rep)
        self._upload_wardrive(cfg, rep)

        api_key = (cfg.get("api_key") or "").strip()
        if not api_key:
            rep.adsb = rep.mesh = "no API key"
            rep.errors.append("no API key configured")
            self.state.event("WDGWars upload skipped — no API key")
            return rep

        client = self.client()
        key_res = client.check_key()
        rep.key_ok = key_res.ok
        if not key_res.ok:
            rep.adsb = rep.mesh = "bad API key"
            rep.errors.append(f"API key rejected: {key_res.error}")
            self.state.event(f"API key rejected: {key_res.error}")
            return rep

        det = self.poll_devices()

        # --- ADS-B --------------------------------------------------------
        if cfg.get("adsb_enabled", True):
            try:
                # Capture the current view into the accumulator, then upload
                # EVERYTHING logged since the last upload (so aircraft that have
                # since flown out of range are still sent).
                self.poll_aircraft()
                # Drop outlier/bogus positions so one glitched fix can't blow the
                # batch's geographic span and get the whole upload server-rejected.
                raw = adsb.filter_plausible_positions(self.state.acc_aircraft_raw(),
                                                      home=adsb.receiver_home(cfg))
                n = len(raw)
                if n == 0:
                    if adsb.active_source() == "mayhem":
                        st = mayhem.status() or {}
                        rep.adsb = ("0 aircraft logged" if st.get("connected")
                                    else "PortaPack not reading" +
                                         (f": {st['error']}" if st.get("error") else ""))
                    else:
                        url = (cfg.get("adsb_json_url") or "").strip()
                        rep.adsb = ("no aircraft source reachable"
                                    if url and not adsb._url_serving(url) else "0 aircraft logged")
                else:
                    res = client.upload_aircraft_chunked(raw)
                    rep.adsb = f"{n} logged -> {res.summary()}"
                    # Mark every chunk the server accepted, even if a later one
                    # failed: only what did not land stays queued for next time.
                    sent = res.detail.get("sent_hexes") or []
                    if sent:
                        self.state.mark_aircraft_uploaded(sent)
                        rep.aircraft_sent = len(sent)
                        self.state.session_uploaded["aircraft"] += int(res.detail.get("aircraft_imported", 0) or 0)
                    if res.ok:
                        self.state.event(f"ADS-B uploaded: {res.summary()}")
                    else:
                        rep.errors.append(f"adsb: {res.error}")
                        self.state.event(f"ADS-B upload incomplete ({len(sent)}/{n} sent): {res.error}")
            except Exception as e:
                rep.adsb = f"error: {e}"
                rep.errors.append(f"adsb: {e}")
                log.exception("ADS-B cycle failed")

        # --- Mesh (MeshCore + Meshtastic) ---------------------------------
        if cfg.get("mesh_enabled", True):
            try:
                self.poll_mesh()   # reads every mesh device into the accumulator
                nodes = self.state.acc_mesh_nodes()
                if not nodes:
                    rep.mesh = "0 nodes logged"
                else:
                    payload = {"networks": [], "aircraft": [], "meshcore_nodes": nodes}
                    res = client.upload_json_hmac(payload)
                    d = res.detail
                    mi = int(d.get("meshcore_imported", 0) or 0)
                    ms = int(d.get("meshcore_already_seen", 0) or 0)
                    mr = int(d.get("meshcore_rejected", 0) or 0)
                    rep.mesh = (f"{len(nodes)} nodes -> imported {mi}, seen {ms}"
                                + (f", rejected {mr}" if mr else ""))
                    if res.ok:
                        rep.mesh_sent = len(nodes)
                        self.state.session_uploaded["mesh"] += mi
                        self.state.mark_mesh_uploaded([n.get("node_id") for n in nodes])
                        self.state.event(f"Mesh uploaded: {rep.mesh}")
                    else:
                        rep.errors.append(f"mesh: {res.error}")
                        self.state.event(f"Mesh upload failed: {res.error}")
            except mesh.MeshUnavailable as e:
                rep.mesh = f"unavailable: {e}"
                self.state.event(f"Mesh unavailable: {e}")
            except Exception as e:
                rep.mesh = f"error: {e}"
                rep.errors.append(f"mesh: {e}")
                log.exception("Mesh cycle failed")

        # Refresh WDGWars stats after an upload so the dashboard reflects it.
        try:
            self.refresh_stats()
        except Exception:
            pass
        log.info("cycle done: %s", rep.line())
        return rep
