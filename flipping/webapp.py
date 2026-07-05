"""Web dashboard. Run: .venv/bin/python -m flipping.webapp  (http://127.0.0.1:8787)

This service is also the seed of milestone 3: /api/flips will grow into the
full account-state -> single-action suggestion endpoint.
"""

import json
from dataclasses import asdict
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from . import backtest, db, engine, portfolio, tracker
from .cli import parse_gp
from .suggestion import AccountOffer, AccountState, HeldItem, suggest

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="flipping-engine")


def _conn():
    # SQLite connections are cheap; one per request avoids cross-thread issues.
    return db.connect()


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/flips")
def api_flips(cash: str = "10m", f2p: bool = False, min_profit: str = "0",
              max_rt: float | None = Query(None, description="max roundtrip minutes"),
              limit: int = Query(20, le=100)):
    conn = _conn()
    try:
        passing, rejected = engine.stable_flips(
            conn, cash=parse_gp(cash), f2p_only=f2p, min_profit=parse_gp(min_profit),
            max_roundtrip_minutes=max_rt)
    finally:
        conn.close()
    freshness = None
    conn = _conn()
    try:
        row = conn.execute("SELECT MAX(fetched_at) AS t FROM latest_snapshots").fetchone()
        freshness = row["t"]
    finally:
        conn.close()
    return {
        "data_fetched_at": freshness,
        "passing": [asdict(c) for c in passing[:limit]],
        "rejected": [asdict(c) for c in rejected],
    }


@app.post("/api/backtest/run")
def api_backtest_run(cash: str = "10m", top: int = Query(15, le=40),
                     capture: float = engine.CAPTURE_FRACTION):
    conn = _conn()
    try:
        return backtest.run(conn, cash=parse_gp(cash), top=top, capture=capture)
    finally:
        conn.close()


@app.get("/api/portfolio")
def api_portfolio(cash: str = "10m", slots: int = Query(8, le=8), f2p: bool = False,
                  max_rt: float | None = None, min_profit: str = "0"):
    """How to deploy the whole cash stack across GE slots right now."""
    return _with_conn(lambda c: portfolio.plan(
        c, cash=parse_gp(cash), slots=slots, f2p_only=f2p,
        max_roundtrip_minutes=max_rt, min_profit=parse_gp(min_profit)))


class OfferIn(BaseModel):
    slot: int
    type: str            # "buy" | "sell"
    item_id: int
    price: int
    quantity: int
    filled: int = 0


class HeldIn(BaseModel):
    item_id: int
    quantity: int


class SuggestionRequest(BaseModel):
    cash: int
    offers: list[OfferIn] = []
    inventory: list[HeldIn] = []
    f2p_only: bool = False
    min_profit: int = 0
    total_slots: int = 8
    max_roundtrip_minutes: float | None = None


@app.post("/api/suggestion")
def api_suggestion(req: SuggestionRequest):
    """The milestone-3 endpoint: full account state in, single action out."""
    state = AccountState(
        cash=req.cash,
        offers=[AccountOffer(**o.model_dump()) for o in req.offers],
        inventory=[HeldItem(**h.model_dump()) for h in req.inventory],
        f2p_only=req.f2p_only,
        min_profit=req.min_profit,
        total_slots=req.total_slots,
        max_roundtrip_minutes=req.max_roundtrip_minutes,
    )
    conn = _conn()
    try:
        return suggest(conn, state)
    finally:
        conn.close()


class FillIn(BaseModel):
    ts: int
    item_id: int
    type: str
    offer_price: int
    quantity: int
    spent: int
    display_name: str = ""


class FillsRequest(BaseModel):
    fills: list[FillIn]


@app.post("/api/fills")
def api_fills(req: FillsRequest):
    """Ground-truth fills observed in-game by the RuneLite plugin.

    These are what eventually replace the backtest's capture-fraction
    assumption with measured per-item fill behavior.
    """
    conn = _conn()
    try:
        conn.executemany(
            "INSERT INTO observed_fills (ts, item_id, type, offer_price, quantity, spent, display_name) "
            "VALUES (?,?,?,?,?,?,?)",
            [(f.ts, f.item_id, f.type, f.offer_price, f.quantity, f.spent, f.display_name)
             for f in req.fills],
        )
        conn.commit()
        return {"acked": len(req.fills)}
    finally:
        conn.close()


class NewFlip(BaseModel):
    item_id: int
    quantity: int
    buy_price: int
    sell_price: int | None = None


class FlipUpdate(BaseModel):
    buy_price: int | None = None
    sell_price: int | None = None
    quantity: int | None = None


class MarkBought(BaseModel):
    actual_price: int | None = None
    actual_quantity: int | None = None


class MarkSold(BaseModel):
    actual_price: int | None = None


def _with_conn(fn):
    conn = _conn()
    try:
        return fn(conn)
    finally:
        conn.close()


@app.get("/api/active-flips")
def api_active_flips():
    return _with_conn(tracker.list_flips)


@app.post("/api/active-flips")
def api_add_flip(req: NewFlip):
    flip_id = _with_conn(lambda c: tracker.add_flip(
        c, req.item_id, req.quantity, req.buy_price, req.sell_price))
    return {"id": flip_id}


@app.patch("/api/active-flips/{flip_id}")
def api_update_flip(flip_id: int, req: FlipUpdate):
    ok = _with_conn(lambda c: tracker.update_prices(
        c, flip_id, req.buy_price, req.sell_price, req.quantity))
    if not ok:
        return JSONResponse({"error": "flip not found or already done"}, status_code=404)
    return {"ok": True}


@app.post("/api/active-flips/{flip_id}/bought")
def api_mark_bought(flip_id: int, req: MarkBought):
    ok = _with_conn(lambda c: tracker.mark_bought(
        c, flip_id, req.actual_price, req.actual_quantity))
    if not ok:
        return JSONResponse({"error": "flip not found or not in buying state"}, status_code=404)
    return {"ok": True}


@app.post("/api/active-flips/{flip_id}/sold")
def api_mark_sold(flip_id: int, req: MarkSold):
    result = _with_conn(lambda c: tracker.mark_sold(c, flip_id, req.actual_price))
    if result is None:
        return JSONResponse(
            {"error": "flip not found, not in selling state, or no sell price given"},
            status_code=404)
    return result


@app.delete("/api/active-flips/{flip_id}")
def api_delete_flip(flip_id: int):
    ok = _with_conn(lambda c: tracker.delete_flip(c, flip_id))
    return {"ok": ok}


@app.get("/api/items/search")
def api_item_search(q: str, limit: int = Query(10, le=25)):
    if len(q) < 2:
        return []
    return _with_conn(lambda c: tracker.search_items(c, q, limit))


@app.get("/api/backtest/latest")
def api_backtest_latest():
    if not backtest.REPORT_PATH.exists():
        return JSONResponse({"error": "no backtest has been run yet"}, status_code=404)
    return json.loads(backtest.REPORT_PATH.read_text())


def main():
    uvicorn.run(app, host="127.0.0.1", port=8787)


if __name__ == "__main__":
    main()
