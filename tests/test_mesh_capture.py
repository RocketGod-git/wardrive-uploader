"""Radio-boundary tests: no physical device, OS Bluetooth service or real key."""
from __future__ import annotations

import asyncio
import copy
import sys
import threading
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from wdgwars_uploader import mesh_capture as capture


NOW = 1793188800.0
LOCAL = 0x61F882D4
PUBLIC = "ab" * 32
EVENT = types.SimpleNamespace(ERROR="error", ADVERTISEMENT="advert", NEW_CONTACT="contact")


def event(payload, kind="success"):
    return types.SimpleNamespace(type=kind, payload=payload)


def interface(local=LOCAL, firmware="2.7.15"):
    node = {"num": local, "user": {"id": f"!{local:08x}", "longName": "Local radio", "role": "CLIENT"},
            "lastHeard": NOW - 500, "deviceMetrics": {"batteryLevel": 50, "uptimeSeconds": 500},
            "localStats": {"numTxRelay": 30}}
    iface = types.SimpleNamespace(nodes={f"!{local:08x}": node},
                                  myInfo=types.SimpleNamespace(my_node_num=local),
                                  metadata=types.SimpleNamespace(firmware_version=firmware),
                                  localNode=Mock(), close=Mock(), isConnected=threading.Event())
    iface.getMyNodeInfo = Mock(return_value=node)
    iface.isConnected.set()
    return iface


def packet(group, local=LOCAL):
    return {"from": local, "fromId": f"!{local:08x}", "decoded": {"telemetry": group}}


def sample(capture_id="capture-1", *, profile="meshtastic-localstats-v1", firmware="2.7.15"):
    return {"capture_id": capture_id, "node": {"network": "meshtastic", "node_id": f"{LOCAL:08x}"},
            "observed_at": NOW, "profile": profile, "firmware_version": firmware,
            "health": {"battery_level": 0}, "health_at": NOW, "uptime_seconds": 42, "uptime_at": NOW}


class ProtoGroup:
    """Minimal protobuf descriptor semantics, independent of protobuf install."""
    def __init__(self, fields, present=()):
        self.DESCRIPTOR = types.SimpleNamespace(fields=[types.SimpleNamespace(
            name=name, json_name=json_name, has_presence=optional)
            for name, json_name, optional, value in fields])
        self.present = set(present)
        for name, _, _, value in fields:
            setattr(self, name, value)

    def HasField(self, name):
        return name in self.present


class MeshtasticCaptureTests(unittest.TestCase):
    def setUp(self):
        self.samples = []
        self.worker = capture.MeshtasticWorker("COM4", capture.MeshStore(), sample_sink=self.samples.append)
        self.worker._local_num = LOCAL
        self.iface = interface()

    def receive(self, telemetry, **kwargs):
        with patch.object(capture.time, "time", return_value=NOW):
            self.worker._on_receive(packet(telemetry, **kwargs), self.iface, self.iface)

    def test_requires_both_exact_interface_and_handshake_subject(self):
        data = packet({"deviceMetrics": {"uptimeSeconds": 10}})
        self.worker._on_receive(data, interface(), self.iface)
        self.receive({"deviceMetrics": {"uptimeSeconds": 10}}, local=LOCAL + 1)
        self.worker._local_num = None
        self.receive({"deviceMetrics": {"uptimeSeconds": 10}})
        self.assertEqual(self.samples, [])
        self.worker._local_num = LOCAL
        self.receive({"deviceMetrics": {"uptimeSeconds": 10}})
        self.assertEqual(len(self.samples), 1)

    def test_cached_node_info_cannot_override_handshake_identity(self):
        self.iface.nodes[f"!{LOCAL:08x}"]["user"]["id"] = "!deadbeef"
        self.receive({"deviceMetrics": {"uptimeSeconds": 10}})
        self.assertEqual(self.samples[-1]["node"]["node_id"], f"{LOCAL:08x}")

    def test_startup_cache_and_repeated_snapshots_are_discovery_only(self):
        for _ in range(3):
            self.worker._capture_all(self.iface)
        self.assertEqual(self.samples, [])
        self.assertEqual(self.worker.store.snapshot()[0]["heard_ts"], NOW - 500)
        self.iface.getMyNodeInfo.assert_not_called()

    def test_health_queue_failure_does_not_interrupt_meshtastic_discovery(self):
        service = capture.MeshCaptureService()
        service._capture_sample(sample())
        self.worker._sample_sink = service._capture_sample
        with patch.object(capture, "_SAMPLE_CAP", 1):
            self.receive({"deviceMetrics": {"uptimeSeconds": 10}})
        self.receive({"deviceMetrics": {"uptimeSeconds": 20}}, local=LOCAL + 1)
        self.worker._capture_all(self.iface)
        self.assertTrue(self.worker.store.snapshot())
        self.assertEqual(service.pending_samples()[0]["capture_id"], "capture-1")
        self.assertIn("queue is full", self.worker.error)

    def test_cached_and_manual_positions_remain_display_only(self):
        self.iface.nodes[f"!{LOCAL:08x}"]["position"] = {"latitude": 40.0, "longitude": -74.0}
        self.worker._capture_all(self.iface)
        self.assertEqual((self.worker.radio["node"]["lat"], self.worker.radio["node"]["lon"]), (40.0, -74.0))
        self.worker.request_position(41.0, -73.0)
        self.worker._apply_position(self.iface)
        self.iface.localNode.setFixedPosition.assert_called_once_with(41.0, -73.0, 0)
        self.assertEqual((self.worker.radio["node"]["lat"], self.worker.radio["node"]["lon"]), (41.0, -73.0))
        self.assertEqual(self.worker.position_status, "set")
        self.assertEqual(self.samples, [])
        self.assertNotIn("observed_at", self.worker.radio)

    def test_device_presence_and_supported_localstats_zero(self):
        dm = ProtoGroup([("battery_level", "batteryLevel", True, 0),
                         ("voltage", "voltage", True, 0.0),
                         ("uptime_seconds", "uptimeSeconds", True, 0)], present={"battery_level"})
        raw = types.SimpleNamespace(HasField=lambda name: name == "device_metrics", device_metrics=dm)
        self.receive({"raw": raw})
        first = self.samples[-1]
        self.assertEqual(first["health"]["battery_level"], 0)
        self.assertNotIn("voltage", first["health"])
        self.assertIsNone(first["uptime_seconds"])
        ls = ProtoGroup([("uptime_seconds", "uptimeSeconds", False, 0),
                         ("num_packets_tx", "numPacketsTx", False, 0),
                         ("num_tx_relay", "numTxRelay", False, 0)])
        raw = types.SimpleNamespace(HasField=lambda name: name == "local_stats", local_stats=ls)
        self.receive({"raw": raw})
        last = self.samples[-1]
        self.assertEqual(last["uptime_seconds"], 0)
        self.assertEqual(last["counters"]["num_packets_tx"], 0)
        self.assertEqual(last["counters"]["num_tx_relay"], 0)
        self.assertEqual(last["profile"], "meshtastic-localstats-v1")

    def test_dict_missing_localstats_keys_do_not_become_zero(self):
        self.receive({"localStats": {"numPacketsTx": 0}})
        self.assertEqual(self.samples[0]["counters"], {"num_packets_tx": 0})
        self.assertIsNone(self.samples[0]["uptime_seconds"])

    def test_external_power_follows_documented_over_100_rule(self):
        self.receive({"deviceMetrics": {"batteryLevel": 101, "voltage": 4.2}})
        self.assertIsNone(self.samples[-1]["battery_level"])
        self.assertIs(self.samples[-1]["health"]["external_power"], True)
        self.receive({"deviceMetrics": {"batteryLevel": 102}})
        self.assertIs(self.samples[-1]["health"]["external_power"], True)
        self.assertIsNone(self.samples[-1]["battery_level"])
        self.receive({"deviceMetrics": {"batteryLevel": -1}})
        self.assertIsNone(self.samples[-1]["health"]["external_power"])

    def test_buffered_event_preserves_device_acquisition_time(self):
        self.receive({"time": NOW - 300, "deviceMetrics": {"uptimeSeconds": 10, "voltage": 4.0}})
        self.assertEqual(self.samples[-1]["observed_at"], NOW - 300)
        self.assertEqual(self.samples[-1]["health_at"], NOW - 300)
        self.assertEqual(self.samples[-1]["uptime_at"], NOW - 300)
        self.assertEqual(self.samples[-1]["host_received_at"], NOW)

    def test_unset_clock_uses_receipt_time_but_bad_clock_is_not_redated(self):
        self.receive({"time": 0, "deviceMetrics": {"uptimeSeconds": 10}})
        self.assertEqual(self.samples[-1]["observed_at"], NOW)
        for bad in (True, -1, "not a clock", NOW + 121, float("nan")):
            with self.subTest(clock=bad):
                with self.assertLogs("wdgwars.capture", level="WARNING"):
                    self.receive({"time": bad, "deviceMetrics": {"uptimeSeconds": 10}})
                self.assertIn("not redated", self.worker.error)
        self.assertEqual(len(self.samples), 1)

        self.receive({"time": NOW, "deviceMetrics": {"uptimeSeconds": 11}})
        self.assertEqual(self.worker.error, "")

    def test_groups_are_immutable_events_with_original_times(self):
        self.receive({"deviceMetrics": {"uptimeSeconds": 10, "voltage": 4.0},
                      "localStats": {"uptimeSeconds": 10, "numPacketsRx": 20, "numTxRelayCanceled": 0}})
        self.assertEqual(len(self.samples), 2)
        device, stats = self.samples
        self.assertNotEqual(device["capture_id"], stats["capture_id"])
        self.assertEqual(device["health_at"], NOW)
        self.assertEqual(stats["counters_at"], NOW)
        self.assertIsNone(stats["health_at"])
        self.assertEqual(stats["counters"]["num_tx_relay_canceled"], 0)
        self.iface.nodes[f"!{LOCAL:08x}"]["user"]["longName"] = "changed"
        self.assertEqual(device["node"]["name"], "Local radio")
        self.assertNotIn("relay_tx_attempts", stats["counters"])
        self.assertNotIn("relay_unique_completed", stats["counters"])

    def test_capture_profile_identifies_the_fresh_group(self):
        self.receive({"localStats": {"numPacketsTx": 5}})
        self.assertEqual(self.samples[-1]["profile"], "meshtastic-localstats-v1")
        self.receive({"deviceMetrics": {"voltage": 4.0}})
        self.assertEqual(self.samples[-1]["profile"], "meshtastic-devicemetrics-v1")
        self.assertEqual(self.samples[-1]["counters"], {})

    def test_message_body_and_fixed_position_cannot_become_telemetry(self):
        self.worker._on_receive({"from": LOCAL, "decoded": {"text": "private message"}}, self.iface, self.iface)
        self.worker.request_position(42.1, -71.2)
        self.worker._apply_position(self.iface)
        self.assertEqual(self.samples, [])
        self.receive({"deviceMetrics": {"uptimeSeconds": 10}})
        self.assertNotIn("position", self.samples[-1])
        self.assertNotIn("private message", repr(self.samples))
        self.assertEqual(self.samples[-1]["node"]["lat"], 42.1)

    def test_stop_blocks_late_callback_and_publish(self):
        self.worker.stop()
        self.receive({"deviceMetrics": {"uptimeSeconds": 10}})
        self.worker._publish(sample())
        self.assertEqual(self.samples, [])


class MeshtasticTransportTests(unittest.TestCase):
    def test_ble_factory_uses_explicit_address_without_serial_guard(self):
        factory = Mock(return_value=object())
        module = types.ModuleType("meshtastic.ble_interface")
        class BLEInterface:
            def __init__(self, address, timeout=300):
                factory(address=address, timeout=timeout)
            def close(self):
                pass
        module.BLEInterface = BLEInterface
        worker = capture.MeshtasticWorker("", capture.MeshStore(), transport="ble", ble_address="AA:BB")
        with patch.dict(sys.modules, {module.__name__: module}), patch.object(worker, "_open_bounded", side_effect=lambda fn, deadline: fn()), patch.object(capture, "_needs_no_reset") as serial_guard:
            worker._open_iface(12)
        factory.assert_called_once_with(address="AA:BB", timeout=12)
        serial_guard.assert_not_called()

    def test_ble_factory_supports_older_sdk_without_timeout_argument(self):
        factory = Mock()
        module = types.ModuleType("meshtastic.ble_interface")
        class BLEInterface:
            def __init__(self, address):
                factory(address=address)
            def close(self):
                pass
        module.BLEInterface = BLEInterface
        worker = capture.MeshtasticWorker("", capture.MeshStore(), transport="ble", ble_address="AA:BB")
        with patch.dict(sys.modules, {module.__name__: module}), patch.object(worker, "_open_bounded", side_effect=lambda fn, deadline: fn()) as bounded:
            worker._open_iface(12)
        factory.assert_called_once_with(address="AA:BB")
        self.assertEqual(bounded.call_args.args[1], 12)

    def test_serial_factory_and_no_reset_guard(self):
        calls = []
        class SerialInterface:
            def __init__(self, **kwargs):
                calls.append(kwargs)
        module = types.ModuleType("meshtastic.serial_interface")
        module.SerialInterface = SerialInterface
        worker = capture.MeshtasticWorker("COM4", capture.MeshStore())
        with patch.dict(sys.modules, {module.__name__: module}), patch.object(worker, "_open_bounded", side_effect=lambda fn, deadline: fn()), patch.object(capture, "_needs_no_reset", return_value=True):
            worker._open_iface()
        self.assertEqual(calls, [{"devPath": "COM4"}])
        self.assertIn("COM4", capture._NO_RESET_PORTS)
        capture._NO_RESET_PORTS.discard("COM4")

    def test_ble_auto_is_refused_and_pairing_failure_propagates(self):
        module = types.ModuleType("meshtastic.ble_interface")
        closed = Mock()
        class BLEInterface:
            def __init__(self, **kwargs):
                raise PermissionError("pairing refused")
            def close(self):
                closed()
        module.BLEInterface = BLEInterface
        worker = capture.MeshtasticWorker("", capture.MeshStore(), transport="ble")
        with patch.dict(sys.modules, {module.__name__: module}):
            with self.assertRaisesRegex(ValueError, "explicit"):
                worker._open_iface()
            worker.ble_address = "AA:BB"
            with self.assertRaisesRegex(PermissionError, "pairing refused"):
                worker._open_iface()
        closed.assert_called_once()

    def test_session_always_unsubscribes_and_closes_even_unsubscribe_error(self):
        pub = Mock()
        pub.unsubscribe.side_effect = RuntimeError("already unsubscribed")
        module = types.ModuleType("pubsub")
        module.pub = pub
        worker = capture.MeshtasticWorker("COM4", capture.MeshStore())
        iface = interface()
        with patch.dict(sys.modules, {"pubsub": module}), patch.object(worker, "_open_iface", return_value=iface), patch.object(worker, "_capture_all", side_effect=RuntimeError("disconnected")):
            with self.assertRaisesRegex(RuntimeError, "disconnected"):
                worker._session()
        pub.subscribe.assert_called_once()
        pub.unsubscribe.assert_called_once()
        iface.close.assert_called_once()
        self.assertIsNone(worker._local_num)
        self.assertFalse(worker.connected)

    def test_canceled_constructor_closes_result_before_releasing_endpoint(self):
        worker = capture.MeshtasticWorker("COM4", capture.MeshStore())
        started, release = threading.Event(), threading.Event()
        iface, errors = Mock(), []
        def factory():
            started.set()
            release.wait(2)
            return iface
        def run():
            try:
                worker._open_bounded(factory, 0.02)
            except Exception as e:
                errors.append(e)
        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(started.wait(1))
        worker.stop()
        # A constructor that cannot yet close retains its reservation.
        self.assertTrue(thread.is_alive())
        release.set()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        iface.close.assert_called_once()
        self.assertIsInstance(errors[0], TimeoutError)


class MeshCoreCaptureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.samples = []
        self.worker = capture.MeshCoreWorker(capture.MeshStore(), port="COM5", sample_sink=self.samples.append)
        self.mc = types.SimpleNamespace(self_info={"public_key": PUBLIC, "adv_type": 1, "name": "Companion"})

    async def test_advertised_position_survives_unsupported_health_queries(self):
        self.mc.self_info.update(adv_lat=40.0, adv_lon=-74.0)
        self.mc.commands = types.SimpleNamespace(set_coords=AsyncMock(return_value=event({})))
        await self.worker._sample_radio(self.mc, EVENT)
        self.assertEqual((self.worker.radio["node"]["lat"], self.worker.radio["node"]["lon"]), (40.0, -74.0))
        self.worker.request_position(41.0, -73.0)
        await self.worker._apply_position(self.mc, EVENT)
        self.mc.commands.set_coords.assert_awaited_once_with(41.0, -73.0)
        self.assertEqual((self.worker.radio["node"]["lat"], self.worker.radio["node"]["lon"]), (41.0, -73.0))
        self.assertEqual(self.worker.position_status, "set")
        self.assertEqual(self.samples, [])
        self.assertNotIn("observed_at", self.worker.radio)

    async def test_sequential_queries_keep_separate_acquisition_timestamps(self):
        commands = types.SimpleNamespace(
            send_device_query=AsyncMock(return_value=event({"ver": "1.11.0", "fw ver": 10})),
            get_stats_core=AsyncMock(return_value=event({"uptime_secs": 100, "battery_mv": 4000, "queue_len": 0, "errors": 0})),
            get_stats_radio=AsyncMock(return_value=event({"last_rssi": -90, "last_snr": 1.25, "tx_air_secs": 5})),
            get_stats_packets=AsyncMock(return_value=event({"sent": 50, "recv": 60, "flood_tx": 12, "direct_rx": 40})))
        self.mc.commands = commands
        with patch.object(capture.time, "time", side_effect=[NOW, NOW + 1, NOW + 5, NOW + 9]):
            await self.worker._sample_radio(self.mc, EVENT)
        core, radio, packets = self.samples
        self.assertEqual(core["observed_at"], NOW + 1)
        self.assertEqual(core["uptime_at"], NOW + 1)
        self.assertEqual(core["health"]["queue_length"], 0)
        self.assertEqual(core["voltage"], 4.0)
        self.assertEqual(radio["health_at"], NOW + 5)
        self.assertIsNone(radio["uptime_seconds"])
        self.assertEqual(packets["counters_at"], NOW + 9)
        self.assertIsNone(packets["uptime_seconds"])
        self.assertEqual(packets["counters"]["num_packets_tx"], 50)
        self.assertNotIn("num_tx_relay", packets["counters"])
        self.assertNotIn("relay_tx_attempts", packets["counters"])
        self.assertTrue(all(s["profile"] == "meshcore-companion-v1" for s in self.samples))
        self.assertTrue(all(s["firmware_version"] == "1.11.0" for s in self.samples))

    async def test_absent_methods_fall_back_to_battery_and_never_protocol_firmware(self):
        self.mc.commands = types.SimpleNamespace(send_device_query=AsyncMock(return_value=event({"fw ver": 10})),
                                                get_bat=AsyncMock(return_value=event({"level": 0})))
        await self.worker._sample_radio(self.mc, EVENT)
        self.assertEqual(self.samples[0]["firmware_version"], "unknown")
        self.assertEqual(self.samples[0]["voltage"], 0.0)
        self.assertIsNone(self.samples[0]["uptime_seconds"])
        self.assertIn("get_stats_core", self.worker._unsupported)
        self.assertIn("get_stats_radio", self.worker._unsupported)
        self.assertIn("get_stats_packets", self.worker._unsupported)

    async def test_method_lookup_and_invocation_not_implemented_are_suppressed(self):
        class Commands:
            @property
            def get_stats_core(self):
                raise NotImplementedError("old library")
            get_stats_radio = AsyncMock(side_effect=NotImplementedError("unsupported"))
        self.mc.commands = Commands()
        await self.worker._sample_radio(self.mc, EVENT)
        await self.worker._sample_radio(self.mc, EVENT)
        self.assertEqual(self.samples, [])
        self.assertIn("get_stats_core", self.worker._unsupported)
        self.mc.commands.get_stats_radio.assert_awaited_once()

    async def test_explicit_unsupported_response_is_not_retried_in_session(self):
        command = AsyncMock(return_value=event({"error_code": 1}, "error"))
        self.mc.commands = types.SimpleNamespace(get_stats_core=command)
        await self.worker._sample_radio(self.mc, EVENT)
        await self.worker._sample_radio(self.mc, EVENT)
        command.assert_awaited_once()
        self.assertEqual(self.samples, [])

    async def test_timeout_does_not_disable_supported_query_or_skip_later_groups(self):
        async def stall():
            await asyncio.sleep(10)
        core = AsyncMock(side_effect=stall)
        self.mc.commands = types.SimpleNamespace(get_stats_core=core,
            get_stats_packets=AsyncMock(return_value=event({"sent": 0, "recv": 0})))
        with patch.object(capture, "_QUERY_TIMEOUT", 0.001):
            await self.worker._sample_radio(self.mc, EVENT)
        self.assertNotIn("get_stats_core", self.worker._unsupported)
        self.assertIn("timed out", self.worker.error)
        self.assertEqual(self.samples[-1]["counters"]["num_packets_tx"], 0)
        core.side_effect = None
        core.return_value = event({"uptime_secs": 5, "battery_mv": 3900})
        await self.worker._sample_radio(self.mc, EVENT)
        self.assertEqual(core.await_count, 2)
        self.assertEqual(self.samples[-2]["uptime_seconds"], 5)
        self.assertEqual(self.worker.error, "")

    async def test_full_public_key_required_and_returned_contact_is_not_a_repeater_measurement(self):
        self.mc.commands = types.SimpleNamespace(get_stats_core=AsyncMock(return_value=event({"uptime_secs": 10})))
        self.mc.self_info["public_key"] = PUBLIC[:16]
        await self.worker._sample_radio(self.mc, EVENT)
        self.assertEqual(self.samples, [])
        self.mc.commands.get_stats_core.assert_not_called()


class MeshCoreTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_serial_factory_preserves_existing_board_reset_guard(self):
        worker = capture.MeshCoreWorker(capture.MeshStore(), port="COM5", baud=230400)
        sdk = types.SimpleNamespace(create_serial=AsyncMock(return_value=object()))
        self.addCleanup(capture._NO_RESET_PORTS.discard, "COM5")
        for protected in (True, False):
            with self.subTest(protected=protected), patch.object(capture, "_needs_no_reset", return_value=protected):
                await worker._open_serial(sdk)
                sdk.create_serial.assert_awaited_with("COM5", 230400)
                self.assertEqual("COM5" in capture._NO_RESET_PORTS, protected)

    async def test_health_queue_failure_does_not_interrupt_meshcore_session(self):
        service = capture.MeshCaptureService()
        service._capture_sample(sample())
        worker = capture.MeshCoreWorker(service.store, port="COM5", sample_sink=service._capture_sample)
        mc = types.SimpleNamespace(self_info={"public_key": PUBLIC}, commands=types.SimpleNamespace(
            get_stats_core=AsyncMock(return_value=event({"uptime_secs": 42})),
            get_stats_packets=AsyncMock(return_value=event({"sent": 1, "recv": 2}))))
        with patch.object(capture, "_SAMPLE_CAP", 1):
            await worker._sample_radio(mc, EVENT)
        # Later queries and normal discovery continue despite health backpressure.
        mc.commands.get_stats_packets.assert_awaited_once()
        worker._capture_contact({"public_key": "cd" * 32, "adv_name": "Discovered", "type": 1})
        self.assertEqual(len(service.snapshot()), 1)
        self.assertEqual(service.pending_samples()[0]["capture_id"], "capture-1")
        self.assertIn("queue is full", service._sample_error)

    async def test_usb_and_ble_targeting_and_cleanup(self):
        for transport, endpoint in (("serial", "COM5"), ("ble", "11:22"), ("ble", "auto")):
            with self.subTest(transport=transport):
                worker = capture.MeshCoreWorker(capture.MeshStore(), port="COM5", transport=transport, ble_address=endpoint)
                worker.stop()  # Complete handshake/cleanup without entering the loop.
                mc = types.SimpleNamespace(is_connected=True, self_info={},
                    commands=types.SimpleNamespace(get_contacts=AsyncMock(return_value=event({}))),
                    subscribe=Mock(side_effect=["first", "second"]), unsubscribe=Mock(), disconnect=AsyncMock())
                factories = types.SimpleNamespace(create_serial=AsyncMock(return_value=mc), create_ble=AsyncMock(return_value=mc))
                module = types.ModuleType("meshcore")
                module.MeshCore, module.EventType = factories, EVENT
                with patch.dict(sys.modules, {"meshcore": module}), patch.object(capture, "_needs_no_reset", return_value=False):
                    await worker._session()
                if transport == "serial":
                    factories.create_serial.assert_awaited_once_with(endpoint, 115200)
                    factories.create_ble.assert_not_called()
                else:
                    factories.create_ble.assert_awaited_once_with(None if endpoint == "auto" else endpoint)
                    factories.create_serial.assert_not_called()
                self.assertEqual(mc.unsubscribe.call_count, 2)
                mc.disconnect.assert_awaited_once()
                self.assertFalse(worker.connected)

    async def test_failed_connection_closes_object(self):
        worker = capture.MeshCoreWorker(capture.MeshStore(), transport="ble", ble_address="11:22")
        mc = types.SimpleNamespace(is_connected=False, disconnect=AsyncMock())
        module = types.ModuleType("meshcore")
        module.MeshCore = types.SimpleNamespace(create_ble=AsyncMock(return_value=mc))
        module.EventType = EVENT
        with patch.dict(sys.modules, {"meshcore": module}):
            with self.assertRaisesRegex(RuntimeError, "could not connect"):
                await worker._session()
        mc.disconnect.assert_awaited_once()



class FakeWorker:
    instances = []
    events = []
    def __init__(self, *args, **kwargs):
        self.port = args[0] if args and isinstance(args[0], str) else kwargs.get("port", "")
        self.transport = kwargs["transport"]
        self.ble_address = kwargs["ble_address"]
        self.radio, self.connected, self.heard, self.position_status, self.error = None, False, 0, "", ""
        self.alive = False
        self.hang = False
        self.kwargs = kwargs
        self.instances.append(self)
    def start(self):
        self.events.append(("start", self))
        self.alive = True
    def stop(self):
        self.events.append(("stop", self))
        if not self.hang:
            self.alive = False
    def join(self, timeout=None):
        self.events.append(("join", self))
    def is_alive(self):
        return self.alive


class CaptureServiceTests(unittest.TestCase):
    def setUp(self):
        self.service = capture.MeshCaptureService()
        FakeWorker.instances, FakeWorker.events = [], []
        self.mt = patch.object(capture, "MeshtasticWorker", FakeWorker)
        self.mc = patch.object(capture, "MeshCoreWorker", FakeWorker)
        self.mt.start()
        self.mc.start()
        self.addCleanup(self.mt.stop)
        self.addCleanup(self.mc.stop)

    def reconcile(self, devices, **kwargs):
        self.service.reconcile(devices, hop_limit=7, advert_minutes=60, **kwargs)

    def test_immutable_peek_ack_and_stop_preserve_pending_measurements(self):
        original = sample()
        self.service._capture_sample(original)
        original["health"]["battery_level"] = 99
        first = self.service.pending_samples()
        first[0]["health"]["battery_level"] = 88
        self.service.stop()
        self.assertEqual(self.service.pending_samples()[0]["health"]["battery_level"], 0)
        self.service._capture_sample(sample("late"))
        self.assertEqual(len(self.service.pending_samples()), 1)
        self.service.acknowledge_samples(["capture-1"])
        self.assertEqual(self.service.pending_samples(), [])
        self.assertEqual(self.service._pending_bytes, 0)

    def test_fingerprint_at_capture_does_not_follow_later_key_changes(self):
        self.service.set_credential_fingerprint("hash-a")
        self.service._capture_sample(sample("a"))
        self.service.set_credential_fingerprint("hash-b")
        self.service._capture_sample(sample("b"))
        self.service.set_credential_fingerprint("")
        self.service._capture_sample(sample("c"))
        self.assertEqual([x["credential_fingerprint"] for x in self.service.pending_samples()], ["hash-a", "hash-b", ""])

    def test_full_queue_reports_error_without_evicting_or_replacing_pending(self):
        with patch.object(capture, "_SAMPLE_CAP", 1):
            self.service._capture_sample(sample())
            size = self.service._pending_bytes
            altered = sample()
            altered["health"] = {"battery_level": 100}
            self.service._capture_sample(altered)
            self.assertEqual(self.service._pending_bytes, size)
            with self.assertRaisesRegex(RuntimeError, "queue is full"):
                self.service._capture_sample(sample("second"))
            self.assertEqual(len(self.service.pending_samples()), 1)
            self.assertEqual(self.service.pending_samples()[0]["health"]["battery_level"], 0)
            self.service.acknowledge_samples(["capture-1"])
            self.assertEqual(self.service._sample_error, "")
            self.service._capture_sample(sample("second"))

    def test_byte_limit_is_independent_of_count(self):
        with patch.object(capture, "_SAMPLE_BYTE_CAP", 1):
            with self.assertRaisesRegex(RuntimeError, "queue is full"):
                self.service._capture_sample(sample())
        self.assertEqual(self.service.pending_samples(), [])

    def test_legacy_defaults_do_not_turn_meshtastic_serial_into_ble(self):
        self.reconcile([{"protocol": "meshtastic", "port": "COM4"}, {"protocol": "meshcore", "port": "COM5"}],
                       transport="ble", ble_address="AA:BB")
        mt, mc = FakeWorker.instances
        self.assertEqual(mt.transport, "serial")
        self.assertEqual(mc.transport, "ble")
        self.assertEqual(mc.ble_address, "AA:BB")

    def test_legacy_meshcore_auto_ble_still_starts_worker(self):
        self.reconcile([{"protocol": "meshcore", "port": "auto"}], transport="ble", ble_address="auto")
        worker = next(iter(self.service._workers.values()))
        self.assertEqual((worker.transport, worker.ble_address), ("ble", "auto"))
        self.assertFalse(self.service._config_errors)

    def test_duplicate_serial_or_ble_endpoint_cannot_be_claimed_twice(self):
        for transport, key, value in (("serial", "port", "COM4"), ("ble", "ble_address", "AA:BB")):
            with self.subTest(transport=transport):
                self.service = capture.MeshCaptureService()
                FakeWorker.instances = []
                self.reconcile([{"protocol": "meshtastic", "transport": transport, key: value},
                                {"protocol": "meshcore", "transport": transport, key: value.lower()}])
                self.assertEqual(len(FakeWorker.instances), 1)
                self.assertIn("already uses", self.service.status()[-1]["error"])

    def test_windows_native_serial_alias_cannot_claim_same_port_twice(self):
        self.reconcile([{"protocol": "meshtastic", "port": "COM4"},
                        {"protocol": "meshcore", "port": r"\\.\COM4"}])
        self.assertEqual(len(FakeWorker.instances), 1)
        self.assertIn("already uses", self.service.status()[-1]["error"])

    def test_settings_change_closes_before_reopening(self):
        dev = {"protocol": "meshcore", "port": "COM4", "baud": 115200}
        self.reconcile([dev])
        original = FakeWorker.instances[0]
        self.reconcile([{**dev, "baud": 9600}])
        replacement = FakeWorker.instances[-1]
        self.assertIsNot(original, replacement)
        self.assertLess(FakeWorker.events.index(("stop", original)), FakeWorker.events.index(("start", replacement)))
        self.assertLess(FakeWorker.events.index(("join", original)), FakeWorker.events.index(("start", replacement)))
        self.assertEqual(replacement.kwargs["baud"], 9600)

    def test_hung_close_reserves_endpoint_and_blocks_transport_switch_replacement(self):
        self.reconcile([{"protocol": "meshtastic", "port": "COM4"}])
        original = FakeWorker.instances[0]
        original.hang = True
        self.reconcile([{"protocol": "meshtastic", "transport": "ble", "ble_address": "AA:BB"}])
        self.assertEqual(len(FakeWorker.instances), 1)
        self.assertIn("previous connection", self.service.status()[-1]["error"])
        original.hang = False
        self.reconcile([{"protocol": "meshtastic", "transport": "ble", "ble_address": "AA:BB"}])
        self.assertEqual(len(FakeWorker.instances), 2)
        self.assertEqual(FakeWorker.instances[-1].transport, "ble")

    def test_explicit_multiple_endpoints_and_disabled_rows(self):
        self.reconcile([{"protocol": "meshtastic", "port": "COM4"},
                        {"protocol": "meshtastic", "transport": "ble", "ble_address": "AA:BB"},
                        {"protocol": "meshcore", "port": "COM5", "enabled": False}])
        self.assertEqual(len(FakeWorker.instances), 2)
        self.reconcile([])
        self.assertTrue(all(not w.is_alive() for w in FakeWorker.instances))
        self.assertEqual(self.service.status(), [])


if __name__ == "__main__":
    unittest.main()
