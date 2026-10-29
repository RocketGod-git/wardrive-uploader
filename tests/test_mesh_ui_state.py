"""Account-scoped display, settings migration and mesh orchestration regression tests."""
import importlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from wdgwars_uploader import config, wardrift
from wdgwars_uploader.poller import Poller
from wdgwars_uploader.state import LiveState

# Wardriving imports platform/USB backends unrelated to the mesh coordination
# under test. Replace that boundary, leaving real core/state/reporter code intact.
_service = types.ModuleType("wdgwars_uploader.wardrive.service")
_service.Wardriver = Mock
with patch.dict(sys.modules, {"wdgwars_uploader.wardrive.service": _service}):
    core = importlib.import_module("wdgwars_uploader.core")

A = "11111111-1111-4111-8111-111111111111"
B = "22222222-2222-4222-8222-222222222222"


def receipt(rid="receipt-a", account=A, influence=.125, accepted=1780000000):
    return {"account_id": account, "accepted_at": accepted, "nodes": 1,
            "receipt": {"receipt_id": rid, "batch_id": "batch-a", "reports_accepted": 1,
                        "currency": 2, "influence": influence, "policy_version": "collector-v2-availability-60",
                        "samples": [{"sample_id": "sample-a", "availability_seconds": 300,
                                     "forwarded": 0, "suppressed": 0, "attempted": None, "failed": None,
                                     "currency": 2, "influence": influence}]}}


class TemporaryState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.cfgdir = patch.object(config, "_DIR", self.dir)
        self.cfgdir.start()
        self.addCleanup(self.cfgdir.stop)


class SettingsTests(TemporaryState):
    def test_legacy_ble_defaults_apply_only_to_meshcore(self):
        devices = config.mesh_devices([{"protocol": "meshcore", "port": "COM1"},
                                       {"protocol": "meshtastic", "port": "COM2"}],
                                      transport="ble", ble_address="AA:BB")
        self.assertEqual((devices[0]["transport"], devices[0]["ble_address"]), ("ble", "AA:BB"))
        self.assertEqual((devices[1]["transport"], devices[1]["port"]), ("serial", "COM2"))
        self.assertEqual(devices[1]["ble_address"], "")

    def test_explicit_transport_and_baud_survive_save_reload(self):
        cfg = config.Config()
        cfg.set("mesh_devices", config.mesh_devices([{"protocol": "meshtastic", "transport": "ble",
                    "ble_address": "RADIO-UUID", "baud": 230400, "enabled": False}]))
        cfg.save()
        device = config.Config().get("mesh_devices")[0]
        self.assertEqual((device["transport"], device["ble_address"], device["baud"], device["enabled"]),
                         ("ble", "RADIO-UUID", 230400, False))

    def test_empty_device_list_keeps_legacy_default_on_restart(self):
        cfg = config.Config()
        self.assertEqual(len(cfg.get("mesh_devices")), 1)
        cfg.set("mesh_devices", [])
        cfg.save()
        self.assertEqual(config.Config().get("mesh_devices")[0]["protocol"], "meshcore")

    def test_legacy_single_device_migrates(self):
        (self.dir / "config.json").write_text(json.dumps({"mesh_transport": "ble", "mesh_ble_address": "AA:BB"}))
        self.assertEqual(config.Config().get("mesh_devices")[0]["ble_address"], "AA:BB")

    def test_invalid_config_shape_uses_defaults(self):
        (self.dir / "config.json").write_text("null")
        self.assertEqual(config.Config().get("mesh_devices")[0]["transport"], "serial")


class ReceiptStateTests(TemporaryState):
    def test_reconciliation_preserves_zero_null_fraction_and_deduplicates(self):
        state = LiveState()
        r = receipt()
        state.reconcile_mesh({"account_id": A, "pending": 0}, [r, r])
        state.reconcile_mesh({"account_id": A, "pending": 0}, [r])
        row = state.receipts("wardrift")[0]
        self.assertEqual(row["forwarded"], 0)
        self.assertEqual(row["suppressed"], 0)
        self.assertIsNone(row["attempted"])
        self.assertIsNone(row["failed"])
        self.assertEqual(row["influence"], .125)
        self.assertEqual((row["currency"], row["availability_seconds"], row["telemetry"]), (2, 300, 1))
        self.assertEqual(len(state.receipts("wardrift")), 1)
        self.assertFalse((self.dir / "receipts_wardrift.jsonl").exists())

    def test_account_switch_and_key_clear_hide_previous_account_rewards(self):
        state = LiveState()
        state.reconcile_mesh({"account_id": A}, [receipt(), receipt("receipt-b", B, .25)])
        self.assertEqual(state.receipts("wardrift")[0]["influence"], .125)
        state.reconcile_mesh({"account_id": B}, [receipt(), receipt("receipt-b", B, .25)])
        self.assertEqual(state.receipts("wardrift")[0]["account_id"], B)
        self.assertEqual(state.receipts("wardrift")[0]["influence"], .25)
        state.reconcile_mesh({"account_id": ""}, [receipt()])
        self.assertEqual(state.receipts("wardrift"), [])

    def test_recent_receipts_restore_from_archive_without_duplicating_history(self):
        rows = [receipt(str(i), accepted=1780000000 + i) for i in range(502)]
        state = LiveState()
        state.reconcile_mesh({"account_id": A}, rows)
        self.assertEqual(len(state.receipts("wardrift")), 500)
        self.assertEqual(state.receipts("wardrift")[0]["receipt_id"], "501")
        restarted = LiveState()
        restarted.reconcile_mesh({"account_id": A}, rows + [rows[0]])
        self.assertEqual(restarted.receipts("wardrift"), state.receipts("wardrift"))
        self.assertEqual(restarted.snapshot()["wardrift"]["earned_total"], state.snapshot()["wardrift"]["earned_total"])

    def test_one_row_per_upload_and_none_for_radio_reports(self):
        state = LiveState()
        batches = [{**receipt(f"r{i}", accepted=1780000000 + i), "upload_id": "upload-1", "report": False}
                   for i in range(8)]
        report = {**receipt("r-report", accepted=1780000900), "upload_id": "upload-2", "report": True}
        state.reconcile_mesh({"account_id": A}, batches + [report])
        rows = state.receipts("wardrift")
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["sent"], rows[0]["telemetry"], rows[0]["currency"]), (8, 8, 16))
        self.assertEqual(rows[0]["availability_seconds"], 2400)
        self.assertEqual(rows[0]["forwarded"], 0)
        self.assertIsNone(rows[0]["attempted"])
        self.assertEqual(rows[0]["t"], 1780000007)
        self.assertEqual(rows[0]["summary"], "8 nodes acknowledged, 8 health samples, +16 currency, +1 influence")

    def test_old_unverified_mesh_rows_cannot_become_current_receipts(self):
        state = LiveState()
        state.add_receipt("wardrift", "mesh", True, "old sent count", currency=999)
        state.add_receipt("wardrift", "wifi", True, "CSV accepted", sent=0, influence=0)
        state.reconcile_mesh({"account_id": A}, [receipt()])
        rows = state.receipts("wardrift")
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["kind"] for row in rows}, {"mesh", "wifi"})
        self.assertNotIn("influence", next(row for row in rows if row["kind"] == "wifi"))
        self.assertEqual(next(row for row in rows if row["kind"] == "mesh")["currency"], 2)

    def test_non_mesh_receipts_keep_existing_storage_and_order(self):
        state = LiveState()
        for site in ("wdgwars", "wardrift"):
            # Existing receipt history uses insertion order and does not dedup IDs.
            with patch("wdgwars_uploader.state.time.time", side_effect=[200, 100]):
                state.add_receipt(site, "aircraft", True, "first", receipt_id="same", sent=0, influence=0)
                state.add_receipt(site, "wifi", True, "second", receipt_id="same", sent=2)
            stored = [json.loads(line) for line in state._receipts_file(site).read_text().splitlines()]
            self.assertEqual(len(stored), 2)
            self.assertNotIn("sent", stored[0])
            self.assertNotIn("influence", stored[0])
            self.assertEqual([r["summary"] for r in state.receipts(site)], ["second", "first"])
            self.assertEqual({r["summary"] for r in state.receipts(site)}, {"first", "second"})


class CoreCoordinationTests(TemporaryState):
    def setUp(self):
        super().setUp()
        self.obj = core.UploaderCore.__new__(core.UploaderCore)
        self.obj.cfg = config.Config()
        self.obj.cfg.set("wardrift_api_key", "test-key")
        self.obj.state = LiveState()
        self.obj.capture = Mock()
        self.obj.capture.pending_samples.return_value = []
        self.obj.capture.snapshot.return_value = []
        self.obj.wardrift_mesh = Mock()
        self.obj.wardrift_mesh.status.return_value = {"account_id": A, "pending": 0}
        self.obj.wardrift_mesh.receipts.return_value = []
        self.obj._wardrift_mesh_lock = threading.RLock()
        self.obj._mesh_schedule_lock = threading.Lock()
        self.obj._mesh_upload_thread = None
        self.obj._mesh_stopping = threading.Event()
        self.obj._wardrift_report_error = ""

    def test_capture_is_acknowledged_only_after_durable_stage(self):
        self.obj.capture.pending_samples.return_value = [{"capture_id": "capture-a"}]
        self.obj.wardrift_mesh.stage.return_value = ["capture-a"]
        order = []
        self.obj.wardrift_mesh.stage.side_effect = lambda samples: order.append("persist") or ["capture-a"]
        self.obj.capture.acknowledge_samples.side_effect = lambda ids: order.append("handoff-ack")
        self.obj._stage_mesh_samples()
        self.assertEqual(order, ["persist", "handoff-ack"])
        self.obj.capture.acknowledge_samples.reset_mock()
        self.obj.wardrift_mesh.stage.side_effect = wardrift.MeshStateError("disk full")
        self.obj._stage_mesh_samples()
        self.obj.capture.acknowledge_samples.assert_not_called()
        self.assertIn("disk full", self.obj.state.snapshot()["wardrift_mesh"]["error"])

    def test_quiet_tick_reconciles_receipt_without_activity_feed_message(self):
        self.obj.wardrift_mesh.receipts.return_value = [receipt()]
        with patch.object(core.wardrift, "WardriftClient") as cls:
            cls.return_value.upload_mesh.return_value = wardrift.MeshResult(nodes=1, telemetry=1)
            self.obj._upload_wardrift_mesh("test-key", core.CycleReport(), poll=False, quiet=True)
        self.assertEqual(self.obj.state.receipts("wardrift")[0]["influence"], .125)
        self.assertEqual(self.obj.state.snapshot()["events"], [])

    def test_wardrift_failures_do_not_change_wdgwars_mesh_payload_or_acknowledgment(self):
        self.obj.cfg.update({"api_key": "wdgwars-key", "adsb_enabled": False, "mesh_devices": []})
        node = {"node_id": "aabbccdd", "network": "meshtastic", "type": "MESHCORE",
                "node_type": "COMPANION", "lat": 40.0, "lon": -74.0, "uploadable": True,
                "tele": {"battery": 75}, "telemetry": {"uptime_seconds": 120},
                "currency": 99, "influence": .5}
        no_position = {"node_id": "11223344", "network": "meshtastic", "uploadable": False}
        self.obj.capture.snapshot.return_value = [node, no_position]
        self.obj.capture.status.return_value = []
        self.obj.capture.pending_samples.return_value = [{"capture_id": "wardrift-only"}]
        self.obj.wardrift_mesh.stage.side_effect = wardrift.MeshStateError("Wardrift queue full")
        client = Mock()
        client.check_key.return_value = types.SimpleNamespace(ok=True)
        client.upload_json_hmac.return_value = types.SimpleNamespace(ok=True, detail={"meshcore_imported": 1})
        with patch.object(self.obj, "client", return_value=client), \
                patch.object(self.obj, "poll_devices"), \
                patch.object(self.obj, "refresh_stats"), \
                patch.object(self.obj, "_upload_wardrive"), \
                patch.object(core.wardrift, "WardriftClient") as wd:
            wd.return_value.upload_mesh.return_value = wardrift.MeshResult(ok=False, error="Wardrift unavailable")
            report = self.obj._run_cycle_inner(core.CycleReport())
        expected = {"node_id": "aabbccdd", "network": "meshtastic", "type": "MESHCORE",
                    "node_type": "COMPANION", "lat": 40.0, "lon": -74.0}
        client.upload_json_hmac.assert_called_once_with(
            {"networks": [], "aircraft": [], "meshcore_nodes": [expected]})
        self.assertEqual(report.mesh_sent, 1)
        self.assertEqual(self.obj.state.session_uploaded["mesh"], 1)
        self.assertEqual(self.obj.state.acc_mesh_nodes(), [])
        self.obj.capture.acknowledge_samples.assert_not_called()
        self.assertTrue(any("Wardrift unavailable" in error for error in report.errors))

    def test_manual_and_tick_do_not_overlap_and_new_captures_stay_queued(self):
        entered, release = threading.Event(), threading.Event()
        def upload(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(3))
            return wardrift.MeshResult(nodes=1)
        with patch.object(core.wardrift, "WardriftClient") as cls:
            cls.return_value.upload_mesh.side_effect = upload
            self.obj.wardrift_mesh_tick()
            try:
                self.assertTrue(entered.wait(2))
                self.obj.wardrift_mesh_tick()
                report = core.CycleReport()
                self.obj._upload_wardrift_mesh("test-key", report, poll=False)
                self.assertEqual(report.wardrift_mesh, "mesh upload already running")
                self.obj.capture.pending_samples.return_value = [{"capture_id": "later"}]
                self.obj._stage_mesh_samples()
                self.obj.capture.acknowledge_samples.assert_not_called()
                self.assertEqual(cls.return_value.upload_mesh.call_count, 1)
            finally:
                release.set()
                self.obj._mesh_upload_thread.join(3)
        self.assertFalse(self.obj._mesh_upload_thread.is_alive())

    def test_inflight_binding_detects_key_change(self):
        def upload(*args, **kwargs):
            self.assertTrue(kwargs["binding_valid"]())
            self.obj.cfg.set("wardrift_api_key", "next-key")
            self.obj.mesh_config_changed()
            self.assertFalse(kwargs["binding_valid"]())
            return wardrift.MeshResult()
        with patch.object(core.wardrift, "WardriftClient") as cls:
            cls.return_value.upload_mesh.side_effect = upload
            self.obj._upload_wardrift_mesh("test-key", core.CycleReport(), poll=False)
        self.obj.capture.set_credential_fingerprint.assert_called_with(wardrift.credential_fingerprint("next-key"))
        self.obj.wardrift_mesh.status.assert_called_with(wardrift.credential_fingerprint("next-key"))

    def test_shutdown_stops_capture_before_final_stage_and_reports_failure(self):
        order = []
        self.obj.capture.pending_samples.return_value = [{"capture_id": "last"}]
        self.obj.capture.stop.side_effect = lambda: order.append("stop-capture")
        self.obj.wardrift_mesh.stage.side_effect = lambda samples: order.append("persist-samples") or ["last"]
        self.obj.wardrift_mesh.save.side_effect = lambda: order.append("save-ledger")
        self.obj.stop_mesh()
        self.assertEqual(order, ["stop-capture", "persist-samples", "save-ledger"])
        self.assertTrue(self.obj._mesh_stopping.is_set())
        self.obj.wardrift_mesh.stage.side_effect = wardrift.MeshStateError("disk full")
        with self.assertRaises(wardrift.MeshStateError):
            self.obj.stop_mesh()

    def test_shutdown_does_not_wait_for_an_upload_in_progress(self):
        self.obj.capture.pending_samples.return_value = [{"capture_id": "last"}]
        self.obj.wardrift_mesh.stage.return_value = ["last"]
        holding, done = threading.Event(), threading.Event()

        def upload():
            with self.obj._wardrift_mesh_lock:
                holding.set()
                done.wait(30)
        t = threading.Thread(target=upload, daemon=True)
        t.start()
        self.assertTrue(holding.wait(2))
        started = time.monotonic()
        try:
            self.obj.stop_mesh()
        finally:
            done.set()
        self.assertLess(time.monotonic() - started, 1)
        self.obj.wardrift_mesh.stage.assert_called_once()
        self.obj.wardrift_mesh.save.assert_called_once()
        t.join(2)

    def test_poller_keeps_independent_mesh_schedule(self):
        with patch("wdgwars_uploader.poller.time.time", return_value=1000):
            poller = Poller(self.obj)
        self.assertEqual(poller.intervals["wardrift_mesh"], 900)
        self.assertEqual(poller._next["wardrift_mesh"], 1120)


@unittest.skipUnless(shutil.which("node"), "Node.js required for dashboard JavaScript validation")
class DashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (Path(__file__).parents[1] / "wdgwars_uploader/webui/dashboard.html").read_text(encoding="utf-8")

    def run_script(self, source):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard.js"
            path.write_text(source, encoding="utf-8")
            return subprocess.run(["node", str(path)], capture_output=True, text=True, check=True).stdout

    def test_all_dashboard_script_syntax(self):
        source = "\n".join(re.findall(r"<script\b[^>]*>(.*?)</script>", self.html, re.S))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard.js"
            path.write_text(source, encoding="utf-8")
            subprocess.run(["node", "--check", str(path)], capture_output=True, text=True, check=True)

    def test_mesh_receipt_uses_existing_modal_with_zero_unknown_and_fractional_values(self):
        source = "const assert=require('node:assert/strict'); let result; const openModal=html=>result=html;\n"
        source += "function escHtml" + self.html.split("function escHtml", 1)[1].split("\n", 1)[0] + "\n"
        source += "const fmt=n=>(+n||0).toLocaleString(); const precise=n=>(+n||0).toLocaleString(undefined,{maximumFractionDigits:8});\n"
        source += "function openUpload" + self.html.split("function openUpload", 1)[1].split("\n$('#up-tabs')", 1)[0] + "\n"
        source += """
const UP={src:'wardrift',rows:[{kind:'mesh',t:1780000000,summary:'<script>bad</script>',receipt_id:'r',
 sent:1,telemetry:1,availability_seconds:300,forwarded:0,suppressed:0,attempted:null,failed:null,
 currency:2,influence:0.000125}]};
openUpload(0);
assert(result.includes('Availability seconds'));
assert(result.includes('Forwarded</span><span class="v">0'));
assert(result.includes('Attempted</span><span class="v">unavailable'));
assert(result.includes('0.000125'));
assert(result.includes('&lt;script&gt;'));
assert(!result.includes('<script>'));
"""
        self.run_script(source)

    def test_late_receipt_fetch_cannot_restore_previous_account_cache(self):
        function = "async function loadUploads" + self.html.split("async function loadUploads", 1)[1].split("\nfunction wdgRowInfo", 1)[0]
        source = "const assert=require('node:assert/strict'); const window={}; const UP={cache:{},loading:{},wardriftVersion:'account-a'};\n"
        source += "let resolve; const api=()=>new Promise(r=>resolve=r);\n" + function
        source += '''
(async()=>{
 const pending=loadUploads('wardrift');
 UP.wardriftVersion='account-b';
 resolve({ok:true,list:[{account_id:'account-a'}]}); await pending;
 assert.equal(UP.cache.wardrift,undefined);
 const next=loadUploads('wardrift');
 resolve({ok:true,list:[{account_id:'account-b'}]}); await next;
 assert.equal(UP.cache.wardrift.list[0].account_id,'account-b');
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        self.run_script(source)

    def test_quiet_ack_and_account_change_invalidate_receipt_cache(self):
        function = "function renderUploads" + self.html.split("function renderUploads", 1)[1].split("\nfunction openUpload", 1)[0]
        source = "const assert=require('node:assert/strict'); const UP={src:'wardrift',cache:{},loading:{},sig:'',cycle:0};\n"
        source += "const elements={}; const $=id=>elements[id]||(elements[id]={style:{},innerHTML:''}); let loads=0; const loadUploads=()=>loads++;\n" + function
        source += '''
const s={config:{has_wardrift_key:true},wardrift_mesh:{account_id:'a',last_acknowledged:1}};
renderUploads(s); assert.equal(loads,1);
UP.cache.wardrift={_at:100,list:[{account_id:'a'}]}; UP.rows=UP.cache.wardrift.list;
s.wardrift_mesh.last_acknowledged=2;
renderUploads(s); assert.equal(loads,2); assert.deepEqual(UP.rows,[]);
UP.cache.wardrift={_at:200,list:[{account_id:'a'}]}; UP.rows=UP.cache.wardrift.list;
s.wardrift_mesh.account_id='b';
renderUploads(s); assert.equal(loads,3); assert.deepEqual(UP.rows,[]);
'''
        self.run_script(source)

    def test_cleared_key_still_shows_retained_delivery_status(self):
        status = "const wdm=" + self.html.split("const wdm=", 1)[1].split("  // Current-seen counts", 1)[0]
        source = "const assert=require('node:assert/strict'); const out={}; const $=()=>out;\n"
        source += "const s={config:{has_wardrift_key:false},wardrift_mesh:{retained:6}};\n" + status
        source += "assert(out.textContent.includes('6 retained for other keys/accounts')); assert(out.textContent.includes('set the shared Wardrift key'));\n"
        self.run_script(source)


if __name__ == "__main__":
    unittest.main()
