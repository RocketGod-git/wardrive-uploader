"""Local dashboard server.

A tiny threaded HTTP server bound to 127.0.0.1 that serves the single-page
dashboard and a small JSON API the page polls. It exposes no data beyond what
the logged-in user already owns, and the API key is write-only over this API
(never returned). Bound to loopback only.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

log = logging.getLogger("wdgwars.server")

_WEBDIR = Path(__file__).resolve().parent / "webui"


def _load(name: str, fallback: bytes) -> bytes:
    try:
        return (_WEBDIR / name).read_bytes()
    except OSError:
        return fallback


class _Handler(BaseHTTPRequestHandler):
    server_version = "WDGWarsUploader"

    # Silence default logging to stderr; route it to the app logger at debug.
    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("%s - %s", self.address_string(), fmt % args)

    # -- helpers -----------------------------------------------------------
    def _send_json(self, obj: Any, code: int = 200) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # The dashboard is read fresh at startup, so an updated build is served
        # immediately -- but a cached copy in the browser hides it until a hard
        # refresh.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length", "0") or "0")
            if n <= 0:
                return {}
            raw = self.rfile.read(n)
            return json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, json.JSONDecodeError):
            return {}

    # -- CSRF / DNS-rebinding defense (loopback-only app) ------------------
    def _allowed_hosts(self) -> set[str]:
        port = self.server.server_port
        return {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}

    def _host_ok(self) -> bool:
        """Block DNS-rebinding: the Host header must be the loopback address, not
        an attacker's domain that happens to resolve to 127.0.0.1."""
        return (self.headers.get("Host") or "").lower() in self._allowed_hosts()

    def _csrf_ok(self) -> bool:
        """For mutations: reject cross-site requests. A browser sends Origin on
        cross-origin POSTs; requiring JSON also defeats 'simple request' CSRF."""
        origin = self.headers.get("Origin")
        if origin is not None:
            allowed = {f"http://{h}" for h in self._allowed_hosts()}
            if origin.lower() not in allowed:
                return False
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        return ctype == "application/json"

    @property
    def app(self):
        return self.server.app  # type: ignore[attr-defined]

    # -- routing -----------------------------------------------------------
    def do_GET(self) -> None:
        if not self._host_ok():
            self._send_json({"error": "forbidden"}, 403)
            return
        parts = urllib.parse.urlparse(self.path)
        path = parts.path
        if path in ("/", "/index.html"):
            self._send_html(self.server.html)          # type: ignore[attr-defined]
        elif path == "/map":
            self._send_html(self.server.map_html)      # type: ignore[attr-defined]
        elif path == "/api/state":
            self._send_json(self.app.state.snapshot())
        elif path == "/api/config":
            self._send_json(self.app.core.state.config_summary)
        elif path == "/api/aircraft-info":
            q = urllib.parse.parse_qs(parts.query)
            hex_id = (q.get("hex") or [""])[0]
            cs = (q.get("cs") or [""])[0]
            from . import enrich
            self._send_json(enrich.full(hex_id, cs))
        elif path == "/api/geocode":
            q = (urllib.parse.parse_qs(parts.query).get("q") or [""])[0]
            from . import geocode
            try:
                self._send_json({"results": geocode.search(q)})
            except Exception as e:  # noqa: BLE001
                self._send_json({"results": [], "error": f"address lookup failed: {e}"})
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        if not self._host_ok() or not self._csrf_ok():
            self._send_json({"error": "forbidden"}, 403)
            return
        path = self.path.split("?", 1)[0]
        body = self._read_body()
        try:
            if path == "/api/upload":
                self.app.trigger_upload()
                self._send_json({"ok": True})
            elif path == "/api/rescan":
                self.app.core.poll_devices()
                self.app.core.poll_aircraft()
                self.app.core.poll_mesh()
                self._send_json({"ok": True, "state": self.app.state.snapshot()})
            elif path == "/api/test-key":
                from .uploader import WDGWarsClient
                key = (body.get("api_key") or self.app.cfg.get("api_key") or "").strip()
                res = WDGWarsClient(key).check_key()
                self._send_json({"ok": res.ok,
                                 "username": res.detail.get("username", ""),
                                 "error": res.error})
            elif path == "/api/config":
                self.app.update_config(body)
                self._send_json({"ok": True, "config": self.app.core.state.config_summary})
            elif path == "/api/radio-position":
                lat, lon = float(body.get("lat")), float(body.get("lon"))
                if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
                    self._send_json({"ok": False, "error": "that is not a valid location"})
                    return
                if body.get("coarse"):
                    # Two decimals is roughly 1 km: close enough for the mesh map,
                    # far enough not to point at a house.
                    lat, lon = round(lat, 2), round(lon, 2)
                n = self.app.core.capture.set_position(lat, lon)
                if not n:
                    self._send_json({"ok": False, "error": "no mesh radio is connected"})
                else:
                    self._send_json({"ok": True, "radios": n, "lat": lat, "lon": lon})
            elif path == "/api/interval":
                h = int(body.get("interval_hours", 4))
                self.app.set_interval(h)
                self._send_json({"ok": True})
            elif path == "/api/refresh-stats":
                self.app.core.refresh_stats()
                self._send_json({"ok": True})
            elif path == "/api/install-dump1090":
                self.app.install_dump1090_async()
                self._send_json({"ok": True})
            elif path == "/api/quit":
                self._send_json({"ok": True})
                threading.Thread(target=self.app.quit, daemon=True).start()
            else:
                self._send_json({"error": "not found"}, 404)
        except Exception as e:
            log.exception("API error on %s", path)
            self._send_json({"error": str(e)}, 500)


class _HTTPServer(ThreadingHTTPServer):
    # Exclusive bind: on Windows the default (SO_REUSEADDR) lets a second process
    # bind the same port, splitting connections. False makes bind() fail when the
    # port is taken, so the port-fallback loop below picks the next free one.
    allow_reuse_address = False
    daemon_threads = True


class DashboardServer:
    def __init__(self, app, host: str = "127.0.0.1", port: int = 8787) -> None:
        self.app = app
        self.host = host
        self.port = port
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    def start(self) -> str:
        html = _load("dashboard.html", b"<h1>dashboard.html missing</h1>")
        map_html = _load("map.html", b"<h1>map.html missing</h1>")
        last_err = None
        for port in range(self.port, self.port + 20):
            try:
                httpd = _HTTPServer((self.host, port), _Handler)
                httpd.app = self.app          # type: ignore[attr-defined]
                httpd.state = self.app.state  # type: ignore[attr-defined]
                httpd.html = html             # type: ignore[attr-defined]
                httpd.map_html = map_html     # type: ignore[attr-defined]
                self._httpd = httpd
                self.port = port
                break
            except OSError as e:
                last_err = e
                continue
        if self._httpd is None:
            raise RuntimeError(f"could not bind dashboard server: {last_err}")
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="wdgw-http", daemon=True)
        self._thread.start()
        log.info("dashboard at %s", self.url)
        return self.url

    def stop(self) -> None:
        if self._httpd:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=3)
