"""The flasher behind the dashboard's "Flash WardriveESP" panel.

It keeps a list of the ESP32 boards on the PC (re-listed whenever the dashboard
asks, which is cheap), finds out what each one runs without resetting it, and runs
one flash job at a time on its own thread. The dashboard polls :meth:`state`.

A job, end to end:

1. The uploader lets go of the board's ports (its mesh capture is paused for them),
   and a port another program holds is reported by name instead of failing later.
2. version.json is read, and the image is downloaded before the board is put in the
   ROM loader whenever its chip is already known.
3. The main chip: connect (C5: at least 10 s after its last ROM session), write the
   merged WardriveESP image at 0x0 with an MD5 check, reset into the firmware. A
   failed session on native USB ends with a plain reset, and the one retry waits the
   10 s and runs at 115200 without the stub, like the Wardrive Go flasher's ROM path.
4. A Biscuit Pro / Ultra (and a published co-processor): the WROOM on its CH340, the
   model read from its boot log first, the merged image over the stock firmware or
   the three parts over our co-processor, a reset, then its hello and
   ``WDCP setboard <model>`` when needed. A retry after the C5 is done only redoes
   the WROOM.
5. ``wdusb hello`` -> ``WD:CAPS`` proves what runs now (version, board, battery gauge,
   second radio), ``WD:HEALTH`` adds the battery level, then ``wdusb bye``.
6. The uploader gets its ports back.
"""

from __future__ import annotations

import collections
import concurrent.futures
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

from . import ports as P
from . import probe as PR
from .engine import EspSession, FlashError
from .firmware import CHIPS, FirmwareError, FirmwareSource, chip_label, version_tuple

log = logging.getLogger("wdgwars.flasher")

C5_GAP = 10.0          # bench rule: about 10 s between ROM sessions on a C5
LIST_EVERY = 2.0
PLAY_URL = "https://play.google.com/store/apps/details?id=com.rocketgod.wardrive"


@dataclass
class BoardInfo:
    device: P.Device
    fw: PR.Firmware | None = None           # what the main chip runs
    wroom: dict | None = None               # Biscuit WROOM boot log result
    wroom_checked: bool = False
    chip: str = ""                          # from a ROM identify
    busy: str = ""                          # "" | "other" | "uploader"
    busy_reason: str = ""
    probing: bool = False
    probed: bool = False
    probed_at: float = 0.0

    @property
    def biscuit(self) -> bool:
        return self.device.pair and self.wroom is not None

    @property
    def main_chip(self) -> str:
        if self.fw and self.fw.chip:
            return self.fw.chip
        if self.chip:
            return self.chip
        if self.biscuit:
            return "ESP32-C5"
        return ""


@dataclass
class Step:
    key: str
    title: str
    stage: str = "waiting"
    fraction: float | None = None
    detail: str = ""

    def as_dict(self) -> dict:
        return {"key": self.key, "title": self.title, "stage": self.stage,
                "fraction": None if self.fraction is None else round(self.fraction, 4),
                "detail": self.detail}


@dataclass
class Job:
    board_id: str
    kind: str = "flash"                     # flash | identify
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    state: str = "running"                  # running | done | failed
    steps: list[Step] = field(default_factory=list)
    log: collections.deque = field(default_factory=lambda: collections.deque(maxlen=300))
    started: float = field(default_factory=time.time)
    finished: float = 0.0
    title: str = ""
    error: str = ""
    hint: str = ""
    result: dict = field(default_factory=dict)
    main_done: bool = False                 # Biscuit: the C5 has WardriveESP, a retry skips it
    wrote: bool = False

    def step(self, key: str) -> Step:
        return next(s for s in self.steps if s.key == key)

    def line(self, text: str) -> None:
        self.log.append(f"{time.strftime('%H:%M:%S')}  {text}")

    def as_dict(self) -> dict:
        return {"id": self.id, "board_id": self.board_id, "kind": self.kind, "state": self.state,
                "title": self.title, "steps": [s.as_dict() for s in self.steps],
                "log": list(self.log)[-80:], "error": self.error, "hint": self.hint,
                "result": self.result, "elapsed": round((self.finished or time.time()) - self.started, 1),
                "can_retry": self.state == "failed"}


class FlasherService:
    def __init__(self, port_guard=None, ports_in_use: Callable[[], dict] | None = None,
                 on_event: Callable[[str], None] | None = None, source: FirmwareSource | None = None,
                 scan: Callable[[], list[P.Port]] | None = None, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time) -> None:
        self.guard = port_guard
        self.ports_in_use = ports_in_use or (lambda: {})
        self.on_event = on_event or (lambda m: None)
        self.source = source or FirmwareSource()
        self._scan = scan or P.scan_ports
        self._sleep = sleep
        self._clock = clock
        self._lock = threading.RLock()
        self._io = threading.Lock()                 # one serial conversation at a time
        self._boards: dict[str, BoardInfo] = {}
        self._unpaired: set[str] = set()            # port keys of pairs that turned out not Biscuits
        self._listed_at = 0.0
        self._queue: collections.deque[str] = collections.deque()
        self._worker: threading.Thread | None = None
        self._job: Job | None = None
        self._job_thread: threading.Thread | None = None
        self._c5_end: dict[str, float] = {}
        self._manifest_error = ""
        self._manifest_thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------ state
    def state(self) -> dict:
        self._ensure_manifest()
        if not self._job_running():
            self._relist()
        with self._lock:
            boards = [self._card(b) for b in self._boards.values()]
            job = self._job.as_dict() if self._job else None
        m = self.source.manifest
        return {"firmware": m.as_dict() if m else None, "firmware_error": self._manifest_error,
                "boards": boards, "scanning": bool(self._queue) or any(b["probing"] for b in boards),
                "job": job, "android_url": PLAY_URL}

    def stop(self) -> None:
        self._stop.set()

    def rescan(self) -> None:
        if self._job_running():
            return
        with self._lock:
            self._boards.clear()
            self._unpaired.clear()
            self._listed_at = 0.0
        threading.Thread(target=self.source.fetch_manifest, kwargs={"force": True}, daemon=True).start()
        self._relist(force=True)

    def dismiss(self) -> None:
        with self._lock:
            if self._job and self._job.state != "running":
                self._job = None
        self._relist(force=True)

    # ------------------------------------------------------------ manifest
    def _ensure_manifest(self) -> None:
        m = self.source.manifest
        if m and time.time() - m.fetched_at < self.source.MANIFEST_TTL:
            return
        if self._manifest_thread and self._manifest_thread.is_alive():
            return

        def get():
            try:
                self.source.fetch_manifest()
                self._manifest_error = ""
            except FirmwareError as e:
                self._manifest_error = str(e)

        self._manifest_thread = threading.Thread(target=get, name="wdgw-flash-manifest", daemon=True)
        self._manifest_thread.start()

    # ------------------------------------------------------------- listing
    def _relist(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._listed_at < LIST_EVERY:
            return
        self._listed_at = now
        try:
            found = self._scan()
        except Exception:  # noqa: BLE001
            log.debug("flasher: port scan failed", exc_info=True)
            return
        devices = P.group([p for p in found])
        # A pair whose CH340 isn't a Biscuit WROOM is two boards.
        split: list[P.Device] = []
        for d in devices:
            if d.pair and any(p.key in self._unpaired for p in d.ports):
                split += [P.Device(ports=[p]) for p in d.ports]
            else:
                split.append(d)
        in_use = self._in_use()
        with self._lock:
            keep: dict[str, BoardInfo] = {}
            for d in split:
                b = self._boards.get(d.id)
                if b is None:
                    b = BoardInfo(device=d)
                    self._queue.append(d.id)
                else:
                    b.device = d          # COM names can change on a replug
                users = [in_use[p.device] for p in d.ports if p.device in in_use]
                # Look again once the uploader or another program lets go of it.
                if not b.probing and d.id not in self._queue and (
                        (b.busy == "uploader" and not users)
                        or (b.busy == "other" and now - b.probed_at > 10)):
                    self._queue.append(d.id)
                b.busy = "uploader" if users else ("" if b.busy == "uploader" else b.busy)
                b.busy_reason = users[0] if users else ("" if b.busy != "other" else b.busy_reason)
                keep[d.id] = b
            self._boards = keep
        self._kick()

    def _in_use(self) -> dict:
        try:
            return dict(self.ports_in_use() or {})
        except Exception:  # noqa: BLE001
            return {}

    def _kick(self) -> None:
        if self._queue and (self._worker is None or not self._worker.is_alive()):
            self._worker = threading.Thread(target=self._probe_loop, name="wdgw-flash-probe", daemon=True)
            self._worker.start()

    def _probe_loop(self) -> None:
        while self._queue and not self._stop.is_set():
            if self._job_running():
                return
            bid = self._queue.popleft()
            with self._lock:
                b = self._boards.get(bid)
            if b is None:
                continue
            b.probing = True
            try:
                with self._io:
                    self._probe(b)
            except Exception:  # noqa: BLE001
                log.debug("flasher: probe failed", exc_info=True)
            finally:
                b.probing = False
                b.probed = True
                b.probed_at = time.time()

    def _probe(self, b: BoardInfo) -> None:
        in_use = self._in_use()          # ports the uploader reads as mesh radios: left alone
        main = b.device.main
        if main.device not in in_use:
            try:
                b.fw = PR.probe_firmware(main.device)
                b.busy = ""
            except PR.PortBusy:
                b.busy, b.busy_reason = "other", "another program"
                return
            except PR.PortGone:
                return
            except Exception as e:  # noqa: BLE001
                log.debug("flasher: probe of %s failed: %s", main.device, e)
                b.fw = PR.Firmware(kind="unknown", detail="Couldn't read it")
        wroom = b.device.wroom
        if wroom is not None and not b.wroom_checked and wroom.device not in in_use:
            try:
                b.wroom = PR.probe_wroom(wroom.device)
            except PR.PortBusy:
                b.busy, b.busy_reason = "other", "another program"
                return
            except Exception:  # noqa: BLE001
                b.wroom = None
            b.wroom_checked = True
            if b.wroom is None:
                with self._lock:
                    self._unpaired.update(p.key for p in b.device.ports)
                self._listed_at = 0.0

    # --------------------------------------------------------------- cards
    def _card(self, b: BoardInfo) -> dict:
        m = self.source.manifest
        latest = m.latest if m else ""
        coproc_pub = m.coproc if m else None
        d = b.device
        main = d.main
        fw = b.fw
        chips = []
        main_chip = b.main_chip
        chips.append({
            "role": "main",
            "chip": main_chip or ("ESP32-C5" if d.pair else ""),
            "port": main.label, "transport": P.TRANSPORT_LABEL.get(main.transport, ""),
            "fw": self._fw_text(b, fw, latest),
        })
        title = ""
        if b.biscuit:
            w = b.wroom or {}
            title = PR.MODEL_NAMES.get(w.get("board", ""), "Biscuit")
            wr = d.wroom
            if w.get("coproc_fw"):
                text = f"Co-processor {w['coproc_fw']}"
                tone = "update" if coproc_pub and version_tuple(w["coproc_fw"]) < version_tuple(coproc_pub) else "ok"
                if w.get("mode"):
                    text += f" · {_mode(w['mode'])}"
            else:
                sv = w.get("stock_version")
                text, tone = (f"Stock Biscuit {sv}" if sv else "Stock Biscuit firmware"), "stock"
            chips.append({"role": "coproc", "chip": "ESP32", "port": wr.label if wr else "",
                          "transport": "CH340", "fw": {"text": text, "tone": tone}})
        elif d.pair:
            wr = d.wroom
            chips.append({"role": "coproc", "chip": "ESP32", "port": wr.label if wr else "",
                          "transport": "CH340", "fw": {"text": "Reading its boot log", "tone": "muted"}})
        if not title:
            if fw and fw.kind == "wardrive" and fw.board_name:
                title = fw.board_name
            elif fw and fw.kind == "biscuit_stock" and fw.board_name:
                title = fw.board_name
            elif d.pair:
                title = "Biscuit Pro / Ultra?"
            else:
                title = main_chip or "ESP32 board"
        action, enabled, why = self._action(b, latest, coproc_pub)
        warn = ""
        if b.busy == "uploader":
            warn = f"This uploader reads {main.label} as a {b.busy_reason}. Flashing pauses it and erases the mesh firmware."
        elif fw and fw.kind == "mesh":
            warn = f"Runs {fw.detail}. Flashing WardriveESP erases it and its settings."
        elif fw and fw.kind == "silent":
            warn = "Says nothing over USB. MeshCore's Bluetooth build looks like this."
        if b.biscuit and not coproc_pub:
            warn = "The co-processor isn't published yet. Only the C5 gets flashed."
        return {
            "id": d.id, "title": title, "biscuit": b.biscuit, "pair": d.pair,
            "ports": [p.label for p in d.ports], "chips": chips,
            "action": action, "can_flash": enabled, "why": why, "warn": warn,
            "needs_confirm": self._needs_confirm(b), "probing": b.probing or not b.probed,
            "busy": b.busy, "winusb": any(p.winusb for p in d.ports),
            "can_identify": not main_chip and not b.busy and b.probed and not d.pair,
            "name": fw.name if fw else "",
        }

    @staticmethod
    def _fw_text(b: BoardInfo, fw: PR.Firmware | None, latest: str) -> dict:
        if b.busy == "uploader":
            return {"text": "Mesh radio in this uploader", "tone": "danger"}
        if b.busy == "other":
            return {"text": "In use by another program", "tone": "warn"}
        if fw is None:
            return {"text": "Checking", "tone": "muted"}
        if fw.kind == "wardrive":
            older = latest and version_tuple(fw.version) < version_tuple(latest)
            return {"text": f"WardriveESP {fw.version}", "tone": "update" if older else "ok"}
        if fw.kind == "mesh":
            return {"text": fw.detail or "Mesh firmware", "tone": "danger"}
        if fw.kind == "biscuit_stock":
            return {"text": "Stock Biscuit firmware", "tone": "stock"}
        if fw.kind == "silent":
            return {"text": "No answer over USB", "tone": "muted"}
        if fw.kind == "other":
            return {"text": "Other firmware", "tone": "muted"}
        return {"text": fw.detail or "Unknown firmware", "tone": "muted"}

    def _action(self, b: BoardInfo, latest: str, coproc: str | None) -> tuple[str, bool, str]:
        if not latest:
            return "Flash", False, "Reading the latest version"
        if b.busy == "other":
            return "Flash", False, f"Close the program using {b.device.main.label}"
        fw = b.fw
        cur = fw.version if fw and fw.kind == "wardrive" else ""
        if b.biscuit and coproc:
            w = b.wroom or {}
            cp = w.get("coproc_fw")
            if cur and cp:
                if version_tuple(cur) < version_tuple(latest) or version_tuple(cp) < version_tuple(coproc):
                    return "Update both chips", True, ""
                return "Reflash both chips", True, ""
            return "Flash both chips", True, ""
        if cur:
            if version_tuple(cur) < version_tuple(latest):
                return f"Update to {latest}", True, ""
            return f"Reflash {latest}", True, ""
        return f"Flash WardriveESP {latest}", True, ""

    @staticmethod
    def _needs_confirm(b: BoardInfo) -> dict | None:
        if b.busy == "uploader" or (b.fw and b.fw.kind == "mesh"):
            what = b.fw.detail if b.fw and b.fw.kind == "mesh" else "MeshCore or Meshtastic"
            return {"title": "Overwrite the mesh firmware?",
                    "lines": [f"This board runs {what}.",
                              "Flashing WardriveESP erases it and its settings.",
                              "Reflash the mesh firmware to go back."]}
        if b.fw and b.fw.kind == "silent":
            return {"title": "Overwrite this board?",
                    "lines": ["It says nothing over USB, which is how MeshCore's Bluetooth build looks.",
                              "Flashing WardriveESP erases whatever it runs."]}
        return None

    # ---------------------------------------------------------------- jobs
    def _job_running(self) -> bool:
        return self._job is not None and self._job.state == "running"

    def flash(self, board_id: str, confirm: bool = False) -> dict:
        return self._start(board_id, "flash", confirm)

    def identify(self, board_id: str) -> dict:
        return self._start(board_id, "identify", True)

    def retry(self) -> dict:
        with self._lock:
            j = self._job
        if not j or j.state != "failed":
            return {"ok": False, "error": "nothing to retry"}
        return self._start(j.board_id, j.kind, True, resume=j if j.main_done else None)

    def _start(self, board_id: str, kind: str, confirm: bool, resume: Job | None = None) -> dict:
        with self._lock:
            if self._job_running():
                return {"ok": False, "error": "A flash is already running."}
            b = self._boards.get(board_id)
            if b is None:
                return {"ok": False, "error": "That board isn't plugged in any more."}
            if kind == "flash" and not confirm:
                c = self._needs_confirm(b)
                if c:
                    return {"ok": False, "confirm": c}
            m = self.source.manifest
            job = Job(board_id=board_id, kind=kind)
            biscuit = b.biscuit and bool(m and m.coproc) and kind == "flash"
            main_title = b.main_chip or "Board"
            if biscuit:
                job.title = PR.MODEL_NAMES.get((b.wroom or {}).get("board", ""), "Biscuit")
                job.steps = [Step("main", f"{main_title} · WardriveESP"), Step("coproc", "ESP32 · co-processor")]
            else:
                job.title = (b.fw.board_name if b.fw and b.fw.board_name else "") or main_title
                job.steps = [Step("main", f"{main_title} · WardriveESP" if kind == "flash" else main_title)]
            if resume:
                job.main_done = True
                job.result = dict(resume.result)
                job.step("main").stage, job.step("main").fraction = "done", 1.0
                job.started = time.time()
            self._job = job
        self._job_thread = threading.Thread(target=self._run, args=(job, b), name="wdgw-flash-job", daemon=True)
        self._job_thread.start()
        return {"ok": True, "job": job.id}

    def _run(self, job: Job, b: BoardInfo) -> None:
        devices = [p.device for p in b.device.ports]
        paused: list[str] = []
        try:
            with self._io:
                paused = self._take_ports(job, b, devices)
                if job.kind == "identify":
                    self._identify(job, b)
                else:
                    self._flash(job, b)
                    if paused:
                        mesh = (f"{', '.join(paused)} is still set up as a mesh radio in Settings. "
                                "Remove it there so the uploader leaves this board alone.")
                        job.result["note"] = f"{job.result['note']} {mesh}" if job.result.get("note") else mesh
            job.state = "done"
        except FlashError as e:
            job.state, job.error, job.hint = "failed", str(e), e.hint
            job.line(f"Failed: {e}")
            for s in job.steps:
                if s.stage not in ("done", "waiting", "skipped"):
                    s.stage = "failed"
            log.info("flasher: job failed: %s", e)
        except FirmwareError as e:
            job.state, job.error = "failed", str(e)
            job.line(f"Failed: {e}")
            for s in job.steps:
                if s.stage not in ("done", "waiting", "skipped"):
                    s.stage = "failed"
        except Exception as e:  # noqa: BLE001
            log.exception("flasher: job crashed")
            job.state, job.error = "failed", "Something went wrong. Try again."
            job.line(f"Failed: {e}")
        finally:
            job.finished = time.time()
            if paused and self.guard is not None:
                try:
                    self.guard.resume_ports(paused)
                except Exception:  # noqa: BLE001
                    log.debug("flasher: resume failed", exc_info=True)
            with self._lock:
                # What runs on it changed: look again on the next listing.
                if job.kind == "flash" and job.state == "done":
                    self._boards.pop(b.device.id, None)
                elif job.state == "done" and job.kind == "identify":
                    pass
            self._listed_at = 0.0
            if job.state == "done" and job.kind == "flash":
                r = job.result
                self.on_event(f"Flashed WardriveESP {r.get('version') or ''} onto "
                              f"{r.get('board_name') or job.title}".strip())

    def _take_ports(self, job: Job, b: BoardInfo, devices: list[str]) -> list[str]:
        """Pause the uploader's own use of these ports, then make sure they open."""
        paused: list[str] = []
        users = {d: r for d, r in self._in_use().items() if d in devices}
        if users and self.guard is not None:
            job.line(f"Pausing the uploader on {', '.join(users)}")
            try:
                paused = list(self.guard.pause_ports(list(users)) or [])
            except Exception:  # noqa: BLE001
                log.debug("flasher: pause failed", exc_info=True)
        for d in devices:
            ok = None
            for _ in range(12 if users else 1):
                ok = PR.port_free(d)
                if ok is not False:
                    break
                self._sleep(0.5)
            if ok is False:
                name = "the board's USB port" if d.startswith("usbjtag://") else d
                if d in users:
                    raise FlashError(f"The uploader is still letting go of {name}.", "Give it a few seconds and try again.")
                raise FlashError(f"Another program has {name} open.",
                                 "Close Arduino IDE, a serial monitor, Wardrive tools or any other flasher, then try again.")
            if ok is None:
                raise FlashError("The board isn't plugged in any more.", "Plug it back in and press Rescan.")
        return paused

    # -- the C5 gap ----
    def _gap(self, job: Job, step: Step, port: P.Port) -> None:
        if not port.native:
            return
        left = self._c5_end.get(port.key, 0.0) + C5_GAP - self._clock()
        if left <= 0:
            return
        step.stage = "settling"
        job.line(f"Giving the chip {left:.0f} s to settle before the next session")
        while left > 0:
            step.detail = f"Letting it settle {left:.0f} s"
            self._sleep(min(0.5, left))
            left = self._c5_end.get(port.key, 0.0) + C5_GAP - self._clock()
        step.detail = ""

    def _session_end(self, port: P.Port) -> None:
        if port.native:
            self._c5_end[port.key] = self._clock()

    # -- identify ----
    def _identify(self, job: Job, b: BoardInfo) -> None:
        step = job.step("main")
        port = b.device.main
        self._gap(job, step, port)
        step.stage = "connecting"
        try:
            with EspSession(port.device, port.transport, classic=False, on_line=job.line) as s:
                try:
                    chip = s.connect()
                except FlashError:
                    if s.chip:            # it answered, just not a WardriveESP chip
                        b.chip = s.chip
                        s.restart()
                    raise
                b.chip = chip
                job.result = {"chip": chip, "mac": s.info.get("mac", ""), "description": s.info.get("description", "")}
                step.stage = "restarting"
                s.restart()
        finally:
            self._session_end(port)
            if port.native and not b.chip:
                PR.plain_reset(port.device)
        step.stage, step.fraction = "done", 1.0

    # -- flash ----
    def _flash(self, job: Job, b: BoardInfo) -> None:
        m = self.source.fetch_manifest()
        biscuit = any(s.key == "coproc" for s in job.steps)
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="wdgw-flash-dl")
        try:
            known = b.main_chip if b.main_chip in CHIPS else ""
            bridge_f = pool.submit(self.source.bridge, known, m.latest) if known and not job.main_done else None
            coproc_f = None
            if biscuit:
                w = b.wroom or {}
                wipe = not w.get("coproc_fw")
                coproc_f = pool.submit(self.source.coproc, m.coproc, wipe, m.coproc_parts)
            job.result.setdefault("latest", m.latest)
            if not job.main_done:
                self._flash_main(job, b, m.latest, bridge_f, known)
            if biscuit:
                self._flash_coproc(job, b, m.coproc, coproc_f)
            self._verify(job, b, m.latest, biscuit)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    def _flash_main(self, job: Job, b: BoardInfo, latest: str, bridge_f, known: str) -> None:
        step = job.step("main")
        port = b.device.main
        for attempt in (0, 1):
            self._gap(job, step, port)
            step.stage, step.fraction, step.detail = "connecting", None, ""
            job.line(f"Connecting to {port.label}" + (" (115200 baud, no stub)" if attempt else ""))
            try:
                with EspSession(port.device, port.transport, fast=attempt == 0,
                                stub=attempt == 0, on_line=job.line) as s:
                    chip = s.connect()
                    job.result.update({"chip": chip, "mac": s.info.get("mac", "")})
                    step.title = f"{chip} · WardriveESP"
                    step.stage = "downloading"

                    def dl(p):
                        step.fraction = p

                    if bridge_f is not None and chip == known:
                        data = bridge_f.result()
                    else:
                        data = self.source.bridge(chip, latest, dl)
                    job.line(f"WardriveESP {latest} for {chip}: {len(data) // 1024} KB")

                    def prog(stage, f):
                        step.stage = stage
                        if f is not None:
                            step.fraction = f

                    job.wrote = True
                    md5 = s.flash([(0x0, data)], prog)
                    job.result.setdefault("md5", []).extend(md5)
                    step.stage, step.fraction = "restarting", 1.0
                    s.restart()
                self._session_end(port)
                break
            except FlashError as e:
                self._session_end(port)
                if port.native:
                    # Bench rule: a failed C5 session ends with a plain reset, never a straight retry.
                    PR.plain_reset(port.device)
                    self._session_end(port)
                if attempt == 0 and e.retry_slow:
                    job.line(f"{e} Trying again more slowly.")
                    continue
                raise
        job.main_done = True
        step.stage, step.fraction = "done", 1.0
        job.line("WardriveESP written and verified")

    def _flash_coproc(self, job: Job, b: BoardInfo, version: str, coproc_f) -> None:
        step = job.step("coproc")
        wr = b.device.wroom
        if wr is None:
            raise FlashError("The Biscuit's second chip is gone.", "Replug the board and try again.")
        w = b.wroom or {}
        board = w.get("board") or "biscuit_dual"
        wipe = not w.get("coproc_fw")
        step.stage = "downloading"
        regions = coproc_f.result() if coproc_f is not None else self.source.coproc(version, wipe, None)
        size = sum(len(d) for _, d in regions)
        job.line(f"Co-processor {version}: {size // 1024} KB, " +
                 ("full image over the stock firmware" if wipe else "keeps its settings"))
        for attempt in (0, 1):
            step.stage, step.fraction = "connecting", None
            try:
                with EspSession(wr.device, "ch34x", classic=True, fast=attempt == 0,
                                stub=attempt == 0, on_line=job.line) as s:
                    s.connect()

                    def prog(stage, f):
                        step.stage = stage
                        if f is not None:
                            step.fraction = f

                    md5 = s.flash(regions, prog)
                    job.result.setdefault("md5", []).extend(md5)
                    step.stage, step.fraction = "restarting", 1.0
                    s.restart()
                break
            except FlashError as e:
                PR.plain_reset(wr.device, native=False)
                if attempt == 0 and e.retry_slow:
                    job.line(f"{e} Retrying at 115200.")
                    continue
                raise FlashError(f"C5 done. Co-processor: {e}", e.hint) from None
        step.stage, step.fraction = "checking", None
        hello = PR.finish_coproc(wr.device, board)
        if hello:
            job.result["coproc_fw"] = hello.get("fw", "")
            job.result["coproc_board"] = hello.get("board", "")
            job.result["coproc_mode"] = hello.get("mode", "")
            job.line(f"Co-processor {hello.get('fw')} running, board {PR.MODEL_NAMES.get(hello.get('board', ''), '?')}")
        else:
            job.result["coproc_fw"] = version
            job.result["coproc_silent"] = True
            job.line("The co-processor didn't say hello yet")
        step.stage, step.fraction = "done", 1.0

    def _verify(self, job: Job, b: BoardInfo, latest: str, biscuit: bool) -> None:
        step = job.step("main")
        port = b.device.main
        step.stage = "checking"
        job.line("Checking what the board runs now (wdusb hello)")
        res = PR.verify_wardrive(port.device, wait=20.0, battery_wait=17.0 if biscuit else 0.0)
        if res is None and port.native:
            job.line("No answer yet. Plain reset and one more look")
            PR.plain_reset(port.device)
            res = PR.verify_wardrive(port.device, wait=12.0, battery_wait=17.0 if biscuit else 0.0)
        if biscuit and res and not str(res["caps"].get("board", "")).startswith("biscuit"):
            self._sleep(4.0)       # the C5 answers the co-processor's hello within a few seconds
            res = PR.verify_wardrive(port.device, wait=10.0, battery_wait=17.0) or res
        r = job.result
        if res:
            caps, health = res["caps"], res.get("health") or {}
            r.update({"verified": True, "version": caps.get("fw", ""), "chip": chip_label(caps.get("chip")) or r.get("chip", ""),
                      "board": caps.get("board", ""),
                      "board_name": (caps.get("board_name") or "").replace("_", " ") or PR.MODEL_NAMES.get(caps.get("board", ""), ""),
                      "gauge": caps.get("batt") == "1", "second_radio": caps.get("coproc", ""),
                      "bands": caps.get("bands", "")})
            if health.get("batt"):
                r["battery"] = health.get("batt")
                r["battery_mv"] = health.get("batt_mv", "")
            if latest and r["version"] and r["version"] != latest:
                r["warning"] = f"It reports {r['version']}, not {latest}. Try flashing again."
            job.line(f"Verified: WardriveESP {r['version']} on {r['chip']}" +
                     (f", {r['board_name']}" if r.get("board_name") else "") +
                     (f", battery {r['battery']}%" if r.get("battery") else "") +
                     (f", second radio {r['second_radio']}" if r.get("second_radio") else ""))
        else:
            r.update({"verified": False, "version": latest})
            r["note"] = "It didn't answer over USB yet. That's OK if it links in the app."
            job.line(r["note"])
        if not biscuit:
            step.stage, step.fraction = "done", 1.0
        else:
            step.stage = "done"


def _mode(m: str) -> str:
    return {"wifi": "Wi-Fi 2.4 GHz", "btc": "BT Classic", "off": "radio off"}.get(m, m)
