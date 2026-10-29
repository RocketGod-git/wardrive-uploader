"""Existing Wardrift aircraft and CSV uploads remain unchanged by mesh additions."""
import gzip
import json
import unittest
from unittest.mock import Mock, patch

from wdgwars_uploader import wardrift


def response(body, status=200):
    result = Mock(status_code=status, headers={"content-type": "application/json"})
    result.json.return_value = body
    return result


class WardriftUploadRegressionTests(unittest.TestCase):
    def test_wardrift_aircraft_keeps_gzip_and_absolute_observation_age(self):
        client = wardrift.WardriftClient("wdk_test", label="testpc")
        client.session.post = Mock(return_value=response({"adsb_count": 1, "adsb_exp": 2, "adsb_currency": 3, "awarded_influence": 4}))
        with patch.object(wardrift.time, "time", return_value=1800000010):
            result = client.upload_aircraft([{"hex": "abcdef", "_obs_ts": 1800000000, "_pos_ts": 1799999998, "lat": 10, "lon": 20}])
        self.assertEqual((result.credited, result.exp, result.currency, result.influence), (1, 2, 3, 4))
        call = client.session.post.call_args
        self.assertEqual(call.args[0], "https://wardrift.net/v1/ingest/signals")
        self.assertEqual(client.session.headers["X-API-Key"], "wdk_test")
        self.assertEqual(call.kwargs["headers"]["Content-Type"], "application/gzip")
        wire = json.loads(gzip.decompress(call.kwargs["data"]))
        self.assertEqual(set(wire), {"now", "aircraft"})
        self.assertEqual(wire["aircraft"][0]["seen"], 10)
        self.assertEqual(wire["aircraft"][0]["seen_pos"], 12)
        self.assertNotIn("_obs_ts", wire["aircraft"][0])

    def test_wardrift_csv_preserves_filename_and_gzip_bytes(self):
        client = wardrift.WardriftClient("wdk_test")
        client.session.post = Mock(return_value=response({"accepted": 1, "awarded_exp": 2}))
        data = gzip.compress(b"WigleWifi-1.6\n")
        ok, _ = client.upload_csv(data, "capture.csv.gz")
        self.assertTrue(ok)
        call = client.session.post.call_args
        self.assertEqual(call.kwargs["data"], data)
        self.assertEqual(call.kwargs["headers"]["X-File-Name"], "capture.csv.gz")
        self.assertEqual(call.args[0], "https://wardrift.net/v1/ingest/signals")


if __name__ == "__main__":
    unittest.main()
