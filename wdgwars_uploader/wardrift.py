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
import copy
import hashlib
import math
import threading
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
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
UA = f"wdgwars-uploader/{__version__} (+https://github.com/RocketGod-git/wardrive-uploader)"
CHUNK = 5000   # aircraft per file, well under the 10 MiB / 200,000-record limits
# The mesh endpoint sits behind a Cloudflare rule that refuses non-browser
# user agents (error 1010).
MESH_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
           f"Chrome/130.0.0.0 Safari/537.36 wdgwars-uploader/{__version__}")
MESH_CHUNK = 100   # nodes per batch; larger batches can outlast the request timeout
MESH_BACKFILL_SECS = 72 * 3600   # how far back the first mesh upload reaches
LOG_BODY_MAX = 4000   # response characters kept in the log per mesh call

log = logging.getLogger("wdgwars.wardrift")


def _key_rejected(status: int) -> str:
    return f"key rejected (HTTP {status}), update the Wardrift key in Settings"


@dataclass
class WardriftResult:
    ok: bool = True
    sent: list[dict[str, Any]] = field(default_factory=list)   # records the server took
    credited: int = 0
    currency: int = 0
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
_MIN_TS = 1577836800.0
_FUTURE_SLACK = 120.0
_UINT32 = 2 ** 32 - 1
_COUNTERS = ("num_packets_tx", "num_packets_rx", "num_packets_rx_bad", "num_rx_dupe",
             "num_tx_relay", "num_tx_relay_canceled", "num_tx_dropped", "relay_tx_attempts",
             "relay_tx_failed", "relay_unique_attempted", "relay_unique_completed",
             "num_online_nodes", "num_total_nodes", "flood_tx", "direct_tx", "flood_rx", "direct_rx")
_GAUGES = {"num_online_nodes", "num_total_nodes"}
_HEALTH_RANGES = {"battery_level": (0, 100), "voltage": (0, 100),
                  "channel_utilization": (0, 100), "air_util_tx": (0, 100),
                  "rssi_dbm": (-200, 30), "snr_db": (-100, 100), "noise_floor_dbm": (-200, 30),
                  "tx_airtime_seconds": (0, _UINT32), "rx_airtime_seconds": (0, _UINT32)}
_ID_LEN = {"meshcore": 16, "meshtastic": 8}
MAX_MESH_BYTES = 2_000_000
MAX_PENDING_SAMPLES = 10_000
MAX_STATE_BYTES = 64_000_000
MAX_QUEUE_BYTES = 32_000_000
MAX_RECEIPTS = 200      # receipts kept per account, newest first; the dashboard shows these
MAX_QUARANTINE = 100    # unusable measurements kept for diagnosis


@dataclass
class MeshResult:
    ok: bool = True
    nodes: int = 0
    telemetry: int = 0
    error: str = ""
    responses: list[tuple[int, str]] = field(default_factory=list)
    currency: int = 0
    influence: float = 0.0

    def summary(self) -> str:
        s = f"{self.nodes} nodes acknowledged, {self.telemetry} health samples" if self.nodes else "nothing new"
        if self.nodes:
            s += f", +{self.currency} currency, +{self.influence:g} influence"
        return (s + f", then failed: {self.error}") if self.error and self.nodes else self.error or s


class MeshStateError(RuntimeError):
    """The delivery ledger cannot safely be read or persisted."""


class MeshQueueFull(MeshStateError):
    """New work cannot fit; previously persisted requests can still be drained."""


def credential_fingerprint(key: str) -> str:
    key = (key or "").strip()
    return hashlib.sha256(key.encode("utf-8")).hexdigest() if key else ""


def _iso(ts: float) -> str:
    # Preserve query/event acquisition precision, including separate groups in a second.
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _num(v: Any, low: float = -math.inf, high: float = math.inf) -> int | float | None:
    try:
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and low <= v <= high else None
    except OverflowError:
        return None


def _uint(v: Any) -> int | None:
    value = _num(v, 0, _UINT32)
    return int(value) if value is not None and int(value) == value else None


def _heard_ts(n: dict[str, Any], now: float | None = None) -> float | None:
    if "heard_ts" in n:
        ts = n["heard_ts"]
    else:
        try:
            ts = calendar.timegm(time.strptime(str(n.get("first_seen")), "%Y-%m-%d %H:%M:%S"))
        except (ValueError, TypeError, OverflowError):
            return None
    try:
        ts = float(ts)
    except (TypeError, ValueError, OverflowError):
        return None
    return ts if _num(ts, _MIN_TS, (now if now is not None else time.time()) + _FUTURE_SLACK) is not None else None


def _wire_node(n: dict[str, Any], heard: float, now: float) -> dict[str, Any] | None:
    net = str(n.get("network") or "").lower()
    nid = str(n.get("node_id") or "").lower().removeprefix("!")
    if not nid or len(nid) != _ID_LEN.get(net, 0) or any(c not in "0123456789abcdef" for c in nid):
        return None
    out: dict[str, Any] = {"network": net, "node_id": nid}
    pub = str(n.get("public_key") or "").lower()
    if net == "meshcore":
        if len(pub) != 64 or any(c not in "0123456789abcdef" for c in pub) or not pub.startswith(nid):
            return None
        out["public_key"] = pub
    role = str(n.get("node_type") or "COMPANION").upper()
    out["node_type"] = role if role in {"COMPANION", "REPEATER", "ROOM_SERVER", "SENSOR"} else (
        "REPEATER" if role in {"ROUTER", "ROUTER_CLIENT", "ROUTER_LATE"} else "COMPANION")
    out["name"] = _text_limit("".join(c for c in str(n.get("name") or nid) if c.isprintable()), 80) or nid
    lat, lon = _num(n.get("lat"), -90, 90), _num(n.get("lon"), -180, 180)
    if lat is not None and lon is not None:
        out.update(lat=round(lat, 6), lon=round(lon, 6))
    snr, hops = _num(n.get("snr"), -100, 100), _uint(n.get("hops"))
    if snr is not None:
        out["snr"] = snr
    if hops is not None and hops <= 255:
        out["hops"] = hops
    out["last_heard"] = _iso(min(heard, now))
    return out


def _identity(node: dict[str, Any]) -> str:
    return node["network"] + ":" + node.get("public_key", node["node_id"])


def _uuid(value: Any) -> bool:
    try:
        return isinstance(value, str) and uuid.UUID(value).int != 0
    except (ValueError, TypeError, AttributeError):
        return False


def _encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _text_limit(value: str, units: int) -> str:
    # The C# contract measures UTF-16 code units, including surrogate pairs.
    return value.encode("utf-16-le", errors="replace")[:units * 2].decode("utf-16-le", errors="ignore")


def _capture_record(radio: dict[str, Any]) -> dict[str, Any]:
    """Keep measurement fields only, with nonfinite readings explicitly unknown."""
    def clean(value):
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items()}
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return _num(value)
        return value if value is None or isinstance(value, (str, bool)) else None

    fields = {"capture_id", "credential_fingerprint", "observed_at", "uptime_seconds", "uptime_at",
              "transport", "firmware_version", "profile", "capability_profile", "role", "hardware",
              "health_at", "counters_at", "host_received_at", "external_power", "queue_length", "error_flags"}
    fields.update(_HEALTH_RANGES)
    out = {k: clean(v) for k, v in radio.items() if k in fields}
    for group, keys in (("node", {"network", "node_id", "public_key", "node_type", "name", "lat", "lon", "snr", "hops"}),
                        ("health", set(_HEALTH_RANGES) | {"external_power", "queue_length", "error_flags"}),
                        ("counters", set(_COUNTERS))):
        raw = radio.get(group)
        if isinstance(raw, dict):
            out[group] = {k: clean(v) for k, v in raw.items() if k in keys}
    return out


def _sample(radio: dict[str, Any], sample_id: str, previous: dict[str, Any], now: float) -> dict[str, Any]:
    obs = _num(radio.get("observed_at"), _MIN_TS, now + _FUTURE_SLACK)
    if obs is None:
        raise ValueError("missing or invalid measurement acquisition time")
    wire = _wire_node(radio.get("node") or {}, obs, now)
    if wire is None:
        raise ValueError("invalid local radio identity (MeshCore requires its full public key)")
    net = wire["network"]
    profile = str(radio.get("profile") or radio.get("capability_profile") or "")
    if net == "meshcore":
        profile = "meshcore-companion-v1"
    elif profile not in {"meshtastic-localstats-v1", "meshtastic-devicemetrics-v1", "meshtastic-basic-v1"}:
        # A device-only event does not establish LocalStats capability.
        profile = "meshtastic-devicemetrics-v1"
    fw = _text_limit(str(radio.get("firmware_version") or "unknown").strip(), 120) or "unknown"
    if net == "meshtastic" and profile == "meshtastic-devicemetrics-v1" and previous.get("profile") == "meshtastic-localstats-v1" and previous.get("fw") == fw:
        # A restart/reconnect may deliver DeviceMetrics before LocalStats. Keep a
        # capability already established by this exact radio and firmware.
        profile = "meshtastic-localstats-v1"
    up = _uint(radio.get("uptime_seconds"))
    if up is not None:
        up_at = _num(radio.get("uptime_at", obs))
        if up_at is None or abs(up_at - obs) > 2:
            raise ValueError("uptime and sample were acquired more than two seconds apart")
    counters = {k: _uint((radio.get("counters") or {}).get(k)) for k in _COUNTERS}
    # Stock firmware cannot establish attempted/unique forwarding evidence.
    for k in ("relay_tx_attempts", "relay_tx_failed", "relay_unique_attempted", "relay_unique_completed"):
        counters[k] = None
    if net == "meshcore":
        for k in ("num_tx_relay", "num_tx_relay_canceled", "num_tx_dropped"):
            counters[k] = None
    for part, total in (("num_tx_relay", "num_packets_tx"), ("num_packets_rx_bad", "num_packets_rx"), ("num_rx_dupe", "num_packets_rx")):
        if counters[part] is not None and counters[total] is not None and counters[part] > counters[total]:
            counters[part] = None
    prev = previous
    reset = not prev.get("epoch") or prev.get("fw") != fw or prev.get("profile") != profile
    if up is not None and prev.get("uptime") is not None and up < prev["uptime"]:
        reset = True
    for k, v in counters.items():
        before = (prev.get("counters") or {}).get(k)
        if k not in _GAUGES and v is not None and before is not None and v < before:
            reset = True
    epoch = str(uuid.uuid4()) if reset else prev["epoch"]
    seq = int(prev.get("seq", 0)) + 1
    transport = radio.get("transport", "usb_serial")
    if transport == "serial":
        transport = "usb_serial"
    if transport not in {"usb_serial", "ble"}:
        raise ValueError("unsupported local radio transport")
    tel: dict[str, Any] = {"sample_id": sample_id, "counter_epoch": epoch, "sample_sequence": seq,
        "observed_at": _iso(obs), "transport": transport, "source": "local_radio", "firmware_version": fw,
        "capability_profile": profile, "uptime_seconds": up}
    received_at = _num(radio.get("host_received_at"), _MIN_TS, now + _FUTURE_SLACK)
    if received_at is not None:
        tel["host_received_at"] = _iso(received_at)
    if radio.get("role"):
        tel["role"] = _text_limit(str(radio["role"]), 40)
    if radio.get("hardware"):
        tel["hardware"] = _text_limit(str(radio["hardware"]), 120)
    for group in ("health", "counters"):
        measured = _num(radio.get(group + "_at"), _MIN_TS, obs + 2)
        if radio.get(group + "_at") is not None and (measured is None or measured < obs - 7 * 86400):
            raise ValueError(f"{group} acquisition time does not match sample")
        if measured is None:
            continue
        if group == "counters":
            tel[group] = {"measured_at": _iso(measured), **counters}
        else:
            raw = {**radio, **(radio.get("health") or {})}
            health = {k: _num(raw.get(k), *bounds) for k, bounds in _HEALTH_RANGES.items()}
            health.update({k: _uint(raw.get(k)) for k in ("queue_length", "error_flags")})
            health["external_power"] = raw.get("external_power") if isinstance(raw.get("external_power"), bool) else None
            level = _uint(raw.get("battery_level"))
            if net == "meshtastic" and level is not None and level > 100:
                health.update(battery_level=None, external_power=True)
            tel[group] = {"measured_at": _iso(measured), **health}
    wire["telemetry"] = tel
    old_counters = {} if reset else dict(prev.get("counters") or {})
    old_counters.update({k: v for k, v in counters.items() if v is not None})
    state = {"epoch": epoch, "seq": seq, "observed_at": obs, "uptime": up if up is not None else (None if reset else prev.get("uptime")),
             "counters": old_counters, "fw": fw, "profile": profile}
    return {"wire": wire, "key": int(obs), "identity": _identity(wire), "radio": state}


class MeshReporter:
    """Single durable, account-bound delivery ledger. No HTTP work occurs here.

    Legacy history remains explicitly unbound. Captures are persisted before handoff
    acknowledgement; exact request bytes and sequence allocation precede every send.
    """
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self.error = ""
        self._read_error = False
        self._data: dict[str, Any] = {}
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self._validate_state(data)
                self._data = data
            else:
                self._data = {"reporter_id": str(uuid.uuid4()), "sequence_no": 0}
            self._data.setdefault("version", 2)
            for k, default in (("accounts", {}), ("credentials", {}), ("credential_errors", {}), ("observations", []),
                               ("batches", []), ("quarantine", []), ("capture_ids", [])):
                self._data.setdefault(k, default)
            # Original fields stay available as unbound diagnostic history; require_fix
            # is intentionally never consulted for positionless telemetry.
            self._data.setdefault("since", 0.0)
            self._data.setdefault("sent", {})
            self._data.setdefault("radios", {})
            self.save()
        except (OSError, ValueError, TypeError, AttributeError, KeyError, OverflowError, MeshStateError) as e:
            self._read_error = True
            self.error = f"Mesh state unavailable; preserve {self.path.name} and repair/restore it: {e}"

    @staticmethod
    def _validate_state(data: Any) -> None:
        def integer(value: Any, minimum: int = 0, maximum: int = 2 ** 63 - 1) -> bool:
            return type(value) is int and minimum <= value <= maximum

        def fingerprint(value: Any, *, empty: bool = False) -> bool:
            return isinstance(value, str) and (empty and not value or
                len(value) == 64 and all(c in "0123456789abcdef" for c in value))

        def identity(value: Any) -> bool:
            if not isinstance(value, str) or ":" not in value:
                return False
            network, key = value.split(":", 1)
            length = {"meshtastic": 8, "meshcore": 64}.get(network)
            return len(key) == length and all(c in "0123456789abcdef" for c in key)

        def receipt_record(rid: str, record: Any, account: str) -> None:
            if not _uuid(rid) or not isinstance(record, dict) or record.get("receipt_id") != rid or record.get("account_id") != account:
                raise ValueError("invalid archived receipt binding")
            if _num(record.get("accepted_at"), _MIN_TS) is None or not integer(record.get("nodes"), 0, 500):
                raise ValueError("invalid archived receipt acknowledgment")
            receipt = record.get("receipt")
            if not isinstance(receipt, dict) or receipt.get("ok") is not True or receipt.get("receipt_id") != rid or not _uuid(receipt.get("batch_id")):
                raise ValueError("invalid archived receipt")
            if not _valid_receipt(receipt) or receipt["reports_accepted"] > record["nodes"]:
                raise ValueError("invalid archived receipt values")

        if not isinstance(data, dict) or not _uuid(data.get("reporter_id")):
            raise ValueError("missing or invalid reporter_id")
        if data.get("version", 2) != 2:
            raise ValueError("unsupported mesh state version")
        seq = data.get("sequence_no", 0)
        if not integer(seq):
            raise ValueError("invalid reporter sequence")
        for k in ("accounts", "credentials", "credential_errors", "sent", "radios"):
            if k in data and not isinstance(data[k], dict):
                raise ValueError(f"invalid {k}")
        for k in ("observations", "batches", "quarantine", "capture_ids"):
            if k in data and not isinstance(data[k], list):
                raise ValueError(f"invalid {k}")
        for account, state in data.get("accounts", {}).items():
            if not _uuid(account) or not isinstance(state, dict):
                raise ValueError("invalid account state")
            for k in ("sent", "radios", "receipts"):
                if not isinstance(state.get(k), dict):
                    raise ValueError(f"invalid account {k}")
            if _num(state.get("since"), 0) is None or not isinstance(state.get("error"), str):
                raise ValueError("invalid account delivery status")
            for subject, cursor in state["sent"].items():
                if not identity(subject) or not integer(cursor):
                    raise ValueError("invalid acknowledged discovery cursor")
            for subject, radio in state["radios"].items():
                if not identity(subject) or not isinstance(radio, dict) or not _uuid(radio.get("epoch")):
                    raise ValueError("invalid radio epoch identity")
                if not integer(radio.get("seq"), 1) or _num(radio.get("observed_at"), _MIN_TS) is None:
                    raise ValueError("invalid radio sample high-water")
                if radio.get("uptime") is not None and not integer(radio["uptime"], 0, _UINT32):
                    raise ValueError("invalid radio uptime high-water")
                if not isinstance(radio.get("fw"), str) or not 0 < len(radio["fw"]) <= 120 or not isinstance(radio.get("profile"), str) or not 0 < len(radio["profile"]) <= 80:
                    raise ValueError("invalid radio capability history")
                counters = radio.get("counters")
                if not isinstance(counters, dict) or any(k not in _COUNTERS or not integer(v, 0, _UINT32) for k, v in counters.items()):
                    raise ValueError("invalid radio counter high-water")
            for rid, record in state["receipts"].items():
                receipt_record(rid, record, account)
        for fp, account in data.get("credentials", {}).items():
            if not fingerprint(fp) or account not in data.get("accounts", {}):
                raise ValueError("invalid credential binding")
        for fp, error in data.get("credential_errors", {}).items():
            if not fingerprint(fp, empty=True) or not isinstance(error, str):
                raise ValueError("invalid credential error status")
        capture_ids = data.get("capture_ids", [])
        if any(not isinstance(value, str) or not value for value in capture_ids) or len(capture_ids) != len(set(capture_ids)):
            raise ValueError("invalid capture handoff history")
        sample_ids = set()
        for observation in data.get("observations", []) + data.get("quarantine", []):
            if not isinstance(observation, dict) or not _uuid(observation.get("sample_id")) or not isinstance(observation.get("radio"), dict):
                raise ValueError("invalid pending observation")
            if not isinstance(observation.get("capture_id"), str) or not observation["capture_id"] or not fingerprint(observation.get("fingerprint"), empty=True):
                raise ValueError("invalid pending capture binding")
            raw = observation["radio"]
            if raw.get("capture_id", observation["capture_id"]) != observation["capture_id"] or raw.get("credential_fingerprint", observation["fingerprint"]) != observation["fingerprint"]:
                raise ValueError("pending capture metadata differs from its binding")
            if observation["sample_id"] in sample_ids:
                raise ValueError("duplicate pending sample identity")
            sample_ids.add(observation["sample_id"])
            if observation.get("account_id") is not None and observation["account_id"] not in data.get("accounts", {}):
                raise ValueError("observation account is missing")
            if observation.get("account_id") is not None and data.get("credentials", {}).get(observation["fingerprint"]) != observation["account_id"]:
                raise ValueError("observation credential does not match its account")
            if "error" in observation and not isinstance(observation["error"], str):
                raise ValueError("invalid quarantine status")
        batches, sequences = set(), set()
        for batch in data.get("batches", []):
            body = json.loads(batch["payload"])
            if batch.get("payload_sha256") != hashlib.sha256(batch["payload"].encode("utf-8")).hexdigest():
                raise ValueError("pending request checksum does not match")
            if not _uuid(batch.get("account_id")) or not _uuid(body.get("batch_id")) or body.get("reporter_id") != data["reporter_id"]:
                raise ValueError("invalid pending batch binding")
            if body.get("sequence_no", 0) > seq or body.get("sequence_no", 0) <= 0:
                raise ValueError("pending batch exceeds sequence high-water")
            if not integer(body.get("sequence_no"), 1) or body["sequence_no"] in sequences or body["batch_id"] in batches:
                raise ValueError("duplicate pending batch or sequence")
            sequences.add(body["sequence_no"])
            batches.add(body["batch_id"])
            if not isinstance(body.get("nodes"), list) or len(body["nodes"]) > 500 or len(batch["payload"].encode("utf-8")) > MAX_MESH_BYTES:
                raise ValueError("invalid pending request size")
            if batch.get("sample_ids") != [n["telemetry"]["sample_id"] for n in body["nodes"] if "telemetry" in n]:
                raise ValueError("pending sample set differs from immutable request")
            if len(batch["sample_ids"]) > 100 or any(not _uuid(sid) or sid in sample_ids for sid in batch["sample_ids"]):
                raise ValueError("invalid or repeated pending sample identity")
            if len(batch["sample_ids"]) != len(set(batch["sample_ids"])):
                raise ValueError("duplicate pending sample identity")
            sample_ids.update(batch["sample_ids"])
            if batch.get("batch_id") != body["batch_id"] or batch.get("sequence_no") != body["sequence_no"] or batch.get("status") not in {"pending", "rejected"}:
                raise ValueError("pending batch metadata differs from immutable request")
            if batch["account_id"] not in data.get("accounts", {}) or len(batch["items"]) != len(body["nodes"]):
                raise ValueError("pending batch acknowledgment state is missing")
            for item, node in zip(batch["items"], body["nodes"]):
                if not isinstance(item, dict) or not identity(item.get("identity")) or item["identity"] != _identity(node) or not integer(item.get("key")):
                    raise ValueError("invalid pending acknowledgment cursor")
            if not integer(batch.get("attempts")) or _num(batch.get("retry_at"), 0) is None or not isinstance(batch.get("error"), str):
                raise ValueError("invalid pending retry status")
        # json.loads permits NaN/Infinity; no part of the ledger may contain them.
        if len(_encode(data).encode("utf-8")) > MAX_STATE_BYTES:
            raise ValueError("mesh ledger exceeds supported size")

    @property
    def reporter_id(self) -> str:
        return self._data.get("reporter_id", "")

    @property
    def sequence_no(self) -> int:
        return self._data.get("sequence_no", 0)

    @property
    def sent(self) -> dict:
        return self._data.get("sent", {})

    @property
    def radios(self) -> dict:
        return self._data.get("radios", {})

    @property
    def since(self) -> float:
        return self._data.get("since", 0)

    def _check(self) -> None:
        if self._read_error:
            raise MeshStateError(self.error)

    def save(self) -> None:
        with self._lock:
            self._check()
            tmp = None
            try:
                payload = _encode(self._data).encode("utf-8")
                if len(payload) > MAX_STATE_BYTES:
                    raise MeshStateError("mesh ledger full; preserve/export the ledger before recovery")
                fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
                with os.fdopen(fd, "wb") as f:
                    f.write(payload)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)
                tmp = None
                # Persist the rename on platforms that support directory fsync.
                if os.name != "nt":
                    fd = os.open(self.path.parent, os.O_RDONLY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                self.error = ""
            except (OSError, ValueError, MeshStateError) as e:
                self.error = f"Mesh state was not persisted; no untracked upload will be sent: {e}"
                raise MeshStateError(self.error) from e
            finally:
                if tmp:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass

    def _commit(self, updated: dict) -> None:
        previous = self._data
        self._data = updated
        try:
            self.save()
        except MeshStateError:
            self._data = previous
            raise

    @staticmethod
    def _account(data: dict, account_id: str) -> dict:
        return data["accounts"].setdefault(account_id, {"sent": {}, "radios": {}, "receipts": {}, "since": 0.0, "error": ""})

    def _capacity(self, data: dict) -> None:
        records = data["observations"] + data["batches"] + data["quarantine"]
        count = len(data["observations"]) + len(data["quarantine"]) + sum(len(b["sample_ids"]) or 1 for b in data["batches"])
        # Reserve envelope/normalization space while observations are still raw,
        # so a full capture queue can always turn its existing work into requests.
        size = len(_encode(records).encode("utf-8")) + 4096 * len(data["observations"])
        if count > MAX_PENDING_SAMPLES or size > MAX_QUEUE_BYTES:
            raise MeshQueueFull("Mesh delivery queue full; pending/rejected measurements retained. Restore delivery or export failures.")

    def stage(self, radios: list[dict[str, Any]], fingerprint: str = "") -> list[str]:
        with self._lock:
            self._check()
            updated = copy.deepcopy(self._data)
            known = set(updated["capture_ids"])
            accepted = []
            for radio in radios:
                radio = _capture_record(radio)
                capture_id = str(radio.get("capture_id") or hashlib.sha256(_encode(radio).encode("utf-8")).hexdigest())
                accepted.append(capture_id)
                if capture_id in known:
                    continue
                fp = str(radio.get("credential_fingerprint", fingerprint))
                updated["observations"].append({"capture_id": capture_id, "sample_id": str(uuid.uuid4()),
                    "fingerprint": fp, "account_id": updated["credentials"].get(fp) if fp else None,
                    "radio": copy.deepcopy(radio)})
                known.add(capture_id)
                updated["capture_ids"].append(capture_id)
            # Capture handoff IDs are a bounded recent cache, not reward deduplication.
            updated["capture_ids"] = updated["capture_ids"][-MAX_PENDING_SAMPLES * 2:]
            self._capacity(updated)
            if updated != self._data:
                self._commit(updated)
            elif self.error:
                self.save()
            return accepted

    def bind(self, fingerprint: str, account_id: str) -> None:
        if not fingerprint or not _uuid(account_id):
            raise MeshStateError("Mesh context did not identify an account")
        with self._lock:
            self._check()
            updated = copy.deepcopy(self._data)
            old = updated["credentials"].get(fingerprint)
            if old and old != account_id:
                raise MeshStateError("Credential account changed; existing measurements remain bound to their original account")
            updated["credentials"][fingerprint] = account_id
            updated["credential_errors"].pop(fingerprint, None)
            self._account(updated, account_id)
            for obs in updated["observations"]:
                if obs["fingerprint"] == fingerprint and obs["account_id"] is None:
                    obs["account_id"] = account_id
            self._commit(updated)

    def context_error(self, fingerprint: str, error: str) -> None:
        with self._lock:
            self._check()
            updated = copy.deepcopy(self._data)
            updated["credential_errors"][fingerprint] = error
            self._commit(updated)

    def prepare(self, nodes: list[dict[str, Any]], account_id: str, now: float) -> None:
        with self._lock:
            self._check()
            updated = copy.deepcopy(self._data)
            account = self._account(updated, account_id)
            if not account["since"]:
                account["since"] = now - MESH_BACKFILL_SECS
            updated["batches"] = [b for b in updated["batches"] if b["status"] != "rejected"]
            items = []
            remaining = []
            for obs in sorted(updated["observations"], key=lambda x: _num(x["radio"].get("observed_at")) or 0):
                if obs["account_id"] != account_id:
                    remaining.append(obs)
                    continue
                try:
                    node = _wire_node(obs["radio"].get("node") or {}, now, now)
                    identity = _identity(node) if node else ""
                    item = _sample(obs["radio"], obs["sample_id"], account["radios"].get(identity, {}), now)
                except (ValueError, TypeError, OverflowError) as e:
                    updated["quarantine"].append({**obs, "error": str(e)})
                    continue
                previous = account["radios"].get(identity, {})
                if item["radio"]["observed_at"] >= previous.get("observed_at", 0):
                    account["radios"][identity] = item["radio"]
                else:
                    # Late observations cannot replace the chronological baseline,
                    # but their sequence allocation must still be remembered.
                    account["radios"][identity] = {**previous, "seq": item["radio"]["seq"]}
                items.append(item)
            updated["observations"] = remaining
            updated["quarantine"] = updated["quarantine"][-MAX_QUARANTINE:]
            covered = dict(account["sent"])
            for batch in updated["batches"]:
                if batch["account_id"] == account_id:
                    for it in batch["items"]:
                        covered[it["identity"]] = max(covered.get(it["identity"], 0), it["key"])
            # Measured subjects need no additional discovery copy in this upload.
            for it in items:
                covered[it["identity"]] = max(covered.get(it["identity"], 0), it["key"])
            # Defer excess discovery to the next tick; it remains in MeshStore.
            # Never let new discovery prevent delivery of already staged health.
            room = MAX_PENDING_SAMPLES - len(updated["observations"]) - len(updated["quarantine"]) - len(items) \
                - sum(len(b["sample_ids"]) or 1 for b in updated["batches"])
            reserved = len(_encode(updated["observations"] + updated["batches"] + updated["quarantine"]).encode("utf-8")) \
                + 4096 * (len(updated["observations"]) + len(items))
            room = min(room, max(0, (MAX_QUEUE_BYTES - reserved) // 4096))
            for node in nodes:
                if room <= 0:
                    break
                heard = _heard_ts(node, now)
                if heard is None or heard < account["since"]:
                    continue
                wire = _wire_node(node, heard, now)
                if wire is None:
                    continue
                identity = _identity(wire)
                if int(heard) <= covered.get(identity, 0):
                    continue
                items.append({"wire": wire, "identity": identity, "key": int(heard)})
                covered[identity] = int(heard)
                room -= 1
            # Allocate metadata once; enforce encoded bytes including the envelope.
            while items:
                updated["sequence_no"] = max(int(now), updated["sequence_no"] + 1)
                body = {"now": _iso(now), "nodes": [], "batch_id": str(uuid.uuid4()),
                        "reporter_id": self.reporter_id, "sequence_no": updated["sequence_no"]}
                part, samples = [], []
                while items and len(part) < MESH_CHUNK:
                    candidate = items[0]
                    tel = candidate["wire"].get("telemetry")
                    if part and bool(tel) != bool(part[0]["wire"].get("telemetry")):
                        break
                    if tel and len(samples) >= 100:
                        break
                    body["nodes"].append(candidate["wire"])
                    if len(_encode(body).encode("utf-8")) > MAX_MESH_BYTES:
                        body["nodes"].pop()
                        if not part:
                            raise MeshStateError("A mesh measurement exceeds the request size limit")
                        break
                    part.append(items.pop(0))
                    if tel:
                        samples.append(tel["sample_id"])
                payload = _encode(body)
                updated["batches"].append({"account_id": account_id, "payload": payload,
                    "payload_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                    "batch_id": body["batch_id"], "sequence_no": body["sequence_no"], "sample_ids": samples,
                    "items": [{"identity": it["identity"], "key": it["key"]} for it in part],
                    "status": "pending", "attempts": 0, "retry_at": 0, "error": ""})
            self._capacity(updated)
            self._commit(updated)

    def pending(self, account_id: str) -> list[dict]:
        with self._lock:
            self._check()
            return copy.deepcopy(sorted((b for b in self._data["batches"] if b["account_id"] == account_id and b["status"] == "pending"),
                                        key=lambda b: b["sequence_no"]))

    def failure(self, batch_id: str, error: str, *, rejected: bool = False, retry_at: float = 0) -> None:
        """A rejected request is dropped. Its nodes were never acknowledged, so the
        next upload sends them again; its health samples are not resent."""
        with self._lock:
            updated = copy.deepcopy(self._data)
            batch = next(b for b in updated["batches"] if b["batch_id"] == batch_id)
            account = self._account(updated, batch["account_id"])
            account["error"] = error
            if rejected:
                updated["batches"] = [b for b in updated["batches"] if b["batch_id"] != batch_id]
                if batch["sample_ids"]:
                    account["telemetry_error"] = error
                log.warning("Wardrift rejected mesh batch %s (%d nodes, %d health samples): %s",
                            batch_id, len(batch["items"]), len(batch["sample_ids"]), error)
            else:
                batch.update(error=error, retry_at=retry_at, attempts=batch["attempts"] + 1)
            self._commit(updated)

    def acknowledge(self, batch: dict, receipt: dict) -> dict:
        with self._lock:
            updated = copy.deepcopy(self._data)
            account = self._account(updated, batch["account_id"])
            if receipt["receipt_id"] in account["receipts"]:
                raise MeshStateError("Receipt ID was already acknowledged; conflicting response retained for review")
            record = {"receipt_id": receipt["receipt_id"], "account_id": batch["account_id"],
                      "accepted_at": time.time(), "nodes": len(batch["items"]), "receipt": receipt}
            account["receipts"][receipt["receipt_id"]] = record
            for it in batch["items"]:
                account["sent"][it["identity"]] = max(account["sent"].get(it["identity"], 0), it["key"])
            account["error"] = ""
            if batch["sample_ids"]:
                account.pop("telemetry_error", None)
            if len(account["receipts"]) > MAX_RECEIPTS:
                keep = sorted(account["receipts"].values(), key=lambda r: r["accepted_at"])[-MAX_RECEIPTS:]
                account["receipts"] = {r["receipt_id"]: r for r in keep}
            updated["batches"] = [b for b in updated["batches"] if b["batch_id"] != batch["batch_id"]]
            self._commit(updated)
            return copy.deepcopy(record)

    def receipts(self, fingerprint: str = "") -> list[dict]:
        with self._lock:
            account = self._data.get("accounts", {}).get(self._data.get("credentials", {}).get(fingerprint), {})
            return copy.deepcopy(sorted(account.get("receipts", {}).values(), key=lambda r: r["accepted_at"]))

    def status(self, fingerprint: str = "") -> dict:
        with self._lock:
            account_id = self._data.get("credentials", {}).get(fingerprint, "")
            batches = [b for b in self._data.get("batches", []) if account_id and b["account_id"] == account_id]
            observations = [o for o in self._data.get("observations", []) if o["fingerprint"] == fingerprint or account_id and o["account_id"] == account_id]
            quarantine = [o for o in self._data.get("quarantine", []) if o["fingerprint"] == fingerprint or account_id and o["account_id"] == account_id]
            receipts = self.receipts(fingerprint)
            account = self._data.get("accounts", {}).get(account_id, {})
            retained = len(self._data.get("batches", [])) - len(batches) \
                + len(self._data.get("observations", [])) - len(observations) \
                + len(self._data.get("quarantine", [])) - len(quarantine)
            return {"account_id": account_id, "pending": sum(b["status"] == "pending" for b in batches) + len(observations),
                    "pending_samples": len(observations) + sum(len(b["sample_ids"]) for b in batches if b["status"] == "pending"),
                    "rejected": sum(b["status"] == "rejected" for b in batches) + len(quarantine),
                    "unverified": sum(o["account_id"] is None for o in observations),
                    "retained": retained,
                    "last_acknowledged": receipts[-1]["accepted_at"] if receipts else None,
                    "error": self.error or self._data.get("credential_errors", {}).get(fingerprint) or account.get("error")
                    or account.get("telemetry_error")
                    or next((b["error"] for b in reversed(batches) if b["status"] == "rejected"), "")
                    or (quarantine[-1]["error"] if quarantine else "")}


def _mesh_json(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _valid_receipt(data: Any) -> bool:
    """Shared validation for server responses and restored receipt history."""
    if not isinstance(data, dict) or data.get("ok") is not True or not _uuid(data.get("receipt_id")) or not _uuid(data.get("batch_id")):
        return False
    samples, count = data.get("samples"), data.get("reports_accepted")
    if type(count) is not int or not 0 <= count <= 100 or not isinstance(samples, list) or len(samples) != count:
        return False
    if any(not isinstance(s, dict) or not _uuid(s.get("sample_id")) for s in samples):
        return False
    if len({s["sample_id"] for s in samples}) != count:
        return False
    for rec in [data, *samples]:
        for key in ("currency", "influence", "availability_seconds", "availability_currency", "availability_influence", "contribution_units"):
            if key in rec and _num(rec[key], 0) is None:
                return False
        for key in ("currency", "availability_currency", "forwarded", "suppressed", "attempted", "failed"):
            value = rec.get(key)
            if value is not None and (type(value) is not int or not 0 <= value <= 2 ** 63 - 1):
                return False
        for key in ("policy_version", "outcome", "evidence", "observed_at"):
            if key in rec and not isinstance(rec[key], str):
                return False
    return True


def mesh_accepted(status: int, text: str, batch: dict | None = None) -> bool:
    """Validate the exact receipt, never acknowledge an arbitrary HTTP success."""
    data = _mesh_json(text)
    if not 200 <= status < 300 or not _valid_receipt(data) or batch is None:
        return False
    body = json.loads(batch["payload"]) if "payload" in batch else batch
    expected = [n["telemetry"]["sample_id"] for n in body["nodes"] if "telemetry" in n]
    return data["batch_id"] == body["batch_id"] and data["reports_accepted"] == len(expected) \
        and {s["sample_id"] for s in data["samples"]} == set(expected)


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
                res.currency += int(receipt.get("adsb_currency") or 0)
            res.sent.extend(part)
        return res

    def upload_csv(self, gz: bytes, name: str) -> tuple[bool, str]:
        """A gzipped WigleWifi CSV of Wi-Fi and Bluetooth rows."""
        receipt, err = self._post(gz, name)
        if err:
            return False, err
        bits = []
        for k, label in (("accepted", "accepted"), ("duplicate", "duplicate"), ("awarded_exp", "XP"),
                         ("awarded_influence", "influence")):
            if receipt.get(k) and not isinstance(receipt[k], bool):   # "accepted" can be a plain true
                bits.append(f"{receipt[k]} {label}" if label != "XP" else f"+{receipt[k]} XP")
        toys = receipt.get("toys") or {}
        if isinstance(toys, dict) and toys.get("new_count"):
            bits.append(f"+{toys['new_count']} toys")
        return True, ", ".join(bits) or "accepted"

    def _post(self, data: bytes, name: str = "") -> tuple[dict[str, Any], str]:
        headers = {"Content-Type": "application/gzip",
                   "X-File-Name": name or upload_file_name(self.label, "aircraft.json.gz")}
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

    def mesh_context(self) -> tuple[str, str]:
        """Verify upload authority using the same key and endpoint as ingestion."""
        try:
            resp = self.session.get(f"{BASE}/ingest/mesh", headers={"Accept": "application/json", "User-Agent": MESH_UA},
                                    timeout=self.timeout, allow_redirects=False)
        except requests.RequestException as e:
            return "", f"Mesh account verification failed: {type(e).__name__}"
        data = _json(resp)
        if resp.status_code != 200 or not _uuid(data.get("account_id")):
            return "", _mesh_error(resp.status_code, resp.text) if resp.status_code != 200 else "Mesh account context is invalid"
        return str(uuid.UUID(data["account_id"])), ""

    def upload_mesh(self, nodes: list[dict[str, Any]], radios: list[dict[str, Any]],
                    reporter: MeshReporter, *, binding_valid=None) -> MeshResult:
        """Stage, verify account, then send durable requests in reporter order.

        Each client freezes one credential. An in-flight receipt always belongs to
        that account, even if Settings changes; binding_valid prevents further sends.
        """
        res = MeshResult()
        fp = credential_fingerprint(self.api_key)
        try:
            deferred_capture = False
            try:
                reporter.stage(radios, fp)
            except MeshQueueFull:
                # Existing durable requests can free capacity. The caller retains
                # unacknowledged capture IDs until a later successful stage.
                deferred_capture = True
            if not fp:
                res.ok, res.error = False, "Set the shared Wardrift API key to deliver mesh measurements"
                return res
            if binding_valid is not None and not binding_valid():
                res.ok, res.error = False, "Wardrift key changed; measurements remain queued for their original account"
                return res
            account_id, error = self.mesh_context()
            if error:
                error = error.replace(self.api_key, "[redacted]")
                reporter.context_error(fp, error)
                res.ok, res.error = False, error
                return res
            reporter.bind(fp, account_id)
            # Drain old immutable requests before allocating new requests. Then
            # drain staged observations, freeing room for any deferred handoff.
            for phase in range(3 if deferred_capture else 2):
                if phase:
                    if binding_valid is not None and not binding_valid():
                        res.ok, res.error = False, "Wardrift key changed; measurements remain queued"
                        return res
                    if phase == 2:
                        reporter.stage(radios, fp)
                    reporter.prepare(nodes, account_id, time.time())
                for batch in reporter.pending(account_id):
                    if binding_valid is not None and not binding_valid():
                        res.ok, res.error = False, "Wardrift key changed; remaining batches are still queued"
                        return res
                    if batch.get("retry_at", 0) > time.time():
                        res.ok, res.error = False, batch.get("error") or "Mesh upload retry is pending"
                        return res
                    status, response = self.post_mesh(batch["payload"])
                    res.responses.append((status, response.replace(self.api_key, "[redacted]")[:LOG_BODY_MAX]))
                    if not mesh_accepted(status, response, batch):
                        error = _mesh_error(status, response)
                        if self.api_key:
                            error = error.replace(self.api_key, "[redacted]")
                        # Two uploads on one key at once is the only 409 worth resending.
                        definitive = (status in (400, 409, 413, 422)
                                      and _mesh_json(response).get("error") != "sequence_conflict")
                        delay = max(min(300, 2 ** min(batch["attempts"] + 1, 8)), getattr(self, "_mesh_retry_after", 0))
                        reporter.failure(batch["batch_id"], error, rejected=definitive, retry_at=time.time() + delay)
                        res.ok, res.error = False, error
                        if definitive:
                            continue  # retained for review; later valid reporter sequences can proceed
                        return res
                    receipt = _mesh_json(response)
                    reporter.acknowledge(batch, receipt)
                    res.nodes += len(batch["items"])
                    res.telemetry += len(batch["sample_ids"])
                    res.currency += receipt.get("currency", 0)
                    res.influence += receipt.get("influence", 0.0)
        except (MeshStateError, OSError, ValueError, TypeError) as e:
            res.ok, res.error = False, str(e)
            reporter.error = str(e)
        return res

    def post_mesh(self, body: dict[str, Any] | str) -> tuple[int, str]:
        """One immutable request attempt; the ledger owns retry timing and bytes."""
        data = body if isinstance(body, str) else _encode(body)
        self._mesh_retry_after = 0.0
        headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": MESH_UA}
        try:
            resp = self.session.post(f"{BASE}/ingest/mesh", data=data.encode("utf-8"),
                                     headers=headers, timeout=self.timeout, allow_redirects=False)
        except requests.RequestException as e:
            return 0, f"Mesh network error: {type(e).__name__}"
        retry = resp.headers.get("Retry-After", "")
        if retry:
            try:
                delay = float(retry)
            except (TypeError, ValueError):
                try:
                    delay = parsedate_to_datetime(retry).timestamp() - time.time()
                except (ValueError, TypeError, OverflowError):
                    delay = 0
            if math.isfinite(delay):
                self._mesh_retry_after = max(0, min(86400, delay))
        log.info("Wardrift mesh POST: HTTP %s (%s request bytes)", resp.status_code, len(data.encode("utf-8")))
        return resp.status_code, resp.text


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


# ---------------------------------------------------------------------------
# Account: optional sign-in for player stats and leaderboards. Those routes take
# a signed-in session, not the upload key. Only the session token is kept; the
# password is sent once at sign-in.
# ---------------------------------------------------------------------------
TIMEFRAMES = ("daily", "7d", "month", "season", "all_time")
SCOPES = ("global", "faction", "guild")
_LB_FIELDS = ("rank", "character_id", "name", "faction_name", "faction_color", "guild_name",
              "level", "class_name", "influence", "influence_share", "routes", "threats",
              "trend", "wifi_count", "bluetooth_count", "adsb_count", "mesh_count", "toy_count",
              "towers_captured", "keeps_captured")


class SessionExpired(Exception):
    """Wardrift no longer accepts the saved session token."""


def _account_session(token: str = "") -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json"})
    if token:
        s.headers["Authorization"] = f"Bearer {token}"
    return s


def sign_in(username: str, password: str, timeout: float = 20.0) -> dict[str, Any]:
    """Exchange a username and password for a remembered session token."""
    resp = _account_session().post(f"{BASE}/auth/sign-in/username", timeout=timeout,
                                   json={"username": username, "password": password,
                                         "remember_me": True})
    data = _json(resp)
    if resp.status_code in (400, 401, 403, 422):
        raise ValueError("Wardrift did not accept that username and password")
    if resp.status_code != 200:
        raise ValueError(data.get("message") or f"Wardrift sign-in failed (HTTP {resp.status_code})")
    sess = data.get("session") if isinstance(data.get("session"), dict) else {}
    user = data.get("user") if isinstance(data.get("user"), dict) else {}
    token = sess.get("token") or data.get("token")
    if not token:
        raise ValueError("Wardrift did not return a session")
    return {"token": token,
            "expires_at": sess.get("expires_at") or sess.get("expiresAt") or "",
            "username": user.get("username") or user.get("name") or username}


def sign_out(token: str) -> None:
    try:
        _account_session(token).post(f"{BASE}/auth/sign-out", timeout=10)
    except requests.RequestException:
        pass


def _get(token: str, path: str, timeout: float = 20.0) -> dict[str, Any]:
    resp = _account_session(token).get(f"{BASE}/{path}", timeout=timeout)
    if resp.status_code in (401, 403):
        raise SessionExpired()
    resp.raise_for_status()
    return _json(resp)


def _lb_row(r: Any) -> dict[str, Any] | None:
    return {k: r.get(k) for k in _LB_FIELDS} if isinstance(r, dict) else None


def leaderboard(token: str, timeframe: str = "all_time", scope: str = "global") -> dict[str, Any]:
    if timeframe not in TIMEFRAMES:
        timeframe = "all_time"
    if scope not in SCOPES:
        scope = "global"
    d = _get(token, f"leaderboard?scope={scope}&timeframe={timeframe}&includeStandings=true")
    return {"timeframe": timeframe, "scope": scope,
            "standings": [r for r in (_lb_row(x) for x in d.get("standings") or []) if r],
            "me": _lb_row(d.get("my_rank")), "fetched": time.time()}


def account(token: str) -> dict[str, Any]:
    """The signed-in player's character, wardrive totals, mesh summary and titles."""
    d = _get(token, "dashboard")
    chars = d.get("characters") or [{}]
    ch = chars[0] if isinstance(chars[0], dict) else {}
    wd = d.get("wardrive") or {}
    m = d.get("mesh") or {}
    tl = d.get("titles") or {}
    tl = tl.get("titles") if isinstance(tl, dict) else tl
    tl = [t for t in tl or [] if isinstance(t, dict)]
    unlocked = [str(t.get("label")) for t in tl if t.get("unlocked") and t.get("label")]
    return {
        "character": {k: ch.get(k) for k in ("name", "level", "exp", "exp_into_level", "exp_for_level",
                                             "exp_to_next_level", "currency", "class_name", "title")},
        "wardrive": {"lifetime": wd.get("lifetime") or {}, "this_week": wd.get("this_week") or {},
                     "streak_days": wd.get("streak_days")},
        "mesh": {"uplink_online": m.get("uplink_online"),
                 "reporting_node_count": m.get("reporting_node_count"),
                 "owned_node_count": m.get("owned_node_count"),
                 "metrics": [{k: x.get(k) for k in ("key", "label", "value", "delta", "caption", "series")}
                             for x in m.get("metrics") or [] if isinstance(x, dict)]},
        "titles": {"unlocked": len(unlocked), "total": len(tl), "labels": unlocked[-12:]},
        "fetched": time.time(),
    }
