"""Backtest harness: replay flips against historical 5m timeseries.

For each top-ranked item we step through its recent history (~30h of 5m
buckets from the wiki timeseries) and simulate the engine's flip at regular
entry points:

  buy  at avgLowPrice[t] + 1  — our bid fills in later buckets where
                                avgLowPrice <= our bid (insta-sellers were
                                accepting less, so they'd hit us first)
  sell at avgHighPrice[t] - 1 — fills in buckets where avgHighPrice >= our ask

We assume our offer captures `capture` fraction of that side's bucket volume.
The simulation answers: do the engine's spreads persist long enough to
round-trip, how long do fills actually take vs the engine's prediction, and
how sensitive is that to the capture assumption.

Realized margin equals predicted margin whenever a flip completes (we fill at
our own prices), so the interesting outputs are FILL RATE and ROUNDTRIP TIME.
"""

import json
import statistics
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from . import stability
from .engine import CAPTURE_FRACTION, MAX_MARGIN_RATIO, MAX_VOLUME_SHARE, rank_flips
from .ge_tax import flip_margin

BUCKET_MINUTES = 5
ENTRY_STEP_BUCKETS = 6          # attempt an entry every 30 minutes
DEFAULT_HORIZON_BUCKETS = 96    # give each flip up to 8 hours to round-trip
REPORT_PATH = Path(__file__).resolve().parent.parent / "logs" / "backtest_latest.json"


@dataclass
class SimResult:
    entry_ts: int
    buy_price: int
    sell_price: int
    quantity: int
    margin: int
    status: str                  # completed | buy_unfilled | sell_unfilled
    roundtrip_minutes: float | None
    profit: int


@dataclass
class ItemReport:
    item_id: int
    name: str
    simulations: int
    completed: int
    fill_rate: float
    median_roundtrip_min: float | None
    predicted_roundtrip_min: float | None
    total_profit: int


def _fill_forward(series, start_idx, price_key, vol_key, target_qty, capture,
                  horizon_end, price_ok) -> tuple[int, float] | None:
    """Walk buckets forward until our offer accumulates target_qty.
    Returns (end_idx, filled_minutes) or None if unfilled within horizon."""
    filled = 0.0
    for i in range(start_idx, min(horizon_end, len(series))):
        b = series[i]
        price = b.get(price_key)
        if price is not None and price_ok(price):
            filled += capture * (b.get(vol_key) or 0)
            if filled >= target_qty:
                return i, (i - start_idx + 1) * BUCKET_MINUTES
    return None


def simulate_entry(series, idx, buy_limit, cash, item_name, capture,
                   horizon=DEFAULT_HORIZON_BUCKETS) -> SimResult | None:
    """Simulate one flip entered at bucket idx. None = engine wouldn't enter here."""
    b = series[idx]
    low, high = b.get("avgLowPrice"), b.get("avgHighPrice")
    if not low or not high or not b.get("lowPriceVolume") or not b.get("highPriceVolume"):
        return None
    buy_price, sell_price = low + 1, high - 1
    if sell_price <= buy_price or buy_price > cash:
        return None
    margin = flip_margin(buy_price, sell_price, item_name)
    if margin < 1 or margin / buy_price > MAX_MARGIN_RATIO:
        return None

    # trailing-hour volume cap, mirroring the engine's MAX_VOLUME_SHARE
    trailing = series[max(0, idx - 12):idx] or [b]
    hourly_low_vol = sum(x.get("lowPriceVolume") or 0 for x in trailing)
    hourly_high_vol = sum(x.get("highPriceVolume") or 0 for x in trailing)
    quantity = min(buy_limit, cash // buy_price,
                   max(1, int(min(hourly_low_vol, hourly_high_vol) * MAX_VOLUME_SHARE)))
    if quantity < 1:
        return None

    horizon_end = idx + horizon
    buy_fill = _fill_forward(series, idx + 1, "avgLowPrice", "lowPriceVolume",
                             quantity, capture, horizon_end, lambda p: p <= buy_price)
    if buy_fill is None:
        return SimResult(b["timestamp"], buy_price, sell_price, quantity, margin,
                         "buy_unfilled", None, 0)
    buy_end_idx, buy_minutes = buy_fill

    sell_fill = _fill_forward(series, buy_end_idx + 1, "avgHighPrice", "highPriceVolume",
                              quantity, capture, horizon_end, lambda p: p >= sell_price)
    if sell_fill is None:
        return SimResult(b["timestamp"], buy_price, sell_price, quantity, margin,
                         "sell_unfilled", None, 0)
    _, sell_minutes = sell_fill

    return SimResult(b["timestamp"], buy_price, sell_price, quantity, margin,
                     "completed", buy_minutes + sell_minutes, margin * quantity)


def backtest_item(conn, item_id, name, buy_limit, cash, capture,
                  predicted_roundtrip_min=None) -> ItemReport | None:
    series = stability.get_timeseries_cached(conn, item_id)
    if not series or len(series) < 24:
        return None
    sims = []
    for idx in range(0, len(series) - DEFAULT_HORIZON_BUCKETS, ENTRY_STEP_BUCKETS):
        r = simulate_entry(series, idx, buy_limit, cash, name, capture)
        if r is not None:
            sims.append(r)
    if not sims:
        return None
    completed = [s for s in sims if s.status == "completed"]
    roundtrips = [s.roundtrip_minutes for s in completed]
    return ItemReport(
        item_id=item_id,
        name=name,
        simulations=len(sims),
        completed=len(completed),
        fill_rate=len(completed) / len(sims),
        median_roundtrip_min=statistics.median(roundtrips) if roundtrips else None,
        predicted_roundtrip_min=predicted_roundtrip_min,
        total_profit=sum(s.profit for s in completed),
    )


def run(conn, cash: int, top: int = 20, capture: float = CAPTURE_FRACTION) -> dict:
    candidates = rank_flips(conn, cash=cash)[:top]
    items, skipped = [], 0
    for c in candidates:
        limit_row = conn.execute(
            "SELECT buy_limit FROM items WHERE id=?", (c.item_id,)).fetchone()
        report = backtest_item(conn, c.item_id, c.name, limit_row["buy_limit"],
                               cash, capture, predicted_roundtrip_min=c.roundtrip_minutes)
        if report is None:
            skipped += 1
            continue
        items.append(report)

    all_sims = sum(i.simulations for i in items)
    all_completed = sum(i.completed for i in items)
    ratios = [i.median_roundtrip_min / i.predicted_roundtrip_min
              for i in items
              if i.median_roundtrip_min and i.predicted_roundtrip_min]
    now = int(time.time())
    conn.executemany(
        "INSERT INTO item_fill_stats VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(item_id) DO UPDATE SET updated_at=excluded.updated_at, "
        "capture=excluded.capture, simulations=excluded.simulations, "
        "fill_rate=excluded.fill_rate, median_roundtrip_min=excluded.median_roundtrip_min",
        [(i.item_id, now, capture, i.simulations, i.fill_rate, i.median_roundtrip_min)
         for i in items],
    )
    conn.commit()

    summary = {
        "ran_at": int(time.time()),
        "capture": capture,
        "cash": cash,
        "items_tested": len(items),
        "items_skipped": skipped,
        "total_simulations": all_sims,
        "overall_fill_rate": (all_completed / all_sims) if all_sims else None,
        # >1 means real fills are slower than the engine predicts at this
        # capture setting; multiply CAPTURE_FRACTION by this to calibrate.
        "median_actual_vs_predicted_roundtrip": statistics.median(ratios) if ratios else None,
        "items": [asdict(i) for i in items],
    }
    REPORT_PATH.parent.mkdir(exist_ok=True)
    REPORT_PATH.write_text(json.dumps(summary, indent=2))
    return summary
