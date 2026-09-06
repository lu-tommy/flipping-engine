"""SQLite storage for price data.

Snapshots are append-only so history accumulates for later
backtesting/volatility work; the engine reads only the newest rows.
"""

import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "prices.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    members INTEGER NOT NULL DEFAULT 0,
    buy_limit INTEGER,          -- GE buy limit per 4h; NULL if unknown
    value INTEGER,              -- base item value
    icon TEXT
);

CREATE TABLE IF NOT EXISTS latest_snapshots (
    fetched_at INTEGER NOT NULL,   -- unix time of our fetch
    item_id INTEGER NOT NULL,
    high INTEGER,                  -- last insta-buy price
    high_time INTEGER,
    low INTEGER,                   -- last insta-sell price
    low_time INTEGER,
    PRIMARY KEY (fetched_at, item_id)
);

CREATE TABLE IF NOT EXISTS bucket_5m (
    bucket_ts INTEGER NOT NULL,    -- bucket start time from the API
    item_id INTEGER NOT NULL,
    avg_high INTEGER,
    high_vol INTEGER NOT NULL DEFAULT 0,
    avg_low INTEGER,
    low_vol INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (bucket_ts, item_id)
);

CREATE TABLE IF NOT EXISTS bucket_1h (
    bucket_ts INTEGER NOT NULL,
    item_id INTEGER NOT NULL,
    avg_high INTEGER,
    high_vol INTEGER NOT NULL DEFAULT 0,
    avg_low INTEGER,
    low_vol INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (bucket_ts, item_id)
);

CREATE TABLE IF NOT EXISTS item_fill_stats (
    item_id INTEGER PRIMARY KEY,
    updated_at INTEGER NOT NULL,
    capture REAL NOT NULL,          -- capture assumption the stats were measured at
    simulations INTEGER NOT NULL,
    fill_rate REAL NOT NULL,        -- fraction of simulated flips that round-tripped
    median_roundtrip_min REAL       -- of completed flips; NULL if none completed
);

CREATE TABLE IF NOT EXISTS observed_fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,            -- when the fill was observed in-game
    item_id INTEGER NOT NULL,
    type TEXT NOT NULL,             -- buy | sell
    offer_price INTEGER NOT NULL,   -- price the offer was placed at
    quantity INTEGER NOT NULL,      -- items filled in this delta
    spent INTEGER NOT NULL,         -- gp actually moved (GE can improve price)
    display_name TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_fills_item ON observed_fills (item_id, ts);

CREATE TABLE IF NOT EXISTS active_flips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL,
    quantity INTEGER NOT NULL,
    buy_price INTEGER NOT NULL,     -- target while buying; actual once bought
    sell_price INTEGER,             -- target/actual sell price
    status TEXT NOT NULL DEFAULT 'buying',  -- buying | selling | done
    created_ts INTEGER NOT NULL,
    bought_ts INTEGER,
    sold_ts INTEGER,
    tax INTEGER,                    -- per completed flip, total gp
    profit INTEGER                  -- post-tax, total gp
);

CREATE INDEX IF NOT EXISTS idx_active_flips_status ON active_flips (status, created_ts);

CREATE INDEX IF NOT EXISTS idx_latest_item ON latest_snapshots (item_id, fetched_at);
CREATE INDEX IF NOT EXISTS idx_5m_item ON bucket_5m (item_id, bucket_ts);
CREATE INDEX IF NOT EXISTS idx_1h_item ON bucket_1h (item_id, bucket_ts);
"""


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def upsert_items(conn: sqlite3.Connection, mapping: list[dict]) -> int:
    rows = [
        (m["id"], m["name"], int(m.get("members", False)),
         m.get("limit"), m.get("value"), m.get("icon"))
        for m in mapping
    ]
    conn.executemany(
        "INSERT INTO items (id, name, members, buy_limit, value, icon) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET name=excluded.name, members=excluded.members, "
        "buy_limit=excluded.buy_limit, value=excluded.value, icon=excluded.icon",
        rows,
    )
    conn.commit()
    return len(rows)


def insert_latest(conn: sqlite3.Connection, fetched_at: int, data: dict[str, dict]) -> int:
    rows = [
        (fetched_at, int(item_id), d.get("high"), d.get("highTime"),
         d.get("low"), d.get("lowTime"))
        for item_id, d in data.items()
    ]
    conn.executemany(
        "INSERT OR IGNORE INTO latest_snapshots VALUES (?,?,?,?,?,?)", rows
    )
    conn.commit()
    return len(rows)


def insert_bucket(conn: sqlite3.Connection, table: str, bucket_ts: int,
                  data: dict[str, dict]) -> int:
    assert table in ("bucket_5m", "bucket_1h")
    rows = [
        (bucket_ts, int(item_id), d.get("avgHighPrice"), d.get("highPriceVolume", 0),
         d.get("avgLowPrice"), d.get("lowPriceVolume", 0))
        for item_id, d in data.items()
    ]
    conn.executemany(f"INSERT OR IGNORE INTO {table} VALUES (?,?,?,?,?,?)", rows)
    conn.commit()
    return len(rows)

# Retention. The snapshot log is append-only and unbounded: it reached 73M rows
# and 6.3GB in two months, which is what let a corruption go unnoticed for weeks
# -- nobody scans a table that size casually.
#
# The retained windows are set by what the code ACTUALLY reads, which is far
# less than what was kept:
#
#   latest_snapshots  every read is `WHERE fetched_at = (SELECT MAX(...))`.
#                     73 million rows existed to serve one. engine, suggestion,
#                     tracker and webapp all do this and nothing reads history.
#   bucket_5m         engine reads `ORDER BY bucket_ts DESC LIMIT 6` -- thirty
#                     minutes. A month is kept for headroom.
#   bucket_1h         the trailing series the ranker actually reasons over, and
#                     small (under 4M rows). Kept in full.
#
# Deliberately conservative against those numbers: days, not hours, so a future
# feature that wants a little history is not immediately blocked.
RETENTION_DAYS = {
    "latest_snapshots": 2,
    "bucket_5m": 30,
}


def prune(conn: sqlite3.Connection, days: dict[str, int] | None = None,
          vacuum: bool = False) -> dict[str, int]:
    """Delete rows past the retention window. Returns rows removed per table."""
    import time as _t
    policy = days or RETENTION_DAYS
    now = int(_t.time())
    removed: dict[str, int] = {}
    for table, keep_days in policy.items():
        column = "fetched_at" if table == "latest_snapshots" else "bucket_ts"
        cutoff = now - keep_days * 86400
        cur = conn.execute(f"DELETE FROM {table} WHERE {column} < ?", (cutoff,))
        removed[table] = cur.rowcount
    conn.commit()
    if vacuum:
        # VACUUM cannot run inside a transaction and rewrites the whole file,
        # so it is opt-in rather than part of every prune.
        conn.execute("VACUUM")
    return removed
