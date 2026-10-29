"""Persistent live-stream capture for mesh radios.

Snapshot-polling a device's node/contact list is limited by the firmware's hard
cap (Meshtastic nRF52 = 80, MeshCore ≈ 350) and its LRU eviction: once the table
is full the radio drops or rolls off nodes, and a 60-second snapshot never sees
what churned through in between. Instead each device holds ONE persistent
connection subscribed to its **live packet stream**, and every node heard is
copied into a local unbounded store the instant its packet crosses the antenna.
Node 81 / 351 and far multi-hop nodes are logged regardless of what the firmware
chooses to keep.

* **Meshtastic** - ``pub.subscribe('meshtastic.receive')``; every POSITION /
  NODEINFO / TELEMETRY packet updates the node, captured from the library's
  merged record before it can be evicted.
* **MeshCore** - ``subscribe(EventType.ADVERTISEMENT / NEW_CONTACT)`` plus
  draining ``pending_contacts`` (adverts the firmware refused to store after
  ``CONTACTS_FULL``). Runs over serial or BLE; BLE dodges the ESP32-S3
  native-USB reset wedge entirely.

The store feeds the same :func:`state.update_mesh` path as a snapshot read, so
the feed, accumulator, upload and sent-history dedup all work unchanged.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import math
import threading
import time
import uuid
from typing import Any, Callable

from . import mesh

log = logging.getLogger("wdgwars.capture")


# Opening a serial port normally asserts DTR/RTS, which on an ESP32-S3 native-USB
# CDC (e.g. the LilyGo T-Deck MeshCore companion) pulses the auto-reset line and
# reboots the board mid-handshake. Those ports are opened with both lines held
# low. This is SCOPED to registered ports only: nRF52 boards (Meshtastic T114) do
# the opposite - they gate their CDC on DTR and need it asserted - so a global
# patch would break them.
_NO_RESET_PORTS: set[str] = set()

# How long a Meshtastic connect may take in total, and how long to wait for the
# radio to start answering a config request before sending another.
_NODEDB_WAIT = 180.0
_REQUEST_RETRY_SECS = 4.0

# Seconds between reads of a MeshCore companion's own battery and counters.
_RADIO_SAMPLE_SECS = 300
_QUERY_TIMEOUT = 10.0
_SAMPLE_CAP = 10000
_SAMPLE_BYTE_CAP = 20_000_000
# A radio without GPS or a phone keeps a clock that can be hours off. Its own
# timestamp is used only when it agrees with the PC within this window.
_CLOCK_BEHIND_SECS = 900
_CLOCK_AHEAD_SECS = 120

# Ports whose DTR/RTS lines reset the attached board. These are opened with both
# lines held low:
#  * ESP32-S3/S2 native-USB CDC;
#  * USB-UART bridges wired to an ESP32's EN/IO0 auto-reset transistors - Station
#    G1/G2 (CH9102), Heltec V2/V3 and T-Beam (CP210x), generic CH340/CH341.
#    Windows raises the two lines one after the other on open, which pulses EN
#    and reboots the radio. The Meshtastic library only guards against this on
#    Linux/macOS (HUPCL); on Windows its one-shot config request then lands
#    mid-boot and the connect waits out its whole timer.
# NOT nRF52840 native USB (Heltec T114, RAK): those GATE their CDC on DTR, and
# holding it low stops them talking at all.
_NO_RESET_IDS = {
    (0x303A, 0x1001), (0x303A, 0x4001),   # ESP32-S3 / S2 native USB
    (0x1A86, 0x55D4),                     # CH9102
    (0x10C4, 0xEA60), (0x10C4, 0xEA70),   # CP210x
    (0x1A86, 0x7523), (0x1A86, 0x5523),   # CH340 / CH341
}


def _needs_no_reset(port: str) -> bool:
    """True for a port whose control lines reset the attached board."""
    if not port:
        return False
    try:
        from serial.tools import list_ports
        for p in list_ports.comports():
            if p.device == port:
                return (getattr(p, "vid", None), getattr(p, "pid", None)) in _NO_RESET_IDS
    except Exception:  # noqa: BLE001
        pass
    return False


def _install_no_reset_serial_open() -> None:
    try:
        import serial as _pyserial
    except Exception:
        return
    if getattr(_pyserial.Serial.open, "_wdgw_no_reset", False):
        return
    _orig = _pyserial.Serial.open

    def _open(self):
        if getattr(self, "port", None) in _NO_RESET_PORTS:
            try:
                self._dtr_state = False
                self._rts_state = False
            except Exception:
                pass
        return _orig(self)

    _open._wdgw_no_reset = True
    _pyserial.Serial.open = _open


_install_no_reset_serial_open()


# ---------------------------------------------------------------------------
# Unbounded, thread-safe store of every node heard (a local "node DB").
# ---------------------------------------------------------------------------
class MeshStore:
    def __init__(self, cap: int = 40000) -> None:
        self._lock = threading.Lock()
        self._nodes: dict[str, dict] = {}     # node_id -> display dict (+ _ts)
        self._cap = cap

    @staticmethod
    def _is_default_name(name: str, node_id: str) -> bool:
        n = (name or "").strip()
        return (not n) or n == node_id or n.lower().startswith("meshtastic ")

    def _merge(self, cur: dict, new: dict) -> dict:
        """Keep the best-known value for each field across sightings: never lose a
        real position or a custom name to a later packet that lacks them."""
        out = dict(cur)
        for k, v in new.items():
            if v is None:
                continue
            if k in ("lat", "lon") and out.get(k) is not None and v is None:
                continue
            if k == "name":
                # prefer a custom name over a firmware-default placeholder
                if self._is_default_name(v, new.get("node_id", "")) and not \
                   self._is_default_name(out.get("name", ""), out.get("node_id", "")):
                    continue
            if k == "heard_ts" and (out.get(k) or 0) >= v:
                continue   # a re-sweep of the radio's table carries an older stamp
            if k == "tele" and isinstance(v, dict):
                merged = dict(out.get("tele") or {})
                merged.update({kk: vv for kk, vv in v.items() if vv is not None})
                out["tele"] = merged
                continue
            out[k] = v
        # uploadable = is there a valid fix now?
        out["uploadable"] = out.get("lat") is not None and out.get("lon") is not None \
            and bool(out.get("node_id"))
        return out

    def put(self, node: dict | None) -> None:
        if not node:
            return
        nid = node.get("node_id")
        if not nid:
            return
        now = time.time()
        with self._lock:
            cur = self._nodes.get(nid)
            merged = self._merge(cur, node) if cur else dict(node)
            merged["_ts"] = now
            self._nodes[nid] = merged
            if len(self._nodes) > self._cap:   # evict the oldest heard
                for k in sorted(self._nodes, key=lambda x: self._nodes[x].get("_ts", 0))[
                        : len(self._nodes) - self._cap]:
                    self._nodes.pop(k, None)

    def touch(self, node_id: str, **fields) -> bool:
        """Update a known node in place. False if the node is not in the store."""
        with self._lock:
            cur = self._nodes.get(node_id)
            if cur is None:
                return False
            cur.update(fields)
            cur["_ts"] = time.time()
            return True

    def snapshot(self, recent_secs: float | None = None) -> list[dict]:
        """All heard nodes, most-recently-heard first. If ``recent_secs`` is set,
        only nodes heard within that window (for the 'in view now' feed)."""
        now = time.time()
        with self._lock:
            vals = list(self._nodes.values())
        if recent_secs is not None:
            vals = [v for v in vals if now - v.get("_ts", 0) <= recent_secs]
        vals.sort(key=lambda v: v.get("_ts", 0), reverse=True)
        return [{k: v for k, v in n.items() if k != "_ts"} for n in vals]

    def count(self) -> int:
        with self._lock:
            return len(self._nodes)

    def dump(self) -> dict:
        with self._lock:
            return {k: dict(v) for k, v in self._nodes.items()}

    def restore(self, data: dict) -> None:
        """Merge a persisted store back in (on startup) so captures accumulate ACROSS
        restarts instead of resetting to the device's capped node DB each time.
        """
        if not isinstance(data, dict):
            return
        with self._lock:
            for k, v in data.items():
                if isinstance(v, dict) and v.get("node_id"):
                    v.setdefault("_ts", 0)
                    self._nodes[k] = v
            if len(self._nodes) > self._cap:
                for x in sorted(self._nodes, key=lambda n: self._nodes[n].get("_ts", 0))[
                        : len(self._nodes) - self._cap]:
                    self._nodes.pop(x, None)


# ---------------------------------------------------------------------------
# Meshtastic worker — persistent SerialInterface + live receive subscription.
# ---------------------------------------------------------------------------
class _PositionRequests:
    """Queue a fixed position for the radio. The worker applies it on its own
    connection, since that thread is the only one allowed to talk to the radio."""
    _pos_request: tuple[float, float] | None = None
    position_status: str = ""

    def request_position(self, lat: float, lon: float) -> None:
        self.position_status = "setting"
        self._pos_request = (lat, lon)

    def _take_position_request(self) -> tuple[float, float] | None:
        req, self._pos_request = self._pos_request, None
        return req


class _SampleWorker:
    """Workers publish copies; only the service owns the pending handoff queue."""
    def _publish(self, sample: dict) -> None:
        # Once shutdown begins, an outstanding radio query cannot create work
        # after the core's final durable handoff.
        if self._stop_event.is_set():
            return
        sample = copy.deepcopy(sample)
        sample.setdefault("capture_id", str(uuid.uuid4()))
        if self._sample_sink:
            try:
                self._sample_sink(sample)
            except Exception as e:
                # Wardrift delivery backpressure must not disconnect the shared
                # radio or interrupt discovery for other upload destinations.
                self.error = f"Could not queue Wardrift health: {e}"
                log.error("%s", self.error)
        self.radio = sample


class MeshtasticWorker(_PositionRequests, _SampleWorker, threading.Thread):
    def __init__(self, port: str, store: MeshStore, hop_limit: int = 0,
                 advert_minutes: int = 60, transport: str = "serial",
                 ble_address: str = "auto", sample_sink: Callable | None = None) -> None:
        super().__init__(name=f"wdgw-cap-mt-{port or ble_address}", daemon=True)
        self.port = port
        self.store = store
        self.hop_limit = hop_limit
        self.advert_minutes = advert_minutes
        self.transport = transport
        self.ble_address = ble_address
        self._sample_sink = sample_sink
        self._stop_event = threading.Event()
        self.connected = False
        self.heard = 0
        self.radio: dict | None = None
        self.error = ""
        self._local_num: int | None = None
        self._iface = None

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._session()
            except Exception as e:
                self.error = str(e)
                log.warning("meshtastic capture on %s dropped: %s", self.port or self.ble_address, e)
            self.connected = False
            self._stop_event.wait(15)

    def _capture_all(self, iface) -> None:
        # A cached table is discovery only, never fresh telemetry evidence.
        for nid, node in dict(getattr(iface, "nodes", None) or {}).items():
            display = mesh.meshtastic_node_to_display(nid, node)
            self.store.put(display)
            if display and node.get("num") == self._local_num and self._local_num is not None:
                # Preserve the existing advertised-position display without
                # publishing cached telemetry to Wardrift's measurement queue.
                self.radio = {**(self.radio or {}), "node": display}

    def _apply_position(self, iface) -> None:
        req = self._take_position_request()
        if not req:
            return
        lat, lon = req
        try:
            iface.localNode.setFixedPosition(lat, lon, 0)
            me = iface.getMyNodeInfo()
            if isinstance(me, dict):
                me.setdefault("position", {}).update(latitude=lat, longitude=lon)
                node = mesh.meshtastic_node_to_display((me.get("user") or {}).get("id", ""), me)
                if node:
                    self.radio = {**(self.radio or {}), "node": node}
            self.position_status = "set"
            log.info("set fixed position on %s: %.5f, %.5f", self.port, lat, lon)
        except Exception as e:
            self.position_status = f"failed: {e}"
            log.warning("could not set position on %s: %s", self.port, e)
        # Manual position is an advertisement, never an internal GNSS fix.

    @staticmethod
    def _group(tel: dict, snake: str, camel: str) -> dict | None:
        raw = tel.get("raw")
        if raw is not None and hasattr(raw, "HasField"):
            try:
                if not raw.HasField(snake):
                    return None
                msg = getattr(raw, snake)
                return {f.json_name: getattr(msg, f.name) for f in msg.DESCRIPTOR.fields
                        if not f.has_presence or msg.HasField(f.name)}
            except (ValueError, AttributeError):
                return None
        # Dict-only callbacks preserve only explicit keys. Missing keys cannot
        # safely imply zero without a known, fresh protobuf message descriptor.
        group = tel.get(camel)
        return dict(group) if isinstance(group, dict) else None

    @staticmethod
    def _observed_at(tel: dict, received_at: float) -> float:
        # Telemetry.time is acquisition time (Unix seconds), with zero meaning
        # unset. A buffered packet keeps its own time; a radio clock that is far
        # from the PC's is wrong, so the time it reached the PC is used instead.
        raw = tel.get("raw")
        stamp = getattr(raw, "time", tel.get("time", 0))
        if stamp is None or stamp == 0 and not isinstance(stamp, bool):
            return received_at
        if (not isinstance(stamp, (int, float)) or isinstance(stamp, bool)
                or not math.isfinite(stamp) or stamp != int(stamp) or stamp < 0):
            raise ValueError("Radio telemetry clock is invalid; measurement was not redated")
        if not received_at - _CLOCK_BEHIND_SECS <= stamp <= received_at + _CLOCK_AHEAD_SECS:
            return received_at
        return float(stamp)

    def _on_receive(self, packet, interface, iface) -> None:
        if interface is not iface or self._stop_event.is_set():
            return
        try:
            now = time.time()
            frm = packet.get("fromId") or packet.get("from")
            nodes = getattr(iface, "nodes", None) or {}
            rec = nodes.get(frm) or nodes.get(str(frm))
            if rec:
                node = mesh.meshtastic_node_to_display(frm, rec)
                if node:
                    node["heard_ts"] = now
                    self.store.put(node)
                    self.heard += 1
            if self._local_num is None or packet.get("from") != self._local_num:
                return
            tel = (packet.get("decoded") or {}).get("telemetry")
            if not isinstance(tel, dict):
                return
            dm = self._group(tel, "device_metrics", "deviceMetrics")
            ls = self._group(tel, "local_stats", "localStats")
            if dm is None and ls is None:
                return
            observed = self._observed_at(tel, now)
            nid = f"!{self._local_num:08x}"
            me = nodes.get(nid) or {"num": self._local_num, "user": {"id": nid}}
            node = mesh.meshtastic_node_to_display(nid, me)
            if not node:
                return
            # Cached NODEINFO supplies display fields, never the authoritative
            # local identity established by the connection handshake.
            node["node_id"] = f"{self._local_num:08x}"
            md = getattr(iface, "metadata", None)
            firmware = getattr(md, "firmware_version", "") or "unknown"
            for group, is_stats in ((dm, False), (ls, True)):
                if group is None:
                    continue
                health = {}
                for wire, key in (("voltage", "voltage"), ("channel_utilization", "channelUtilization"),
                                  ("air_util_tx", "airUtilTx"), ("noise_floor_dbm", "noiseFloor")):
                    if key in group:
                        health[wire] = group[key]
                if "batteryLevel" in group:
                    level = group["batteryLevel"]
                    valid_level = type(level) is int and 0 <= level <= 2 ** 32 - 1
                    external = valid_level and level > 100
                    health["battery_level"] = None if external else level
                    health["external_power"] = external if valid_level else None
                counters = {}
                if is_stats:
                    for wire, key in (("num_packets_tx", "numPacketsTx"), ("num_packets_rx", "numPacketsRx"),
                                      ("num_packets_rx_bad", "numPacketsRxBad"), ("num_rx_dupe", "numRxDupe"),
                                      ("num_tx_relay", "numTxRelay"), ("num_tx_relay_canceled", "numTxRelayCanceled"),
                                      ("num_tx_dropped", "numTxDropped"), ("num_online_nodes", "numOnlineNodes"),
                                      ("num_total_nodes", "numTotalNodes")):
                        if key in group:
                            counters[wire] = group[key]
                sample = {
                    "protocol": "meshtastic", "transport": "ble" if self.transport == "ble" else "usb_serial",
                    "node": node, "observed_at": observed,
                    "host_received_at": now,
                    "firmware_version": firmware,
                    "profile": "meshtastic-localstats-v1" if is_stats else "meshtastic-devicemetrics-v1",
                    "uptime_seconds": group.get("uptimeSeconds"),
                    "uptime_at": observed if "uptimeSeconds" in group else None,
                    "health": health, "health_at": observed if health else None,
                    "battery_level": health.get("battery_level"), "voltage": health.get("voltage"),
                    "counters": counters, "counters_at": observed if is_stats else None,
                    "role": str((me.get("user") or {}).get("role") or "CLIENT"),
                }
                self._publish(sample)
            if self.error.startswith("Radio telemetry clock is invalid"):
                self.error = ""
        except Exception as e:
            self.error = str(e)
            log.warning("could not capture local Meshtastic telemetry: %s", e)

    def _open_iface(self, deadline: float = _NODEDB_WAIT + 30.0):
        """Construct the official interface with cancellation-aware cleanup.

        A timed-out third-party constructor reserves its endpoint until it returns;
        opening a replacement beside an unclosed constructor would leak handles.
        """
        if self.transport == "ble":
            import inspect
            from meshtastic.ble_interface import BLEInterface
            if not self.ble_address or self.ble_address.lower() == "auto":
                raise ValueError("Set an explicit Meshtastic BLE address in Settings")
            class _ClosingBLEInterface(BLEInterface):
                def __init__(self, **kwargs):
                    # The supported library can fail after connecting but before
                    # registering its exit handler. Keep partial construction
                    # closeable on pairing/config/notification failures as well.
                    self._exit_handler = None
                    self._want_receive = False
                    self._receiveThread = None
                    self.client = None
                    try:
                        super().__init__(**kwargs)
                    except BaseException:
                        try:
                            self.close()
                        except Exception:
                            log.debug("Could not close failed BLE construction", exc_info=True)
                        raise

            # Older supported SDKs have a fixed internal configuration timeout.
            # The outer connection deadline/endpoint reservation applies to both.
            options = {"address": self.ble_address}
            if "timeout" in inspect.signature(BLEInterface).parameters:
                options["timeout"] = int(deadline)
            factory = lambda: _ClosingBLEInterface(**options)
            return self._open_bounded(factory, deadline)
        from meshtastic.serial_interface import SerialInterface

        class _PatientSerialInterface(SerialInterface):
            # The library sends ONE config request and then waits a hard-coded 30 s. An
            # ESP32 radio in light sleep (a ROUTER wakes only on a LoRa packet - serial
            # data does not wake it) never sees that request, so the connect fails
            # however long it waits. Observed on a Station G1: awake ~10 s after each
            # packet, then asleep until the next. So keep a request outstanding: if the
            # radio has not started answering within a few seconds, send wake bytes plus
            # a fresh request. Never re-send once myInfo has arrived - that is the first
            # thing a radio returns, and a re-send would restart (and wipe) a download
            # already in progress. Each request carries a new random id, and only the
            # matching config_complete finishes the connect.
            def _waitConnected(self, timeout=30.0):
                import time as _time
                deadline = _time.monotonic() + max(timeout, _NODEDB_WAIT)
                while not self.isConnected.is_set() and not self.failure:
                    left = deadline - _time.monotonic()
                    if left <= 0:
                        break
                    if self.isConnected.wait(min(_REQUEST_RETRY_SECS, left)):
                        break
                    if self.myInfo is not None:
                        continue            # it is answering: let the download finish
                    try:
                        self._writeBytes(bytes([0xC3] * 32))   # START2 x32: resync/wake
                        _time.sleep(0.1)
                        self._startConfig()
                    except Exception:  # noqa: BLE001
                        break
                return super()._waitConnected(timeout=max(0.01, deadline - _time.monotonic()))

        # Same guard as the MeshCore worker: an ESP32 radio behind a USB-UART
        # bridge reboots when Windows toggles DTR/RTS on open.
        if _needs_no_reset(self.port):
            _NO_RESET_PORTS.add(self.port)
        else:
            _NO_RESET_PORTS.discard(self.port)
        return self._open_bounded(lambda: _PatientSerialInterface(devPath=self.port), deadline)

    def _open_bounded(self, factory, deadline):
        box: dict[str, Any] = {}
        cancelled = threading.Event()

        def opening():
            try:
                iface = factory()
                if cancelled.is_set():
                    iface.close()
                else:
                    box["iface"] = iface
            except Exception as e:
                box["err"] = e

        opener = threading.Thread(target=opening, name=f"open-{self.port or self.ble_address}", daemon=True)
        opener.start()
        end = time.monotonic() + deadline
        while opener.is_alive():
            opener.join(0.1)
            if self._stop_event.is_set() or time.monotonic() >= end:
                cancelled.set()
                self.error = "Radio connection did not close; endpoint held until cleanup finishes"
                # Keep the worker/endpoint reserved while a library constructor
                # still owns it. Never open another connection beside a hung one.
        if self._stop_event.is_set() or time.monotonic() >= end:
            cancelled.set()
        if cancelled.is_set():
            if "iface" in box:
                box["iface"].close()
            raise TimeoutError(self.error or "Radio connection canceled before use")
        if "err" in box:
            raise box["err"]
        return box["iface"]

    def _session(self) -> None:
        from pubsub import pub

        iface = self._open_iface()
        self._iface = iface
        self.connected = True
        self.error = ""
        # Only the handshake identifies this subject radio. A foreign interface's
        # callback or a cached record can never establish it.
        self._local_num = getattr(getattr(iface, "myInfo", None), "my_node_num", None)

        def on_recv(packet, interface):
            self._on_receive(packet, interface, iface)

        subscribed = False
        try:
            pub.subscribe(on_recv, "meshtastic.receive")
            subscribed = True
            if self.hop_limit:
                try:
                    mesh._maybe_set_meshtastic_hops(iface, self.hop_limit)
                except Exception:
                    pass
            self._capture_all(iface)
            last_advert = time.time()
            while not self._stop_event.wait(1):
                connected = getattr(iface, "isConnected", None)
                if connected is not None and not connected.is_set():
                    raise ConnectionError("Meshtastic disconnected")
                self._capture_all(iface)
                self._apply_position(iface)
                if self.advert_minutes and time.time() - last_advert >= self.advert_minutes * 60:
                    mesh._meshtastic_announce(iface)
                    last_advert = time.time()
        finally:
            self.connected = False
            self._local_num = None
            if subscribed:
                try:
                    pub.unsubscribe(on_recv, "meshtastic.receive")
                except Exception:
                    log.debug("Meshtastic callback was already unsubscribed", exc_info=True)
            try:
                iface.close()
            finally:
                self._iface = None


# ---------------------------------------------------------------------------
# MeshCore worker — persistent connection + live ADVERTISEMENT subscription.
# ---------------------------------------------------------------------------
class MeshCoreWorker(_PositionRequests, _SampleWorker, threading.Thread):
    def __init__(self, store: MeshStore, port: str = "", transport: str = "serial",
                 ble_address: str = "auto", baud: int = 115200,
                 advert_minutes: int = 60, sample_sink: Callable | None = None) -> None:
        super().__init__(name=f"wdgw-cap-mc-{port or ble_address}", daemon=True)
        self.store = store
        self.port = port
        self.transport = transport
        self.ble_address = ble_address
        self.baud = baud
        self.advert_minutes = advert_minutes
        self._stop_event = threading.Event()
        self.connected = False
        self.heard = 0
        self.radio: dict | None = None    # latest reading of the companion itself
        self._unsupported: set[str] = set()
        self._firmware: str | None = None
        self._sample_sink = sample_sink
        self.error = ""
        self._loop = None
        self._task = None

    def stop(self) -> None:
        self._stop_event.set()
        if self._loop and self._task:
            try:
                self._loop.call_soon_threadsafe(self._task.cancel)
            except RuntimeError:
                pass

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                asyncio.run(self._run_session())
            except asyncio.CancelledError:
                pass
            except Exception as e:
                self.error = str(e)
                log.warning("meshcore capture on %s dropped: %s", self.port or self.ble_address, e)
            finally:
                self._loop = self._task = None
            self.connected = False
            self._stop_event.wait(15)

    async def _run_session(self):
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.current_task()
        await self._session()

    def _capture_contact(self, contact: Any, heard: float | None = None) -> None:
        d = mesh.contact_to_node(contact)
        if d:
            if heard:
                d["heard_ts"] = heard
            self.store.put(d)
            self.heard += 1

    async def _apply_position(self, mc, EventType) -> None:
        """Set a queued position as the companion's advertised location."""
        req = self._take_position_request()
        if not req:
            return
        lat, lon = req
        try:
            ev = await asyncio.wait_for(mc.commands.set_coords(lat, lon), _QUERY_TIMEOUT)
            if ev is None or getattr(ev, "type", None) == EventType.ERROR:
                raise RuntimeError("the radio refused the position")
            info = getattr(mc, "self_info", None)
            if isinstance(info, dict):
                info["adv_lat"], info["adv_lon"] = lat, lon
            self.position_status = "set"
            log.info("set advertised position on %s: %.5f, %.5f",
                     self.port or self.ble_address, lat, lon)
        except Exception as e:  # noqa: BLE001
            self.position_status = f"failed: {e}"
            log.warning("could not set position on %s: %s", self.port or self.ble_address, e)
        await self._sample_radio(mc, EventType)

    async def _sample_radio(self, mc, EventType) -> None:
        """Queries run sequentially; each response is its own dated evidence.

        Slow radio queries never move an earlier uptime to a later timestamp.
        Missing/unsupported methods are suppressed only for this connection.
        """
        async def ask(name: str):
            if name in self._unsupported:
                return None
            try:
                fn = getattr(mc.commands, name, None)
                if not callable(fn):
                    self._unsupported.add(name)
                    return None
                ev = await asyncio.wait_for(fn(), timeout=_QUERY_TIMEOUT)
            except (AttributeError, NotImplementedError):
                self._unsupported.add(name)
                return None
            except asyncio.TimeoutError:
                self.error = f"Radio query {name} timed out; retrying next sample"
                return None
            except Exception as e:
                self.error = f"Radio query {name} failed: {e}"
                return None
            payload = getattr(ev, "payload", None)
            if ev is None or getattr(ev, "type", None) == EventType.ERROR:
                if isinstance(payload, dict) and payload.get("error_code") == 1:
                    self._unsupported.add(name)
                return None
            if isinstance(payload, dict):
                if self.error.startswith(f"Radio query {name} "):
                    self.error = ""
                return payload, time.time()
            return None

        if self._firmware is None:
            dev = await ask("send_device_query")
            if dev:
                # 'fw ver' is a binary protocol revision, NOT the firmware build.
                fw = dev[0].get("ver") or dev[0].get("fw_build")
                self._firmware = fw.strip() if isinstance(fw, str) and fw.strip() else "unknown"
        info = dict(getattr(mc, "self_info", None) or {})
        node = mesh.contact_to_node({"public_key": info.get("public_key"), "type": info.get("adv_type", 1),
                                     "adv_name": info.get("name"), "adv_lat": info.get("adv_lat"),
                                     "adv_lon": info.get("adv_lon")})
        if node:
            self.radio = {**(self.radio or {}), "node": node}
        pub = str((node or {}).get("public_key") or "")
        if len(pub) != 64 or any(c not in "0123456789abcdef" for c in pub):
            return

        def emit(result, *, health=None, counters=None, uptime=None):
            if not result:
                return
            at = result[1]
            health = health or {}
            self._publish({"protocol": "meshcore", "transport": "ble" if self.transport == "ble" else "usb_serial",
                           "node": node, "observed_at": at,
                           "firmware_version": self._firmware or "unknown", "profile": "meshcore-companion-v1",
                           "uptime_seconds": uptime, "uptime_at": at if uptime is not None else None,
                           "health": health, "health_at": at if health else None,
                           "battery_level": None, "voltage": health.get("voltage"),
                           "counters": counters or {}, "counters_at": at if counters else None,
                           "role": node.get("node_type") or "COMPANION"})

        def voltage(mv):
            return mv / 1000.0 if isinstance(mv, (int, float)) and not isinstance(mv, bool) else None

        core = await ask("get_stats_core")
        if core:
            emit(core, uptime=core[0].get("uptime_secs"), health={
                "voltage": voltage(core[0].get("battery_mv")),
                "queue_length": core[0].get("queue_len"), "error_flags": core[0].get("errors")})
        if not core or core[0].get("battery_mv") is None:
            bat = await ask("get_bat")
            if bat:
                emit(bat, health={"voltage": voltage(bat[0].get("level"))})
        radio = await ask("get_stats_radio")
        if radio:
            emit(radio, health={wire: radio[0][key] for wire, key in (
                ("rssi_dbm", "last_rssi"), ("snr_db", "last_snr"), ("noise_floor_dbm", "noise_floor"),
                ("tx_airtime_seconds", "tx_air_secs"), ("rx_airtime_seconds", "rx_air_secs")) if key in radio[0]})
        pkts = await ask("get_stats_packets")
        if pkts:
            counters = {wire: pkts[0][key] for wire, key in (
                ("num_packets_tx", "sent"), ("num_packets_rx", "recv"), ("num_packets_rx_bad", "recv_errors"),
                ("flood_tx", "flood_tx"), ("direct_tx", "direct_tx"), ("flood_rx", "flood_rx"),
                ("direct_rx", "direct_rx")) if key in pkts[0]}
            # MeshCore recv counts successful reads; recv_errors is separate.
            # Wardrift's common RX total includes both, as Meshtastic does. Old
            # frames lack errors: retain their reported RX and unknown errors.
            good, bad = pkts[0].get("recv"), pkts[0].get("recv_errors")
            if all(type(v) is int and 0 <= v <= 2 ** 32 - 1 for v in (good, bad)):
                total = good + bad
                counters["num_packets_rx"] = total if total <= 2 ** 32 - 1 else None
            emit(pkts, counters=counters)

    async def _open_serial(self, MeshCore):
        # Preserve the existing board-specific open guard and SDK factory.
        if _needs_no_reset(self.port):
            _NO_RESET_PORTS.add(self.port)
        else:
            _NO_RESET_PORTS.discard(self.port)
        return await asyncio.wait_for(MeshCore.create_serial(self.port, self.baud), 45)

    async def _session(self) -> None:
        from meshcore import MeshCore, EventType

        if self.transport == "ble":
            addr = None if (not self.ble_address or self.ble_address.lower() == "auto") else self.ble_address
            mc = await asyncio.wait_for(MeshCore.create_ble(addr), 45)
        else:
            mc = await self._open_serial(MeshCore)
        if mc is None or not getattr(mc, "is_connected", True):
            # Close before raising: the failed object can still own the port, and
            # a leaked handle turns the next retry into "Access is denied".
            if mc is not None:
                for closer in ("disconnect", "stop"):
                    fn = getattr(mc, closer, None)
                    if fn:
                        try:
                            res = fn()
                            if hasattr(res, "__await__"):
                                await res
                        except Exception:  # noqa: BLE001
                            pass
            raise RuntimeError("could not connect to a MeshCore companion")
        self.connected = True
        self.error = ""
        self._unsupported = set()
        self._firmware = None
        log.info("meshcore capture connected (%s)", self.port or self.ble_address)

        def on_advert(ev):
            # The ADVERTISEMENT push carries only the public key. A known node is
            # marked heard without resetting its stored type and name to defaults.
            payload = getattr(ev, "payload", None) or {}
            pub = str(payload.get("public_key") or "").lower()
            now = time.time()
            if pub and self.store.touch(pub[:16], heard_ts=now, first_seen=mesh._fmt_ts(now)):
                self.heard += 1
            else:
                self._capture_contact(payload, heard=now)

        def on_contact(ev):
            # NEW_CONTACT carries the full contact record.
            self._capture_contact(getattr(ev, "payload", None), heard=time.time())

        subscriptions = []
        try:
            subscriptions.append(mc.subscribe(EventType.ADVERTISEMENT, on_advert))
            subscriptions.append(mc.subscribe(EventType.NEW_CONTACT, on_contact))
            # seed from the current contact table
            res = await asyncio.wait_for(mc.commands.get_contacts(), _QUERY_TIMEOUT)
            payload = getattr(res, "payload", res)
            for c in (payload.values() if isinstance(payload, dict) else (payload or [])):
                self._capture_contact(c)
            await self._sample_radio(mc, EventType)
            last_advert = last_sample = time.time()
            while not self._stop_event.is_set():
                await asyncio.sleep(1)
                if not getattr(mc, "is_connected", True):
                    raise ConnectionError("MeshCore disconnected")
                await self._apply_position(mc, EventType)
                if time.time() - last_sample >= _RADIO_SAMPLE_SECS:
                    await self._sample_radio(mc, EventType)
                    last_sample = time.time()
                # drain any adverts the firmware refused to store (table full)
                for key in list(getattr(mc, "pending_contacts", {}) or {}):
                    c = mc.pop_pending_contact(key)
                    if c:
                        self._capture_contact(c)
                if self.advert_minutes and (time.time() - last_advert) >= self.advert_minutes * 60:
                    try:
                        await asyncio.wait_for(mc.commands.send_advert(flood=True), _QUERY_TIMEOUT)
                    except Exception:
                        pass
                    last_advert = time.time()
        finally:
            self.connected = False
            for subscription in subscriptions:
                try:
                    mc.unsubscribe(subscription)
                except Exception:
                    pass
            for closer in ("disconnect", "stop"):
                fn = getattr(mc, closer, None)
                if fn:
                    try:
                        r = fn()
                        if asyncio.iscoroutine(r):
                            await r
                    except Exception:
                        pass
                    break


# ---------------------------------------------------------------------------
# Service — one worker per configured device, feeding a shared store.
# ---------------------------------------------------------------------------
class MeshCaptureService:
    def __init__(self, persist_path: str | None = None) -> None:
        self.store = MeshStore()
        self._workers: dict[str, threading.Thread] = {}   # key -> worker
        self._lock = threading.Lock()
        self._persist_path = persist_path
        self._persist_ts = 0.0
        self._sample_lock = threading.Lock()
        self._pending: dict[str, dict] = {}
        self._pending_bytes = 0
        self._credential_fingerprint = ""
        self._accept_samples = True
        self._sample_error = ""
        self._config_errors: list[dict] = []
        self._load()

    def _load(self) -> None:
        if not self._persist_path:
            return
        try:
            import json
            import os
            if os.path.exists(self._persist_path):
                with open(self._persist_path, encoding="utf-8") as f:
                    self.store.restore(json.load(f))
                log.info("restored %d nodes from the persisted capture store", self.store.count())
        except Exception as e:  # noqa: BLE001
            log.debug("could not load capture store: %s", e)

    def persist(self, force: bool = False) -> None:
        """Save the accumulated store so it survives restarts (throttled)."""
        if not self._persist_path:
            return
        now = time.time()
        if not force and (now - self._persist_ts) < 30:
            return
        self._persist_ts = now
        try:
            import json
            import os
            import tempfile
            data = json.dumps(self.store.dump())
            d = os.path.dirname(self._persist_path)
            fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data)
            os.replace(tmp, self._persist_path)
        except Exception as e:  # noqa: BLE001
            log.debug("could not persist capture store: %s", e)

    @staticmethod
    def _endpoint(dev: dict) -> str:
        import os
        transport = dev.get("transport", "serial")
        endpoint = str(dev.get("ble_address", "") if transport == "ble" else dev.get("port", "")).strip()
        # Windows accepts both COM4 and its native \\.\COM4 device path.
        if transport == "serial" and endpoint.startswith("\\\\.\\") and endpoint[4:].upper().startswith("COM"):
            endpoint = endpoint[4:]
        if transport == "ble" or endpoint.upper().startswith("COM"):
            endpoint = endpoint.upper()
        elif endpoint.startswith("/"):
            endpoint = os.path.realpath(endpoint)
        return f"{transport}:{endpoint}"

    @staticmethod
    def _key(dev: dict) -> str:
        return f"{dev.get('protocol', 'meshcore')}:{MeshCaptureService._endpoint(dev)}"

    def set_credential_fingerprint(self, fingerprint: str) -> None:
        with self._sample_lock:
            self._credential_fingerprint = fingerprint or ""

    def _capture_sample(self, sample: dict) -> None:
        item = copy.deepcopy(sample)
        with self._sample_lock:
            if not self._accept_samples:
                return
            item.setdefault("capture_id", str(uuid.uuid4()))
            if item["capture_id"] in self._pending:
                return  # Never replace an immutable observation on handoff retry.
            item["credential_fingerprint"] = self._credential_fingerprint
            size = len(json.dumps(item).encode("utf-8"))
            if len(self._pending) >= _SAMPLE_CAP or self._pending_bytes + size > _SAMPLE_BYTE_CAP:
                self._sample_error = "Local health queue is full; fresh health capture paused until pending samples are saved"
                raise RuntimeError(self._sample_error)
            item["_queue_bytes"] = size
            self._pending[item["capture_id"]] = item
            self._pending_bytes += size

    def pending_samples(self) -> list[dict]:
        """Peek immutable observations; acknowledge only after durable staging."""
        with self._sample_lock:
            return copy.deepcopy(list(self._pending.values()))

    def acknowledge_samples(self, capture_ids) -> None:
        with self._sample_lock:
            for capture_id in capture_ids:
                old = self._pending.pop(capture_id, None)
                if old:
                    self._pending_bytes -= old.get("_queue_bytes", 0)
            if len(self._pending) < _SAMPLE_CAP and self._pending_bytes < _SAMPLE_BYTE_CAP:
                self._sample_error = ""

    def reconcile(self, devices: list[dict], hop_limit: int, advert_minutes: int,
                  transport: str = "serial", ble_address: str = "auto",
                  credential_fingerprint: str | None = None) -> None:
        """Close removed/changed connections before claiming their endpoints."""
        if credential_fingerprint is not None:
            self.set_credential_fingerprint(credential_fingerprint)
        with self._sample_lock:
            self._accept_samples = True
        want, errors, endpoints = {}, [], set()
        for slot, supplied in enumerate(devices or []):
            if not supplied.get("enabled", True):
                continue
            dev = dict(supplied)
            dev["_slot"] = slot
            proto = str(dev.get("protocol") or "meshcore").lower()
            dev["protocol"] = proto
            # Legacy global BLE defaults belong only to MeshCore.
            dev["transport"] = dev.get("transport") or (transport if proto == "meshcore" else "serial")
            if dev["transport"] == "usb_serial":
                dev["transport"] = "serial"
            dev["ble_address"] = dev.get("ble_address") or (ble_address if proto == "meshcore" else "")
            endpoint = self._endpoint(dev)
            error = ""
            if proto not in ("meshtastic", "meshcore") or dev["transport"] not in ("serial", "ble"):
                error = "Unsupported radio protocol or transport"
            elif proto == "meshtastic" and dev["transport"] == "ble" and str(dev["ble_address"]).lower() in ("", "auto"):
                error = "Set an explicit BLE address in Settings"
            elif dev["transport"] == "serial" and str(dev.get("port") or "").lower() in ("", "auto"):
                error = "No serial port selected or detected"
            elif endpoint in endpoints:
                error = "Another configured radio already uses this endpoint"
            if error:
                errors.append({**dev, "connected": False, "error": error})
                continue
            endpoints.add(endpoint)
            dev["_settings"] = (int(dev.get("baud", 115200) or 115200), hop_limit, advert_minutes)
            want[self._key(dev)] = dev
        with self._lock:
            self._config_errors = errors
            for key, worker in list(self._workers.items()):
                dev = want.get(key)
                if dev is None or getattr(worker, "_settings", None) != dev["_settings"]:
                    worker.stop()
                    worker.join(timeout=3)
                    if not worker.is_alive():
                        del self._workers[key]
            busy = {getattr(w, "_endpoint", "") for w in self._workers.values() if w.is_alive()}
            for key, dev in want.items():
                previous = self._workers.get(key)
                if previous is not None and previous.is_alive():
                    if getattr(previous, "_settings", None) != dev["_settings"]:
                        self._config_errors.append({**dev, "connected": False,
                                                    "error": "Waiting for the previous connection to close"})
                    continue
                # An entry switching USB/BLE has a different endpoint key, but
                # its previous worker must finish cleanup before replacement.
                if any(w.is_alive() and getattr(w, "_slot", None) == dev["_slot"]
                       for old_key, w in self._workers.items() if old_key != key and old_key not in want):
                    self._config_errors.append({**dev, "connected": False,
                                                "error": "Waiting for the previous connection to close"})
                    continue
                endpoint = self._endpoint(dev)
                if endpoint in busy:
                    self._config_errors.append({**dev, "connected": False,
                                                "error": "Waiting for the previous connection to close"})
                    continue
                common = {"transport": dev["transport"], "ble_address": dev["ble_address"],
                          "advert_minutes": advert_minutes, "sample_sink": self._capture_sample}
                if dev["protocol"] == "meshtastic":
                    worker = MeshtasticWorker(dev.get("port", ""), self.store, hop_limit=hop_limit, **common)
                else:
                    worker = MeshCoreWorker(self.store, port=dev.get("port", ""),
                                            baud=dev["_settings"][0], **common)
                worker._settings = dev["_settings"]
                worker._endpoint = endpoint
                worker._slot = dev["_slot"]
                worker.start()
                self._workers[key] = worker
                busy.add(endpoint)

    def stop(self) -> None:
        # Serialize with any callback currently enqueuing. No late callback can
        # add work after this method returns and the core performs its final save.
        with self._sample_lock:
            self._accept_samples = False
        with self._lock:
            for worker in self._workers.values():
                worker.stop()
            for key, worker in list(self._workers.items()):
                worker.join(timeout=3)
                if not worker.is_alive():
                    del self._workers[key]
        self.persist(force=True)
        # Pending samples remain readable for the core's final durable stage.

    def status(self) -> list[dict]:
        with self._lock:
            out = []
            for key, worker in self._workers.items():
                radio = copy.deepcopy(getattr(worker, "radio", None) or {})
                node = radio.get("node") or {}
                pos = [node["lat"], node["lon"]] if node.get("lat") is not None and node.get("lon") is not None else None
                out.append({"port": worker.port, "protocol": key.split(":", 1)[0],
                            "transport": worker.transport, "ble_address": worker.ble_address,
                            "connected": bool(worker.connected), "heard": worker.heard, "contacts": worker.heard,
                            "position": pos, "position_status": worker.position_status,
                            "observed_at": radio.get("observed_at"),
                            "error": self._sample_error or getattr(worker, "error", "")})
            return out + copy.deepcopy(self._config_errors)

    def snapshot(self, recent_secs: float | None = None) -> list[dict]:
        return self.store.snapshot(recent_secs)

    def set_position(self, lat: float, lon: float) -> int:
        """Queue a fixed position on every connected radio. Returns how many."""
        with self._lock:
            workers = [w for w in self._workers.values() if getattr(w, "connected", False)]
        for w in workers:
            w.request_position(lat, lon)
        return len(workers)

    def local_radios(self) -> list[dict]:
        """Latest reading of each connected radio; Wardrift uses pending_samples."""
        with self._lock:
            workers = list(self._workers.values())
        return [dict(w.radio) for w in workers
                if getattr(w, "connected", False) and getattr(w, "radio", None)]
