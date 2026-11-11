"""dump1090 downloads on its own when an SDR that needs it is plugged in and none is on disk."""
import threading
import time
import unittest
from unittest import mock

from wdgwars_uploader import adsb, prereq


def _wait_done():
    for _ in range(200):
        if adsb.install_status()["state"] != "downloading":
            return
        time.sleep(0.01)


class AutoInstall(unittest.TestCase):
    def setUp(self):
        adsb._install.update(state="", msg="", at=0.0, tries=0)
        self.events = []
        self.done = threading.Event()
        adsb.set_hooks(self.events.append, self.done.set)

    def tearDown(self):
        adsb.set_hooks(None, None)
        adsb._install.update(state="", msg="", at=0.0, tries=0)
        adsb._last_launch_attempt = 0.0

    def test_rtlsdr_without_decoder_starts_a_download(self):
        cfg = {"adsb_enabled": True, "adsb_source": "rtlsdr", "dump1090_autostart": True}
        with mock.patch.object(adsb, "_url_serving", return_value=False), \
             mock.patch.object(adsb, "find_dump1090", return_value=None), \
             mock.patch.object(adsb, "install_dump1090", return_value="C:/t/dump1090.exe") as inst, \
             mock.patch("wdgwars_uploader.mayhem.stop"):
            self.assertFalse(adsb.ensure_decoder(cfg, sdr_present=True))
            self.assertTrue(self.done.wait(2))
        inst.assert_called_once()
        self.assertEqual(adsb.install_status()["state"], "ready")
        self.assertTrue(any("dump1090 ready" in e for e in self.events))

    def test_no_rtlsdr_no_download(self):
        cfg = {"adsb_enabled": True, "adsb_source": "rtlsdr", "dump1090_autostart": True}
        with mock.patch.object(adsb, "_url_serving", return_value=False), \
             mock.patch.object(adsb, "find_dump1090", return_value=None), \
             mock.patch.object(adsb, "start_install") as start, \
             mock.patch("wdgwars_uploader.mayhem.stop"):
            adsb.ensure_decoder(cfg, sdr_present=False)
        start.assert_not_called()

    def test_failure_waits_then_install_now_retries(self):
        with mock.patch.object(adsb, "install_dump1090", side_effect=RuntimeError("offline")):
            self.assertTrue(adsb.start_install())
            self.assertTrue(self.done.wait(2))
            _wait_done()
            st = adsb.install_status()
            self.assertEqual(st["state"], "failed")
            self.assertFalse(adsb.start_install())          # inside the retry window
            with mock.patch.object(adsb, "find_dump1090", return_value=None):
                chk = prereq.check_dump1090()
            self.assertEqual(chk["status"], "download failed")
            self.assertEqual(chk["action"], "install-dump1090")
            self.done.clear()
            self.assertTrue(adsb.start_install(force=True))  # the Install now button
            self.assertTrue(self.done.wait(2))

    def test_one_download_at_a_time(self):
        gate = threading.Event()
        with mock.patch.object(adsb, "install_dump1090", side_effect=lambda progress=None: gate.wait(2) and "x"):
            self.assertTrue(adsb.start_install())
            self.assertFalse(adsb.start_install(force=True))
            with mock.patch.object(adsb, "find_dump1090", return_value=None):
                self.assertEqual(prereq.check_dump1090()["status"], "downloading on its own…")
            gate.set()
            self.assertTrue(self.done.wait(2))


if __name__ == "__main__":
    unittest.main()
