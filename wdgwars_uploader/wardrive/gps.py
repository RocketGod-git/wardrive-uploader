"""GPS for wardriving.

Sources, all optional and used together:

* a USB or Bluetooth GPS on a COM port, read as NMEA;
* a phone streaming NMEA over the network (TCP or UDP, port 10110 by default);
* Windows location: built-in GPS, or Wi-Fi and cell positioning.

The most accurate fresh fix wins, and every row carries that fix's accuracy.
Windows' IP-address fallback is not used: it is usually the provider's city
center, kilometers away, which would stack every network on one wrong point.
"""

from __future__ import annotations

import asyncio
import calendar
import logging
import math
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("wdgwars.gps")

FIX_MAX_AGE = 3.0         # seconds a precise fix stays usable


def max_age(acc: float) -> float:
    """How old a fix may be. Coarse positions (Wi-Fi or cell, refreshed every
    several seconds) may be older, since the error from age stays well under
    their stated accuracy."""
    return max(FIX_MAX_AGE, min(30.0, acc / 10.0))
NET_PORT = 10110          # the usual NMEA-over-IP port
_BAUDS = (9600, 4800, 38400, 115200)
_GPS_WORDS = re.compile(r"gps|gnss|u-?blox|sirf|globalsat|nmea", re.I)
_UBLOX_VID = 0x1546


def _finite(x: Any) -> float | None:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


@dataclass
class Fix:
    lat: float
    lon: float
    alt: float | None
    acc: float            # estimated horizontal accuracy, meters
    speed: float | None   # m/s
    course: float | None
    sats: int | None
    utc: float            # when the fix was taken (unix seconds)
    at: float             # time.monotonic() when it was received
    source: str

    def age(self) -> float:
        return time.monotonic() - self.at

    def usable(self) -> bool:
        return self.age() <= max_age(self.acc)

    def as_dict(self) -> dict[str, Any]:
        return {"lat": round(self.lat, 6), "lon": round(self.lon, 6),
                "alt": None if self.alt is None else round(self.alt, 1),
                "acc": round(self.acc, 1), "speed": None if self.speed is None else round(self.speed, 2),
                "sats": self.sats, "age": round(self.age(), 1), "source": self.source,
                "usable": self.usable()}


# --- NMEA -------------------------------------------------------------------
def _checksum_ok(line: str) -> bool:
    if "*" not in line:
        return True
    body, _, cs = line[1:].partition("*")
    v = 0
    for ch in body:
        v ^= ord(ch)
    try:
        return v == int(cs[:2], 16)
    except ValueError:
        return False


def _coord(val: str, hemi: str) -> float | None:
    if not val or not hemi:
        return None
    try:
        dot = val.index(".")
        deg = float(val[:dot - 2])
        minutes = float(val[dot - 2:])
    except ValueError:
        return None
    d = deg + minutes / 60.0
    return -d if hemi in "SW" else d


class NmeaParser:
    """Combines RMC (position, speed, date), GGA (fix quality, satellites,
    HDOP, altitude) and GST (error estimate) into fixes."""

    def __init__(self, source: str) -> None:
        self.source = source
        self.gga: dict[str, Any] = {}
        self.gst: dict[str, Any] = {}
        self.last_sentence = 0.0

    def feed(self, line: str) -> Fix | None:
        line = line.strip()
        if not line.startswith("$") or not _checksum_ok(line):
            return None
        self.last_sentence = time.monotonic()
        parts = line.split("*")[0].split(",")
        kind = parts[0][3:]
        if kind == "GGA" and len(parts) >= 10:
            try:
                self.gga = {"q": int(parts[6] or 0), "sats": int(parts[7] or 0),
                            "hdop": float(parts[8] or 99), "alt": float(parts[9]) if parts[9] else None,
                            "at": time.monotonic()}
            except ValueError:
                self.gga = {}
        elif kind == "GST" and len(parts) >= 8:
            try:
                self.gst = {"err": math.hypot(float(parts[6]), float(parts[7])), "at": time.monotonic()}
            except ValueError:
                self.gst = {}
        elif kind == "RMC" and len(parts) >= 10:
            return self._rmc(parts)
        return None

    def _rmc(self, p: list[str]) -> Fix | None:
        if p[2] != "A":
            return None
        lat, lon = _coord(p[3], p[4]), _coord(p[5], p[6])
        if lat is None or lon is None or (lat == 0 and lon == 0):
            return None
        now = time.monotonic()
        gga = self.gga if now - self.gga.get("at", 0) < 2 else {}
        if gga and gga.get("q", 0) == 0:
            return None
        gst = self.gst if now - self.gst.get("at", 0) < 2 else {}
        if gst:
            acc = gst["err"]
        elif gga:
            acc = max(2.5, gga["hdop"] * 5.0)    # HDOP times a typical receiver error
        else:
            acc = 15.0
        utc = time.time()
        try:
            t, d = p[1], p[9]
            utc = calendar.timegm((2000 + int(d[4:6]), int(d[2:4]), int(d[0:2]),
                                   int(t[0:2]), int(t[2:4]), int(float(t[4:]))))
        except (ValueError, IndexError):
            pass
        speed = float(p[7]) * 0.514444 if p[7] else None
        course = float(p[8]) if p[8] else None
        return Fix(lat, lon, gga.get("alt"), acc, speed, course, gga.get("sats"), utc, now, self.source)


# --- sources ----------------------------------------------------------------
class _Source:
    name = ""
    id = ""

    def __init__(self) -> None:
        self.fix: Fix | None = None
        self.state = "starting"
        self.detail = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name=f"gps-{self.name}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        raise NotImplementedError

    def status(self) -> dict[str, Any]:
        f = self.fix if self.fix and self.fix.age() < 30 else None
        return {"id": self.id, "name": self.name, "state": self.state, "detail": self.detail,
                "fix": f.as_dict() if f else None}


class SerialGps(_Source):
    def __init__(self, port: str) -> None:
        super().__init__()
        self.port = port
        self.name = f"GPS on {port}"
        self.id = f"gps:serial:{port}"

    def _run(self) -> None:
        import serial
        while not self._stop.is_set():
            for baud in _BAUDS:
                if self._stop.is_set():
                    return
                try:
                    with serial.Serial(self.port, baud, timeout=1) as ser:
                        self.state, self.detail = "listening", f"{baud} baud"
                        parser = NmeaParser(self.name)
                        quiet_since = time.monotonic()
                        while not self._stop.is_set():
                            raw = ser.readline()
                            if raw:
                                f = parser.feed(raw.decode("ascii", "replace"))
                                if f:
                                    self.fix = f
                                if parser.last_sentence > quiet_since:
                                    quiet_since = parser.last_sentence
                                    self.state = "fix" if self.fix and self.fix.age() < 5 else "no fix yet"
                            if time.monotonic() - quiet_since > 4:
                                break    # nothing NMEA at this speed: try the next one
                except Exception as e:  # noqa: BLE001 - port busy, unplugged, BT link down
                    self.state, self.detail = "unavailable", str(e)[:120]
                    self._stop.wait(3)
            self._stop.wait(2)


class NetworkGps(_Source):
    """A phone's location: from its browser (the page the dashboard's QR code
    opens), or NMEA from a GPS app over TCP or UDP."""

    def __init__(self, port: int = NET_PORT) -> None:
        super().__init__()
        self.port = port
        self.name = "Phone GPS"
        self.id = "gps:phone"
        self.peers: set[str] = set()
        from .phoneweb import PhoneWeb
        self.web = PhoneWeb(self._browser_fix)

    def stop(self) -> None:
        super().stop()
        self.web.stop()

    def _browser_fix(self, d: dict[str, Any], peer: str) -> None:
        lat, lon, acc = _finite(d.get("lat")), _finite(d.get("lon")), _finite(d.get("acc"))
        if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("no position")
        speed = _finite(d.get("speed"))
        self.fix = Fix(lat, lon, _finite(d.get("alt")), acc if acc is not None else 50.0, speed,
                       _finite(d.get("course")), None, time.time(), time.monotonic(), "Phone GPS (browser)")
        self.state = "fix"
        self.peers.add(peer)
        self.detail = "browser · " + ", ".join(sorted(self.peers))

    def _run(self) -> None:
        self.web.start(lan_addresses())
        threading.Thread(target=self._udp, name="gps-net-udp", daemon=True).start()
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("0.0.0.0", self.port))
            srv.listen(4)
            srv.settimeout(1.0)
        except OSError as e:
            self.state, self.detail = "unavailable", f"port {self.port}: {e}"
            return
        self.state = "listening"
        while not self._stop.is_set():
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._client, args=(conn, addr[0]), daemon=True).start()
        srv.close()

    def _feed(self, parser: NmeaParser, data: str, peer: str) -> None:
        for line in data.splitlines():
            f = parser.feed(line)
            if f:
                self.fix = f
                self.state = "fix"
                self.peers.add(peer)
                self.detail = "from " + ", ".join(sorted(self.peers))

    def _client(self, conn: socket.socket, peer: str) -> None:
        parser = NmeaParser(self.name)
        buf = ""
        conn.settimeout(15)
        with conn:
            while not self._stop.is_set():
                try:
                    chunk = conn.recv(4096)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk.decode("ascii", "replace")
                if "\n" in buf:
                    head, _, buf = buf.rpartition("\n")
                    self._feed(parser, head, peer)

    def _udp(self) -> None:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.bind(("0.0.0.0", self.port))
            s.settimeout(1.0)
        except OSError as e:
            self.detail = f"UDP port {self.port}: {e}"
            return
        parser = NmeaParser(self.name)
        while not self._stop.is_set():
            try:
                data, addr = s.recvfrom(8192)
            except socket.timeout:
                continue
            except OSError:
                break
            self._feed(parser, data.decode("ascii", "replace"), addr[0])
        s.close()


class WindowsGps(_Source):
    """Windows location from any source except the IP-address fallback."""

    name = "Windows location"
    id = "gps:windows"

    def _run(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception as e:  # noqa: BLE001
            self.state, self.detail = "unavailable", str(e)[:120]

    async def _main(self) -> None:
        from datetime import timedelta
        from winrt.windows.devices.geolocation import Geolocator, PositionSource
        await Geolocator.request_access_async()
        g = Geolocator()
        g.desired_accuracy_in_meters = 5
        self.state = "listening"
        kinds = {PositionSource.SATELLITE: "GPS", PositionSource.WI_FI: "Wi-Fi",
                 PositionSource.CELLULAR: "cell"}
        while not self._stop.is_set():
            try:
                pos = await asyncio.wait_for(
                    g.get_geoposition_async_with_age_and_timeout(timedelta(seconds=2), timedelta(seconds=10)), 12)
                c = pos.coordinate
                src = c.position_source
                if src == PositionSource.IP_ADDRESS:
                    self.state, self.detail = "no position", "only an IP-address estimate"
                else:
                    try:
                        age = max(0.0, time.time() - c.timestamp.timestamp())
                    except Exception:
                        age = 0.0
                    p = c.point.position
                    kind = kinds.get(src, "other")
                    # Windows reports unknown speed, heading or altitude as NaN
                    self.fix = Fix(p.latitude, p.longitude, _finite(p.altitude), _finite(c.accuracy) or 99.0,
                                   _finite(c.speed), _finite(c.heading), None, time.time() - age,
                                   time.monotonic() - age, f"Windows location ({kind})")
                    self.state, self.detail = "fix", kind
            except Exception as e:  # noqa: BLE001
                self.detail = str(e)[:120]
            await asyncio.sleep(1)


def gps_ports(exclude: set[str]) -> list[str]:
    """COM ports that identify as a GPS (u-blox, or GPS/GNSS in the name).
    Mesh radios and other boards are never opened unasked, since opening a
    port can reset them."""
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    out = []
    for p in list_ports.comports():
        if p.device in exclude:
            continue
        text = " ".join(str(x or "") for x in (p.description, p.manufacturer, p.product))
        if getattr(p, "vid", None) == _UBLOX_VID or _GPS_WORDS.search(text):
            out.append(p.device)
    return out


@dataclass
class GpsHub:
    sources: list[_Source] = field(default_factory=list)

    def start(self, net_port: int, exclude: set[str], off: set[str]) -> None:
        """Every location source there is; the ones in ``off`` are listed but not run."""
        self.stop()
        srcs: list[_Source] = [SerialGps(p) for p in gps_ports(exclude)]
        srcs += [NetworkGps(net_port), WindowsGps()]
        for s in srcs:
            if s.id in off:
                s.state = "off"
            else:
                s.start()
        self.sources = srcs

    def stop(self) -> None:
        for s in self.sources:
            s.stop()
        self.sources = []

    def best(self) -> Fix | None:
        fixes = [s.fix for s in self.sources if s.fix and s.fix.usable()]
        return min(fixes, key=lambda f: (f.acc, f.age())) if fixes else None

    def status(self) -> dict[str, Any]:
        b = self.best()
        return {"fix": b.as_dict() if b else None, "sources": [s.status() for s in self.sources]}


def lan_addresses() -> list[str]:
    """This PC's LAN IPv4 addresses, for the phone setup hint."""
    out = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith(("127.", "169.254.")) and ip not in out:
                out.append(ip)
    except OSError:
        pass
    return out
