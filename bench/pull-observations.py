#!/usr/bin/env python3
"""Instrumented ClickHouse -> ArrowIpcSink pull. Emits OBSERVATIONS, not claims.

Decomposes wall time into the three stages that actually consume it -- the
ClickHouse query, the coercion layer, and the sink write -- so that a slow pull
is attributed to the right component instead of being reported as an undivided
"ingest cost".

Instrumentation WRAPS THE REAL FUNCTIONS on the real daemon path rather than
reimplementing the pull with timers around it. A reimplementation measures the
harness, not the pipeline.

The machine is NOT quiesced for these numbers and no quiet-machine gate is
applied, so everything here is an observation for context. It is not a
benchmark lane and must not be quoted as one.

Usage:
    .venv/bin/python bench/pull-observations.py --source meshbench.perf_1m \
        --target perf --outdir ~/bench-ch/out
"""
from __future__ import annotations

import argparse
import asyncio
import resource
import time
from pathlib import Path

from r64_db_engine.core.config import Config
from r64_db_engine.core.daemon import build_daemon

TIMINGS: dict[str, float] = {"ch_query": 0.0, "coercion": 0.0, "sink_write": 0.0}


def _instrument() -> None:
    """Wrap the real call sites, accumulating elapsed time per stage."""
    from r64_db_engine.drivers.clickhouse import driver as chdrv
    from r64_db_engine.sinks.arrow_ipc import ArrowIpcSink

    real_query_df = chdrv.ClickHouseDriver._query_df
    real_pre_coerce = chdrv._pre_coerce_values
    real_apply = chdrv.apply_coercion
    real_write = ArrowIpcSink.write

    async def timed_query_df(self, sql, params=None):
        t0 = time.perf_counter()
        try:
            return await real_query_df(self, sql, params)
        finally:
            TIMINGS["ch_query"] += time.perf_counter() - t0

    def timed_pre_coerce(df, col_types):
        t0 = time.perf_counter()
        try:
            return real_pre_coerce(df, col_types)
        finally:
            TIMINGS["coercion"] += time.perf_counter() - t0

    def timed_apply(df, column_dtypes, ascii_sanitize=True):
        t0 = time.perf_counter()
        try:
            return real_apply(df, column_dtypes, ascii_sanitize)
        finally:
            TIMINGS["coercion"] += time.perf_counter() - t0

    def timed_write(self, df, target):
        t0 = time.perf_counter()
        try:
            return real_write(self, df, target)
        finally:
            TIMINGS["sink_write"] += time.perf_counter() - t0

    chdrv.ClickHouseDriver._query_df = timed_query_df
    chdrv._pre_coerce_values = timed_pre_coerce
    chdrv.apply_coercion = timed_apply
    ArrowIpcSink.write = timed_write


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--target", required=True)
    ap.add_argument("--outdir", required=True)
    args = ap.parse_args()

    out = Path(args.outdir).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    state = out / ".state" / args.target

    cfg = Config.model_validate({
        "dialect": "clickhouse",
        "clickhouse": {
            "host": "127.0.0.1", "port": 8123,
            "database": "meshbench", "user": "default", "secure": False,
        },
        "row64": {"loading_dir": str(out), "group": "CH"},
        "sink": {
            "type": "arrow_ipc",
            "output_dir": str(out),
            "dictionary_columns": {args.target: ["status"]},
            "timestamp_unit": "us",
        },
        "tables": [{
            "source": args.source, "target": args.target,
            "mode": "full_refresh", "cadence": "5s",
        }],
        "runtime": {"state_dir": str(state)},
        "telemetry": {"health_port": 0, "metrics_port": 0},
    })

    _instrument()
    daemon = build_daemon(cfg)

    t0 = time.perf_counter()
    asyncio.run(daemon.run(once=True))
    wall = time.perf_counter() - t0

    path = daemon.writer.target_path(args.target)
    size = path.stat().st_size
    # ru_maxrss is in KILOBYTES on Linux (bytes on macOS). One /1024 gets MiB.
    peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024

    accounted = sum(TIMINGS.values())
    print(f"\n=== {args.source} -> {path}")
    print(f"{'stage':16s} {'seconds':>9s} {'% wall':>8s}")
    for stage in ("ch_query", "coercion", "sink_write"):
        print(f"{stage:16s} {TIMINGS[stage]:9.2f} {TIMINGS[stage]/wall*100:7.1f}%")
    print(f"{'accounted':16s} {accounted:9.2f} {accounted/wall*100:7.1f}%")
    print(f"{'other':16s} {wall-accounted:9.2f} {(wall-accounted)/wall*100:7.1f}%")
    print(f"{'WALL':16s} {wall:9.2f} {100.0:7.1f}%")
    print(f"\noutput size      {size/1024/1024:9.1f} MiB ({size} bytes)")
    print(f"peak RSS (self)  {peak_rss:9.1f} MiB")


if __name__ == "__main__":
    main()
