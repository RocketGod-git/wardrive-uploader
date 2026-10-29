"""Corrupt delivery state fails closed without replacing installation identity."""
import copy
import hashlib
import json
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock

from wdgwars_uploader import wardrift as w

ACCOUNT = "11111111-1111-4111-8111-111111111111"
IDENTITY = "meshtastic:aabbccdd"


class StateValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "mesh.json"
        self.fp = w.credential_fingerprint("test-key")
        self.reporter = w.MeshReporter(self.path)
        self.reporter.bind(self.fp, ACCOUNT)
        self.at = time.time() - 10
        self.radio = {"capture_id": "fresh-capture", "credential_fingerprint": self.fp,
                      "observed_at": self.at, "node": {"network": "meshtastic", "node_id": "aabbccdd"},
                      "profile": "meshtastic-localstats-v1", "firmware_version": "test-firmware",
                      "uptime_seconds": 100, "uptime_at": self.at, "counters_at": self.at,
                      "counters": {"num_packets_tx": 10, "num_packets_rx": 20, "num_tx_relay": 5}}
        self.reporter.stage([self.radio])
        self.observations = json.loads(self.path.read_text())
        self.reporter.prepare([], ACCOUNT, time.time())
        self.pending = json.loads(self.path.read_text())
        batch = self.reporter.pending(ACCOUNT)[0]
        payload = json.loads(batch["payload"])
        self.rid = str(uuid.uuid4())
        receipt = {"ok": True, "receipt_id": self.rid, "batch_id": batch["batch_id"],
                   "reports_accepted": 1, "currency": 0, "influence": .125,
                   "policy_version": "collector-v2-availability-60",
                   "samples": [{"sample_id": payload["nodes"][0]["telemetry"]["sample_id"],
                                "forwarded": 0, "suppressed": None, "attempted": None, "failed": None,
                                "currency": 0, "influence": .125, "availability_seconds": 0}]}
        self.reporter.acknowledge(batch, receipt)
        self.accepted = json.loads(self.path.read_text())

    def assert_rejected(self, data):
        original = json.dumps(data)
        self.path.write_text(original)
        reporter = w.MeshReporter(self.path)
        self.assertEqual(self.path.read_text(), original, "Corrupt state must be preserved for recovery")
        self.assertIn("preserve", reporter.error)
        # Core calls both methods during startup even when its ledger is broken.
        self.assertEqual(reporter.receipts(self.fp), [])
        self.assertIn("preserve", reporter.status(self.fp)["error"])
        client = w.WardriftClient("test-key")
        client.session = Mock()
        result = client.upload_mesh([], [], reporter)
        self.assertFalse(result.ok)
        client.session.get.assert_not_called()
        client.session.post.assert_not_called()

    def test_valid_pending_observations_batches_and_receipts_reload(self):
        for data in (self.observations, self.pending, self.accepted):
            with self.subTest(shape=list(data)):
                self.path.write_text(json.dumps(data))
                restored = w.MeshReporter(self.path)
                self.assertFalse(restored.error)
                self.assertEqual(restored.reporter_id, self.reporter.reporter_id)
        restored = w.MeshReporter(self.path)
        self.assertEqual(restored.receipts(self.fp)[0]["receipt"]["influence"], .125)

    def test_legacy_unbound_history_is_retained_without_new_shape_requirements(self):
        legacy = {"reporter_id": self.reporter.reporter_id, "sequence_no": 123,
                  "sent": {"aabbccdd": 1234}, "radios": {"aabbccdd": {"epoch": "old-epoch", "seq": 42}}}
        self.path.write_text(json.dumps(legacy))
        restored = w.MeshReporter(self.path)
        self.assertFalse(restored.error)
        self.assertEqual(restored.radios, legacy["radios"])
        self.assertEqual(restored.sent, legacy["sent"])
        self.assertFalse(restored.receipts(self.fp))

    def test_corrupt_archive_binding_and_required_fields_fail_closed(self):
        cases = [("accepted_at", "yesterday"), ("accepted_at", float("nan")), ("nodes", "1"),
                 ("nodes", True), ("account_id", str(uuid.uuid4())), ("receipt_id", str(uuid.uuid4())),
                 ("receipt", None)]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                data = copy.deepcopy(self.accepted)
                data["accounts"][ACCOUNT]["receipts"][self.rid][field] = value
                self.assert_rejected(data)
        data = copy.deepcopy(self.accepted)
        del data["accounts"][ACCOUNT]["receipts"][self.rid]["accepted_at"]
        self.assert_rejected(data)

    def test_corrupt_receipt_values_and_duplicate_samples_fail_closed(self):
        for field, value in (("ok", 1), ("reports_accepted", 2), ("samples", {}),
                             ("currency", .5), ("influence", float("inf"))):
            with self.subTest(field=field):
                data = copy.deepcopy(self.accepted)
                data["accounts"][ACCOUNT]["receipts"][self.rid]["receipt"][field] = value
                self.assert_rejected(data)
        for field, value in (("forwarded", "1"), ("failed", -1), ("availability_seconds", []),
                             ("sample_id", "invalid"), ("outcome", {})):
            with self.subTest(sample_field=field):
                data = copy.deepcopy(self.accepted)
                data["accounts"][ACCOUNT]["receipts"][self.rid]["receipt"]["samples"][0][field] = value
                self.assert_rejected(data)
        data = copy.deepcopy(self.accepted)
        record = data["accounts"][ACCOUNT]["receipts"][self.rid]
        record["nodes"] = 2
        record["receipt"]["reports_accepted"] = 2
        record["receipt"]["samples"] *= 2
        self.assert_rejected(data)

    def test_corrupt_radio_highwater_and_discovery_cursors_fail_closed(self):
        for field, value in (("epoch", "bad"), ("seq", True), ("seq", 0), ("observed_at", "old"),
                             ("uptime", -1), ("fw", None), ("profile", ""),
                             ("counters", {"num_packets_tx": .5}), ("counters", {"other": 1})):
            with self.subTest(field=field, value=value):
                data = copy.deepcopy(self.pending)
                data["accounts"][ACCOUNT]["radios"][IDENTITY][field] = value
                self.assert_rejected(data)
        for subject, value in (("aabbccdd", 123), (IDENTITY, "123"), (IDENTITY, -1)):
            data = copy.deepcopy(self.accepted)
            data["accounts"][ACCOUNT]["sent"] = {subject: value}
            self.assert_rejected(data)

    def test_corrupt_observation_account_binding_fails_closed(self):
        for field, value in (("capture_id", None), ("fingerprint", "raw-key"),
                             ("fingerprint", w.credential_fingerprint("other-key")),
                             ("account_id", str(uuid.uuid4()))):
            with self.subTest(field=field):
                data = copy.deepcopy(self.observations)
                data["observations"][0][field] = value
                self.assert_rejected(data)
        data = copy.deepcopy(self.observations)
        data["observations"][0]["radio"]["credential_fingerprint"] = ""
        self.assert_rejected(data)
        data = copy.deepcopy(self.observations)
        data["capture_ids"] = [None]
        self.assert_rejected(data)

    def test_corrupt_pending_acknowledgment_and_retry_metadata_fail_closed(self):
        for field, value in (("attempts", -1), ("retry_at", float("nan")), ("error", {}),
                             ("items", [{"identity": "meshtastic:ffffffff", "key": 123}])):
            with self.subTest(field=field):
                data = copy.deepcopy(self.pending)
                data["batches"][0][field] = value
                self.assert_rejected(data)
        data = copy.deepcopy(self.pending)
        data["batches"] *= 2
        self.assert_rejected(data)

    def test_corrupt_embedded_payload_fails_despite_recomputed_checksum(self):
        data = copy.deepcopy(self.pending)
        batch = data["batches"][0]
        payload = json.loads(batch["payload"])
        payload["nodes"][0]["node_id"] = "ffffffff"
        batch["payload"] = json.dumps(payload)
        batch["payload_sha256"] = hashlib.sha256(batch["payload"].encode()).hexdigest()
        self.assert_rejected(data)


if __name__ == "__main__":
    unittest.main()
