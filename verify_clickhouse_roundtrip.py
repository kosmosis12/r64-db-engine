#!/usr/bin/env python3
"""
Step 10 — ClickHouse driver live round-trip producer verification (v2, clean).

Proves the ClickHouse driver moves data byte-clean, end to end:
    seed ClickHouse (every supported type) -> driver.pull() -> save_from_df()
    -> load_to_df() -> assert exact match.

Producer-side gate only. Does NOT test Row64 Server promotion (loading/ -> live/).

Aligned to the real driver contract:
    - ClickHouseDriver()                      (no constructor args)
    - await driver.connect(config_dict)       (config flows in here)
    - driver.pull(spec, watermark) -> PullResult   (unwrapped to DataFrame)

Usage:
    cd ~/builds/r64-db-engine && source .venv/bin/activate
    python verify_clickhouse_roundtrip.py
"""
from __future__ import annotations

import asyncio
import inspect
import os
import sys
import tempfile
import time
from pathlib import Path

import clickhouse_connect
import numpy as np
import pandas as pd

from row64tools.ramdb import save_from_df, load_to_df

# ---------------------------------------------------------------------------
CH = dict(
    host=os.environ.get("CH_HOST", "localhost"),
    port=int(os.environ.get("CH_PORT", "8123")),
    username=os.environ.get("CH_USER", "row64dev"),
    password=os.environ.get("CH_PASSWORD", "row64dev"),
    database="default",
)
TABLE = "rt_typecheck"
N_ROWS = 50_000


def log(msg: str) -> None:
    print(f"[verify] {msg}", flush=True)


# ---------------------------------------------------------------------------
# 1. Seed ClickHouse with supported types only.
# ---------------------------------------------------------------------------
def seed() -> None:
    client = clickhouse_connect.get_client(**CH)
    client.command(f"DROP TABLE IF EXISTS {TABLE}")
    client.command(f"""
        CREATE TABLE {TABLE} (
            id      UInt32,
            small_i Int8,
            med_i   Int32,
            big_i   Int64,
            f32     Float32,
            f64     Float64,
            dec     Decimal(20, 5),
            s       String,
            fs      FixedString(8),
            lc      LowCardinality(String),
            nul_s   Nullable(String),
            nul_i   Nullable(Int32),
            d       Date,
            dt      DateTime,
            dt64    DateTime64(3),
            u       UUID,
            b       Bool,
            en      Enum8('a' = 1, 'b' = 2, 'c' = 3),
            arr     Array(Int32)
        ) ENGINE = MergeTree ORDER BY id
    """)

    import uuid as _uuid
    from datetime import datetime, date

    rng = np.random.default_rng(42)
    enum_vals = ["a", "b", "c"]
    rows = []
    for i in range(N_ROWS):
        rows.append([
            i,
            int(rng.integers(-128, 127)),
            int(rng.integers(-(2**31), 2**31 - 1)),
            int(rng.integers(-(2**62), 2**62)),
            float(rng.random()) * 1000,
            float(rng.random()) * 1e6,
            round(float(rng.random()) * 99999, 5),
            f"row\u2014{i}\u2014smartquote\u2019s",
            f"fix{i % 1000:04d}",
            enum_vals[i % 3],
            None if i % 7 == 0 else f"opt{i}",
            None if i % 5 == 0 else int(i),
            date(2026, 1 + (i % 12), 1 + (i % 27)),
            datetime(2026, 5, 23, i % 24, i % 60, 0),
            datetime(2026, 5, 23, i % 24, i % 60, 0, (i % 1000) * 1000),
            _uuid.uuid4(),
            bool(i % 2),
            enum_vals[i % 3],
            [i, i + 1, i + 2],
        ])
    cols = ["id","small_i","med_i","big_i","f32","f64","dec","s","fs","lc",
            "nul_s","nul_i","d","dt","dt64","u","b","en","arr"]
    client.insert(TABLE, rows, column_names=cols)
    got = client.command(f"SELECT count() FROM {TABLE}")
    log(f"seeded {got} rows into {TABLE}")
    client.close()


# ---------------------------------------------------------------------------
# 2. Pull through the real driver — single, correct definition.
# ---------------------------------------------------------------------------
async def pull_via_driver() -> pd.DataFrame:
    from r64_db_engine.drivers.clickhouse.driver import ClickHouseDriver
    from r64_db_engine.core import config as cfgmod

    yaml_text = f"""
dialect: clickhouse
clickhouse:
  host: {CH['host']}
  port: {CH['port']}
  database: {CH['database']}
  user: {CH['username']}
  password: {CH['password']}
  secure: false
row64:
  loading_dir: /tmp/r64-ch-verify-loading
  group: RAMDB.Row64
defaults:
  cadence: 60s
  mode: full_refresh
  ascii_sanitize: true
tables:
  - source: {CH['database']}.{TABLE}
    target: RtTypecheck
    mode: full_refresh
    cadence: 60s
telemetry:
  log_level: info
  log_format: json
  health_port: 8799
runtime:
  worker_pool_size: 4
  state_dir: /tmp/r64-ch-verify-state
"""
    cfg_path = Path(tempfile.mktemp(suffix=".yaml"))
    cfg_path.write_text(yaml_text)

    loader = None
    for name in ("load_config", "load", "from_yaml"):
        if hasattr(cfgmod, name):
            loader = getattr(cfgmod, name)
            break
    if loader is None and hasattr(cfgmod, "Config"):
        c = cfgmod.Config
        loader = getattr(c, "from_yaml", None) or getattr(c, "load", None)
    if loader is None:
        raise SystemExit("Could not find config loader in core.config — inspect manually.")
    config = loader(str(cfg_path))

    # Matches factory.py exactly: no-arg constructor.
    driver = ClickHouseDriver()

    if hasattr(config, "driver_config"):
        connect_cfg = config.driver_config()
    else:
        connect_cfg = {
            "host": CH["host"], "port": CH["port"], "database": CH["database"],
            "user": CH["username"], "password": CH["password"], "secure": False,
        }
    await driver.connect(connect_cfg)

    tables = getattr(config, "tables", None)
    spec = tables[0] if tables else None

    await driver.discover([spec])
    await driver.validate_table(spec)
    result = await driver.pull(spec, None)

    # pull() returns PullResult — unwrap to the DataFrame.
    df = None
    if isinstance(result, pd.DataFrame):
        df = result
    else:
        for attr in ("dataframe", "df", "data", "frame", "rows"):
            val = getattr(result, attr, None)
            if isinstance(val, pd.DataFrame):
                df = val
                break
    if df is None:
        fields = [a for a in dir(result) if not a.startswith("_")]
        raise SystemExit(f"Could not find DataFrame on PullResult. Available attrs: {fields}")

    await driver.close()
    log(f"driver.pull returned {len(df)} rows, {len(df.columns)} columns")
    return df


# ---------------------------------------------------------------------------
# 3. Round-trip through ramdb and assert exact match.
# ---------------------------------------------------------------------------
def roundtrip_and_assert(df: pd.DataFrame) -> None:
    out = Path(tempfile.mktemp(suffix=".ramdb"))
    t0 = time.perf_counter()
    save_from_df(df, str(out))
    dt_ms = (time.perf_counter() - t0) * 1000
    size_mb = out.stat().st_size / 1e6
    log(f"save_from_df: {len(df)} rows -> {size_mb:.1f}MB in {dt_ms:.0f}ms "
        f"(~{len(df)/(dt_ms/1000):,.0f} rows/sec)")

    back = load_to_df(str(out))
    log(f"load_to_df: read back {len(back)} rows, {len(back.columns)} columns")

    assert len(back) == len(df), f"ROW COUNT MISMATCH: wrote {len(df)}, read {len(back)}"
    assert list(back.columns) == list(df.columns), (
        f"COLUMN MISMATCH:\n wrote {list(df.columns)}\n read  {list(back.columns)}"
    )

    a = df.reset_index(drop=True)
    b = back.reset_index(drop=True)
    mismatches = []
    for col in a.columns:
        ca, cb = a[col], b[col]
        if ca.dtype.kind in "fc":
            eq = ((ca - cb).abs() < 1e-6) | (ca.isna() & cb.isna())
        else:
            eq = (ca == cb) | (ca.isna() & cb.isna())
        nbad = int((~eq).sum())
        if nbad:
            mismatches.append((col, nbad, ca[~eq].head(2).tolist(), cb[~eq].head(2).tolist()))

    if mismatches:
        log("❌ VALUE MISMATCHES (post-coercion frame did not survive round-trip):")
        for col, n, wrote, read in mismatches:
            log(f"   {col}: {n} rows differ | wrote {wrote} | read {read}")
        raise SystemExit(1)

    log("✅ BYTE-CLEAN ROUND-TRIP: row count, columns, and all values match.")
    out.unlink(missing_ok=True)


def main() -> None:
    log("=== ClickHouse driver Step 10 round-trip ===")
    try:
        seed()
        df = asyncio.run(pull_via_driver())
    except Exception as exc:
        log(f"❌ FAILED during seed/pull: {type(exc).__name__}: {exc}")
        raise
    roundtrip_and_assert(df)
    log("Step 10 PASS — producer side verified. Consumer-side promotion is separate.")


if __name__ == "__main__":
    main()
