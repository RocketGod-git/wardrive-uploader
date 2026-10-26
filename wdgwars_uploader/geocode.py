"""Address lookup for the radio position picker, using OpenStreetMap Nominatim.

Called only when the user presses Find, so it stays well inside Nominatim's
usage policy (one request at a time, identified by a real User-Agent).
"""

from __future__ import annotations

from typing import Any

import requests

from . import __version__

URL = "https://nominatim.openstreetmap.org/search"
UA = f"wdgwars-uploader/{__version__} (+https://github.com/RocketGod-git/wdgwars-uploader)"


def search(query: str, limit: int = 5) -> list[dict[str, Any]]:
    """Places matching ``query`` as ``{name, lat, lon}``, best match first."""
    q = (query or "").strip()
    if not q:
        return []
    resp = requests.get(URL, params={"q": q, "format": "jsonv2", "limit": limit},
                        headers={"User-Agent": UA}, timeout=15)
    resp.raise_for_status()
    out: list[dict[str, Any]] = []
    for item in resp.json() or []:
        try:
            out.append({"name": item.get("display_name") or "",
                        "lat": float(item["lat"]), "lon": float(item["lon"])})
        except (KeyError, TypeError, ValueError):
            continue
    return out
