"""Client for the OSRS Wiki real-time prices API.

Usage policy (https://oldschool.runescape.wiki/w/RuneScape:Real-time_Prices):
  - descriptive User-Agent required; generic agents are blocked
  - use bulk endpoints; never loop per-item over /latest
  - no hard rate limit, but be polite
"""

import requests

BASE_URL = "https://prices.runescape.wiki/api/v1/osrs"
USER_AGENT = "flipping-engine-prototype - tommylu3h@gmail.com"

_session = requests.Session()
_session.headers["User-Agent"] = USER_AGENT


def _get(path: str, params: dict | None = None) -> dict:
    resp = _session.get(f"{BASE_URL}{path}", params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_mapping() -> list[dict]:
    """Item metadata: id, name, members, limit (GE buy limit), value, etc."""
    return _get("/mapping")


def fetch_latest() -> dict[str, dict]:
    """Instant high/low price + timestamps for every item. Keyed by item id (str)."""
    return _get("/latest")["data"]


def fetch_5m(timestamp: int | None = None) -> tuple[dict[str, dict], int]:
    """Latest complete 5-minute bucket: avgHighPrice/highPriceVolume/avgLowPrice/lowPriceVolume."""
    body = _get("/5m", {"timestamp": timestamp} if timestamp else None)
    return body["data"], body["timestamp"]


def fetch_1h(timestamp: int | None = None) -> tuple[dict[str, dict], int]:
    """Latest complete 1-hour bucket, same shape as /5m."""
    body = _get("/1h", {"timestamp": timestamp} if timestamp else None)
    return body["data"], body["timestamp"]


def fetch_timeseries(item_id: int, timestep: str = "5m") -> list[dict]:
    """Up to 365 historical buckets for one item. timestep: 5m|1h|6h|24h.

    Only endpoint where a per-item call is appropriate; use lazily.
    """
    return _get("/timeseries", {"id": item_id, "timestep": timestep})["data"]
