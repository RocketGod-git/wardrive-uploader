"""WardriveESP firmware source: version.json and the images on rocketgod.dev.

Same source and the same checks as the Wardrive Go flasher:

* ``version.json`` gives ``latest`` (WardriveESP), ``coproc`` (the Biscuit Pro / Ultra
  co-processor) and ``coproc_parts`` (its separate images and where each goes).
* WardriveESP is always the merged image ``bins/<chip>/wardrive-bridge-merged.bin``
  written at 0x0, for a first flash and for an update alike.
* The co-processor goes in as the merged image at 0x0 over the stock Biscuit firmware
  (that clears the stock NVS), and as the three parts at 0x1000 / 0x8000 / 0x10000
  over our own co-processor, so NVS (0x9000-0xF000) with the stored model and scan
  mode survives. Parts that fail to download are cut from the merged image instead.
* Every download is size checked and structure checked (image magic, chip id in the
  bootloader and app headers, partition table magic) before it gets near a board.

Both sites are tried in order, the WardriveESP folder first, then the old iosESP one.
"""

from __future__ import annotations

import logging
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable

import requests

from .. import __version__

log = logging.getLogger("wdgwars.flasher")

SITES = ("https://rocketgod.dev/WardriveESP", "https://rocketgod.dev/iosESP")
UA = f"wardrive-uploader/{__version__} (+https://github.com/RocketGod-git/wardrive-uploader)"

FLASH_MAX = 4 * 1024 * 1024        # WardriveESP's partition table needs a 4 MB part
COPROC_APP_AT = 0x10000            # nvs 0x9000-0xF000 and phy_init 0xF000 sit below it

# chip name (as esptool and WD:CAPS report it) -> (bins folder, image chip id, bootloader offset)
CHIPS = {
    "ESP32-C3": ("esp32c3", 5, 0x0),
    "ESP32-C5": ("esp32c5", 23, 0x2000),
    "ESP32-C6": ("esp32c6", 13, 0x0),
    "ESP32-S3": ("esp32s3", 9, 0x0),
}

_VER_RE = re.compile(r"^\d+(\.\d+){1,3}$")
_PART_FILE_RE = re.compile(r"^[A-Za-z0-9._-]+\.bin$")

Progress = Callable[[float | None], None]


class FirmwareError(Exception):
    """A download or check failed. The message is short and user-facing."""


@dataclass
class Manifest:
    latest: str
    date: str = ""
    coproc: str | None = None
    coproc_parts: list[tuple[int, str]] | None = None
    fetched_at: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return {"latest": self.latest, "date": self.date, "coproc": self.coproc,
                "coproc_parts": bool(self.coproc_parts)}


def chip_label(chip: str | None) -> str:
    """'esp32c5' or 'ESP32-C5' -> 'ESP32-C5'."""
    if not chip:
        return ""
    u = chip.strip().upper()
    if u.startswith("ESP32") and len(u) > 5 and u[5] != "-":
        u = "ESP32-" + u[5:]
    return u


def version_tuple(v: str | None) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in str(v or "").split("."))
    except ValueError:
        return ()


def parse_coproc_parts(a) -> list[tuple[int, str]] | None:
    """version.json ``coproc_parts``: only the co-processor's three regions, never one
    that reaches into NVS (0x9000-0xF000)."""
    if not isinstance(a, list) or not a:
        return None
    out: list[tuple[int, str]] = []
    for p in a:
        if not isinstance(p, dict):
            return None
        try:
            at = int(str(p.get("at", "")).lower().removeprefix("0x"), 16)
        except ValueError:
            return None
        f = str(p.get("file", ""))
        if not _PART_FILE_RE.match(f):
            return None
        out.append((at, f))
    out.sort()
    return out if [a for a, _ in out] == [0x1000, 0x8000, 0x10000] else None


def parse_manifest(obj) -> Manifest:
    if not isinstance(obj, dict):
        raise FirmwareError("Couldn't read the latest version. Try again.")
    latest = str(obj.get("latest") or "").strip()
    if not _VER_RE.match(latest):
        raise FirmwareError("Couldn't read the latest version. Try again.")
    coproc = str(obj.get("coproc") or "").strip()
    coproc_ok = bool(_VER_RE.match(coproc))
    return Manifest(latest=latest, date=str(obj.get("date") or ""),
                    coproc=coproc if coproc_ok else None,
                    coproc_parts=parse_coproc_parts(obj.get("coproc_parts")) if coproc_ok else None)


# -- structure checks ----------------------------------------------------------

def _u16(b: bytes, i: int) -> int:
    return b[i] | (b[i + 1] << 8)


def is_image(b: bytes, at: int, chip_id: int) -> bool:
    """An ESP image header at ``at`` built for ``chip_id`` (0 = classic ESP32)."""
    return len(b) > at + 16 and b[at] == 0xE9 and _u16(b, at + 12) == chip_id


def is_table(b: bytes, at: int) -> bool:
    return len(b) > at + 2 and b[at] == 0xAA and b[at + 1] == 0x50


def check_bridge(chip: str, data: bytes) -> None:
    folder, chip_id, bl = CHIPS[chip]
    ok = (0x30000 <= len(data) <= FLASH_MAX
          and is_image(data, bl, chip_id)          # bootloader built for this chip
          and is_table(data, 0x8000)               # partition table
          and is_image(data, 0x20000, chip_id))    # ota_0 app
    if not ok:
        raise FirmwareError(f"The download isn't a valid {chip} image. Try again.")


def check_coproc_merged(data: bytes) -> None:
    ok = (0x10100 <= len(data) <= FLASH_MAX and is_image(data, 0x1000, 0)
          and is_table(data, 0x8000) and is_image(data, COPROC_APP_AT, 0))
    if not ok:
        raise FirmwareError("The download isn't a valid co-processor image. Try again.")


def check_coproc_part(at: int, data: bytes) -> None:
    if at == 0x1000:
        ok = 16 <= len(data) <= 0x7000 and is_image(data, 0, 0)
    elif at == 0x8000:
        ok = 16 <= len(data) <= 0x1000 and is_table(data, 0)
    else:
        ok = 16 <= len(data) <= 0x300000 and is_image(data, 0, 0)
    if not ok:
        raise FirmwareError("The download isn't a valid co-processor image. Try again.")


# -- downloads -----------------------------------------------------------------

class FirmwareSource:
    """version.json + images, with a small in-memory cache so a retry never downloads
    the same image twice."""

    MANIFEST_TTL = 300.0

    def __init__(self, session: requests.Session | None = None, sites=SITES) -> None:
        self.http = session or requests.Session()
        self.sites = tuple(sites)
        self._manifest: Manifest | None = None
        self._cache: dict[str, bytes] = {}

    @property
    def manifest(self) -> Manifest | None:
        return self._manifest

    def _from_sites(self, fn):
        last: Exception | None = None
        for site in self.sites:
            try:
                return fn(site)
            except Exception as e:  # noqa: BLE001
                last = e
                log.info("flasher: %s failed (%s), trying the next site", site, e)
        if isinstance(last, FirmwareError):
            raise last
        raise FirmwareError("Download failed. Check your connection and try again.")

    def _get(self, url: str, max_bytes: int, progress: Progress | None = None) -> bytes:
        headers = {"User-Agent": UA, "Cache-Control": "no-cache", "Pragma": "no-cache"}
        try:
            resp = self.http.get(url, headers=headers, timeout=(15, 20), stream=True)
        except requests.RequestException:
            raise FirmwareError("No connection. Check your internet and try again.") from None
        with resp:
            if resp.status_code != 200:
                raise FirmwareError(f"Download failed (HTTP {resp.status_code}). Try again.")
            total = int(resp.headers.get("Content-Length") or 0)
            if resp.headers.get("Content-Encoding"):
                total = 0          # the length is of the compressed body
            if total > max_bytes:
                raise FirmwareError("The download is too big. Try again later.")
            buf = bytearray()
            try:
                for chunk in resp.iter_content(16 * 1024):
                    buf += chunk
                    if len(buf) > max_bytes:
                        raise FirmwareError("The download is too big. Try again later.")
                    if progress:
                        progress(len(buf) / total if total else None)
            except requests.RequestException:
                raise FirmwareError("Download cut off. Try again.") from None
            if total and len(buf) != total:
                raise FirmwareError("Download cut off. Try again.")
            return bytes(buf)

    def fetch_manifest(self, force: bool = False) -> Manifest:
        m = self._manifest
        if m and not force and time.time() - m.fetched_at < self.MANIFEST_TTL:
            return m

        def get(site: str) -> Manifest:
            import json
            raw = self._get(f"{site}/version.json?t={int(time.time() * 1000)}", 256 * 1024)
            try:
                return parse_manifest(json.loads(raw.decode("utf-8-sig")))
            except ValueError:
                raise FirmwareError("Couldn't read the latest version. Try again.") from None

        self._manifest = self._from_sites(get)
        return self._manifest

    def _bin(self, path: str, version: str, max_bytes: int, check, progress: Progress | None) -> bytes:
        key = f"{path}@{version}"
        if key in self._cache:
            if progress:
                progress(1.0)
            return self._cache[key]

        def get(site: str) -> bytes:
            data = self._get(f"{site}/{path}?v={urllib.parse.quote(version)}", max_bytes, progress)
            check(data)
            return data

        data = self._from_sites(get)
        self._cache[key] = data
        return data

    def bridge(self, chip: str, version: str, progress: Progress | None = None) -> bytes:
        """The merged WardriveESP image for ``chip``, checked."""
        if chip not in CHIPS:
            raise FirmwareError(f"{chip} isn't supported. WardriveESP needs a C3, C5, C6 or S3.")
        folder = CHIPS[chip][0]
        return self._bin(f"bins/{folder}/wardrive-bridge-merged.bin", version, FLASH_MAX,
                         lambda d: check_bridge(chip, d), progress)

    def coproc(self, version: str, wipe: bool, parts: list[tuple[int, str]] | None,
               progress: Progress | None = None) -> list[tuple[int, bytes]]:
        """Regions for the Biscuit's WROOM. ``wipe`` = it still runs the stock firmware:
        the merged image at 0x0. Otherwise only bootloader, table and app, so NVS stays."""
        if not wipe and parts:
            try:
                out = []
                for i, (at, f) in enumerate(parts):
                    def sub(p, i=i):
                        if progress:
                            progress(None if p is None else (i + p) / len(parts))
                    out.append((at, self._bin(f"bins/esp32/{f}", version, FLASH_MAX,
                                              lambda d, at=at: check_coproc_part(at, d), sub)))
                return out
            except FirmwareError as e:
                log.info("flasher: co-processor parts failed (%s), cutting them from the merged image", e)
        b = self._bin("bins/esp32/wardrive-coproc-merged.bin", version, FLASH_MAX,
                      check_coproc_merged, progress)
        if wipe:
            return [(0x0, b)]
        return [(0x1000, b[0x1000:0x9000]), (COPROC_APP_AT, b[COPROC_APP_AT:])]
