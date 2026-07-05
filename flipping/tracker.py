"""Active flip tracking: the manual replacement for in-game offer tracking.

Lifecycle: buying -> selling -> done.
  - add: you placed (or intend to place) a buy offer
  - mark bought: buy filled in-game; records actual buy price and logs a
    ground-truth buy fill for calibration
  - mark sold: sell filled; computes post-tax profit and logs the sell fill

Live low/high prices are merged into listings so that the moment a buy
completes you can see the current sell price without leaving the dashboard.
"""

import time

from .ge_tax import flip_margin, sale_tax


def add_flip(conn, item_id: int, quantity: int, buy_price: int,
             sell_price: int | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO active_flips (item_id, quantity, buy_price, sell_price, status, created_ts) "
        "VALUES (?,?,?,?,'buying',?)",
        (item_id, quantity, buy_price, sell_price, int(time.time())),
    )
    conn.commit()
    return cur.lastrowid


def update_prices(conn, flip_id: int, buy_price: int | None = None,
                  sell_price: int | None = None, quantity: int | None = None) -> bool:
    row = conn.execute("SELECT id FROM active_flips WHERE id=? AND status != 'done'",
                       (flip_id,)).fetchone()
    if row is None:
        return False
    sets, params = [], []
    for col, val in (("buy_price", buy_price), ("sell_price", sell_price),
                     ("quantity", quantity)):
        if val is not None:
            sets.append(f"{col}=?")
            params.append(val)
    if not sets:
        return True
    params.append(flip_id)
    conn.execute(f"UPDATE active_flips SET {', '.join(sets)} WHERE id=?", params)
    conn.commit()
    return True


def mark_bought(conn, flip_id: int, actual_price: int | None = None,
                actual_quantity: int | None = None, display_name: str = "") -> bool:
    row = conn.execute("SELECT * FROM active_flips WHERE id=? AND status='buying'",
                       (flip_id,)).fetchone()
    if row is None:
        return False
    now = int(time.time())
    price = actual_price if actual_price is not None else row["buy_price"]
    qty = actual_quantity if actual_quantity is not None else row["quantity"]
    conn.execute(
        "UPDATE active_flips SET status='selling', buy_price=?, quantity=?, bought_ts=? WHERE id=?",
        (price, qty, now, flip_id),
    )
    conn.execute(
        "INSERT INTO observed_fills (ts, item_id, type, offer_price, quantity, spent, display_name) "
        "VALUES (?,?,?,?,?,?,?)",
        (now, row["item_id"], "buy", price, qty, price * qty, display_name),
    )
    conn.commit()
    return True


def mark_sold(conn, flip_id: int, actual_price: int | None = None,
              display_name: str = "") -> dict | None:
    row = conn.execute("SELECT * FROM active_flips WHERE id=? AND status='selling'",
                       (flip_id,)).fetchone()
    if row is None:
        return None
    now = int(time.time())
    price = actual_price if actual_price is not None else row["sell_price"]
    if price is None:
        return None
    name_row = conn.execute("SELECT name FROM items WHERE id=?", (row["item_id"],)).fetchone()
    item_name = name_row["name"] if name_row else None
    qty = row["quantity"]
    tax = sale_tax(price, item_name) * qty
    profit = flip_margin(row["buy_price"], price, item_name) * qty
    conn.execute(
        "UPDATE active_flips SET status='done', sell_price=?, sold_ts=?, tax=?, profit=? WHERE id=?",
        (price, now, tax, profit, flip_id),
    )
    conn.execute(
        "INSERT INTO observed_fills (ts, item_id, type, offer_price, quantity, spent, display_name) "
        "VALUES (?,?,?,?,?,?,?)",
        (now, row["item_id"], "sell", price, qty, price * qty - tax, display_name),
    )
    conn.commit()
    return {"profit": profit, "tax": tax}


def delete_flip(conn, flip_id: int) -> bool:
    cur = conn.execute("DELETE FROM active_flips WHERE id=?", (flip_id,))
    conn.commit()
    return cur.rowcount > 0


def list_flips(conn, include_done: int = 10) -> dict:
    """Active flips with live prices merged, plus recent completed and session stats."""
    active = conn.execute("""
        SELECT f.*, i.name, l.high AS live_high, l.low AS live_low
        FROM active_flips f
        LEFT JOIN items i ON i.id = f.item_id
        LEFT JOIN (
            SELECT item_id, high, low FROM latest_snapshots
            WHERE fetched_at = (SELECT MAX(fetched_at) FROM latest_snapshots)
        ) l ON l.item_id = f.item_id
        WHERE f.status != 'done'
        ORDER BY f.created_ts
    """).fetchall()

    out_active = []
    for r in active:
        d = dict(r)
        live_high = r["live_high"]
        # what selling right now would net, per the live market
        if live_high:
            d["suggested_sell"] = live_high - 1
            d["profit_at_suggested"] = flip_margin(
                r["buy_price"], live_high - 1, r["name"]) * r["quantity"]
        else:
            d["suggested_sell"] = None
            d["profit_at_suggested"] = None
        out_active.append(d)

    done = conn.execute(
        "SELECT f.*, i.name FROM active_flips f LEFT JOIN items i ON i.id=f.item_id "
        "WHERE f.status='done' ORDER BY f.sold_ts DESC LIMIT ?", (include_done,)
    ).fetchall()

    day_ago = int(time.time()) - 24 * 3600
    stats = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(profit),0) AS profit, COALESCE(SUM(tax),0) AS tax "
        "FROM active_flips WHERE status='done' AND sold_ts > ?", (day_ago,)
    ).fetchone()

    return {
        "active": out_active,
        "recent_done": [dict(r) for r in done],
        "session": {"completed_24h": stats["n"], "profit_24h": stats["profit"],
                    "tax_24h": stats["tax"]},
    }


def search_items(conn, query: str, limit: int = 10) -> list[dict]:
    rows = conn.execute(
        "SELECT id, name, buy_limit FROM items WHERE name LIKE ? ORDER BY LENGTH(name) LIMIT ?",
        (f"%{query}%", limit),
    ).fetchall()
    return [dict(r) for r in rows]
