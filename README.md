# flipping-engine

OSRS Grand Exchange flip suggestion engine — a recreation of the closed-source
backend behind Flipping Copilot. See `~/flipping-copilot-study.md` for the full
architectural study of the original.

## Quick start

```sh
.venv/bin/python -m flipping.cli ingest              # fetch current wiki prices
.venv/bin/python -m flipping.cli top --cash 10m      # ranked flip candidates
.venv/bin/python -m flipping.cli top --f2p --cash 2m --min-profit 50k
.venv/bin/python -m flipping.cli backtest --cash 10m # replay flips vs history
.venv/bin/python -m flipping.webapp                  # dashboard on :8787
```

Run `ingest` on a cron (every 1–5 min) to accumulate history; `top` only reads
the local db.

## Design

- `flipping/wiki_api.py` — OSRS Wiki real-time prices client. Bulk endpoints
  only, descriptive User-Agent (both required by their usage policy).
- `flipping/db.py` — append-only SQLite snapshots (`prices.db`), so history
  accumulates for future backtesting/volatility work.
- `flipping/ge_tax.py` — GE tax math (2% floor-rounded, 5M cap, <50 gp free,
  exemption list). Rules verified 2026-07-05; last changed 29 May 2025.
- `flipping/engine.py` — heuristic ranker v1. Models buying at `low+1`,
  selling at `high-1`; ranks by estimated GP/hr with guards for stale quotes,
  one-sided markets, and too-good-to-be-true margins. Tunable constants are
  documented at the top of the file.
- `flipping/stability.py` — timeseries z-score check on top candidates:
  rejects flips whose entry price chases a spike or whose exit price implies
  a crash in progress. Per-item timeseries fetches are cached in SQLite.

Ingestion runs every 5 minutes via launchd
(`~/Library/LaunchAgents/com.tommylu.flipping-ingest.plist`, logs in `logs/`).

## Roadmap

1. ~~Ingestion + heuristic ranker v1~~ (done)
2. ~~Timeseries stability scoring + backtest harness~~ (done) — the first
   backtest showed real roundtrips running ~3.6x slower than predicted, so
   `CAPTURE_FRACTION` was cut 0.10 -> 0.03 and the median actual/predicted
   ratio came out at 1.0.

   **That calibration was later found to be wrong, and is no longer in effect.**
   It conflated two separate effects: how much volume an offer captures once
   price reaches it, and how long price takes to get there at all. Most of the
   measured delay was the second. v3 models the waiting explicitly via touch
   fractions, so capture is back at **0.10** — see the comment above
   `CAPTURE_FRACTION` in `flipping/engine.py`, which is authoritative. Real
   fills logged through the tracker are the intended calibrator; none have been
   recorded yet.
3. ~~Suggestion API service~~ (done) — `POST /api/suggestion`: full account
   state (cash, open offers, held inventory) in → single
   abort/sell/buy/wait action out. Ranker uses backtest-measured per-item
   fill rates, with the overall backtest fill rate as prior for unmeasured
   items (prevents adverse selection toward untested items).
4. RuneLite plugin integration (talk to /api/suggestion; use the original
   flipping-copilot plugin source as reference for widget IDs / GE events)

## Web dashboard

`flipping/webapp.py` (FastAPI) + `flipping/static/index.html`: sortable
candidate table with cash/F2P/min-profit filters, stability rejections with
reasons, one-click backtest, 60s auto-refresh. Item names link to the wiki
price page.

## Tests

```sh
.venv/bin/python -m unittest discover -s tests
```

## Retention

The snapshot log is append-only. It reached **73M rows and 6.3 GB in two
months**, which is what let a database corruption sit unnoticed for weeks —
nobody casually scans a table that size.

The retained windows are set by what the code actually reads, which turned out
to be far less than what was being kept:

| Table | Kept | What reads it |
|---|---|---|
| `latest_snapshots` | 2 days | every read is `WHERE fetched_at = (SELECT MAX(...))` — 73M rows existed to serve one |
| `bucket_5m` | 30 days | `ORDER BY bucket_ts DESC LIMIT 6` — thirty minutes |
| `bucket_1h` | everything | the trailing series the ranker reasons over, and small |

```sh
python -m flipping.cli prune --dry-run   # report only
python -m flipping.cli prune --vacuum    # delete and reclaim
```

Runs weekly via `com.tommylu.flipping-prune`. First run took the database from
**6.3 GB to 893 MB**.

## A note on the calibration figure

The backtest's `median_actual_vs_predicted_roundtrip` was measured at ~1.03 on
the pre-recovery database and sits at **0.57–0.67 across four runs on the
repaired one**. The difference is not noise: `engine.py` selects candidates
partly from `bucket_5m`, that table was the corrupted one, and a different
candidate set produces a different measurement.

So the current honest reading is that **round trips complete roughly a third
faster than the ranker predicts**, and `CAPTURE_FRACTION` is due another look on
clean data. The earlier 1.03 should not be quoted.
