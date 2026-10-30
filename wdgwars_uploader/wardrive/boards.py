"""WardriveESP boards, linked and run the way Wardrive Go runs them.

Every free board in range links over Bluetooth on its own; a board plugged in
over USB (firmware 1.6+) runs over USB instead and its Bluetooth link stays
down. Each board gets a role (capture, Wi-Fi or one board on Bluetooth) and a
share of the channels, and what the boards hear goes into the same run as this
PC's own adapters: access points, Bluetooth devices, handshakes and PMKIDs, and
a board GPS as one more location source.

A linked board is never dropped or rotated. A board held by another phone or
host is left alone, and a board this PC lets go hears ``bye`` so the next host
links it at once.

All board state lives on one thread with its own event loop; scanners and USB
readers hand their input over to it.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import esp_proto as P
from .capture import Hit
from .gps import Fix

log = logging.getLogger("wdgwars.boards")

STALL_S = 15.0
SETUP_SLOW_S = 32.0
SETUP_PATIENCE_S = 35.0
FAIL_WINDOW_S = 60.0
HEARD_S = 30.0
HEARD_CP_S = 90.0
OFFLINE_GRACE_S = 120.0
PLAN_SETTLE_S = 2.5
PLAN_MAX_WAIT_S = 8.0
PLAN_STAGGER_S = 0.15
BOARD_GROUP_SIZE = 6
BEACON_TTL_S = 30.0
TAKEN_UNSURE_S = 8.0
HOLDER_AFFINITY_S = 60.0
BYE_FLUSH_S = 0.3
FALLBACK_IDLE_S = 20.0

# USB serial chips WardriveESP boards use: CP210x, ESP32 native USB, CH340/CH341, CH9102/CH343.
USB_IDS = {(0x10C4, 0xEA60), (0x303A, 0x1001), (0x1A86, 0x7523), (0x1A86, 0x5523), (0x1A86, 0x55D4), (0x1A86, 0x55D3)}
NATIVE_USB = {(0x303A, 0x1001)}

CONNECTING, LINKED, STREAMING, OFFLINE = "connecting", "linked", "streaming", "offline"


def _now() -> float:
    return time.monotonic()


@dataclass
class BoardInfo:
    id: str
    name: str
    transport: str = "BLE"
    phase: str = CONNECTING
    chip: str | None = None
    bands: str | None = None
    supported: list[int] = field(default_factory=list)
    share: list[int] = field(default_factory=list)
    fw: str | None = None
    foreign: bool = False
    legacy: bool = False
    bt_capable: bool = False
    gps_capable: bool = False
    cap_capable: bool = False
    bt_wanted: bool = False
    cap_wanted: bool = False
    bt_mode: bool = False
    cap_mode: bool = False
    captures: int = 0
    board_name: str | None = None
    has_battery: bool = False
    battery: int | None = None
    coproc_mode: str | None = None
    coproc_state: str | None = None

    @property
    def dual_band(self) -> bool:
        return "5g" in (self.bands or "")

    @property
    def key(self) -> str:
        return P.board_suffix(self.name) or self.id

    @property
    def tag(self) -> str:
        return self.name.rsplit("-", 1)[-1] if "-" in self.name else self.id.replace(":", "")[-4:]

    @property
    def splittable(self) -> bool:
        return bool(self.supported)


class _Listener:
    def on_connected(self, t) -> None: ...
    def on_ready(self, t) -> None: ...
    def on_bytes(self, t, data: bytes) -> None: ...
    def on_closed(self, t, reason: str) -> None: ...


class _NoLink:
    """The place-holder link of a board that dropped: its tile shows while it is
    away, and its next advert links it again."""
    kind = "BLE"
    framed = True

    def __init__(self, bid: str, name: str) -> None:
        self.id, self.name = bid, name

    def open(self, listener) -> None: ...
    def send(self, command: str) -> bool: return False
    def resubscribe(self) -> None: ...
    def start_setup(self) -> None: ...
    def close(self) -> None: ...


class BleLink:
    """The GATT link to one board: connect (with discovery) -> the bridge's turn ->
    subscribe to TX -> ready. Commands go out in order, one write at a time."""
    kind = "BLE"
    framed = True

    def __init__(self, bridge: "BoardBridge", device: Any, name: str, timeout: float) -> None:
        self.bridge = bridge
        self.device = device
        self.id = str(getattr(device, "address", device)).upper()
        self.name = name
        self.timeout = timeout
        self.client = None
        self.rx = None
        self.closed = False
        self.listener: _Listener | None = None
        self._q: collections.deque = collections.deque()
        self._writing = False
        self._cid = 1

    def open(self, listener: _Listener) -> None:
        self.listener = listener
        self.bridge.loop.create_task(self._connect())

    async def _connect(self) -> None:
        from bleak import BleakClient
        try:
            self.client = BleakClient(self.device, disconnected_callback=self._on_disconnect, timeout=self.timeout)
            await self.client.connect()
        except Exception as e:  # noqa: BLE001 - out of range, refused, Bluetooth off
            self._fail(f"connect failed ({str(e)[:80] or type(e).__name__})")
            return
        if self.closed:
            await self._disconnect()
            return
        self.listener.on_connected(self)

    def start_setup(self) -> None:
        self.bridge.loop.create_task(self._setup())

    async def _setup(self) -> None:
        try:
            svc = self.client.services.get_service(P.SERVICE)
            if svc is None:
                self._fail("WardriveESP service not found")
                return
            self.rx = svc.get_characteristic(P.RX)
            tx = svc.get_characteristic(P.TX)
            if self.rx is None or tx is None:
                self._fail("RX/TX characteristic missing")
                return
            await self.client.start_notify(tx, self._on_notify)
        except Exception as e:  # noqa: BLE001
            self._fail(f"subscribe failed ({str(e)[:80] or type(e).__name__})")
            return
        if not self.closed:
            self.listener.on_ready(self)

    def _on_notify(self, _ch, data: bytearray) -> None:
        if not self.closed:
            self.listener.on_bytes(self, bytes(data))

    def _on_disconnect(self, _client) -> None:
        self.bridge.post(lambda: self._fail("disconnected"))

    def _fail(self, reason: str) -> None:
        if self.closed:
            return
        self.closed = True
        self.bridge.loop.create_task(self._disconnect())
        if self.listener:
            self.listener.on_closed(self, reason)

    def send(self, command: str) -> bool:
        b = command.encode("utf-8")
        if self.rx is None or self.closed or not b or b"\x00" in b:
            return False
        self._q.append(P.cmd_frame(command, self._cid))
        self._cid += 1
        if not self._writing:
            self._writing = True
            self.bridge.loop.create_task(self._drain())
        return True

    async def _drain(self) -> None:
        try:
            while self._q and not self.closed:
                frame = self._q.popleft()
                try:
                    # Without response first: a write whose answer never comes holds the link
                    # until the stack gives up. The board acks every command on the stream.
                    await self.client.write_gatt_char(self.rx, frame, response=False)
                except Exception:  # noqa: BLE001
                    try:
                        await self.client.write_gatt_char(self.rx, frame, response=True)
                    except Exception as e:  # noqa: BLE001
                        self.bridge.note(f"{self.name}: write failed ({str(e)[:60]})")
        finally:
            self._writing = False

    def resubscribe(self) -> None:
        async def again() -> None:
            try:
                tx = self.client.services.get_service(P.SERVICE).get_characteristic(P.TX)
                try:
                    await self.client.stop_notify(tx)
                except Exception:  # noqa: BLE001
                    pass
                await self.client.start_notify(tx, self._on_notify)
            except Exception as e:  # noqa: BLE001
                self.bridge.note(f"{self.name}: re-subscribe failed ({str(e)[:60]})")
        if self.client is not None and not self.closed:
            self.bridge.loop.create_task(again())

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._q.clear()
        self.bridge.loop.create_task(self._disconnect())

    async def _disconnect(self) -> None:
        c = self.client
        if c is None:
            return
        try:
            await asyncio.wait_for(c.disconnect(), 5)
        except Exception:  # noqa: BLE001
            pass


class UsbLink:
    """One open USB session as a bridge link: plain text lines in, commands out.
    Its reader thread writes the queued commands to the port."""
    kind = "USB"
    framed = False

    def __init__(self, bridge: "BoardBridge", bid: str, name: str) -> None:
        self.bridge = bridge
        self.id, self.name = bid, name
        self.out: collections.deque = collections.deque()
        self.closed = False
        self.listener: _Listener | None = None

    def open(self, listener: _Listener) -> None:
        self.listener = listener
        listener.on_connected(self)
        listener.on_ready(self)

    def send(self, command: str) -> bool:
        c = command.strip()[:240]
        if self.closed or not c:
            return False
        self.out.append(c)
        return True

    def resubscribe(self) -> None:
        self.send("caps")

    def start_setup(self) -> None: ...

    def close(self) -> None:
        self.closed = True
        self.out.clear()

    def line(self, text: str) -> None:
        if self.closed:
            return
        data = (text + "\n").encode("utf-8")
        self.bridge.post(lambda: self.listener and self.listener.on_bytes(self, data))

    def end(self, reason: str) -> None:
        if self.closed:
            return
        self.closed = True
        self.bridge.post(lambda: self.listener and self.listener.on_closed(self, reason))


class _Session:
    def __init__(self, t, info: BoardInfo) -> None:
        self.t = t
        self.info = info
        self.since = _now()
        self.offline_since = 0.0
        self.offline_hidden = False
        self.last_rx = 0.0
        self.ready = False
        self.rx_buf = bytearray()
        self.line_buf = bytearray()
        self.last_caps_ask = 0.0
        self.iam_sent: str | None = None
        self.nudged_at = 0.0
        self.setup_started_at = 0.0
        self.heard: dict[str, float] = {}
        self.heard_bt: dict[str, float] = {}
        self.heard_cp: dict[str, float] = {}
        self.coproc_asked = False
        self.bt_commanded = False
        self.cap_commanded = False
        self.sent_plan: list[int] | None = None
        self.want_plan: list[int] | None = None
        self.want_all = False


@dataclass
class _Beacon:
    id: str
    name: str
    rssi: int
    seen_at: float
    linked: bool
    holder: str = ""
    holder_iphone: bool = False
    usb: bool = False
    fw: str | None = None
    link_end: int = 0


class _HeldElsewhere:
    def __init__(self, holder: str, at: float) -> None:
        self.holder, self.at, self.logged = holder, at, False


class BoardGps:
    """The boards' GPS as one more location source (the most accurate fresh fix of
    every source is the one used)."""
    id = "gps:boards"
    name = "WardriveESP GPS"

    def __init__(self) -> None:
        self.fix: Fix | None = None
        self.state = "no fix yet"
        self.detail = ""
        self.present = False

    def status(self) -> dict[str, Any]:
        f = self.fix if self.fix and self.fix.age() < 30 else None
        return {"id": self.id, "name": self.name, "state": self.state, "detail": self.detail,
                "fix": f.as_dict() if f else None}


def windows_country() -> str | None:
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(16)
        if ctypes.windll.kernel32.GetUserDefaultGeoName(buf, 16):
            return buf.value
    except Exception:  # noqa: BLE001
        pass
    return None


class BoardBridge(_Listener):
    def __init__(self, on_hit: Callable[[Hit], None], on_event: Callable[[str], None]) -> None:
        self.on_hit = on_hit
        self.on_event = on_event
        self.gps = BoardGps()
        self.loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._aps: dict[str, dict[str, Any]] = {}
        self._bts: dict[str, dict[str, Any]] = {}
        self._published: list[dict[str, Any]] = []
        self._nearby: list[dict[str, Any]] = []
        self.usb = UsbBoards(self)
        self.region = P.Region.for_country(windows_country())
        # settings, set from any thread through configure()
        self.enabled = True
        self.off: set[str] = set()
        self.nickname = ""
        # bridge-thread state
        self.sessions: dict[str, _Session] = {}
        self.usb_held: dict[str, str] = {}
        self.held: set[str] = set()
        self.crash_looping: dict[str, str] = {}
        self.bad_boots: dict[str, list[tuple[int, float]]] = {}
        self.brownouts: dict[str, list[tuple[int, float]]] = {}
        self.link_fails: dict[str, tuple[int, float]] = {}
        self.setup_active: str | None = None
        self.setup_active_since = 0.0
        self.setup_queue: collections.deque = collections.deque()
        self.plan_slot: dict[str, int] = {}
        self.plan_memory: dict[str, list[int]] = {}
        self.outbox: collections.deque = collections.deque()
        self.outbox_busy = False
        self.outbox_defer_since = 0.0
        self.plan_first_ask = 0.0
        self._plan_handle: asyncio.TimerHandle | None = None
        self.last_plan_line = ""
        self.captured_keys: set[str] = set()
        self.beacons: dict[str, _Beacon] = {}
        self.linked_unsure_since: dict[str, float] = {}
        self.held_elsewhere: dict[str, _HeldElsewhere] = {}
        self.advert_posted: dict[str, float] = {}
        self.last_main_advert = 0.0
        self._fallback = None
        self._fallback_started = 0.0
        self._note_at: dict[str, float] = {}
        self.announced: set[str] = set()
        self.stopping = False
        self._dirty = True
        self._tick = 0

    # ---- lifecycle (any thread) -------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        ready = threading.Event()

        def run() -> None:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.loop.call_soon(ready.set)
            self.loop.call_later(1.0, self._tick_cb)
            self.loop.run_forever()
            try:
                self.loop.run_until_complete(asyncio.sleep(0.2))
            except Exception:  # noqa: BLE001
                pass
            self.loop.close()

        self.stopping = False
        self._thread = threading.Thread(target=run, name="wardriveesp", daemon=True)
        self._thread.start()
        ready.wait(5)
        self._log(f"bridge on, looking for WardriveESP boards (channel region {self.region.name})")
        self.usb.start()

    def stop(self, wait: float = 3.0) -> None:
        """Let every board go (`bye` first, so another host links it at once) and stop."""
        self.usb.stop()
        if not self.loop or not self._thread:
            return
        done = threading.Event()

        def shut() -> None:
            self.stopping = True
            n = sum(1 for s in self.sessions.values() if self._say_bye(s))

            def finish() -> None:
                self._teardown()

                async def flush() -> None:
                    await asyncio.sleep(0.5)
                    self.loop.stop()
                    done.set()
                self.loop.create_task(flush())
            self.loop.call_later(BYE_FLUSH_S if n else 0, finish)
        self.post(shut)
        done.wait(wait)
        self._thread.join(1.0)
        self._thread = None

    def post(self, fn: Callable[[], None]) -> None:
        loop = self.loop
        if loop is None or loop.is_closed():
            return

        def safe() -> None:
            try:
                fn()
            except Exception:  # noqa: BLE001
                log.exception("WardriveESP bridge error")
        try:
            loop.call_soon_threadsafe(safe)
        except RuntimeError:
            pass

    def configure(self, enabled: bool, off: set[str], nickname: str, skip_ports: set[str]) -> None:
        """``off``: board ids ("12FA") switched off on this PC. ``skip_ports``: COM ports that
        belong to something else (mesh radios, the PortaPack, a GPS) and are never opened."""
        self.usb.skip = set(skip_ports)
        nick = P.holder_nickname(nickname)

        def apply() -> None:
            was = self.enabled
            self.enabled = enabled
            newly_off = off - self.off
            self.off = set(off)
            if nick != self.nickname:
                self.nickname = nick
                for s in self.sessions.values():
                    self._introduce(s)
            if was and not enabled:
                self._log("bridge off")
                if sum(1 for s in self.sessions.values() if self._say_bye(s)):
                    self.loop.call_later(BYE_FLUSH_S, lambda: (not self.enabled) and self._teardown())
                else:
                    self._teardown()
            elif enabled and not was:
                self._log("bridge on, looking for WardriveESP boards")
            for sid, s in list(self.sessions.items()):
                if s.info.key in newly_off and s.t.kind != "USB":
                    self._log(f"{s.info.name}: switched off on this PC")
                    self._say_bye(s)
                    self.held.discard(sid)
                    t = s.t
                    self.loop.call_later(BYE_FLUSH_S, t.close)
                    self._drop(sid, None)
            self._dirty = True
        self.post(apply)

    # ---- adverts (scanner threads) ----------------------------------------
    def on_advert(self, device: Any, adv: Any) -> None:
        """Every advert of the PC's own Bluetooth scan comes through here (cheap)."""
        self.last_main_advert = _now()
        self._advert(device, adv)

    def _advert(self, device: Any, adv: Any) -> None:
        uuids = [u.lower() for u in (adv.service_uuids or [])]
        name = adv.local_name
        if P.SERVICE not in uuids and P.STATUS not in uuids and not P.is_board_name(name):
            return
        bid = str(device.address).upper()
        now = _now()
        last = self.advert_posted.get(bid)
        if last is not None and now - last < 1.0:
            return
        self.advert_posted[bid] = now
        mfg = (adv.manufacturer_data or {}).get(P.BEACON_COMPANY)
        ext = (adv.service_data or {}).get(P.EXTRAS)
        beacon = P.parse_beacon(mfg)
        extras = P.parse_extras(ext)
        status = P.STATUS in uuids or bool(beacon and beacon.linked)
        rssi = int(getattr(adv, "rssi", 0) or 0)
        self.post(lambda: self._advert_locked(device, bid, name, rssi, beacon, extras, status))

    # ---- bridge thread -----------------------------------------------------
    def _log(self, msg: str) -> None:
        log.info("WardriveESP: %s", msg)

    def note(self, msg: str) -> None:
        self._log(msg)

    def _note_once(self, key: str, msg: str) -> None:
        now = _now()
        if now - self._note_at.get(key, -60.0) < 60.0:
            return
        self._note_at[key] = now
        self._log(msg)

    def _advert_locked(self, device, bid: str, adv_name: str | None, rssi: int, b: P.Beacon | None,
                       x: P.Extras | None, status: bool) -> None:
        self._note_beacon(bid, adv_name, rssi, b, x, status)
        if status or not self.enabled or self.stopping:
            return
        dev_name = getattr(device, "name", None)
        name = adv_name if P.board_suffix(adv_name) else             dev_name if P.board_suffix(dev_name) else             (self.beacons[bid].name if bid in self.beacons else None) or adv_name or P.NAME_PREFIX
        key = P.board_suffix(name) or bid
        if key in self.off:
            self._note_once("off" + bid, f"{name} heard · switched off on this PC")
            return
        if bid in self.crash_looping:
            self._note_once("crash" + bid, f"{name} heard · not linking (paused after repeated crashes)")
            return
        if self._on_usb(name):
            self._note_once("usb" + bid, f"{name} heard · on USB, not linking over Bluetooth")
            return
        now = _now()
        e = self.held_elsewhere.get(bid)
        if e is not None:
            ago = now - e.at
            if self.beacons.get(bid) and self.beacons[bid].link_end == P.LINK_END_RELEASED:
                del self.held_elsewhere[bid]
                self._log(f"{name}: released by {e.holder}, linking")
            elif ago < HOLDER_AFFINITY_S:
                if not e.logged:
                    e.logged = True
                    self._log(f"{name}: held by {e.holder} until {int(ago)} s ago, leaving it for them")
                return
            else:
                del self.held_elsewhere[bid]
                if e.logged:
                    self._log(f"{name}: {e.holder} hasn't held it for {int(ago)} s, linking")
        direct_busy = any(s.info.id != bid and s.info.phase == CONNECTING and now - s.since < 10
                          for s in self.sessions.values())
        s = self.sessions.get(bid)
        if s is not None:
            if s.info.phase == CONNECTING:
                return
            if s.info.phase == OFFLINE:
                wait = self.link_fails.get(bid, (0, 0.0))[1]
                if wait > now:
                    self._note_once("wait" + bid, f"{name} heard · next try in {int(wait - now + 1)} s")
                    return
                if not direct_busy:
                    self._log(f"{s.info.name}: back ({rssi} dBm), linking")
            else:
                # Linked, yet advertising: it rebooted faster than the old link timed out.
                if now - s.since < 3 or now - s.last_rx < 15:
                    return
                self._log(f"{s.info.name}: advertising again, the old link is gone, relinking")
                s.t.close()
                self._drop(bid, None, keep_offline=True)
        if direct_busy:
            self._note_once("busy" + bid, f"{name} heard ({rssi} dBm) · waiting for another board's link to finish")
            return
        linked = sum(1 for z in self.sessions.values() if z.info.phase in (LINKED, STREAMING))
        self._connect(BleLink(self, device, name, STALL_S + 2.0 * linked - 1.0))

    def _connect(self, t) -> None:
        old = self.sessions.get(t.id)
        if old is not None and old.t is not t:
            old.t.close()
        self._setup_done(t.id)
        self.sessions[t.id] = _Session(t, BoardInfo(t.id, t.name, t.kind))
        self._log(f"found {t.name}, connecting…")
        t.open(self)
        self._dirty = True

    def _on_usb(self, name: str | None) -> bool:
        sfx = P.board_suffix(name)
        return sfx is not None and sfx in self.usb_held

    def attach_usb(self, t: UsbLink) -> None:
        def run() -> None:
            if not self.enabled:
                t.close()
                return
            sfx = P.board_suffix(t.name)
            if sfx:
                self.usb_held[sfx] = t.id
                for sid, s in list(self.sessions.items()):
                    if s.t is not t and s.t.kind != "USB" and P.board_suffix(s.info.name) == sfx:
                        s.t.close()
                        self._drop(sid, "on USB now, Bluetooth link closed")
            self._log(f"{t.name}: USB session open")
            self._connect(t)
        self.post(run)

    def _release_usb(self, s: _Session) -> None:
        sfx = P.board_suffix(s.info.name)
        if sfx and self.usb_held.get(sfx) == s.t.id:
            del self.usb_held[sfx]
            self._log(f"{s.info.name}: off USB, it links over Bluetooth when it advertises")

    # listener (bridge thread)
    def _session(self, t) -> _Session | None:
        s = self.sessions.get(t.id)
        return s if s is not None and s.t is t else None

    def on_connected(self, t) -> None:
        s = self._session(t)
        if s is None:
            t.close()
            return
        s.info.phase = LINKED
        s.since = _now()
        if t.kind == "BLE":
            self.held.add(t.id)
            self.held_elsewhere.pop(t.id, None)
            self._begin_setup(s)
        self._dirty = True

    def on_ready(self, t) -> None:
        s = self._session(t)
        if s is None:
            return
        s.ready = True
        self._setup_done(t.id)
        s.coproc_asked = False
        self._log(f"{t.name} subscribed, starting the stream")
        self._send(s, P.START)
        if s.info.fw is None and s.info.phase == STREAMING:
            s.last_caps_ask = _now()
            self._send(s, "caps")
        if s.info.splittable:
            self._schedule_plan()

    def on_bytes(self, t, data: bytes) -> None:
        s = self._session(t)
        if s is None:
            return
        s.last_rx = _now()
        if t.framed:
            s.rx_buf += data
            self._drain_frames(s)
        else:
            s.line_buf += data
            self._drain_lines(s)

    def on_closed(self, t, reason: str) -> None:
        s = self._session(t)
        if s is None:
            return
        if t.id in self.held and self.enabled:
            self._note_link_fail(t.id, s)
            self._drop(t.id, f"{reason}, reconnects when back", keep_offline=True)
            return
        self._drop(t.id, reason)

    def _drop(self, sid: str, note: str | None, keep_offline: bool = False) -> None:
        s = self.sessions.pop(sid, None)
        if s is None:
            return
        if note:
            self._log(f"{s.info.name}: {note}")
        self._setup_done(sid)
        if keep_offline and self.enabled and sid in self.held:
            info = BoardInfo(sid, s.info.name, s.info.transport, OFFLINE, chip=s.info.chip, bands=s.info.bands,
                             fw=s.info.fw, board_name=s.info.board_name)
            z = _Session(_NoLink(sid, s.info.name), info)
            z.offline_since = _now()
            self.sessions[sid] = z
        if s.t.kind == "USB":
            self._release_usb(s)
        self._dirty = True
        if s.ready:
            self._schedule_plan(PLAN_MAX_WAIT_S)

    def _note_link_fail(self, sid: str, s: _Session) -> None:
        now = _now()
        if s.info.phase not in (LINKED, STREAMING) or now - s.since >= FAIL_WINDOW_S:
            return
        count = self.link_fails.get(sid, (0, 0.0))[0] + 1
        wait = 0.0 if count < 2 else min(60.0, 10.0 * (1 << min(count - 2, 3)))
        self.link_fails[sid] = (count, now + wait)
        if wait:
            self._log(f"{s.info.name}: link didn't hold ({count} in a row), next try in {int(wait)} s")

    # setup, one board at a time
    def _begin_setup(self, s: _Session) -> None:
        sid = s.info.id
        if sid in self.setup_queue:
            self.setup_queue.remove(sid)
        a = self.setup_active
        if a and a != sid and a in self.sessions and _now() - self.setup_active_since < SETUP_SLOW_S:
            self.setup_queue.append(sid)
            self._log(f"{s.info.name} connected, setting up after {len(self.setup_queue)} other(s)")
            return
        self._start_setup(s, False)

    def _start_setup(self, s: _Session, from_queue: bool) -> None:
        self.setup_active = s.info.id
        self.setup_active_since = s.setup_started_at = _now()
        self._log(f"{s.info.name}: its turn, subscribing…" if from_queue else f"{s.info.name} connected, subscribing…")
        s.t.start_setup()

    def _setup_done(self, sid: str) -> None:
        if sid in self.setup_queue:
            self.setup_queue.remove(sid)
        if self.setup_active != sid:
            return
        self.setup_active = None
        self._next_setup()

    def _next_setup(self) -> None:
        if self.setup_active is not None:
            return
        while self.setup_queue:
            s = self.sessions.get(self.setup_queue.popleft())
            if s is None or s.info.phase != LINKED or s.setup_started_at or s.ready:
                continue
            self._start_setup(s, True)
            return

    def _teardown(self) -> None:
        self.held.clear()
        self.crash_looping.clear()
        self.bad_boots.clear()
        self.brownouts.clear()
        self.linked_unsure_since.clear()
        self.setup_active = None
        self.setup_queue.clear()
        self.link_fails.clear()
        self.outbox_defer_since = 0.0
        for s in self.sessions.values():
            s.t.close()
        self.sessions.clear()
        self.usb_held.clear()
        self.outbox.clear()
        if self._plan_handle:
            self._plan_handle.cancel()
        self.plan_first_ask = 0.0
        self.plan_slot.clear()
        self.plan_memory.clear()
        self.last_plan_line = ""
        self._stop_fallback()
        with self._lock:
            self._aps.clear()
            self._bts.clear()
        self._dirty = True

    def _drop_stalled(self) -> None:
        now = _now()
        # A LINKED board is never dropped or rotated: with many links slowness is load,
        # not a hang. Linked boards only get nudges spaced by the board count.
        linked = sum(1 for s in self.sessions.values() if s.info.phase in (LINKED, STREAMING))
        nudge_every = max(30.0, 5.0 * linked)
        if self.setup_active and now - self.setup_active_since > SETUP_SLOW_S:
            if self.setup_queue and self.setup_active in self.sessions:
                self._log(f"{self.sessions[self.setup_active].info.name}: setup is slow, letting the next board start")
            self.setup_active = None
        self._next_setup()
        for sid, s in list(self.sessions.items()):
            if s.info.phase in (LINKED, STREAMING) and now - s.since > FAIL_WINDOW_S:
                self.link_fails.pop(sid, None)
            ph = s.info.phase
            if ph == CONNECTING and now - s.since > STALL_S + 2.0 * linked:
                s.t.close()
                if sid in self.held:
                    self._drop(sid, "link attempt didn't complete, reconnects when back", keep_offline=True)
                else:
                    self._drop(sid, f"link attempt hung ({int(now - s.since)} s), retrying")
            elif ph == LINKED and s.last_rx == 0 and now - s.since > 20:
                if sid in self.setup_queue or (s.setup_started_at and now - s.setup_started_at < SETUP_PATIENCE_S):
                    continue
                if s.nudged_at and now - s.nudged_at < nudge_every:
                    continue
                s.nudged_at = now
                self._log(f"{s.info.name}: linked but silent, re-subscribing and asking for caps")
                s.t.resubscribe()
                if s.ready:
                    self._send(s, "caps")
            elif ph == LINKED and s.ready and now - s.since > 20:
                if s.nudged_at and now - s.nudged_at < nudge_every:
                    continue
                s.nudged_at = now
                self._log(f"{s.info.name}: linked but not streaming, asking for caps and restarting the stream")
                self._send(s, "caps")
                self._restart_stream(s)
            elif ph == STREAMING and s.ready and s.info.fw is None and now - s.since > 20:
                if s.nudged_at and now - s.nudged_at < nudge_every:
                    continue
                s.nudged_at = now
                self._log(f"{s.info.name}: streaming without board details, asking again")
                self._send(s, "caps")

    def _tick_cb(self) -> None:
        try:
            self._tick_locked()
        except Exception:  # noqa: BLE001
            log.exception("WardriveESP tick failed")
        if self.loop and self.loop.is_running():
            self.loop.call_later(1.0, self._tick_cb)

    def _tick_locked(self) -> None:
        self._tick += 1
        now = _now()
        for sid, s in list(self.sessions.items()):
            if s.info.phase == OFFLINE and not s.offline_hidden and now - s.offline_since > OFFLINE_GRACE_S:
                s.offline_hidden = True
                self._dirty = True
        cutoff = now - BEACON_TTL_S
        for bid in [k for k, b in self.beacons.items() if b.seen_at < cutoff]:
            del self.beacons[bid]
            self._dirty = True
        for bid in [k for k, e in self.held_elsewhere.items() if now - e.at > HOLDER_AFFINITY_S * 2]:
            del self.held_elsewhere[bid]
        if len(self.advert_posted) > 256:
            self.advert_posted = {k: v for k, v in self.advert_posted.items() if v >= cutoff}
        if self.gps.fix and self.gps.fix.age() > 10:
            self.gps.state = "no fix yet"
        for s in self.sessions.values():
            if s.info.fw and s.info.phase == STREAMING and now - s.since > 15:
                self._announce(s)
        if self._tick % 5 == 0 and self.enabled:
            self._drop_stalled()
            self._maintain_fallback()
        if self._tick % 2 == 0 or self._dirty:
            self._publish()

    # own scan while the PC's main Bluetooth scan isn't delivering
    def _maintain_fallback(self) -> None:
        now = _now()
        want = self.enabled and now - self.last_main_advert > FALLBACK_IDLE_S
        if want and self._fallback is None and now - self._fallback_started > 30:
            self._fallback_started = now
            self.loop.create_task(self._run_fallback())
        elif not want and self._fallback is not None and now - self._fallback_started > 30:
            self._stop_fallback()

    async def _run_fallback(self) -> None:
        try:
            from bleak import BleakScanner
            sc = BleakScanner(lambda d, a: self._advert(d, a), scanning_mode="active")
            await sc.start()
            self._fallback = sc
            self._log("PC Bluetooth scan idle, own board scan started")
        except Exception as e:  # noqa: BLE001 - no adapter, Bluetooth off
            self._note_once("fallback", f"board scan unavailable ({str(e)[:80] or type(e).__name__})")

    def _stop_fallback(self) -> None:
        sc, self._fallback = self._fallback, None
        if sc is not None and self.loop:
            async def stop() -> None:
                try:
                    await sc.stop()
                except Exception:  # noqa: BLE001
                    pass
            self.loop.create_task(stop())

    # beacons and who holds a board
    def _note_beacon(self, bid: str, adv_name: str | None, rssi: int, b: P.Beacon | None, x: P.Extras | None,
                     status: bool) -> None:
        now = _now()
        prev = self.beacons.get(bid)
        if b is not None:
            name = (adv_name if not status and P.is_board_name(adv_name) else None) \
                or (prev.name if prev and P.board_suffix(prev.name) == P.board_suffix(b.name) else None) or b.name
            nxt = _Beacon(bid, name, rssi, now, status or b.linked, holder=b.holder if (status or b.linked) else "",
                          holder_iphone=b.holder_iphone, usb=b.usb, fw=b.fw, link_end=prev.link_end if prev else 0)
        elif status:
            if prev is None:
                return
            nxt = prev
            nxt.rssi, nxt.seen_at, nxt.linked = rssi, now, True
        else:
            name = adv_name if P.is_board_name(adv_name) else (prev.name if prev else None)
            if name is None:
                return
            nxt = _Beacon(bid, name, rssi, now, False, link_end=prev.link_end if prev else 0,
                          fw=prev.fw if prev else None)
        if x is not None:
            nxt.link_end = x.link_end
        if prev is None or (prev.linked, prev.holder, prev.name) != (nxt.linked, nxt.holder, nxt.name):
            self._dirty = True
        self.beacons[bid] = nxt
        s = self.sessions.get(bid)
        if s is not None and P.board_suffix(s.info.name) is None and P.board_suffix(nxt.name):
            # Linked before its name was heard: the status beacon carries it.
            self._log(f"{s.info.name} ({bid}) is {nxt.name}")
            s.info.name = s.t.name = nxt.name
            self._dirty = True
            if s.info.fw:
                self._announce(s)
        self._taken_check(bid, nxt, now)
        self._note_holder(bid, nxt, now)

    def _note_holder(self, bid: str, b: _Beacon, now: float) -> None:
        if not b.linked:
            return
        s = self.sessions.get(bid)
        if s is not None and s.info.phase in (LINKED, STREAMING):
            return
        if b.usb:
            other = not self._on_usb(b.name)
        elif b.holder:
            other = b.holder_iphone or b.holder != self.nickname
        else:
            other = s is None
        if not other:
            return
        who = "a USB host" if b.usb else (b.holder or "another phone")
        e = self.held_elsewhere.setdefault(bid, _HeldElsewhere(who, now))
        e.holder, e.at, e.logged = who, now, False

    def _taken_check(self, bid: str, b: _Beacon, now: float) -> None:
        """A board this PC held that another host picks up leaves this PC at once and
        links here again only once it advertises as free."""
        s = self.sessions.get(bid)
        away = s is not None and s.t.kind != "USB" and s.info.phase in (OFFLINE, CONNECTING)
        if s is None or not away or not b.linked:
            self.linked_unsure_since.pop(bid, None)
            return
        ours = not b.holder_iphone and bool(b.holder) and b.holder == self.nickname
        if b.usb:
            other = not self._on_usb(b.name)
        elif b.holder:
            other = not ours
        else:
            other = False
        if not other:
            if ours:
                self.linked_unsure_since.pop(bid, None)
                return
            since = self.linked_unsure_since.setdefault(bid, now)
            if now - since < TAKEN_UNSURE_S:
                return
        self.linked_unsure_since.pop(bid, None)
        who = "a USB host" if b.usb else (b.holder or "another phone")
        self.held.discard(bid)
        s.t.close()
        self._drop(bid, f"picked up by {who}, off this PC (links here again when it's free)")
        self._schedule_plan(0)

    def _say_bye(self, s: _Session) -> bool:
        """Firmware 1.7.3+: this PC lets the board go on purpose, so other hosts link it
        without waiting."""
        if s.t.kind == "USB" or not s.ready or not s.info.fw or P.is_older(s.info.fw, P.BYE_MIN_FW):
            return False
        return s.t.send("bye")

    def _introduce(self, s: _Session) -> None:
        """Firmware 1.6.5+: tell the board who holds the link, so other hosts see it busy."""
        if not s.ready or not s.info.fw or P.is_older(s.info.fw, P.IAM_MIN_FW):
            return
        if not self.nickname or s.iam_sent == self.nickname:
            return
        if s.t.send(f"iam a {self.nickname}"):
            s.iam_sent = self.nickname

    # roles and the channel split
    def _decide_roles(self) -> None:
        """Capture on every capable board. With 2+ boards and a dual-band one, ONE board
        goes Bluetooth, preferring a 2.4 GHz-only board, and keeps it while it fits.
        Never zero Wi-Fi/capture boards."""
        linked = [s for s in self.sessions.values() if s.ready and not s.info.foreign]
        if not linked:
            return
        desired: dict[str, str] = {}
        bt_id = None
        if len(linked) >= 2 and any(s.info.dual_band for s in linked):
            capable = sorted((s for s in linked if s.info.bt_capable), key=lambda s: s.info.id)
            pool = [s for s in capable if not s.info.dual_band] or capable
            pick = next((s for s in pool if s.info.bt_wanted or s.bt_commanded), pool[0] if pool else None)
            bt_id = pick.info.id if pick else None
        for s in linked:
            desired[s.info.id] = "bt" if s.info.id == bt_id else ("cap" if s.info.cap_capable else "wifi")
        if all(v == "bt" for v in desired.values()):
            f = linked[0]
            desired[f.info.id] = "cap" if f.info.cap_capable else "wifi"
        for s in linked:
            role = desired.get(s.info.id, "wifi")
            s.info.bt_wanted, s.info.cap_wanted = role == "bt", role == "cap"

    def _group(self, s: _Session) -> int:
        return self.plan_slot.get(s.info.id, 0) // BOARD_GROUP_SIZE + 1

    def _decide_plans(self) -> None:
        for s in self.sessions.values():
            s.want_plan, s.want_all = None, False
        live = [s for s in self.sessions.values() if s.info.splittable and s.ready and not s.info.foreign]
        for sid in list(self.plan_slot):
            if sid not in self.sessions:
                del self.plan_slot[sid]
                self.plan_memory.pop(f"cap/{sid}", None)
                self.plan_memory.pop(f"wifi/{sid}", None)
        for s in sorted(live, key=lambda z: (z.since, z.info.id)):
            if s.info.id in self.plan_slot:
                continue
            taken = set(self.plan_slot.values())
            seat = 0
            while seat in taken:
                seat += 1
            self.plan_slot[s.info.id] = seat
        for s in live:
            if not s.info.cap_wanted:
                self.plan_memory.pop(f"cap/{s.info.id}", None)
            if s.info.cap_wanted or s.info.bt_wanted:
                self.plan_memory.pop(f"wifi/{s.info.id}", None)
        ordered = sorted(live, key=lambda z: (self.plan_slot[z.info.id], z.info.id))
        groups: dict[int, list[_Session]] = {}
        for s in ordered:
            groups.setdefault(self._group(s), []).append(s)
        for members in groups.values():
            self._split_group("cap", [s for s in members if s.info.cap_wanted], lone_sweeps_all=True)
            self._split_group("wifi", [s for s in members if not s.info.bt_wanted and not s.info.cap_wanted],
                              lone_sweeps_all=False)
        self._log_plan(live)

    def _split_group(self, role: str, boards: list[_Session], lone_sweeps_all: bool) -> None:
        region = self.region
        previous = {s.info.id: self.plan_memory[f"{role}/{s.info.id}"] for s in boards
                    if f"{role}/{s.info.id}" in self.plan_memory}
        plan = None if lone_sweeps_all and len(boards) <= 1 else \
            P.plan([(s.info.id, region.limit(s.info.supported)) for s in boards], set(), previous)
        for s in boards:
            key = f"{role}/{s.info.id}"
            share = plan.shares.get(s.info.id) if plan else None
            if share is not None:
                self.plan_memory[key] = share
            else:
                self.plan_memory.pop(key, None)
            universe = region.limit(s.info.supported)
            s.want_plan = share if share is not None else universe
            s.want_all = share is None and len(universe) == len(s.info.supported)

    def _log_plan(self, live: list[_Session]) -> None:
        if not live:
            return
        parts = []
        for s in sorted(live, key=lambda z: (self._group(z), z.info.tag)):
            what = "bt" if s.info.bt_wanted else ("all" if s.want_all else P.compact(s.want_plan or []))
            parts.append(f"G{self._group(s)} {s.info.tag}→{what}")
        line = " · ".join(parts)
        if line == self.last_plan_line:
            return
        self.last_plan_line = line
        self._log(f"channel plan ({len(live)} boards): {line}")

    def _schedule_plan(self, delay: float | None = None) -> None:
        if not self.loop:
            return
        delay = PLAN_SETTLE_S if delay is None else delay
        now = _now()
        if not self.plan_first_ask:
            self.plan_first_ask = now
        wait = max(0.0, min(delay, PLAN_MAX_WAIT_S - (now - self.plan_first_ask)))
        if self._plan_handle:
            self._plan_handle.cancel()
        self._plan_handle = self.loop.call_later(wait, self._plan_pass)

    def _plan_pass(self) -> None:
        self.plan_first_ask = 0.0
        try:
            self._decide_roles()
            self._decide_plans()
            for s in sorted(self.sessions.values(), key=lambda z: z.info.name):
                if self._needs_send(s) and s.info.id not in self.outbox:
                    self.outbox.append(s.info.id)
            self._dirty = True
            self._drain_outbox()
        except Exception:  # noqa: BLE001
            log.exception("WardriveESP plan failed")

    def _needs_send(self, s: _Session) -> bool:
        if not s.ready or s.info.foreign:
            return False
        if s.info.bt_wanted != s.bt_commanded or s.info.cap_wanted != s.cap_commanded:
            return True
        return s.want_plan is not None and s.want_plan != s.sent_plan

    def _drain_outbox(self) -> None:
        if self.outbox_busy:
            return
        if self.setup_active is None:
            self.outbox_defer_since = 0.0
        elif self.outbox:
            now = _now()
            if not self.outbox_defer_since:
                self.outbox_defer_since = now
            if now - self.outbox_defer_since < PLAN_MAX_WAIT_S:
                self.outbox_busy = True
                self.loop.call_later(0.5, self._outbox_next)
                return
        while self.outbox:
            s = self.sessions.get(self.outbox.popleft())
            if s is None or not self._send_wanted(s):
                continue
            self.outbox_busy = True
            self.loop.call_later(PLAN_STAGGER_S, self._outbox_next)
            return

    def _outbox_next(self) -> None:
        self.outbox_busy = False
        self._drain_outbox()

    def _send_wanted(self, s: _Session) -> bool:
        if not self._needs_send(s):
            return False
        want_bt, want_cap = s.info.bt_wanted, s.info.cap_wanted
        if want_bt != s.bt_commanded or want_cap != s.cap_commanded:
            cmd = "cap on" if want_cap else "bt on" if want_bt else "cap off" if s.cap_commanded else "bt off"
            s.bt_commanded, s.cap_commanded = want_bt, want_cap
            s.sent_plan = None
            s.info.share = []
            if not want_bt:
                s.heard_bt.clear()
            self._send(s, cmd)
        p = s.want_plan
        if p is not None and p != s.sent_plan and not want_bt:
            s.sent_plan = p
            s.info.share = [] if s.want_all else list(p)
            if want_cap:
                self._send(s, "ch all" if s.want_all else "ch " + ",".join(map(str, p)))
            else:
                self._send(s, f"{P.START} -ch " + ",".join(map(str, p)))
        self._dirty = True
        return True

    def _restart_stream(self, s: _Session) -> None:
        self._send(s, P.START)
        s.bt_commanded = s.cap_commanded = False
        s.sent_plan = None
        s.info.share = []
        self._schedule_plan(0)

    # commands and parsing
    def _send(self, s: _Session, command: str) -> None:
        if s.ready and s.t.send(command):
            log.debug("WardriveESP: → [%s] %s", s.info.tag, command)

    def _drain_frames(self, s: _Session) -> None:
        b = s.rx_buf
        while len(b) >= P.HEADER:
            if b[0] != P.MAGIC0 or b[1] != P.MAGIC1:
                k = 1
                while k < len(b) and b[k] != P.MAGIC0:
                    k += 1
                del b[:k]
                continue
            n = b[10] | (b[11] << 8)
            frame = P.HEADER + n
            if len(b) < frame:
                return
            kind = b[3]
            payload = bytes(b[P.HEADER:frame])
            del b[:frame]
            if kind == P.T_DATA:
                s.line_buf += payload
                self._drain_lines(s)
            elif kind == P.T_ERR:
                msg = payload.decode("utf-8", "replace")
                if msg:
                    self._log(f"{s.info.name}: ERR {msg}")
                    if "comm link down" in msg.lower():
                        self._mark_foreign(s)

    def _drain_lines(self, s: _Session) -> None:
        b = s.line_buf
        while True:
            nl = b.find(b"\n")
            if nl < 0:
                if len(b) > 8192:
                    b.clear()
                return
            line = b[:nl].decode("utf-8", "replace").strip()
            del b[:nl + 1]
            if line:
                try:
                    self._parse_line(s, line)
                except Exception:  # noqa: BLE001
                    log.debug("WardriveESP: bad line %r", line[:120], exc_info=True)

    def _mark_foreign(self, s: _Session) -> None:
        if not s.info.foreign:
            s.info.foreign = True
            self._log(f"{s.info.name}: isn't running WardriveESP (it answers with an error instead of streaming)")
            self._dirty = True

    _DATA = ("WD:AP ", "WD:BT ", "WD:STATUS", "WD:CLIENT ", "WD:HS ", "WD:PMKID ", "WD:GPS ")

    def _parse_line(self, s: _Session, line: str) -> None:
        if "comm link down" in line.lower():
            self._mark_foreign(s)
        now = _now()
        if s.info.phase != STREAMING and line.startswith(self._DATA):
            s.info.phase = STREAMING
            self._log(f"{s.info.name}: streaming")
            self._dirty = True
        if s.info.fw is None and s.ready and s.info.phase == STREAMING and not line.startswith("WD:CAPS"):
            n = sum(1 for z in self.sessions.values() if z.info.phase in (LINKED, STREAMING))
            if now - s.last_caps_ask > max(4.0, 1.0 * n):
                s.last_caps_ask = now
                self._send(s, "caps")
        if line.startswith("WD:AP "):
            ap = P.parse_ap(line)
            if ap is None:
                return
            (s.heard_cp if ap.coproc else s.heard)[ap.bssid] = now
            self._note_ap(ap, s.info.name)
            return
        if line.startswith("WD:BT "):
            bt = P.parse_bt(line)
            if bt is None:
                return
            (s.heard_cp if bt.coproc else s.heard_bt)[bt.mac] = now
            self._note_bt(bt, s.info.name)
            return
        if line.startswith("WD:GPS "):
            g = P.parse_gps(line)
            if g is None:
                return
            if not s.info.gps_capable:
                s.info.gps_capable = True
                self._dirty = True
            self.gps.fix = Fix(g.lat, g.lon, g.alt, 10.0, None, None, g.sats, time.time(), now, f"{s.info.name} GPS")
            self.gps.present = True
            self.gps.state, self.gps.detail = "fix", s.info.name
            return
        if line.startswith(("WD:HS ", "WD:PMKID ")):
            c = P.parse_capture(line)
            if c is None:
                return
            s.info.captures += 1
            self._dirty = True
            if c.key in self.captured_keys:
                return
            self.captured_keys.add(c.key)
            if len(self.captured_keys) > 20_000:
                self.captured_keys.clear()
            self._log(f"★ {'HANDSHAKE' if c.kind == 2 else 'PMKID'} captured: {c.ssid or c.bssid} ({s.info.name})")
            hit = Hit(kind=c.kind, key=c.key, bssid=c.bssid, sta=c.sta, ssid=c.ssid or None, channel=c.channel,
                      rssi=c.rssi, time=time.time(), hashline=c.hc22000)
            threading.Thread(target=self.on_hit, args=(hit,), daemon=True).start()
            return
        if line.startswith("WD:CLIENT "):
            return
        if line.startswith("WD:MODE "):
            mode = P.key_values(line, "WD:MODE").get("mode", "wifi")
            bt, cap = mode == "bt", mode == "cap"
            if (s.info.bt_mode, s.info.cap_mode) != (bt, cap):
                s.info.bt_mode, s.info.cap_mode = bt, cap
                self._log(f"{s.info.name}: " + ("capturing (handshakes/PMKIDs)" if cap else
                                                f"scanning {'Bluetooth' if bt else 'Wi-Fi'}"))
                self._dirty = True
            return
        if line.startswith("WD:COPROC "):
            kv = P.key_values(line, "WD:COPROC")
            mode = kv.get("mode")
            if mode in P.COPROC_MODES:
                if s.info.coproc_mode and s.info.coproc_mode != mode or mode == "off":
                    s.heard_cp.clear()
                if (s.info.coproc_mode, s.info.coproc_state) != (mode, kv.get("state", "")):
                    s.info.coproc_mode, s.info.coproc_state = mode, kv.get("state", "")
                    self._dirty = True
            return
        if line.startswith("WD:HEALTH "):
            kv = P.key_values(line, "WD:HEALTH")
            try:
                pct = max(0, min(100, int(kv["batt"])))
            except (KeyError, ValueError):
                return
            if s.info.battery != pct:
                s.info.battery, s.info.has_battery = pct, True
                self._dirty = True
            return
        if line.startswith("WD:CAPS "):
            kv = P.key_values(line, "WD:CAPS")
            i = s.info
            before = (list(i.supported), i.bt_capable, i.cap_capable, i.bands)
            i.chip, i.bands, i.fw = kv.get("chip"), kv.get("bands"), kv.get("fw")
            i.bt_capable, i.gps_capable, i.cap_capable = kv.get("bt") == "1", kv.get("gps") == "1", kv.get("cap") == "1"
            i.supported = P.channel_list(kv.get("chans"))
            i.legacy = False
            i.board_name = (kv.get("board_name") or "").replace("_", " ").strip() or None
            i.has_battery = kv.get("batt") == "1"
            i.coproc_mode = kv.get("coproc") if kv.get("coproc") in P.COPROC_MODES else None
            if i.coproc_mode is None:
                s.heard_cp.clear()
            if i.coproc_mode is not None and not s.coproc_asked and s.ready:
                s.coproc_asked = True
                self._send(s, "coproc")
            self._log(f"{i.name}: {i.chip or 'ESP32'} · fw {i.fw or '?'} · {'2.4 + 5 GHz' if i.dual_band else '2.4 GHz'}"
                      f" · {len(i.supported)} channels" + (" · Bluetooth scan" if i.bt_capable else "")
                      + (" · GPS" if i.gps_capable else "") + (" · capture" if i.cap_capable else "")
                      + (f" · {i.board_name}" if i.board_name else "") + f" · {s.t.kind}")
            if P.board_suffix(i.name):
                self._announce(s)
            self._introduce(s)
            self._dirty = True
            if before != (i.supported, i.bt_capable, i.cap_capable, i.bands):
                self._schedule_plan()
            return
        if line.startswith("WD:BEGIN"):
            if s.info.fw is None and P.is_board_name(s.info.name):
                s.info.legacy = True
            s.info.phase = STREAMING
            self._dirty = True
        elif line.startswith("WD:BOOT "):
            self._check_crash_loop(s, line)
        elif line.startswith("WD:") and not line.startswith(("WD:STATUS", "WD:HEALTH")):
            self._log(f"[{s.info.tag}] {line[:200]}")

    def _announce(self, s: _Session) -> None:
        """One activity line per board per run, once its name and details are known."""
        i = s.info
        if i.key in self.announced or i.id in self.announced:
            return
        self.announced.update((i.key, i.id))
        self.on_event(f"{i.name} linked over {'USB' if s.t.kind == 'USB' else 'Bluetooth'}"
                      f" ({i.board_name or i.chip or 'ESP32'} · fw {i.fw or '?'})")

    def _check_crash_loop(self, s: _Session, line: str) -> None:
        """Three different boots ending in a crash or watchdog reset within 90 s: stop
        re-linking that board. A brownout never counts, only a power hint in the log."""
        kv = P.key_values(line, "WD:BOOT")
        reset = kv.get("reset")
        try:
            boots = int(kv["boots"])
        except (KeyError, ValueError):
            return
        name, now = s.info.name, _now()
        if reset == "brownout":
            seen = [x for x in self.brownouts.get(name, []) if now - x[1] < 90]
            if any(x[0] == boots for x in seen):
                return
            seen.append((boots, now))
            self.brownouts[name] = seen
            if len(seen) == 3:
                self._log(f"{name}: keeps losing power (3 brownouts in 90 s), check its cable or battery")
            return
        if reset not in ("crash", "watchdog"):
            return
        seen = [x for x in self.bad_boots.get(name, []) if now - x[1] < 90]
        if any(x[0] == boots for x in seen):
            return
        seen.append((boots, now))
        self.bad_boots[name] = seen
        self._log(f"{name}: came back from a {reset} reset (boot #{boots}, {len(seen)} in the last 90 s)")
        if len(seen) < 3:
            return
        sid = s.info.id
        self.crash_looping[sid] = name
        self.held.discard(sid)
        self.bad_boots.pop(name, None)
        self._log(f"{name}: keeps crashing, {len(seen)} resets in under 90 s. Stopped re-linking it.")
        self.on_event(f"{name} keeps crashing, stopped re-linking it")
        s.t.close()
        self._drop(sid, f"stopped re-linking ({reset} loop)")

    # ---- what the boards hear (read from any thread) ----------------------
    def _note_ap(self, ap: P.Ap, board: str) -> None:
        now, utc = _now(), time.time()
        with self._lock:
            prev = self._aps.get(ap.bssid)
            # The strongest reading of the last few seconds, and when it was heard, for the GPS match.
            if prev is None or now - prev["at"] > 4 or ap.rssi >= prev["rssi"]:
                self._aps[ap.bssid] = {
                    "mac": ap.bssid, "ssid": ap.ssid or (prev or {}).get("ssid", ""), "rssi": max(-120, min(-20, ap.rssi)),
                    "freq": P.frequency(ap.channel) or (prev or {}).get("freq"),
                    "channel": ap.channel or (prev or {}).get("channel"),
                    "auth": ap.caps or (prev or {}).get("auth", ""), "adapter": board, "at": now, "utc": utc, "seen": now}
            else:
                prev["seen"] = now
                if ap.ssid and not prev["ssid"]:
                    prev["ssid"] = ap.ssid

    def _note_bt(self, bt: P.Bt, board: str) -> None:
        now, utc = _now(), time.time()
        mac = bt.mac.lower()
        with self._lock:
            prev = self._bts.get(mac)
            if prev is None or now - prev["at"] > 4 or bt.rssi >= prev["rssi"]:
                rec = {"mac": mac, "name": bt.name or (prev or {}).get("name", ""), "rssi": max(-120, min(-20, bt.rssi)),
                       "mfgr": bt.company_id, "adapter": board, "at": now, "utc": utc, "seen": now}
                if bt.classic_cod is not None:
                    rec["type"], rec["auth"] = "BT", P.classic_auth_mode(bt.classic_cod)
                self._bts[mac] = rec
            else:
                prev["seen"] = now
                if bt.name and not prev["name"]:
                    prev["name"] = bt.name

    def _fresh(self, d: dict[str, dict[str, Any]], max_age: float) -> list[dict[str, Any]]:
        now = _now()
        with self._lock:
            for k in [k for k, v in d.items() if now - v["seen"] > 300]:
                del d[k]
            return [dict(v) for v in d.values() if now - v["seen"] <= max_age]

    def fresh_wifi(self, max_age: float) -> list[dict[str, Any]]:
        return self._fresh(self._aps, max_age)

    def fresh_bt(self, max_age: float) -> list[dict[str, Any]]:
        return self._fresh(self._bts, max_age)

    # ---- dashboard ---------------------------------------------------------
    def _publish(self) -> None:
        self._dirty = False
        now = _now()
        out = []
        for s in self.sessions.values():
            i = s.info
            if i.phase == OFFLINE and s.offline_hidden:
                continue
            role = "Bluetooth" if i.bt_mode or i.bt_wanted else "capture" if i.cap_mode or i.cap_wanted else "Wi-Fi"
            scanning = [] if i.bt_wanted else (i.share or self.region.limit(i.supported))
            chans = "" if i.bt_wanted else ("all channels" if not i.share else f"{len(i.share)} channels")
            out.append({
                "id": f"esp:{i.key}", "name": i.name, "board": i.board_name, "transport": i.transport,
                "phase": i.phase, "chip": i.chip, "fw": i.fw, "role": role, "channels": chans,
                "channel_list": P.compact(scanning),
                "aps": sum(1 for t in s.heard.values() if now - t < HEARD_S),
                "bt": sum(1 for t in s.heard_bt.values() if now - t < HEARD_S),
                "coproc": sum(1 for t in s.heard_cp.values() if now - t < HEARD_CP_S),
                "captures": i.captures, "battery": i.battery, "gps": i.gps_capable, "foreign": i.foreign,
                "crashing": i.id in self.crash_looping,
            })
        out.sort(key=lambda b: b["name"])
        here = {P.board_suffix(b["name"]) for b in out}
        nearby = [{"name": b.name, "holder": b.holder or ("a USB host" if b.usb else "another phone"), "rssi": b.rssi}
                  for b in self.beacons.values()
                  if b.linked and b.id not in self.sessions and P.board_suffix(b.name) not in here
                  and not self._on_usb(b.name) and not (b.holder and b.holder == self.nickname and not b.holder_iphone)]
        with self._lock:
            self._published = out
            self._nearby = sorted(nearby, key=lambda b: b["name"])

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"enabled": self.enabled, "boards": list(self._published), "elsewhere": list(self._nearby),
                    "region": self.region.name}


class UsbBoards:
    """WardriveESP 1.6+ boards on a USB serial port. Every port with an ESP32 serial
    chip that nothing else owns is asked once, when it appears, whether it is a
    WardriveESP board (`wdusb hello` -> `WD:CAPS usb=1`). The reset lines are
    never pulsed, so a board or radio that answers something else is left running."""

    HELLO, PING, BYE = "wdusb hello\n", "wdusb ping\n", "wdusb bye\n"
    PROBE_S = 4.2
    PING_EVERY_S = 15.0
    SILENT_S = 40.0

    def __init__(self, bridge: BoardBridge) -> None:
        self.bridge = bridge
        self.skip: set[str] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._ports: dict[tuple, str] = {}        # port key -> "probing" / "board" / "none" / "busy:<t>" / "off:<sfx>"

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._watch, name="wardriveesp-usb", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _watch(self) -> None:
        try:
            from serial.tools import list_ports
        except ImportError:
            return
        was_enabled = True
        while not self._stop.is_set():
            try:
                enabled = self.bridge.enabled
                with self._lock:
                    if enabled and not was_enabled:
                        self._ports = {k: v for k, v in self._ports.items() if v in ("probing", "board")}
                    was_enabled = enabled
                    present = set()
                    for p in list_ports.comports():
                        vp = (getattr(p, "vid", None), getattr(p, "pid", None))
                        if vp not in USB_IDS or p.device in self.skip:
                            continue
                        key = (p.device, vp, p.serial_number)
                        present.add(key)
                        st = self._ports.get(key)
                        if st and st.startswith("busy:") and time.time() - float(st[5:]) > 30:
                            st = None
                        if st and st.startswith("off:") and st[4:] not in self.bridge.off:
                            st = None
                        if st is not None or not enabled:
                            continue
                        self._ports[key] = "probing"
                        threading.Thread(target=self._port, args=(p.device, vp, key), name=f"wardriveesp-{p.device}",
                                         daemon=True).start()
                    for k in [k for k, v in self._ports.items() if k not in present and v not in ("probing", "board")]:
                        del self._ports[k]
            except Exception:  # noqa: BLE001
                log.debug("WardriveESP USB scan failed", exc_info=True)
            self._stop.wait(4)

    def _set(self, key: tuple, state: str | None) -> None:
        with self._lock:
            if state is None:
                self._ports.pop(key, None)
            else:
                self._ports[key] = state

    def _port(self, device: str, vp: tuple, key: tuple) -> None:
        import serial
        ser = serial.Serial()
        ser.port, ser.baudrate, ser.timeout, ser.write_timeout = device, 115200, 0.15, 2
        ser.dtr = vp in NATIVE_USB          # a UART bridge keeps DTR (GPIO0) released
        ser.rts = False
        try:
            ser.open()
        except Exception as e:  # noqa: BLE001 - in use by another program, unplugged
            log.debug("WardriveESP USB: %s not opened: %s", device, e)
            self._set(key, f"busy:{time.time()}")
            return
        state: str | None = "none"
        try:
            board = self._probe(ser)
            if board is None:
                log.info("WardriveESP USB: %s (%04x:%04x) did not answer as a WardriveESP board", device, *vp)
                return
            log.info("WardriveESP USB: found WardriveESP %s on %s (chip=%s name=%s)", board.get("fw"), device,
                     board.get("chip"), board.get("name"))
            self._set(key, "board")
            state = self._hold(ser, device, board)
        except Exception as e:  # noqa: BLE001 - unplugged mid-session
            log.info("WardriveESP USB: %s closed (%s)", device, e)
            state = None
        finally:
            try:
                ser.close()
            except Exception:  # noqa: BLE001
                pass
            self._set(key, state)

    @staticmethod
    def _lines(buf: bytearray, data: bytes):
        for b in data:
            if b == 0x0A:
                line = buf.decode("utf-8", "replace").strip()
                buf.clear()
                if line:
                    yield line
            elif b != 0x0D:
                if len(buf) < 1024:
                    buf.append(b)
                else:
                    buf.clear()

    @staticmethod
    def _read(ser) -> bytes:
        n = ser.in_waiting
        return ser.read(n if n else 1)

    @staticmethod
    def _caps(line: str) -> dict[str, str] | None:
        at = line.find("WD:CAPS")
        if at < 0:
            return None
        kv = P.key_values(line[at:], "WD:CAPS")
        if kv.get("usb") != "1" or not (kv.get("fw") or "").strip():
            return None
        return kv

    def _probe(self, ser) -> dict[str, str] | None:
        buf = bytearray()
        found = None
        end = time.monotonic() + self.PROBE_S
        next_hello = 0.0
        while found is None and not self._stop.is_set() and time.monotonic() < end:
            now = time.monotonic()
            if now >= next_hello:
                ser.write(self.HELLO.encode())
                next_hello = now + 1.0
            for line in self._lines(buf, self._read(ser)):
                if found is None:
                    found = self._caps(line)
        if found is None:
            return None
        ser.write(b"info\n")
        name = None
        end = time.monotonic() + 2.0
        while name is None and not self._stop.is_set() and time.monotonic() < end:
            for line in self._lines(buf, self._read(ser)):
                if name is None and P.is_board_name(line):
                    name = P.name_in(line)
        ser.write(self.BYE.encode())
        found["name"] = name or ""
        return found

    def _hold(self, ser, device: str, board: dict[str, str]) -> str | None:
        """Hold a session open while the bridge wants this board: ping every 15 s, reopen
        when it goes quiet, `wdusb bye` when it is switched off or the app stops."""
        name = board.get("name") or P.NAME_PREFIX
        sfx = P.board_suffix(name)
        bid = "usb:" + (sfx or device)
        buf = bytearray()
        state = "idle"
        next_hello = last_write = last_proto = 0.0
        link: UsbLink | None = None

        def write(s: str) -> None:
            nonlocal last_write
            ser.write(s.encode("utf-8"))
            last_write = time.monotonic()

        def end_link(why: str) -> None:
            nonlocal link
            if link is not None:
                link.end(why)
                link = None
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                wanted = self.bridge.enabled and (sfx is None or sfx not in self.bridge.off)
                if not wanted:
                    if state != "idle":
                        write(self.BYE)
                    end_link("switched off")
                    log.info("WardriveESP USB: %s released", device)
                    return f"off:{sfx}" if self.bridge.enabled and sfx else None
                if state != "open" and now >= next_hello:
                    state = "opening"
                    write(self.HELLO)
                    next_hello = now + 1.0
                elif state == "open" and now - last_proto > self.SILENT_S:
                    log.info("WardriveESP USB: %s went quiet, reopening", device)
                    end_link("USB went quiet")
                    state = "opening"
                    write(self.HELLO)
                    next_hello = now + 1.0
                elif state == "open" and now - last_write >= self.PING_EVERY_S:
                    write(self.PING)
                if link is not None:
                    while link.out:
                        write(link.out.popleft() + "\n")
                for line in self._lines(buf, self._read(ser)):
                    if state != "open":
                        if state == "opening" and self._caps(line) is not None:
                            state = "open"
                            last_proto = time.monotonic()
                            link = UsbLink(self.bridge, bid, name)
                            self.bridge.attach_usb(link)
                            link.line(line[line.find("WD:CAPS"):])
                        continue
                    if line.startswith("WD:"):
                        last_proto = time.monotonic()
                    if line.startswith("WD:USB bye"):
                        end_link("USB " + line[7:].strip())
                        state = "idle"
                        next_hello = 0.0
                        continue
                    if line.startswith("WD:USB pong"):
                        continue
                    if link is not None:
                        link.line(line)
            if state != "idle":
                try:
                    write(self.BYE)
                except Exception:  # noqa: BLE001
                    pass
            return None
        finally:
            end_link("app closing" if self._stop.is_set() else "unplugged")
