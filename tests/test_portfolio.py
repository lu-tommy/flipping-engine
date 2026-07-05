import time
import unittest
from unittest.mock import patch

from flipping import db
from flipping.portfolio import plan

NOW = int(time.time())


def fixture_conn():
    conn = db.connect(":memory:")
    conn.executemany("INSERT INTO items (id, name, members, buy_limit) VALUES (?,?,?,?)", [
        (1, "Cheap liquid", 0, 10000),
        (2, "Expensive item", 0, 10),
    ])
    conn.executemany("INSERT INTO latest_snapshots VALUES (?,?,?,?,?,?)", [
        (NOW, 1, 1100, NOW - 30, 1000, NOW - 30),
        (NOW, 2, 2_200_000, NOW - 30, 2_000_000, NOW - 30),
    ])
    for h in range(3):
        ts = NOW - h * 3600
        conn.execute("INSERT INTO bucket_1h VALUES (?,?,?,?,?,?)", (ts, 1, 1100, 5000, 1000, 5000))
        conn.execute("INSERT INTO bucket_1h VALUES (?,?,?,?,?,?)", (ts, 2, 2_200_000, 20, 2_000_000, 20))
    conn.commit()
    return conn


@patch("flipping.stability.get_timeseries_cached", lambda *a, **k: None)
class TestPortfolio(unittest.TestCase):
    def test_allocates_across_slots_within_budget(self):
        conn = fixture_conn()
        p = plan(conn, cash=10_000_000, slots=4)
        self.assertGreaterEqual(p["slots_used"], 2)
        self.assertLessEqual(p["total_cost"], 10_000_000)
        self.assertEqual(p["cash_left"], 10_000_000 - p["total_cost"])
        # every allocation affordable within its rolling budget
        for a in p["allocations"]:
            self.assertGreaterEqual(a["quantity"], 1)
            self.assertEqual(a["cost"], a["quantity"] * a["buy_price"])

    def test_skips_unaffordable_per_slot(self):
        conn = fixture_conn()
        # 2m cash over 4 slots = 500k/slot: the 2m item can't fit a slot budget
        p = plan(conn, cash=2_000_000, slots=4)
        ids = [a["item_id"] for a in p["allocations"]]
        self.assertNotIn(2, ids)

    def test_projected_rate_positive(self):
        conn = fixture_conn()
        p = plan(conn, cash=10_000_000, slots=8)
        self.assertGreater(p["projected_gp_per_hour"], 0)


if __name__ == "__main__":
    unittest.main()
