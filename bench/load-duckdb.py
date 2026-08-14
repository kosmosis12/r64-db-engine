#!/usr/bin/env python
"""Load the meshbench Parquet exports into a local .duckdb, and VERIFY the load.

The dataset is not regenerated — it is the same meshbench rows the ClickHouse
campaign measured, moved across so the two lanes can be compared on identical
data. `bench/GROUND-TRUTH-clickhouse.json` therefore carries over verbatim and
is never patched: if a number disagrees, the load is wrong, not the truth.

Export step (run first, against the live container):

    docker exec meshroad-ch clickhouse-client --query \
      "SELECT * FROM meshbench.perf_1m ORDER BY row_id FORMAT Parquet" \
      > ~/bench-ch/perf_1m.parquet

# Two type divergences the export introduces, both handled here

1. `status` is `LowCardinality(String)` in ClickHouse and lands as plain
   `VARCHAR`. Parquet has no LowCardinality, so the dictionary encoding is a
   property of the ARTIFACT, not of the source, and is re-applied at the sink
   via `dictionary_columns`. Nothing to fix at load.

2. `event_time` is `DateTime64(6)` and lands as `TIMESTAMP WITH TIME ZONE`,
   because ClickHouse marks it adjusted-to-UTC in the Parquet metadata.
   **DuckDB's default session TimeZone is the machine's local zone**, so a
   naive `CAST(... AS TIMESTAMP)` silently shifts every value by the local UTC
   offset — 8 hours on this box. Every ground-truth check would still pass,
   because none of them touch `event_time`. `SET TimeZone='UTC'` before the
   cast is what makes the load correct, and the min/max assertion below is what
   proves it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import duckdb

BENCH = Path(__file__).resolve().parent
GROUND_TRUTH = BENCH / "GROUND-TRUTH-clickhouse.json"

# Captured from the live ClickHouse container at load time, formatted as UTC.
# These are the only expectations not already in the ground-truth file, because
# the ClickHouse campaign never needed a timestamp bound.
EVENT_TIME_BOUNDS = {
    "perf_1m": ("2026-01-01 00:00:15.184566", "2026-06-29 23:59:30.942340"),
}

CHECKS = (
    ("count", "count(*)", "count"),
    ("scaled_amount_sum_exact", "sum(CAST(round(amount*100) AS BIGINT))",
     "scaled_amount_sum_exact_int"),
    ("sum_quantity", "sum(quantity)", "sum_quantity"),
    ("count_score_null", "count(*) FILTER (WHERE score IS NULL)", "count_score_null"),
    ("uniq_status", "count(DISTINCT status)", "uniq_status"),
    ("uniq_region", "count(DISTINCT region)", "uniq_region"),
    ("uniq_product_name", "count(DISTINCT product_name)", "uniq_product_name"),
    ("max_account_id", "max(account_id)", "max_account_id"),
    ("count_status_active", "count(*) FILTER (WHERE status='active')",
     "count_status_active"),
    ("count_region_west", "count(*) FILTER (WHERE region='West')", "count_region_west"),
)


def load(con: duckdb.DuckDBPyConnection, table: str, parquet: Path) -> None:
    con.execute(f"DROP TABLE IF EXISTS {table}")
    con.execute(
        f"""
        CREATE TABLE {table} AS
        SELECT * REPLACE (CAST(event_time AS TIMESTAMP) AS event_time)
        FROM read_parquet('{parquet}')
        ORDER BY row_id
        """
    )


def verify(con: duckdb.DuckDBPyConnection, table: str) -> bool:
    truth = json.loads(GROUND_TRUTH.read_text())["tables"][table]
    projection = ", ".join(expr for _, expr, _ in CHECKS)
    row = con.execute(f"SELECT {projection} FROM {table}").fetchone()
    assert row is not None

    ok = True
    print(f"\n=== {table}: transfer verification vs GROUND-TRUTH-clickhouse.json ===")
    for (name, _, truth_key), got in zip(CHECKS, row, strict=True):
        want = truth[truth_key]
        match = got == want
        ok &= match
        print(f"  {'OK  ' if match else 'FAIL'} {name:24s} duckdb={got:<14} truth={want}")

    if table in EVENT_TIME_BOUNDS:
        bounds = con.execute(
            f"SELECT strftime(min(event_time), '%Y-%m-%d %H:%M:%S.%f'), "
            f"strftime(max(event_time), '%Y-%m-%d %H:%M:%S.%f') FROM {table}"
        ).fetchone()
        want_bounds = EVENT_TIME_BOUNDS[table]
        match = tuple(bounds or ()) == want_bounds
        ok &= match
        print(f"  {'OK  ' if match else 'FAIL'} {'event_time_bounds':24s} "
              f"duckdb={bounds} truth={want_bounds}")

    # Row order is what makes the artifact byte-reproducible; assert it directly
    # rather than trusting the CREATE TABLE ... ORDER BY to have stuck.
    ordered = con.execute(
        f"SELECT count(*) FROM (SELECT row_id, lag(row_id) OVER () AS prev FROM {table}) "
        f"WHERE prev IS NOT NULL AND row_id <= prev"
    ).fetchone()
    assert ordered is not None
    match = ordered[0] == 0
    ok &= match
    print(f"  {'OK  ' if match else 'FAIL'} {'row_id_monotonic':24s} violations={ordered[0]}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(Path.home() / "bench-ch" / "meshbench.duckdb"))
    ap.add_argument("--parquet-dir", default=str(Path.home() / "bench-ch"))
    ap.add_argument("--tables", nargs="*", default=["perf_1m", "perf_10m"])
    args = ap.parse_args()

    con = duckdb.connect(args.db)
    # Load-time correctness, not cosmetics — see the module docstring.
    con.execute("SET TimeZone='UTC'")

    ok = True
    for table in args.tables:
        parquet = Path(args.parquet_dir) / f"{table}.parquet"
        if not parquet.exists():
            print(f"SKIP {table}: {parquet} not found")
            continue
        print(f"loading {table} from {parquet} ({parquet.stat().st_size:,} bytes)")
        load(con, table, parquet)
        ok &= verify(con, table)

    con.close()
    print("\nALL CHECKS PASS:", ok)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
