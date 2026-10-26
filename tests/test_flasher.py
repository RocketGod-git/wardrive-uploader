"""Offline tests for the desktop WardriveESP flasher: no board, no network.

Run from the repo root:  python -m pytest -q
"""

from __future__ import annotations

import threading
import time

import pytest

from wdgwars_uploader.flasher import firmware as F
from wdgwars_uploader.flasher import ports as P
from wdgwars_uploader.flasher import probe as PR
from wdgwars_uploader.flasher import service as S
from wdgwars_uploader.flasher.engine import FlashError


# ----------------------------------------------------------------- helpers

def image(chip_id: int, size: int, bl_at: int = 0, app_at: int = 0x20000, table: bool = True) -> bytes:
    b = bytearray(b"\xff" * size)
    for at in (bl_at, app_at):
        if at + 16 <= size:
            b[at] = 0xE9
            b[at + 12] = chip_id & 0xFF
            b[at + 13] = chip_id >> 8
    if table:
        b[0x8000], b[0x8001] = 0xAA, 0x50
    return bytes(b)


C5_IMAGE = image(23, 0x40000, bl_at=0x2000)
COPROC_MERGED = image(0, 0x20000, bl_at=0x1000, app_at=0x10000)

MANIFEST = {
    "latest": "1.6.9", "date": "2026-10-26", "coproc": "0.2.1",
    "coproc_parts": [{"at": "0x1000", "file": "wardrive-coproc-bootloader.bin"},
                     {"at": "0x8000", "file": "wardrive-coproc-partition-table.bin"},
                     {"at": "0x10000", "file": "wardrive_coproc.bin"}],
}


class FakeResp:
    def __init__(self, status=200, body=b"", length=None):
        self.status_code = status
        self.body = body
        self.headers = {"Content-Length": str(len(body) if length is None else length)}

    def iter_content(self, n):
        for i in range(0, len(self.body), n):
            yield self.body[i:i + n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class FakeHttp:
    """URL substring -> response, records every URL asked for."""

    def __init__(self, routes):
        self.routes = routes
        self.urls = []

    def get(self, url, **kw):
        self.urls.append(url)
        for k, v in self.routes.items():
            if k in url:
                return v() if callable(v) else v
        return FakeResp(404)


# ----------------------------------------------------------------- firmware

def test_manifest_parses_latest_and_coproc_parts():
    m = F.parse_manifest(MANIFEST)
    assert m.latest == "1.6.9" and m.coproc == "0.2.1"
    assert m.coproc_parts == [(0x1000, "wardrive-coproc-bootloader.bin"),
                              (0x8000, "wardrive-coproc-partition-table.bin"),
                              (0x10000, "wardrive_coproc.bin")]


def test_coproc_parts_never_reach_into_nvs():
    bad = [{"at": "0x1000", "file": "a.bin"}, {"at": "0x9000", "file": "nvs.bin"}, {"at": "0x10000", "file": "c.bin"}]
    assert F.parse_coproc_parts(bad) is None
    assert F.parse_coproc_parts([{"at": "0x1000", "file": "../evil.bin"}]) is None
    assert F.parse_manifest({"latest": "1.6.9", "coproc": "soon"}).coproc is None


def test_bridge_image_checks():
    F.check_bridge("ESP32-C5", C5_IMAGE)
    with pytest.raises(F.FirmwareError):
        F.check_bridge("ESP32-C6", C5_IMAGE)          # built for another chip
    with pytest.raises(F.FirmwareError):
        F.check_bridge("ESP32-C5", C5_IMAGE[:0x1000])  # too small


def test_download_falls_back_to_the_second_site_and_checks_length():
    http = FakeHttp({
        "WardriveESP/version.json": FakeResp(500),
        "iosESP/version.json": FakeResp(200, b'{"latest":"1.6.9"}'),
        "WardriveESP/bins/esp32c5": FakeResp(200, C5_IMAGE, length=len(C5_IMAGE) + 10),   # cut off
        "iosESP/bins/esp32c5": FakeResp(200, C5_IMAGE),
    })
    src = F.FirmwareSource(session=http)
    assert src.fetch_manifest().latest == "1.6.9"
    assert src.bridge("ESP32-C5", "1.6.9") == C5_IMAGE
    assert any("iosESP/bins/esp32c5/wardrive-bridge-merged.bin?v=1.6.9" in u for u in http.urls)
    n = len(http.urls)
    src.bridge("ESP32-C5", "1.6.9")                    # cached: no second download
    assert len(http.urls) == n


def test_coproc_wipe_writes_merged_at_zero_and_update_keeps_nvs():
    parts = {
        "wardrive-coproc-bootloader.bin": image(0, 0x5000, app_at=0x7FFF, table=False),
        "wardrive-coproc-partition-table.bin": b"\xaa\x50" + b"\xff" * 0xBFE,
        "wardrive_coproc.bin": image(0, 0x30000, app_at=0x3FFFF, table=False),
    }
    http = FakeHttp({f"esp32/{k}": FakeResp(200, v) for k, v in parts.items()}
                    | {"wardrive-coproc-merged.bin": FakeResp(200, COPROC_MERGED)})
    src = F.FirmwareSource(session=http, sites=("https://x",))
    m = F.parse_manifest(MANIFEST)
    wipe = src.coproc("0.2.1", True, m.coproc_parts)
    assert [a for a, _ in wipe] == [0x0]
    upd = src.coproc("0.2.1", False, m.coproc_parts)
    assert [a for a, _ in upd] == [0x1000, 0x8000, 0x10000]
    assert all(not (a < 0xF000 and a + len(d) > 0x9000) for a, d in upd)   # NVS untouched


def test_coproc_parts_failure_cuts_regions_from_merged():
    http = FakeHttp({"wardrive-coproc-merged.bin": FakeResp(200, COPROC_MERGED)})
    src = F.FirmwareSource(session=http, sites=("https://x",))
    upd = src.coproc("0.2.1", False, F.parse_manifest(MANIFEST).coproc_parts)
    assert [(a, len(d)) for a, d in upd] == [(0x1000, 0x8000), (0x10000, len(COPROC_MERGED) - 0x10000)]


# ----------------------------------------------------------------- ports

def port(dev, vid, pid, sn="", loc=""):
    return P.Port(device=dev, vid=vid, pid=pid, serial=sn, location=loc)


def test_biscuit_chips_pair_by_hub():
    ps = [port("COM7", 0x303A, 0x1001, "38:44:BE:31:5F:20", "1-3.1:x.0"),
          port("COM6", 0x1A86, 0x7523, "", "1-3.2"),
          port("COM9", 0x10C4, 0xEA60, "0001", "1-4")]
    devs = P.group(ps)
    pair = [d for d in devs if d.pair]
    assert len(pair) == 1 and pair[0].main.device == "COM7" and pair[0].wroom.device == "COM6"
    assert any(d.main.device == "COM9" and not d.pair for d in devs)


def test_two_biscuits_on_two_hubs_stay_two():
    ps = [port("COM7", 0x303A, 0x1001, "a", "1-3.1:x.0"), port("COM6", 0x1A86, 0x7523, "", "1-3.2"),
          port("COM11", 0x303A, 0x1001, "b", "1-5.1:x.0"), port("COM10", 0x1A86, 0x7523, "", "1-5.2")]
    pairs = sorted((d.main.device, d.wroom.device) for d in P.group(ps) if d.pair)
    assert pairs == [("COM11", "COM10"), ("COM7", "COM6")]


def test_separate_boards_on_different_hubs_do_not_pair():
    ps = [port("COM7", 0x303A, 0x1001, "a", "1-3.1:x.0"), port("COM6", 0x1A86, 0x7523, "", "1-4.2")]
    assert not any(d.pair for d in P.group(ps))


def test_winusb_c5_pairs_with_the_only_ch340():
    c5 = P.Port(device="usbjtag://38:44:BE:31:5F:20", vid=0x303A, pid=0x1001, serial="38:44:BE:31:5F:20",
                location="3.1", winusb=True)
    devs = P.group([c5, port("COM6", 0x1A86, 0x7523, "", "1-3.2")])
    assert len(devs) == 1 and devs[0].pair and devs[0].main.label == "No COM port (WinUSB)"


# ----------------------------------------------------------------- parsers

CAPS = ("I (1299) WardriveESP: WD:CAPS fw=1.6.8 chip=esp32c5 bands=2g,5g ota=1 bt=1 gps=0 cap=1 usb=1 "
        "board=biscuit_ultra board_name=Biscuit_Ultra batt=1 coproc=wifi link=usb chans=1,6,11\n")
STOCK_WROOM = ("rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)\n"
               "BISCUIT V1 - WROOM BLE Gateway  Version: v1.6.2\nDevice name: Biscuit Ultra\n")


def test_caps_and_health():
    assert PR.caps_of(CAPS)["board"] == "biscuit_ultra"
    assert PR.caps_of(CAPS[:-1]) is None              # not complete yet
    assert PR.caps_of("WD:CAPS fw=1.0 usb=0\n") is None
    h = PR.health_of("WD:HEALTH up=30 heap=1 batt=87 batt_mv=4012 batt_rate=-1.2\n")
    assert h["batt"] == "87" and h["batt_mv"] == "4012"


def test_wroom_log_stock_and_coproc():
    w = PR.parse_wroom_log(STOCK_WROOM)
    assert w == {"board": "biscuit_ultra", "coproc_fw": None, "stock_version": "v1.6.2", "mode": None}
    assert PR.parse_wroom_log("BISCUIT V1 - WROOM BLE Gateway\nDevice name: Biscuit Pro\n")["board"] == "biscuit_pro"
    h = PR.parse_wroom_log("WDCP hello proto=1 board=biscuit_dual fw=0.2.0 chip=esp32 gauge=1 scan=1 mode=wifi mac=x\n")
    assert h["board"] == "biscuit_dual" and h["coproc_fw"] == "0.2.0" and h["mode"] == "wifi"
    assert PR.parse_wroom_log("ets Jun  8 2016\nHello from some ESP32\n") is None


def test_mesh_and_stock_fingerprints():
    assert PR.MESH_RE.search("INFO | 12:00:01 [PowerFSM] Enter state: ON")
    assert PR.MESH_RE.search("BLE: SerialBLEInterface -> connecting...")
    assert PR.stock_c5_of("*** NEW FIRMWARE BUILD ***  Board: Biscuit Ultra  LED: NeoPixel") == "Biscuit Ultra"
    assert PR.stock_c5_of("[WD-HEALTH] wifi_cb=1 accepted=2") == "Biscuit Crumb"


class FakeSerial:
    """A board on the other end of a serial port, answering line by line."""

    def __init__(self, answer, boot=""):
        self.answer = answer
        self.out = bytearray(boot.encode())
        self.sent = []
        self.rts = self.dtr = False
        self.closed = False

    def read(self, n):
        if not self.out:
            time.sleep(0.01)
            return b""
        b = bytes(self.out[:n])
        del self.out[:n]
        return b

    def write(self, data):
        for line in data.decode().replace("\r", "").split("\n"):
            if line:
                self.sent.append(line)
                self.out += self.answer(line).encode()
        return len(data)

    def close(self):
        self.closed = True


def test_probe_firmware_reads_caps_and_says_bye(monkeypatch):
    fake = FakeSerial(lambda l: CAPS if l == "wdusb hello" else
                      ("WardriveESP 1.6.8  ·  esp32c5 rev 1.0  ·  WardriveESP-5F20\n" if l == "info" else ""))
    monkeypatch.setattr(PR, "open_quiet", lambda dev, baud=115200: fake)
    fw = PR.probe_firmware("COM7", listen=0.05)
    assert fw.kind == "wardrive" and fw.version == "1.6.8" and fw.chip == "ESP32-C5"
    assert fw.board_name == "Biscuit Ultra" and fw.name == "WardriveESP-5F20"
    assert fake.sent[-1] == "wdusb bye" and fake.closed


def test_probe_firmware_spots_meshtastic(monkeypatch):
    fake = FakeSerial(lambda l: "", boot="INFO  | ??:??:?? 3 [SerialConsole] Opening serial\n")
    monkeypatch.setattr(PR, "open_quiet", lambda dev, baud=115200: fake)
    fw = PR.probe_firmware("COM5", listen=0.05, hellos=1)
    assert fw.kind == "mesh" and fw.detail == "Meshtastic"


def test_finish_coproc_sends_setboard_when_nvs_was_wiped(monkeypatch):
    def answer(line):
        if line == "WDCP setboard biscuit_ultra":
            return "board set to biscuit_ultra\nWDCP hello proto=1 board=biscuit_ultra fw=0.2.1 chip=esp32 gauge=1 scan=1 mode=wifi mac=x\n"
        return ""
    fake = FakeSerial(answer, boot="WDCP hello proto=1 board=biscuit_dual fw=0.2.1 chip=esp32 gauge=1 scan=1 mode=wifi mac=x\n")
    monkeypatch.setattr(PR, "open_quiet", lambda dev, baud=115200: fake)
    hello = PR.finish_coproc("COM6", "biscuit_ultra")
    assert "WDCP setboard biscuit_ultra" in fake.sent
    assert hello["board"] == "biscuit_ultra" and hello["fw"] == "0.2.1"


def test_verify_reads_battery_from_health(monkeypatch):
    fake = FakeSerial(lambda l: (CAPS + "WD:HEALTH up=5 heap=1 batt=87 batt_mv=4012\n") if l == "wdusb hello" else "")
    monkeypatch.setattr(PR, "open_quiet", lambda dev, baud=115200: fake)
    res = PR.verify_wardrive("COM7", wait=3, battery_wait=1)
    assert res["caps"]["coproc"] == "wifi" and res["health"]["batt"] == "87"
    assert fake.sent[-1] == "wdusb bye"


# ----------------------------------------------------------------- service

class Clock:
    def __init__(self):
        self.t = 1000.0
        self.slept = 0.0

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s
        self.slept += s


class FakeSource:
    MANIFEST_TTL = 300

    def __init__(self, coproc="0.2.1"):
        self.manifest = F.parse_manifest(dict(MANIFEST, coproc=coproc))
        self.calls = []

    def fetch_manifest(self, force=False):
        return self.manifest

    def bridge(self, chip, version, progress=None):
        self.calls.append(("bridge", chip, version))
        return C5_IMAGE

    def coproc(self, version, wipe, parts, progress=None):
        self.calls.append(("coproc", version, wipe))
        return [(0x0, COPROC_MERGED)] if wipe else [(0x1000, b"\xe9" * 16), (0x8000, b"\xaa\x50"), (0x10000, b"\xe9" * 16)]


class Guard:
    def __init__(self):
        self.paused, self.resumed = [], []

    def pause_ports(self, ports):
        self.paused += ports
        return ports

    def resume_ports(self, ports):
        self.resumed += ports


def biscuit_ports():
    return [port("COM7", 0x303A, 0x1001, "38:44:BE:31:5F:20", "1-3.1:x.0"), port("COM6", 0x1A86, 0x7523, "", "1-3.2")]


@pytest.fixture
def rig(monkeypatch):
    """A Biscuit Ultra on stock firmware, with every board conversation faked."""
    events = []
    sessions = []

    class FakeSession:
        fail_next = []

        def __init__(self, device, transport, classic=False, fast=True, stub=True, on_line=None):
            self.device, self.transport, self.classic, self.fast, self.stub = device, transport, classic, fast, stub
            self.chip = ""
            self.info = {"mac": "38:44:be:31:5f:20"}
            sessions.append(self)
            events.append(("open", device, fast))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            events.append(("close", self.device))

        def connect(self):
            if FakeSession.fail_next:
                raise FakeSession.fail_next.pop(0)
            self.chip = "ESP32" if self.classic else "ESP32-C5"
            return self.chip

        def flash(self, regions, on_progress):
            events.append(("flash", self.device, [a for a, _ in regions]))
            on_progress("writing", 0.5)
            on_progress("writing", 1.0)
            return ["md5"] * len(regions)

        def restart(self):
            events.append(("restart", self.device))

    monkeypatch.setattr(S, "EspSession", FakeSession)
    monkeypatch.setattr(PR, "port_free", lambda d: True)
    monkeypatch.setattr(PR, "probe_firmware", lambda d: PR.Firmware(kind="biscuit_stock", board_name="Biscuit Ultra"))
    monkeypatch.setattr(PR, "probe_wroom", lambda d: PR.parse_wroom_log(STOCK_WROOM))
    monkeypatch.setattr(PR, "plain_reset", lambda d, native=True: events.append(("plain_reset", d)))
    monkeypatch.setattr(PR, "finish_coproc", lambda d, board: events.append(("finish", d, board)) or
                        {"board": board, "fw": "0.2.1", "mode": "wifi"})
    monkeypatch.setattr(PR, "verify_wardrive", lambda d, wait=15, battery_wait=0: events.append(("verify", d)) or
                        {"caps": PR.caps_of(CAPS.replace("1.6.8", "1.6.9")), "health": {"batt": "87", "batt_mv": "4012"}})
    clock = Clock()
    guard = Guard()
    svc = S.FlasherService(port_guard=guard, ports_in_use=lambda: {"COM7": "MeshCore radio"} if rig_in_use else {},
                           source=FakeSource(), scan=biscuit_ports, sleep=clock.sleep, clock=clock.time)
    return svc, events, sessions, FakeSession, clock, guard


rig_in_use = False


def settle(svc):
    """List the boards and wait for the background probe."""
    svc.state()
    for _ in range(200):
        st = svc.state()
        if not st["scanning"]:
            return st
        time.sleep(0.01)
    raise AssertionError("probe never finished")


def wait_job(svc):
    for _ in range(500):
        j = svc.state()["job"]
        if j and j["state"] != "running":
            return j
        time.sleep(0.01)
    raise AssertionError("job never finished")


def test_biscuit_shows_as_one_device_with_two_chips(rig):
    svc = rig[0]
    st = settle(svc)
    assert len(st["boards"]) == 1
    card = st["boards"][0]
    assert card["biscuit"] and card["title"] == "Biscuit Ultra"
    assert [c["chip"] for c in card["chips"]] == ["ESP32-C5", "ESP32"]
    assert card["chips"][1]["fw"]["text"] == "Stock Biscuit v1.6.2"
    assert card["action"] == "Flash both chips" and card["can_flash"]


def test_one_click_flashes_both_chips_in_order(rig):
    svc, events, sessions, _, clock, guard = rig
    st = settle(svc)
    assert svc.flash(st["boards"][0]["id"])["ok"]
    j = wait_job(svc)
    assert j["state"] == "done", j
    order = [e for e in events if e[0] in ("flash", "restart", "finish", "verify")]
    assert order == [("flash", "COM7", [0x0]), ("restart", "COM7"),
                     ("flash", "COM6", [0x0]), ("restart", "COM6"),        # stock: merged image at 0x0
                     ("finish", "COM6", "biscuit_ultra"), ("verify", "COM7")]
    r = j["result"]
    assert r["version"] == "1.6.9" and r["board_name"] == "Biscuit Ultra" and r["battery"] == "87"
    assert r["second_radio"] == "wifi" and r["coproc_fw"] == "0.2.1" and r["verified"]
    assert sessions[1].classic and sessions[1].transport == "ch34x" and sessions[1].fast


def test_failed_c5_session_plain_resets_and_waits_ten_seconds(rig):
    svc, events, sessions, FakeSession, clock, guard = rig
    st = settle(svc)
    FakeSession.fail_next = [FlashError("The board stopped answering while loading the flasher.", retry_slow=True)]
    svc.flash(st["boards"][0]["id"])
    j = wait_job(svc)
    assert j["state"] == "done"
    i = events.index(("plain_reset", "COM7"))
    assert events[i + 1][0] == "open" and events[i + 1][2] is False     # retry at 115200
    assert sessions[1].stub is False                                      # ROM path, no stub
    assert clock.slept >= S.C5_GAP - 0.01


def test_retry_after_coproc_failure_only_redoes_the_wroom(rig):
    svc, events, sessions, FakeSession, clock, guard = rig
    st = settle(svc)
    bid = st["boards"][0]["id"]
    real_flash = FakeSession.flash
    calls = {"n": 0}

    def flash(self, regions, on_progress):
        if self.classic and calls["n"] == 0:
            calls["n"] += 1
            raise FlashError("Write failed at 40%.")
        return real_flash(self, regions, on_progress)

    FakeSession.flash = flash
    try:
        svc.flash(bid)
        j = wait_job(svc)
        assert j["state"] == "failed" and j["error"].startswith("C5 done. Co-processor:")
        events.clear()
        assert svc.retry()["ok"]
        j = wait_job(svc)
        assert j["state"] == "done"
        assert not any(e[0] == "flash" and e[1] == "COM7" for e in events)   # C5 left alone
        assert ("flash", "COM6", [0x0]) in events
    finally:
        FakeSession.flash = real_flash


def test_mesh_radio_needs_confirmation_and_is_paused(rig, monkeypatch):
    global rig_in_use
    svc, events, sessions, FakeSession, clock, guard = rig
    monkeypatch.setattr(PR, "probe_firmware", lambda d: PR.Firmware(kind="mesh", detail="MeshCore"))
    rig_in_use = True
    try:
        st = settle(svc)
        card = st["boards"][0]
        assert card["needs_confirm"] and card["warn"]
        res = svc.flash(card["id"])
        assert not res["ok"] and res["confirm"]["title"] == "Overwrite the mesh firmware?"
        assert svc.flash(card["id"], confirm=True)["ok"]
        wait_job(svc)
        assert guard.paused == ["COM7"] and guard.resumed == ["COM7"]
    finally:
        rig_in_use = False


def test_port_held_by_another_program_is_named(rig, monkeypatch):
    svc = rig[0]
    st = settle(svc)
    monkeypatch.setattr(PR, "port_free", lambda d: d != "COM6")
    svc.flash(st["boards"][0]["id"])
    j = wait_job(svc)
    assert j["state"] == "failed" and j["error"] == "Another program has COM6 open."
    assert "serial monitor" in j["hint"]


def test_update_over_our_coproc_keeps_nvs(rig, monkeypatch):
    svc, events, sessions, FakeSession, clock, guard = rig
    monkeypatch.setattr(PR, "probe_firmware", lambda d: PR.firmware_from_caps(PR.caps_of(CAPS)))
    monkeypatch.setattr(PR, "probe_wroom", lambda d: PR.parse_wroom_log(
        "WDCP hello proto=1 board=biscuit_ultra fw=0.2.0 chip=esp32 gauge=1 scan=1 mode=wifi mac=x\n"))
    st = settle(svc)
    card = st["boards"][0]
    assert card["action"] == "Update both chips"
    assert card["chips"][0]["fw"] == {"text": "WardriveESP 1.6.8", "tone": "update"}
    svc.flash(card["id"])
    assert wait_job(svc)["state"] == "done"
    assert ("flash", "COM6", [0x1000, 0x8000, 0x10000]) in events
    assert ("coproc", "0.2.1", False) in svc.source.calls


# ----------------------------------------------------------------- mesh capture

def test_capture_pause_releases_the_port_and_resume_gives_it_back():
    from wdgwars_uploader import mesh_capture as MC

    class W(threading.Thread):
        def __init__(self, port):
            super().__init__(daemon=True)
            self.port = port
            self._stop_ev = threading.Event()

        def stop(self):
            self._stop_ev.set()

        def run(self):
            self._stop_ev.wait(5)

    svc = MC.MeshCaptureService()
    w = W("COM7")
    w.start()
    svc._workers["meshcore:COM7"] = w
    assert svc.pause_ports(["COM7"]) == ["COM7"]
    assert not w.is_alive() and not svc._workers
    svc.reconcile([{"protocol": "meshcore", "port": "COM7"}], hop_limit=0, advert_minutes=0)
    assert not svc._workers                      # paused: not restarted
    svc.resume_ports(["COM7"])
    assert "COM7" not in svc._paused


# ----------------------------------------------------------------- engine (esptool mocked)

def test_session_writes_each_region_and_checks_md5(monkeypatch):
    import hashlib

    import esptool.cmds as C
    from wdgwars_uploader.flasher import engine as E

    writes = []
    monkeypatch.setattr(C, "attach_flash", lambda esp: None)
    monkeypatch.setattr(C, "write_flash", lambda esp, ad, **kw: writes.append((ad[0][0], len(ad[0][1]), kw)))

    class Esp:
        def __init__(self, good):
            self.good = good

        def flash_md5sum(self, at, size):
            return hashlib.md5(b"\x5a" * size).hexdigest() if self.good else "0" * 32

    regions = [(0x1000, b"\x5a" * 0x6000), (0x10000, b"\x5a" * 0x20000)]
    seen = []
    s = E.EspSession("COM6", "ch34x", classic=True)
    s.esp = Esp(True)
    md5 = s.flash(regions, lambda st, f: seen.append((st, f)))
    assert [w[0] for w in writes] == [0x1000, 0x10000]
    assert all(w[2] == {"flash_size": "keep", "compress": True} for w in writes)   # no erase-all: NVS stays
    assert len(md5) == 2 and seen[-1] == ("writing", 1.0)
    s.esp = Esp(False)
    with pytest.raises(FlashError, match="Verify failed"):
        s.flash(regions, lambda st, f: None)


def test_usbjtag_ports_report_native_usb_to_esptool():
    from esptool.loader import ESPLoader

    from wdgwars_uploader.flasher import engine as E
    E._patch_usbjtag_vid_pid()
    fake = types_ns(_port=types_ns(port="usbjtag://38:44:BE:31:5F:20"))
    assert ESPLoader.get_usb_vid_pid(fake) == (0x303A, 0x1001)


def types_ns(**kw):
    import types
    return types.SimpleNamespace(**kw)
