"""Wardrift (wardrift.net) aircraft uploads.

Aircraft are sent to ``POST /v1/ingest/signals`` as a gzipped dump1090
``aircraft.json`` file, authenticated with a character API key (``wdk_...``).
The upload is processed synchronously and answered with a receipt.

Wardrift dates a sighting as ``now - seen`` and credits each aircraft once per
UTC observation day, so every record carries the absolute time it was observed
(``_obs_ts`` and ``_pos_ts``, stamped when it was collected). The relative ages
are rebuilt against the upload's ``now``.
"""

from __future__ import annotations

import gzip
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import requests

from . import __version__

BASE = "https://wardrift.net/v1"
UA = f"wdgwars-uploader/{__version__} (+https://github.com/RocketGod-git/wdgwars-uploader)"
CHUNK = 5000   # aircraft per file, well under the 10 MiB / 200,000-record limits

log = logging.getLogger("wdgwars.wardrift")


@dataclass
class WardriftResult:
    ok: bool = True
    sent: list[dict[str, Any]] = field(default_factory=list)   # records the server took
    credited: int = 0
    exp: int = 0
    influence: int = 0
    error: str = ""

    def summary(self) -> str:
        if not self.sent:
            return f"failed: {self.error}" if self.error else "nothing to send"
        s = f"{len(self.sent)} sent, {self.credited} new, +{self.exp} XP"
        return s + (f", then failed: {self.error}" if self.error else "")


def _record(a: dict[str, Any], now: float) -> dict[str, Any] | None:
    """Accumulated dump1090 record -> record for an upload made at ``now``.
    Records without a stamped observation time are skipped rather than dated
    to the upload."""
    obs = a.get("_obs_ts")
    if not isinstance(obs, (int, float)):
        return None
    out = {k: v for k, v in a.items() if not k.startswith("_")}
    out["seen"] = max(0.0, round(now - obs, 1))
    pos = a.get("_pos_ts")
    if isinstance(pos, (int, float)):
        out["seen_pos"] = max(0.0, round(now - pos, 1))
    else:
        out.pop("seen_pos", None)
    return out


def _json(resp: requests.Response) -> dict[str, Any]:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


class WardriftClient:
    def __init__(self, api_key: str, timeout: float = 60.0) -> None:
        self.api_key = (api_key or "").strip()
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"X-API-Key": self.api_key, "User-Agent": UA})

    def upload_aircraft(self, records: list[dict[str, Any]]) -> WardriftResult:
        """Upload accumulated aircraft in chunks. ``sent`` lists the records the
        server accepted, so a failure part-way through keeps only the rest."""
        res = WardriftResult()
        for i in range(0, len(records), CHUNK):
            part = records[i:i + CHUNK]
            now = time.time()
            body = [r for r in (_record(a, now) for a in part) if r]
            if body:
                data = gzip.compress(json.dumps({"now": now, "aircraft": body}).encode("utf-8"))
                receipt, err = self._post(data)
                if err:
                    res.ok, res.error = False, err
                    break
                res.credited += int(receipt.get("adsb_count") or 0)
                res.exp += int(receipt.get("adsb_exp") or 0)
                res.influence += int(receipt.get("awarded_influence") or 0)
            res.sent.extend(part)
        return res

    def _post(self, data: bytes) -> tuple[dict[str, Any], str]:
        headers = {"Content-Type": "application/gzip", "X-File-Name": "aircraft.json.gz"}
        for attempt in range(3):
            try:
                resp = self.session.post(f"{BASE}/ingest/signals", data=data,
                                         headers=headers, timeout=self.timeout)
            except requests.RequestException as e:
                return {}, str(e)
            body = _json(resp)
            if resp.status_code == 200:
                return body, ""
            code = body.get("error") or f"HTTP {resp.status_code}"
            # Two uploads on one key at the same moment; the same file is retried.
            if resp.status_code == 409 and code == "sequence_conflict" and attempt < 2:
                time.sleep(1.0 + attempt)
                continue
            msg = body.get("message")
            return body, f"{code}: {msg}" if msg else code
        return {}, "sequence_conflict"
