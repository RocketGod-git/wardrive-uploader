"""Update check and one-click install from the GitHub releases page.

The check compares the latest release tag with this version. Installing
downloads the MSI, verifies it against the SHA-256 digest GitHub publishes for
the asset, then hands over to a small PowerShell helper that waits for the app
to close, runs the installer (Windows asks for permission once) and starts the
app again. If the install fails or is declined, the old version starts again.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

import requests

from . import __version__
from .config import config_dir

log = logging.getLogger(__name__)

REPO = "RocketGod-git/wardrive-uploader"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
UA = f"wardrive-uploader/{__version__} (+https://github.com/{REPO})"
CHECK_EVERY = 6 * 3600
_NO_WINDOW = 0x08000000
_DETACHED = 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP

_HELPER = r"""
param([int]$AppPid, [string]$Msi, [string]$Exe, [string]$Result)
$ErrorActionPreference = 'SilentlyContinue'
Wait-Process -Id $AppPid -Timeout 60
$name = [IO.Path]::GetFileNameWithoutExtension($Exe)
$t = 0
while ((Get-Process -Name $name) -and $t -lt 30) { Start-Sleep 1; $t++ }
Get-Process -Name $name | Stop-Process -Force
$code = -1
try {
  $p = Start-Process msiexec.exe -ArgumentList "/i `"$Msi`" /passive /norestart" -Verb RunAs -Wait -PassThru -ErrorAction Stop
  $code = $p.ExitCode
} catch { $code = 1602 }
Set-Content -Path $Result -Value "{`"code`": $code, `"msi`": `"$([IO.Path]::GetFileName($Msi))`"}" -Encoding ASCII
Start-Process -FilePath $Exe
"""


def parse_version(v: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", v or "")
    return tuple(int(n) for n in nums[:4]) or (0,)


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False)) and sys.platform == "win32"


class Updater:
    def __init__(self, on_quit: Callable[[], None], on_event: Callable[[str], None]) -> None:
        self.on_quit, self.on_event = on_quit, on_event
        self._lock = threading.Lock()
        self._release: dict[str, Any] = {}
        self.status: dict[str, Any] = {
            "current": __version__, "latest": "", "available": False, "checked": 0.0,
            "phase": "idle", "progress": 0, "error": "", "notes": "", "page": "",
            "can_install": frozen(),
        }
        self._report_last_install()

    # -- check ---------------------------------------------------------------
    def check(self) -> dict[str, Any]:
        if self.status["phase"] in ("downloading", "installing"):
            return self.status
        self.status.update(phase="checking", error="")
        try:
            resp = requests.get(API, timeout=20, headers={"User-Agent": UA, "Accept": "application/vnd.github+json"})
            resp.raise_for_status()
            rel = resp.json()
            tag = str(rel.get("tag_name") or "")
            asset = next((a for a in rel.get("assets") or []
                          if str(a.get("name", "")).lower().endswith(".msi")), None)
            newer = parse_version(tag) > parse_version(__version__) and asset is not None
            with self._lock:
                self._release = {"tag": tag, "asset": asset or {}}
            self.status.update(latest=tag.lstrip("v"), available=newer, checked=time.time(),
                               notes=str(rel.get("body") or "")[:600], page=str(rel.get("html_url") or ""),
                               phase="idle")
        except Exception as e:  # noqa: BLE001 - shown in the dashboard, retried later
            self.status.update(phase="idle", error=f"update check failed: {e}", checked=time.time())
        return self.status

    # -- install -------------------------------------------------------------
    def install(self) -> dict[str, Any]:
        """Start the download and install in the background."""
        if not self.status["available"] or self.status["phase"] in ("downloading", "installing"):
            return self.status
        if not frozen():
            self.status["error"] = "updates install from the installed app only"
            return self.status
        self.status.update(phase="downloading", progress=0, error="")
        threading.Thread(target=self._install, name="wdgw-update", daemon=True).start()
        return self.status

    def _install(self) -> None:
        try:
            msi = self._download()
            self.status.update(phase="installing", progress=100)
            self.on_event(f"Installing update {self.status['latest']}; the app will reopen when it is done")
            self._launch_helper(msi)
            time.sleep(0.5)
            self.on_quit()
        except Exception as e:  # noqa: BLE001
            log.exception("update failed")
            self.status.update(phase="idle", error=f"update failed: {e}")

    def _download(self) -> Path:
        with self._lock:
            asset = dict(self._release.get("asset") or {})
        url, size = asset.get("browser_download_url"), int(asset.get("size") or 0)
        digest = str(asset.get("digest") or "")
        if not url or not url.startswith(f"https://github.com/{REPO}/releases/download/"):
            raise ValueError("unexpected download location")
        out = Path(tempfile.gettempdir()) / f"WardriveUploader-{self.status['latest']}.msi"
        h, got = hashlib.sha256(), 0
        with requests.get(url, stream=True, timeout=60, headers={"User-Agent": UA}) as resp:
            resp.raise_for_status()
            with open(out, "wb") as f:
                for chunk in resp.iter_content(1 << 16):
                    f.write(chunk)
                    h.update(chunk)
                    got += len(chunk)
                    if size:
                        self.status["progress"] = int(got * 100 / size)
        if size and got != size:
            raise ValueError(f"download incomplete ({got} of {size} bytes)")
        if digest.startswith("sha256:") and h.hexdigest() != digest.split(":", 1)[1].lower():
            out.unlink(missing_ok=True)
            raise ValueError("download did not match the published checksum")
        return out

    def _launch_helper(self, msi: Path) -> None:
        helper = Path(tempfile.gettempdir()) / "wardrive-uploader-update.ps1"
        helper.write_text(_HELPER, encoding="utf-8")
        cmd = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
               "-File", str(helper), "-AppPid", str(os.getpid()), "-Msi", str(msi),
               "-Exe", sys.executable, "-Result", str(config_dir() / "update_result.json")]
        subprocess.Popen(cmd, creationflags=_NO_WINDOW | _DETACHED, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _report_last_install(self) -> None:
        """After an update the helper leaves its result; say how it went."""
        path = config_dir() / "update_result.json"
        try:
            data = json.loads(path.read_text(encoding="ascii"))
            path.unlink()
        except (OSError, ValueError):
            return
        code = int(data.get("code", -1))
        if code in (0, 3010):
            self.on_event(f"Updated to {__version__}")
        elif code == 1602:
            self.on_event("Update canceled; still on " + __version__)
        else:
            self.on_event(f"Update did not install (installer code {code}); still on {__version__}")
