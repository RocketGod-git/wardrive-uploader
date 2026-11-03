"""Wardrive files on a device's SD card, brought in automatically.

A WardriveESP board's offline runs (over Bluetooth or USB), a GhostESP board's
WiGLE CSVs and a Flipper Zero's saved files all come in the same way, as in
Wardrive Go: the device's driver lists the files, skips the ones taken before and
the ones that aren't WiGLE CSVs, then copies each new one when no other device is
copying. Each file is imported as its own run and goes out with the next upload
to WiGLE, WDGWars and Wardrift. Nothing on the device is changed, except that a
WardriveESP board deletes a run once it has been imported (it keeps runs only
until an app takes them).

The record of handled files is ``sd_import.json`` in the wardrive folder:
``<device key>|<path>|<size>``, so a file that grows comes back as new.
"""

from __future__ import annotations

import calendar
import csv
import io
import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

log = logging.getLogger("wdgwars.sdimport")

HEAD_BYTES = 64


@dataclass(frozen=True)
class RemoteFile:
    path: str
    size: int

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]


class FileSource(Protocol):
    device_key: str
    device_label: str
    next_time: str

    def list(self) -> list[RemoteFile]: ...
    def head(self, f: RemoteFile, n: int) -> bytes | None: ...
    def read(self, f: RemoteFile, out: io.BufferedIOBase, on_bytes: Callable[[int], None]) -> int: ...
    def taken(self, f: RemoteFile) -> bool: ...


class NotWardriveCsv(Exception):
    pass


def is_wigle_header(head: bytes) -> bool:
    """True when the file starts with a WiGLE CSV pre-header (WigleWifi-1.4, -1.6...)."""
    s = head.decode("utf-8", "replace").lstrip("﻿").lstrip()
    return s.startswith("WigleWifi-1.")


def _f(v: str | None) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def _i(v: str | None) -> int | None:
    try:
        return int(float(v)) if v not in (None, "") else None
    except ValueError:
        return None


def parse_wigle_csv(data: bytes) -> tuple[list[dict[str, Any]], int]:
    """WiGLE CSV rows as run-log rows, in time order, plus the count of rows left out
    (no position, no usable time, damaged). FirstSeen is UTC, as WiGLE writes it."""
    text = data.decode("utf-8", "replace").lstrip("﻿")
    lines = text.splitlines()
    if not lines or not lines[0].lstrip().startswith("WigleWifi-1."):
        raise NotWardriveCsv("not a WiGLE CSV")
    if len(lines) < 2 or not lines[1].startswith("MAC"):
        raise NotWardriveCsv("no WiGLE column header")
    rd = csv.reader(lines[1:])
    cols = [c.strip() for c in next(rd)]
    ix = {c: i for i, c in enumerate(cols)}

    def g(r: list[str], k: str) -> str:
        i = ix.get(k)
        return r[i].strip() if i is not None and i < len(r) else ""

    rows: list[dict[str, Any]] = []
    left = 0
    seen: set[tuple[str, float]] = set()
    for r in rd:
        if not r:
            continue
        try:
            mac = g(r, "MAC").lower()
            if len(mac) != 17:
                left += 1
                continue
            lat, lon = _f(g(r, "CurrentLatitude")), _f(g(r, "CurrentLongitude"))
            if lat is None or lon is None or (lat == 0 and lon == 0) or abs(lat) > 90 or abs(lon) > 180:
                left += 1
                continue
            ts = g(r, "FirstSeen")
            t = float(calendar.timegm(time.strptime(ts, "%Y-%m-%d %H:%M:%S"))) if ts else None
            # Rows a board wrote before its clock was set count from 1970.
            if t is None or t < 1_420_000_000:
                left += 1
                continue
            rssi = _i(g(r, "RSSI"))
            if rssi is None:
                left += 1
                continue
            if (mac, t) in seen:
                continue
            seen.add((mac, t))
            typ = (g(r, "Type") or "WIFI").upper()
            rows.append({
                "mac": mac, "ssid": g(r, "SSID"), "auth": g(r, "AuthMode"), "t": t,
                "ch": _i(g(r, "Channel")), "freq": _i(g(r, "Frequency")), "rssi": rssi,
                "lat": round(lat, 7), "lon": round(lon, 7),
                "alt": _f(g(r, "AltitudeMeters")), "acc": _f(g(r, "AccuracyMeters")),
                "mfgr": g(r, "MfgrId") or None, "type": typ,
            })
        except Exception:  # noqa: BLE001 - one damaged row never stops the file
            left += 1
    rows.sort(key=lambda x: x["t"])
    return rows, left


@dataclass
class ImportResult:
    rows: int
    new: int
    left_out: int


class SdImport:
    """One device copies at a time; the others keep scanning and wait their turn."""

    def __init__(self, folder: Path, importer: Callable[[list[dict[str, Any]], str, str | None], ImportResult],
                 on_event: Callable[[str], None]) -> None:
        self.file = folder / "sd_import.json"
        self.importer = importer
        self.on_event = on_event
        self._turn = threading.Lock()
        self._lock = threading.Lock()
        self.waiting: dict[str, str] = {}          # device key -> label, offered and not finished
        self.live: dict[str, Any] | None = None     # the file being copied now
        self.last = ""                              # the last finished import, one line
        self._data = self._load()

    # -- record of handled files ------------------------------------------------
    def _load(self) -> dict[str, list[str]]:
        try:
            d = json.loads(self.file.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                return {"seen": list(d.get("seen", [])), "del": list(d.get("del", []))}
        except (OSError, ValueError):
            pass
        return {"seen": [], "del": []}

    def _save(self) -> None:
        try:
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data), encoding="utf-8")
            tmp.replace(self.file)
        except OSError as e:
            log.warning("could not save the SD import record: %s", e)

    @staticmethod
    def _entry(src: FileSource, f: RemoteFile) -> str:
        return f"{src.device_key}|{f.path}|{f.size}"

    def _mark(self, key: str, entries: list[str], add: bool = True) -> None:
        if not entries:
            return
        with self._lock:
            s = set(self._data[key])
            s.update(entries) if add else s.difference_update(entries)
            self._data[key] = sorted(s)
            self._save()

    @property
    def busy(self) -> bool:
        return self._turn.locked()

    # -- driver side --------------------------------------------------------------
    def find_new(self, src: FileSource, stop: Callable[[], bool]) -> list[RemoteFile]:
        """New wardrive files on ``src``: not handled before and, where the start of a
        file can be read cheaply, WiGLE. Deletes that failed last time are asked again."""
        files = src.list()
        self._retry_deletes(src, files, stop)
        if not files:
            return []
        with self._lock:
            seen = set(self._data["seen"])
        fresh = [f for f in files if self._entry(src, f) not in seen]
        pending: list[RemoteFile] = []
        not_wigle: list[str] = []
        for f in fresh:
            if stop():
                break
            try:
                head = src.head(f, HEAD_BYTES)
            except Exception as e:  # noqa: BLE001
                log.info("SD import: couldn't read the start of %s on %s: %s", f.path, src.device_label, e)
                continue
            if head is None or is_wigle_header(head):
                pending.append(f)
            else:
                not_wigle.append(self._entry(src, f))
        self._mark("seen", not_wigle)
        log.info("SD import: %s · %d file(s), %d handled before, %d not WiGLE, %d new", src.device_label,
                 len(files), len(files) - len(fresh), len(not_wigle), len(pending))
        return pending

    def _retry_deletes(self, src: FileSource, files: list[RemoteFile], stop: Callable[[], bool]) -> None:
        with self._lock:
            mine = [e for e in self._data["del"] if e.startswith(src.device_key + "|")]
        if not mine:
            return
        on_card = {self._entry(src, f): f for f in files}
        done = []
        for e in mine:
            if stop():
                break
            f = on_card.get(e)
            if f is None:
                done.append(e)
                continue
            try:
                if src.taken(f):
                    done.append(e)
                    log.info("SD import: %s on %s deleted on this link (imported before)", f.path, src.device_label)
            except Exception as x:  # noqa: BLE001
                log.info("SD import: delete of %s failed again: %s", f.path, x)
        self._mark("del", done, add=False)

    def offer(self, src: FileSource, files: list[RemoteFile]) -> None:
        if not files:
            return
        offline = src.device_key.startswith("wdesp:")
        what = (f"{len(files)} offline run{'s' if len(files) != 1 else ''}" if offline
                else f"{len(files)} file{'s' if len(files) != 1 else ''}")
        with self._lock:
            self.waiting[src.device_key] = src.device_label
        self.on_event(f"{src.device_label}: {what} on the SD card, bringing {'them' if len(files) != 1 else 'it'} in")

    def withdraw(self, src: FileSource) -> None:
        with self._lock:
            if self.waiting.pop(src.device_key, None) is None:
                return
        log.info("SD import: %s gone before the copy, retries %s", src.device_label, src.next_time)

    def try_transfer(self, src: FileSource, files: list[RemoteFile], stop: Callable[[], bool]) -> bool:
        """Copy and import ``files`` if no other device is copying now (False: try again
        later). Runs on the device's own thread."""
        if not self._turn.acquire(blocking=False):
            return False
        try:
            self._transfer(src, files, stop)
        finally:
            with self._lock:
                self.waiting.pop(src.device_key, None)
                self.live = None
            self._turn.release()
        return True

    def _transfer(self, src: FileSource, files: list[RemoteFile], stop: Callable[[], bool]) -> None:
        imported = rows = new = skipped = failed = 0
        for n, f in enumerate(files, 1):
            if stop():
                break
            t0 = time.monotonic()
            buf = io.BytesIO()

            def progress(done: int, n=n, f=f, t0=t0) -> None:
                sec = max(0.001, time.monotonic() - t0)
                self.live = {"device": src.device_label, "file": f.name, "index": n, "count": len(files),
                             "done": done, "total": f.size, "bps": int(done / sec)}
            try:
                progress(0)
                src.read(f, buf, progress)
                data = buf.getvalue()
                if not is_wigle_header(data[:HEAD_BYTES]):
                    skipped += 1
                    self._mark("seen", [self._entry(src, f)])
                    log.info("SD import: %s isn't a WiGLE CSV, skipped", f.path)
                    continue
                parsed, left = parse_wigle_csv(data)
                r = self.importer(parsed, f"{src.device_label} {f.name}", getattr(src, "count_as", None))
                r.left_out += left
                imported += 1
                rows += r.rows
                new += r.new
                self._mark("seen", [self._entry(src, f)])
                log.info("SD import: %s · %d bytes in %.1f s · %d rows, %d new, %d left out", f.path, len(data),
                         time.monotonic() - t0, r.rows, r.new, r.left_out)
                what = "offline run" if src.device_key.startswith("wdesp:") else "SD file"
                self.on_event(f"{src.device_label}: {what} {f.name} imported · {r.rows:,} rows · {r.new:,} new"
                              + (" · going out with the next upload" if r.rows else ""))
                try:
                    kept = not src.taken(f)
                except Exception as e:  # noqa: BLE001
                    log.info("SD import: %s imported, but the device kept it: %s", f.path, e)
                    kept = True
                if kept and src.device_key.startswith("wdesp:"):
                    self._mark("del", [self._entry(src, f)])
            except NotWardriveCsv as e:
                skipped += 1
                self._mark("seen", [self._entry(src, f)])
                log.info("SD import: %s: %s", f.path, e)
            except Exception as e:  # noqa: BLE001 - link lost, unplugged
                failed += 1
                log.info("SD import: %s failed: %s", f.path, e)
                if stop():
                    break
        parts = [f"imported {imported} file{'s' if imported != 1 else ''}", f"{rows:,} rows", f"{new:,} new networks"]
        if skipped:
            parts.append(f"{skipped} not WiGLE")
        if failed:
            parts.append(f"{failed} failed, retries {src.next_time}")
        self.last = f"{src.device_label}: " + " · ".join(parts)
        if len(files) > 1 or skipped or failed:
            self.on_event(self.last)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"waiting": list(self.waiting.values()), "live": dict(self.live) if self.live else None,
                    "last": self.last}

