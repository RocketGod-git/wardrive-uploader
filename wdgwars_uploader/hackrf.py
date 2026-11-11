"""HackRF One / HackRF Pro as an ADS-B receiver, decoded on the PC by dump1090.

A HackRF cannot decode aircraft by itself, and the dump1090 build this app uses
only drives RTL-SDRs. So the HackRF is run here, in user space over libusb, and
its samples are handed to dump1090 through dump1090's own ``rtl_tcp`` network
input on a localhost port. Everything after that (decoding, positions,
aircraft.json, uploads) is the same path an RTL-SDR takes.

The radio side is ported from the HackRF driver in Wardrive Go and Mayhem Mobile
(``HackRfOne.kt``), which is field-proven on the One and the Pro:

* Same vendor protocol as libhackrf. The HackRF Pro ("Praline", board id 5)
  shares the One's USB id and command set, so one driver covers both.
* Mayhem's proven ADS-B gain: LNA 32 dB, VGA 32 dB, RF amp on. Max gain buries
  the pulses in noise, and the amp off loses about 14 dB.
* The Pro's quarter-shift fix: after entering RX the tuner is set to an
  off-target frequency and then the real one, so the firmware re-runs its tuning
  and re-programs the FPGA rotation. Without it a Pro streams nothing useful.
* 2 Msps, the rate both apps use on the HackRF.

A PortaPack running Mayhem shows up as a USB serial console, not as a HackRF.
:func:`portapack_to_hackrf` types ``hackrf`` into that console, as Mayhem Mobile
does, and the board reboots into HackRF mode a few seconds later. The HackRF
firmware asks Windows for WinUSB itself, so no driver install is needed.
"""

from __future__ import annotations

import logging
import queue
import socket
import struct
import threading
import time
from typing import Any

log = logging.getLogger("wdgwars.hackrf")

VID = 0x1D50
# HackRF One / One r9 / Pro = 0x6089, Jawbreaker = 0x604B, rad1o = 0xCC15.
PIDS = (0x6089, 0x604B, 0xCC15)
PID_MAYHEM_SERIAL = 0x6018

# Where dump1090 connects for the samples (rtl_tcp protocol). Not 1234, the
# usual rtl_tcp port, so a real rtl_tcp server on this PC is left alone.
TCP_PORT = 31234
SAMPLE_RATE = 2_000_000
ADSB_FREQ = 1_090_000_000
LNA_DB, VGA_DB, AMP_ON = 32, 32, True

# libhackrf vendor requests
_REQ_TRANSCEIVER_MODE = 1
_REQ_SAMPLE_RATE = 6
_REQ_BB_FILTER = 7
_REQ_BOARD_ID = 14
_REQ_VERSION = 15
_REQ_SET_FREQ = 16
_REQ_AMP = 17
_REQ_LNA = 19
_REQ_VGA = 20
_REQ_HW_SYNC = 29
_MODE_OFF, _MODE_RX = 0, 1
_IN, _OUT = 0xC0, 0x40
_CTRL_MS = 300
_EP_RX = 0x81
_NBUF, _BUFSZ = 12, 16384
_BB_FILTERS = (1_750_000, 2_500_000, 3_500_000, 5_000_000, 5_500_000, 6_000_000, 7_000_000,
               8_000_000, 9_000_000, 10_000_000, 12_000_000, 14_000_000, 15_000_000,
               20_000_000, 24_000_000, 28_000_000)
_BOARDS = {0: "Jellybean", 1: "Jawbreaker", 2: "HackRF One", 3: "rad1o", 4: "HackRF One r9",
           5: "HackRF Pro"}
# HackRF samples are signed 8-bit, rtl_tcp's are offset-128 unsigned: flip the top bit.
_TO_U8 = bytes(b ^ 0x80 for b in range(256))

# rtl_tcp: "RTL0", tuner type (5 = R820T), gain count (29 = R820T table)
_RTL_HEADER = struct.pack(">4sII", b"RTL0", 5, 29)
_RTL_SET_FREQ, _RTL_SET_RATE = 0x01, 0x02


def bb_filter_for(rate: int) -> int:
    """Largest baseband filter strictly below the sample rate (libhackrf's rule)."""
    chosen = _BB_FILTERS[0]
    for bw in _BB_FILTERS:
        if bw < rate:
            chosen = bw
        else:
            break
    return chosen


def board_name(board_id: int) -> str:
    return _BOARDS.get(board_id, "HackRF")


def find_usb() -> tuple[int, int] | None:
    """(vid, pid) of a HackRF on USB, or None. Enumeration only: nothing is opened."""
    try:
        import usb1
        with usb1.USBContext() as ctx:
            for d in ctx.getDeviceIterator(skip_on_error=True):
                if d.getVendorID() == VID and d.getProductID() in PIDS:
                    return VID, d.getProductID()
    except Exception as e:  # noqa: BLE001
        log.debug("HackRF scan failed: %s", e)
    return None


def portapack_to_hackrf(port: str) -> bool:
    """Switch a PortaPack running Mayhem into HackRF mode over its serial console.

    Sends ``hackrf`` once, the way Mayhem Mobile and the Mayhem web app do (115200
    8N1, DTR and RTS up). The board reboots and comes back as a plain HackRF. The
    shell can wedge, so this does not retry.
    """
    try:
        import serial
        with serial.Serial(port, 115200, timeout=1, write_timeout=2) as s:
            s.dtr = True
            s.rts = True
            time.sleep(0.15)
            s.reset_input_buffer()
            s.write(b"\r\n")
            time.sleep(0.15)
            s.write(b"hackrf\r\n")
            s.flush()
        log.info("PortaPack on %s: sent \"hackrf\", it is rebooting into HackRF mode", port)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("PortaPack on %s: could not switch it to HackRF mode: %s", port, e)
        return False


class Bridge:
    """Runs the HackRF and serves its samples to dump1090 on localhost."""

    def __init__(self, port: int = TCP_PORT) -> None:
        self.port = port
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._q: queue.Queue[bytes] = queue.Queue(maxsize=64)
        self._client: socket.socket | None = None
        self._client_lock = threading.Lock()
        self._retune: dict[str, int] = {}
        self._lock = threading.Lock()
        self.status: dict[str, Any] = {"state": "starting", "board": "", "firmware": "",
                                       "client": False, "msps": 0.0, "dropped": 0,
                                       "no_signal": False, "error": ""}

    # ---- lifecycle ----
    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._threads = [threading.Thread(target=self._radio_loop, name="hackrf-radio", daemon=True),
                         threading.Thread(target=self._serve, name="hackrf-tcp", daemon=True),
                         threading.Thread(target=self._send_loop, name="hackrf-send", daemon=True)]
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        self._stop.set()
        self._drop_client()
        for t in self._threads:
            t.join(timeout=3)
        self._threads = []

    @property
    def running(self) -> bool:
        return any(t.is_alive() for t in self._threads)

    def _set(self, **kv: Any) -> None:
        with self._lock:
            self.status.update(kv)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self.status)

    # ---- rtl_tcp side (dump1090 connects here) ----
    def _serve(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(("127.0.0.1", self.port))
        except OSError as e:
            self._set(state="error", error=f"port {self.port} is in use: {e}")
            log.warning("HackRF bridge: cannot listen on %d: %s", self.port, e)
            return
        srv.listen(1)
        srv.settimeout(0.5)
        try:
            while not self._stop.is_set():
                try:
                    c, _ = srv.accept()
                except socket.timeout:
                    continue
                c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self._drop_client()
                try:
                    c.sendall(_RTL_HEADER)
                except OSError:
                    c.close()
                    continue
                with self._client_lock:
                    self._client = c
                self._set(client=True)
                log.info("HackRF bridge: decoder connected")
                threading.Thread(target=self._read_cmds, args=(c,), name="hackrf-cmd",
                                 daemon=True).start()
        finally:
            srv.close()

    def _read_cmds(self, c: socket.socket) -> None:
        """dump1090 asks for a frequency and a sample rate. Gain requests are for
        an R820T and are ignored: the HackRF keeps Mayhem's ADS-B gain."""
        buf = b""
        while not self._stop.is_set():
            try:
                chunk = c.recv(64)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while len(buf) >= 5:
                cmd, param = struct.unpack(">BI", buf[:5])
                buf = buf[5:]
                if cmd == _RTL_SET_FREQ:
                    self._retune["freq"] = param
                elif cmd == _RTL_SET_RATE:
                    self._retune["rate"] = param
        with self._client_lock:
            if self._client is c:
                self._client = None
                self._set(client=False)
        try:
            c.close()
        except OSError:
            pass

    def _drop_client(self) -> None:
        with self._client_lock:
            c, self._client = self._client, None
        if c is not None:
            try:
                c.close()
            except OSError:
                pass
        self._set(client=False)

    def _send_loop(self) -> None:
        while not self._stop.is_set():
            try:
                data = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            with self._client_lock:
                c = self._client
            if c is None:
                continue
            try:
                c.sendall(data)
            except OSError:
                self._drop_client()

    def _feed(self, raw: bytes) -> None:
        with self._client_lock:
            has_client = self._client is not None
        if not has_client:
            return
        try:
            self._q.put_nowait(raw.translate(_TO_U8))
        except queue.Full:
            with self._lock:
                self.status["dropped"] += 1

    # ---- radio side ----
    def _radio_loop(self) -> None:
        import usb1
        while not self._stop.is_set():
            try:
                with usb1.USBContext() as ctx:
                    h = None
                    for pid in PIDS:
                        h = ctx.openByVendorIDAndProductID(VID, pid, skip_on_error=True)
                        if h is not None:
                            break
                    if h is None:
                        self._set(state="waiting", error="")
                        self._stop.wait(2.0)
                        continue
                    try:
                        self._run(ctx, h)
                    finally:
                        try:
                            h.close()
                        except Exception:  # noqa: BLE001
                            pass
            except Exception as e:  # noqa: BLE001
                self._set(state="error", error=str(e))
                log.warning("HackRF: %s", e)
            self._set(state="waiting", msps=0.0)
            self._stop.wait(2.0)

    def _run(self, ctx: Any, h: Any) -> None:
        import usb1
        h.claimInterface(0)
        board = board_name(self._read_byte(h, _REQ_BOARD_ID))
        try:
            fw = bytes(h.controlRead(_IN, _REQ_VERSION, 0, 0, 255, _CTRL_MS)).decode("ascii", "replace").strip("\x00 ")
        except Exception:  # noqa: BLE001
            fw = ""
        self._set(board=board, firmware=fw, error="", no_signal=False)
        log.info("HackRF: %s, firmware %s", board, fw or "?")
        freq, rate = ADSB_FREQ, SAMPLE_RATE
        self._bring_up(h, freq, rate)
        self._set(state="streaming")

        got = {"bytes": 0, "probe": 0, "nonzero": 0, "t": time.time()}
        alive = {"n": 0}

        def done(t: Any) -> None:
            alive["n"] -= 1
            st = t.getStatus()
            if st == usb1.TRANSFER_COMPLETED:
                n = t.getActualLength()
                if n:
                    raw = bytes(t.getBuffer()[:n])
                    got["bytes"] += n
                    if not got["probe"]:      # one buffer per window is enough
                        got["probe"], got["nonzero"] = n, n - raw.count(0)
                    self._feed(raw)
            if st in (usb1.TRANSFER_COMPLETED, usb1.TRANSFER_TIMED_OUT) and not self._stop.is_set():
                try:
                    t.submit()
                    alive["n"] += 1
                except usb1.USBError:
                    pass

        xfers = []
        for _ in range(_NBUF):
            t = h.getTransfer()
            t.setBulk(_EP_RX, _BUFSZ, callback=done, timeout=1000)
            t.submit()
            alive["n"] += 1
            xfers.append(t)
        try:
            while not self._stop.is_set() and alive["n"] > 0:
                ctx.handleEventsTimeout(0.1)
                if self._retune:
                    want = dict(self._retune)
                    self._retune.clear()
                    rate2 = int(want.get("rate", rate))
                    freq2 = int(want.get("freq", freq))
                    if rate2 != rate:
                        rate = rate2
                        self._ctrl(h, _REQ_SAMPLE_RATE, data=struct.pack("<II", rate, 1))
                        self._set_bb(h, bb_filter_for(rate))
                    if freq2 != freq or "rate" in want:
                        freq = freq2
                        self._set_freq(h, freq + 1_000_000)   # Pro: re-run tuning after RX
                        self._set_freq(h, freq)
                    log.info("HackRF: %.3f MHz at %.2f Msps", freq / 1e6, rate / 1e6)
                now = time.time()
                if now - got["t"] >= 2.0:
                    msps = got["bytes"] / 2 / (now - got["t"]) / 1e6
                    # Fewer than 1 in 50 samples non-zero: the front end delivers nothing.
                    no_sig = got["probe"] > 0 and got["nonzero"] * 50 < got["probe"]
                    self._set(msps=round(msps, 2), no_signal=no_sig)
                    got.update(bytes=0, probe=0, nonzero=0, t=now)
            if alive["n"] <= 0 and not self._stop.is_set():
                raise RuntimeError("the HackRF stopped sending samples (unplugged?)")
        finally:
            for t in xfers:
                try:
                    t.cancel()
                except usb1.USBError:
                    pass
            end = time.time() + 1.0
            while alive["n"] > 0 and time.time() < end:
                try:
                    ctx.handleEventsTimeout(0.05)
                except usb1.USBError:
                    break
            try:
                self._ctrl(h, _REQ_TRANSCEIVER_MODE, value=_MODE_OFF)
                self._ctrl(h, _REQ_AMP, value=0)
            except Exception:  # noqa: BLE001
                pass
            try:
                h.releaseInterface(0)
            except Exception:  # noqa: BLE001
                pass

    def _bring_up(self, h: Any, freq: int, rate: int) -> None:
        # Clean slate first: a HackRF left in RECEIVE ignores a second RECEIVE and
        # streams zeros, and a leftover hardware-sync mode waits for a trigger.
        self._ctrl(h, _REQ_TRANSCEIVER_MODE, value=_MODE_OFF)
        time.sleep(0.1)
        self._ctrl(h, _REQ_HW_SYNC, value=0)
        self._ctrl(h, _REQ_SAMPLE_RATE, data=struct.pack("<II", rate, 1))
        self._set_bb(h, bb_filter_for(rate))
        self._set_freq(h, freq)
        self._gain(h)
        self._ctrl(h, _REQ_TRANSCEIVER_MODE, value=_MODE_RX)
        time.sleep(0.04)
        # The Pro tunes off-center and its FPGA rotates the signal back. Entering RX
        # clears that rotation and only a frequency change re-applies it, and the
        # firmware skips an unchanged frequency, so step off and back. Gains are set
        # again because entering RX can reset them on the Pro. Harmless on a One.
        self._set_freq(h, freq + 1_000_000)
        self._set_freq(h, freq)
        self._gain(h)
        time.sleep(0.06)

    def _gain(self, h: Any) -> None:
        self._ctrl(h, _REQ_AMP, value=1 if AMP_ON else 0)
        h.controlRead(_IN, _REQ_LNA, 0, LNA_DB & ~0x07, 1, _CTRL_MS)
        h.controlRead(_IN, _REQ_VGA, 0, VGA_DB & ~0x01, 1, _CTRL_MS)

    def _set_freq(self, h: Any, hz: int) -> None:
        mhz = hz // 1_000_000
        self._ctrl(h, _REQ_SET_FREQ, data=struct.pack("<II", mhz, hz - mhz * 1_000_000))

    def _set_bb(self, h: Any, bw: int) -> None:
        self._ctrl(h, _REQ_BB_FILTER, value=bw & 0xFFFF, index=(bw >> 16) & 0xFFFF)

    @staticmethod
    def _ctrl(h: Any, req: int, value: int = 0, index: int = 0, data: bytes = b"") -> None:
        h.controlWrite(_OUT, req, value, index, data, _CTRL_MS)

    @staticmethod
    def _read_byte(h: Any, req: int) -> int:
        try:
            b = h.controlRead(_IN, req, 0, 0, 1, _CTRL_MS)
            return b[0] if b else -1
        except Exception:  # noqa: BLE001
            return -1


_bridge: Bridge | None = None


def ensure_bridge() -> Bridge:
    global _bridge
    if _bridge is None:
        _bridge = Bridge()
    if not _bridge.running:
        _bridge.start()
    return _bridge


def stop_bridge() -> None:
    if _bridge is not None and _bridge.running:
        _bridge.stop()


def bridge_status() -> dict[str, Any] | None:
    return _bridge.snapshot() if _bridge is not None and _bridge.running else None
