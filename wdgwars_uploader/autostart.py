"""Start with Windows via the per-user Run registry key
(``HKCU\\...\\Run`` — no admin needed). Mid-session crash-restart is handled
separately by :mod:`wdgwars_uploader.watchdog`. All launches use ``--boot`` so
the dashboard doesn't pop open at every logon.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "WardriveUploader"
OLD_NAME = "WDGWarsUploader"          # before the rename; cleaned up on start
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

log = logging.getLogger("wdgwars.autostart")


def _startup_shortcut() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "WardriveUploader.lnk"


def _sync_startup_shortcut(enable: bool) -> None:
    """A Startup-folder .lnk is a SECOND, independent logon trigger - HKCU Run keys
    can silently fail to fire after Windows-Update reboots (session-restore / ARSO),
    and the two paths rarely fail together. Created via PowerShell to avoid an extra
    dependency.
    """
    if sys.platform != "win32":
        return
    lnk = _startup_shortcut()
    if not enable:
        try:
            lnk.unlink()
        except OSError:
            pass
        return
    if not getattr(sys, "frozen", False):
        # A second logon trigger is only safe where a takeover launch can kill the
        # duplicate it races with, and _kill_stale_app_instances() is frozen-build
        # only. From a source install both triggers fire, neither instance dies, and
        # the two fight over every COM port.
        return
    target = _run_target()
    if not target:
        return
    exe, args, workdir = target
    lnk.parent.mkdir(parents=True, exist_ok=True)
    ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}');"
          f"$s.TargetPath='{exe}';$s.Arguments='{args}';"
          f"$s.WorkingDirectory='{workdir}';$s.Save()")
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       capture_output=True, timeout=20, creationflags=_NO_WINDOW)
    except Exception as e:  # noqa: BLE001
        log.debug("startup-shortcut sync failed: %s", e)


def _run_target() -> tuple[str, str, str] | None:
    """How to relaunch this install at logon: ``(exe, arguments, workdir)``.

    A frozen build launches its own exe. A run-from-source install launches the
    venv's pythonw against the ABSOLUTE ``run_app.py``: a Run-key command
    inherits an arbitrary working directory, so ``-m wdgwars_uploader`` would not
    find the package, whereas Python always puts a script's own directory on
    ``sys.path``.
    """
    if getattr(sys, "frozen", False):
        local = ensure_local_copy()
        exe = str(local) if (local and local.exists()) else sys.executable
        return exe, "--boot", str(Path(exe).parent)
    py = sys.executable
    pyw = str(Path(py).with_name("pythonw.exe"))     # no console window
    exe = pyw if Path(pyw).exists() else py
    script = Path(__file__).resolve().parent.parent / "run_app.py"
    if not script.exists():
        return None
    return exe, f'"{script}" --boot', str(script.parent)


def _local_exe() -> Path:
    """A stable LOCAL (never OneDrive) home for the autostart exe copy."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    d = Path(base) / "WardriveUploader"
    d.mkdir(parents=True, exist_ok=True)
    return d / "WardriveUploader.exe"


def remove_old_name() -> None:
    """Remove what builds from before the rename left for starting with Windows:
    the Run value, the Startup shortcut and the local exe copy, so the old
    version can never start again."""
    if sys.platform != "win32":
        return
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, OLD_NAME)
        log.info("removed the old auto-start entry")
    except OSError:
        pass
    base = os.environ.get("APPDATA") or str(Path.home())
    lnk = Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / f"{OLD_NAME}.lnk"
    try:
        lnk.unlink()
    except OSError:
        pass
    old = Path(os.environ.get("LOCALAPPDATA") or str(Path.home())) / OLD_NAME
    if old.is_dir():
        shutil.rmtree(old, ignore_errors=True)


def ensure_local_copy() -> Path | None:
    """Mirror the frozen exe to a local path so autostart never depends on OneDrive
    having hydrated the file at logon (a OneDrive on-demand exe is a reparse point
    that may be unavailable early in the boot). Returns the local path, or None when
    not frozen or the copy failed.
    """
    if not getattr(sys, "frozen", False):
        return None
    src = Path(sys.executable)
    dst = _local_exe()
    try:
        if src.resolve() == dst.resolve():
            return dst   # already running from the local copy
    except OSError:
        pass
    try:
        s = src.stat()
        if (not dst.exists()) or s.st_size != dst.stat().st_size \
                or s.st_mtime > dst.stat().st_mtime + 1:
            shutil.copy2(str(src), str(dst))
            log.info("mirrored exe to local autostart copy: %s", dst)
        return dst
    except OSError as e:
        log.warning("could not mirror exe locally: %s", e)
        return None


def _launch_command() -> str:
    target = _run_target()
    if not target:
        return f'"{sys.executable}" -m wdgwars_uploader --boot'
    exe, args, _ = target
    return f'"{exe}" {args}'


def is_enabled() -> bool:
    if sys.platform != "win32":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as k:
            winreg.QueryValueEx(k, VALUE_NAME)
            return True
    except OSError:
        return False


def enable() -> bool:
    if sys.platform != "win32":
        return False
    import winreg
    ok = False
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.SetValueEx(k, VALUE_NAME, 0, winreg.REG_SZ, _launch_command())
        log.info("registered auto-start (Run key)")
        ok = True
    except OSError as e:
        log.warning("auto-start Run-key enable failed: %s", e)
    _sync_startup_shortcut(True)   # redundant second logon trigger
    return ok


def disable() -> None:
    if sys.platform != "win32":
        return
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, VALUE_NAME)
        log.info("removed auto-start")
    except FileNotFoundError:
        pass
    except OSError as e:
        log.warning("auto-start disable failed: %s", e)
    _sync_startup_shortcut(False)


def sync(desired: bool) -> None:
    remove_old_name()
    # enable() overwrites the value, so calling it when desired keeps the
    # registered command current (e.g. picks up the --boot flag).
    if desired:
        enable()
    elif is_enabled():
        disable()
