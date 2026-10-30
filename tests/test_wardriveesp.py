"""WardriveESP protocol, channel split and bridge behavior; no radios or ports."""
import time
import unittest

from wdgwars_uploader.wardrive import boards as B
from wdgwars_uploader.wardrive import esp_proto as P

C5_CHANS = "1,2,3,4,5,6,7,8,9,10,11,12,13,36,40,44,48,52,56,60,64,100,104,108,112,116,120,124,128,132,136,140,144,149,153,157,161,165"
S3_CHANS = "1,2,3,4,5,6,7,8,9,10,11,12,13"


def caps(fw="1.7.4", chip="esp32c5", bands="2g,5g", chans=C5_CHANS, bt=1, cap=1, gps=0):
    return f"WD:CAPS fw={fw} chip={chip} bands={bands} ota=1 bt={bt} gps={gps} cap={cap} usb=1 link=usb chans={chans}"


class ProtoTest(unittest.TestCase):
    def test_status_beacon_from_a_real_board(self):
        b = P.parse_beacon(bytes.fromhex("572b282c001703d88e533234"))
        self.assertTrue(b.linked and b.scanning and b.gps)
        self.assertFalse(b.usb or b.updating or b.holder_iphone)
        self.assertEqual((b.mode, b.temp_c, b.count, b.fw, b.name, b.holder),
                         (2, 40, 44, "1.7.3", "WardriveESP-D88E", "S24"))
        self.assertIsNone(P.parse_beacon(b"\x00" * 12))

    def test_advert_extras(self):
        x = P.parse_extras(bytes.fromhex("0dff7f000000"))
        self.assertTrue(x.frame_b and x.screen)
        self.assertEqual(x.link_end, P.LINK_END_LOST)
        self.assertEqual(x.chan_bits, 0x7FFF << 24)
        a = P.parse_extras(bytes.fromhex("0207ffff0700"))
        self.assertFalse(a.frame_b)
        self.assertEqual((a.board, a.battery, a.link_end), (7, None, P.LINK_END_RELEASED))
        self.assertEqual(P.extras_board_name(a.board), "Koko Marauder V8")
        self.assertEqual(P.extras_channels(a.chan_bits)[:3], [1, 2, 3])

    def test_cmd_frame(self):
        f = P.cmd_frame("caps", 0x01020304)
        self.assertEqual(f[:6], bytes([0x47, 0x42, 0x01, P.T_CMD, 0, 1]))
        self.assertEqual(f[6:10], bytes([4, 3, 2, 1]))
        self.assertEqual(f[10:12], bytes([4, 0]))
        self.assertEqual(f[12:], b"caps")

    def test_lines(self):
        ap = P.parse_ap("WD:AP ts=1 bssid=AA:BB:CC:DD:EE:FF ssid_hex=486f6d65 rssi=-61 ch=36 auth=WPA2 hidden=0")
        self.assertEqual((ap.bssid, ap.ssid, ap.rssi, ap.channel, ap.caps), ("aa:bb:cc:dd:ee:ff", "Home", -61, 36, "[WPA2-PSK-CCMP][ESS]"))
        self.assertEqual(P.parse_ap("WD:AP bssid=AA:BB ssid_hex= rssi=-1"), None)
        hid = P.parse_ap("WD:AP bssid=AA:BB:CC:DD:EE:FF ssid_hex=41 rssi=-70 ch=6 auth=OPEN hidden=1")
        self.assertEqual((hid.ssid, hid.caps), ("", "[ESS]"))
        bt = P.parse_bt("WD:BT ts=1 mac=aa:bb:cc:dd:ee:01 type=random rssi=-80 name_hex=54 mfg=004c conn=1")
        self.assertEqual((bt.mac, bt.name, bt.company_id, bt.connectable, bt.classic_cod), ("AA:BB:CC:DD:EE:01", "T", 0x4C, True, None))
        cl = P.parse_bt("WD:BT mac=aa:bb:cc:dd:ee:02 type=public rssi=-70 name_hex= mfg= conn=0 kind=classic cod=5a020c src=coproc")
        self.assertEqual((cl.classic_cod, cl.coproc), (0x5A020C, True))
        self.assertEqual(P.classic_auth_mode(cl.classic_cod), "Smartphone;10")
        g = P.parse_gps("WD:GPS ts=1 lat=34.26 lon=-118.77 alt=300.5 fix=1 sats=9")
        self.assertEqual((g.lat, g.lon, g.alt, g.sats), (34.26, -118.77, 300.5, 9))
        self.assertIsNone(P.parse_gps("WD:GPS lat=0 lon=0 fix=1"))
        self.assertIsNone(P.parse_gps("WD:GPS lat=34 lon=-118 fix=0"))
        hs = P.parse_capture("WD:HS ts=1 bssid=AA:BB:CC:DD:EE:FF sta=11:22:33:44:55:66 ssid_hex=486f6d65 ch=6 rssi=-50 "
                             "hc22000=WPA*02*ABCDEF*aabbccddeeff*112233445566*486f6d65*00*00*00")
        self.assertEqual((hs.kind, hs.key, hs.sta, hs.ssid), (2, "abcdef", "11:22:33:44:55:66", "Home"))
        pm = P.parse_capture("WD:PMKID bssid=AA:BB:CC:DD:EE:FF sta=11:22:33:44:55:66 ssid_hex= ch=1 rssi=-40 pmkid=" + "AB" * 16)
        self.assertEqual((pm.kind, pm.key), (1, "ab" * 16))

    def test_names(self):
        self.assertEqual(P.board_suffix("WardriveESP-12fa"), "12FA")
        self.assertEqual(P.board_suffix("iosESP-12FA"), "12FA")
        self.assertIsNone(P.board_suffix("iosESP"))
        self.assertEqual(P.name_in("WardriveESP 1.7.4  ·  esp32c5 rev 0.1  ·  WardriveESP-d88e"), "WardriveESP-D88E")
        self.assertEqual(P.holder_nickname("Jeremy's\tPC with a very long name"), "Jeremy'sPC with a")
        self.assertTrue(P.is_older("1.6.4", "1.7.3") and not P.is_older("1.7.3", "1.7.3"))

    def test_region(self):
        us = P.Region.for_country("us")
        self.assertEqual(us.limit([1, 12, 13, 36, 165, 169]), [1, 36, 165])
        self.assertEqual(P.Region.for_country("DE").limit([1, 13, 144, 149]), [1, 13])
        self.assertEqual(P.Region.for_country("JP").limit([1, 14]), [1, 14])
        self.assertEqual(us.limit([12, 13]), [12, 13])      # nothing left: the board keeps its own

    def test_split_covers_every_channel_once(self):
        chans = P.channel_list(C5_CHANS)
        for n in range(2, 9):
            boards = [(f"b{i}", chans) for i in range(n)]
            p = P.plan(boards)
            got = sorted(c for v in p.shares.values() for c in v)
            self.assertEqual(got, sorted(chans))
            sizes = [len(v) for v in p.shares.values()]
            self.assertLessEqual(max(sizes) - min(sizes), 1)
            self.assertEqual(p.sweep_all, [])

    def test_split_priority_and_stickiness(self):
        chans = P.channel_list(C5_CHANS)
        p = P.plan([("a", chans), ("b", chans), ("c", chans)])
        self.assertEqual([len({1, 6, 11} & set(p.shares[k])) for k in "abc"], [1, 1, 1])
        again = P.plan([("a", chans), ("b", chans), ("c", chans), ("d", chans)], previous=p.shares)
        moved = sum(len(set(p.shares[k]) - set(again.shares[k])) for k in "abc")
        self.assertLessEqual(moved, len(chans) // 4 + 1)

    def test_split_more_boards_than_channels(self):
        p = P.plan([(f"b{i}", [1, 6, 11]) for i in range(5)])
        self.assertEqual(sorted(len(v) for v in p.shares.values()), [1, 1, 1])
        self.assertEqual(p.sweep_all, ["b3", "b4"])

    def test_split_mixed_bands(self):
        p = P.plan([("c5", P.channel_list(C5_CHANS)), ("s3", P.channel_list(S3_CHANS))])
        self.assertTrue(all(c <= 14 for c in p.shares["s3"]))
        self.assertEqual(sorted(p.shares["c5"] + p.shares["s3"]), sorted(P.channel_list(C5_CHANS)))

    def test_compact(self):
        self.assertEqual(P.compact([1, 6, 11, 36, 40, 44, 48, 149]), "1, 6, 11, 36–48, 149")
        self.assertEqual(P.compact([1, 2, 3, 5]), "1–3, 5")


class FakeSerial:
    """Answers like a WardriveESP board on a USB-UART bridge."""

    def __init__(self, board=True):
        self.board = board
        self.rx = bytearray()
        self.sent = []

    def write(self, b):
        line = b.decode().strip()
        self.sent.append(line)
        if not self.board:
            self.rx += b"Unknown command\r\n"
        elif line == "wdusb hello":
            self.rx += b"I (123) boot: log text\r\n" + caps().encode() + b"\r\nWD:BOOT fw=1.7.4 reset=poweron boots=3\r\n"
        elif line == "info":
            self.rx += "WardriveESP 1.7.4  ·  esp32c5 rev 0.1  ·  WardriveESP-12FA\r\n".encode()

    @property
    def in_waiting(self):
        return len(self.rx)

    def read(self, n):
        if not self.rx:
            time.sleep(0.01)
            return b""
        out = bytes(self.rx[:n])
        del self.rx[:n]
        return out


class UsbProbeTest(unittest.TestCase):
    def test_probe_finds_a_board(self):
        u = B.UsbBoards(bridge=None)
        ser = FakeSerial()
        kv = u._probe(ser)
        self.assertEqual((kv["fw"], kv["chip"], kv["name"]), ("1.7.4", "esp32c5", "WardriveESP-12FA"))
        self.assertEqual(ser.sent[0], "wdusb hello")
        self.assertEqual(ser.sent[-1], "wdusb bye")

    def test_probe_leaves_other_firmware(self):
        u = B.UsbBoards(bridge=None)
        u.PROBE_S = 1.2
        ser = FakeSerial(board=False)
        self.assertIsNone(u._probe(ser))
        self.assertTrue(all(x == "wdusb hello" for x in ser.sent))


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self._settle = B.PLAN_SETTLE_S
        B.PLAN_SETTLE_S = 0.05
        self.hits = []
        self.events = []
        self.br = B.BoardBridge(on_hit=self.hits.append, on_event=self.events.append)
        self.br.region = P.Region("ALL")
        self.br.usb.start = lambda: None
        self.br.start()

    def tearDown(self):
        self.br.stop(1.0)
        B.PLAN_SETTLE_S = self._settle

    def board(self, sfx, caps_line):
        link = B.UsbLink(self.br, "usb:" + sfx, "WardriveESP-" + sfx)
        self.br.attach_usb(link)
        time.sleep(0.05)
        link.line(caps_line)
        return link

    def sent(self, link):
        time.sleep(0.6)
        return list(link.out)

    def test_one_board_captures_on_every_channel(self):
        self.br.configure(True, set(), "Desk PC", set())
        a = self.board("AAAA", caps())
        cmds = self.sent(a)
        self.assertIn("iam a Desk PC", cmds)
        self.assertEqual(cmds[0], P.START)
        self.assertIn("cap on", cmds)
        self.assertEqual(cmds[-1], "ch all")
        self.assertIn("WardriveESP-AAAA linked over USB (esp32c5 · fw 1.7.4)", self.events)

    def test_two_boards_one_goes_bluetooth(self):
        a = self.board("AAAA", caps())
        b = self.board("BBBB", caps(chip="esp32s3", bands="2g", chans=S3_CHANS))
        ca, cb = self.sent(a), self.sent(b)
        self.assertIn("bt on", cb)
        self.assertNotIn("bt on", ca)
        self.assertIn("cap on", ca)
        self.assertEqual(ca[-1], "ch all")

    def test_three_boards_split_capture(self):
        links = [self.board(s, caps()) for s in ("AAAA", "BBBB", "CCCC")]
        cmds = [self.sent(x) for x in links]
        self.assertIn("bt on", cmds[0])
        shares = [P.channel_list(c[-1][3:]) for c in cmds[1:]]
        self.assertTrue(all(c[-1].startswith("ch ") and c[-1] != "ch all" for c in cmds[1:]))
        self.assertEqual(sorted(shares[0] + shares[1]), sorted(P.channel_list(C5_CHANS)))
        st = self.br.status()
        self.assertEqual([b["id"] for b in st["boards"]], ["esp:AAAA", "esp:BBBB", "esp:CCCC"])

    def test_data_lands_in_the_run(self):
        a = self.board("AAAA", caps(gps=1))
        a.line("WD:AP ts=1 bssid=AA:BB:CC:DD:EE:FF ssid_hex=486f6d65 rssi=-61 ch=36 auth=WPA2 hidden=0")
        a.line("WD:BT ts=1 mac=AA:BB:CC:DD:EE:01 type=random rssi=-80 name_hex=54 mfg=004c conn=1")
        a.line("WD:GPS ts=1 lat=34.26 lon=-118.77 alt=300 fix=1 sats=9")
        a.line("WD:PMKID bssid=AA:BB:CC:DD:EE:FF sta=11:22:33:44:55:66 ssid_hex=486f6d65 ch=36 rssi=-40 pmkid=" + "ab" * 16)
        time.sleep(0.4)
        wifi = self.br.fresh_wifi(5)
        self.assertEqual([(x["mac"], x["ssid"], x["freq"], x["auth"]) for x in wifi],
                         [("aa:bb:cc:dd:ee:ff", "Home", 5180, "[WPA2-PSK-CCMP][ESS]")])
        self.assertEqual([x["mac"] for x in self.br.fresh_bt(5)], ["aa:bb:cc:dd:ee:01"])
        self.assertEqual(round(self.br.gps.fix.lat, 2), 34.26)
        self.assertEqual([(h.kind, h.ssid, h.bssid) for h in self.hits], [(1, "Home", "aa:bb:cc:dd:ee:ff")])

    def test_switched_off_board_is_left_alone(self):
        self.br.configure(True, {"AAAA"}, "PC", set())
        time.sleep(0.1)

        class Adv:
            service_uuids = [P.SERVICE]
            local_name = "WardriveESP-AAAA"
            manufacturer_data = {}
            service_data = {}
            rssi = -50

        class Dev:
            address = "11:22:33:44:55:66"
        self.br.on_advert(Dev(), Adv())
        time.sleep(0.2)
        self.assertEqual(self.br.sessions, {})

    def test_board_held_by_a_phone_is_not_linked(self):
        class Adv:
            service_uuids = [P.STATUS]
            local_name = None
            manufacturer_data = {P.BEACON_COMPANY: bytes.fromhex("572b282c001703d88e533234")}
            service_data = {}
            rssi = -43

        class Dev:
            address = "3C:DC:75:86:D8:8E"
        self.br.on_advert(Dev(), Adv())
        time.sleep(0.3)
        self.assertEqual(self.br.sessions, {})
        self.br._publish()
        self.assertEqual(self.br.status()["elsewhere"], [{"name": "WardriveESP-D88E", "holder": "S24", "rssi": -43}])
        self.assertIn("3C:DC:75:86:D8:8E", self.br.held_elsewhere)


if __name__ == "__main__":
    unittest.main()
