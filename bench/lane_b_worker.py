#!/usr/bin/env python3
"""Lane B1 worker — one op, in a FRESH process, the way a Python workflow does it.

Loads the .arrow with memory_map + to_pandas and computes the op in pandas.
This is deliberately the naive-but-normal shape: the whole table is
materialized, because that is what `to_pandas()` does and what a typical
notebook/script actually pays. meshroad's projection pushdown means lane A
touches only the columns an op needs; that difference is a REAL mechanism and
is reported as such in the findings, not hidden.

Emits JSON so the orchestrator never has to parse human text. Load and compute
are timed separately so the two costs can be attributed independently, and the
result value is returned so cross-lane agreement can be asserted rather than
assumed.
"""
from __future__ import annotations

import argparse
import json
import time


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--op", required=True)
    args = ap.parse_args()

    import pyarrow as pa
    import pyarrow.ipc as ipc

    t0 = time.perf_counter()
    table = ipc.open_file(pa.memory_map(args.file)).read_all()
    df = table.to_pandas()
    load_s = time.perf_counter() - t0

    t1 = time.perf_counter()
    result = compute(df, args.op)
    compute_s = time.perf_counter() - t1

    print(json.dumps({"load_s": load_s, "compute_s": compute_s, "result": result}))


def compute(df, op: str):
    if op == "SUM(amount)":
        return float(df["amount"].sum())
    if op == "GROUPSUM(region,amount)":
        g = df.groupby("region", observed=True)["amount"].sum()
        return int(len(g))
    if op == "FILTER status=active":
        return int((df["status"] == "active").sum())
    if op == "DISTINCT product_name":
        return int(df["product_name"].nunique())
    if op == "MAX(account_id)":
        return int(df["account_id"].max())
    if op == "UPPER(status) group":
        v = df["status"].astype("str").str.upper().value_counts()
        return int(len(v))
    raise SystemExit(f"unknown op: {op}")


if __name__ == "__main__":
    main()
