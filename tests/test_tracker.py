import time
import unittest

from flipping import db, tracker

NOW = int(time.time())


def fixture_conn():
    conn = db.connect(":memory:")
    conn.executemany("INSERT INTO items (id, name, members, buy_limit) VALUES (?,?,?,?)", [
        (1, "Abyssal whip", 1, 70),
        (2, "Lobster", 0, 6000),  # tax-exempt
    ])
    conn.execute("INSERT INTO latest_snapshots VALUES (?,?,?,?,?,?)",
                 (NOW, 1, 1_500_000, NOW, 1_450_000, NOW))
    conn.commit()
    return conn


class TestTracker(unittest.TestCase):
    def setUp(self):
        self.conn = fixture_conn()

    def test_full_lifecycle_with_tax(self):
        fid = tracker.add_flip(self.conn, item_id=1, quantity=2, buy_price=1_450_000)
        self.assertTrue(tracker.mark_bought(self.conn, fid))
        result = tracker.mark_sold(self.conn, fid, actual_price=1_500_000)
        # tax: 2% of 1.5m = 30k/item; margin = 1.5m - 30k - 1.45m = 20k/item
        self.assertEqual(result["tax"], 60_000)
        self.assertEqual(result["profit"], 40_000)

        row = self.conn.execute("SELECT * FROM active_flips WHERE id=?", (fid,)).fetchone()
        self.assertEqual(row["status"], "done")

        fills = self.conn.execute(
            "SELECT type, quantity, spent FROM observed_fills ORDER BY id").fetchall()
        self.assertEqual([f["type"] for f in fills], ["buy", "sell"])
        self.assertEqual(fills[0]["spent"], 2_900_000)
        self.assertEqual(fills[1]["spent"], 3_000_000 - 60_000)

    def test_exempt_item_no_tax(self):
        fid = tracker.add_flip(self.conn, item_id=2, quantity=100, buy_price=150)
        tracker.mark_bought(self.conn, fid)
        result = tracker.mark_sold(self.conn, fid, actual_price=170)
        self.assertEqual(result["tax"], 0)
        self.assertEqual(result["profit"], 2000)

    def test_bought_records_actual_price(self):
        fid = tracker.add_flip(self.conn, item_id=1, quantity=1, buy_price=1_450_000)
        tracker.mark_bought(self.conn, fid, actual_price=1_449_000)
        row = self.conn.execute("SELECT buy_price, status FROM active_flips WHERE id=?",
                                (fid,)).fetchone()
        self.assertEqual(row["buy_price"], 1_449_000)
        self.assertEqual(row["status"], "selling")

    def test_cannot_sell_before_bought(self):
        fid = tracker.add_flip(self.conn, item_id=1, quantity=1, buy_price=100)
        self.assertIsNone(tracker.mark_sold(self.conn, fid, actual_price=200))

    def test_sold_requires_a_price(self):
        fid = tracker.add_flip(self.conn, item_id=1, quantity=1, buy_price=100)
        tracker.mark_bought(self.conn, fid)
        self.assertIsNone(tracker.mark_sold(self.conn, fid))  # no target, no actual

    def test_update_prices_and_list_live_merge(self):
        fid = tracker.add_flip(self.conn, item_id=1, quantity=1, buy_price=1_400_000)
        tracker.update_prices(self.conn, fid, sell_price=1_490_000)
        listing = tracker.list_flips(self.conn)
        flip = listing["active"][0]
        self.assertEqual(flip["sell_price"], 1_490_000)
        self.assertEqual(flip["live_high"], 1_500_000)
        self.assertEqual(flip["suggested_sell"], 1_499_999)
        self.assertEqual(flip["name"], "Abyssal whip")
        # profit if sold at live suggested: (1499999 - 2% tax) - 1400000
        self.assertGreater(flip["profit_at_suggested"], 0)

    def test_session_stats(self):
        fid = tracker.add_flip(self.conn, item_id=1, quantity=1, buy_price=1_450_000)
        tracker.mark_bought(self.conn, fid)
        tracker.mark_sold(self.conn, fid, actual_price=1_500_000)
        listing = tracker.list_flips(self.conn)
        self.assertEqual(listing["session"]["completed_24h"], 1)
        self.assertEqual(listing["session"]["profit_24h"], 20_000)

    def test_delete(self):
        fid = tracker.add_flip(self.conn, item_id=1, quantity=1, buy_price=100)
        self.assertTrue(tracker.delete_flip(self.conn, fid))
        self.assertFalse(tracker.delete_flip(self.conn, fid))

    def test_search_items(self):
        results = tracker.search_items(self.conn, "whip")
        self.assertEqual(results[0]["name"], "Abyssal whip")


if __name__ == "__main__":
    unittest.main()
