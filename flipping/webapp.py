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

from . import backtest, db, engine
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
              limit: int = Query(20, le=100)):
    conn = _conn()
    try:
        passing, rejected = engine.stable_flips(
            conn, cash=parse_gp(cash), f2p_only=f2p, min_profit=parse_gp(min_profit))
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


@app.get("/api/backtest/latest")
def api_backtest_latest():
    if not backtest.REPORT_PATH.exists():
        return JSONResponse({"error": "no backtest has been run yet"}, status_code=404)
    return json.loads(backtest.REPORT_PATH.read_text())


def main():
    uvicorn.run(app, host="127.0.0.1", port=8787)


if __name__ == "__main__":
    main()
