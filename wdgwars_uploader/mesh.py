"""Read MeshCore nodes from a Heltec (or any MeshCore companion) over USB serial
or BLE and build the WDGWars ``meshcore_nodes`` payload.

Upload format: the node list is wrapped in the HMAC envelope and POSTed to
``/api/upload``. The inner payload must carry all three slots
``{networks, aircraft, meshcore_nodes}``. See :mod:`wdgwars_uploader.uploader`.

Per-node contract (the server drops a row that fails any of these):

* ``node_id``   - 8-16 LOWERCASE hex, derived from ``public_key[:16]``.
* ``node_type`` - COMPANION / REPEATER / ROOM_SERVER / SENSOR.
* ``lat``/``lon`` - a real fix; 0/0 and null-island are dropped.
* ``type`` is the constant ``"MESHCORE"`` and ``network`` the constant
  ``"meshcore"``. The node's role goes in ``node_type``; swapping the two makes
  the server accept the upload but import nothing.
* ``public_key`` - full 64 hex; ``node_id`` must be its prefix.

The MeshCore contact list carries identity and last advertised position only, so
``rssi`` is sent as ``null``.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any

log = logging.getLogger("wdgwars.mesh")

# A MeshCore companion allows only ONE client at a time. The poller and the
# upload cycle both read the device from different threads, so every access is
# serialized through this lock.
_SERIAL_LOCK = threading.Lock()

NODE_TYPE_MAP = {1: "COMPANION", 2: "REPEATER", 3: "ROOM_SERVER", 4: "SENSOR"}
DEFAULT_NODE_TYPE = "REPEATER"


class MeshUnavailable(RuntimeError):
    """The meshcore library isn't installed or the device couldn't be reached."""


class MeshHang(MeshUnavailable):
    """The Meshtastic serial connect blew past its deadline and cannot be safely
    interrupted - a known ``meshtastic`` library deadlock where a failed
    ``SerialInterface.__init__`` calls ``close()``, which does a thread ``join()``
    that never returns. The worker thread is left holding the COM port, so the only
    clean recovery is a process restart, which reopens the port fresh.
    """


# SerialInterface() normally connects + loads the node DB in ~5-15s. Anything
# past this is the deadlock above, not a slow link.
_MESH_CONNECT_DEADLINE = 60.0


def _get(obj: Any, *names: str, default: Any = None) -> Any:
    """Read a field from a dict or an object, trying several spellings."""
    for n in names:
        if isinstance(obj, dict):
            if n in obj:
                return obj[n]
        else:
            if hasattr(obj, n):
                return getattr(obj, n)
    return default


def _norm_coord(v: Any) -> float | None:
    """MeshCore advertises lat/lon as int32 * 1e6. Most library versions already
    divide to degrees; a value still arriving as a big integer is scaled down."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if abs(f) > 180.0:  # still raw fixed-point
        f = f / 1e6
    return f


def _fmt_ts(unix_ts: Any) -> str:
    try:
        t = float(unix_ts)
        if t > 0:
            return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t))
    except (TypeError, ValueError):
        pass
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def _heard_ts(unix_ts: Any) -> float | None:
    """The radio's own last-heard stamp, or None when it has none."""
    try:
        t = float(unix_ts)
    except (TypeError, ValueError):
        return None
    return t if t > 0 else None


def _hav_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    import math
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def filter_outlier_positions(nodes: list[dict], max_km: float = 800.0) -> list[dict]:
    """Null out mesh positions that are implausibly far from the local cluster
    (a corrupt Position packet — e.g. lat OK but lon ~0 — puts a node in the
    Atlantic). The node stays in the list as 'heard' but loses its bad fix, so it
    won't skew the map or get uploaded with a garbage location. Keeps no-GPS
    nodes untouched."""
    import statistics
    pts = [(n["lat"], n["lon"]) for n in nodes
           if n.get("lat") is not None and n.get("lon") is not None]
    if len(pts) < 3:
        return nodes
    med_lat = statistics.median(p[0] for p in pts)
    med_lon = statistics.median(p[1] for p in pts)
    out: list[dict] = []
    for n in nodes:
        if n.get("lat") is None or n.get("lon") is None:
            out.append(n)
            continue
        if _hav_km(n["lat"], n["lon"], med_lat, med_lon) <= max_km:
            out.append(n)
        else:
            m = dict(n)
            m["lat"] = None
            m["lon"] = None
            m["uploadable"] = False
            out.append(m)
    return out


def contact_to_node(contact: Any) -> dict[str, Any] | None:
    """Convert one MeshCore contact into a display node dict, keeping it even
    without a position (marked ``uploadable=False``) so the feed shows
    everything heard. Returns None only if the contact has no usable
    identity (no valid public key). The upload path stages only uploadable ones."""
    pub = _get(contact, "public_key", "pubkey", "publicKey")
    if not pub or not isinstance(pub, str):
        return None
    pub = pub.strip().lower()
    if len(pub) < 16 or any(c not in "0123456789abcdef" for c in pub[:16]):
        return None

    lat = _norm_coord(_get(contact, "adv_lat", "lat", "latitude"))
    lon = _norm_coord(_get(contact, "adv_lon", "lon", "longitude"))
    if lat is not None and lon is not None and abs(lat) < 1e-6 and abs(lon) < 1e-6:
        lat = lon = None  # null island == no fix

    type_byte = _get(contact, "type", "node_type", "kind", default=None)
    node_type = NODE_TYPE_MAP.get(type_byte, DEFAULT_NODE_TYPE) if isinstance(type_byte, int) else DEFAULT_NODE_TYPE

    name = _get(contact, "adv_name", "name", "display_name", default="") or pub[:16]
    last = _get(contact, "last_advert", "last_seen", "last_heard", "lastmod")

    node = {
        "node_id": pub[:16],
        "node_type": node_type,
        "name": str(name)[:64],
        "lat": round(lat, 6) if lat is not None else None,
        "lon": round(lon, 6) if lon is not None else None,
        "rssi": None,
        "first_seen": _fmt_ts(last),
        "heard_ts": _heard_ts(last),
        "type": "MESHCORE",
        "network": "meshcore",
        "uploadable": lat is not None and lon is not None,
    }
    if len(pub) == 64:  # full key present -> include it (node_id is its prefix)
        node["public_key"] = pub
    return node


async def _read_contacts_ble_async(address: str, advert: bool = False) -> list[Any]:
    """Read contacts from a MeshCore BLE companion. ``address`` may be "" /
    "auto" (first MeshCore-looking device) or a name/MAC to match."""
    try:
        from meshcore import MeshCore  # type: ignore
    except Exception as e:  # pragma: no cover
        raise MeshUnavailable(f"meshcore library not available: {e}")
    if not hasattr(MeshCore, "create_ble"):
        raise MeshUnavailable("installed meshcore has no BLE support")

    mc = None
    try:
        addr = None if (not address or address.lower() == "auto") else address
        mc = await MeshCore.create_ble(addr)
        if mc is None or not getattr(mc, "is_connected", True):
            raise MeshUnavailable("could not connect to a MeshCore BLE companion")
        result = await mc.commands.get_contacts()
        payload = getattr(result, "payload", result)
        await _maybe_advert(mc, advert)
        if isinstance(payload, dict):
            return list(payload.values())
        if isinstance(payload, list):
            return payload
        return []
    finally:
        for closer in ("disconnect", "close"):
            fn = getattr(mc, closer, None)
            if fn:
                try:
                    res = fn()
                    if asyncio.iscoroutine(res):
                        await res
                except Exception:
                    pass
                break


async def _maybe_advert(mc, advert: bool) -> None:
    if not advert:
        return
    try:
        await mc.commands.send_advert(flood=True)
        log.info("sent flood advert")
    except Exception as e:
        log.debug("send_advert failed: %s", e)


async def _read_contacts_async(port: str, baud: int, advert: bool = False) -> list[Any]:
    try:
        from meshcore import MeshCore  # type: ignore
    except Exception as e:  # pragma: no cover
        raise MeshUnavailable(f"meshcore library not available: {e}")

    mc = None
    # Newer meshcore exposes the create_serial factory; fall back to the
    # SerialConnection + MeshCore(con) pattern for older versions.
    try:
        if hasattr(MeshCore, "create_serial"):
            mc = await MeshCore.create_serial(port, baud)
        else:
            from meshcore import SerialConnection  # type: ignore
            con = SerialConnection(port, baud)
            await con.connect()
            mc = MeshCore(con)
            await mc.connect()

        # create_serial returns None (and logs "not a serial companion") when the
        # device on the port doesn't speak the MeshCore USB-serial companion
        # protocol -- e.g. it's a BLE-only companion, a repeater/room-server
        # build, or Meshtastic.
        if mc is None or not getattr(mc, "is_connected", True):
            raise MeshUnavailable(
                f"{port} did not answer the MeshCore serial-companion protocol "
                "(is it running USB-serial Companion firmware?)")

        result = await mc.commands.get_contacts()
        payload = getattr(result, "payload", result)
        await _maybe_advert(mc, advert)
        if isinstance(payload, dict):
            return list(payload.values())
        if isinstance(payload, list):
            return payload
        return []
    finally:
        # Best-effort clean disconnect.
        for closer in ("disconnect", "close"):
            fn = getattr(mc, closer, None)
            if fn:
                try:
                    res = fn()
                    if asyncio.iscoroutine(res):
                        await res
                except Exception:
                    pass
                break


# --- Meshtastic --------------------------------------------------------------
# Meshtastic device role -> WDGWars node_type.
MESHTASTIC_ROLE_MAP = {
    "ROUTER": "REPEATER", "ROUTER_LATE": "REPEATER", "ROUTER_CLIENT": "REPEATER",
    "REPEATER": "REPEATER", "CLIENT": "COMPANION", "CLIENT_MUTE": "COMPANION",
    "CLIENT_HIDDEN": "COMPANION", "TRACKER": "COMPANION", "TAK": "COMPANION",
    "TAK_TRACKER": "COMPANION", "SENSOR": "SENSOR", "LOST_AND_FOUND": "SENSOR",
}


def meshtastic_node_to_display(nid: str, node: dict) -> dict[str, Any] | None:
    """Convert one Meshtastic node-DB entry to a display node dict, keeping every
    node *heard* even if it has no position or no NodeInfo yet. The ``uploadable``
    flag marks the ones that satisfy WDGWars' rules (valid node_id + a real fix);
    the upload path stages only those.

    Meshtastic sends Position and NodeInfo as separate packets, so a node is
    commonly heard (by number) minutes before its name or its GPS arrives. It is
    surfaced the whole time rather than hidden until both land.
    """
    user = node.get("user") or {}
    pos = node.get("position") or {}
    lat, lon = pos.get("latitude"), pos.get("longitude")
    try:
        lat = float(lat) if lat is not None else None
        lon = float(lon) if lon is not None else None
    except (TypeError, ValueError):
        lat = lon = None
    if lat is not None and lon is not None and abs(lat) < 1e-6 and abs(lon) < 1e-6:
        lat = lon = None  # null island == no fix

    node_id = (user.get("id") or nid or "").lstrip("!").lower()[:16]
    if not node_id:
        return None
    id_ok = len(node_id) >= 8 and all(c in "0123456789abcdef" for c in node_id[:8])
    role = (user.get("role") or "CLIENT")
    name = user.get("longName") or user.get("shortName") or node_id
    uploadable = bool(id_ok and lat is not None and lon is not None)
    out = {
        "node_id": node_id,
        "node_type": MESHTASTIC_ROLE_MAP.get(role, "COMPANION"),
        "name": str(name)[:64],
        "lat": round(lat, 6) if lat is not None else None,
        "lon": round(lon, 6) if lon is not None else None,
        "rssi": node.get("snr"),
        "first_seen": _fmt_ts(node.get("lastHeard")),
        "heard_ts": _heard_ts(node.get("lastHeard")),
        "type": "MESHCORE",       # envelope marker constant (server sorts by `network`)
        "network": "meshtastic",
        "uploadable": uploadable,
    }
    # Display-only telemetry (NOT part of the WDGWars upload schema — the state
    # layer whitelists upload fields, so these never leak into the payload).
    dm = node.get("deviceMetrics") or {}
    em = node.get("environmentMetrics") or {}
    tele = {
        "snr": node.get("snr"),
        "hops_away": node.get("hopsAway"),
        "short_name": user.get("shortName"),
        "hw_model": user.get("hwModel"),
        "role": role,
        "battery": dm.get("batteryLevel"),
        "voltage": dm.get("voltage"),
        "ch_util": dm.get("channelUtilization"),
        "air_util_tx": dm.get("airUtilTx"),
        "uptime_s": dm.get("uptimeSeconds"),
        "temp_c": em.get("temperature"),
        "humidity": em.get("relativeHumidity"),
        "pressure": em.get("barometricPressure"),
    }
    out["tele"] = {k: v for k, v in tele.items() if v is not None}
    return out


def _maybe_set_meshtastic_hops(iface, hop_limit: int) -> None:
    """One-time: raise the device's LoRa hop limit to ``hop_limit`` (max 7) if it
    is not already there. Writing config reboots the node, so it is only written
    when the value differs."""
    if not hop_limit or hop_limit < 1:
        return
    try:
        lora = iface.localNode.localConfig.lora
        if getattr(lora, "hop_limit", None) != hop_limit:
            lora.hop_limit = min(int(hop_limit), 7)
            iface.localNode.writeConfig("lora")
            log.info("set Meshtastic hop_limit=%d (device will reboot)", min(hop_limit, 7))
    except Exception as e:
        log.debug("could not set meshtastic hops: %s", e)


def _meshtastic_announce(iface) -> None:
    """Broadcast a NodeInfo request with ``want_response`` set - the Meshtastic
    analog of a MeshCore flood advert. Nearby nodes that receive it reply with their
    own NodeInfo, which populates the node DB faster than waiting for their periodic
    beacon. Best-effort; any failure is non-fatal.

    ``sendPosition`` is deliberately not used: it would broadcast a 0,0 position and
    mark this node at null island. An empty NodeInfo request carries no location yet
    still sets the want_response flag.
    """
    try:
        try:
            from meshtastic import portnums_pb2
            portnum = portnums_pb2.PortNum.NODEINFO_APP
        except Exception:
            portnum = 4  # NODEINFO_APP
        iface.sendData(b"", destinationId="^all", portNum=portnum, wantResponse=True)
        log.info("sent Meshtastic NodeInfo want_response nudge")
    except Exception as e:
        log.debug("meshtastic announce failed: %s", e)


def read_meshtastic(port: str, baud: int = 115200, hop_limit: int = 0,
                    timeout: float = 30.0, announce: bool = False) -> list[dict[str, Any]]:
    """Read the Meshtastic node DB over serial and return WDGWars node dicts.
    Optionally raise the device hop limit first, and (if ``announce``) fire a
    want_response nudge so nearby nodes reveal themselves sooner."""
    if not port:
        raise MeshUnavailable("no Meshtastic serial port")
    try:
        from meshtastic.serial_interface import SerialInterface
    except Exception as e:
        raise MeshUnavailable(f"meshtastic library not available: {e}")
    if not _SERIAL_LOCK.acquire(timeout=max(timeout, 30.0)):
        raise MeshUnavailable(f"Meshtastic {port} busy")

    # The meshtastic SerialInterface connect can deadlock un-interruptibly on a
    # wedged port (see MeshHang), so run it in a worker thread with a hard
    # deadline instead of calling it inline where it would freeze the poller.
    box: dict[str, Any] = {}

    def _worker() -> None:
        iface = None
        try:
            iface = SerialInterface(devPath=port)   # connects + loads the node DB
            if hop_limit:
                _maybe_set_meshtastic_hops(iface, hop_limit)
            if announce:
                _meshtastic_announce(iface)
            box["nodes"] = dict(getattr(iface, "nodes", None) or {})
        except Exception as e:  # noqa: BLE001 - reported back to the caller
            box["err"] = e
        finally:
            if iface is not None:
                try:
                    iface.close()
                except Exception:
                    pass
            box["done"] = True

    t = threading.Thread(target=_worker, name="wdgw-mesh-read", daemon=True)
    t.start()
    t.join(_MESH_CONNECT_DEADLINE)
    if not box.get("done"):
        # Deliberately do NOT release _SERIAL_LOCK: the daemon worker still owns
        # the port and will never return. Signal a hang so the app recovers the
        # port with a clean restart; the process teardown frees the handle.
        raise MeshHang(
            f"Meshtastic connect on {port} exceeded {_MESH_CONNECT_DEADLINE:.0f}s "
            "(serial wedged); restarting to recover the port")
    _SERIAL_LOCK.release()
    if "err" in box:
        raise MeshUnavailable(f"Meshtastic read failed on {port}: {box['err']}") from box["err"]
    nodes_raw = box.get("nodes") or {}
    out: list[dict[str, Any]] = []
    for nid, node in nodes_raw.items():
        n = meshtastic_node_to_display(nid, node)
        if n:
            out.append(n)
    up = sum(1 for n in out if n.get("uploadable"))
    log.info("Meshtastic %s: %d heard, %d uploadable", port, len(out), up)
    return out


def read_nodes(port: str, baud: int = 115200, timeout: float = 30.0,
               transport: str = "serial", ble_address: str = "auto",
               advert: bool = False) -> list[dict[str, Any]]:
    """Connect to the MeshCore device, read its contacts, and return WDGWars node
    dicts. If ``advert`` is set, also send a flood advert in the same session, which
    announces this node mesh-wide to aid discovery. Raises
    :class:`MeshUnavailable` if the library or device is missing.
    """
    if transport == "ble":
        coro = _read_contacts_ble_async(ble_address, advert)
        where = f"BLE {ble_address}"
    else:
        if not port:
            raise MeshUnavailable("no MeshCore serial port")
        coro = _read_contacts_async(port, baud, advert)
        where = port
    # Serialize device access; a MeshCore companion is single-client.
    if not _SERIAL_LOCK.acquire(timeout=max(timeout, 30.0)):
        raise MeshUnavailable(f"MeshCore {where} busy (another read in progress)")
    try:
        contacts = asyncio.run(asyncio.wait_for(coro, timeout))
    except MeshUnavailable:
        raise
    except asyncio.TimeoutError as e:
        raise MeshUnavailable(f"timed out talking to MeshCore on {where}") from e
    except Exception as e:
        raise MeshUnavailable(f"MeshCore read failed on {where}: {e}") from e
    finally:
        _SERIAL_LOCK.release()

    nodes: list[dict[str, Any]] = []
    for c in contacts:
        n = contact_to_node(c)
        if n:
            nodes.append(n)
    up = sum(1 for n in nodes if n.get("uploadable"))
    log.info("MeshCore %s: %d contacts, %d heard, %d uploadable", where, len(contacts), len(nodes), up)
    return nodes
