import time
import unittest
from unittest.mock import patch

from flipping import db
from flipping.engine import rank_flips

NOW = int(time.time())
CASH = 10_000_000


def conn_with(buckets_1h, buckets_5m=(), items=None, latest=None):
    conn = db.connect(":memory:")
    conn.executemany(
        "INSERT INTO items (id, name, members, buy_limit) VALUES (?,?,?,?)",
        items or [(1, "Steady item", 0, 10000), (2, "Spiky item", 0, 10000)])
    conn.executemany(
        "INSERT INTO latest_snapshots VALUES (?,?,?,?,?,?)",
        latest or [(NOW, 1, 1100, NOW - 30, 1000, NOW - 30),
                   (NOW, 2, 1100, NOW - 30, 1000, NOW - 30)])
    conn.executemany("INSERT INTO bucket_1h VALUES (?,?,?,?,?,?)", buckets_1h)
    conn.executemany("INSERT INTO bucket_5m VALUES (?,?,?,?,?,?)", buckets_5m)
    conn.commit()
    return conn


def no_ts(conn, item_id, timestep="5m"):
    return None


@patch("flipping.stability.get_timeseries_cached", no_ts)
class TestConsistency(unittest.TestCase):
    def test_item_active_every_hour_passes_spotty_item_fails(self):
        buckets = []
        for h in range(3):
            ts = NOW - h * 3600
            buckets.append((ts, 1, 1100, 1000, 1000, 1000))  # steady: always active
            # spiky: volume only in the newest bucket, absent otherwise
            if h == 0:
                buckets.append((ts, 2, 1100, 9000, 1000, 9000))
        conn = conn_with(buckets)
        ids = [c.item_id for c in rank_flips(conn, cash=CASH)]
        self.assertIn(1, ids)
        self.assertNotIn(2, ids)

    def test_one_sided_hour_fails_consistency(self):
        buckets = []
        for h in range(3):
            ts = NOW - h * 3600
            buckets.append((ts, 1, 1100, 1000, 1000, 1000))
            # item 2 has a zero-sell-side hour in the middle
            buckets.append((ts, 2, 1100, 1000 if h != 1 else 0, 1000, 1000))
        conn = conn_with(buckets)
        ids = [c.item_id for c in rank_flips(conn, cash=CASH)]
        self.assertEqual(ids, [1])

    def test_median_volume_not_spike(self):
        # hours: 100, 100, 9000 -> median 100 caps quantity, not the spike
        buckets = [(NOW - h * 3600, 1, 1100, v, 1000, v)
                   for h, v in enumerate([9000, 100, 100])]
        conn = conn_with(buckets, items=[(1, "Steady item", 0, 10000)],
                         latest=[(NOW, 1, 1100, NOW - 30, 1000, NOW - 30)])
        c = rank_flips(conn, cash=CASH)[0]
        self.assertLessEqual(c.quantity, 25)  # 25% of median 100

    def test_outlier_latest_margin_rejected_by_avg_margin(self):
        # latest quotes show a juicy spread, but hourly averages show ~none
        buckets = [(NOW - h * 3600, 1, 1005, 1000, 1000, 1000) for h in range(3)]
        conn = conn_with(buckets, items=[(1, "Steady item", 0, 10000)],
                         latest=[(NOW, 1, 1100, NOW - 30, 1000, NOW - 30)])
        self.assertEqual(rank_flips(conn, cash=CASH), [])

    def test_quick_mode_requires_recent_5m_activity(self):
        buckets_1h = [(NOW - h * 3600, 1, 1100, 1000, 1000, 1000) for h in range(3)]
        # only 2 of 6 recent 5m buckets active
        buckets_5m = [(NOW - i * 300, 1, 1100, 100 if i < 2 else 0, 1000, 100 if i < 2 else 0)
                      for i in range(6)]
        conn = conn_with(buckets_1h, buckets_5m,
                         items=[(1, "Steady item", 0, 10000)],
                         latest=[(NOW, 1, 1100, NOW - 30, 1000, NOW - 30)])
        # normal mode skips the 5m recency check; quick mode enforces it
        self.assertEqual(len(rank_flips(conn, cash=CASH)), 1)
        self.assertEqual(rank_flips(conn, cash=CASH, max_roundtrip_minutes=60), [])


def make_series(n, low, high):
    return [{"timestamp": i * 300, "avgLowPrice": low, "avgHighPrice": high,
             "lowPriceVolume": 500, "highPriceVolume": 500} for i in range(n)]


class TestDeepRepricing(unittest.TestCase):
    def setUp(self):
        buckets = [(NOW - h * 3600, 1, 1100, 1000, 1000, 1000) for h in range(3)]
        self.conn = conn_with(buckets, items=[(1, "Steady item", 0, 10000)],
                              latest=[(NOW, 1, 1100, NOW - 30, 1000, NOW - 30)])

    def test_stable_series_priced_at_edges(self):
        series = make_series(124, 1000, 1100)
        with patch("flipping.stability.get_timeseries_cached", lambda *a, **k: series):
            flips = rank_flips(self.conn, cash=CASH)
        self.assertEqual(len(flips), 1)
        c = flips[0]
        self.assertTrue(c.stability.ok)
        self.assertEqual(c.buy_price, 1000)   # p25 of lows
        self.assertEqual(c.sell_price, 1100)  # p75 of highs
        self.assertEqual(c.margin, 78)        # 1100 - 22 tax - 1000

    def test_downtrend_rejected(self):
        # sell side holds but the bid side collapsed recently: mid down ~5%
        series = make_series(100, 1000, 1100) + make_series(24, 900, 1100)
        with patch("flipping.stability.get_timeseries_cached", lambda *a, **k: series):
            flips = rank_flips(self.conn, cash=CASH)
        self.assertEqual(len(flips), 1)
        self.assertFalse(flips[0].stability.ok)
        self.assertIn("downtrend", flips[0].stability.reason)

    def test_crash_below_sell_rejected(self):
        # recent highs fell below the p75 target sell
        series = make_series(120, 1000, 1100) + make_series(4, 950, 1020)
        with patch("flipping.stability.get_timeseries_cached", lambda *a, **k: series):
            flips = rank_flips(self.conn, cash=CASH)
        self.assertFalse(flips[0].stability.ok)
        self.assertIn("below target sell", flips[0].stability.reason)


if __name__ == "__main__":
    unittest.main()
