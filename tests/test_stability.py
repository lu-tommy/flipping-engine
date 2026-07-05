import unittest

from flipping.stability import MIN_DATA_POINTS, Z_MAX, assess, price_stats


def make_series(low: int, high: int, n: int = 50, low_jitter: int = 0):
    """Synthetic buckets around fixed low/high prices."""
    out = []
    for i in range(n):
        jitter = (i % 3 - 1) * low_jitter  # -j, 0, +j repeating
        out.append({"avgLowPrice": low + jitter, "avgHighPrice": high + jitter,
                    "lowPriceVolume": 100, "highPriceVolume": 100})
    return out


class TestPriceStats(unittest.TestCase):
    def test_stats_over_stable_series(self):
        stats = price_stats(make_series(1000, 1100, low_jitter=10))
        mean_low, std_low, mean_high, std_high = stats
        self.assertAlmostEqual(mean_low, 1000, delta=5)
        self.assertAlmostEqual(mean_high, 1100, delta=5)
        self.assertLess(std_low, 15)

    def test_insufficient_data_returns_none(self):
        self.assertIsNone(price_stats(make_series(1000, 1100, n=MIN_DATA_POINTS - 1)))

    def test_buckets_missing_prices_are_skipped(self):
        series = make_series(1000, 1100)
        for b in series[::2]:
            b["avgLowPrice"] = None
        self.assertIsNotNone(price_stats(series))


class TestAssess(unittest.TestCase):
    def test_stable_flip_passes(self):
        series = make_series(1000, 1100, low_jitter=10)
        result = assess(buy_price=1001, sell_price=1099, series=series)
        self.assertTrue(result.ok)
        self.assertEqual(result.reason, "ok")

    def test_spike_buy_rejected(self):
        # buying far above where sellers have recently been
        series = make_series(1000, 1100, low_jitter=10)
        result = assess(buy_price=1300, sell_price=1350, series=series)
        self.assertFalse(result.ok)
        self.assertIn("spike", result.reason)
        self.assertGreater(result.buy_z, Z_MAX)

    def test_crash_sell_rejected(self):
        # the current high is far below the recent highs: market moving down
        series = make_series(1000, 1100, low_jitter=10)
        result = assess(buy_price=850, sell_price=900, series=series)
        self.assertFalse(result.ok)
        self.assertIn("crash", result.reason)

    def test_no_series_passes_unchecked(self):
        result = assess(1000, 1100, None)
        self.assertTrue(result.ok)
        self.assertIn("unchecked", result.reason)

    def test_thin_history_rejected(self):
        result = assess(1000, 1100, make_series(1000, 1100, n=5))
        self.assertFalse(result.ok)
        self.assertIn("history", result.reason)


if __name__ == "__main__":
    unittest.main()
