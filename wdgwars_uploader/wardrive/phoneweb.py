"""Phone GPS from the phone's own browser, no app needed.

The dashboard shows a QR code that opens a small page this PC serves over HTTPS
on the local network. The page streams the phone's location here. Browsers only
give a page the location over HTTPS, so the PC makes its own certificate; the
phone shows a one-time warning about it. The key in the QR link keeps other
devices on the network from feeding positions.
"""

from __future__ import annotations

import datetime
import ipaddress
import json
import logging
import secrets
import socket
import ssl
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from ..config import config_dir

log = logging.getLogger("wdgwars.phoneweb")

PORT = 10111


def phone_key() -> str:
    p = config_dir() / "phone_gps_key.txt"
    try:
        k = p.read_text(encoding="ascii").strip()
        if len(k) >= 16:
            return k
    except OSError:
        pass
    k = secrets.token_urlsafe(18)
    try:
        p.write_text(k, encoding="ascii")
    except OSError:
        pass
    return k


def _cert(ips: list[str]) -> tuple[Path, Path]:
    """A self-signed certificate for this PC, made once and kept."""
    crt, key = config_dir() / "phone_gps_cert.pem", config_dir() / "phone_gps_key.pem"
    if crt.exists() and key.exists():
        return crt, key
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    pk = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Wardrive Uploader")])
    alt = [x509.DNSName(socket.gethostname()), x509.DNSName("localhost")]
    for ip in ips + ["127.0.0.1"]:
        try:
            alt.append(x509.IPAddress(ipaddress.ip_address(ip)))
        except ValueError:
            pass
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(pk.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName(alt), critical=False)
            .sign(pk, hashes.SHA256()))
    key.write_bytes(pk.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption()))
    crt.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return crt, key


_WEBUI = Path(__file__).resolve().parent.parent / "webui"


def _asset(name: str) -> bytes:
    try:
        return (_WEBUI / name).read_bytes()
    except OSError:
        return b""


class PhoneWeb:
    """HTTPS server for the phone page. ``on_fix(dict, peer)`` gets each position."""

    def __init__(self, on_fix: Callable[[dict[str, Any], str], None], port: int = PORT) -> None:
        self.on_fix, self.port = on_fix, port
        self.key = phone_key()
        self.error = ""
        self._srv: ThreadingHTTPServer | None = None

    def start(self, ips: list[str]) -> None:
        try:
            crt, key = _cert(ips)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(str(crt), str(key))
            owner = self

            class H(BaseHTTPRequestHandler):
                def log_message(self, fmt: str, *args: Any) -> None:
                    log.debug("%s - %s", self.address_string(), fmt % args)

                def _key_ok(self) -> bool:
                    q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                    return secrets.compare_digest((q.get("k") or [""])[0], owner.key)

                def _send(self, code: int, ctype: str, body: bytes) -> None:
                    self.send_response(code)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(body)

                def do_GET(self) -> None:
                    path = urllib.parse.urlsplit(self.path).path
                    if path == "/tpp.png":
                        return self._send(200, "image/png", _asset("static/tpp.png"))
                    if path != "/":
                        return self._send(404, "text/plain", b"not found")
                    if not self._key_ok():
                        return self._send(403, "text/html; charset=utf-8",
                                          b"<h3 style='font-family:sans-serif'>Scan the QR code on the PC again.</h3>")
                    self._send(200, "text/html; charset=utf-8", _asset("phone.html") or b"phone.html missing")

                def do_POST(self) -> None:
                    if urllib.parse.urlsplit(self.path).path != "/fix" or not self._key_ok():
                        return self._send(403, "text/plain", b"forbidden")
                    try:
                        n = min(int(self.headers.get("Content-Length") or 0), 4096)
                        fix = json.loads(self.rfile.read(n) or b"{}")
                        owner.on_fix(fix, self.client_address[0])
                    except (ValueError, TypeError) as e:
                        return self._send(400, "text/plain", str(e).encode())
                    self._send(204, "text/plain", b"")

            srv = ThreadingHTTPServer(("0.0.0.0", self.port), H)
            # The TLS handshake runs on each request's own thread, so a client that
            # stalls mid-handshake cannot hold up the others.
            srv.socket = ctx.wrap_socket(srv.socket, server_side=True, do_handshake_on_connect=False)
            srv.daemon_threads = True
            self._srv = srv
            threading.Thread(target=srv.serve_forever, name="phone-gps-web", daemon=True).start()
            self.error = ""
        except Exception as e:  # noqa: BLE001 - port taken, certificate trouble
            self.error = f"phone page unavailable: {e}"
            log.warning(self.error)

    def stop(self) -> None:
        if self._srv:
            try:
                self._srv.shutdown()
                self._srv.server_close()
            except Exception:
                pass
            self._srv = None

    def url(self, ip: str) -> str:
        return f"https://{ip}:{self.port}/?k={self.key}"


def qr_svg(text: str) -> str:
    import segno
    return segno.make(text, error="m").svg_inline(scale=6, dark="#000", light="#fff", border=2)

