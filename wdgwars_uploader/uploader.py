"""WDGWars API client.

Three upload paths:

* **Aircraft** - flat records in chunks through the signed endpoint
  ``/endpoint/upload/`` (:func:`upload_aircraft_chunked`). The trailing slash is
  required, and ``/endpoint/*`` aliases ``/api/*`` without Cloudflare's per-IP
  burst limit.
* **Mesh nodes** - the same HMAC envelope to ``/api/upload``; the node shape is
  documented in :mod:`wdgwars_uploader.mesh`.
* **Files** - a CSV/LOG, or its ``.gz``, to ``/api/upload-csv``. A raw ``.json``
  is rejected ("Only .csv, .log, .gz files accepted"), so an aircraft JSON is
  always gzipped on this path; the server then sniffs it and reports
  ``format: "dump1090"``.

The HMAC envelope is ``{data: b64(json), nonce, sig=HMAC(key, nonce+data)}`` and
its inner payload must always carry all three slots ``{networks, aircraft,
meshcore_nodes}``. The plain endpoints take the 64-char hex key in the
``X-API-Key`` header (``/profile`` -> API Keys).
"""

from __future__ import annotations

import gzip
import io
import json
import logging
from dataclasses import dataclass
from typing import Any

import requests

from . import __version__
from .config import upload_file_name

BASE = "https://wdgwars.pl"
UA = f"wdgwars-mesh-adsb-uploader/{__version__} (+https://github.com/RocketGod-git/wdgwars-mesh-adsb-uploader)"

log = logging.getLogger("wdgwars.upload")


@dataclass
class UploadResult:
    ok: bool
    detail: dict[str, Any]
    error: str = ""

    def summary(self) -> str:
        if not self.ok:
            return f"failed: {self.error}"
        r = self.detail.get("result", self.detail)
        bits = []
        for k in ("aircraft_imported", "aircraft_already_seen", "chunks",
                  "imported", "already_seen", "captured", "updated", "skipped",
                  "unique_icaos", "format"):
            if k in r:
                bits.append(f"{k}={r[k]}")
        return ", ".join(bits) or "ok"


class WDGWarsClient:
    def __init__(self, api_key: str, timeout: int = 60, label: str = "") -> None:
        self.label = label
        self.api_key = (api_key or "").strip()
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"X-API-Key": self.api_key, "User-Agent": UA})

    # -- account -----------------------------------------------------------
    def check_key(self) -> UploadResult:
        """Validate the key via /api/me. Cheap, read-only."""
        if not self.api_key:
            return UploadResult(False, {}, "no API key set")
        try:
            resp = self.session.get(f"{BASE}/api/me", timeout=self.timeout)
            data = _safe_json(resp)
            if resp.status_code == 200 and data.get("ok"):
                return UploadResult(True, data)
            return UploadResult(False, data, data.get("error") or f"HTTP {resp.status_code}")
        except requests.RequestException as e:
            return UploadResult(False, {}, str(e))

    def get_stats(self) -> dict[str, Any]:
        """Full /api/me payload (counts, badges, gang, rank, credits)."""
        try:
            resp = self.session.get(f"{BASE}/api/me", timeout=self.timeout)
            data = _safe_json(resp)
            return data if resp.status_code == 200 else {}
        except requests.RequestException:
            return {}

    def get_leaderboard(self) -> dict[str, Any]:
        """/api/leaderboard: today / week / all_time / gangs / hunters."""
        try:
            resp = self.session.get(f"{BASE}/api/leaderboard", timeout=self.timeout)
            data = _safe_json(resp)
            return data if resp.status_code == 200 else {}
        except requests.RequestException:
            return {}

    # -- ADS-B -------------------------------------------------------------
    def upload_aircraft_json(self, aircraft_obj: dict[str, Any]) -> UploadResult:
        """Gzip a dump1090-shape aircraft object and upload it.

        Polls the async job to completion so the caller gets the real import
        counts, not just "queued".
        """
        raw = json.dumps(aircraft_obj).encode("utf-8")
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb", filename="aircraft.json") as gz:
            gz.write(raw)
        buf.seek(0)
        files = {"file": (upload_file_name(self.label, "aircraft.json.gz"), buf, "application/gzip")}
        try:
            resp = self.session.post(f"{BASE}/api/upload-csv", files=files, timeout=self.timeout)
            data = _safe_json(resp)
            if not (resp.status_code in (200, 202) and data.get("ok")):
                return UploadResult(False, data, data.get("error") or f"HTTP {resp.status_code}")
            # v1 returns the result inline; the queue path returns a job to poll.
            if data.get("queued") and data.get("job_id"):
                return self._poll_job(int(data["job_id"]))
            return UploadResult(True, data)
        except requests.RequestException as e:
            return UploadResult(False, {}, str(e))

    def _poll_job(self, job_id: int, deadline: float = 180.0, delay: float = 2.0) -> UploadResult:
        """Poll an async upload job until it finishes. The server's job queue can
        take well over a minute under load, so poll patiently — the alternative
        is giving up while the job succeeds server-side and re-uploading next
        cycle. A transient poll error is retried, not treated as failure."""
        import time
        end = time.time() + deadline
        while time.time() < end:
            try:
                resp = self.session.get(f"{BASE}/api/v2/upload-job/{job_id}", timeout=self.timeout)
                data = _safe_json(resp)
                status = data.get("status")
                if status == "done":
                    return UploadResult(True, data)
                if status == "failed":
                    return UploadResult(False, data, str(data.get("result") or "job failed"))
            except requests.RequestException as e:
                log.warning("poll error: %s", e)
            time.sleep(delay)
        # Not confirmed in time: report as unfinished so the caller keeps the
        # batch queued and retries. (The job may still complete server-side; the
        # retry is deduplicated by ICAO.)
        return UploadResult(False, {"job_id": job_id}, "upload still processing (will retry)")

    # -- aircraft via the signed endpoint, in chunks ----------------------
    # Why not upload_aircraft_json: /api/upload-csv queues an async job, and the
    # job-status endpoint redirects API-key clients to the browser login page, so
    # a client never sees "done" - it keeps the batch queued and re-sends it
    # every cycle. The signed endpoint answers synchronously with real counters.
    # Matches the maintained wdgwars.pl feeders (gungnir / Muninn):
    # /endpoint/upload/ - the trailing slash is required - chunked, with a pause
    # between chunks.
    SIGNED_URL = f"{BASE}/endpoint/upload/"
    AIRCRAFT_CHUNK = 400
    CHUNK_PAUSE = 1.0

    @staticmethod
    def _aircraft_record(a: dict[str, Any], first_seen: str) -> dict[str, Any] | None:
        """dump1090 aircraft dict -> the signed endpoint's flat record. Reads
        both field spellings: readsb/dump1090-fa (alt_baro, gs) and the bundled
        gvanem build (altitude, speed)."""
        def num(*vals: Any) -> int:
            for v in vals:
                try:
                    return int(float(v))
                except (TypeError, ValueError):   # None, or "ground"
                    continue
            return 0
        try:
            lat, lon = float(a["lat"]), float(a["lon"])
        except (KeyError, TypeError, ValueError):
            return None
        icao = str(a.get("hex") or "").strip().upper()
        if not icao:
            return None
        return {
            "icao": icao,
            "callsign": str(a.get("flight") or a.get("callsign") or "").strip(),
            "lat": lat, "lon": lon,
            "alt_ft": num(a.get("alt_baro"), a.get("altitude")),
            "speed_kt": num(a.get("gs"), a.get("speed")),
            "heading": num(a.get("track"), a.get("heading")),
            "first_seen": first_seen,
            "type": "ADSB",
        }

    def _post_signed(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any], str]:
        """POST one HMAC envelope. Returns (http_status, json, error). Retries
        network errors and 5xx with backoff; never retries 4xx."""
        import base64
        import hashlib
        import hmac
        import secrets
        import time
        data_b64 = base64.b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
        err = ""
        for attempt in range(3):
            nonce = secrets.token_hex(8)
            sig = hmac.new(self.api_key.encode(), (nonce + data_b64).encode(),
                           hashlib.sha256).hexdigest()
            try:
                resp = self.session.post(self.SIGNED_URL,
                                         json={"data": data_b64, "nonce": nonce, "sig": sig},
                                         headers={"Accept": "application/json"},
                                         timeout=max(self.timeout, 120))
            except requests.RequestException as e:
                err = str(e)
            else:
                if "json" not in (resp.headers.get("content-type") or ""):
                    # A login, maintenance or WAF page - not an answer.
                    return resp.status_code, {}, f"HTTP {resp.status_code}: non-JSON reply (login/maintenance page?)"
                data = _safe_json(resp)
                if resp.status_code < 500:
                    return resp.status_code, data, data.get("error") or ""
                err = f"HTTP {resp.status_code}"
            time.sleep(2 * (2 ** attempt))
        return 0, {}, err or "upload failed"

    def upload_aircraft_chunked(self, aircraft: list[dict[str, Any]],
                                chunk: int | None = None) -> UploadResult:
        """Upload dump1090-shape aircraft in chunks through the signed endpoint.

        ``detail["sent_hexes"]`` lists the aircraft the server accepted, chunk
        by chunk, so the caller can mark exactly those as sent even when a
        later chunk fails: a problem part-way through no longer re-queues work
        that already landed.
        """
        import time
        from datetime import datetime, timezone
        first_seen = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        pairs = [(a.get("hex"), r) for a in aircraft
                 for r in [self._aircraft_record(a, first_seen)] if r]
        size = max(1, int(chunk or self.AIRCRAFT_CHUNK))
        sent: list[str] = []
        imported = seen = done = 0
        i = 0
        error = ""
        while i < len(pairs):
            part = pairs[i:i + size]
            status, data, err = self._post_signed(
                {"networks": [], "aircraft": [r for _, r in part], "meshcore_nodes": []})
            if status == 413 and size > 25:
                size = max(25, size // 2)          # too big: halve and retry this chunk
                log.warning("aircraft chunk too large; retrying at %d", size)
                continue
            if status == 429:
                wait = data.get("retry_after")
                error = f"rate limited (429){f', retry after {wait}s' if wait else ''}; rest stays queued"
                break
            if status != 200 or not data.get("ok", False):
                error = err or f"HTTP {status}"
                break
            imp = int(data.get("aircraft_imported", 0) or 0)
            dup = int(data.get("aircraft_already_seen", 0) or 0)
            note = " ".join(str(data.get(k) or "") for k in ("info", "message", "note")).lower()
            deliberate_skip = "already uploaded recently" in note or "no new processing" in note
            if imp == 0 and dup == 0 and not deliberate_skip:
                # HTTP 200 ok:true with every counter zero: the server
                # rejected the records without saying so.
                error = f"server accepted the request but counted none of {len(part)} aircraft"
                break
            imported += imp
            seen += dup
            done += 1
            sent.extend(h for h, _ in part if h)
            i += len(part)
            if i < len(pairs):
                time.sleep(self.CHUNK_PAUSE)
        remaining = len(pairs) - i
        total_chunks = done + (-(-remaining // size) if remaining else 0)
        detail = {"result": {"aircraft_imported": imported, "aircraft_already_seen": seen,
                             "chunks": f"{done}/{total_chunks}"},
                  "sent_hexes": sent, "aircraft_imported": imported}
        return UploadResult(not error, detail, error)

    # -- generic CSV (WiGLE / mesh CSV) -----------------------------------
    def upload_csv_bytes(self, data_bytes: bytes, filename: str = "data.csv") -> UploadResult:
        """POST an already-formatted CSV/LOG (or its .gz) to /api/upload-csv."""
        files = {"file": (upload_file_name(self.label, filename), io.BytesIO(data_bytes), "text/csv")}
        try:
            resp = self.session.post(f"{BASE}/api/upload-csv", files=files, timeout=self.timeout)
            data = _safe_json(resp)
            if not (resp.status_code in (200, 202) and data.get("ok")):
                return UploadResult(False, data, data.get("error") or f"HTTP {resp.status_code}")
            if data.get("queued") and data.get("job_id"):
                return self._poll_job(int(data["job_id"]))
            return UploadResult(True, data)
        except requests.RequestException as e:
            return UploadResult(False, {}, str(e))

    # -- generic JSON HMAC upload (/api/upload) ----------------------------
    def upload_json_hmac(self, payload: dict[str, Any]) -> UploadResult:
        """POST a payload to /api/upload with the HMAC-SHA256 envelope the API
        documents: {data: b64(json), nonce, sig=HMAC(key, nonce+data)}."""
        import base64
        import hashlib
        import hmac
        import secrets

        data_b64 = base64.b64encode(json.dumps(payload).encode()).decode()
        nonce = secrets.token_hex(8)
        sig = hmac.new(self.api_key.encode(), (nonce + data_b64).encode(),
                       hashlib.sha256).hexdigest()
        body = {"data": data_b64, "nonce": nonce, "sig": sig}
        try:
            resp = self.session.post(
                f"{BASE}/api/upload", json=body,
                headers={"Content-Type": "application/json"}, timeout=self.timeout)
            data = _safe_json(resp)
            if resp.status_code == 200 and data.get("ok", True) and "error" not in data:
                return UploadResult(True, data)
            return UploadResult(False, data, data.get("error") or f"HTTP {resp.status_code}")
        except requests.RequestException as e:
            return UploadResult(False, {}, str(e))


def _safe_json(resp: requests.Response) -> dict[str, Any]:
    try:
        return resp.json()
    except ValueError:
        return {"error": (resp.text or "")[:300]}
