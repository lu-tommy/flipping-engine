"""Account-state -> single-action suggestion (milestone 3).

Mirrors Flipping Copilot's decision structure: given everything about the
account (cash, open GE offers, held items), return exactly one next action.

Priority order:
  1. ABORT any open offer that has gone off-market (won't fill at its price)
  2. SELL held inventory (capital tied up in items earns nothing)
  3. BUY the top-ranked stable candidate if a slot and cash are free
  4. WAIT
"""

from dataclasses import dataclass, field

from .engine import stable_flips


@dataclass
class AccountOffer:
    slot: int
    type: str          # "buy" | "sell"
    item_id: int
    price: int
    quantity: int
    filled: int = 0


@dataclass
class HeldItem:
    item_id: int
    quantity: int


@dataclass
class AccountState:
    cash: int
    offers: list[AccountOffer] = field(default_factory=list)
    inventory: list[HeldItem] = field(default_factory=list)
    f2p_only: bool = False
    min_profit: int = 0
    total_slots: int = 8   # 3 for F2P


def _latest_prices(conn, item_ids):
    if not item_ids:
        return {}
    qmarks = ",".join("?" * len(item_ids))
    rows = conn.execute(f"""
        SELECT item_id, high, low FROM latest_snapshots
        WHERE fetched_at = (SELECT MAX(fetched_at) FROM latest_snapshots)
          AND item_id IN ({qmarks})
    """, list(item_ids)).fetchall()
    return {r["item_id"]: (r["high"], r["low"]) for r in rows}


def _item_names(conn, item_ids):
    if not item_ids:
        return {}
    qmarks = ",".join("?" * len(item_ids))
    rows = conn.execute(f"SELECT id, name FROM items WHERE id IN ({qmarks})",
                        list(item_ids)).fetchall()
    return {r["id"]: r["name"] for r in rows}


def suggest(conn, state: AccountState) -> dict:
    involved = {o.item_id for o in state.offers} | {h.item_id for h in state.inventory}
    prices = _latest_prices(conn, involved)
    names = _item_names(conn, involved)

    # 1. off-market offers first: dead offers block slots and tie up capital
    for o in state.offers:
        p = prices.get(o.item_id)
        if p is None:
            continue
        high, low = p
        if o.type == "buy" and low is not None and low > o.price:
            return {
                "type": "abort", "slot": o.slot, "item_id": o.item_id,
                "name": names.get(o.item_id, str(o.item_id)),
                "message": f"Buy offer outbid: market low is now {low}, your bid is {o.price}",
            }
        if o.type == "sell" and high is not None and high < o.price:
            return {
                "type": "abort", "slot": o.slot, "item_id": o.item_id,
                "name": names.get(o.item_id, str(o.item_id)),
                "message": f"Sell offer overpriced: market high is now {high}, your ask is {o.price}",
            }

    # 2. sell held items, most valuable first
    sellable = [(h, prices.get(h.item_id)) for h in state.inventory]
    sellable = [(h, p) for h, p in sellable if p and p[0]]
    if sellable:
        h, (high, _) = max(sellable, key=lambda t: t[0].quantity * t[1][0])
        return {
            "type": "sell", "item_id": h.item_id,
            "name": names.get(h.item_id, str(h.item_id)),
            "price": high - 1, "quantity": h.quantity,
            "message": f"Sell {h.quantity} x {names.get(h.item_id, h.item_id)} at {high - 1}",
        }

    # 3. open a new position if there's room
    if len(state.offers) < state.total_slots and state.cash > 0:
        skip = {o.item_id for o in state.offers} | {h.item_id for h in state.inventory}
        passing, _ = stable_flips(conn, cash=state.cash, f2p_only=state.f2p_only,
                                  min_profit=state.min_profit)
        for c in passing:
            if c.item_id in skip:
                continue
            return {
                "type": "buy", "item_id": c.item_id, "name": c.name,
                "price": c.buy_price, "quantity": c.quantity,
                "expected_profit": c.est_profit,
                "expected_duration_minutes": c.roundtrip_minutes,
                "fill_rate": c.fill_rate,
                "message": f"Buy {c.quantity} x {c.name} at {c.buy_price}",
            }

    # 4. nothing to do
    return {"type": "wait", "message": "All slots working and offers on-market"}
