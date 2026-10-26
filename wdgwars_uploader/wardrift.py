"""Wardrift (wardrift.net) aircraft and mesh uploads.

Aircraft are sent to ``POST /v1/ingest/signals`` as a gzipped dump1090
``aircraft.json`` file, authenticated with a character API key (``wdk_...``).
The upload is processed synchronously and answered with a receipt.

Wardrift dates a sighting as ``now - seen`` and credits each aircraft once per
UTC observation day, so every record carries the absolute time it was observed
(``_obs_ts`` and ``_pos_ts``, stamped when it was collected). The relative ages
are rebuilt against the upload's ``now``.

Mesh nodes go to ``POST /v1/ingest/mesh`` as JSON with the same key: every node
heard since the last accepted mesh upload, plus a ``telemetry`` block on the
node of each locally connected radio (battery, uptime, packet counters). The
first mesh upload of an install reaches back 72 hours; after that each node goes
again only when it has been heard since it was last delivered.
"""

from __future__ import annotations

import calendar
import gzip
import json
import logging
import os
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

from . import __version__
from .config import upload_file_name

BASE = "https://wardrift.net/v1"
UA = f"wdgwars-uploader/{__version__} (+https://github.com/RocketGod-git/wdgwars-uploader)"
CHUNK = 5000   # aircraft per file, well under the 10 MiB / 200,000-record limits
# The mesh endpoint sits behind a Cloudflare rule that refuses non-browser
# user agents (error 1010).
MESH_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
           f"Chrome/130.0.0.0 Safari/537.36 wdgwars-uploader/{__version__}")
MESH_CHUNK = 100   # nodes per batch; larger batches can outlast the request timeout
MESH_BACKFILL_SECS = 72 * 3600   # how far back the first mesh upload reaches
MESH_RETRIES = 2   # resends of one batch after a network error, 5xx or sequence conflict
LOG_BODY_MAX = 4000   # response characters kept in the log per mesh call

log = logging.getLogger("wdgwars.wardrift")


def _key_rejected(status: int) -> str:
    return f"key rejected (HTTP {status}), update the Wardrift key in Settings"


@dataclass
class WardriftResult:
    ok: bool = True
    sent: list[dict[str, Any]] = field(default_factory=list)   # records the server took
    credited: int = 0
    exp: int = 0
    influence: int = 0
    error: str = ""

    def summary(self) -> str:
        if not self.sent:
            return self.error or "nothing to send"
        s = f"{len(self.sent)} sent, {self.credited} new, +{self.exp} XP"
        return s + (f", then failed: {self.error}" if self.error else "")


def _record(a: dict[str, Any], now: float) -> dict[str, Any] | None:
    """Accumulated dump1090 record -> record for an upload made at ``now``.
    Records without a stamped observation time are skipped rather than dated
    to the upload."""
    obs = a.get("_obs_ts")
    if not isinstance(obs, (int, float)):
        return None
    out = {k: v for k, v in a.items() if not k.startswith("_")}
    out["seen"] = max(0.0, round(now - obs, 1))
    pos = a.get("_pos_ts")
    if isinstance(pos, (int, float)):
        out["seen_pos"] = max(0.0, round(now - pos, 1))
    else:
        out.pop("seen_pos", None)
    return out


def _json(resp: requests.Response) -> dict[str, Any]:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


# --- Mesh ---------------------------------------------------------------------
_MIN_TS = 1577836800.0   # 2020-01-01; anything earlier is a radio with no clock set
_FUTURE_SLACK = 600.0    # a heard time further ahead than this is a wrong clock
_COUNTERS = ("num_packets_tx", "num_packets_rx", "num_tx_relay",
             "num_tx_relay_canceled", "relay_tx_attempts", "relay_tx_failed")
_ID_LEN = {"meshcore": 16, "meshtastic": 8}


@dataclass
class MeshResult:
    ok: bool = True
    nodes: int = 0          # nodes the server accepted
    telemetry: int = 0      # local-radio samples the server accepted
    error: str = ""
    responses: list[tuple[int, str]] = field(default_factory=list)   # (status, body) per call

    def summary(self) -> str:
        if not self.nodes:
            return self.error or "nothing new"
        s = f"{self.nodes} nodes sent" + (", telemetry" if self.telemetry else "")
        return s + (f", then failed: {self.error}" if self.error else "")


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _heard_ts(n: dict[str, Any], now: float | None = None) -> float | None:
    """When a node was last heard, or None without a believable time. Nodes
    stored before ``heard_ts`` existed fall back to ``first_seen``."""
    if "heard_ts" in n:
        ts = n["heard_ts"]
    else:
        try:
            ts = calendar.timegm(time.strptime(str(n.get("first_seen")), "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            return None
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        return None
    if ts < _MIN_TS or ts > (now or time.time()) + _FUTURE_SLACK:
        return None
    return ts


def _wire_node(n: dict[str, Any], heard: float, now: float) -> dict[str, Any] | None:
    """Captured node -> mesh ingest node. ``lat``/``lon`` are left out without a
    fix."""
    net = n.get("network")
    nid = str(n.get("node_id") or "").lower()
    if len(nid) != _ID_LEN.get(net, 0) or any(c not in "0123456789abcdef" for c in nid):
        return None
    out: dict[str, Any] = {"network": net, "node_id": nid}
    pub = str(n.get("public_key") or "").lower()
    if net == "meshcore" and len(pub) == 64:
        out["public_key"] = pub
    out["node_type"] = n.get("node_type") or "COMPANION"
    out["name"] = str(n.get("name") or nid)[:64]
    if n.get("lat") is not None and n.get("lon") is not None:
        out["lat"], out["lon"] = round(float(n["lat"]), 6), round(float(n["lon"]), 6)
    out["last_heard"] = _iso(min(heard, now))
    return out


def _num(v: Any) -> Any:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


class MeshReporter:
    """Mesh upload state kept across restarts: this install's ``reporter_id``,
    the last ``sequence_no``, the backfill floor (``since``), the heard time last
    delivered for each node, and the counter epoch of each local radio."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.reporter_id = ""
        self.sequence_no = 0
        self.since = 0.0                      # nodes heard before this are never sent (set on first use)
        self.sent: dict[str, int] = {}        # node_id -> heard time delivered
        self.radios: dict[str, dict] = {}     # local node_id -> epoch state
        self.require_fix = False              # server refused nodes without lat/lon
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.reporter_id = str(data.get("reporter_id") or "")
            self.sequence_no = int(data.get("sequence_no") or 0)
            self.since = float(data.get("since") or 0)
            self.sent = {k: int(v) for k, v in (data.get("sent") or {}).items()}
            self.radios = {k: v for k, v in (data.get("radios") or {}).items() if isinstance(v, dict)}
            self.require_fix = bool(data.get("require_fix"))
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        if not self.reporter_id:
            self.reporter_id = str(uuid.uuid4())
            self.save()

    def save(self) -> None:
        data = json.dumps({"reporter_id": self.reporter_id, "sequence_no": self.sequence_no,
                           "since": self.since, "sent": self.sent, "radios": self.radios,
                           "require_fix": self.require_fix})
        try:
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(data)
                os.replace(tmp, self.path)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError as e:
            log.warning("could not save Wardrift mesh state: %s", e)

    def next_sequence(self) -> int:
        """Strictly increasing per reporter, seeded from the clock. Saved on use
        so a number is never sent twice for different batches."""
        self.sequence_no = max(int(time.time()), self.sequence_no + 1)
        self.save()
        return self.sequence_no

    def due(self, nodes: list[dict[str, Any]], now: float) -> list[dict[str, Any]]:
        """Nodes heard since the heard time last delivered for them (and since
        the backfill floor). Nodes without a believable heard time are skipped."""
        out = []
        for n in nodes:
            heard = _heard_ts(n, now)
            if heard is None or heard < self.since:
                continue
            key = int(heard)
            nid = str(n.get("node_id") or "").lower()
            if nid in self.sent and key <= self.sent[nid]:
                continue
            wire = _wire_node(n, heard, now)
            if wire is None or (self.require_fix and "lat" not in wire):
                continue
            out.append({"wire": wire, "key": key})
        return out

    def sample(self, radio: dict[str, Any], now: float,
               stored: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """The local radio's node with a ``telemetry`` block, or None if this
        reading was already delivered. ``stored`` is the same node as captured
        off the air, used for a position the radio itself does not report. The
        epoch state is kept only once the server accepts it (see :meth:`accept`)."""
        # Dated when the uptime was measured, so the uptime always matches its
        # timestamp; a later read of the same measurement is not sent again.
        obs = float(radio.get("uptime_at") or radio.get("observed_at") or 0)
        node = dict(radio.get("node") or {})
        if stored and (node.get("lat") is None or node.get("lon") is None):
            node["lat"], node["lon"] = stored.get("lat"), stored.get("lon")
        heard = max(obs, _heard_ts(stored, now) or 0) if stored else obs
        wire = _wire_node(node, heard, now)
        if wire is None or obs <= 0:
            return None
        nid = wire["node_id"]
        prev = self.radios.get(nid) or {}
        if obs <= float(prev.get("observed_at") or 0):
            return None
        up = _num(radio.get("uptime_seconds"))
        up = int(up) if up is not None else None
        counters = {k: _num((radio.get("counters") or {}).get(k)) for k in _COUNTERS}
        tx, rx = counters["num_packets_tx"], counters["num_packets_rx"]
        fw = radio.get("firmware_version") or None
        # A new counter epoch whenever the radio's counters may have restarted:
        # uptime or a packet counter went backwards, or the firmware changed.
        reset = not prev.get("epoch")
        for cur, last in ((up, prev.get("uptime")), (tx, prev.get("tx")), (rx, prev.get("rx"))):
            if cur is not None and last is not None and cur < last:
                reset = True
        if fw and prev.get("fw") and fw != prev["fw"]:
            reset = True
        epoch = str(uuid.uuid4()) if reset else prev["epoch"]
        seq = 1 if reset else int(prev.get("seq") or 0) + 1

        tel: dict[str, Any] = {
            "sample_id": str(uuid.uuid4()),
            "counter_epoch": epoch,
            "sample_sequence": seq,
            "observed_at": _iso(obs),
            "transport": radio.get("transport") or "usb_serial",
            "source": "local_radio",
            "firmware_version": fw,
            "capability_profile": radio.get("profile"),
            "uptime_seconds": up,
        }
        # A health or counters block nobody measured is left out.
        if radio.get("health_at"):
            level = _num(radio.get("battery_level"))
            volts = _num(radio.get("voltage"))
            tel["health"] = {
                "measured_at": _iso(float(radio["health_at"])),
                # Meshtastic reports 101 for external power; the field is a percentage.
                "battery_level": max(0, min(100, int(level))) if level is not None else None,
                "voltage": round(float(volts), 3) if volts is not None else None,
            }
        if radio.get("counters_at"):
            tel["counters"] = {"measured_at": _iso(float(radio["counters_at"])), **counters}
        wire["telemetry"] = tel
        state = {"epoch": epoch, "seq": seq, "observed_at": obs,
                 "uptime": up if up is not None else prev.get("uptime"),
                 "tx": tx if tx is not None else prev.get("tx"),
                 "rx": rx if rx is not None else prev.get("rx"),
                 "fw": fw or prev.get("fw")}
        return {"wire": wire, "key": int(heard), "radio": state}

    def items(self, nodes: list[dict[str, Any]], radios: list[dict[str, Any]],
              now: float) -> list[dict[str, Any]]:
        """Everything due, local radios first so their telemetry always rides
        in the first batch. The first call sets the backfill floor."""
        if not self.since:
            self.since = now - MESH_BACKFILL_SECS
            self.save()
        by_id = {str(n.get("node_id") or "").lower(): n for n in nodes}
        samples: dict[str, dict[str, Any]] = {}
        for r in sorted(radios, key=lambda r: float(r.get("observed_at") or 0), reverse=True):
            nid = str((r.get("node") or {}).get("node_id") or "").lower()
            if nid in samples:
                continue   # the same radio twice (replugged): the newest reading goes
            s = self.sample(r, now, by_id.get(nid))
            if s and not (self.require_fix and "lat" not in s["wire"]):
                samples[nid] = s
        return list(samples.values()) + [it for it in self.due(nodes, now)
                                         if it["wire"]["node_id"] not in samples]

    def batch_body(self, part: list[dict[str, Any]], now: float) -> dict[str, Any]:
        """One request body. Its batch_id and sequence_no are kept for every
        resend of this batch."""
        return {"now": _iso(now), "nodes": [it["wire"] for it in part],
                "batch_id": str(uuid.uuid4()), "reporter_id": self.reporter_id,
                "sequence_no": self.next_sequence()}

    def plan(self, nodes: list[dict[str, Any]], radios: list[dict[str, Any]],
             now: float) -> list[dict[str, Any]]:
        """Request bodies for everything due: the local radios' telemetry on its
        own first, since it is small and is what availability is measured from,
        then heard nodes in batches of at most MESH_CHUNK."""
        items = self.items(nodes, radios, now)
        own = [it for it in items if it.get("radio")]
        rest = [it for it in items if not it.get("radio")]
        parts = ([own] if own else []) + [rest[i:i + MESH_CHUNK]
                                          for i in range(0, len(rest), MESH_CHUNK)]
        return [{"items": part, "body": self.batch_body(part, now)} for part in parts]

    def accept(self, items: list[dict[str, Any]]) -> None:
        for it in items:
            nid = it["wire"]["node_id"]
            self.sent[nid] = max(it["key"], self.sent.get(nid, 0))
            if it.get("radio"):
                self.radios[nid] = it["radio"]
        self.save()


def _mesh_json(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def mesh_accepted(status: int, text: str) -> bool:
    """A 2xx answer, unless its body says ``"accepted": false``."""
    return 200 <= status < 300 and _mesh_json(text).get("accepted", True) is not False


def _log_body(text: str) -> str:
    if not text:
        return "(empty response body)"
    if len(text) > LOG_BODY_MAX:
        return f"{text[:LOG_BODY_MAX]}... ({len(text) - LOG_BODY_MAX} more characters)"
    return text


class WardriftClient:
    def __init__(self, api_key: str, timeout: float = 60.0, label: str = "") -> None:
        self.api_key = (api_key or "").strip()
        self.label = label
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"X-API-Key": self.api_key, "User-Agent": UA})

    def upload_aircraft(self, records: list[dict[str, Any]]) -> WardriftResult:
        """Upload accumulated aircraft in chunks. ``sent`` lists the records the
        server accepted, so a failure part-way through keeps only the rest."""
        res = WardriftResult()
        for i in range(0, len(records), CHUNK):
            part = records[i:i + CHUNK]
            now = time.time()
            body = [r for r in (_record(a, now) for a in part) if r]
            if body:
                data = gzip.compress(json.dumps({"now": now, "aircraft": body}).encode("utf-8"))
                receipt, err = self._post(data)
                if err:
                    res.ok, res.error = False, err
                    break
                res.credited += int(receipt.get("adsb_count") or 0)
                res.exp += int(receipt.get("adsb_exp") or 0)
                res.influence += int(receipt.get("awarded_influence") or 0)
            res.sent.extend(part)
        return res

    def _post(self, data: bytes) -> tuple[dict[str, Any], str]:
        headers = {"Content-Type": "application/gzip",
                   "X-File-Name": upload_file_name(self.label, "aircraft.json.gz")}
        for attempt in range(3):
            try:
                resp = self.session.post(f"{BASE}/ingest/signals", data=data,
                                         headers=headers, timeout=self.timeout)
            except requests.RequestException as e:
                return {}, str(e)
            body = _json(resp)
            if resp.status_code == 200:
                return body, ""
            if resp.status_code in (401, 403):
                return body, _key_rejected(resp.status_code)
            code = body.get("error") or f"HTTP {resp.status_code}"
            # Two uploads on one key at the same moment; the same file is retried.
            if resp.status_code == 409 and code == "sequence_conflict" and attempt < 2:
                time.sleep(1.0 + attempt)
                continue
            msg = body.get("message")
            return body, f"{code}: {msg}" if msg and msg != code else code
        return {}, "sequence_conflict"

    def upload_mesh(self, nodes: list[dict[str, Any]], radios: list[dict[str, Any]],
                    reporter: MeshReporter) -> MeshResult:
        """Send the nodes heard since the last accepted mesh upload, plus each
        local radio's telemetry, in batches. Only accepted batches move the
        per-node cursor, so whatever failed goes again next cycle."""
        res = MeshResult()
        for batch in reporter.plan(nodes, radios, time.time()):
            part, body = batch["items"], batch["body"]
            status, text = self.post_mesh(body)
            res.responses.append((status, text))
            if status in (400, 422) and not reporter.require_fix:
                # The format allows leaving lat/lon out; if the server disagrees,
                # resend the positioned nodes and leave the rest out from now on.
                fixed = [it for it in part if "lat" in it["wire"]]
                if fixed and len(fixed) < len(part):
                    log.info("Wardrift mesh: batch refused, retrying without unpositioned nodes")
                    st2, text2 = self.post_mesh(reporter.batch_body(fixed, time.time()))
                    res.responses.append((st2, text2))
                    if mesh_accepted(st2, text2):
                        reporter.require_fix = True
                        log.info("Wardrift mesh: nodes without a position are left out from now on")
                        part, status, text = fixed, st2, text2
            if not mesh_accepted(status, text):
                res.ok, res.error = False, _mesh_error(status, text)
                break
            reporter.accept(part)
            res.nodes += len(part)
            res.telemetry += sum(1 for it in part if it.get("radio"))
        return res

    def post_mesh(self, body: dict[str, Any]) -> tuple[int, str]:
        """POST one batch body. The same body (same batch_id and sequence_no) is
        resent after a network error, a 5xx or a sequence conflict. Every attempt
        is logged with its HTTP status and response body."""
        data = json.dumps(body)
        nodes = body.get("nodes") or []
        tag = (f"batch {str(body.get('batch_id'))[:8]} seq={body.get('sequence_no')} "
               f"({len(nodes)} nodes, {len(data)} B)")
        for n in nodes:
            if "telemetry" in n:
                log.info("Wardrift mesh telemetry: %s", json.dumps(n))
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "User-Agent": MESH_UA}
        status, text = 0, ""
        for attempt in range(1, MESH_RETRIES + 2):
            try:
                resp = self.session.post(f"{BASE}/ingest/mesh", data=data.encode("utf-8"),
                                         headers=headers, timeout=self.timeout)
                status, text = resp.status_code, resp.text
            except requests.RequestException as e:
                status, text = 0, f"network error: {type(e).__name__}: {e}"
            log.info("Wardrift mesh POST /v1/ingest/mesh %s attempt %d: %s", tag, attempt,
                     f"HTTP {status}: {_log_body(text)}" if status else text)
            conflict = status == 409 and _mesh_json(text).get("error") == "sequence_conflict"
            if not (status == 0 or status >= 500 or conflict) or attempt > MESH_RETRIES:
                break
            time.sleep(1.5 * attempt)
        return status, text


def _mesh_error(status: int, text: str) -> str:
    if not status:
        return text or "no response"
    if status in (401, 403):
        return _key_rejected(status)
    data = _mesh_json(text)
    if data.get("error") or data.get("message"):
        code, msg = data.get("error"), data.get("message")
        detail = f"{code}: {msg}" if code and msg and msg != code else (code or msg)
        return f"HTTP {status} {detail}"
    if 200 <= status < 300:
        return f"HTTP {status} batch not accepted"
    return f"HTTP {status}"
