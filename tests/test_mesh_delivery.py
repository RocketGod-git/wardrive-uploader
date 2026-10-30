"""Durability, account binding and wire contract tests; no external HTTP/radios."""
import copy
import json
import math
from pathlib import Path
import tempfile
import time
import unittest
import uuid
from unittest.mock import Mock, patch

import requests

from wdgwars_uploader import wardrift as w

ACCOUNT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ACCOUNT_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
KEY_A = "test-key-a"
KEY_B = "test-key-b"


def radio(at=None, *, network="meshtastic", capture_id=None, **updates):
    at = at or time.time() - 10
    node = {"network": network, "node_id": "aabbccdd", "name": "Local radio", "node_type": "COMPANION"}
    if network == "meshcore":
        node.update(node_id="0123456789abcdef", public_key="0123456789abcdef" + "ab" * 24)
    value = {"capture_id": capture_id or str(uuid.uuid4()), "node": node, "observed_at": at,
             "uptime_seconds": 1000, "uptime_at": at, "firmware_version": "test-firmware",
             "profile": "meshtastic-localstats-v1", "transport": "usb_serial",
             "health_at": at, "health": {"battery_level": 0, "voltage": 3.9},
             "counters_at": at, "counters": {"num_packets_tx": 100, "num_packets_rx": 200,
                 "num_tx_relay": 10, "num_tx_relay_canceled": 3, "num_total_nodes": 10}}
    value.update(updates)
    return value


def response(status=200, body=None, headers=None):
    result = Mock(status_code=status, text=json.dumps(body) if body is not None else "")
    result.json.return_value = body if body is not None else {}
    result.headers = headers or {}
    return result


def receipt(body, **updates):
    if isinstance(body, bytes):
        body = json.loads(body)
    samples = [{"sample_id": n["telemetry"]["sample_id"], "enrollment_id": str(uuid.uuid4()),
                "observed_at": n["telemetry"]["observed_at"], "outcome": "baseline", "evidence": "aggregate_packets",
                "forwarded": 0, "suppressed": None, "attempted": None, "failed": None,
                "contribution_units": 0, "currency": 0, "influence": 0.125,
                "availability_seconds": 0, "availability_currency": 0, "availability_influence": 0,
                "policy_version": "collector-v2-availability-60"}
               for n in body["nodes"] if "telemetry" in n]
    result = {"ok": True, "receipt_id": str(uuid.uuid4()), "batch_id": body["batch_id"],
              "reports_accepted": len(samples), "samples": samples, "currency": 0, "influence": 0.125,
              "policy_version": "collector-v2-availability-60"}
    result.update(updates)
    return result


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "mesh.json"
        self.reporter = w.MeshReporter(self.path)
        self.fp = w.credential_fingerprint(KEY_A)

    def prepare(self, readings=None, nodes=None, account=ACCOUNT_A, fp=None):
        self.reporter.stage(readings or [], fp or self.fp)
        self.reporter.bind(fp or self.fp, account)
        self.reporter.prepare(nodes or [], account, time.time())
        return self.reporter.pending(account)

    def client(self, key=KEY_A, account=ACCOUNT_A):
        client = w.WardriftClient(key)
        client.session = Mock()
        client.session.get.return_value = response(body={"account_id": account})
        client.session.post.side_effect = lambda *a, **kw: response(body=receipt(kw["data"]))
        return client

    def test_legacy_identity_sequence_and_unbound_history_survive(self):
        legacy = {"reporter_id": str(uuid.uuid4()), "sequence_no": 9_000_000_000,
                  "since": time.time() - 100, "sent": {"aabbccdd": 123},
                  "radios": {"aabbccdd": {"epoch": "old", "seq": 30}}, "require_fix": True}
        self.path.write_text(json.dumps(legacy))
        self.reporter = w.MeshReporter(self.path)
        batches = self.prepare([radio()])
        loaded = json.loads(self.path.read_text())
        self.assertEqual(self.reporter.reporter_id, legacy["reporter_id"])
        self.assertGreater(batches[0]["sequence_no"], legacy["sequence_no"])
        self.assertEqual(loaded["sent"], legacy["sent"])
        self.assertEqual(loaded["radios"], legacy["radios"])
        self.assertNotIn("lat", json.loads(batches[0]["payload"])["nodes"][0])
        self.assertEqual(json.loads(batches[0]["payload"])["nodes"][0]["telemetry"]["sample_sequence"], 1)

    def test_malformed_state_is_preserved_and_never_sends(self):
        for content in ("broken JSON", '{}', '{"reporter_id":"invalid"}', json.dumps({"reporter_id": str(uuid.uuid4()), "batches": [{}]})):
            with self.subTest(content=content):
                self.path.write_text(content)
                reporter = w.MeshReporter(self.path)
                client = self.client()
                result = client.upload_mesh([], [radio()], reporter)
                self.assertFalse(result.ok)
                self.assertIn("preserve", result.error)
                client.session.post.assert_not_called()
                self.assertEqual(self.path.read_text(), content)

    def test_stage_deduplicates_handoff_and_allocates_sample_id_once(self):
        sample = radio()
        self.reporter.stage([sample], self.fp)
        before = json.loads(self.path.read_text())["observations"][0]
        self.reporter = w.MeshReporter(self.path)
        self.reporter.stage([sample], self.fp)
        after = json.loads(self.path.read_text())["observations"]
        self.assertEqual(after, [before])
        self.assertNotIn(KEY_A, self.path.read_text())

    def test_account_rotation_resumes_identical_pending_bytes(self):
        first = self.prepare([radio()])[0]
        other_fp = w.credential_fingerprint("new-key-same-account")
        self.reporter.bind(other_fp, ACCOUNT_A)
        self.reporter.prepare([], ACCOUNT_A, time.time() + 1)
        self.assertEqual(self.reporter.pending(ACCOUNT_A)[0]["payload"], first["payload"])
        self.assertEqual(self.reporter.status(other_fp)["pending"], 1)

    def test_different_account_cannot_drain_or_relabel_old_queue(self):
        original = self.prepare([radio()])[0]
        client = self.client(KEY_B, ACCOUNT_B)
        result = client.upload_mesh([], [], self.reporter)
        self.assertTrue(result.ok)
        client.session.post.assert_not_called()
        self.assertEqual(self.reporter.pending(ACCOUNT_A)[0], original)
        self.assertFalse(self.reporter.receipts(w.credential_fingerprint(KEY_B)))

    def test_unverified_captures_bind_only_to_their_own_credential(self):
        old_fp = w.credential_fingerprint(KEY_B)
        self.reporter.stage([radio()], old_fp)
        self.reporter.stage([radio(credential_fingerprint="")], self.fp)
        self.reporter.bind(self.fp, ACCOUNT_A)
        self.reporter.prepare([], ACCOUNT_A, time.time())
        self.assertEqual(self.reporter.pending(ACCOUNT_A), [])
        self.assertEqual(self.reporter.status(old_fp)["unverified"], 1)
        self.reporter.bind(old_fp, ACCOUNT_B)
        self.reporter.prepare([], ACCOUNT_B, time.time())
        self.assertEqual(len(self.reporter.pending(ACCOUNT_B)), 1)
        data = json.loads(self.path.read_text())
        self.assertEqual(len(data["observations"]), 1)
        self.assertIsNone(data["observations"][0]["account_id"])

    def test_verified_fingerprint_cannot_change_account(self):
        self.reporter.bind(self.fp, ACCOUNT_A)
        with self.assertRaises(w.MeshStateError):
            self.reporter.bind(self.fp, ACCOUNT_B)
        self.assertEqual(self.reporter.status(self.fp)["account_id"], ACCOUNT_A)

    def test_no_http_after_persistence_failure(self):
        client = self.client()
        with patch.object(w.os, "replace", side_effect=OSError("disk full")):
            result = client.upload_mesh([], [radio()], self.reporter)
        self.assertFalse(result.ok)
        client.session.post.assert_not_called()
        self.assertEqual(json.loads(self.path.read_text())["observations"], [])
        self.assertFalse(list(self.path.parent.glob("*.tmp")))

    def test_queue_full_retains_original_work(self):
        first = radio()
        self.reporter.stage([first], self.fp)
        before = self.path.read_bytes()
        with patch.object(w, "MAX_PENDING_SAMPLES", 1):
            with self.assertRaisesRegex(w.MeshStateError, "queue full"):
                self.reporter.stage([radio()], self.fp)
        self.assertEqual(before, self.path.read_bytes())

    def test_queue_bytes_bound(self):
        with patch.object(w, "MAX_QUEUE_BYTES", 100):
            with self.assertRaises(w.MeshStateError):
                self.reporter.stage([radio()], self.fp)
        self.assertEqual(json.loads(self.path.read_text())["observations"], [])

    def test_counter_epochs_continuity_and_reset_causes(self):
        first = radio()
        prev = w._sample(first, str(uuid.uuid4()), {}, time.time())["radio"]
        next_radio = copy.deepcopy(first)
        next_radio.update(observed_at=first["observed_at"] + 1, uptime_seconds=1001,
                          transport="ble", session="new-connection")
        next_radio["uptime_at"] = next_radio["health_at"] = next_radio["counters_at"] = next_radio["observed_at"]
        next_radio["counters"]["num_total_nodes"] = 2
        unchanged = w._sample(next_radio, str(uuid.uuid4()), prev, time.time())["radio"]
        self.assertEqual(unchanged["epoch"], prev["epoch"])
        for delta in ({"uptime_seconds": 1}, {"firmware_version": "new-fw"},
                      {"profile": "meshtastic-basic-v1"},
                      {"counters": {"num_packets_tx": 100, "num_packets_rx": 200, "num_tx_relay": 9}},
                      {"counters": {"num_packets_rx_bad": 0, "num_packets_rx": 1}}):
            with self.subTest(delta=delta):
                changed = w._sample({**next_radio, **delta}, str(uuid.uuid4()), prev, time.time())["radio"]
                self.assertNotEqual(changed["epoch"], prev["epoch"])
                self.assertGreater(changed["seq"], prev["seq"])

    def test_staging_preserves_all_intermediate_uptime_events(self):
        at = time.time() - 600
        samples = [radio(at + i * 300, uptime_seconds=1000 + i * 300) for i in range(2)]
        batches = self.prepare(samples)
        nodes = json.loads(batches[0]["payload"])["nodes"]
        self.assertEqual([n["telemetry"]["uptime_seconds"] for n in nodes], [1000, 1300])
        self.assertEqual(nodes[0]["telemetry"]["counter_epoch"], nodes[1]["telemetry"]["counter_epoch"])
        self.assertEqual([n["telemetry"]["sample_sequence"] for n in nodes], [1, 2])

    def test_pending_request_is_not_mutated_by_new_capture(self):
        old = self.prepare([radio()])[0]
        self.reporter.stage([radio(time.time() - 1, uptime_seconds=1009)], self.fp)
        self.reporter.prepare([], ACCOUNT_A, time.time())
        self.assertEqual(self.reporter.pending(ACCOUNT_A)[0], old)
        self.assertEqual(len(self.reporter.pending(ACCOUNT_A)), 2)

    def test_telemetry_and_node_count_limits(self):
        at = time.time() - 200
        readings = [radio(at + i, uptime_seconds=1000 + i) for i in range(101)]
        discovery = [{"network": "meshtastic", "node_id": f"{i:08x}", "heard_ts": at} for i in range(601)]
        batches = self.prepare(readings, discovery)
        for batch in batches:
            nodes = json.loads(batch["payload"])["nodes"]
            self.assertLessEqual(len(nodes), 500)
            self.assertLessEqual(sum("telemetry" in n for n in nodes), w.MESH_SAMPLE_CHUNK)
            self.assertLessEqual(len(batch["payload"].encode("utf-8")), w.MAX_MESH_BYTES)
        self.assertEqual(sum(len(b["sample_ids"]) for b in batches), 101)
        self.assertEqual(sum(len(b["items"]) for b in batches), 702)

    def test_encoded_byte_limit_including_multibyte_names(self):
        nodes = [{"network": "meshtastic", "node_id": f"{i:08x}", "heard_ts": time.time() - 1,
                  "name": "📻" * 80} for i in range(8)]
        with patch.object(w, "MAX_MESH_BYTES", 1000):
            batches = self.prepare(nodes=nodes)
        self.assertGreater(len(batches), 1)
        self.assertTrue(all(len(b["payload"].encode("utf-8")) <= 1000 for b in batches))

    def test_quarantine_bad_time_without_blocking_valid_work(self):
        bad = radio(health_at=time.time() + 20)
        batches = self.prepare([bad, radio()])
        self.assertEqual(sum(len(b["sample_ids"]) for b in batches), 1)
        self.assertEqual(self.reporter.status(self.fp)["rejected"], 1)
        self.assertEqual(json.loads(self.path.read_text())["quarantine"][0]["capture_id"], bad["capture_id"])

    def test_lost_response_restart_retries_identical_bytes(self):
        client = self.client()
        saved_receipt = {}
        def committed_then_lost(*args, **kwargs):
            saved_receipt.update(receipt(kwargs["data"]))
            raise requests.Timeout()
        client.session.post.side_effect = committed_then_lost
        first = client.upload_mesh([], [radio()], self.reporter)
        self.assertFalse(first.ok)
        original = client.session.post.call_args.kwargs["data"]
        reloaded = w.MeshReporter(self.path)
        retry_client = self.client()
        retry_client.session.post.side_effect = None
        retry_client.session.post.return_value = response(body=saved_receipt)
        with patch.object(w.time, "time", return_value=time.time() + 10):
            result = retry_client.upload_mesh([], [], reloaded)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(retry_client.session.post.call_args.kwargs["data"], original)
        self.assertEqual(len(reloaded.receipts(self.fp)), 1)
        self.assertEqual(reloaded.pending(ACCOUNT_A), [])
        saved_sample = reloaded.receipts(self.fp)[0]["receipt"]["samples"][0]
        self.assertEqual(saved_sample["forwarded"], 0)
        self.assertIsNone(saved_sample["attempted"])
        self.assertEqual(result.influence, 0.125)

    def test_failed_receipt_persistence_keeps_original_retry(self):
        batch = self.prepare([radio()])[0]
        answer = receipt(json.loads(batch["payload"]))
        with patch.object(w.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(w.MeshStateError):
                self.reporter.acknowledge(batch, answer)
        reloaded = w.MeshReporter(self.path)
        self.assertEqual(reloaded.pending(ACCOUNT_A), [batch])
        self.assertEqual(reloaded.receipts(self.fp), [])
        reloaded.acknowledge(batch, answer)
        reloaded = w.MeshReporter(self.path)
        self.assertEqual(len(reloaded.receipts(self.fp)), 1)
        self.assertEqual(reloaded.pending(ACCOUNT_A), [])

    def test_acknowledgment_updates_discovery_atomically(self):
        node = {"network": "meshtastic", "node_id": "12345678", "heard_ts": time.time() - 1}
        batch = self.prepare(nodes=[node])[0]
        self.reporter.acknowledge(batch, receipt(json.loads(batch["payload"])))
        self.reporter.prepare([node], ACCOUNT_A, time.time())
        self.assertEqual(self.reporter.pending(ACCOUNT_A), [])
        self.assertIn("meshtastic:12345678", json.loads(self.path.read_text())["accounts"][ACCOUNT_A]["sent"])

    def test_context_failure_preserves_unverified_measurement(self):
        for status, body in ((401, {}), (403, {}), (302, {}), (200, {"account_id": "bad"})):
            with self.subTest(status=status):
                client = self.client()
                client.session.get.return_value = response(status, body)
                result = client.upload_mesh([], [radio()], self.reporter)
                self.assertFalse(result.ok)
                client.session.post.assert_not_called()
                self.assertTrue(self.reporter.status(self.fp)["unverified"])
                self.assertIs(client.session.get.call_args.kwargs["allow_redirects"], False)

    def test_key_changed_before_send_keeps_samples(self):
        client = self.client()
        result = client.upload_mesh([], [radio()], self.reporter, binding_valid=lambda: False)
        self.assertFalse(result.ok)
        client.session.get.assert_not_called()
        client.session.post.assert_not_called()
        self.assertEqual(self.reporter.status(self.fp)["unverified"], 1)

    def test_inflight_receipt_cannot_credit_new_account(self):
        client = self.client()
        current = {"key": KEY_A}
        def upload(*a, **kw):
            current["key"] = KEY_B
            return response(body=receipt(kw["data"]))
        client.session.post.side_effect = upload
        result = client.upload_mesh([], [radio()], self.reporter, binding_valid=lambda: current["key"] == KEY_A)
        self.assertTrue(result.ok)
        self.assertEqual(self.reporter.receipts(self.fp)[0]["account_id"], ACCOUNT_A)
        self.reporter.bind(w.credential_fingerprint(KEY_B), ACCOUNT_B)
        self.assertEqual(len(self.reporter.receipts(self.fp)), 1)
        self.assertEqual(self.reporter.receipts(w.credential_fingerprint(KEY_B)), [])

    def test_429_and_5xx_backoff_does_not_reallocate_request(self):
        for status in (429, 500):
            with self.subTest(status=status):
                client = self.client()
                client.session.post.side_effect = None
                client.session.post.return_value = response(status, {"error": "busy"}, {"Retry-After": "120"})
                result = client.upload_mesh([], [radio()], self.reporter)
                self.assertFalse(result.ok)
                batch = self.reporter.pending(ACCOUNT_A)[0]
                self.assertGreater(batch["retry_at"], time.time() + 110)
                client.session.post.reset_mock()
                client.upload_mesh([], [], self.reporter)
                client.session.post.assert_not_called()
                self.assertEqual(self.reporter.pending(ACCOUNT_A)[0]["payload"], batch["payload"])

    def test_definitive_conflict_is_dropped_and_later_work_can_proceed(self):
        client = self.client()
        self.prepare([radio()])
        second = radio(time.time() - 1, uptime_seconds=1009)
        calls = []
        def upload(*a, **kw):
            calls.append(kw["data"])
            return response(409, {"error": "sample_conflict"}) if len(calls) == 1 else response(body=receipt(kw["data"]))
        client.session.post.side_effect = upload
        result = client.upload_mesh([], [second], self.reporter)
        self.assertFalse(result.ok)
        self.assertEqual(result.telemetry, 1)
        stored = json.loads(self.path.read_text())
        self.assertEqual(stored["batches"], [])
        self.assertIn("sample_conflict", result.error)
        self.assertEqual(self.reporter.status(self.fp)["error"], "")

    def test_rejected_health_never_takes_nodes_with_it(self):
        client = self.client()
        heard = time.time() - 30
        nodes = [{"network": "meshtastic", "node_id": f"{i:08x}", "name": f"n{i}", "heard_ts": heard} for i in range(1, 4)]
        bodies = []
        def upload(*a, **kw):
            body = json.loads(kw["data"])
            bodies.append(body)
            if any("telemetry" in n for n in body["nodes"]):
                return response(409, {"error": "radio_reporting_elsewhere", "message": "conflict"})
            return response(body=receipt(body))
        client.session.post.side_effect = upload
        result = client.upload_mesh(nodes, [radio()], self.reporter)
        self.assertEqual(result.nodes, 3)
        self.assertTrue(all(len(b["nodes"]) == 1 for b in bodies if any("telemetry" in n for n in b["nodes"])))
        status = self.reporter.status(self.fp)
        self.assertEqual(status["pending"], 0)
        self.assertIn("radio_reporting_elsewhere", status["error"])
        client.upload_mesh(nodes, [radio(uptime_seconds=1100)], self.reporter)
        self.assertIn("radio_reporting_elsewhere", self.reporter.status(self.fp)["error"])
        client.session.post.side_effect = lambda *a, **kw: response(body=receipt(kw["data"]))
        client.upload_mesh(nodes, [radio(uptime_seconds=1200)], self.reporter)
        self.assertEqual(self.reporter.status(self.fp)["error"], "")

    def test_rejected_discovery_is_sent_again(self):
        client = self.client()
        nodes = [{"network": "meshtastic", "node_id": "0000abcd", "name": "n", "heard_ts": time.time() - 30}]
        client.session.post.side_effect = lambda *a, **kw: response(400, {"error": "invalid_batch"})
        self.assertEqual(client.upload_mesh(nodes, [], self.reporter).nodes, 0)
        client.session.post.side_effect = lambda *a, **kw: response(body=receipt(kw["data"]))
        self.assertEqual(client.upload_mesh(nodes, [], self.reporter).nodes, 1)

    def test_sequence_conflict_is_resent_not_dropped(self):
        client = self.client()
        client.session.post.side_effect = lambda *a, **kw: response(409, {"error": "sequence_conflict"})
        client.upload_mesh([], [radio()], self.reporter)
        stored = json.loads(self.path.read_text())
        self.assertEqual([b["status"] for b in stored["batches"]], ["pending"])

    def test_receipts_record_their_upload(self):
        client = self.client()
        nodes = [{"network": "meshtastic", "node_id": f"{i:08x}", "name": "n", "heard_ts": time.time() - 30}
                 for i in range(1, 151)]
        client.upload_mesh(nodes, [], self.reporter)
        client.upload_mesh([], [radio()], self.reporter, report=True)
        records = self.reporter.receipts(self.fp)
        self.assertEqual(len(records), 3)
        self.assertEqual(records[0]["upload_id"], records[1]["upload_id"])
        self.assertNotEqual(records[1]["upload_id"], records[2]["upload_id"])
        self.assertEqual([r["report"] for r in records], [False, False, True])

    def test_receipt_history_is_bounded(self):
        client = self.client()
        client.session.post.side_effect = lambda *a, **kw: response(body=receipt(kw["data"]))
        for i in range(w.MAX_RECEIPTS + 5):
            client.upload_mesh([], [radio(time.time() - 100 + i * 0.01, uptime_seconds=1000 + i)], self.reporter)
        self.assertEqual(len(self.reporter.receipts(self.fp)), w.MAX_RECEIPTS)

    def test_redirect_post_is_not_followed_or_acknowledged(self):
        client = self.client()
        client.session.post.side_effect = None
        client.session.post.return_value = response(302)
        result = client.upload_mesh([], [radio()], self.reporter)
        self.assertFalse(result.ok)
        self.assertFalse(client.session.post.call_args.kwargs["allow_redirects"])
        self.assertEqual(self.reporter.receipts(self.fp), [])

    def test_nonfinite_capture_values_are_persisted_as_unknown(self):
        sample = radio()
        sample["health"].update(voltage=math.inf, channel_utilization=math.nan)
        sample["counters"]["num_packets_tx"] = 10 ** 400
        batch = self.prepare([sample])[0]
        tel = json.loads(batch["payload"])["nodes"][0]["telemetry"]
        self.assertIsNone(tel["health"]["voltage"])
        self.assertIsNone(tel["counters"]["num_packets_tx"])
        self.assertNotIn("NaN", batch["payload"])

    def test_edited_pending_bytes_are_detected_on_restart(self):
        self.prepare([radio()])
        state = json.loads(self.path.read_text())
        state["batches"][0]["payload"] = state["batches"][0]["payload"].replace("Local radio", "Altered radio")
        self.path.write_text(json.dumps(state))
        reporter = w.MeshReporter(self.path)
        client = self.client()
        result = client.upload_mesh([], [], reporter)
        self.assertFalse(result.ok)
        self.assertIn("checksum", result.error)
        client.session.post.assert_not_called()

    def test_established_capability_survives_restart_before_localstats_arrives(self):
        self.prepare([radio()])
        self.reporter = w.MeshReporter(self.path)
        self.reporter.stage([radio(time.time() - 1, profile="meshtastic-devicemetrics-v1", uptime_seconds=1009,
                                  counters={}, counters_at=None, transport="ble")], self.fp)
        self.reporter.prepare([], ACCOUNT_A, time.time())
        tel = [json.loads(b["payload"])["nodes"][0]["telemetry"] for b in self.reporter.pending(ACCOUNT_A)]
        self.assertEqual(tel[1]["capability_profile"], "meshtastic-localstats-v1")
        self.assertEqual(tel[0]["counter_epoch"], tel[1]["counter_epoch"])

    def test_expired_immutable_retry_can_receive_stored_receipt(self):
        batch = self.prepare([radio()])[0]
        answer = receipt(json.loads(batch["payload"]))
        client = self.client()
        client.session.post.side_effect = None
        client.session.post.return_value = response(body=answer)
        with patch.object(w.time, "time", return_value=time.time() + 10 * 86400):
            result = client.upload_mesh([], [], self.reporter)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(client.session.post.call_args.kwargs["data"], batch["payload"].encode())

    def test_context_error_survives_fresh_capture_and_is_account_isolated(self):
        client = self.client()
        client.session.get.return_value = response(401, {})
        client.upload_mesh([], [radio()], self.reporter)
        self.reporter.stage([radio()], self.fp)
        self.reporter = w.MeshReporter(self.path)
        self.assertIn("key rejected", self.reporter.status(self.fp)["error"])
        self.assertFalse(self.reporter.status(w.credential_fingerprint(KEY_B))["error"])

    def test_acquisition_queue_does_not_persist_message_payloads(self):
        value = radio(message="private", decoded={"text": "private"})
        value["node"]["message"] = "private"
        value["health"]["payload"] = "private"
        self.reporter.stage([value], self.fp)
        self.assertNotIn("private", self.path.read_text())

    def test_full_queue_drains_old_batch_before_new_discovery(self):
        old = self.prepare([radio()])[0]
        client = self.client()
        node = {"network": "meshtastic", "node_id": "12345678", "heard_ts": time.time() - 1}
        with patch.object(w, "MAX_PENDING_SAMPLES", 1):
            result = client.upload_mesh([node], [], self.reporter)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(client.session.post.call_args_list[0].kwargs["data"], old["payload"].encode())
        self.assertEqual(client.session.post.call_count, 2)
        self.assertFalse(self.reporter.pending(ACCOUNT_A))

    def test_full_capture_handoff_retries_after_old_work_drains(self):
        old = self.prepare([radio()])[0]
        fresh = radio(time.time() - 1, uptime_seconds=1009)
        client = self.client()
        with patch.object(w, "MAX_PENDING_SAMPLES", 1):
            result = client.upload_mesh([], [fresh], self.reporter)
        self.assertTrue(result.ok, result.error)
        self.assertFalse(self.reporter.pending(ACCOUNT_A))
        self.assertEqual(client.session.post.call_args_list[0].kwargs["data"], old["payload"].encode())
        self.assertEqual(result.telemetry, 2)

    def test_full_staged_observations_defer_excess_discovery(self):
        sample = radio()
        node = {"network": "meshtastic", "node_id": "12345678", "heard_ts": time.time() - 1}
        client = self.client()
        with patch.object(w, "MAX_PENDING_SAMPLES", 1):
            first = client.upload_mesh([node], [sample], self.reporter)
            second = client.upload_mesh([node], [], self.reporter)
        self.assertTrue(first.ok, first.error)
        self.assertTrue(second.ok, second.error)
        self.assertEqual(first.telemetry, 1)
        self.assertEqual(second.nodes, 1)

    def test_late_observations_do_not_reuse_allocated_sequences(self):
        at = time.time() - 10
        self.prepare([radio(at)])
        self.reporter.stage([radio(at - 1), radio(at - 2)], self.fp)
        self.reporter.prepare([], ACCOUNT_A, time.time())
        self.reporter.stage([radio(at + 1, uptime_seconds=1001)], self.fp)
        self.reporter.prepare([], ACCOUNT_A, time.time())
        telemetry = [n["telemetry"] for b in self.reporter.pending(ACCOUNT_A) for n in json.loads(b["payload"])["nodes"]]
        self.assertEqual([t["sample_sequence"] for t in telemetry], [1, 2, 3, 4])
        self.assertEqual(len({t["counter_epoch"] for t in telemetry}), 1)


class MappingTest(unittest.TestCase):
    def test_identity_name_role_and_fields(self):
        node = radio(network="meshcore")["node"]
        node.update(node_id=node["node_id"].upper(), public_key=node["public_key"].upper(), name="\x00A\n" + "b" * 90,
                    node_type="ROUTER", lat=0, lon=0, snr=0, hops=0, message="private text")
        wire = w._wire_node(node, time.time(), time.time())
        self.assertEqual(wire["node_type"], "REPEATER")
        self.assertEqual(len(wire["name"]), 80)
        self.assertEqual(wire["lat"], 0)
        self.assertEqual(wire["hops"], 0)
        self.assertNotIn("message", wire)
        self.assertEqual(wire["public_key"], node["public_key"].lower())
        for delta in ({"public_key": "g" * 64}, {"node_id": "f" * 16}, {"public_key": "a" * 16}):
            self.assertIsNone(w._wire_node({**node, **delta}, time.time(), time.time()))

    def test_bounds_zero_fractional_and_external_power(self):
        value = radio(firmware_version=None)
        value["health"] = {"battery_level": 101, "voltage": 0, "channel_utilization": math.nan, "queue_length": 1.5}
        value["counters"] = {"num_packets_tx": 0, "num_packets_rx": -1, "num_tx_relay": 1.5,
                             "relay_tx_attempts": 7, "num_rx_dupe": True}
        tel = w._sample(value, str(uuid.uuid4()), {}, time.time())["wire"]["telemetry"]
        self.assertEqual(tel["firmware_version"], "unknown")
        self.assertTrue(tel["health"]["external_power"])
        self.assertIsNone(tel["health"]["battery_level"])
        self.assertEqual(tel["health"]["voltage"], 0)
        self.assertIsNone(tel["health"]["queue_length"])
        self.assertEqual(tel["counters"]["num_packets_tx"], 0)
        for k in ("num_packets_rx", "num_tx_relay", "relay_tx_attempts", "num_rx_dupe"):
            self.assertIsNone(tel["counters"][k])

    def test_meshcore_profile_no_inferred_forwarding_or_gps(self):
        value = radio(network="meshcore", profile="meshcore-stats-v1")
        value["node"].update(lat=12, lon=34)
        value.update(internal_gnss=True, position={"lat": 12, "lon": 34, "provenance": "own_gps",
                     "accuracy_meters": 1, "measured_at": value["observed_at"]})
        tel = w._sample(value, str(uuid.uuid4()), {}, time.time())["wire"]["telemetry"]
        self.assertEqual(tel["capability_profile"], "meshcore-companion-v1")
        for k in ("num_tx_relay", "num_tx_relay_canceled", "relay_tx_failed"):
            self.assertIsNone(tel["counters"][k])
        self.assertNotIn("position", tel)

    def test_late_groups_do_not_move_uptime_timestamp(self):
        value = radio()
        value["health_at"] += 3
        with self.assertRaisesRegex(ValueError, "acquisition time"):
            w._sample(value, str(uuid.uuid4()), {}, time.time())
        value = radio()
        value["uptime_at"] -= 3
        with self.assertRaisesRegex(ValueError, "uptime"):
            w._sample(value, str(uuid.uuid4()), {}, time.time())

    def test_meshcore_full_keys_do_not_collide_on_short_prefix(self):
        a = radio(network="meshcore")["node"]
        b = {**a, "public_key": a["public_key"][:16] + "cd" * 24}
        self.assertNotEqual(w._identity(a), w._identity(b))

    def test_non_bmp_names_respect_server_utf16_limit(self):
        node = {**radio()["node"], "name": "📻" * 80}
        wire = w._wire_node(node, time.time(), time.time())
        self.assertEqual(len(wire["name"].encode("utf-16-le")) // 2, 80)


class ReceiptTest(unittest.TestCase):
    def setUp(self):
        self.body = {"batch_id": str(uuid.uuid4()), "nodes": [w._sample(radio(), str(uuid.uuid4()), {}, time.time())["wire"]]}
        self.good = receipt(self.body)

    def test_requires_success_object_and_exact_sample_set(self):
        self.assertTrue(w.mesh_accepted(200, json.dumps(self.good), self.body))
        self.assertFalse(w.mesh_accepted(200, json.dumps(self.good)))
        for raw in ("", "<html>challenge</html>", "[]", "null", "{}"):
            self.assertFalse(w.mesh_accepted(200, raw, self.body))
        for changes in ({"ok": 1}, {"ok": False}, {"receipt_id": ""}, {"receipt_id": str(uuid.UUID(int=0))},
                        {"batch_id": str(uuid.uuid4())}, {"reports_accepted": True}, {"reports_accepted": 0},
                        {"samples": []}, {"samples": self.good["samples"] * 2},
                        {"samples": [{"sample_id": str(uuid.uuid4())}]}, {"influence": float("nan")}):
            with self.subTest(changes=changes):
                self.assertFalse(w.mesh_accepted(200, json.dumps({**self.good, **changes}), self.body))
        self.assertFalse(w.mesh_accepted(500, json.dumps(self.good), self.body))

    def test_discovery_only_requires_matching_empty_receipt(self):
        body = {"batch_id": str(uuid.uuid4()), "nodes": []}
        good = receipt(body)
        self.assertTrue(w.mesh_accepted(200, json.dumps(good), body))
        good["samples"] = self.good["samples"]
        self.assertFalse(w.mesh_accepted(200, json.dumps(good), body))

    def test_malformed_nullable_counts_cannot_poison_receipt_archive(self):
        for count in ("1", {}, True, -1, 1.5, 2 ** 63):
            bad = copy.deepcopy(self.good)
            bad["samples"][0]["forwarded"] = count
            self.assertFalse(w.mesh_accepted(200, json.dumps(bad), self.body))


if __name__ == "__main__":
    unittest.main()
