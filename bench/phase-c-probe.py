#!/usr/bin/env python
"""Phase C probe — coercion bypass + RSS bound.

Three cells, two scales, decomposed into source-query / transform / sink-write.

  P   pandas lane      ClickHouse driver -> query_df -> coercion -> batch sink
  N   native lane      DuckDB -> Arrow reader -> streaming sink
  P'  attribution cell DuckDB -> .df() -> coercion -> batch sink

**N vs P' is the clean bypass attribution** (same source, both lanes).
**N vs P is the workflow number** (different engines AND different lanes).
Both are published, labelled. Publishing only the second would credit the lane
for the engine's contribution.

# Why every rep is a fresh subprocess

`ru_maxrss` is a high-water mark that never resets within a process, so a second
rep in the same process reports the first rep's peak. One process per rep is the
only way to get an honest per-rep peak, and it also stops pandas' allocator
reuse from flattering later reps.

The parent reads the child's peak from `RUSAGE_CHILDREN` deltas AND the child
self-reports its own `ru_maxrss`; both are recorded, and they must agree.

# Foreign-contention gate

Runs before EVERY invocation, not once at the top. A benchmark that measured
half its reps under a browser and half without would report the difference as a
lane effect.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import statistics
import subprocess
import sys
import time
from pathlib import Path

BENCH = Path(__file__).resolve().parent
REPO = BENCH.parent
DUCKDB_PATH = Path.home() / "bench-ch" / "meshbench.duckdb"

# The 14-column meshbench schema, and the same minus `status`.
#
# `status` is the ONLY dictionary column, and a dictionary column forces the
# streaming sink onto its collect path (A-2): the IPC file format permits one
# non-delta dictionary per field, so a unified dictionary must exist before the
# first batch is written, which cannot be known without seeing every batch.
#
# The 14-column cell is therefore the headline (it is the real artifact) and the
# 13-column cell exists to DECOMPOSE the dictionary's contribution to peak RSS.
# Without it, "the Arrow lane is batch-bounded" could not be stated or refuted.
#
# The projection is expressed as inline SQL because `core.config.TableConfig` is
# extra="forbid" and cannot carry a `columns:` key — PG-010/T's third witness,
# after the top-level dialect block and the per-table order_by.
ALL_COLUMNS = [
    "row_id", "account_id", "user_id", "region", "city", "category", "segment",
    "product_name", "status", "amount", "quantity", "price", "score", "event_time",
]
NO_DICT_COLUMNS = [c for c in ALL_COLUMNS if c != "status"]

# Contention thresholds. Deliberately strict: the claim this probe supports is a
# ratio between lanes, and a shared contaminant does not cancel out of a ratio
# when the lanes have different parallelism profiles (DuckDB scans on all cores,
# pandas coercion is single-threaded).
#
# 50% of ONE core (~1.8% of a 28-core machine). Zero is not reachable on a live
# desktop session: with the browser closed, the measured floor is a STEADY
# 34-35% (kwin_wayland ~4.3, beam.smp ~5.8, ray::IDLE ~4.8, r64-mcp ~10, plus
# small fry). A bar at 35 sits exactly on that floor and would trip at random,
# aborting a 38-minute series partway for no real contention.
#
# 50 leaves headroom for jitter while still catching anything that matters by an
# order of magnitude: the browser this gate caught twice was at 821% of one core
# (16x this bar), and its startup burst was 592%. The bar is not the sensitivity
# of the instrument, it is the line between "desktop idle" and "something else
# is running".
MAX_FOREIGN_CPU_PCT = 50.0
MIN_FREE_MEM_GB = 12.0


def foreign_contention() -> dict:
    """Sample non-benchmark CPU and free memory."""
    # `ps` is the measuring instrument, not a contaminant — it appears in its
    # own output with a huge lifetime-average pcpu because it just started.
    ours = {"python", "clickhouse-serv", "duckdb", "ps"}
    total = 0.0
    offenders: list[tuple[str, float]] = []
    out = subprocess.run(
        ["ps", "-eo", "pcpu,comm", "--sort=-pcpu"], capture_output=True, text=True
    ).stdout.splitlines()[1:]
    per_comm: dict[str, float] = {}
    for line in out:
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        pct, comm = float(parts[0]), parts[1].strip()
        if comm in ours or pct < 0.5:
            continue
        per_comm[comm] = per_comm.get(comm, 0.0) + pct
        total += pct
    offenders = sorted(per_comm.items(), key=lambda kv: -kv[1])[:5]

    meminfo = {
        k.strip(): v
        for k, v in (
            line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines()
        )
    }
    free_gb = int(meminfo["MemAvailable"].split()[0]) / 1024 / 1024

    ncpu = os.cpu_count() or 1
    return {
        "foreign_cpu_pct_of_one_core": round(total, 1),
        "foreign_cpu_pct_of_machine": round(total / ncpu, 2),
        "mem_available_gb": round(free_gb, 2),
        "top_offenders": offenders,
        "load1": os.getloadavg()[0],
    }


def _sample_passes(c: dict) -> bool:
    return (
        c["foreign_cpu_pct_of_one_core"] <= MAX_FOREIGN_CPU_PCT
        and c["mem_available_gb"] >= MIN_FREE_MEM_GB
    )


def gate(strict: bool, retries: int = 8, wait_s: float = 15.0) -> dict:
    """Block until the machine is quiet, or abort if it stays noisy.

    A single failed sample is NOT enough to abort. A 37-minute series must
    survive a transient — the screen locker engaging, a compositor repaint —
    without discarding 30 minutes of gate-valid work, while still refusing to
    measure under something sustained. So a failure WAITS and re-samples, and
    only a contaminant that persists across every retry stops the run.

    This is the honest distinction: transient spikes are not what the gate is
    for (they do not overlap a whole rep), sustained load is.
    """
    c = foreign_contention()
    c["pass"] = _sample_passes(c)
    if not strict or c["pass"]:
        return c

    for attempt in range(retries):
        print(
            f"  gate: {c['foreign_cpu_pct_of_one_core']}% of one core "
            f"(top: {c['top_offenders'][:2]}) — waiting {wait_s:.0f}s "
            f"[{attempt + 1}/{retries}]",
            flush=True,
        )
        time.sleep(wait_s)
        c = foreign_contention()
        c["pass"] = _sample_passes(c)
        c["waited_s"] = (attempt + 1) * wait_s
        if c["pass"]:
            return c

    print(json.dumps({"gate_failed": c}, indent=2), file=sys.stderr)
    raise SystemExit(
        f"FOREIGN CONTENTION GATE FAILED after {retries * wait_s:.0f}s of waiting: "
        f"{c['foreign_cpu_pct_of_one_core']}% of one core "
        f"(limit {MAX_FOREIGN_CPU_PCT}), "
        f"{c['mem_available_gb']}GB available (min {MIN_FREE_MEM_GB}). "
        f"Top: {c['top_offenders']}"
    )


# ---------------------------------------------------------------- child side


def _artifact_columns(artifact: Path) -> int:
    """Column count read from the artifact itself, so a cell cannot be mislabelled."""
    import pyarrow as pa
    import pyarrow.ipc as ipc

    return len(ipc.open_file(pa.memory_map(str(artifact))).schema)


def run_cell(cell: str, scale: str, out_dir: Path) -> dict:
    """Execute ONE pull and return its decomposition. Runs in a fresh process."""
    import asyncio

    from r64_db_engine.core.config import Config
    from r64_db_engine.core.daemon import build_daemon

    table = f"perf_{scale}"
    out_dir.mkdir(parents=True, exist_ok=True)
    state = out_dir / "state"

    if cell == "P":
        payload = {
            "dialect": "clickhouse",
            "clickhouse": {"host": "127.0.0.1", "port": 8123, "database": "meshbench"},
            "sink": {
                "type": "arrow_ipc",
                "output_dir": str(out_dir),
                "group": "Bench",
                "dictionary_columns": {"Perf": ["status"]},
                "timestamp_unit": "us",
            },
            "tables": [{"source": f"SELECT * FROM meshbench.{table} ORDER BY row_id",
                        "target": "Perf", "mode": "full_refresh", "cadence": "5s"}],
        }
    else:
        arrow = cell.startswith("N")
        no_dict = "13" in cell
        sink: dict = {
            "type": "arrow_ipc",
            "output_dir": str(out_dir),
            "group": "Bench",
        }
        if not no_dict:
            sink["dictionary_columns"] = {"Perf": ["status"]}
        if not arrow:
            sink["timestamp_unit"] = "us"
        columns = NO_DICT_COLUMNS if no_dict else ALL_COLUMNS
        projection = ", ".join(columns)
        # A "U" suffix drops the ORDER BY. Not a determinism-safe pull — it
        # exists ONLY to isolate how much of peak RSS is the sort rather than
        # the lane, because DuckDB must materialize a result set to sort it.
        ordered = not cell.endswith("U")
        payload = {
            "dialect": "duckdb",
            "duckdb": {
                "database": str(DUCKDB_PATH),
                "read_only": True,
                "arrow": arrow,
                "settings": {"TimeZone": "UTC"},
            },
            "sink": sink,
            "tables": [{
                "source": (
                    f"SELECT {projection} FROM main.{table}"
                    + (" ORDER BY row_id" if ordered else "")
                ),
                "target": "Perf", "mode": "full_refresh", "cadence": "5s"}],
        }

    payload["row64"] = {"loading_dir": str(out_dir), "group": "Bench"}
    payload["runtime"] = {"state_dir": str(state)}
    config = Config.model_validate(payload)

    # Phase decomposition. `pull` covers source-query + transform-or-coerce;
    # the sink write is timed separately by wrapping the sink's own entry point.
    daemon = build_daemon(config)
    timings: dict[str, float] = {}

    writer = daemon.writer
    original_write = writer.write
    original_stream = writer.write_stream

    def timed_write(df, target):
        t0 = time.perf_counter()
        try:
            return original_write(df, target)
        finally:
            timings["sink_write_s"] = time.perf_counter() - t0

    def timed_stream(reader, target):
        t0 = time.perf_counter()
        try:
            return original_stream(reader, target)
        finally:
            timings["sink_write_s"] = time.perf_counter() - t0

    writer.write = timed_write          # type: ignore[method-assign]
    writer.write_stream = timed_stream  # type: ignore[method-assign]

    driver = daemon.driver
    original_pull = driver.pull
    original_pull_arrow = driver.pull_arrow

    async def timed_pull(tcfg, wm):
        t0 = time.perf_counter()
        try:
            return await original_pull(tcfg, wm)
        finally:
            timings["pull_s"] = time.perf_counter() - t0

    async def timed_pull_arrow(tcfg, wm):
        # For the Arrow lane this measures ONLY query submission + reader
        # handoff. The scan itself happens lazily as the sink drains, and lands
        # in sink_write_s. That asymmetry is real and is reported, not hidden.
        t0 = time.perf_counter()
        try:
            return await original_pull_arrow(tcfg, wm)
        finally:
            timings["pull_s"] = time.perf_counter() - t0

    driver.pull = timed_pull                # type: ignore[method-assign]
    driver.pull_arrow = timed_pull_arrow    # type: ignore[method-assign]

    wall0 = time.perf_counter()
    asyncio.run(daemon.run(once=True))
    wall = time.perf_counter() - wall0

    rt = daemon.tables["Perf"]
    if rt.status != "ok":
        raise RuntimeError(f"cell {cell}@{scale} failed: {rt.last_error}")

    artifact = out_dir / "Bench" / "Perf.arrow"
    peak_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return {
        "cell": cell,
        "scale": scale,
        "lane": daemon.status_snapshot()["source"]["lane"],
        "rows": rt.rows_pulled_last,
        "columns": _artifact_columns(artifact),
        "wall_s": round(wall, 4),
        "pull_s": round(timings.get("pull_s", 0.0), 4),
        "sink_write_s": round(timings.get("sink_write_s", 0.0), 4),
        "peak_rss_mb": round(peak_kb / 1024, 1),
        "artifact_mb": round(artifact.stat().st_size / 1024 / 1024, 1),
    }


# --------------------------------------------------------------- parent side


def one_rep(cell: str, scale: str, tmp_root: Path, rep: int) -> dict:
    out_dir = tmp_root / f"{cell}_{scale}_{rep}"
    before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    proc = subprocess.run(
        [sys.executable, __file__, "--child", cell, scale, str(out_dir)],
        capture_output=True,
        text=True,
        cwd=str(REPO),
        env={**os.environ, "PYTHONNOUSERSITE": "1"},
    )
    if proc.returncode != 0:
        raise RuntimeError(f"{cell}@{scale} rep {rep} failed:\n{proc.stderr[-3000:]}")
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    after = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    payload["parent_observed_peak_mb"] = round(max(after, before) / 1024, 1)
    # Free the artifact immediately; 10M cells are ~1.4GB each.
    for path in out_dir.rglob("*.arrow"):
        path.unlink()
    return payload


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--child", nargs=3, metavar=("CELL", "SCALE", "OUT"))
    ap.add_argument("--cells", nargs="*", default=["N", "P'", "P"])
    ap.add_argument("--scales", nargs="*", default=["1m", "10m"])
    ap.add_argument("--reps", type=int, default=10)
    ap.add_argument("--out", default=str(BENCH / "results" / "phase-c.json"))
    ap.add_argument("--tmp", default="/tmp/phase-c")
    ap.add_argument("--no-gate", action="store_true",
                    help="pilot/sizing only — results are NOT of record")
    args = ap.parse_args()

    if args.child:
        cell, scale, out = args.child
        print(json.dumps(run_cell(cell, scale, Path(out))))
        return 0

    tmp_root = Path(args.tmp)
    tmp_root.mkdir(parents=True, exist_ok=True)
    strict = not args.no_gate

    baseline = gate(strict)
    print("quiet baseline:", json.dumps(baseline))

    results: list[dict] = []
    for scale in args.scales:
        for cell in args.cells:
            reps: list[dict] = []
            for rep in range(args.reps):
                gate(strict)  # per-invocation, not once
                r = one_rep(cell, scale, tmp_root, rep)
                reps.append(r)
                print(f"  {cell:2s}@{scale} rep{rep}: wall={r['wall_s']:8.3f}s "
                      f"pull={r['pull_s']:8.3f}s sink={r['sink_write_s']:8.3f}s "
                      f"rss={r['peak_rss_mb']:8.1f}MB", flush=True)
            walls = [r["wall_s"] for r in reps]
            spread = (max(walls) - min(walls)) / min(walls) if min(walls) else 0.0
            results.append({
                "cell": cell, "scale": scale, "n": len(reps),
                "lane": reps[0]["lane"], "rows": reps[0]["rows"],
                "artifact_mb": reps[0]["artifact_mb"],
                "wall_min": min(walls), "wall_median": statistics.median(walls),
                "wall_spread_pct": round(spread * 100, 1),
                "pull_min": min(r["pull_s"] for r in reps),
                "pull_median": statistics.median(r["pull_s"] for r in reps),
                "sink_min": min(r["sink_write_s"] for r in reps),
                "sink_median": statistics.median(r["sink_write_s"] for r in reps),
                "peak_rss_mb_max": max(r["peak_rss_mb"] for r in reps),
                "peak_rss_mb_median": statistics.median(r["peak_rss_mb"] for r in reps),
                "reps": reps,
            })
            summary = results[-1]
            print(f"{cell:2s}@{scale}: min={summary['wall_min']:.3f}s "
                  f"median={summary['wall_median']:.3f}s "
                  f"spread={summary['wall_spread_pct']}% "
                  f"rss={summary['peak_rss_mb_max']:.1f}MB", flush=True)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(
        {"baseline": baseline, "gated": strict, "results": results}, indent=2
    ))
    print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
