"""Portfolio slot planner: deploy the whole cash stack across GE slots.

Single-flip ranking answers "what's the best item"; a flipper with 8 slots
needs "how do I deploy 10m across 8 slots right now". Greedy allocation:
walk the ranked list, give each slot an equal share of the remaining cash,
size the order to that budget, and roll unspent cash into later slots.
"""

from dataclasses import asdict

from .engine import stable_flips


def plan(conn, cash: int, slots: int = 8, f2p_only: bool = False,
         max_roundtrip_minutes: float | None = None, min_profit: int = 0) -> dict:
    ranked, _ = stable_flips(conn, cash=cash, f2p_only=f2p_only,
                             min_profit=min_profit,
                             max_roundtrip_minutes=max_roundtrip_minutes)
    allocations = []
    remaining = cash
    slots_left = slots

    for c in ranked:
        if slots_left == 0 or remaining <= 0:
            break
        budget = remaining // slots_left
        qty = min(c.quantity, budget // c.buy_price)
        if qty < 1:
            continue
        cost = qty * c.buy_price
        est_profit = c.margin * qty
        alloc = asdict(c)
        alloc.update({
            "quantity": qty,
            "cost": cost,
            "est_profit": est_profit,
            # roundtrip scales roughly linearly with order size
            "roundtrip_minutes": max(5.0, c.roundtrip_minutes * qty / max(c.quantity, 1)),
        })
        allocations.append(alloc)
        remaining -= cost
        slots_left -= 1

    total_cost = sum(a["cost"] for a in allocations)
    # slots run concurrently: projected rate is the sum of per-slot rates
    projected = sum(a["est_profit"] / (a["roundtrip_minutes"] / 60)
                    for a in allocations if a["roundtrip_minutes"] > 0)
    return {
        "slots_used": len(allocations),
        "slots_total": slots,
        "total_cost": total_cost,
        "cash_left": cash - total_cost,
        "projected_gp_per_hour": int(projected),
        "allocations": allocations,
    }
