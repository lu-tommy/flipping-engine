import time
import unittest
from unittest.mock import patch

from flipping import db
from flipping.suggestion import AccountOffer, AccountState, HeldItem, suggest

NOW = int(time.time())


def fixture_conn():
    """In-memory db with two liquid items and one thin item."""
    conn = db.connect(":memory:")
    conn.executemany("INSERT INTO items (id, name, members, buy_limit) VALUES (?,?,?,?)", [
        (1, "Liquid item", 0, 1000),
        (2, "Second item", 0, 500),
        (3, "Thin item", 0, 10),
    ])
    # latest quotes: (item, high, low)
    conn.executemany("INSERT INTO latest_snapshots VALUES (?,?,?,?,?,?)", [
        (NOW, 1, 1100, NOW - 60, 1000, NOW - 60),
        (NOW, 2, 5200, NOW - 60, 5000, NOW - 60),
        (NOW, 3, 900, NOW - 3600 * 2, 700, NOW - 3600 * 2),  # stale + wide
    ])
    # 1h volumes: liquid both sides for items 1, 2; item 3 one-sided
    conn.executemany("INSERT INTO bucket_1h VALUES (?,?,?,?,?,?)", [
        (NOW, 1, 1100, 5000, 1000, 5000),
        (NOW, 2, 5200, 800, 5000, 900),
        (NOW, 3, 900, 5, 700, 0),
    ])
    conn.commit()
    return conn


def no_timeseries(conn, item_id, timestep="5m"):
    return None  # stability check passes as "unchecked"


@patch("flipping.stability.get_timeseries_cached", no_timeseries)
class TestSuggest(unittest.TestCase):
    def setUp(self):
        self.conn = fixture_conn()

    def test_empty_account_suggests_buy(self):
        s = suggest(self.conn, AccountState(cash=1_000_000))
        self.assertEqual(s["type"], "buy")
        self.assertIn(s["item_id"], (1, 2))
        self.assertGreater(s["quantity"], 0)

    def test_outbid_buy_offer_aborted_first(self):
        # our bid 900 but sellers now accept 1000: dead offer
        state = AccountState(cash=1_000_000, offers=[
            AccountOffer(slot=0, type="buy", item_id=1, price=900, quantity=100)])
        s = suggest(self.conn, state)
        self.assertEqual(s["type"], "abort")
        self.assertEqual(s["slot"], 0)
        self.assertIn("outbid", s["message"])

    def test_overpriced_sell_offer_aborted(self):
        state = AccountState(cash=0, offers=[
            AccountOffer(slot=2, type="sell", item_id=1, price=1200, quantity=100)])
        s = suggest(self.conn, state)
        self.assertEqual(s["type"], "abort")
        self.assertIn("overpriced", s["message"])

    def test_on_market_offers_not_aborted(self):
        state = AccountState(cash=0, offers=[
            AccountOffer(slot=0, type="buy", item_id=1, price=1001, quantity=100),
            AccountOffer(slot=1, type="sell", item_id=2, price=5100, quantity=10)])
        s = suggest(self.conn, state)
        self.assertEqual(s["type"], "wait")

    def test_held_inventory_sold_before_new_buys(self):
        state = AccountState(cash=1_000_000, inventory=[HeldItem(item_id=1, quantity=50)])
        s = suggest(self.conn, state)
        self.assertEqual(s["type"], "sell")
        self.assertEqual(s["item_id"], 1)
        self.assertEqual(s["price"], 1099)  # high - 1
        self.assertEqual(s["quantity"], 50)

    def test_most_valuable_holding_sold_first(self):
        state = AccountState(cash=0, inventory=[
            HeldItem(item_id=1, quantity=10),     # 10 * 1100 = 11k
            HeldItem(item_id=2, quantity=100)])   # 100 * 5200 = 520k
        s = suggest(self.conn, state)
        self.assertEqual(s["item_id"], 2)

    def test_no_free_slot_waits(self):
        offers = [AccountOffer(slot=i, type="buy", item_id=1, price=1001, quantity=1)
                  for i in range(8)]
        s = suggest(self.conn, AccountState(cash=1_000_000, offers=offers))
        self.assertEqual(s["type"], "wait")

    def test_buy_skips_items_already_held_or_offered(self):
        state = AccountState(cash=1_000_000,
                             offers=[AccountOffer(slot=0, type="buy", item_id=1,
                                                  price=1001, quantity=10)])
        s = suggest(self.conn, state)
        if s["type"] == "buy":
            self.assertNotEqual(s["item_id"], 1)

    def test_f2p_slot_limit(self):
        offers = [AccountOffer(slot=i, type="buy", item_id=1, price=1001, quantity=1)
                  for i in range(3)]
        s = suggest(self.conn, AccountState(cash=1_000_000, offers=offers,
                                            total_slots=3))
        self.assertEqual(s["type"], "wait")


if __name__ == "__main__":
    unittest.main()
