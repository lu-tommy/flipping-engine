"""Fetch current data from the wiki API into SQLite. Safe to run repeatedly."""

import time

from . import db, wiki_api


def run(conn) -> dict:
    stats = {}

    stats["items"] = db.upsert_items(conn, wiki_api.fetch_mapping())

    fetched_at = int(time.time())
    stats["latest"] = db.insert_latest(conn, fetched_at, wiki_api.fetch_latest())

    data_5m, ts_5m = wiki_api.fetch_5m()
    stats["5m"] = db.insert_bucket(conn, "bucket_5m", ts_5m, data_5m)

    data_1h, ts_1h = wiki_api.fetch_1h()
    stats["1h"] = db.insert_bucket(conn, "bucket_1h", ts_1h, data_1h)

    return stats


if __name__ == "__main__":
    conn = db.connect()
    print(run(conn))
