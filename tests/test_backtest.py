import unittest

from flipping.backtest import SimResult, simulate_entry

CASH = 10_000_000


def bucket(low, high, low_vol=1000, high_vol=1000, ts=0):
    return {"timestamp": ts, "avgLowPrice": low, "avgHighPrice": high,
            "lowPriceVolume": low_vol, "highPriceVolume": high_vol}


def flat_series(n=200, low=1000, high=1100):
    return [bucket(low, high, ts=i * 300) for i in range(n)]


class TestSimulateEntry(unittest.TestCase):
    def test_completes_on_stable_liquid_market(self):
        r = simulate_entry(flat_series(), 0, buy_limit=100, cash=CASH,
                           item_name=None, capture=0.1)
        self.assertIsInstance(r, SimResult)
        self.assertEqual(r.status, "completed")
        self.assertGreater(r.profit, 0)
        # 100 qty at 10% of 1000/bucket = 1 bucket per side
        self.assertEqual(r.roundtrip_minutes, 10.0)

    def test_buy_never_fills_when_price_rises_away(self):
        series = flat_series(5)
        # after entry, sellers only accept far higher prices: our bid is never hit
        series += [bucket(5000, 5100, ts=(5 + i) * 300) for i in range(150)]
        r = simulate_entry(series, 4, buy_limit=100, cash=CASH,
                           item_name=None, capture=0.1)
        self.assertEqual(r.status, "buy_unfilled")
        self.assertEqual(r.profit, 0)

    def test_sell_never_fills_after_crash(self):
        series = flat_series(10)
        # crash right after entry: buys fill instantly (sellers dump), but
        # nobody ever insta-buys near our ask again
        series += [bucket(500, 550, ts=(10 + i) * 300) for i in range(150)]
        r = simulate_entry(series, 9, buy_limit=100, cash=CASH,
                           item_name=None, capture=0.1)
        self.assertEqual(r.status, "sell_unfilled")

    def test_skips_entry_with_no_spread(self):
        series = [bucket(1000, 1001, ts=i * 300) for i in range(150)]
        self.assertIsNone(simulate_entry(series, 0, buy_limit=100, cash=CASH,
                                         item_name=None, capture=0.1))

    def test_skips_outlier_margin(self):
        # 50% spread = data artifact, engine would never enter
        series = [bucket(1000, 1500, ts=i * 300) for i in range(150)]
        self.assertIsNone(simulate_entry(series, 0, buy_limit=100, cash=CASH,
                                         item_name=None, capture=0.1))

    def test_low_capture_slows_fills(self):
        fast = simulate_entry(flat_series(), 0, buy_limit=100, cash=CASH,
                              item_name=None, capture=0.1)
        slow = simulate_entry(flat_series(), 0, buy_limit=100, cash=CASH,
                              item_name=None, capture=0.01)
        self.assertEqual(slow.status, "completed")
        self.assertGreater(slow.roundtrip_minutes, fast.roundtrip_minutes)


if __name__ == "__main__":
    unittest.main()
