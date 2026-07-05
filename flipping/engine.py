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

# Fraction of one side's hourly flow we assume our offer captures WHEN the
# market price touches our level. The earlier 0.03 "calibration" conflated two
# effects: the backtest's slowness came mostly from waiting for price to cross
# our quotes, which v3 now models explicitly via touch fractions — so capture
# returns to 0.10. Real fills logged via the tracker are the true calibrator.
CAPTURE_FRACTION = 0.10
# v3 percentile pricing: quote near the edges of the recent price
# distribution — the spread IS the flip profit — and account for the waiting
# via touch fractions instead of surrendering margin. p25 of lows ~= the
# typical bid edge without chasing single-trade outliers.
BUY_PERCENTILE = 0.25
SELL_PERCENTILE = 0.75
# Quick mode steps slightly inside the edges: a bit less margin, faster touch.
QUICK_BUY_PERCENTILE = 0.35
QUICK_SELL_PERCENTILE = 0.65
# How many coarse-ranked candidates get timeseries-based deep pricing.
DEEP_PRICE_TOP = 60
# Reject buys when the recent mid-price sits this far below the earlier mid:
# buying into a falling market means the exit price keeps moving away.
DOWNTREND_LIMIT = 0.02
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

    for c in candidates[:DEEP_PRICE_TOP]:
        _deep_reprice(conn, c, cash, max_roundtrip_minutes, prior_fill_rate)

    candidates.sort(key=lambda c: c.gp_per_hour, reverse=True)
    return candidates


def _percentile(sorted_vals: list, q: float):
    return sorted_vals[min(len(sorted_vals) - 1, max(0, int(q * len(sorted_vals))))]


def _deep_reprice(conn, c: "FlipCandidate", cash: int,
                  max_roundtrip_minutes: float | None, prior_fill_rate: float) -> None:
    """Replace coarse latest-quote pricing with percentile pricing from the
    item's recent 5m price distribution (v3).

    Prices are chosen for fill velocity: a bid at the p55 of recent lows is
    fillable in ~55% of 5m windows. Fill-rate estimates are scaled by those
    touch fractions — this models the price-crossing wait that the backtest
    exposed, instead of blaming it on a low capture fraction.
    """
    quick = max_roundtrip_minutes is not None
    series = stability.get_timeseries_cached(conn, c.item_id)
    if not series:
        c.stability = stability.StabilityResult(True, "unchecked: no timeseries")
        return  # keep coarse numbers

    recent = series[-288:]  # ~24h of 5m buckets
    lows = sorted(b["avgLowPrice"] for b in recent if b.get("avgLowPrice"))
    highs = sorted(b["avgHighPrice"] for b in recent if b.get("avgHighPrice"))
    if len(lows) < 12 or len(highs) < 12:
        c.stability = stability.StabilityResult(False, "too little trade history")
        return

    buy = _percentile(lows, QUICK_BUY_PERCENTILE if quick else BUY_PERCENTILE)
    sell = _percentile(highs, QUICK_SELL_PERCENTILE if quick else SELL_PERCENTILE)
    if buy > cash:
        c.stability = stability.StabilityResult(False, "buy price above cash stack")
        return
    margin = flip_margin(buy, sell, c.name)
    if margin < 1:
        c.stability = stability.StabilityResult(False, "no margin at velocity prices")
        return
    if margin / buy > MAX_MARGIN_RATIO:
        c.stability = stability.StabilityResult(False, "margin too good to be true")
        return

    # crash guard: if the market's current highs sit below our ask, the exit
    # price is historical fiction right now
    last_highs = [b["avgHighPrice"] for b in recent[-3:] if b.get("avgHighPrice")]
    if last_highs and max(last_highs) < sell:
        c.stability = stability.StabilityResult(False, "market trading below target sell")
        return

    # downtrend guard: recent ~2h mid-price vs the earlier window
    mids = [(b["avgHighPrice"] + b["avgLowPrice"]) / 2
            for b in recent if b.get("avgHighPrice") and b.get("avgLowPrice")]
    if len(mids) >= 36:
        recent_mid = statistics.median(mids[-24:])
        older_mid = statistics.median(mids[:-24])
        drop = (recent_mid - older_mid) / older_mid
        if drop < -DOWNTREND_LIMIT:
            c.stability = stability.StabilityResult(
                False, f"downtrend: mid price {abs(drop):.1%} below earlier level")
            return

    touch_buy = sum(1 for v in lows if v <= buy) / len(lows)
    touch_sell = sum(1 for v in highs if v >= sell) / len(highs)
    buy_rate = CAPTURE_FRACTION * c.hourly_buy_side_vol * touch_buy    # items/hour
    sell_rate = CAPTURE_FRACTION * c.hourly_sell_side_vol * touch_sell
    if buy_rate <= 0 or sell_rate <= 0:
        c.stability = stability.StabilityResult(False, "one-sided at these prices")
        return

    quantity = min(
        c.quantity if c.quantity > 0 else 1,   # coarse caps (limit/cash/volume share)
        max(1, int((max_roundtrip_minutes / 60) / (1 / buy_rate + 1 / sell_rate)))
        if quick else 10**9,
        cash // buy,
    )
    roundtrip_hours = max(quantity / buy_rate + quantity / sell_rate, MIN_ROUNDTRIP_HOURS)
    if quick and roundtrip_hours * 60 > max_roundtrip_minutes * 1.5:
        c.stability = stability.StabilityResult(False, "too slow even at velocity prices")
        return

    est_profit = margin * quantity
    fill = c.fill_rate if c.fill_rate is not None else prior_fill_rate
    # abort-and-reprice model: an unfilled flip is re-priced and usually still
    # completes at reduced margin, not a total loss — so soften the penalty
    ev = est_profit * (0.5 + 0.5 * fill)

    c.buy_price = buy
    c.sell_price = sell
    c.margin = margin
    c.quantity = quantity
    c.est_profit = est_profit
    c.roundtrip_minutes = roundtrip_hours * 60
    c.gp_per_hour = int(ev / roundtrip_hours)
    c.stability = stability.assess(buy, sell, series)


def stable_flips(conn, **kwargs) -> tuple[list[FlipCandidate], list[FlipCandidate]]:
    """(passing, rejected) split of ranked candidates by stability check."""
    ranked = rank_flips(conn, **kwargs)
    passing = [c for c in ranked if c.stability.ok]
    rejected = [c for c in ranked if not c.stability.ok]
    return passing, rejected
