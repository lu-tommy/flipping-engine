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
