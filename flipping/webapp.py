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

from . import backtest, db, engine
from .cli import parse_gp

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


@app.get("/api/backtest/latest")
def api_backtest_latest():
    if not backtest.REPORT_PATH.exists():
        return JSONResponse({"error": "no backtest has been run yet"}, status_code=404)
    return json.loads(backtest.REPORT_PATH.read_text())


def main():
    uvicorn.run(app, host="127.0.0.1", port=8787)


if __name__ == "__main__":
    main()
