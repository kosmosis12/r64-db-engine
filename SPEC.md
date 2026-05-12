# r64-db-engine — Specification

**Status:** Draft v1.0 — implementation contract
**Author:** Kos Russell
**Date:** 11 May 2026
**Implementer:** Claude Code
**Repo target:** `kosmosis12/r64-db-engine` (private initially)

---

## 1. Problem statement

Row64 Server's current external-database integration model is unergonomic for prospects and operators:

1. Per-table bespoke Python scripts using `row64tools.ramdb.save_from_df()`
2. Hand-managed `.env` files and credentials
3. Cron-job orchestration with no health surface
4. Manual schema definition — no discovery, no drift detection
5. Silent failures: a broken cron job means stale data with no alert
6. Type-coercion gotchas (ASCII sanitization, NaN-in-int, datetime64 native vs. string) rediscovered painfully each time

For PostgreSQL specifically — by far the most common operational database in Row64's prospect base — this turns a "should be 10 minutes" integration into a half-day project per prospect.

**`r64-db-engine` is a single supervised daemon that takes a YAML config and a Postgres connection string and continuously materializes configured tables into Row64 Server's loading directory as `.ramdb` files.**

It is the operational layer between Postgres and Row64. It is not a bidirectional connector. It is not a query federation layer. It is a hardened, schema-aware, watermark-tracking, atomic-write ingestion daemon.

---

## 2. Non-goals

To prevent scope creep, this is explicitly **not**:

- A query layer (Row64 does its own compute on the ramdb)
- A write-back path to Postgres (one-way only)
- A general ETL framework (Airbyte, dbt, Fivetran exist)
- A multi-source orchestrator (one daemon = one Postgres source; multiple sources = multiple daemons)
- A UI (CLI + config file + `/health` endpoint only)

If a feature does not directly reduce friction in "Postgres table → Row64 ramdb file," it does not belong in v1.

---

## 3. Architecture

```
┌──────────────┐    SQL pulls    ┌──────────────────┐    .ramdb writes    ┌─────────────────┐
│  PostgreSQL  │ ◄────────────── │  r64-db-engine   │ ──────────────────► │  Row64 Server   │
│  (source)    │  watermarked    │     daemon       │  atomic rename      │  loading/ → live/│
└──────────────┘                 └──────────────────┘                     └─────────────────┘
                                          │
                                          ├── reads:  /etc/r64-db-engine/config.yaml
                                          ├── state:  ~/.r64-db-engine/state.db (SQLite)
                                          ├── logs:   stdout (JSON, journald-friendly)
                                          └── health: HTTP :8765/health
```

**Process model:** single long-running process, supervised by systemd. Internal scheduler dispatches per-table pull jobs on their configured cadence. No external orchestration (no cron, no Airflow, no celery).

**Concurrency:** one in-flight pull per table at a time; multiple tables can pull concurrently up to a bounded worker pool (default: 4). If a pull is still running when the next cadence tick fires, the tick is skipped (logged as `skipped_overlap`) and the next tick is awaited.

### 3.1 Driver abstraction

The engine is source-agnostic. All source-database knowledge lives behind a `Driver` ABC in `core/driver.py`. v0.1 ships one driver (`PostgresDriver`); v0.2+ adds Redshift, ClickHouse, BigQuery, Snowflake, Databricks against the same interface.

```python
# core/driver.py
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, AsyncIterator
import pandas as pd

@dataclass(frozen=True)
class TableMetadata:
    schema: str
    name: str
    columns: list["ColumnMetadata"]
    estimated_rows: int | None
    candidate_incremental_keys: list[str]  # timestamp/int columns suitable for watermarking

@dataclass(frozen=True)
class ColumnMetadata:
    name: str
    source_type: str          # native source type (e.g., "bigint", "jsonb", "timestamptz")
    nullable: bool
    pandas_dtype: str         # target pandas dtype after coercion (e.g., "int64", "string")

@dataclass(frozen=True)
class PullResult:
    dataframe: pd.DataFrame
    new_watermark: str | int | None   # None for full_refresh
    rows_pulled: int
    duration_ms: int

@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    errors: list[str]
    warnings: list[str]

class Driver(ABC):
    """Abstract base for source-database drivers.

    One Driver instance per running daemon. Drivers are stateful — they
    hold a connection pool and reuse it across pulls. Drivers are
    expected to be async-safe.
    """

    @classmethod
    @abstractmethod
    def dialect_name(cls) -> str:
        """Short identifier for this driver. Used in config (e.g., 'postgres')."""

    @abstractmethod
    async def connect(self, config: dict[str, Any]) -> None:
        """Establish connection pool. Called once at daemon startup.
        Raises ConnectionError on permanent auth/network failures."""

    @abstractmethod
    async def close(self) -> None:
        """Cleanly close all connections. Called on daemon shutdown."""

    @abstractmethod
    async def discover(self, schema_filter: str | None = None) -> list[TableMetadata]:
        """List available tables with column metadata and incremental-key candidates.
        Used by `r64-db-engine discover` CLI and by `validate` to confirm
        configured tables exist."""

    @abstractmethod
    async def validate_table(self, table_config: dict[str, Any]) -> ValidationResult:
        """Pre-pull validation: table exists, columns exist, incremental_key
        is a sensible type, user-supplied SQL parses. No data fetched."""

    @abstractmethod
    async def pull(
        self,
        table_config: dict[str, Any],
        previous_watermark: str | int | None,
    ) -> PullResult:
        """Execute the pull. For incremental mode, apply WHERE filter using
        previous_watermark. Returns DataFrame with all type coercion applied
        (column dtypes match TableMetadata.pandas_dtype) and the new watermark."""

    @abstractmethod
    def coerce_value(self, value: Any, source_type: str) -> Any:
        """Dialect-specific single-value coercion. Used by tests; pull() applies
        this internally."""
```

**Driver registration:** drivers register themselves via `drivers/__init__.py` exposing a `DRIVERS: dict[str, type[Driver]]` map. CLI resolves `dialect: postgres` in config to `DRIVERS["postgres"]` at daemon startup.

**What this enforces:** the daemon, scheduler, ramdb writer, state store, and health endpoint never import from `drivers/postgres/`. Adding a Redshift driver requires zero changes to `core/`. This is the discipline that makes `r64-db-engine` an engine instead of a Postgres script with delusions.

---

## 4. Configuration

### 4.1 File location

- Default: `/etc/r64-db-engine/config.yaml`
- Overridable: `--config /path/to/config.yaml`
- Environment variable substitution: `${VAR_NAME}` resolved from process env at startup. Missing required vars → fail-fast at startup with clear error.

### 4.2 Schema

```yaml
# r64-db-engine configuration

dialect: postgres               # required; resolves to drivers/postgres/

postgres:
  host: ${PG_HOST}
  port: 5432                    # default 5432
  database: analytics           # required
  user: ${PG_USER}              # required
  password: ${PG_PASSWORD}      # required (or use .pgpass; see §4.4)
  sslmode: prefer               # disable | allow | prefer | require | verify-ca | verify-full
  application_name: r64-db-engine  # appears in pg_stat_activity
  connect_timeout: 10           # seconds
  statement_timeout: 300        # seconds; per-query timeout sent as SET LOCAL

row64:
  loading_dir: /var/www/ramdb/loading/RAMDB.Row64
  group: PostgresSource         # subdirectory name under loading_dir
  # final path per table: {loading_dir}/{group}/{target}.ramdb

defaults:
  cadence: 60s                  # default per-table cadence if not specified
  mode: full_refresh            # default mode
  max_rows: null                # null = unbounded; set integer to cap per-pull
  ascii_sanitize: true          # see §6.2

tables:
  - source: public.orders       # schema-qualified table name
    target: Orders              # ramdb filename (no extension)
    mode: incremental
    incremental_key: updated_at
    cadence: 60s

  - source: public.customers
    target: Customers
    mode: full_refresh
    cadence: 5m

  - source: |                   # inline SQL — anything valid as a subquery
      SELECT id, region, SUM(amount) AS revenue
      FROM transactions
      WHERE created_at >= NOW() - INTERVAL '30 days'
      GROUP BY 1, 2
    target: RegionalRevenue30d
    mode: full_refresh
    cadence: 15m

  - source: analytics.event_log
    target: Events
    mode: incremental
    incremental_key: event_id   # integer key works too
    incremental_type: int       # 'timestamp' (default) | 'int'
    cadence: 30s

telemetry:
  log_level: info               # debug | info | warning | error
  log_format: json              # json | text
  health_port: 8765             # 0 to disable health endpoint
  metrics_port: 0               # 0 = off; nonzero = Prometheus exposition

runtime:
  worker_pool_size: 4           # concurrent table pulls
  state_dir: ~/.r64-db-engine   # watermark SQLite location
  shutdown_grace_seconds: 30    # how long to wait for in-flight pulls on SIGTERM
```

### 4.3 Cadence syntax

- `30s`, `60s`, `5m`, `1h`, `24h` — standard Go-duration style
- Minimum cadence: 5s (lower is rejected at startup with a clear error)
- No cron expressions in v1 (keep it simple)

### 4.4 Auth fallback chain

In priority order:

1. `password` field in config (resolved from env)
2. `PGPASSWORD` env var
3. `~/.pgpass` file (libpq standard)
4. Fail-fast with error pointing to all three options

---

## 5. Operational modes

### 5.1 `full_refresh`

- Pulls entire result set from source on each cadence tick
- Overwrites the target `.ramdb` atomically
- Use for: dimension tables, small reference data, anything where incremental keys don't exist
- Performance note: log a warning if a full_refresh pull exceeds 60s or returns >1M rows; both are signs the table wants `incremental` mode

### 5.2 `incremental`

- On first run: pulls everything (no watermark exists yet), records the max `incremental_key` value as the watermark
- On subsequent runs: pulls `WHERE {incremental_key} > {last_watermark}`, appends to target ramdb, records new max
- **Critical:** ramdb format doesn't natively support append; v1 implementation reads existing ramdb into a DataFrame, concatenates the new rows, writes back atomically. Acceptable for tables up to ~5M rows. Above that, document the limitation and recommend `full_refresh` with `WHERE` clause in inline SQL.
- `incremental_type: timestamp` (default): treats the column as `timestamp` / `timestamptz`; watermark stored as ISO8601 string
- `incremental_type: int`: treats column as `bigint` / `int`; watermark stored as integer

### 5.3 Watermark storage

SQLite at `{state_dir}/state.db`:

```sql
CREATE TABLE watermarks (
  target TEXT PRIMARY KEY,
  watermark_value TEXT NOT NULL,
  watermark_type TEXT NOT NULL,    -- 'timestamp' | 'int'
  last_success_at TEXT NOT NULL,   -- ISO8601
  rows_pulled INTEGER NOT NULL,
  last_pull_duration_ms INTEGER NOT NULL
);

CREATE TABLE pull_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  target TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL,            -- 'success' | 'error' | 'skipped_overlap'
  rows_pulled INTEGER,
  error_message TEXT
);

CREATE INDEX idx_pull_history_target_started ON pull_history(target, started_at DESC);
```

Pull history retention: last 100 rows per target, oldest pruned on insert.

---

## 6. Type coercion — the hard part

This is the institutional memory of every ramdb ingestion you've shipped. Encoded here so it never has to be rediscovered.

### 6.1 PostgreSQL → pandas → ramdb mapping

| Postgres type | psycopg returns | Pandas dtype target | Ramdb-safe? | Notes |
|---|---|---|---|---|
| `smallint`, `integer`, `bigint` | int | `int64` (after NaN→0 fill) | yes | NaN in int columns force float promotion → `save_from_df` fails. Fill explicitly. |
| `real`, `double precision` | float | `float64` | yes | NaN allowed |
| `numeric`, `decimal` | Decimal | `float64` (with precision-loss warning) | yes | Log warning if cast loses precision >0.0001 |
| `text`, `varchar`, `char` | str | `string` | yes after sanitize | See §6.2 |
| `boolean` | bool | `bool` | yes | NaN → False with debug-level log |
| `date` | datetime.date | `datetime64[ns]` | yes | Coerce via `pd.to_datetime` |
| `timestamp`, `timestamptz` | datetime.datetime | `datetime64[ns]` | yes | `tz_convert('UTC').tz_localize(None)` for tz-aware |
| `time`, `timetz` | datetime.time | `string` (HH:MM:SS) | yes | Ramdb has no native time-of-day; serialize as string |
| `interval` | datetime.timedelta | `int64` (microseconds) | yes | Document the conversion explicitly |
| `uuid` | UUID | `string` | yes | str() representation |
| `json`, `jsonb` | dict/list | `string` (compact JSON) | yes | `json.dumps(default=str)`; warn on >64KB values |
| `bytea` | bytes | `string` (hex) | yes | Hex-encoded; warn on >64KB values |
| `array` (any) | list | `string` (JSON) | yes | Same as jsonb path |
| `inet`, `cidr`, `macaddr` | str | `string` | yes | str() of psycopg's adapted object |
| `tsvector`, `tsquery` | str | `string` | yes | Raw text representation |
| `geometry` (PostGIS) | str (WKT) or bytes (WKB) | `string` (WKT) | yes | Require `ST_AsText()` in source SQL if column is WKB |
| `xml` | str | `string` | yes | Raw text |
| `range` types | str | `string` | yes | Raw text representation |

### 6.2 ASCII sanitization

When `defaults.ascii_sanitize: true` (default), every string column passes through:

```python
df[col] = df[col].astype(str).str.encode('ascii', errors='replace').str.decode('ascii')
```

This replaces em-dashes, smart quotes, accented chars, emoji with `?`. Lossy but ramdb-safe.

When `ascii_sanitize: false`, strings pass through unchanged. **Document clearly:** ramdb's underlying serializer will crash on certain non-ASCII chars; opt out at your own risk.

Per-table override available:
```yaml
- source: public.notes
  target: Notes
  ascii_sanitize: false   # I know what I'm doing
```

### 6.3 NaN handling

- Integer columns with NaN → fill with 0, log row count of fills at debug level
- Float columns with NaN → preserve (ramdb supports float NaN)
- String columns with NaN → fill with empty string `""`
- Boolean columns with NaN → fill with False
- Timestamp columns with NaT → preserve (ramdb supports timestamp NaT as int64 sentinel)

### 6.4 Schema drift detection

On each pull, compare current column list to the column list of the previous successful pull (stored in state.db as JSON):

- **New column added:** log warning, include it in the pull, update stored schema
- **Column removed:** log warning, omit it from the ramdb (don't fail)
- **Column type changed:** log error with both types, include best-effort coercion, do not fail the pull but flag in `/health`

Schema state table:
```sql
CREATE TABLE schemas (
  target TEXT PRIMARY KEY,
  columns_json TEXT NOT NULL,      -- [{"name": "id", "pg_type": "bigint", "pd_dtype": "int64"}, ...]
  observed_at TEXT NOT NULL
);
```

---

## 7. Atomic write protocol

**Critical invariant:** Row64 Server must never see a half-written `.ramdb` file in `loading/`.

Implementation:

1. Resolve target path: `{loading_dir}/{group}/{target}.ramdb`
2. Ensure `{loading_dir}/{group}/` exists (create with `mode=0o755` if not)
3. Write to tempfile in the **same directory** (not /tmp; `os.rename` is only atomic within a filesystem): `{loading_dir}/{group}/.{target}.ramdb.tmp.{uuid}`
4. After successful `save_from_df`, `os.rename(tempfile, final_path)` — atomic on POSIX
5. On any exception during write: delete the tempfile, log error, do not touch the final file
6. On daemon SIGTERM/SIGINT during write: cleanup tempfile in a finally block, then exit

**Directory creation:** if `{loading_dir}/{group}/` doesn't exist at startup, create it. If `{loading_dir}` itself doesn't exist, fail-fast with a clear error pointing to the Row64 Server install.

**Permissions:** the daemon process must be able to write to `{loading_dir}/{group}/`. Document the systemd `User=` and group setup in the README. Recommend running as a `row64` user that's also a member of the `www-data` group (or whatever group owns `/var/www/ramdb/`).

---

## 8. Telemetry

### 8.1 Structured logs (default: JSON to stdout)

Each log entry:
```json
{
  "ts": "2026-05-11T18:23:45.123Z",
  "level": "info",
  "event": "pull_success",
  "target": "Orders",
  "rows": 41203,
  "duration_ms": 1842,
  "mode": "incremental",
  "watermark_before": "2026-05-11T18:22:00Z",
  "watermark_after": "2026-05-11T18:23:42Z"
}
```

Standard events: `daemon_start`, `daemon_stop`, `config_loaded`, `schema_discovered`, `pull_start`, `pull_success`, `pull_error`, `pull_skipped_overlap`, `schema_drift`, `watermark_advanced`, `health_check`.

### 8.2 Health endpoint

`GET http://localhost:8765/health` returns:

```json
{
  "status": "ok",                              // "ok" | "degraded" | "error"
  "uptime_seconds": 4392,
  "version": "0.1.0",
  "postgres": {
    "connected": true,
    "host": "db.example.com",
    "database": "analytics"
  },
  "tables": [
    {
      "target": "Orders",
      "status": "ok",
      "mode": "incremental",
      "last_success_at": "2026-05-11T18:23:42Z",
      "rows_pulled_last": 41203,
      "rows_pulled_total": 8721044,
      "watermark": "2026-05-11T18:23:42Z",
      "schema_drift_detected": false
    },
    {
      "target": "Customers",
      "status": "error",
      "mode": "full_refresh",
      "last_success_at": "2026-05-11T15:00:00Z",
      "last_error": "psycopg.OperationalError: connection closed",
      "last_error_at": "2026-05-11T18:00:00Z",
      "consecutive_failures": 3
    }
  ]
}
```

Health status logic:
- `ok` — all tables succeeded within the last 3× their cadence
- `degraded` — any table has 1-2 consecutive failures, or any schema drift detected
- `error` — any table has 3+ consecutive failures, or Postgres connection is down

HTTP status:
- 200 if `ok`
- 200 if `degraded` (still alive, surface in body)
- 503 if `error`

### 8.3 Prometheus metrics (optional, `metrics_port > 0`)

```
r64_db_engine_pulls_total{target="Orders",status="success"} 14823
r64_db_engine_pulls_total{target="Orders",status="error"} 2
r64_db_engine_pull_duration_seconds{target="Orders",quantile="0.5"} 1.842
r64_db_engine_rows_pulled_total{target="Orders"} 8721044
r64_db_engine_table_last_success_timestamp_seconds{target="Orders"} 1715451822
r64_db_engine_postgres_up 1
r64_db_engine_uptime_seconds 4392
```

---

## 9. Failure modes & retries

### 9.1 Transient failures (retry)

- Connection reset, timeout, "server closed connection unexpectedly"
- `psycopg.OperationalError` with retryable codes (08000–08007 connection exceptions, 57P01 admin shutdown)

Retry policy:
- 3 retries per cadence tick
- Exponential backoff: 1s, 4s, 16s
- After 3 failed retries within a tick, log `pull_error`, increment consecutive_failures, wait for next cadence tick

### 9.2 Permanent failures (do not retry)

- Authentication failure (28000 invalid_authorization)
- Permission denied on table (42501 insufficient_privilege)
- Table does not exist (42P01 undefined_table)
- Syntax error in user-supplied SQL (42601)

These log a single `pull_error` with the SQLSTATE, mark the target as `error` in health, and **continue running the daemon** (other tables may still succeed). Do not crash the daemon on a single bad table.

### 9.3 Postgres connection lost

If the daemon loses connection mid-pull:
- Cancel in-flight pulls cleanly (rollback any open transactions)
- Mark connection as `connected: false` in health
- Reconnect attempts: every 5s, with exponential backoff to max 60s
- Resume normal cadence-driven pulls once reconnected

### 9.4 Watermark corruption recovery

If `state.db` is missing or corrupt at startup:
- Log warning
- Treat all incremental tables as if they were running for the first time
- Re-pull the full source for each → re-establish watermarks
- Document this in the README as the manual reset procedure ("delete state.db")

---

## 10. CLI surface

```
r64-db-engine run [--config /path/to/config.yaml] [--once]
    Start the daemon. --once runs each table exactly once and exits.

r64-db-engine validate [--config /path/to/config.yaml]
    Parse config, connect to Postgres, validate all configured tables exist,
    print schema discovery results, exit. No writes.

r64-db-engine discover [--config /path/to/config.yaml] [--schema PUBLIC]
    Connect to Postgres, list all tables in the given schema with row counts
    and suggested incremental_key candidates. Useful for first-time setup.

r64-db-engine status [--health-url http://localhost:8765/health]
    Query the running daemon's health endpoint and print a human-readable summary.

r64-db-engine version
    Print version and exit.

r64-db-engine install-systemd [--user row64] [--config /path/to/config.yaml]
    Generate and install a systemd unit file at
    /etc/systemd/system/r64-db-engine.service. Print the next steps
    (systemctl enable / start) but do not execute them.
```

---

## 11. Dependencies

Hard requirements:
- Python 3.11+
- `psycopg[binary]>=3.1` (modern psycopg, not psycopg2)
- `pandas>=2.0`
- `row64tools>=1.0.6` (the existing Row64 library)
- `pyyaml>=6.0`
- `pydantic>=2.0` (config validation)

Optional:
- `prometheus_client>=0.17` (only loaded if `metrics_port > 0`)

Dev:
- `pytest>=7`
- `pytest-asyncio`
- `ruff` (linting)
- `mypy` (type checking)

Distribution:
- `pyproject.toml` with `[project.scripts]` entry: `r64-db-engine = r64_db_engine.cli:main`
- Installable via `pip install r64-db-engine` (post-PyPI publish; not v0.1)
- For v0.1: install via `pip install git+ssh://git@github.com/kosmosis12/r64-db-engine.git`

---

## 12. Repo layout

**Architecture intent:** this repo is an engine, not a Postgres-only tool. Postgres is the first driver. The `core/` package is source-agnostic; `drivers/postgres/` is the v0.1 implementation. Future drivers (Redshift, ClickHouse, BigQuery, Snowflake, Databricks) drop into `drivers/` against the same `Driver` ABC.

```
r64-db-engine/
├── README.md                       # quickstart, install, config reference
├── SPEC.md                         # this document (canonical contract)
├── LICENSE                         # MIT
├── pyproject.toml
├── .gitignore
├── .github/
│   └── workflows/
│       └── ci.yml                  # lint + test on push
├── src/
│   └── r64_db_engine/
│       ├── __init__.py
│       ├── cli.py                  # argparse entry point
│       ├── core/                   # source-agnostic engine
│       │   ├── __init__.py
│       │   ├── driver.py           # Driver ABC (see §3.1)
│       │   ├── config.py           # pydantic models + YAML loader
│       │   ├── coercion.py         # generic coercion framework (NaN/ASCII/atomic rules)
│       │   ├── ramdb_writer.py     # atomic write, directory mgmt
│       │   ├── state.py            # SQLite state store
│       │   ├── daemon.py           # async event loop, scheduler, worker pool
│       │   ├── health.py           # HTTP health endpoint
│       │   ├── metrics.py          # Prometheus exposition (optional)
│       │   ├── logging.py          # structured JSON logging
│       │   └── systemd.py          # install-systemd command
│       └── drivers/
│           ├── __init__.py         # driver registry; resolves dialect_name → class
│           └── postgres/
│               ├── __init__.py
│               ├── driver.py       # PostgresDriver(Driver) implementation
│               └── coercion.py     # PG-specific type mappings from §6.1
├── tests/
│   ├── conftest.py                 # pytest fixtures (test postgres via testcontainers)
│   ├── core/
│   │   ├── test_coercion_framework.py   # generic NaN/ASCII/atomic rules
│   │   ├── test_config.py
│   │   ├── test_state.py
│   │   ├── test_ramdb_writer.py         # atomicity, tempfile cleanup
│   │   ├── test_daemon.py
│   │   └── test_health.py
│   ├── drivers/
│   │   └── postgres/
│   │       ├── test_coercion.py         # every type in §6.1 has a unit test
│   │       └── test_driver.py           # connect, discover, pull
│   └── e2e/
│       └── test_postgres_to_ramdb.py    # full daemon against real postgres (testcontainers)
├── examples/
│   ├── minimal.yaml                # single table, full_refresh
│   ├── incremental.yaml            # multi-table with watermarks
│   └── production.yaml             # all features, commented
└── scripts/
    └── dev_postgres.sh             # spin up a local postgres for testing
```

---

## 13. Acceptance criteria

The implementation is considered v0.1 complete when **all of the following pass** on Kos's Custom PC (Manjaro Linux, dual 3060, Row64 Server installed):

1. `pip install -e .` from the repo root succeeds
2. `r64-db-engine validate --config examples/minimal.yaml` connects to a local Postgres, discovers schema, and exits 0
3. `r64-db-engine run --once --config examples/minimal.yaml` produces a valid `.ramdb` file in the loading directory that Row64 Server picks up within 60s
4. `r64-db-engine run --config examples/incremental.yaml` runs as a daemon for at least 5 minutes without crashing, with at least one incremental pull observed pulling 0 new rows (steady state) and at least one pulling >0 rows (after a test insert)
5. `curl localhost:8765/health` returns valid JSON with all tables `ok`
6. Kill the daemon mid-pull (SIGTERM) — no `.ramdb.tmp.*` files left in the loading directory
7. Delete `state.db` and restart — daemon re-pulls everything and re-establishes watermarks without manual intervention
8. Introduce a deliberate type-coercion edge case (e.g., a `numeric(20,5)` column, a `jsonb` column with nested arrays, a `text` column with em-dashes) — verify the ramdb loads back correctly via `row64tools.ramdb.load_to_df()`
9. `pytest` runs with all unit tests passing (integration tests gated behind `--integration` flag requiring a test Postgres)
10. `ruff check` and `mypy src/` both pass with zero errors

---

## 14. Out of scope (explicitly deferred to later versions)

- v0.2: Schema discovery UI (`r64-db-engine discover` is CLI only in v0.1)
- v0.2: Per-row filtering beyond what user can express in inline SQL
- v0.3: CDC mode (logical replication via wal2json/pgoutput) — possible v0.3 if there's demand
- v0.3: Multi-source-database support (multiple Postgres connections in one daemon)
- v1.0: PyPI release with versioned stability guarantees
- v1.0: `row64-redshift-sync` and `row64-clickhouse-sync` siblings sharing a common core library

---

## 15. Implementation notes for Claude Code

### Build order (strict — earlier modules must pass tests before later ones)

1. **`core/driver.py`** — Driver ABC, dataclasses (`TableMetadata`, `ColumnMetadata`, `PullResult`, `ValidationResult`). No logic, just the contract.
2. **`core/coercion.py`** — generic framework: NaN-in-int handling, ASCII sanitization, NaT preservation, atomic-write contract. **Source-agnostic** — no Postgres knowledge here. Tests in `tests/core/test_coercion_framework.py`.
3. **`drivers/postgres/coercion.py`** — every Postgres type from §6.1 as a dispatch dict keyed on psycopg type OID. **Unit-test every type** in §6.1 before moving on (`tests/drivers/postgres/test_coercion.py`).
4. **`drivers/postgres/driver.py`** — `PostgresDriver(Driver)` implementing `connect`, `discover`, `validate_table`, `pull`. Integration tests against testcontainers-python Postgres.
5. **`core/ramdb_writer.py`** — atomic write (tempfile + rename), directory mgmt, permission errors. Tests verify SIGTERM mid-write leaves no `.tmp.*` files.
6. **`core/state.py`** — SQLite watermark + schema + pull_history stores. Tests cover corruption recovery (§9.4).
7. **`core/daemon.py`** — async event loop, per-table scheduler, worker pool. Resolves `dialect:` from config to `DRIVERS[name]`, instantiates the driver. **Imports nothing from `drivers/`** — only from `core/driver.py` and the registry.
8. **`core/health.py`** — HTTP endpoint per §8.2.
9. **`cli.py`** — argparse entry point with all subcommands from §10.

### Hard rules

- **The Driver ABC is the firewall.** `core/` modules never import from `drivers/postgres/`. If a `core/` test requires Postgres specifics, the test belongs in `tests/drivers/postgres/` or `tests/e2e/`, not `tests/core/`.
- **Use `asyncio` throughout.** Per-table scheduling and the health endpoint both want async; `psycopg[binary]` supports async natively. Don't fight the language.
- **Use `testcontainers-python` for integration tests.** Spins up real Postgres in Docker for the test run — far better than mocking the database client.
- **Keep `core/daemon.py` under 300 lines.** If it gets longer, modules are doing too much; split.
- **The README is part of the deliverable.** Quickstart: install → `dev_postgres.sh` to spin up a test database → minimal.yaml → `run --once` → see the ramdb file land. If a new prospect can't follow that in 10 minutes, the README has failed.
- **Reference the row64-ramdb-files skill** for canonical type-handling examples. Especially the ASCII sanitization preprocessor and the NaN-in-int handling — both are battle-tested.
- **Match the row64tools API exactly** — `row64tools.ramdb.save_from_df(df, path)`, no other variants. Other Row64 docs reference `r64.save()` or `row64tools.save()`; these are wrong.
- **Loading directory writes only.** Never write to `live/`. The server promotes from loading to live on its `RAMDB_UPDATE` cycle (default 60s). Document this in the README.

### What "done" looks like for v0.1

Acceptance criteria in §13 must all pass. Beyond that, the proof the architecture is right is this: writing a stub `drivers/redshift/` directory with an empty `RedshiftDriver(Driver)` class should require **zero changes** to anything in `core/`. If `core/` needs to know about Redshift, the abstraction has leaked and the v0.1 PR isn't done.

---

## 16. Open questions (resolve before implementation)

1. **License:** MIT (matches Row64's open ecosystem positioning) — confirm with Marc/Mikhail before public release. Private repo for v0.1 sidesteps this.
2. **Repo home:** `kosmosis12/r64-db-engine` initially. If Marc/Mikhail approve, move to `Row64/r64-db-engine` as a sanctioned integration.
3. **Versioning:** semver from day one. v0.1.0 on first working release, v0.x.y until production-validated, v1.0.0 after Row64 sanctions it.
4. **Telemetry sink:** v0.1 ships stdout JSON only. Whether to add OpenTelemetry traces is a v0.3 question; not relevant yet.

---

**End of spec.** Implementation can begin against this document. Any deviation that affects the §13 acceptance criteria should be flagged before merging.
