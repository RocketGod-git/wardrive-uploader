"""Update check and one-click install from the GitHub releases page.

The check compares the latest release tag with this version. Installing
downloads the MSI and verifies it against the SHA-256 digest GitHub publishes
for the asset. Windows then asks for permission while the app is still open; if
granted, an elevated helper waits for the app to close, runs the installer with
its progress bar showing and starts the app again as the normal user. If the
prompt is declined the app stays open and nothing changes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import ctypes
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
# Runs elevated (started through the Windows permission prompt). It restarts the
# app through Explorer so the app runs as the normal user, not as admin.
_HELPER = r"""
param([int]$AppPid, [string]$Msi, [string]$Exe, [string]$Result)
$ErrorActionPreference = 'SilentlyContinue'
Wait-Process -Id $AppPid -Timeout 60
$name = [IO.Path]::GetFileNameWithoutExtension($Exe)
$t = 0
while ((Get-Process -Name $name) -and $t -lt 30) { Start-Sleep 1; $t++ }
Get-Process -Name $name | Stop-Process -Force
Start-Sleep 1
$code = -1
try {
  $p = Start-Process msiexec.exe -ArgumentList "/i `"$Msi`" /passive /norestart" -Wait -PassThru -ErrorAction Stop
  $code = $p.ExitCode
} catch { $code = -2 }
Set-Content -Path $Result -Value "{`"code`": $code, `"msi`": `"$([IO.Path]::GetFileName($Msi))`"}" -Encoding ASCII
Start-Process explorer.exe -ArgumentList "`"$Exe`""
"""


class _ShellExecuteInfo(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("fMask", ctypes.c_ulong), ("hwnd", ctypes.c_void_p),
                ("lpVerb", ctypes.c_wchar_p), ("lpFile", ctypes.c_wchar_p), ("lpParameters", ctypes.c_wchar_p),
                ("lpDirectory", ctypes.c_wchar_p), ("nShow", ctypes.c_int), ("hInstApp", ctypes.c_void_p),
                ("lpIDList", ctypes.c_void_p), ("lpClass", ctypes.c_wchar_p), ("hkeyClass", ctypes.c_void_p),
                ("dwHotKey", ctypes.c_ulong), ("hIcon", ctypes.c_void_p), ("hProcess", ctypes.c_void_p)]


def _run_elevated(exe: str, params: str) -> int:
    """Start ``exe`` through the Windows permission prompt. Returns 0 once it is
    running, or the Windows error code (1223 = the prompt was declined)."""
    info = _ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x00000040 | 0x00000400   # SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC
    info.lpVerb, info.lpFile, info.lpParameters, info.nShow = "runas", exe, params, 0
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)):
        return ctypes.GetLastError() or 1
    if info.hProcess:
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(info.hProcess))
    return 0


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
            self.status.update(phase="approve", progress=100)
            err = self._launch_helper(msi)
            if err == 1223:
                self.status.update(phase="idle", error="update canceled at the Windows prompt")
                self.on_event("Update canceled at the Windows prompt")
                return
            if err:
                raise OSError(f"could not start the installer (Windows error {err})")
            self.status.update(phase="installing")
            self.on_event(f"Installing update {self.status['latest']}; the app will reopen when it is done")
            time.sleep(2.5)   # let the dashboard show the last step before the app closes
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

    def _launch_helper(self, msi: Path) -> int:
        helper = Path(tempfile.gettempdir()) / "wardrive-uploader-update.ps1"
        helper.write_text(_HELPER, encoding="utf-8")
        result = config_dir() / "update_result.json"
        params = (f'-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{helper}" '
                  f'-AppPid {os.getpid()} -Msi "{msi}" -Exe "{sys.executable}" -Result "{result}"')
        return _run_elevated("powershell.exe", params)

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
        elif code == 1618:
            self.on_event("Another install was running, so the update waited; try Install update again")
        else:
            self.on_event(f"Update did not install (installer code {code}); still on {__version__}")
