"""Timeseries-based stability checks.

The v1 ranker's biggest failure mode is suggesting items whose spread only
exists because the price is moving: a crash makes the stale high look like
profit; a spike makes the low look cheap. We guard by comparing the candidate's
entry/exit prices against the item's recent price distribution.

Timeseries fetches are per-item (the one endpoint where that's appropriate),
done lazily for top candidates only, and cached in SQLite.
"""

import json
import statistics
import time
from dataclasses import dataclass

from . import wiki_api

CACHE_TTL_S = 30 * 60
# 24h of 5m buckets
WINDOW_BUCKETS = 288
# Need at least this many priced buckets on each side to judge stability.
MIN_DATA_POINTS = 12
# Reject if entry/exit price is more than this many std devs against us.
Z_MAX = 2.0
# std floor as a fraction of price, so ultra-stable items don't produce
# huge z-scores from 1gp wiggles.
STD_FLOOR_FRACTION = 0.005

CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS ts_cache (
    item_id INTEGER NOT NULL,
    timestep TEXT NOT NULL,
    fetched_at INTEGER NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (item_id, timestep)
);
"""


@dataclass
class StabilityResult:
    ok: bool
    reason: str       # "ok" | "unchecked: ..." | rejection reason
    buy_z: float | None = None   # how far our buy price sits above recent lows
    sell_z: float | None = None  # how far our sell price sits below recent highs


def get_timeseries_cached(conn, item_id: int, timestep: str = "5m") -> list[dict] | None:
    conn.executescript(CACHE_SCHEMA)
    now = int(time.time())
    row = conn.execute(
        "SELECT fetched_at, payload FROM ts_cache WHERE item_id=? AND timestep=?",
        (item_id, timestep),
    ).fetchone()
    if row and row["fetched_at"] > now - CACHE_TTL_S:
        return json.loads(row["payload"])
    try:
        series = wiki_api.fetch_timeseries(item_id, timestep)
    except Exception:
        # network/API failure: serve stale cache if we have one
        return json.loads(row["payload"]) if row else None
    conn.execute(
        "INSERT INTO ts_cache VALUES (?,?,?,?) "
        "ON CONFLICT(item_id, timestep) DO UPDATE SET fetched_at=excluded.fetched_at, "
        "payload=excluded.payload",
        (item_id, timestep, now, json.dumps(series)),
    )
    conn.commit()
    return series


def price_stats(series: list[dict], window: int = WINDOW_BUCKETS):
    """(mean_low, std_low, mean_high, std_high) over the recent window,
    or None if there's not enough data to judge."""
    recent = series[-window:]
    lows = [b["avgLowPrice"] for b in recent if b.get("avgLowPrice")]
    highs = [b["avgHighPrice"] for b in recent if b.get("avgHighPrice")]
    if len(lows) < MIN_DATA_POINTS or len(highs) < MIN_DATA_POINTS:
        return None
    return (
        statistics.fmean(lows), statistics.stdev(lows),
        statistics.fmean(highs), statistics.stdev(highs),
    )


def assess(buy_price: int, sell_price: int, series: list[dict] | None) -> StabilityResult:
    if series is None:
        return StabilityResult(True, "unchecked: no timeseries")
    stats = price_stats(series)
    if stats is None:
        return StabilityResult(False, "too little trade history")
    mean_low, std_low, mean_high, std_high = stats
    std_low = max(std_low, mean_low * STD_FLOOR_FRACTION)
    std_high = max(std_high, mean_high * STD_FLOOR_FRACTION)

    # Buying well above where sellers have recently been = chasing a spike.
    buy_z = (buy_price - mean_low) / std_low
    # Selling well below where buyers have recently been = the high is stale
    # and the real market has moved down (crash in progress).
    sell_z = (sell_price - mean_high) / std_high

    if buy_z > Z_MAX:
        return StabilityResult(False, f"buy {buy_z:.1f}σ above recent lows (spike)", buy_z, sell_z)
    if sell_z < -Z_MAX:
        return StabilityResult(False, f"sell {abs(sell_z):.1f}σ below recent highs (crash)", buy_z, sell_z)
    return StabilityResult(True, "ok", buy_z, sell_z)
