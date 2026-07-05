# flipping-engine

OSRS Grand Exchange flip suggestion engine — a recreation of the closed-source
backend behind Flipping Copilot. See `~/flipping-copilot-study.md` for the full
architectural study of the original.

## Quick start

```sh
.venv/bin/python -m flipping.cli ingest              # fetch current wiki prices
.venv/bin/python -m flipping.cli top --cash 10m      # ranked flip candidates
.venv/bin/python -m flipping.cli top --f2p --cash 2m --min-profit 50k
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
2. ~~Timeseries stability scoring~~ (done) — remaining: backtest harness over
   accumulated snapshots; tune `CAPTURE_FRACTION` against observed fills
3. Suggestion API service: full account state in → single BUY/SELL/ABORT/WAIT
   action out (protocol modeled on the Flipping Copilot client's protobuf)
4. RuneLite plugin integration

## Tests

```sh
.venv/bin/python -m unittest discover -s tests
```
