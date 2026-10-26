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
import logging
import threading
import time
import uuid
from typing import Any

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
class MeshtasticWorker(threading.Thread):
    def __init__(self, port: str, store: MeshStore, hop_limit: int = 0,
                 advert_minutes: int = 60) -> None:
        super().__init__(name=f"wdgw-cap-mt-{port}", daemon=True)
        self.port = port
        self.store = store
        self.hop_limit = hop_limit
        self.advert_minutes = advert_minutes
        self._stop = threading.Event()
        self.connected = False
        self.heard = 0
        self.radio: dict | None = None    # latest reading of the radio itself
        self._session_id = ""
        self._metrics_at: float | None = None
        self._stats_at: float | None = None

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self._session()
            except Exception as e:  # noqa: BLE001
                log.warning("meshtastic capture on %s dropped: %s", self.port, e)
            self.connected = False
            self._stop.wait(15)     # backoff before reconnect

    def _capture_all(self, iface) -> None:
        for nid, node in dict(getattr(iface, "nodes", None) or {}).items():
            d = mesh.meshtastic_node_to_display(nid, node)
            if d:
                self.store.put(d)

    def _sample_radio(self, iface) -> None:
        """Record the radio's own DeviceMetrics and LocalStats for the Wardrift
        mesh upload. Both arrive as telemetry packets from the radio itself."""
        try:
            me = iface.getMyNodeInfo() or {}
        except Exception:  # noqa: BLE001
            return
        user = me.get("user") or {}
        num = me.get("num")
        nid = user.get("id") or (f"!{num:08x}" if isinstance(num, int) else "")
        node = mesh.meshtastic_node_to_display(nid, me)
        if not node or len(node["node_id"]) != 8:
            return
        now = time.time()
        dm = me.get("deviceMetrics") or {}
        ls = me.get("localStats") or {}
        level = dm.get("batteryLevel")
        counters: dict[str, Any] = {}
        if ls:
            # Protobuf-to-dict leaves out zero values, so a missing counter is 0.
            counters = {"num_packets_tx": ls.get("numPacketsTx", 0),
                        "num_packets_rx": ls.get("numPacketsRx", 0),
                        "num_tx_relay": ls.get("numTxRelay", 0),
                        "num_tx_relay_canceled": ls.get("numTxRelayCanceled", 0)}
        # Uptime from whichever packet is newer: DeviceMetrics reach the computer
        # every minute, LocalStats every 15 minutes.
        if dm.get("uptimeSeconds") is not None and (
                not ls or (self._metrics_at or 0) >= (self._stats_at or 0)):
            uptime, uptime_at = dm.get("uptimeSeconds"), self._metrics_at
        else:
            uptime, uptime_at = (ls.get("uptimeSeconds") if ls else None), self._stats_at
        md = getattr(iface, "metadata", None)
        self.radio = {
            "protocol": "meshtastic",
            "transport": "usb_serial",
            "session": self._session_id,
            "node": node,
            "observed_at": now,
            "firmware_version": getattr(md, "firmware_version", "") or None,
            "uptime_seconds": uptime,
            "uptime_at": uptime_at or now,
            # 101 means external power, not a battery reading.
            "battery_level": level if isinstance(level, int) and 0 <= level <= 100 else None,
            "voltage": dm.get("voltage"),
            "health_at": (self._metrics_at or now) if dm else None,
            "counters": counters,
            "counters_at": (self._stats_at or now) if ls else None,
            "profile": ("meshtastic-localstats-v1" if ls else
                        "meshtastic-devicemetrics-v1" if dm else "meshtastic-basic-v1"),
        }

    def _open_iface(self, deadline: float = _NODEDB_WAIT + 30.0):
        """Open the SerialInterface in a sub-thread with a hard deadline - the
        meshtastic connect can deadlock un-interruptibly on a wedged port, which must
        not block the worker forever (it retries instead).
        """
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
        box: dict[str, Any] = {}

        def _open():
            try:
                box["iface"] = _PatientSerialInterface(devPath=self.port)
            except Exception as e:  # noqa: BLE001
                box["err"] = e

        t = threading.Thread(target=_open, name=f"open-{self.port}", daemon=True)
        t.start()
        t.join(deadline)
        if "iface" in box:
            return box["iface"]
        if "err" in box:
            raise box["err"]
        raise TimeoutError(f"meshtastic connect on {self.port} exceeded {deadline:.0f}s")

    def _session(self) -> None:
        from pubsub import pub

        iface = self._open_iface()
        self.connected = True
        self._session_id = uuid.uuid4().hex
        self._metrics_at = self._stats_at = None
        log.info("meshtastic capture connected on %s", self.port)
        if self.hop_limit:
            try:
                mesh._maybe_set_meshtastic_hops(iface, self.hop_limit)
            except Exception:
                pass

        def on_recv(packet, interface):   # fires on the lib's reader thread
            try:
                frm = packet.get("fromId") or packet.get("from")
                nodes = getattr(interface, "nodes", None) or {}
                rec = nodes.get(frm) or nodes.get(str(frm))
                if rec:
                    d = mesh.meshtastic_node_to_display(frm, rec)
                    if d:
                        d["heard_ts"] = time.time()
                        self.store.put(d)
                        self.heard += 1
                tel = (packet.get("decoded") or {}).get("telemetry") or {}
                my_num = getattr(getattr(interface, "myInfo", None), "my_node_num", None)
                if tel and my_num is not None and packet.get("from") == my_num:
                    if "deviceMetrics" in tel:
                        self._metrics_at = time.time()
                    if "localStats" in tel:
                        self._stats_at = time.time()
            except Exception:
                pass

        sub = pub.subscribe(on_recv, "meshtastic.receive")
        try:
            self._capture_all(iface)          # seed from the device's current DB
            self._sample_radio(iface)
            last_advert = time.time()
            while not self._stop.is_set():
                self._stop.wait(15)
                if self._stop.is_set():
                    break
                self._capture_all(iface)      # re-sweep the merged DB (cheap, in-memory)
                self._sample_radio(iface)
                if self.advert_minutes and (time.time() - last_advert) >= self.advert_minutes * 60:
                    mesh._meshtastic_announce(iface)
                    last_advert = time.time()
        finally:
            try:
                pub.unsubscribe(sub)
            except Exception:
                pass
            try:
                iface.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# MeshCore worker — persistent connection + live ADVERTISEMENT subscription.
# ---------------------------------------------------------------------------
class MeshCoreWorker(threading.Thread):
    def __init__(self, store: MeshStore, port: str = "", transport: str = "serial",
                 ble_address: str = "auto", baud: int = 115200,
                 advert_minutes: int = 60) -> None:
        super().__init__(name=f"wdgw-cap-mc-{port or ble_address}", daemon=True)
        self.store = store
        self.port = port
        self.transport = transport
        self.ble_address = ble_address
        self.baud = baud
        self.advert_minutes = advert_minutes
        self._stop = threading.Event()
        self.connected = False
        self.heard = 0
        self.radio: dict | None = None    # latest reading of the companion itself
        self._session_id = ""
        self._unsupported: set[str] = set()
        self._firmware: str | None = None

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                asyncio.run(self._session())
            except Exception as e:  # noqa: BLE001
                log.warning("meshcore capture on %s dropped: %s", self.port or self.ble_address, e)
            self.connected = False
            self._stop.wait(15)

    def _capture_contact(self, contact: Any, heard: float | None = None) -> None:
        d = mesh.contact_to_node(contact)
        if d:
            if heard:
                d["heard_ts"] = heard
            self.store.put(d)
            self.heard += 1

    async def _sample_radio(self, mc, EventType) -> None:
        """Read the companion's own battery, uptime and packet counters for the
        Wardrift mesh upload. A command the firmware answers with an error (older
        firmware has no GET_STATS) is not sent again this session."""
        async def ask(name: str, fn):
            if name in self._unsupported:
                return None
            try:
                ev = await fn()
            except Exception:  # noqa: BLE001
                return None
            payload = getattr(ev, "payload", None)
            if ev is None or ev.type == EventType.ERROR:
                if isinstance(payload, dict) and "error_code" in payload:
                    self._unsupported.add(name)
                return None
            return (payload, time.time()) if isinstance(payload, dict) else None

        if self._firmware is None and "device" not in self._unsupported:
            dev = await ask("device", mc.commands.send_device_query)
            if dev:
                self._firmware = (dev[0].get("ver") or dev[0].get("fw_build") or "").strip()
        bat = await ask("battery", mc.commands.get_bat)
        core = await ask("core", mc.commands.get_stats_core)
        pkts = await ask("packets", mc.commands.get_stats_packets)

        info = dict(getattr(mc, "self_info", None) or {})
        node = mesh.contact_to_node({
            "public_key": info.get("public_key"), "type": info.get("adv_type"),
            "adv_name": info.get("name"), "adv_lat": info.get("adv_lat"),
            "adv_lon": info.get("adv_lon")})
        if not node or len(node.get("public_key") or "") != 64:
            return
        mv, health_at = None, None
        if core and core[0].get("battery_mv"):
            mv, health_at = core[0]["battery_mv"], core[1]
        elif bat and bat[0].get("level"):
            mv, health_at = bat[0]["level"], bat[1]
        counters: dict[str, Any] = {}
        if pkts:
            counters = {"num_packets_tx": pkts[0].get("sent"),
                        "num_packets_rx": pkts[0].get("recv")}
        self.radio = {
            "protocol": "meshcore",
            "transport": "ble" if self.transport == "ble" else "usb_serial",
            "session": self._session_id,
            "node": node,
            "observed_at": time.time(),
            "firmware_version": self._firmware or None,
            "uptime_seconds": core[0].get("uptime_secs") if core else None,
            "uptime_at": core[1] if core else None,
            "battery_level": None,     # MeshCore reports millivolts only
            "voltage": round(mv / 1000.0, 3) if mv else None,
            "health_at": health_at,
            "counters": counters,
            "counters_at": pkts[1] if pkts else None,
            "profile": ("meshcore-stats-v1" if core or pkts else
                        "meshcore-battery-v1" if mv else "meshcore-basic-v1"),
        }

    async def _session(self) -> None:
        from meshcore import MeshCore, EventType

        if self.transport == "ble":
            addr = None if (not self.ble_address or self.ble_address.lower() == "auto") else self.ble_address
            mc = await MeshCore.create_ble(addr)
        else:
            if self.port:
                # Hold DTR/RTS low only where the lines reset the board (see
                # _NO_RESET_IDS). Doing it to an nRF52 board (T114) silences its CDC, which
                # presents exactly like "not a MeshCore companion".
                if _needs_no_reset(self.port):
                    _NO_RESET_PORTS.add(self.port)
                else:
                    _NO_RESET_PORTS.discard(self.port)
            mc = await MeshCore.create_serial(self.port, self.baud)
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
        self._session_id = uuid.uuid4().hex
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

        mc.subscribe(EventType.ADVERTISEMENT, on_advert)
        mc.subscribe(EventType.NEW_CONTACT, on_contact)
        try:
            # seed from the current contact table
            res = await mc.commands.get_contacts()
            payload = getattr(res, "payload", res)
            for c in (payload.values() if isinstance(payload, dict) else (payload or [])):
                self._capture_contact(c)
            await self._sample_radio(mc, EventType)
            last_advert = last_sample = time.time()
            while not self._stop.is_set():
                await asyncio.sleep(3)
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
                        await mc.commands.send_advert(flood=True)
                    except Exception:
                        pass
                    last_advert = time.time()
        finally:
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
    def _key(dev: dict) -> str:
        return f"{dev.get('protocol','meshcore')}:{dev.get('port','auto')}"

    def reconcile(self, devices: list[dict], hop_limit: int, advert_minutes: int,
                  transport: str = "serial", ble_address: str = "auto") -> None:
        """Start workers for enabled devices, stop those no longer wanted."""
        want: dict[str, dict] = {}
        for dev in devices or []:
            if dev.get("enabled", True):
                want[self._key(dev)] = dev
        with self._lock:
            # stop removed/disabled
            for key in list(self._workers):
                if key not in want:
                    w = self._workers.pop(key)
                    try:
                        w.stop()
                    except Exception:
                        pass
            # start new
            for key, dev in want.items():
                if key in self._workers and self._workers[key].is_alive():
                    continue
                proto = (dev.get("protocol") or "meshcore").lower()
                if proto == "meshtastic":
                    w: threading.Thread = MeshtasticWorker(
                        dev.get("port", ""), self.store, hop_limit=hop_limit,
                        advert_minutes=advert_minutes)
                else:
                    w = MeshCoreWorker(
                        self.store, port=dev.get("port", ""), transport=transport,
                        ble_address=ble_address, baud=int(dev.get("baud", 115200) or 115200),
                        advert_minutes=advert_minutes)
                w.start()
                self._workers[key] = w

    def stop(self) -> None:
        self.persist(force=True)   # never lose the accumulated store on shutdown
        with self._lock:
            for w in self._workers.values():
                try:
                    w.stop()
                except Exception:
                    pass
            self._workers.clear()

    def status(self) -> list[dict]:
        with self._lock:
            out = []
            for key, w in self._workers.items():
                proto, port = key.split(":", 1)
                heard = int(getattr(w, "heard", 0))
                out.append({"port": port, "protocol": proto,
                            "connected": bool(getattr(w, "connected", False)),
                            "heard": heard, "contacts": heard})
            return out

    def snapshot(self, recent_secs: float | None = None) -> list[dict]:
        return self.store.snapshot(recent_secs)

    def local_radios(self) -> list[dict]:
        """Latest reading of each connected radio's own health and counters."""
        with self._lock:
            workers = list(self._workers.values())
        return [dict(w.radio) for w in workers
                if getattr(w, "connected", False) and getattr(w, "radio", None)]
