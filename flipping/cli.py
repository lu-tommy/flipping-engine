"""CLI: `python -m flipping.cli ingest` and `python -m flipping.cli top`."""

import argparse

from . import db, engine, ingest


def parse_gp(s: str) -> int:
    """Accept 10m / 500k / 2b / plain integers."""
    s = s.lower().replace(",", "")
    mult = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}
    if s[-1] in mult:
        return int(float(s[:-1]) * mult[s[-1]])
    return int(s)


def fmt_gp(n: int | float) -> str:
    n = int(n)
    if abs(n) >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}b"
    if abs(n) >= 1_000_000:
        return f"{n / 1_000_000:.2f}m"
    if abs(n) >= 10_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def cmd_ingest(args) -> None:
    conn = db.connect(args.db)
    stats = ingest.run(conn)
    print(f"ingested: {stats}")


def cmd_top(args) -> None:
    conn = db.connect(args.db)
    flips, rejected = engine.stable_flips(conn, cash=parse_gp(args.cash),
                                          f2p_only=args.f2p,
                                          min_profit=parse_gp(args.min_profit))
    flips = flips[: args.limit]
    if not flips:
        print("No candidates. Run `ingest` first or relax filters.")
        return

    header = (f"{'ITEM':<32} {'BUY':>10} {'SELL':>10} {'MARGIN':>8} "
              f"{'QTY':>7} {'PROFIT':>9} {'RT(min)':>8} {'GP/HR':>9} {'VOL(b/s)':>13}")
    print(header)
    print("-" * len(header))
    for c in flips:
        print(f"{c.name[:31]:<32} {fmt_gp(c.buy_price):>10} {fmt_gp(c.sell_price):>10} "
              f"{fmt_gp(c.margin):>8} {c.quantity:>7} {fmt_gp(c.est_profit):>9} "
              f"{c.roundtrip_minutes:>8.1f} {fmt_gp(c.gp_per_hour):>9} "
              f"{c.hourly_buy_side_vol:>6}/{c.hourly_sell_side_vol:<6}")

    if args.show_rejected and rejected:
        print(f"\nRejected by stability check ({len(rejected)}):")
        for c in rejected:
            print(f"  {c.name:<32} {fmt_gp(c.gp_per_hour):>9} gp/hr  -- {c.stability.reason}")


def main() -> None:
    p = argparse.ArgumentParser(prog="flipping", description="OSRS flip suggestion engine")
    p.add_argument("--db", default=str(db.DEFAULT_DB_PATH), help="SQLite db path")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("ingest", help="fetch current wiki price data into the db")

    top = sub.add_parser("top", help="show ranked flip candidates")
    top.add_argument("--cash", default="10m", help="cash stack, e.g. 10m, 500k")
    top.add_argument("--limit", type=int, default=20)
    top.add_argument("--f2p", action="store_true", help="free-to-play items only")
    top.add_argument("--min-profit", default="0", help="minimum estimated total profit")
    top.add_argument("--show-rejected", action="store_true",
                     help="also list candidates rejected by the stability check")

    args = p.parse_args()
    {"ingest": cmd_ingest, "top": cmd_top}[args.command](args)


if __name__ == "__main__":
    main()
