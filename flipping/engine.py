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

import statistics
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
# In quick-flip mode the item must be trading *right now*.
QUICK_MAX_QUOTE_AGE_S = 10 * 60
# How many recent 1h buckets volume/price medians are computed over.
HOURLY_HISTORY_BUCKETS = 6
# Quick mode: item must have traded both sides in this many of the last 6
# five-minute buckets.
QUICK_5M_ACTIVE_REQUIRED = 4
# Margins above this fraction of the buy price are treated as data artifacts.
MAX_MARGIN_RATIO = 0.12
# Never plan to absorb more than this share of one side's hourly volume.
MAX_VOLUME_SHARE = 0.25
# How many top candidates get the (per-item, cached) timeseries stability check.
STABILITY_CHECK_TOP = 40
# Backtest-measured fill stats older than this are ignored.
FILL_STATS_MAX_AGE_S = 7 * 24 * 3600


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
    # backtest-measured; None until this item has been backtested
    fill_rate: float | None = None


def _hourly_history(conn) -> dict[int, dict]:
    """Per-item medians over the last HOURLY_HISTORY_BUCKETS 1h buckets.

    consistent = the item traded on both sides in every sampled hour.
    Items missing from some buckets are inconsistent by definition.
    """
    ts_rows = conn.execute(
        "SELECT DISTINCT bucket_ts FROM bucket_1h ORDER BY bucket_ts DESC LIMIT ?",
        (HOURLY_HISTORY_BUCKETS,)).fetchall()
    ts_list = [r["bucket_ts"] for r in ts_rows]
    if not ts_list:
        return {}
    qmarks = ",".join("?" * len(ts_list))
    rows = conn.execute(
        f"SELECT item_id, avg_high, high_vol, avg_low, low_vol "
        f"FROM bucket_1h WHERE bucket_ts IN ({qmarks})", ts_list).fetchall()

    grouped: dict[int, list] = {}
    for r in rows:
        grouped.setdefault(r["item_id"], []).append(r)

    out = {}
    n_buckets = len(ts_list)
    for item_id, bs in grouped.items():
        active = sum(1 for b in bs if (b["high_vol"] or 0) > 0 and (b["low_vol"] or 0) > 0)
        highs = [b["avg_high"] for b in bs if b["avg_high"]]
        lows = [b["avg_low"] for b in bs if b["avg_low"]]
        out[item_id] = {
            "consistent": len(bs) == n_buckets and active == n_buckets,
            "med_high_vol": int(statistics.median([b["high_vol"] or 0 for b in bs])),
            "med_low_vol": int(statistics.median([b["low_vol"] or 0 for b in bs])),
            "med_avg_high": int(statistics.median(highs)) if highs else None,
            "med_avg_low": int(statistics.median(lows)) if lows else None,
        }
    return out


def _recent_5m_activity(conn) -> tuple[dict[int, int], int]:
    """(item_id -> two-sided-active count over the last six 5m buckets,
    required count scaled down if fewer buckets have been collected)."""
    ts_rows = conn.execute(
        "SELECT DISTINCT bucket_ts FROM bucket_5m ORDER BY bucket_ts DESC LIMIT 6").fetchall()
    ts_list = [r["bucket_ts"] for r in ts_rows]
    if not ts_list:
        return {}, 0
    qmarks = ",".join("?" * len(ts_list))
    rows = conn.execute(
        f"SELECT item_id, COUNT(*) AS n FROM bucket_5m "
        f"WHERE bucket_ts IN ({qmarks}) AND high_vol > 0 AND low_vol > 0 "
        f"GROUP BY item_id", ts_list).fetchall()
    required = min(QUICK_5M_ACTIVE_REQUIRED, len(ts_list))
    return {r["item_id"]: r["n"] for r in rows}, required


def rank_flips(conn, cash: int, f2p_only: bool = False,
               min_profit: int = 0, max_roundtrip_minutes: float | None = None,
               now: int | None = None) -> list[FlipCandidate]:
    now = now or int(time.time())

    # Prior fill probability for items the backtest hasn't measured. Without
    # this, measured items are penalized by their real fill rate while
    # unmeasured ones keep optimistic formula numbers and unfairly win the
    # ranking (adverse selection toward untested items).
    prior_row = conn.execute(
        "SELECT AVG(fill_rate) AS p FROM item_fill_stats WHERE updated_at > ?",
        (now - FILL_STATS_MAX_AGE_S,),
    ).fetchone()
    prior_fill_rate = prior_row["p"] if prior_row["p"] is not None else 1.0

    hourly = _hourly_history(conn)
    recent_5m_active, active_required = (
        _recent_5m_activity(conn) if max_roundtrip_minutes is not None else (None, 0))
    quote_max_age = QUICK_MAX_QUOTE_AGE_S if max_roundtrip_minutes is not None else MAX_QUOTE_AGE_S

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
               f.fill_rate, f.median_roundtrip_min, f.updated_at AS fill_updated_at
        FROM items i
        JOIN latest l ON l.item_id = i.id
        LEFT JOIN item_fill_stats f ON f.item_id = i.id
        WHERE l.high IS NOT NULL AND l.low IS NOT NULL
          AND i.buy_limit IS NOT NULL
    """).fetchall()

    candidates = []
    for r in rows:
        if f2p_only and r["members"]:
            continue
        if min(r["high_time"] or 0, r["low_time"] or 0) < now - quote_max_age:
            continue

        hist = hourly.get(r["id"])
        if hist is None or not hist["consistent"]:
            # not two-sided-active in every recent hour: too spotty to trust
            continue
        # medians across recent hours, not one (possibly lucky) bucket
        sell_side_vol = hist["med_high_vol"]
        buy_side_vol = hist["med_low_vol"]
        if sell_side_vol == 0 or buy_side_vol == 0:
            continue

        if recent_5m_active is not None and recent_5m_active.get(r["id"], 0) < active_required:
            continue  # quick mode: must be trading both sides right now

        buy_price = r["low"] + 1
        sell_price = r["high"] - 1
        if buy_price <= 0 or sell_price <= buy_price or buy_price > cash:
            continue

        # Rank on the conservative margin: the worse of (a) the latest-trade
        # spread and (b) the median averaged spread over recent hours. A
        # margin that only exists in one outlier trade dies here.
        margin_latest = flip_margin(buy_price, sell_price, r["name"])
        margin = margin_latest
        if hist["med_avg_low"] and hist["med_avg_high"]:
            margin_avg = flip_margin(hist["med_avg_low"] + 1,
                                     hist["med_avg_high"] - 1, r["name"])
            margin = min(margin_latest, margin_avg)
        if margin < 1:
            continue
        if margin / buy_price > MAX_MARGIN_RATIO:
            continue

        quantity = min(
            r["buy_limit"],
            cash // buy_price,
            max(1, int(min(buy_side_vol, sell_side_vol) * MAX_VOLUME_SHARE)),
        )
        if max_roundtrip_minutes is not None:
            # quick-flip mode: shrink the order until the round trip fits the
            # time budget. buy_h + sell_h <= budget solved for quantity:
            budget_h = max_roundtrip_minutes / 60
            time_qty = int(budget_h * CAPTURE_FRACTION
                           * (buy_side_vol * sell_side_vol) / (buy_side_vol + sell_side_vol))
            quantity = min(quantity, time_qty)
        if quantity < 1:
            continue

        buy_hours = quantity / (CAPTURE_FRACTION * buy_side_vol)
        sell_hours = quantity / (CAPTURE_FRACTION * sell_side_vol)
        roundtrip_hours = max(buy_hours + sell_hours, MIN_ROUNDTRIP_HOURS)

        # Prefer measured behavior over the formula when the backtest has
        # covered this item recently: use the observed roundtrip time and
        # weight profit by the observed probability of completing at all.
        fill_rate = None
        fresh_stats = (r["fill_rate"] is not None
                       and r["fill_updated_at"] > now - FILL_STATS_MAX_AGE_S)
        if fresh_stats:
            fill_rate = r["fill_rate"]
            # observed roundtrips were measured at full backtest quantity;
            # in quick-flip mode our reduced quantity makes them stale, so
            # keep the formula estimate there instead
            if r["median_roundtrip_min"] and max_roundtrip_minutes is None:
                roundtrip_hours = max(r["median_roundtrip_min"] / 60, MIN_ROUNDTRIP_HOURS)

        est_profit = margin * quantity
        if est_profit < min_profit:
            continue
        effective_fill = fill_rate if fill_rate is not None else prior_fill_rate
        expected_profit = est_profit * effective_fill

        # active-flipping mode: skip anything that turns over too slowly,
        # no matter how good its GP/hr looks on paper
        if max_roundtrip_minutes is not None and roundtrip_hours * 60 > max_roundtrip_minutes:
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
            gp_per_hour=int(expected_profit / roundtrip_hours),
            hourly_buy_side_vol=buy_side_vol,
            hourly_sell_side_vol=sell_side_vol,
            fill_rate=fill_rate,
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
