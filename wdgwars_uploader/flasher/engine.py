"""ROM loader sessions through the esptool package.

One :class:`EspSession` is one connection to one chip's ROM loader: reset into it,
identify the chip, refuse what WardriveESP can't run on, load the stub, raise the
baud on UART bridges, write regions with an MD5 check of each, and reset back into
the firmware.

Rules carried over from the Wardrive Go flasher and the Biscuit bench:

* Native USB (303a:1001) resets with the USB-Serial-JTAG sequence, UART bridges with
  the classic DTR/RTS auto-reset. A WinUSB port (``usbjtag://``) is told it is native
  USB, since esptool can't read a VID/PID from a URL.
* WardriveESP chips are C3, C5, C6 and S3. A classic ESP32 is only ever opened on
  purpose as a Biscuit's WROOM. Secure boot, secure download mode and flash
  encryption are refused.
* CP210x boards and the Biscuit's CH340 run at 460800 baud. The caller retries at
  115200 when a fast session fails.
* Nothing outside the written regions is erased, so a co-processor update leaves NVS
  alone.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from typing import Callable

log = logging.getLogger("wdgwars.flasher")

WARDRIVE_CHIPS = ("ESP32-C3", "ESP32-C5", "ESP32-C6", "ESP32-S3")
FAST_BAUD = 460800


class FlashError(Exception):
    """A failure with a short, one-line, user-facing message."""

    def __init__(self, message: str, hint: str = "", retry_slow: bool = False) -> None:
        super().__init__(message)
        self.hint = hint
        self.retry_slow = retry_slow


# -- esptool output -> our progress ---------------------------------------------

_sink_lock = threading.Lock()
_sink: dict = {"line": None, "progress": None}
_installed = False


def _install_logger() -> None:
    """Route esptool's console output to the running session (one at a time)."""
    global _installed
    if _installed:
        return
    from esptool.logger import TemplateLogger, log as esplog

    class _UiLogger(TemplateLogger):
        def print(self, *args, **kwargs):
            text = (kwargs.get("sep") or " ").join(str(a) for a in args).replace("\r", "").strip()
            fn = _sink.get("line")
            if text and fn:
                fn(text)

        def note(self, message):
            self.print(f"Note: {message}")

        def warning(self, message):
            self.print(f"Warning: {message}")

        def error(self, message):
            self.print(f"Error: {message}")

        def stage(self, finish=False):
            pass

        def progress_bar(self, cur_iter, total_iters, prefix="", suffix="", bar_length=30):
            fn = _sink.get("progress")
            if fn and total_iters:
                fn(cur_iter / float(total_iters))

        def set_verbosity(self, verbosity):
            pass

    esplog.set_logger(_UiLogger())
    _patch_usbjtag_vid_pid()
    _installed = True


def _patch_usbjtag_vid_pid() -> None:
    """esptool reads the USB id from the COM port list, which has no ``usbjtag://``
    ports. Report 303a:1001 for them so esptool uses the USB-Serial-JTAG reset and
    watchdog handling."""
    from esptool.loader import ESPLoader
    orig = ESPLoader.get_usb_vid_pid
    if getattr(orig, "_usbjtag", False):
        return

    def get_usb_vid_pid(self):
        port = getattr(getattr(self, "_port", None), "port", "") or ""
        if str(port).lower().startswith("usbjtag://"):
            return 0x303A, 0x1001
        return orig(self)

    get_usb_vid_pid._usbjtag = True  # type: ignore[attr-defined]
    ESPLoader.get_usb_vid_pid = get_usb_vid_pid


def _clean(e: BaseException) -> str:
    return " ".join(str(e).split())[:200]


class EspSession:
    """One ROM loader session on one port."""

    def __init__(self, device: str, transport: str, classic: bool = False, fast: bool = True,
                 stub: bool = True, on_line: Callable[[str], None] | None = None) -> None:
        self.device = device
        self.transport = transport
        self.native = transport == "usb_jtag"
        self.classic = classic
        self.fast = fast and (transport == "cp210x" or (transport == "ch34x" and classic))
        self.stub = stub
        self.on_line = on_line
        self.esp = None
        self.chip = ""
        self.info: dict = {}

    # -- lifecycle ----
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self) -> None:
        esp, self.esp = self.esp, None
        if esp is not None:
            try:
                esp._port.close()
            except Exception:  # noqa: BLE001
                pass
        with _sink_lock:
            _sink["line"] = _sink["progress"] = None

    def _bind(self, progress: Callable[[float], None] | None = None) -> None:
        with _sink_lock:
            _sink["line"] = self.on_line
            _sink["progress"] = progress

    # -- connect + identify ----
    def connect(self, attempts: int = 4) -> str:
        _install_logger()
        from esptool.cmds import detect_chip, run_stub
        from esptool.util import FatalError
        self._bind()
        mode = "usb-reset" if self.native else "default-reset"
        try:
            esp = detect_chip(self.device, 115200, mode, connect_attempts=attempts)
        except FatalError as e:
            msg = str(e).lower()
            if "filenotfound" in msg or "cannot find" in msg or "not found" in msg:
                n = self._name()
                raise FlashError(f"{n[:1].upper() + n[1:]} isn't there any more.",
                                 "Plug the board back in and press Rescan.") from None
            if "access is denied" in msg or "busy" in msg or "permission" in msg:
                raise FlashError(f"Another program has {self._name()} open.",
                                 "Close Arduino IDE, a serial monitor or any other flasher, then try again.") from None
            if "unexpected chip" in msg or "autodetect" in msg:
                raise FlashError("This chip isn't one esptool knows.", "WardriveESP needs a C3, C5, C6 or S3.") from None
            raise FlashError("No answer from the board.",
                             "Hold BOOT, tap RESET, release BOOT, then try again.", retry_slow=True) from None
        except Exception as e:  # noqa: BLE001
            raise FlashError(f"Couldn't open {self._name()} ({_clean(e)}).",
                             "Unplug the board, plug it back in and try again.") from None
        self.esp = esp
        chip = esp.CHIP_NAME
        self.chip = chip
        if self.classic and chip != "ESP32":
            raise FlashError(f"Expected the Biscuit's ESP32, found {chip}.")
        if not self.classic and chip not in WARDRIVE_CHIPS:
            raise FlashError(f"{chip} isn't supported.", "WardriveESP runs on ESP32-C3, C5, C6 and S3.")
        self._security(esp)
        try:
            self.info["mac"] = ":".join(f"{b:02x}" for b in esp.read_mac())
        except Exception:  # noqa: BLE001
            pass
        try:
            self.info["description"] = esp.get_chip_description()
        except Exception:  # noqa: BLE001
            self.info["description"] = chip
        log.info("flasher: %s in the ROM loader on %s (%s)", chip, self.device, self.info.get("description"))
        if self.stub:
            try:
                self.esp = esp = run_stub(esp)
            except Exception as e:  # noqa: BLE001
                raise FlashError("The board stopped answering while loading the flasher.",
                                 retry_slow=True) from e
        if self.fast:
            try:
                esp.change_baud(FAST_BAUD)
                self.info["baud"] = FAST_BAUD
            except Exception as e:  # noqa: BLE001
                raise FlashError("460800 baud didn't hold.", retry_slow=True) from e
        else:
            self.info["baud"] = 115200
        return chip

    def _name(self) -> str:
        return "the board's USB port" if self.device.startswith("usbjtag://") else self.device

    def _security(self, esp) -> None:
        if esp.CHIP_NAME == "ESP32":
            return        # the classic ESP32 ROM has no GET_SECURITY_INFO
        try:
            si = esp.get_security_info()
        except Exception:  # noqa: BLE001
            return
        flags = int(si.get("flags", 0) or 0)
        crypt = int(si.get("flash_crypt_cnt", 0) or 0)
        if flags & 0x1:
            raise FlashError("Secure boot is on. This board can't take WardriveESP.")
        if flags & 0x4:
            raise FlashError("Secure download mode is on. This board can't take WardriveESP.")
        if bin(crypt & 0x7).count("1") % 2:
            raise FlashError("Flash encryption is on. This board can't take WardriveESP.")

    # -- write ----
    def flash(self, regions: list[tuple[int, bytes]],
              on_progress: Callable[[str, float | None], None]) -> list[str]:
        """Write each region, each verified with SPI_FLASH_MD5. Returns the MD5s."""
        from esptool.cmds import attach_flash, write_flash
        from esptool.util import FatalError
        esp = self.esp
        if esp is None:
            raise FlashError("Not connected.")
        total = max(1, sum(len(d) for _, d in regions))
        done = 0
        md5s = []
        try:
            attach_flash(esp)
        except Exception as e:  # noqa: BLE001
            raise FlashError("The board's flash didn't respond.", retry_slow=True) from e
        for at, data in regions:
            if at % 0x1000:
                raise FlashError("Unaligned flash offset.")
            size = len(data)

            def prog(f: float, done=done, size=size):
                on_progress("writing", (done + f * size) / total)

            self._bind(prog)
            on_progress("writing", done / total)
            try:
                write_flash(esp, [(at, data)], flash_size="keep", compress=True)
            except FatalError as e:
                msg = str(e)
                if "MD5" in msg or "empty" in msg:
                    raise FlashError("Verify failed. The flash doesn't match the image.") from None
                raise FlashError(f"Write failed at {int(100 * done / total)}%.", retry_slow=True) from e
            except Exception as e:  # noqa: BLE001
                raise FlashError(f"Write failed at {int(100 * done / total)}%.", retry_slow=True) from e
            on_progress("verifying", None)
            want = hashlib.md5(data).hexdigest()
            try:
                got = esp.flash_md5sum(at, size)
            except Exception as e:  # noqa: BLE001
                raise FlashError("Couldn't verify the flash.") from e
            if got != want:
                log.warning("flasher: MD5 mismatch at 0x%x: flash %s vs image %s", at, got, want)
                raise FlashError("Verify failed. The flash doesn't match the image.")
            log.info("flasher: 0x%x verified, MD5 %s", at, want)
            md5s.append(want)
            done += size
            on_progress("writing", done / total)
        return md5s

    def restart(self) -> None:
        """Hard reset into the firmware (RTS pulse, DTR low)."""
        esp = self.esp
        if esp is None:
            return
        try:
            esp.hard_reset()
        except Exception:  # noqa: BLE001
            log.debug("flasher: hard reset failed", exc_info=True)
