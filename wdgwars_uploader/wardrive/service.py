"""The wardriving run: scans, positions, the run log and its uploads.

Every few seconds each Wi-Fi adapter scans and Bluetooth reports what it heard.
A sighting is logged only with a GPS fix taken within a moment of when it was
actually heard, so a stale reading is never placed at the current position.
A network gets another row when it is heard again further down the road or
noticeably stronger, which is what WiGLE uses to place it.

Rows go to a log on disk per run (one JSON object per line, in time order).
Each upload target keeps its own position in each log, so a failed upload is
retried from where it stopped and one target never holds back another:

* WiGLE gets every row.
* Wardrift gets every row from the last 72 hours, in time order (it rebuilds
  the drive from row order).
* WDGWars gets the latest row per network from the last 72 hours, and skips a
  network it was sent within the hour.
"""

from __future__ import annotations

import collections
import gzip
import io
import json
import logging
import math
import platform
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .. import __version__
from ..config import config_dir, device_label
from .ble import BleScanner
from .capture import Hit, wpasec_my_nets, wpasec_results, wpasec_upload
from .gps import GpsHub, gps_ports, lan_addresses, max_age, NET_PORT
from .monitor import MonitorManager

log = logging.getLogger("wdgwars.wardrive")

SCAN_EVERY = 4.0
MOVE_M = 25.0            # a new row once a network is heard this far from its last row
STRONGER_DB = 6          # or this much stronger (after MIN_GAP_S)
MIN_GAP_S = 10.0
FIX_MATCH_S = 1.5        # how close in time a fix must be to the sighting
RECENT_S = 72 * 3600     # WDGWars and Wardrift only take recent rows
WDGW_RESEND_S = 3600
TARGETS = ("wigle", "wdgwars", "wardrift")
CSV_COLUMNS = ("MAC,SSID,AuthMode,FirstSeen,Channel,Frequency,RSSI,CurrentLatitude,"
               "CurrentLongitude,AltitudeMeters,AccuracyMeters,RCOIs,MfgrId,Type")


def _dist_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    r = 6371000.0
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


def _csv_field(v: Any) -> str:
    s = "" if v is None else str(v)
    if any(c in s for c in ',"\n\r'):
        s = '"' + s.replace('"', '""') + '"'
    return s


def _num(v: float | None) -> str:
    if v is None:
        return ""
    s = f"{v:.10f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


def csv_row(r: dict[str, Any]) -> str:
    """One WigleWifi-1.6 row, written the way the WiGLE app writes it: lower-case
    MAC, UTC time, blank channel/frequency/manufacturer when unknown."""
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(r["t"]))
    return ",".join([
        _csv_field(r["mac"]), _csv_field(r.get("ssid")), _csv_field(r.get("auth")), ts,
        "" if r.get("ch") is None else str(r["ch"]),
        "" if not r.get("freq") else str(r["freq"]),
        str(r["rssi"]), _num(r["lat"]), _num(r["lon"]), _num(r.get("alt")), _num(r.get("acc")),
        "", "" if not r.get("mfgr") else str(r["mfgr"]), r["type"],
    ])


def csv_header(label: str) -> str:
    return (f"WigleWifi-1.6,appRelease={__version__},model=Wardrive Uploader,release=Windows {platform.release()},"
            f"device={device_label(label)},display=,board=,brand=Windows\n{CSV_COLUMNS}\n")


MAX_FILE_BYTES = 9_500_000   # Wardrift takes at most 10 MiB per file, before and after compression
MAX_FILE_ROWS = 150_000


def _chunks(rows: list[dict[str, Any]], header: str):
    """(rows, csv bytes) pieces, each under the per-file size and row limits."""
    head = header.encode("utf-8")
    part: list[dict[str, Any]] = []
    lines: list[bytes] = []
    size = len(head)
    for r in rows:
        line = (csv_row(r) + "\n").encode("utf-8")
        if part and (size + len(line) > MAX_FILE_BYTES or len(part) >= MAX_FILE_ROWS):
            yield part, head + b"".join(lines)
            part, lines, size = [], [], len(head)
        part.append(r)
        lines.append(line)
        size += len(line)
    if part:
        yield part, head + b"".join(lines)


class Wardriver:
    def __init__(self, on_event: Callable[[str], None]) -> None:
        self.on_event = on_event
        self.dir = config_dir() / "wardrive"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.gps = GpsHub()
        self.ble: BleScanner | None = None
        self.wifi = None
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._gps_key: tuple | None = None
        self.running = False
        self.error = ""
        self._fixes: collections.deque = collections.deque(maxlen=120)
        self.run_name = ""
        self._reset_run()
        self.index = self._load_index()
        self.all_wifi = self._load_seen("seen_wifi.txt")
        self.all_ble = self._load_seen("seen_ble.txt")
        self.last_upload: dict[str, str] = {}
        self.monitor = MonitorManager(on_hit=self._on_hit)
        self.off: set[str] = set()
        self.wpasec_key = ""
        self._wpasec_lock = threading.Lock()
        self._wpasec_timer: threading.Timer | None = None
        self.wpasec_checked = 0.0
        self.captures = self._load_captures()
        self.run_pmkids = self.run_handshakes = 0

    # -- persistence -----------------------------------------------------------
    def _reset_run(self) -> None:
        self.run_name = time.strftime("run-%Y%m%d-%H%M%S.jsonl")
        self.live_wifi: list[dict[str, Any]] = []
        self.live_ble: list[dict[str, Any]] = []
        self._last_row: dict[str, tuple[float, float, int, float]] = {}
        self.run_wifi: set[str] = set()
        self.run_ble: set[str] = set()
        self.new_wifi = self.new_ble = 0
        self.rows = 0
        self.trail: collections.deque = collections.deque(maxlen=1500)

    def _load_index(self) -> dict[str, Any]:
        try:
            data = json.loads((self.dir / "index.json").read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("runs", {})
                data.setdefault("wdgw_sent", {})
                return data
        except (OSError, ValueError):
            pass
        return {"runs": {}, "wdgw_sent": {}}

    def _save_index(self) -> None:
        with self._lock:
            cutoff = time.time() - WDGW_RESEND_S
            self.index["wdgw_sent"] = {k: v for k, v in self.index["wdgw_sent"].items() if v >= cutoff}
            payload = json.dumps(self.index)
        tmp = self.dir / "index.json.tmp"
        try:
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(self.dir / "index.json")
        except OSError as e:
            log.warning("could not save wardrive index: %s", e)

    def _load_seen(self, name: str) -> set[int]:
        out: set[int] = set()
        try:
            with open(self.dir / name, encoding="ascii") as f:
                for line in f:
                    try:
                        out.add(int(line.strip(), 16))
                    except ValueError:
                        pass
        except OSError:
            pass
        return out

    # -- lifecycle -------------------------------------------------------------
    def apply(self, cfg: dict[str, Any], exclude_ports: set[str]) -> None:
        """Use every adapter, Bluetooth radio and location source found, except
        the ones switched off. Also picks up GPS ports plugged in since."""
        off = set(cfg.get("hw_off") or [])
        self.off = off
        key = (cfg.get("wpasec_key") or "").strip()
        if key and key != self.wpasec_key:
            # A new key, or the app just opened: send anything not sent yet and
            # pull the account's cracked keys.
            self.wpasec_key = key
            threading.Thread(target=self._wpasec_sync, name="wpasec", daemon=True).start()
        self.wpasec_key = key
        net_port = int(cfg.get("gps_net_port") or NET_PORT)
        key = (net_port, tuple(sorted(exclude_ports)), tuple(sorted(x for x in off if x.startswith("gps:"))),
               tuple(gps_ports(exclude_ports)))
        if key != self._gps_key:
            self._gps_key = key
            self.gps.start(net_port, exclude_ports, off)
        if not self.running:
            self.start()
        if self.wifi:
            self.wifi.off = {x[5:] for x in off if x.startswith("wifi:")}
        self.monitor.off = {x[4:] for x in off if x.startswith("mon:")}
        if "bt" in off and self.ble:
            self.ble.stop()
            self.ble = None
        elif "bt" not in off and self.ble is None:
            self.ble = BleScanner()
            self.ble.start()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self.running = True
        self.error = ""
        try:
            from .wifi import WifiScanner
            self.wifi = WifiScanner()
        except Exception as e:  # noqa: BLE001
            self.wifi = None
            self.error = f"Wi-Fi scanning unavailable: {e}"
        if self.wifi:
            self.wifi.off = {x[5:] for x in self.off if x.startswith("wifi:")}
        if "bt" not in self.off:
            self.ble = BleScanner()
            self.ble.start()
        self.monitor.start()
        self._thread = threading.Thread(target=self._loop, name="wardrive", daemon=True)
        self._thread.start()
        threading.Thread(target=self._gps_sampler, name="wardrive-gps", daemon=True).start()
        self.on_event("Wardriving started")

    def stop(self) -> None:
        if not self.running:
            self.gps.stop()
            self._gps_key = None
            return
        self.running = False
        self._stop.set()
        self.gps.stop()
        self._gps_key = None
        self.monitor.stop()
        if self.ble:
            self.ble.stop()
            self.ble = None
        if self.wifi:
            try:
                self.wifi.close()
            except Exception:
                pass
        self.on_event("Wardriving stopped")

    # -- scanning --------------------------------------------------------------
    def _gps_sampler(self) -> None:
        last = None
        while not self._stop.is_set():
            f = self.gps.best()
            if f is not None and f is not last:
                last = f
                with self._lock:
                    self._fixes.append(f)
            self._stop.wait(0.25)

    def _fix_at(self, at: float):
        """The fix taken closest to monotonic time ``at``, if close enough."""
        with self._lock:
            fixes = list(self._fixes)
        best = min(fixes, key=lambda f: abs(f.at - at), default=None)
        if best is None:
            return None
        slack = FIX_MATCH_S if (best.speed or 0) > 2 else 4.0
        return best if abs(best.at - at) <= max(slack, max_age(best.acc)) else None

    def _loop(self) -> None:
        while not self._stop.is_set():
            t0 = time.monotonic()
            nets: list[dict[str, Any]] = []
            try:
                if self.wifi:
                    ifs = self.wifi.trigger()
                    self._stop.wait(SCAN_EVERY)
                    nets = self.wifi.read(ifs)
                else:
                    self._stop.wait(SCAN_EVERY)
                bles = self.ble.fresh(SCAN_EVERY + 1) if self.ble else []
                merged = {n["mac"]: n for n in nets}
                for n in self.monitor.fresh(SCAN_EVERY + 2):
                    if n["mac"] not in merged or n["rssi"] > merged[n["mac"]]["rssi"]:
                        merged[n["mac"]] = n
                nets = list(merged.values())
                self._ingest(nets, bles)
            except Exception as e:  # noqa: BLE001
                self.error = f"scan error: {e}"
                log.exception("wardrive scan failed")
                self._stop.wait(max(0.0, SCAN_EVERY - (time.monotonic() - t0)))

    def _ingest(self, nets: list[dict[str, Any]], bles: list[dict[str, Any]]) -> None:
        rows: list[dict[str, Any]] = []
        new_wifi: list[str] = []
        new_ble: list[str] = []
        for kind, items in (("WIFI", nets), ("BLE", bles)):
            for it in items:
                fix = self._fix_at(it["at"])
                if fix is None:
                    continue
                mac = it["mac"]
                pos = (fix.lat, fix.lon)
                prev = self._last_row.get(mac)
                if prev is not None:
                    moved = _dist_m(prev[:2], pos)
                    stronger = it["rssi"] >= prev[2] + STRONGER_DB and it["utc"] - prev[3] >= MIN_GAP_S
                    if moved < MOVE_M and not stronger:
                        continue
                self._last_row[mac] = (fix.lat, fix.lon, it["rssi"], it["utc"])
                rows.append({
                    "mac": mac, "ssid": it.get("ssid") if kind == "WIFI" else it.get("name"),
                    "auth": it.get("auth", "") if kind == "WIFI" else "", "t": round(it["utc"], 3),
                    "ch": it.get("channel"), "freq": it.get("freq"), "rssi": it["rssi"],
                    "lat": round(fix.lat, 7), "lon": round(fix.lon, 7),
                    "alt": None if fix.alt is None else round(fix.alt, 1), "acc": round(fix.acc, 1),
                    "mfgr": it.get("mfgr"), "type": kind,
                })
                run_set = self.run_wifi if kind == "WIFI" else self.run_ble
                if mac not in run_set:
                    run_set.add(mac)
                    n = int(mac.replace(":", ""), 16)
                    all_set = self.all_wifi if kind == "WIFI" else self.all_ble
                    if n not in all_set:
                        all_set.add(n)
                        (new_wifi if kind == "WIFI" else new_ble).append(f"{n:012x}")
        with self._lock:
            self.live_wifi, self.live_ble = nets, bles
            self.new_wifi += len(new_wifi)
            self.new_ble += len(new_ble)
            best = self.gps.best()
            if best and (not self.trail or _dist_m(self.trail[-1], (best.lat, best.lon)) >= 10):
                self.trail.append((round(best.lat, 6), round(best.lon, 6)))
        if rows:
            rows.sort(key=lambda r: r["t"])
            self._append(rows)
        for name, items in (("seen_wifi.txt", new_wifi), ("seen_ble.txt", new_ble)):
            if items:
                try:
                    with open(self.dir / name, "a", encoding="ascii") as f:
                        f.write("\n".join(items) + "\n")
                except OSError:
                    pass

    def _append(self, rows: list[dict[str, Any]]) -> None:
        with self._lock:
            with open(self.dir / self.run_name, "a", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
            meta = self.index["runs"].setdefault(self.run_name, {"rows": 0})
            meta["rows"] = meta.get("rows", 0) + len(rows)
            self.rows += len(rows)
        self._save_index()

    # -- handshake and PMKID captures ------------------------------------------------
    def _load_captures(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        try:
            with open(self.dir / "captures.jsonl", encoding="utf-8") as f:
                for line in f:
                    try:
                        h = json.loads(line)
                        out[h["key"]] = h
                    except (ValueError, KeyError):
                        pass
        except OSError:
            pass
        return out

    def _on_hit(self, hit: Hit) -> None:
        fix = self._fix_at(time.monotonic()) or self.gps.best()
        rec = {"kind": hit.kind, "key": hit.key, "bssid": hit.bssid, "sta": hit.sta, "ssid": hit.ssid,
               "channel": hit.channel, "rssi": hit.rssi, "time": round(hit.time, 3), "hashline": hit.hashline,
               "lat": round(fix.lat, 7) if fix else None, "lon": round(fix.lon, 7) if fix else None}
        with self._lock:
            if hit.key in self.captures:
                return
            self.captures[hit.key] = rec
            if hit.kind == 2:
                self.run_handshakes += 1
            else:
                self.run_pmkids += 1
            try:
                with open(self.dir / "captures.jsonl", "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                with open(self.dir / "captures.22000", "a", encoding="ascii") as f:
                    f.write(hit.line() + "\n")
            except OSError as e:
                log.warning("could not save capture: %s", e)
        what = "Handshake" if hit.kind == 2 else "PMKID"
        self.on_event(f"{what} captured: {hit.ssid or '(hidden)'} ({hit.bssid}) ch {hit.channel}")
        if self.wpasec_key:
            # Send it to wpa-sec right away; a short wait batches the frames of one
            # handshake burst into a single upload.
            if self._wpasec_timer:
                self._wpasec_timer.cancel()
            self._wpasec_timer = threading.Timer(8.0, self._wpasec_sync)
            self._wpasec_timer.daemon = True
            self._wpasec_timer.start()

    def _wpasec_sync(self) -> None:
        if not self.wpasec_key:
            return
        try:
            msg = self.upload_wpasec(self.wpasec_key)
            if msg not in ("nothing new",):
                self.on_event(f"wpa-sec: {msg}")
        except Exception as e:  # noqa: BLE001
            log.warning("wpa-sec sync failed: %s", e)

    def upload_wpasec(self, key: str) -> str:
        """Send captures wpa-sec has not had yet, then read back every key the
        account has cracked. One at a time: a capture, the app opening and the
        regular upload can all ask at once."""
        with self._wpasec_lock:
            return self._upload_wpasec(key)

    def _upload_wpasec(self, key: str) -> str:
        with self._lock:
            sent = set(self.index.get("wpasec_sent", []))
            todo = [h for k, h in self.captures.items() if k not in sent]
        msg = "nothing new"
        if todo:
            ok, msg = wpasec_upload(key, todo)
            if not ok:
                self.last_upload["wpasec"] = msg
                return f"failed: {msg}"
            with self._lock:
                self.index["wpasec_sent"] = sorted(sent | {h["key"] for h in todo})
                self.index.setdefault("wpasec_first_sent", time.time())
            self._save_index()
            msg = f"{len(todo)} captures sent"
        known = {h["bssid"].replace(":", ""): h.get("ssid") or "" for h in self.captures.values()}
        names: dict[str, str] = {}
        cracked = wpasec_results(key, known, names=names)
        if cracked is not None:
            self.wpasec_checked = time.time()
            with self._lock:
                before = len(self.index.get("cracked", {}))
                self.index["cracked"] = {**self.index.get("cracked", {}), **cracked}
                self.index["cracked_ssid"] = {**self.index.get("cracked_ssid", {}), **names}
                new = len(self.index["cracked"]) - before
            self._save_index()
            if new:
                msg += f", {new} newly cracked"
                self.on_event(f"wpa-sec cracked {new} network{'s' if new != 1 else ''}")
        nets = wpasec_my_nets(key)
        if nets is not None:
            with self._lock:
                self.index["wpasec_nets"] = {n["bssid"]: n for n in nets}
                self.index["wpasec_nets_checked"] = time.time()
                for n in nets:
                    if n["cracked"] and n["psk"]:
                        self.index.setdefault("cracked", {})[n["bssid"]] = n["psk"]
                        self.index.setdefault("cracked_ssid", {})[n["bssid"]] = n["ssid"]
            self._save_index()
        self.last_upload["wpasec"] = msg
        return msg

    # -- uploads ---------------------------------------------------------------
    def pending(self, target: str) -> int:
        with self._lock:
            return sum(max(0, m.get("rows", 0) - m.get(target, 0)) for m in self.index["runs"].values())

    def _read(self, run: str, start: int) -> list[dict[str, Any]]:
        out = []
        try:
            with open(self.dir / run, encoding="utf-8") as f:
                for i, line in enumerate(f):
                    if i < start:
                        continue
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        out.append({})       # keep line numbers aligned with the index
        except OSError:
            pass
        return out

    def _advance(self, run: str, target: str, n: int) -> None:
        with self._lock:
            m = self.index["runs"].setdefault(run, {"rows": n})
            m[target] = max(m.get(target, 0), n)
        self._save_index()

    def upload(self, target: str, label: str, send: Callable[[bytes, str], tuple[bool, str]]) -> str:
        """Send what ``target`` has not received yet. ``send(gzipped_csv, file_name)``
        returns (ok, message). Returns a one-line summary."""
        with self._lock:
            runs = sorted((r, dict(m)) for r, m in self.index["runs"].items()
                          if m.get(target, 0) < m.get("rows", 0))
        if not runs:
            return "nothing new"
        sent = 0
        notes = []
        for run, meta in runs:
            start = meta.get(target, 0)
            rows = self._read(run, start)
            end = start + len(rows)
            good = [r for r in rows if r.get("mac")]
            if target != "wigle":
                good = [r for r in good if r["t"] >= time.time() - RECENT_S]
            if target == "wdgwars":
                latest: dict[str, dict[str, Any]] = {}
                for r in good:
                    latest[r["mac"]] = r
                with self._lock:
                    recent = dict(self.index["wdgw_sent"])
                cutoff = time.time() - WDGW_RESEND_S
                good = sorted((r for r in latest.values() if recent.get(r["mac"], 0) < cutoff),
                              key=lambda r: r["t"])
            ok_all = True
            for part, raw in _chunks(good, csv_header(label)):
                buf = io.BytesIO()
                with gzip.GzipFile(fileobj=buf, mode="wb", filename="wardrive.csv") as gz:
                    gz.write(raw)
                name = time.strftime(f"WardriveUploader-{device_label(label)}-%Y%m%d-%H%M%S.csv.gz")
                ok, msg = send(buf.getvalue(), name)
                if not ok:
                    ok_all = False
                    notes.append(msg)
                    break
                sent += len(part)
                notes.append(msg)
                if target == "wdgwars":
                    now = time.time()
                    with self._lock:
                        for r in part:
                            self.index["wdgw_sent"][r["mac"]] = now
            if not ok_all:
                self.last_upload[target] = notes[-1] if notes else "failed"
                return f"{sent} rows sent, then failed: {notes[-1] if notes else ''}"
            self._advance(run, target, end)
        summary = f"{sent} rows sent" + (f" ({notes[-1]})" if notes and notes[-1] else "")
        self.last_upload[target] = summary
        return summary

    def phone_link(self) -> dict[str, Any]:
        """The QR code and link that open the phone GPS page."""
        from .phoneweb import qr_svg
        src = next((s for s in self.gps.sources if s.id == "gps:phone"), None)
        if src is None:
            return {"ok": False, "error": "Phone GPS is switched off. Turn it back on in Hardware."}
        ips = lan_addresses()
        if not ips:
            return {"ok": False, "error": "This PC is not on a network."}
        links = [{"ip": ip, "url": src.web.url(ip), "svg": qr_svg(src.web.url(ip))} for ip in ips]
        return {"ok": True, "links": links, "error": src.web.error, "nmea_port": src.port}

    # -- dashboard -------------------------------------------------------------
    def _captures_view(self) -> dict[str, Any]:
        with self._lock:
            caps = list(self.captures.values())
            sent = set(self.index.get("wpasec_sent", []))
            cracked = dict(self.index.get("cracked", {}))
            cracked_ssid = dict(self.index.get("cracked_ssid", {}))
            nets = dict(self.index.get("wpasec_nets", {}))
            first_sent = float(self.index.get("wpasec_first_sent") or 0)
            nets_checked = float(self.index.get("wpasec_nets_checked") or 0)
        recent = sorted(caps, key=lambda h: -h["time"])[:40]
        here = {h["bssid"] for h in caps}

        def status(h: dict[str, Any]) -> dict[str, Any]:
            """Where one capture stands on wpa-sec."""
            n = nets.get(h["bssid"])
            if cracked.get(h["bssid"]):
                return {"state": "cracked", "text": "cracked", "psk": cracked[h["bssid"]]}
            if h["key"] not in sent:
                return {"state": "queued", "text": "waiting to send" if self.wpasec_key else "add a wpa-sec key to send"}
            if n:
                works = n.get("works") or ""
                return {"state": "working", "text": f"cracking · {works} passes" if works and works != "0" else "on wpa-sec, in the queue",
                        "type": n.get("type", ""), "feat": n.get("feat", ""), "info": n.get("info", "")}
            return {"state": "sent", "text": "sent, not on your account yet"}

        # wpa-sec takes any key string. If nothing sent ever shows up under this key,
        # the key is most likely mistyped or not the account's.
        key_warning = ""
        if (self.wpasec_key and first_sent and nets_checked and time.time() - first_sent > 600
                and not any(b in nets for b in here)):
            key_warning = ("wpa-sec does not list any of your captures under this key. It accepts any key, "
                           "so check that it is the one from your wpa-sec account (wpa-sec.stanev.org/?get_key).")
        return {
            "pmkids": sum(1 for h in caps if h["kind"] == 1), "handshakes": sum(1 for h in caps if h["kind"] == 2),
            "run_pmkids": self.run_pmkids, "run_handshakes": self.run_handshakes,
            "pending_wpasec": sum(1 for h in caps if h["key"] not in sent), "cracked": len(cracked),
            "eapol": self.monitor.capture.diag(),
            "recent": [{"ssid": h.get("ssid"), "bssid": h["bssid"], "kind": h["kind"], "time": h["time"],
                        "psk": cracked.get(h["bssid"]), "status": status(h)} for h in recent],
            "sent": sum(1 for h in caps if h["key"] in sent),
            "on_account": sum(1 for b in here if b in nets),
            "account_nets": len(nets),
            "key_warning": key_warning,
            "folder": str(self.dir),
            "keys": sorted(({"bssid": b, "ssid": cracked_ssid.get(b, ""), "psk": p, "here": b in here}
                            for b, p in cracked.items()), key=lambda k: (k["ssid"] or "~").lower()),
            "wpasec_checked": self.wpasec_checked,
            "wpasec_last": self.last_upload.get("wpasec", ""),
        }

    def snapshot(self, cfg: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            wifi = sorted(self.live_wifi, key=lambda r: -r["rssi"])[:80]
            ble = sorted(self.live_ble, key=lambda r: -r["rssi"])[:80]
            gps = self.gps.status()
            fix = gps.get("fix")
            reason = ""
            if self.running and not fix:
                reason = "waiting for a location fix; nothing is logged without one"
            return {
                "running": self.running,
                "error": self.error, "reason": reason, "logging": bool(self.running and fix),
                "gps": gps, "lan": lan_addresses(), "net_port": int(cfg.get("gps_net_port") or NET_PORT),
                "wifi": {"in_view": len(self.live_wifi), "run": len(self.run_wifi), "new": self.new_wifi,
                         "all": len(self.all_wifi)},
                "ble": {"in_view": len(self.live_ble), "run": len(self.run_ble), "new": self.new_ble,
                        "all": len(self.all_ble)},
                "rows": self.rows,
                "adapters": [dict(a, id=f"wifi:{a['name']}") for a in (self.wifi.adapters if self.wifi else [])]
                            + self.monitor.status(),
                "captures": self._captures_view(),
                "bt": {"state": self.ble.state if self.ble else ("off" if "bt" in self.off else "starting"),
                       "detail": self.ble.detail if self.ble else ""},
                "pending": {t: self.pending(t) for t in TARGETS},
                "last_upload": dict(self.last_upload),
                "feed_wifi": [{k: r.get(k) for k in ("mac", "ssid", "rssi", "channel", "auth")} for r in wifi],
                "feed_ble": [{k: r.get(k) for k in ("mac", "name", "rssi", "mfgr")} for r in ble],
                "trail": list(self.trail)[-600:],
            }
