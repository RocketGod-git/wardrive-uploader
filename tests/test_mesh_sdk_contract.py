"""Exercise real supported SDK decoders, with no serial/BLE or network I/O.

Install the mesh SDK requirements to run these in addition to mocked tests.
"""
import importlib.util
import inspect
import json
import struct
import tempfile
import time
import types
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from wdgwars_uploader import mesh_capture as capture, wardrift

LOCAL = 0x61f882d4
PUBLIC = "ab" * 32


@unittest.skipUnless(importlib.util.find_spec("meshtastic"), "Meshtastic SDK is required")
class MeshtasticSDKTests(unittest.TestCase):
    def setUp(self):
        from meshtastic.mesh_interface import MeshInterface
        from meshtastic.protobuf import mesh_pb2
        self.iface = MeshInterface(noProto=True)
        self.iface.myInfo = mesh_pb2.MyNodeInfo(my_node_num=LOCAL)
        self.iface.metadata = mesh_pb2.DeviceMetadata(firmware_version="sdk-test")
        self.iface.nodes = {}
        self.iface.nodesByNum = {}
        self.samples = []
        self.worker = capture.MeshtasticWorker("unused", capture.MeshStore(), sample_sink=self.samples.append)
        self.worker._local_num = LOCAL
        self.at = int(time.time()) - 300

    def receive(self, telemetry):
        from meshtastic import mesh_interface
        from meshtastic.protobuf import mesh_pb2, portnums_pb2
        from pubsub import pub
        packet = mesh_pb2.MeshPacket(to=LOCAL, id=1)
        setattr(packet, "from", LOCAL)
        packet.decoded.portnum = portnums_pb2.TELEMETRY_APP
        packet.decoded.payload = telemetry.SerializeToString()
        def received(packet, interface):
            self.worker._on_receive(packet, interface, self.iface)
        pub.subscribe(received, "meshtastic.receive")
        try:
            with patch.object(mesh_interface.publishingThread, "queueWork", side_effect=lambda fn: fn()):
                self.iface._handlePacketFromRadio(packet)
        finally:
            pub.unsubscribe(received, "meshtastic.receive")

    def test_real_device_metrics_presence_and_measurement_age(self):
        from meshtastic.protobuf import telemetry_pb2
        tel = telemetry_pb2.Telemetry(time=self.at)
        tel.device_metrics.battery_level = 101
        tel.device_metrics.uptime_seconds = 123
        self.receive(tel)
        self.assertEqual(len(self.samples), 1)
        sample = self.samples[0]
        self.assertEqual(sample["observed_at"], self.at)
        self.assertNotIn("voltage", sample["health"])
        self.assertIsNone(sample["health"]["battery_level"])
        self.assertTrue(sample["health"]["external_power"])
        with tempfile.TemporaryDirectory() as directory:
            rep = wardrift.MeshReporter(Path(directory) / "state.json")
            fp, account = wardrift.credential_fingerprint("test-key"), str(uuid.uuid4())
            rep.stage(self.samples, fp)
            rep.bind(fp, account)
            rep.prepare([], account, time.time())
            wire = json.loads(rep.pending(account)[0]["payload"])["nodes"][0]["telemetry"]
        self.assertEqual(wire["observed_at"], wardrift._iso(self.at))
        self.assertEqual(wire["health"]["measured_at"], wire["observed_at"])
        self.assertIn("host_received_at", wire)

    def test_real_localstats_non_optional_zero_survives_sdk_json_omission(self):
        from meshtastic.protobuf import telemetry_pb2
        tel = telemetry_pb2.Telemetry(time=self.at)
        tel.local_stats.SetInParent()  # All supported scalar measurements are zero.
        self.receive(tel)
        sample = self.samples[0]
        self.assertEqual(sample["profile"], "meshtastic-localstats-v1")
        self.assertEqual(sample["uptime_seconds"], 0)
        self.assertEqual(sample["counters"]["num_tx_relay"], 0)
        self.assertEqual(sample["health"]["channel_utilization"], 0)
        self.assertEqual(sample["observed_at"], self.at)

    def test_real_ble_constructor_through_uploader_with_mocked_radio_io(self):
        from meshtastic.ble_interface import BLEInterface
        from meshtastic.mesh_interface import MeshInterface
        client = Mock()
        client.has_characteristic.return_value = False
        worker = capture.MeshtasticWorker("", capture.MeshStore(), transport="ble",
                                         ble_address="00:11:22:33:44:55")
        # Run the real installed SDK constructor; only transport I/O and its
        # config exchange are mocked. This checks old/new constructor signatures.
        with patch.object(BLEInterface, "connect", return_value=client) as connect, \
                patch.object(BLEInterface, "_receiveFromRadioImpl"), \
                patch.object(MeshInterface, "_startConfig"), \
                patch.object(MeshInterface, "_waitConnected"), \
                patch.object(MeshInterface, "waitForConfig"), \
                patch.object(MeshInterface, "close"):
            iface = worker._open_iface(30)
            try:
                self.assertIsInstance(iface, BLEInterface)
                connect.assert_called_once_with("00:11:22:33:44:55")
                client.start_notify.assert_called()
            finally:
                iface.close()
        client.disconnect.assert_called_once()
        client.close.assert_called_once()


@unittest.skipUnless(importlib.util.find_spec("meshcore"), "MeshCore SDK is required")
class MeshCoreSDKTests(unittest.IsolatedAsyncioTestCase):
    async def decode(self, data):
        from meshcore.reader import MessageReader
        events = []
        dispatcher = types.SimpleNamespace(dispatch=AsyncMock(side_effect=events.append))
        await MessageReader(dispatcher).handle_rx(bytearray(data))
        self.assertEqual(len(events), 1)
        return events[0]

    async def collect(self, extended=True):
        from meshcore import EventType
        core = await self.decode(struct.pack("<BBHIHB", 24, 0, 4050, 123, 0, 0))
        radio = await self.decode(struct.pack("<BBhbbII", 24, 1, -110, -90, -8, 12, 34))
        payload = struct.pack("<BB6I", 24, 2, 5, 9, 3, 6, 2, 3)
        if extended:
            payload += struct.pack("<I", 7)
        packets = await self.decode(payload)
        device = await self.decode(bytes([13, 3, 100, 8]) + struct.pack("<I", 123456)
                                   + b"2026-10-28\0\0" + b"test board".ljust(40, b"\0") + b"v1.12.0".ljust(20, b"\0"))
        self.assertEqual(core.type, EventType.STATS_CORE)
        self.assertEqual(radio.type, EventType.STATS_RADIO)
        self.assertEqual(packets.type, EventType.STATS_PACKETS)
        samples = []
        worker = capture.MeshCoreWorker(capture.MeshStore(), sample_sink=samples.append)
        mc = types.SimpleNamespace(self_info={"public_key": PUBLIC, "name": "test companion", "adv_type": 1},
            commands=types.SimpleNamespace(send_device_query=AsyncMock(return_value=device),
                get_stats_core=AsyncMock(return_value=core), get_stats_radio=AsyncMock(return_value=radio),
                get_stats_packets=AsyncMock(return_value=packets)))
        await worker._sample_radio(mc, EventType)
        return samples

    async def test_real_stats_decoder_units_and_good_plus_bad_receive_total(self):
        core, radio, packets = await self.collect()
        self.assertEqual(core["firmware_version"], "v1.12.0")
        self.assertEqual(core["voltage"], 4.05)
        self.assertEqual(core["uptime_seconds"], 123)
        self.assertEqual(core["health"]["error_flags"], 0)
        self.assertEqual(radio["health"]["snr_db"], -2.0)
        self.assertEqual(radio["health"]["tx_airtime_seconds"], 12)
        self.assertEqual(packets["counters"]["num_packets_rx"], 12)
        self.assertEqual(packets["counters"]["num_packets_rx_bad"], 7)
        normalized = wardrift._sample(packets, str(uuid.uuid4()), {}, time.time())["wire"]["telemetry"]
        self.assertEqual(normalized["counters"]["num_packets_rx_bad"], 7)
        self.assertIsNone(normalized["counters"]["num_tx_relay"])
        self.assertIsNone(normalized["uptime_seconds"])

    async def test_real_legacy_packet_frame_keeps_unknown_errors(self):
        packets = (await self.collect(extended=False))[-1]
        self.assertEqual(packets["counters"]["num_packets_rx"], 5)
        self.assertIsNone(packets["counters"]["num_packets_rx_bad"])

    async def test_real_error_frame_suppresses_unsupported_stats(self):
        from meshcore import EventType
        unsupported = await self.decode(bytes([1, 1]))
        samples = []
        worker = capture.MeshCoreWorker(capture.MeshStore(), sample_sink=samples.append)
        fn = AsyncMock(return_value=unsupported)
        mc = types.SimpleNamespace(self_info={"public_key": PUBLIC}, commands=types.SimpleNamespace(get_stats_core=fn))
        await worker._sample_radio(mc, EventType)
        await worker._sample_radio(mc, EventType)
        self.assertEqual(samples, [])
        fn.assert_awaited_once()

    async def test_sdk_serial_and_ble_factory_signatures(self):
        from meshcore import MeshCore
        inspect.signature(MeshCore.create_serial).bind("unused", 115200)
        inspect.signature(MeshCore.create_ble).bind("00:11:22:33:44:55")


if __name__ == "__main__":
    unittest.main()
