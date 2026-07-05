"""Heuristic flip ranking (engine v1).

For each item we model the standard flip: place a buy offer 1 gp above the
current insta-sell price (`low`), then sell 1 gp below the current insta-buy
price (`high`). Rank by estimated GP/hour.

Volume semantics from the wiki API:
  highPriceVolume = items insta-BOUGHT at the high price. Our SELL offer at
                    ~high is filled by these buyers.
  lowPriceVolume  = items insta-SOLD at the low price. Our BUY offer at
                    ~low is filled by these sellers.

Guards against the classic data traps:
  - stale quotes (item barely trades; last trade too old)
  - one-sided markets (volume on only one side of the book)
  - too-good-to-be-true margins (usually a crash in progress or manipulation)
"""

import time
from dataclasses import dataclass, field

from . import stability
from .ge_tax import flip_margin

# Fraction of one side's hourly flow we assume our offer captures.
# Calibrated 2026-07-05: at 0.10 the backtest showed real roundtrips running
# ~3.6x slower than predicted (43% fill rate within 8h). 0.10/3.6 ~= 0.03.
# Re-run `cli backtest` after data accumulates and adjust.
CAPTURE_FRACTION = 0.03
# Floor on estimated round-trip so thin items don't show absurd GP/hr.
MIN_ROUNDTRIP_HOURS = 5 / 60
# Quotes older than this are considered stale and the item is skipped.
MAX_QUOTE_AGE_S = 30 * 60
# Margins above this fraction of the buy price are treated as data artifacts.
MAX_MARGIN_RATIO = 0.12
# Never plan to absorb more than this share of one side's hourly volume.
MAX_VOLUME_SHARE = 0.25
# How many top candidates get the (per-item, cached) timeseries stability check.
STABILITY_CHECK_TOP = 40


@dataclass
class FlipCandidate:
    item_id: int
    name: str
    members: bool
    buy_price: int
    sell_price: int
    margin: int
    quantity: int
    est_profit: int
    roundtrip_minutes: float
    gp_per_hour: int
    hourly_buy_side_vol: int   # lowPriceVolume: how fast our buy fills
    hourly_sell_side_vol: int  # highPriceVolume: how fast our sell fills
    stability: stability.StabilityResult = field(
        default_factory=lambda: stability.StabilityResult(True, "unchecked: below check depth"))


def rank_flips(conn, cash: int, f2p_only: bool = False,
               min_profit: int = 0, now: int | None = None) -> list[FlipCandidate]:
    now = now or int(time.time())

    rows = conn.execute("""
        WITH latest AS (
            SELECT * FROM latest_snapshots
            WHERE fetched_at = (SELECT MAX(fetched_at) FROM latest_snapshots)
        ),
        hour AS (
            SELECT * FROM bucket_1h
            WHERE bucket_ts = (SELECT MAX(bucket_ts) FROM bucket_1h)
        )
        SELECT i.id, i.name, i.members, i.buy_limit,
               l.high, l.high_time, l.low, l.low_time,
               h.high_vol, h.low_vol
        FROM items i
        JOIN latest l ON l.item_id = i.id
        JOIN hour h   ON h.item_id = i.id
        WHERE l.high IS NOT NULL AND l.low IS NOT NULL
          AND i.buy_limit IS NOT NULL
    """).fetchall()

    candidates = []
    for r in rows:
        if f2p_only and r["members"]:
            continue
        if min(r["high_time"] or 0, r["low_time"] or 0) < now - MAX_QUOTE_AGE_S:
            continue
        sell_side_vol = r["high_vol"] or 0
        buy_side_vol = r["low_vol"] or 0
        if sell_side_vol == 0 or buy_side_vol == 0:
            continue

        buy_price = r["low"] + 1
        sell_price = r["high"] - 1
        if buy_price <= 0 or sell_price <= buy_price or buy_price > cash:
            continue

        margin = flip_margin(buy_price, sell_price, r["name"])
        if margin < 1:
            continue
        if margin / buy_price > MAX_MARGIN_RATIO:
            continue

        quantity = min(
            r["buy_limit"],
            cash // buy_price,
            max(1, int(min(buy_side_vol, sell_side_vol) * MAX_VOLUME_SHARE)),
        )
        if quantity < 1:
            continue

        buy_hours = quantity / (CAPTURE_FRACTION * buy_side_vol)
        sell_hours = quantity / (CAPTURE_FRACTION * sell_side_vol)
        roundtrip_hours = max(buy_hours + sell_hours, MIN_ROUNDTRIP_HOURS)

        est_profit = margin * quantity
        if est_profit < min_profit:
            continue

        candidates.append(FlipCandidate(
            item_id=r["id"],
            name=r["name"],
            members=bool(r["members"]),
            buy_price=buy_price,
            sell_price=sell_price,
            margin=margin,
            quantity=quantity,
            est_profit=est_profit,
            roundtrip_minutes=roundtrip_hours * 60,
            gp_per_hour=int(est_profit / roundtrip_hours),
            hourly_buy_side_vol=buy_side_vol,
            hourly_sell_side_vol=sell_side_vol,
        ))

    candidates.sort(key=lambda c: c.gp_per_hour, reverse=True)

    for c in candidates[:STABILITY_CHECK_TOP]:
        series = stability.get_timeseries_cached(conn, c.item_id)
        c.stability = stability.assess(c.buy_price, c.sell_price, series)

    return candidates


def stable_flips(conn, **kwargs) -> tuple[list[FlipCandidate], list[FlipCandidate]]:
    """(passing, rejected) split of ranked candidates by stability check."""
    ranked = rank_flips(conn, **kwargs)
    passing = [c for c in ranked if c.stability.ok]
    rejected = [c for c in ranked if not c.stability.ok]
    return passing, rejected
