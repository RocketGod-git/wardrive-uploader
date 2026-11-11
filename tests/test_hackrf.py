"""HackRF One / Pro as an ADS-B source: source order, PortaPack switch, rtl_tcp bridge."""
import socket
import struct
import time
import unittest
from unittest import mock

from wdgwars_uploader import adsb, detect, hackrf


def det(rtl=False, hrf=False, pp_port=""):
    d = detect.DetectResult(rtlsdr_present=rtl, hackrf_present=hrf)
    if pp_port:
        d.serial_ports = [detect.SerialDevice(port=pp_port, vid=0x1D50, pid=0x6018, description="PortaPack",
                                             manufacturer="", is_portapack=True)]
    return d


class SourceOrder(unittest.TestCase):
    def setUp(self):
        adsb._pp_switch.clear()

    def tearDown(self):
        adsb._pp_switch.clear()

    def test_rtlsdr_then_hackrf_then_portapack(self):
        cfg = {"adsb_source": "auto"}
        self.assertEqual(adsb.resolve_source(cfg, det(rtl=True, hrf=True, pp_port="COM9")), ("rtlsdr", ""))
        self.assertEqual(adsb.resolve_source(cfg, det(hrf=True, pp_port="COM9")), ("hackrf", ""))
        # a PortaPack alone is switched into HackRF mode
        self.assertEqual(adsb.resolve_source(cfg, det(pp_port="COM9")), ("hackrf", "COM9"))
        self.assertEqual(adsb.resolve_source(cfg, det()), ("", ""))

    def test_explicit_mayhem_keeps_the_serial_log(self):
        self.assertEqual(adsb.resolve_source({"adsb_source": "mayhem"}, det(pp_port="COM9")), ("mayhem", "COM9"))

    def test_switch_once_then_fall_back_to_serial(self):
        cfg = {"adsb_source": "auto", "adsb_enabled": True, "dump1090_autostart": True}
        d = det(pp_port="COM9")
        with mock.patch.object(hackrf, "find_usb", return_value=None), \
             mock.patch.object(hackrf, "portapack_to_hackrf", return_value=True) as sw, \
             mock.patch("wdgwars_uploader.mayhem.stop"), \
             mock.patch("wdgwars_uploader.mayhem.ensure_reader", return_value=True) as reader:
            self.assertFalse(adsb.ensure_decoder(cfg, det=d))
            self.assertFalse(adsb.ensure_decoder(cfg, det=d))
            sw.assert_called_once_with("COM9")              # "hackrf" is typed once per plug-in
            adsb._pp_switch["COM9"]["sent"] -= adsb._PP_SWITCH_WAIT + 1
            adsb.ensure_decoder(cfg, det=d)                  # no HackRF came back: give up
            self.assertEqual(adsb.resolve_source(cfg, d), ("mayhem", "COM9"))
            with mock.patch.object(adsb, "stop_dump1090"), mock.patch.object(hackrf, "stop_bridge"):
                adsb.ensure_decoder(cfg, det=d)
            reader.assert_called_once()
        # once the console goes away (rebooted into HackRF mode) a later plug-in tries again
        adsb.resolve_source(cfg, det(hrf=True))
        self.assertEqual(adsb.resolve_source(cfg, d), ("hackrf", "COM9"))

    def test_hackrf_launches_dump1090_on_the_bridge(self):
        cfg = {"adsb_source": "auto", "adsb_enabled": True, "dump1090_autostart": True}
        with mock.patch.object(hackrf, "find_usb", return_value=(0x1D50, 0x6089)), \
             mock.patch.object(hackrf, "ensure_bridge") as br, \
             mock.patch("wdgwars_uploader.mayhem.stop"), \
             mock.patch.object(adsb, "_url_serving", return_value=False), \
             mock.patch.object(adsb, "is_running", return_value=False), \
             mock.patch.object(adsb, "find_dump1090", return_value="C:/t/dump1090.exe"), \
             mock.patch.object(adsb, "_kill_stray_decoders"), mock.patch.object(adsb, "_prune_decoder_logs"), \
             mock.patch.object(adsb, "_apply_decoder_cfg") as cfgw, \
             mock.patch.object(adsb, "launch_dump1090", return_value=True) as launch:
            adsb._last_launch_attempt = 0.0
            self.assertTrue(adsb.ensure_decoder(cfg, det=det(hrf=True)))
        br.assert_called_once()
        self.assertEqual(launch.call_args[0][1], f"--net --device tcp://127.0.0.1:{hackrf.TCP_PORT}")
        self.assertEqual(cfgw.call_args[0][3], "2M")
        adsb._last_launch_attempt = 0.0


class BridgeNetwork(unittest.TestCase):
    def test_rtl_tcp_header_samples_and_commands(self):
        class NoRadio(hackrf.Bridge):
            def _radio_loop(self):
                self._stop.wait()
        br = NoRadio(port=0)
        # pick a free port
        s = socket.socket(); s.bind(("127.0.0.1", 0)); br.port = s.getsockname()[1]; s.close()
        br.start()
        try:
            c = None
            for _ in range(50):
                try:
                    c = socket.create_connection(("127.0.0.1", br.port), timeout=2); break
                except OSError:
                    time.sleep(0.05)
            hdr = c.recv(12)
            self.assertEqual(hdr[:4], b"RTL0")
            self.assertEqual(struct.unpack(">II", hdr[4:]), (5, 29))
            for _ in range(50):
                if br.snapshot()["client"]:
                    break
                time.sleep(0.02)
            br._feed(bytes([0x00, 0x7F, 0x80, 0xFF]))       # signed 0, 127, -128, -1
            self.assertEqual(c.recv(4), bytes([0x80, 0xFF, 0x00, 0x7F]))
            c.sendall(struct.pack(">BI", 0x01, 1_090_000_000) + struct.pack(">BI", 0x02, 2_000_000)
                      + struct.pack(">BI", 0x04, 400))
            for _ in range(50):
                if len(br._retune) == 2:
                    break
                time.sleep(0.02)
            self.assertEqual(br._retune, {"freq": 1_090_000_000, "rate": 2_000_000})
            c.close()
        finally:
            br.stop()

    def test_helpers(self):
        self.assertEqual(hackrf.bb_filter_for(2_000_000), 1_750_000)
        self.assertEqual(hackrf.bb_filter_for(10_000_000), 9_000_000)
        self.assertEqual(hackrf.board_name(5), "HackRF Pro")
        self.assertEqual(hackrf.board_name(2), "HackRF One")


if __name__ == "__main__":
    unittest.main()
