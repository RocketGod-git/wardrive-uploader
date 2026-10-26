"""Tray application + backend host.

The tray anchors the always-on background work:
* the **scheduler** thread runs uploads on the chosen interval,
* the **poller** thread keeps the dashboard's live feed/counters fresh,
* the **dashboard server** thread serves the web UI + JSON API on 127.0.0.1.

The dashboard opens as a native window (pywebview) if available, otherwise in
the default browser. pystray's icon owns the main thread.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

from . import APP_NAME, __version__, adsb, autostart, mayhem, traymenu, updater, watchdog
from .config import Config, INTERVAL_CHOICES_HOURS, config_dir
from .core import UploaderCore
from .logutil import setup_logging
from .poller import Poller
from .scheduler import Scheduler
from .server import DashboardServer
from .state import LiveState

log = logging.getLogger("wdgwars.app")
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _make_icon(active: bool = True, busy: bool = False) -> Image.Image:
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    ring = (255, 45, 149, 255) if busy else ((0, 229, 255, 255) if active else (127, 140, 141, 255))
    d.ellipse((6, 6, size - 6, size - 6), outline=ring, width=5)
    d.ellipse((22, 22, size - 22, size - 22), outline=ring, width=3)
    d.line((size // 2, size // 2, size - 12, 14), fill=ring, width=4)
    d.ellipse((size // 2 - 4, size // 2 - 4, size // 2 + 4, size // 2 + 4), fill=ring)
    return img


class UploaderApp:
    def __init__(self) -> None:
        self.cfg = Config()
        self.state = LiveState()
        self.core = UploaderCore(self.cfg, self.state)
        self.scheduler = Scheduler(self.cfg, self._run_cycle)
        self.poller = Poller(self.core)
        self.server = DashboardServer(self)
        self.updater = updater.Updater(on_quit=self.quit, on_event=self.state.event)
        self.state.update_status = self.updater.status
        self._icon: pystray.Icon | None = None
        self._stop = threading.Event()

    # -- lifecycle ---------------------------------------------------------
    def run(self, open_dash: bool = True) -> None:
        autostart.sync(bool(self.cfg.get("start_on_boot")))
        watchdog.clear_clean_stop()   # a later crash counts as unexpected
        self.core.poll_devices()
        try:
            url = self.server.start()
        except Exception:
            log.exception("dashboard server failed to start")
            url = ""
        self.scheduler.start()
        self.poller.start()
        # keep scheduler's next-run visible in state
        threading.Thread(target=self._status_loop, name="wdgw-status", daemon=True).start()

        if url and open_dash:
            self.open_dashboard()

        watchdog.spawn(os.getpid())   # relaunch on an unexpected exit
        threading.Thread(target=self._update_loop, name="wdgw-updates", daemon=True).start()

        self._popup = traymenu.TrayPopup("Wardrive Uploader", __version__, self._tray_model, {
            "upload": self.trigger_upload,
            "dashboard": self.open_dashboard,
            "map": lambda: self.server.url and webbrowser.open(self.server.url.rstrip("/") + "/map"),
            "interval": self.set_interval,
            "boot": self._toggle_boot,
            "folder": self._open_folder,
            "tpp": lambda: webbrowser.open("https://discord.gg/thepirates"),
            "update": self.updater.install,
            "site": lambda: webbrowser.open("https://rocketgod.dev"),
            "quit": self.quit,
        })
        self._icon = traymenu.TrayIcon(APP_NAME, self._tray_icon(True, False), APP_NAME,
                                       menu=self._menu(), on_right_click=self._popup.show)
        self._icon.run()  # blocks main thread until stop()

    def _status_loop(self) -> None:
        while not self._stop.is_set():
            watchdog.beat()   # liveness heartbeat so the watchdog can spot a hang
            self.state.next_run_ts = self.scheduler.next_run_ts
            self.state.interval_hours = int(self.cfg.get("interval_hours", 4))
            if self._icon:
                det = self.core.last_detect
                active = bool(det and (det.adsb_radio_present or det.mesh_present)) and bool(self.cfg.get("api_key"))
                self._icon.icon = self._tray_icon(active, self.state.upload_in_progress)
                self._icon.title = f"{APP_NAME} — {self._status_text()}"
            self._stop.wait(4.0)

    def _update_loop(self) -> None:
        """Check for a new release shortly after start, then every few hours."""
        if self._stop.wait(45):
            return
        told = ""
        while True:
            st = self.updater.check()
            if st.get("available") and st.get("latest") != told:
                told = st["latest"]
                self.state.event(f"Update {told} is available")
                if self._icon:
                    try:
                        self._icon.notify(f"Version {told} is ready. Install it from the dashboard or the tray menu.",
                                          "Wardrive Uploader update")
                    except Exception:
                        pass
            if self._stop.wait(updater.CHECK_EVERY):
                return

    # -- tray --------------------------------------------------------------
    @staticmethod
    def _tray_icon(active: bool, busy: bool) -> Image.Image:
        return traymenu.tray_image("busy" if busy else ("on" if active else "")) or _make_icon(active, busy)

    def _tray_model(self) -> dict:
        """Live values for the styled tray menu."""
        s = self.state.snapshot()
        dev, live, pend = s.get("devices") or {}, s.get("live") or {}, s.get("pending") or {}
        sdr, pp, mesh = dev.get("rtlsdr") or {}, dev.get("portapack") or {}, dev.get("mesh") or {}
        air = live.get("aircraft", 0)
        if sdr.get("present"):
            adsb_row = ("warn" if sdr.get("driver_ok") is False else "on", "ADS-B", f"RTL-SDR · {air} in view")
        elif pp.get("present"):
            adsb_row = ("on" if pp.get("connected") else "warn", "ADS-B", f"PortaPack · {air} in view")
        else:
            adsb_row = ("off", "ADS-B", "no radio")
        mdevs = mesh.get("devices") or []
        conn = sum(1 for d in mdevs if d.get("connected"))
        heard = live.get("meshtastic", 0) + live.get("meshcore", 0)
        if mdevs:
            mesh_row = ("on" if conn else "warn", "Mesh", f"{conn}/{len(mdevs)} radios · {heard:,} heard")
        else:
            mesh_row = ("on" if mesh.get("connected") else "off", "Mesh",
                        f"{heard:,} heard" if mesh.get("present") else "no radio")
        busy = bool(s.get("upload_in_progress"))
        nxt = s.get("next_run_ts") or 0
        next_row = ("busy" if busy else "", "Next upload",
                    "uploading…" if busy else (f"in {traymenu.fmt_duration(nxt - time.time())}" if nxt else "—"))
        queue_row = ("on" if (pend.get("aircraft") or pend.get("mesh")) else "", "Staged",
                     f"{pend.get('aircraft', 0):,} aircraft · {pend.get('mesh', 0):,} mesh")
        games = []
        st = s.get("stats") or {}
        if st.get("username"):
            r = (st.get("your_rank") or {}).get("all_time")
            games.append({"game": "WDGWARS", "name": st["username"], "color": traymenu.CYAN,
                          "rank": f"#{r} all-time" if r else ""})
        wd = s.get("wardrift") or {}
        ch = (wd.get("account") or {}).get("character") or {}
        if wd.get("session") == "ok" and ch.get("name"):
            me = (wd.get("leaderboard") or {}).get("me") or {}
            games.append({"game": "WARDRIFT", "name": f"{ch['name']} · Lv {ch.get('level') or '?'}",
                          "color": traymenu.GOLD, "rank": f"#{me['rank']} global" if me.get("rank") else ""})
        return {"adsb": adsb_row, "mesh": mesh_row, "queue": queue_row, "next": next_row,
                "games": games, "activity": "uploading" if busy else "online",
                "interval": int(self.cfg.get("interval_hours", 4)), "boot": autostart.is_enabled(),
                "update": (self.updater.status.get("latest") if self.updater.status.get("available")
                           and self.updater.status.get("can_install") else "")}

    def _status_text(self) -> str:
        d = self.core.last_detect
        p = self.state.pending()
        bits = []
        if d:
            if d.rtlsdr_present:
                bits.append("RTL-SDR OK")
            elif d.portapack_present:
                bits.append("PortaPack OK")
            else:
                bits.append("no SDR")
            bits.append("Mesh OK" if d.mesh_present else "no Mesh")
        bits.append(f"queued {p['aircraft']}+{p['mesh']}")
        return " · ".join(bits)

    def _menu(self) -> pystray.Menu:
        def interval_item(h: int):
            label = f"{h} hour" if h == 1 else f"{h} hours"
            return pystray.MenuItem(label, lambda i, it: self.set_interval(h),
                                    checked=lambda it, hh=h: int(self.cfg.get("interval_hours", 4)) == hh,
                                    radio=True)
        return pystray.Menu(
            pystray.MenuItem(lambda it: self._status_text(), None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open dashboard", lambda i, it: self.open_dashboard(), default=True),
            pystray.MenuItem("Upload now", lambda i, it: self.trigger_upload()),
            pystray.MenuItem("Interval", pystray.Menu(*[interval_item(h) for h in INTERVAL_CHOICES_HOURS])),
            pystray.MenuItem("Start on Windows boot", lambda i, it: self._toggle_boot(),
                             checked=lambda it: autostart.is_enabled()),
            pystray.MenuItem("Open log / config folder", lambda i, it: self._open_folder()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", lambda i, it: self.quit()),
        )

    # -- actions used by tray + server ------------------------------------
    def _run_cycle(self) -> None:
        self.core.run_cycle()

    def trigger_upload(self) -> None:
        self.scheduler.trigger_now()

    def set_interval(self, hours: int) -> None:
        self.cfg.set("interval_hours", int(hours))
        self.cfg.save()
        self.scheduler.reschedule()

    def update_config(self, values: dict) -> None:
        allowed = {
            "api_key", "wardrift_api_key", "upload_device_label", "interval_hours", "adsb_enabled", "adsb_json_url", "adsb_json_file",
            "adsb_source", "mayhem_port", "mayhem_log_path", "mayhem_poll_secs",
            "mayhem_autostart_app", "adsb_home_lat", "adsb_home_lon",
            "dump1090_exe", "dump1090_args", "dump1090_autostart", "mesh_enabled",
            "mesh_transport", "mesh_port", "mesh_baud", "mesh_ble_address",
            "mesh_advert_minutes", "mesh_devices", "mesh_hop_limit",
            "dump1090_gain", "start_on_boot", "upload_on_start",
        }
        clean = {k: v for k, v in values.items() if k in allowed}
        key_changed = "api_key" in clean and clean["api_key"] != self.cfg.get("api_key")
        self.cfg.update(clean)
        self.cfg.save()
        autostart.sync(bool(self.cfg.get("start_on_boot")))
        self.scheduler.reschedule()
        self.core.poll_devices()
        if key_changed:
            threading.Thread(target=self.core.refresh_stats, daemon=True).start()

    def wardrift_sign_in(self, username: str, password: str) -> dict:
        """Sign in to Wardrift for stats. Only the session token is saved."""
        from . import wardrift
        username = (username or "").strip()
        if not username or not password:
            return {"ok": False, "error": "enter your Wardrift username and password"}
        try:
            s = wardrift.sign_in(username, password)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}
        self.cfg.update({"wardrift_session": s["token"], "wardrift_session_expires": s["expires_at"],
                         "wardrift_username": s["username"]})
        self.cfg.save()
        self.core.poll_devices()   # refresh the config summary
        threading.Thread(target=self.core.refresh_wardrift, daemon=True).start()
        return {"ok": True, "username": s["username"]}

    def wardrift_sign_out(self) -> None:
        from . import wardrift
        token = self.cfg.get("wardrift_session") or ""
        self.cfg.update({"wardrift_session": "", "wardrift_session_expires": ""})
        self.cfg.save()
        self.core.refresh_wardrift()
        self.core.poll_devices()
        if token:
            threading.Thread(target=wardrift.sign_out, args=(token,), daemon=True).start()

    def install_dump1090_async(self) -> None:
        def work():
            try:
                exe = adsb.install_dump1090(progress=lambda m: self.state.event(f"dump1090: {m}"))
                self.state.event(f"dump1090 ready: {exe}")
                self.core.poll_devices()
            except Exception as e:
                self.state.event(f"dump1090 install failed: {e}")
                log.exception("dump1090 install failed")
        threading.Thread(target=work, daemon=True).start()

    def open_dashboard(self) -> None:
        url = self.server.url
        if not url:
            return
        # Prefer a native window (pywebview) launched as a separate process so it
        # never contends with pystray's main-thread loop; fall back to browser.
        try:
            import webview  # noqa: F401
            launcher = [sys.executable]
            if not getattr(sys, "frozen", False):
                launcher.append(str(Path(__file__).resolve().parent.parent / "run_app.py"))
            launcher += ["--window", url]
            subprocess.Popen(launcher, creationflags=_NO_WINDOW)
        except Exception:
            webbrowser.open(url)

    def _toggle_boot(self) -> None:
        new = not autostart.is_enabled()
        autostart.sync(new)
        self.cfg.set("start_on_boot", new)
        self.cfg.save()

    def _open_folder(self) -> None:
        try:
            os.startfile(str(config_dir()))  # type: ignore[attr-defined]
        except Exception:
            log.exception("could not open config folder")

    def quit(self) -> None:
        log.info("shutting down")
        watchdog.mark_clean_stop()   # tell the watchdog this exit is intentional
        self._stop.set()
        try:
            self.state.persist(force=True)   # never lose the un-uploaded queue
            self.state.save_collected(force=True)
        except Exception:
            pass
        for stopper in (self.scheduler.stop, self.poller.stop, self.core.capture.stop,
                        self.server.stop, adsb.stop_dump1090, mayhem.stop):
            try:
                stopper()
            except Exception:
                pass
        if self._icon:
            try:
                self._icon.stop()
            except Exception:
                pass


def open_window(url: str) -> None:
    """Native dashboard window (separate process). Requires pywebview."""
    import webview
    webview.create_window("Wardrive Uploader", url, width=1300, height=880, min_size=(900, 640))
    webview.start()


_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _kill_stale_app_instances() -> None:
    """Frozen build only: forcibly end any OTHER WDGWarsUploader *app* instances,
    leaving watchdogs and this process alone. A serial deadlock can leave a hung
    zombie that still holds the COM port and the dashboard socket; a restart must
    clear it before reopening them, or the new instance comes up crippled - no
    dashboard, no mesh.
    """
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return
    me = os.getpid()
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='WDGWarsUploader.exe'\" | "
          "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                             capture_output=True, text=True, timeout=15, creationflags=_NO_WINDOW)
    except Exception:
        return
    for line in (out.stdout or "").splitlines():
        parts = line.strip().split("\t", 1)
        if not parts or not parts[0]:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        cmd = parts[1] if len(parts) > 1 else ""
        if pid == me or "--watchdog" in cmd:
            continue   # never kill this process or a watchdog sidecar
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=10, creationflags=_NO_WINDOW)
            log.warning("cleared stale app instance pid %s", pid)
        except Exception:
            pass


def main(open_dash: bool = True, takeover: bool = False) -> None:
    setup_logging()
    log.info("%s v%s starting%s", APP_NAME, __version__, " (takeover)" if takeover else "")
    if takeover:
        # A watchdog restart or logon autostart: kill any hung predecessor first so
        # the COM port and port 8787 are free.
        _kill_stale_app_instances()
        time.sleep(1.5)   # let the OS release the port + socket handles
    lock = config_dir() / "app.lock"
    # A takeover launch skips the lock because it has just killed any hung
    # predecessor - but that kill only exists for the frozen build. From source,
    # ignoring the lock would start a second copy alongside a live first one, so
    # the lock is respected there.
    can_clear = getattr(sys, "frozen", False)
    if (not takeover or not can_clear) and lock.exists():
        try:
            pid = int(lock.read_text().strip() or "0")
            if pid and _pid_alive(pid):
                # Already running: surface the running instance's dashboard
                # rather than starting a second one.
                log.info("already running (pid %s)%s", pid,
                         "; opening dashboard" if open_dash else "")
                if open_dash:
                    try:
                        webbrowser.open("http://127.0.0.1:8787/")
                    except Exception:
                        pass
                return
        except Exception:
            pass
    try:
        lock.write_text(str(os.getpid()))
    except Exception:
        pass
    try:
        UploaderApp().run(open_dash=open_dash)
    finally:
        try:
            lock.unlink()
        except Exception:
            pass


def _pid_alive(pid: int) -> bool:
    if sys.platform != "win32":
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW)
        return str(pid) in (out.stdout or "")
    except Exception:
        return False


if __name__ == "__main__":
    main()
