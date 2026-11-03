import base64
import io
import threading
import time
import zlib

from wdgwars_uploader.wardrive import biscuit as BI
from wdgwars_uploader.wardrive import esp_proto as P
from wdgwars_uploader.wardrive import serialdev as SD
from wdgwars_uploader.wardrive.boards import BoardSdSource
from wdgwars_uploader.wardrive.sdimport import ImportResult, RemoteFile, SdImport, parse_wigle_csv

WIGLE = (b"WigleWifi-1.4,appRelease=1.7.6,model=AWOK Dual C5,release=1.7.6,device=WardriveESP-D88E,display=,board=,brand=\n"
         b"MAC,SSID,AuthMode,FirstSeen,Channel,RSSI,CurrentLatitude,CurrentLongitude,AltitudeMeters,AccuracyMeters,Type\n"
         b"aa:bb:cc:dd:ee:01,Home,[WPA2-PSK-CCMP][ESS],2026-11-03 13:30:51,6,-61,34.1,-118.2,90,5,WIFI\n"
         b"aa:bb:cc:dd:ee:02,\"Cafe, Free\",[ESS],2026-11-03 13:30:52,149,-70,34.2,-118.3,91,5,WIFI\n"
         b"aa:bb:cc:dd:ee:03,NoFix,[ESS],2026-11-03 13:30:53,1,-70,0,0,0,0,WIFI\n"
         b"aa:bb:cc:dd:ee:04,Early,[ESS],1970-01-01 00:00:09,1,-70,34.2,-118.3,0,0,WIFI\n")


def test_wigle_csv_rows():
    rows, left = parse_wigle_csv(WIGLE)
    assert [r["mac"] for r in rows] == ["aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"]
    assert rows[1]["ssid"] == "Cafe, Free" and rows[1]["ch"] == 149
    assert rows[0]["t"] == 1793712651.0      # FirstSeen is UTC (2026-11-03 13:30:51)
    assert left == 2                          # no position, clock not set


def test_piglet():
    assert P.is_piglet({"fw": "12.83.00"}) and P.piglet_label("12.83.00") == "Piglet v2.83"
    assert P.piglet_label("warhog-1.0") == "Piglet"
    assert not P.is_partner_board({"fw": "1.7.6", "board": "awok_dual_c5"})
    assert P.is_partner_board({"fw": "1.0", "board": "acme_box"}) and P.partner_name({"board": "acme_box"}) == "Acme box"
    assert not P.is_piglet({"fw": "12.83.00", "board": "koko_v8"})


def test_marauder_rows():
    for row in ("-63 Ch: 44 aa:bb:cc:dd:ee:ff ESSID: Home",
                "RSSI: -63 Ch: 44 BSSID: aa:bb:cc:dd:ee:ff ESSID Len: 4 ESSID: Home",
                "> RSSI: -63 Ch: 44 BSSID: aa:bb:cc:dd:ee:ff ESSID: Home"):
        m = SD.AP_RE.match(row)
        assert m and (m.group(1), m.group(2), m.group(4)) == ("-63", "44", "Home"), row
    assert SD.AP_RE.match("-70 Ch: 1 aa:bb:cc:dd:ee:ff ESSID:").group(4) == ""
    assert SD.looks_like_marauder("sniffbeacon  Sniff beacons") and not SD.looks_like_marauder("Build Name: Marauder V8 ghost> ")


def test_ghost_list_and_auth():
    multi = ("[0] SSID: umc,\n     BSSID: 34:53:D2:C4:5D:E7,\n     RSSI: -64,\n     Channel: 157,\n     Band: 5GHz,\n"
             "     Security: WPA2\n     Vendor: ASUS\n"
             "I (82360) WiFiManager: [1] SSID: (Hidden), BSSID: 00:11:22:33:44:55, RSSI: -80, Channel: 6, Security: Open\n")
    recs = SD.parse_ap_list(multi)
    assert recs[0]["bssid"] == "34:53:d2:c4:5d:e7" and recs[0]["ch"] == 157 and recs[0]["auth"] == "[WPA2-PSK-CCMP][ESS]"
    assert recs[0]["maker"] == "ASUS" and recs[1]["ssid"] is None and recs[1]["auth"] == "[ESS]"
    assert SD.ghost_auth("WPA2/WPA3") == "[WPA2-PSK-CCMP][WPA3-SAE-CCMP][ESS]" and SD.ghost_auth("weird") is None
    assert SD.chip_from_text("Hardware: ESP32-C5 DevKit") == ("ESP32-C5", True)


def test_flipper_helpers():
    assert SD.flipper_name("FLIP_ACMASAAT") == "Acmasaat" and SD.flipper_name("ABC") is None
    assert SD.looks_like_flipper_cli("\r\n>: ") and not SD.looks_like_flipper_cli("ghost> ")
    kv = SD.flipper_kv("firmware.origin.fork : Momentum\nfirmware.version : mntm-dev\ncharge.level : 91\n")
    assert SD.flipper_firmware(kv) == "Momentum mntm-dev" and SD.flipper_battery(kv) == (91, "")


def test_biscuit_lines():
    assert BI.parse_ap("My,Net,AA:BB:CC:DD:EE:FF,36,-55,[WPA2_PSK]") == ("My,Net", "aa:bb:cc:dd:ee:ff", 36, -55,
                                                                         "[WPA2-PSK-CCMP][ESS]")
    assert BI.parse_ap(",AA:BB:CC:DD:EE:FF,200,-55,OPEN") is None
    assert BI.parse_bt(",11:22:33:44:55:66,-70,BLE,0201,SF=AB") == ("", "11:22:33:44:55:66", -70)
    assert BI.epoch_time_fw("v1.6.25") and not BI.epoch_time_fw("v1.6.2")
    assert BI.is_biscuit("Biscuit Ultra", []) and BI.is_biscuit(None, [BI.SERVICE]) and not BI.is_biscuit("Biscuits", [])


class FakeSource:
    device_key, device_label, next_time = "flipper:x", "Flipper Zero X", "next plug-in"

    def __init__(self):
        self.files = [RemoteFile("/ext/a.csv", len(WIGLE)), RemoteFile("/ext/notes.txt", 5)]

    def list(self):
        return self.files

    def head(self, f, n):
        return WIGLE[:n] if f.name == "a.csv" else b"hello"

    def read(self, f, out, on_bytes):
        out.write(WIGLE)
        on_bytes(len(WIGLE))
        return len(WIGLE)

    def taken(self, f):
        return True


def test_sd_import_once(tmp_path):
    got = []

    def importer(rows, label, count_as):
        got.append((len(rows), label))
        return ImportResult(len(rows), len(rows), 0)
    sd = SdImport(tmp_path, importer, lambda m: None)
    src = FakeSource()
    files = sd.find_new(src, lambda: False)
    assert [f.name for f in files] == ["a.csv"]
    sd.offer(src, files)
    assert sd.try_transfer(src, files, lambda: False)
    assert got == [(2, "Flipper Zero X a.csv")]
    assert sd.find_new(src, lambda: False) == []          # handled, and the .txt isn't WiGLE


def test_board_sd_chunks():
    """sd read chunks come back out of order and one damaged: the file still lands whole."""
    data = WIGLE
    sent = []
    src = BoardSdSource("wdesp:x", "WardriveESP-X", sent.append)
    f = RemoteFile("/runs/run_1.csv", len(data))
    damaged = {"done": False}

    def board():
        answered = 0
        while answered < len(sent) or not damaged["done"] or answered < 10:
            time.sleep(0.01)
            while answered < len(sent):
                cmd = sent[answered].split()
                answered += 1
                off, n = int(cmd[3]), int(cmd[4])
                chunk = data[off:off + n]
                crc = zlib.crc32(chunk)
                if off == 0 and not damaged["done"]:
                    damaged["done"] = True
                    crc ^= 1
                src.lines.put(f"WD:SD data name=run_1.csv off={off} len={len(chunk)} size={len(data)} "
                              f"crc={crc:08x} b64={base64.b64encode(chunk).decode()}")
            if answered >= 3 and sent and sent[-1].startswith("sd read run_1.csv 0"):
                pass
    BoardSdSource.CHUNK, old = 200, BoardSdSource.CHUNK
    try:
        t = threading.Thread(target=board, daemon=True)
        t.start()
        out = io.BytesIO()
        assert src.read(f, out, lambda n: None) == len(data)
        assert out.getvalue() == data
    finally:
        BoardSdSource.CHUNK = old


def test_imported_rows_go_out_with_next_upload(tmp_path, monkeypatch):
    import gzip
    from wdgwars_uploader.wardrive import service
    monkeypatch.setattr(service, "config_dir", lambda: tmp_path)
    w = service.Wardriver(lambda m: None)
    rows, _ = parse_wigle_csv(WIGLE)
    for r in rows:
        r["t"] = time.time() - 60          # recent, so every target takes them
    r = w.import_rows(rows, "WardriveESP-D88E run_20.csv", "D88E")
    assert (r.rows, r.new) == (2, 2)
    assert w.pending("wigle") == 2 and w.pending("wardrift") == 2
    assert len(w.run_wifi) == 2 and w.sd_rows == 2
    assert len(w.boards.collected["D88E"][0]) == 2
    sent = []
    assert w.upload("wigle", "PC", lambda data, name: (sent.append(gzip.decompress(data)), (True, "ok"))[1]).startswith("2 rows")
    assert b"aa:bb:cc:dd:ee:01,Home" in sent[0] and w.pending("wigle") == 0
