"""pyserial URL handler ``usbjtag://[serial]`` for an ESP USB-Serial/JTAG port
(303a:1001) bound to WinUSB, which has no COM port.

Some PCs have the C5's CDC interface bound to WinUSB (Zadig, libwdi), so Windows
shows no COM port for it. This talks CDC straight over libusb instead: bulk
0x01/0x81 on interface 1, DTR/RTS via SET_CONTROL_LINE_STATE on interface 0. It
reconnects if the device drops off the bus during a reset. ``serial`` picks one
board by its USB serial number (its MAC); empty takes the first one.

A reader thread keeps one 64-byte IN transfer pending with a long timeout.
Short-timeout reads lose data on WinUSB: a transfer canceled by its timeout just
as a packet lands drops that packet (esptool then fails with "Corrupt data,
expected 0x1000 bytes but received 0xf80").

Registered with pyserial by :mod:`wdgwars_uploader.flasher.ports`.
"""

from __future__ import annotations

import logging
import threading
import time
import urllib.parse

from serial.serialutil import PortNotOpenError, SerialBase, SerialException, Timeout

log = logging.getLogger("wdgwars.flasher")

VID, PID = 0x303A, 0x1001
EP_OUT, EP_IN = 0x01, 0x81


def backend():
    import libusb_package
    return libusb_package.get_libusb1_backend()


def find(serial: str = ""):
    """The 303a:1001 device with this serial number (any one when empty), or None."""
    import usb.core
    import usb.util
    be = backend()
    for dev in usb.core.find(find_all=True, idVendor=VID, idProduct=PID, backend=be) or []:
        if not serial:
            return dev
        try:
            sn = usb.util.get_string(dev, dev.iSerialNumber) if dev.iSerialNumber else ""
        except Exception:  # noqa: BLE001 - bound to another driver: not ours to open
            continue
        if (sn or "").strip().lower() == serial.strip().lower():
            return dev
    return None


class Serial(SerialBase):
    def open(self):
        if self._port is None:
            raise SerialException("usbjtag: no port given")
        self._want = urllib.parse.unquote(str(self._port).split("://", 1)[-1].strip("/"))
        self._dev = None
        self._rx = bytearray()
        self._cv = threading.Condition()
        self._dev_lock = threading.Lock()
        self._connect()
        self.is_open = True
        self._reader = threading.Thread(target=self._read_loop, name="usbjtag-rx", daemon=True)
        self._reader.start()
        self._update_dtr_state()

    def _connect(self, wait: float = 3.0):
        import usb.core
        import usb.util
        end = time.time() + wait
        err: Exception | None = None
        while True:
            dev = find(self._want)
            if dev is not None:
                try:
                    try:
                        dev.set_configuration()
                    except usb.core.USBError:
                        pass
                    usb.util.claim_interface(dev, 0)
                    usb.util.claim_interface(dev, 1)
                    self._dev = dev
                    return
                except usb.core.USBError as e:
                    err = e
            if time.time() > end:
                why = "not found" if dev is None else f"busy ({err})"
                raise SerialException(f"usbjtag: cannot open 303a:1001 {self._want or ''} ({why})")
            time.sleep(0.1)

    def close(self):
        self.is_open = False
        if getattr(self, "_reader", None) is not None:
            self._reader.join(1.5)
            self._reader = None
        if getattr(self, "_dev", None) is not None:
            try:
                import usb.util
                usb.util.dispose_resources(self._dev)
            except Exception:  # noqa: BLE001
                pass
        self._dev = None

    def _read_loop(self):
        import usb.core
        while self.is_open:
            dev = self._dev
            if dev is None:
                time.sleep(0.05)
                continue
            try:
                data = bytes(dev.read(EP_IN, 64, 1000))
            except usb.core.USBTimeoutError:
                continue
            except usb.core.USBError:
                if self.is_open:
                    try:
                        self._reconnect(dev)
                    except SerialException:
                        time.sleep(0.2)
                continue
            if data:
                with self._cv:
                    self._rx += data
                    self._cv.notify_all()

    def _reconnect(self, failed_dev):
        with self._dev_lock:
            if self._dev is not failed_dev:
                return  # another thread already reconnected
            try:
                import usb.util
                usb.util.dispose_resources(failed_dev)
            except Exception:  # noqa: BLE001
                pass
            self._dev = None
            time.sleep(0.3)
            self._connect(wait=5.0)

    def _lines(self):
        import usb.core
        dev = self._dev
        if dev is None:
            return
        v = (1 if self._dtr_state else 0) | (2 if self._rts_state else 0)
        try:
            dev.ctrl_transfer(0x21, 0x22, v, 0, None, 500)
        except usb.core.USBError:
            self._reconnect(dev)

    def _update_dtr_state(self):
        if self.is_open:
            self._lines()

    def _update_rts_state(self):
        if self.is_open:
            self._lines()

    def _update_break_state(self):
        pass

    def _reconfigure_port(self, *a, **k):
        pass

    @property
    def in_waiting(self):
        if not self.is_open:
            raise PortNotOpenError()
        with self._cv:
            return len(self._rx)

    def read(self, size=1):
        if not self.is_open:
            raise PortNotOpenError()
        t = Timeout(self._timeout)
        with self._cv:
            while len(self._rx) < size:
                left = t.time_left()
                if left is not None and left <= 0:
                    break
                self._cv.wait(left)
            out = bytes(self._rx[:size])
            del self._rx[:size]
        return out

    def write(self, data):
        import usb.core
        if not self.is_open:
            raise PortNotOpenError()
        data = bytes(data)
        for i in range(0, len(data), 4096):
            dev = self._dev
            try:
                dev.write(EP_OUT, data[i:i + 4096], 2000)
            except usb.core.USBTimeoutError:
                # The running app isn't draining its USB RX FIFO.
                log.debug("usbjtag: write of %d bytes timed out (device not reading)", len(data))
                return i
            except usb.core.USBError:
                self._reconnect(dev)
                self._dev.write(EP_OUT, data[i:i + 4096], 2000)
        return len(data)

    def flush(self):
        pass

    def reset_input_buffer(self):
        time.sleep(0.01)
        with self._cv:
            self._rx.clear()

    def reset_output_buffer(self):
        pass

    def from_url(self, url):
        return url
