"""Biscuit wardriving devices (Crumb, Pro, Ultra) over Bluetooth, the way Wardrive Go
links them.

One service, newline-framed text lines `TYPE:SUB:PAYLOAD`. On link the PC sends the
time, then `CMD:wardriveall:`, and takes the Biscuit's own raw sightings: `DATA:AP`
and `DATA:BT` only. Everything it interprets or relays (Flock verdicts, other
nodes' sightings, stats) is ignored. The Status JSON is read every 15 s for the
battery. Never paired. Every Biscuit heard links by itself and links again when it
comes back; it follows the WardriveESP switch, like on the phone.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from datetime import datetime
from typing import Any

log = logging.getLogger("wdgwars.biscuit")

SERVICE = "4fafc201-1fb5-459e-8fcc-c5c9c331914b"
CMD = "beb5483e-36e1-4688-b7f5-ea07361b26a8"
RSP = "beb5483e-36e1-4688-b7f5-ea07361b26a9"
STATUS = "beb5483e-36e1-4688-b7f5-ea07361b26aa"
DIS_MODEL = "00002a24-0000-1000-8000-00805f9b34fb"
DIS_SERIAL = "00002a25-0000-1000-8000-00805f9b34fb"
DIS_FW = "00002a26-0000-1000-8000-00805f9b34fb"
DIS_SW = "00002a28-0000-1000-8000-00805f9b34fb"

CONNECT_S = 15.0
STATUS_S = 15.0
SILENT_S = 45.0
IDLE_RESTART_S = 30.0
RETRY_S = 5.0

MAC = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")
CAPS = {
    "OPEN": "[ESS]", "OWE": "[RSN-OWE-CCMP][ESS]", "WEP": "[WEP][ESS]",
    "WPA_PSK": "[WPA-PSK-CCMP][ESS]", "WPA_ENTERPRISE": "[WPA-EAP-CCMP][ESS]",
    "WPA2_PSK": "[WPA2-PSK-CCMP][ESS]", "WPA_WPA2_PSK": "[WPA-PSK-CCMP][WPA2-PSK-CCMP][ESS]",
    "WPA2_ENTERPRISE": "[WPA2-EAP-CCMP][ESS]", "WPA3_PSK": "[WPA3-SAE-CCMP][ESS]",
    "WPA3_ENTERPRISE": "[WPA3-EAP-CCMP][ESS]", "WPA2_WPA3_PSK": "[WPA2-PSK-CCMP][WPA3-SAE-CCMP][ESS]",
    "WPA2_WPA3_ENTERPRISE": "[WPA2-EAP-CCMP][WPA3-EAP-CCMP][ESS]", "WAPI_PSK": "[WAPI-PSK][ESS]",
}


def is_biscuit(name: str | None, uuids: list[str] | None) -> bool:
    return SERVICE in [u.lower() for u in (uuids or [])] or name == "Biscuit" or bool(name and name.startswith("Biscuit "))


def rssi_of(s: str) -> int | None:
    try:
        v = int(s.strip())
    except ValueError:
        return None
    return v if -127 <= v <= -1 else None


def channel_of(s: str) -> int | None:
    try:
        v = int(s.strip())
    except ValueError:
        return None
    return v if 1 <= v <= 14 or 32 <= v <= 177 else None


def wigle_caps(token: str) -> str | None:
    return CAPS.get(token.strip().strip("[]").upper())


def parse_ap(payload: str) -> tuple[str, str, int, int, str | None] | None:
    """`SSID,BSSID,Channel,RSSI,Security`, taken from the right so an SSID never
    shifts the fields."""
    p = payload.split(",")
    if len(p) < 5:
        return None
    i = len(p) - 5
    bssid = p[i + 1].strip()
    ch, rssi = channel_of(p[i + 2]), rssi_of(p[i + 3])
    if not MAC.match(bssid) or ch is None or rssi is None:
        return None
    return ",".join(p[:i + 1]), bssid.lower(), ch, rssi, wigle_caps(p[i + 4])


def parse_bt(payload: str) -> tuple[str, str, int] | None:
    """`Name,MAC,RSSI,Type[,Payload][,SF=XX]`: name, MAC and RSSI only."""
    p = payload.split(",")
    for i in range(1, len(p) - 1):
        mac = p[i].strip()
        if not MAC.match(mac):
            continue
        rssi = rssi_of(p[i + 1])
        if rssi is None:
            continue
        return ",".join(p[:i]), mac.upper(), rssi
    return None


def epoch_time_fw(fw: str | None) -> bool:
    """Firmware 1.6.25 and newer takes the time as epoch seconds."""
    try:
        n = [int(re.match(r"\d*", x).group() or 0) for x in (fw or "").strip().lstrip("v").split(".")]
    except (ValueError, AttributeError):
        return False
    a, b, c = (n + [0, 0, 0])[:3]
    return a > 1 or (a == 1 and (b > 6 or (b == 6 and c >= 25)))


def time_sync(epoch: bool) -> str:
    if epoch:
        return f"TIME:SYNC:{int(time.time())}"
    return "TIME:SYNC:" + datetime.now().astimezone().replace(microsecond=0).isoformat()


class Biscuits:
    """Every Biscuit in range, each on its own link. Runs its own event loop thread."""

    def __init__(self, bridge) -> None:
        self.bridge = bridge
        self.loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self.tasks: dict[str, asyncio.Task] = {}
        self.retry_at: dict[str, float] = {}
        self.devices: dict[str, Any] = {}        # address -> latest BLEDevice
        self.stopping = False

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.stopping = False
        ready = threading.Event()

        def run() -> None:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            ready.set()
            self.loop.run_forever()
        self._thread = threading.Thread(target=run, name="biscuit", daemon=True)
        self._thread.start()
        ready.wait(2)

    def stop(self) -> None:
        self.stopping = True
        loop = self.loop
        if loop is None:
            return

        async def shut() -> None:
            tasks = list(self.tasks.values())
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            asyncio.run_coroutine_threadsafe(shut(), loop).result(6)
        except Exception:  # noqa: BLE001
            pass
        loop.call_soon_threadsafe(loop.stop)
        self.loop = None

    def wanted(self, address: str) -> bool:
        return self.bridge.enabled and not self.stopping and f"bisc:{address}" not in self.bridge.off_other

    def on_advert(self, device, adv) -> None:
        """A Biscuit advert from the PC's Bluetooth scan (any thread)."""
        loop = self.loop
        if loop is None:
            return
        loop.call_soon_threadsafe(self._advert, device, adv)

    def _advert(self, device, adv) -> None:
        a = device.address
        self.devices[a] = device
        name = adv.local_name or device.name or "Biscuit"
        if a not in self.tasks:
            if not self.wanted(a):
                if self.bridge.enabled and not self.stopping:      # switched off on its own: its tile stays
                    self.bridge.set_other(f"bisc:{a}", name=name, kind="Biscuit", transport="Bluetooth",
                                          state="off", detail="")
                return
            if time.monotonic() < self.retry_at.get(a, 0):
                return
            self.tasks[a] = asyncio.ensure_future(self._session(a, name))

    async def _session(self, address: str, name: str) -> None:
        oid = f"bisc:{address}"
        from bleak import BleakClient
        info: dict[str, Any] = {"name": name, "kind": "Biscuit", "transport": "Bluetooth"}
        lines: asyncio.Queue[str] = asyncio.Queue()
        buf = bytearray()
        state = {"address": address, "last_rx": time.monotonic(), "last_data": time.monotonic(), "last_start": 0.0,
                 "restart_after_stop": False, "epoch": False, "time_sent": 0.0, "scanning": False}
        heard_ap: dict[str, float] = {}
        heard_bt: dict[str, float] = {}

        def show(st: str, detail: str = "", **kw: Any) -> None:
            info.update(kw)
            self.bridge.set_other(oid, state=st, detail=detail, **info)

        def on_notify(_c, data: bytearray) -> None:
            state["last_rx"] = time.monotonic()
            buf.extend(data)
            while True:
                nl = buf.find(b"\n")
                if nl < 0:
                    if len(buf) > 8192:
                        buf.clear()
                    return
                ln = buf[:nl].decode("utf-8", "replace").rstrip("\r")
                del buf[:nl + 1]
                if ln:
                    lines.put_nowait(ln)
        client = None
        live = False
        try:
            show("linking", "connecting…")
            device = self.devices.get(address, address)
            client = BleakClient(device, timeout=CONNECT_S)
            await client.connect()

            async def read_str(u: str) -> str | None:
                try:
                    return bytes(await client.read_gatt_char(u)).decode("utf-8", "replace").strip("\x00 ").strip() or None
                except Exception:  # noqa: BLE001 - not on this model
                    return None
            if client.services.get_service(SERVICE) is None:
                show("failed", "not a Biscuit")
                self.retry_at[address] = time.monotonic() + 300
                return
            model, fw = await read_str(DIS_MODEL), await read_str(DIS_FW)
            sw, serial = await read_str(DIS_SW), await read_str(DIS_SERIAL)
            if model:
                info["name"] = model
            tag = (serial or address.replace(":", ""))[-4:]
            info.update(fw=fw, scanner_fw=sw, serial=serial, tag=tag)
            await client.start_notify(RSP, on_notify)

            async def send(line: str) -> None:
                if line == "CMD:wardriveall:":
                    state["last_start"] = time.monotonic()
                    state["restart_after_stop"] = False
                await client.write_gatt_char(CMD, (line.rstrip("\n") + "\n").encode(), response=True)
            live = True
            log.info("Biscuit: linked %s (%s, fw %s / %s)", info["name"], address, fw or "?", sw or "?")
            self.bridge.on_event(f"{info['name']} linked over Bluetooth")
            show("live", "starting")
            state["epoch"] = epoch_time_fw(fw)
            await send(time_sync(state["epoch"]))
            state["time_sent"] = time.monotonic()
            await send("CMD:wardriveall:")
            next_status = time.monotonic() + STATUS_S
            next_show = 0.0
            while client.is_connected and self.wanted(address):
                try:
                    ln = await asyncio.wait_for(lines.get(), 0.25)
                except asyncio.TimeoutError:
                    ln = None
                if ln:
                    await self._handle(ln, send, state, heard_ap, heard_bt, info["name"])
                now = time.monotonic()
                if now >= next_status:
                    next_status = now + STATUS_S
                    try:
                        raw = bytes(await client.read_gatt_char(STATUS))
                        state["last_rx"] = now
                        o = json.loads(raw.decode("utf-8", "replace"))
                        code = o.get("status", -1)
                        c5 = o.get("c5State")
                        busy = code == 2 or c5 == 2
                        bat = o.get("battery")
                        if isinstance(bat, int) and 0 <= bat <= 100:
                            info["battery"] = bat
                        t = o.get("temperatureC")
                        if isinstance(t, (int, float)) and -60 < t < 150:
                            info["temp"] = round(t)
                        if "c5Connected" in o:
                            info["scanner_up"] = bool(o["c5Connected"])
                        if not busy and now - state["last_start"] > IDLE_RESTART_S and now - state["last_data"] > IDLE_RESTART_S:
                            log.info("Biscuit: %s idle (status %s, c5State %s), restarting wardrive", address, code, c5)
                            await send("CMD:wardriveall:")
                    except Exception:  # noqa: BLE001 - one bad read never ends the link
                        pass
                if state["restart_after_stop"] and now - state["last_start"] > 3:
                    await send("CMD:wardriveall:")
                if now - state["last_rx"] > SILENT_S:
                    log.info("Biscuit: %s silent %d s, relinking", address, SILENT_S)
                    break
                if now >= next_show:
                    next_show = now + 2
                    show("live", "scanning" if state["scanning"] else "starting",
                         aps=sum(1 for t in heard_ap.values() if now - t < 30),
                         bt=sum(1 for t in heard_bt.values() if now - t < 30))
            if client.is_connected:
                try:
                    await client.write_gatt_char(CMD, b"CMD:stopscan:\n", response=True)
                except Exception:  # noqa: BLE001
                    pass
        except asyncio.CancelledError:
            if client is not None and client.is_connected:
                try:
                    await client.write_gatt_char(CMD, b"CMD:stopscan:\n", response=True)
                except Exception:  # noqa: BLE001
                    pass
            raise
        except Exception as e:  # noqa: BLE001
            log.info("Biscuit: %s link error: %s", address, e)
        finally:
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:  # noqa: BLE001
                    pass
            self.tasks.pop(address, None)
            self.retry_at[address] = time.monotonic() + (3 if live else RETRY_S)
            if self.wanted(address):
                show("linking" if live else "failed", "reconnecting…" if live else "couldn't connect, retrying")
            else:
                self.bridge.drop_other(oid)

    async def _handle(self, line: str, send, state: dict, heard_ap: dict, heard_bt: dict, name: str) -> None:
        c1 = line.find(":")
        c2 = line.find(":", c1 + 1) if c1 >= 0 else -1
        if c2 < 0:
            return
        typ, sub, payload = line[:c1], line[c1 + 1:c2], line[c2 + 1:]
        now = time.monotonic()
        if typ == "DATA" and sub == "AP":
            ap = parse_ap(payload)
            if ap:
                ssid, bssid, ch, rssi, caps = ap
                state["last_data"] = now
                state["scanning"] = True
                heard_ap[bssid] = now
                self.bridge.feed_ap(bssid, ssid, rssi, ch, caps, name, f"bisc:{state['address']}")
        elif typ == "DATA" and sub == "BT":
            bt = parse_bt(payload)
            if bt:
                bname, mac, rssi = bt
                state["last_data"] = now
                state["scanning"] = True
                heard_bt[mac] = now
                self.bridge.feed_bt(mac, bname, rssi, name, f"bisc:{state['address']}")
        elif typ == "TIME" and sub == "REQUEST":
            if state["time_sent"] and not state["epoch"] and now - state["time_sent"] >= 3:
                state["epoch"] = True
                log.info("Biscuit: %s asked for the time again, answering in epoch seconds", name)
            await send(time_sync(state["epoch"]))
            state["time_sent"] = now
        elif typ == "RSP":
            if sub.startswith("wardrive") and payload.startswith("OK"):
                state["scanning"] = True
            if sub.startswith("wardrive") and payload.startswith("ERROR"):
                log.info("Biscuit: %s wardriveall refused (%s), stopping the current op", name, payload)
                await send("CMD:stopscan:")
                state["restart_after_stop"] = True
        elif typ == "STATUS":
            if sub == "2" and "wardrive" in payload.lower():
                state["scanning"] = True
            if sub == "1" and state["restart_after_stop"]:
                await send("CMD:wardriveall:")
            elif sub == "1":
                state["scanning"] = False
        elif typ == "ERROR":
            log.info("Biscuit: %s %s", name, line)
