"""WiGLE.net upload client.

Auth is HTTP Basic with the account's API Name and API Token (wigle.net ->
Account -> API). Uploads are a multipart POST of a WigleWifi-1.6 CSV to
/api/v2/file/upload, file field "file".

/file/upload also takes anonymous uploads: with a wrong API Name or Token it
answers 200 and files the upload under no account. So the credentials are
checked against /api/v2/profile/user first, and nothing is sent unless WiGLE
says they belong to an account.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from . import __version__

log = logging.getLogger("wdgwars.wigle")

BASE = "https://api.wigle.net/api/v2"
UA = f"wardrive-uploader/{__version__} (+https://github.com/RocketGod-git/wardrive-uploader)"


def _session(name: str, token: str) -> requests.Session:
    s = requests.Session()
    s.auth = (name.strip(), token.strip())
    s.headers.update({"User-Agent": UA, "Accept": "application/json"})
    return s


def verify(name: str, token: str, timeout: float = 20.0) -> tuple[bool, str]:
    """(True, user name) when the key belongs to an account, else (False, why)."""
    if not name.strip() or not token.strip():
        return False, "enter your WiGLE API Name and Token"
    try:
        resp = _session(name, token).get(f"{BASE}/profile/user", timeout=timeout)
    except requests.RequestException as e:
        return False, f"could not reach WiGLE: {e}"
    if resp.status_code in (401, 403):
        return False, "WiGLE did not accept that API Name and Token"
    try:
        data: dict[str, Any] = resp.json()
    except ValueError:
        data = {}
    if resp.status_code != 200 or not data.get("success", True):
        return False, data.get("message") or f"WiGLE answered HTTP {resp.status_code}"
    for k in ("userName", "username", "user"):
        if _usable_name(data.get(k)):
            return True, _usable_name(data.get(k))
    return True, account_name(name, token) or "your account"


def upload(name: str, token: str, data: bytes, filename: str, timeout: float = 180.0) -> tuple[bool, str]:
    ok, who = verify(name, token)
    if not ok:
        return False, who
    files = {"file": (filename, data, "application/gzip" if filename.endswith(".gz") else "text/csv")}
    try:
        resp = _session(name, token).post(f"{BASE}/file/upload", files=files, timeout=timeout)
    except requests.RequestException as e:
        return False, f"WiGLE upload failed: {e}"
    try:
        body: dict[str, Any] = resp.json()
    except ValueError:
        body = {}
    if resp.status_code in (401, 403):
        return False, "WiGLE did not accept that API Name and Token"
    if not 200 <= resp.status_code < 300:
        return False, f"WiGLE upload failed (HTTP {resp.status_code})"
    if body.get("success") is False:
        return False, f"WiGLE did not take the file: {body.get('message') or 'no reason given'}"
    log.info("WiGLE upload %s: HTTP %s %s", filename, resp.status_code, str(body)[:300])
    return True, f"uploaded as {who}" + (f" ({_trans_id(body)})" if _trans_id(body) else "")


def _trans_id(body: dict[str, Any]) -> str:
    """The transaction id, wherever this answer put it ("results" has been both
    a list and an object holding a "transids" list)."""
    res = body.get("results")
    if isinstance(res, list):
        res = res[0] if res else None
    if isinstance(res, dict):
        if res.get("transId") or res.get("transid"):
            return str(res.get("transId") or res.get("transid"))
        tids = res.get("transids")
        if isinstance(tids, list) and tids:
            first = tids[0]
            return str(first.get("transId") or "") if isinstance(first, dict) else str(first)
    return str(body.get("transid") or body.get("transId") or "")


# --- account name, upload history, leaderboards (ports of Wardrive Go's WigleApi/WigleStats) ---
def _get(path: str, name: str = "", token: str = "", timeout: float = 30.0) -> tuple[int, dict[str, Any]]:
    s = _session(name, token) if name and token else requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json"})
    resp = s.get(f"{BASE}{path}", timeout=timeout)
    try:
        data = resp.json()
    except ValueError:
        data = {}
    return resp.status_code, data if isinstance(data, dict) else {}


def _usable_name(v: Any) -> str:
    s = str(v or "").strip()
    if not s or s.startswith(("AID", "{", "[")) or s == "anonymous" or len(s) > 64:
        return ""
    return s


def account_name(name: str, token: str) -> str:
    """The account's WiGLE user name. /profile/user does not document its fields,
    so the likely ones are tried before /stats/user's documented "user"."""
    try:
        code, data = _get("/profile/user", name, token)
        if code in (401, 403):
            return ""
        for k in ("userName", "username", "user"):
            if _usable_name(data.get(k)):
                return _usable_name(data.get(k))
        _, data = _get("/stats/user", name, token)
        return _usable_name(data.get("user"))
    except requests.RequestException:
        return ""


def _trans_date(t: Any) -> str:
    d = str(t or "").split("-")[0]
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 and d.isdigit() else ""


def _first(o: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if k in o and o[k] not in (None, "", "null"):
            return o[k]
    return ""


def _int(v: Any) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


_TRANS_KEYS = {"transid", "transID", "fileName", "filename", "name", "firstTime", "lastupdt", "trans_time", "time",
               "status", "state", "percentDone", "percent", "discoveredGps", "discovered", "btDiscoveredGps",
               "btDiscovered", "genDiscoveredGps", "genDiscovered", "totalGps", "total", "btTotalGps", "btTotal",
               "genTotalGps", "genTotal", "fileSize", "bytes", "size", "fileLines"}


def transactions(name: str, token: str, max_rows: int = 500) -> dict[str, Any]:
    """Upload history, newest first. WiGLE pages /file/transactions at about 100
    rows, so pages are walked until a short page, a page with nothing new, or ``max_rows``."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    depth = -1
    start = 0
    while start < max_rows:
        end = min(start + 100, max_rows)
        try:
            code, j = _get(f"/file/transactions?pagestart={start}&pageend={end}", name, token)
        except requests.RequestException as e:
            if out:
                break
            return {"ok": False, "error": f"could not reach WiGLE: {e}", "list": []}
        if code in (401, 403):
            return {"ok": False, "error": "WiGLE did not accept the API Name and Token", "list": []}
        if code != 200:
            if out:
                break
            return {"ok": False, "error": f"WiGLE answered HTTP {code}", "list": []}
        if start == 0 and "processingQueueDepth" in j:
            depth = _int(j.get("processingQueueDepth"))
        rows = j.get("results") or j.get("transactions") or []
        before = len(out)
        for o in rows:
            if not isinstance(o, dict):
                continue
            tid = str(_first(o, "transid", "transID"))
            if tid and tid in seen:
                continue
            seen.add(tid)
            status = str(_first(o, "status", "state"))
            pct = _int(_first(o, "percentDone", "percent")) if _first(o, "percentDone", "percent") != "" else -1
            out.append({
                "id": tid, "file": str(_first(o, "fileName", "filename", "name")) or tid,
                "date": str(_first(o, "firstTime", "lastupdt", "trans_time", "time")),
                "status": status, "percent": pct,
                "done": pct >= 100 or status.upper() == "D"
                        or any(w in status.lower() for w in ("done", "complete", "success")),
                "new_wifi": _int(_first(o, "discoveredGps", "discovered")),
                "new_bt": _int(_first(o, "btDiscoveredGps", "btDiscovered")),
                "new_cell": _int(_first(o, "genDiscoveredGps", "genDiscovered")),
                "total_wifi": _int(_first(o, "totalGps", "total")),
                "total_bt": _int(_first(o, "btTotalGps", "btTotal")),
                "total_cell": _int(_first(o, "genTotalGps", "genTotal")),
                "bytes": _int(_first(o, "fileSize", "bytes", "size")),
                "lines": _int(_first(o, "fileLines")),
                "extras": {k: v for k, v in o.items() if k not in _TRANS_KEYS
                           and isinstance(v, (str, int, float, bool)) and str(v) not in ("", "null")},
            })
        if len(out) == before or len(rows) < end - start:
            break
        start = end
    return {"ok": True, "list": out, "queue": depth}


def _user_row(o: dict[str, Any]) -> dict[str, Any]:
    return {
        "rank": _int(o.get("rank")), "name": str(o.get("userName") or ""),
        "wifi_gps": _int(o.get("discoveredWiFiGPS")), "wifi": _int(o.get("discoveredWiFi")),
        "wifi_pct": float(o.get("discoveredWiFiGPSPercent") or 0), "bt": _int(o.get("discoveredBtGPS")),
        "cell": _int(o.get("discoveredCellGPS")), "month": _int(o.get("eventMonthCount")),
        "prev_month": _int(o.get("eventPrevMonthCount")), "prev_rank": _int(o.get("prevRank")),
        "prev_month_rank": _int(o.get("prevMonthRank")), "locations": _int(o.get("totalWiFiLocations")),
        "first": _trans_date(o.get("first")), "last": _trans_date(o.get("last")), "self": bool(o.get("self")),
    }


def standings(sort: str = "discovered", start: int = 0, end: int = 100, name: str = "", token: str = "") -> dict[str, Any]:
    """Global user standings. WiGLE lists the "anonymous" bucket at #1 on every
    board; it is dropped and the rest renumbered 1..N."""
    try:
        code, j = _get(f"/stats/standings?sort={sort}&pagestart={start}&pageend={end}", name, token)
    except requests.RequestException as e:
        return {"ok": False, "error": f"could not reach WiGLE: {e}", "users": []}
    if code != 200:
        return {"ok": False, "error": f"WiGLE answered HTTP {code}", "users": []}
    rows = [_user_row(o) for o in (j.get("results") or []) if isinstance(o, dict)]
    real = [u for u in rows if u["name"].lower() != "anonymous"]
    shift = 0 if start == 0 else 1
    for i, u in enumerate(real):
        u["rank"] = start + i + 1 - shift
        for k in ("prev_rank", "prev_month_rank"):     # WiGLE's own numbers still count "anonymous"
            if u[k] > 1:
                u[k] -= 1
    return {"ok": True, "users": real, "total": max(0, _int(j.get("totalUsers")) - 1),
            "me": str(j.get("myUsername") or "")}


def race(sort: str, my_rank: int, name: str = "", token: str = "", span: int = 2) -> list[dict[str, Any]]:
    """The players just above and below ``my_rank`` (board numbering) on one
    board, for the "next target" panel."""
    if my_rank <= 0:
        return []
    start = max(1, my_rank - span)            # the board's row ``my_rank - span``; raw rows count "anonymous" first
    board = standings(sort, start, my_rank + span + 1, name, token)
    return [u for u in board.get("users", []) if abs(u["rank"] - my_rank) <= span]


def groups(name: str = "", token: str = "") -> dict[str, Any]:
    try:
        code, j = _get("/stats/group", name, token)
    except requests.RequestException as e:
        return {"ok": False, "error": f"could not reach WiGLE: {e}", "groups": []}
    if code != 200:
        return {"ok": False, "error": f"WiGLE answered HTTP {code}", "groups": []}
    out = [{"id": str(o.get("groupId") or ""), "name": str(o.get("groupName") or ""), "owner": str(o.get("owner") or ""),
            "discovered": _int(o.get("discovered")), "total": _int(o.get("total")), "members": _int(o.get("members"))}
           for o in (j.get("groups") or []) if isinstance(o, dict)]
    return {"ok": True, "groups": out}


def group_members(group_id: str, me: str = "", name: str = "", token: str = "") -> dict[str, Any]:
    """Current members of one team, ranked by all-time Wi-Fi with GPS. WiGLE also
    returns members who left (status "L"); they are skipped."""
    try:
        code, j = _get(f"/group/groupMembers?groupid={requests.utils.quote(group_id)}", name, token)
    except requests.RequestException as e:
        return {"ok": False, "error": f"could not reach WiGLE: {e}", "members": []}
    if code != 200:
        return {"ok": False, "error": f"WiGLE answered HTTP {code}", "members": []}
    rows = []
    for o in j.get("users") or []:
        if not isinstance(o, dict) or str(o.get("status") or "").upper() == "L":
            continue
        n = str(o.get("username") or "")
        rows.append({"name": n, "discovered": _int(o.get("discovered")), "total": _int(o.get("total")),
                     "month": _int(o.get("monthCount")), "prev_month": _int(o.get("prevMonthCount")),
                     "first": _trans_date(o.get("firstTransid")), "last": _trans_date(o.get("lastTransid")),
                     "self": bool(me) and n.lower() == me.lower()})
    rows.sort(key=lambda r: -r["discovered"])
    for i, r in enumerate(rows):
        r["rank"] = i + 1
    return {"ok": True, "group": str((j.get("group") or {}).get("groupName") or ""), "members": rows}


def user_stats(name: str, token: str) -> dict[str, Any]:
    """The account's own standing (/stats/user): all-time and monthly ranks with
    last period's, Wi-Fi/Bluetooth/cell counts and first/last upload. Ranks move
    up one to match the boards, which leave out "anonymous"."""
    try:
        code, j = _get("/stats/user", name, token)
    except requests.RequestException as e:
        return {"ok": False, "error": f"could not reach WiGLE: {e}"}
    if code in (401, 403):
        return {"ok": False, "error": "WiGLE did not accept the API Name and Token"}
    if code != 200 or not j.get("success", False):
        return {"ok": False, "error": str(j.get("message") or f"WiGLE answered HTTP {code}")}
    s = j.get("statistics") or {}

    def deanon(r: Any) -> int:
        r = _int(r)
        return r - 1 if r > 1 else r

    return {
        "ok": True, "name": str(j.get("user") or s.get("userName") or ""),
        "rank": deanon(j.get("rank") or s.get("rank")), "month_rank": deanon(j.get("monthRank") or s.get("monthRank")),
        "prev_rank": deanon(s.get("prevRank")), "prev_month_rank": deanon(s.get("prevMonthRank")),
        "wifi_gps": _int(s.get("discoveredWiFiGPS")), "wifi": _int(s.get("discoveredWiFi")),
        "wifi_pct": float(s.get("discoveredWiFiGPSPercent") or 0), "bt": _int(s.get("discoveredBtGPS")),
        "bt_all": _int(s.get("discoveredBt")), "cell": _int(s.get("discoveredCellGPS")),
        "cell_all": _int(s.get("discoveredCell")), "locations": _int(s.get("totalWiFiLocations")),
        "month": _int(s.get("eventMonthCount")), "prev_month": _int(s.get("eventPrevMonthCount")),
        "first": _trans_date(s.get("first")), "last": _trans_date(s.get("last")),
    }
