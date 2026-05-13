# r64-db-engine

[![CI](https://github.com/kosmosis12/r64-db-engine/actions/workflows/ci.yml/badge.svg)](https://github.com/kosmosis12/r64-db-engine/actions/workflows/ci.yml)

A supervised daemon that continuously materializes external-database tables
into Row64 Server's loading directory as `.ramdb` files. One YAML config and
a Postgres connection string is all it needs.

## What it is, what it solves

Row64's external-DB integration model — per-table Python scripts, hand-rolled
`.env` files, cron, no health surface, silently-stale ramdbs — turns a "should
be 10 minutes" integration into a half-day-per-prospect project.

`r64-db-engine` is the operational layer between Postgres and Row64. Point it
at a database, list the tables you want, and on each cadence tick it:

1. Pulls from Postgres (with `WHERE incremental_key > last_watermark` if
   incremental).
2. Applies ramdb-safe coercion: NaN-in-int filled, optional ASCII sanitization,
   tz strip, `jsonb`/`array`/`bytea`/`uuid`/`interval` serialization
   (full table in [`SPEC.md`](SPEC.md) §6.1).
3. Atomically writes `loading_dir/group/Target.ramdb` (tempfile + rename in
   the same directory — Row64 Server never sees a half-written file).
4. Updates a SQLite-backed watermark and pull-history store; surfaces health
   on `/health` and (optionally) Prometheus metrics on `/metrics`.

It is **not** a query layer, not a write-back path, not a general ETL tool,
not a multi-source orchestrator. One daemon, one Postgres source, `.ramdb`
files out the other side.

This repo is an **engine**, not a Postgres-only tool. Postgres is the first
driver (v0.1); Redshift, ClickHouse, BigQuery, Snowflake, Databricks plug in
behind the same `Driver` ABC.

```
┌──────────────┐    SQL pulls    ┌──────────────────┐    .ramdb writes    ┌─────────────────┐
│  PostgreSQL  │ ◄────────────── │  r64-db-engine   │ ──────────────────► │  Row64 Server   │
│  (source)    │  watermarked    │     daemon       │  atomic rename      │  loading/ → live/│
└──────────────┘                 └──────────────────┘                     └─────────────────┘
```

---

## Quickstart (10 minutes, fresh clone)

Prerequisites: Python 3.11+, Docker, GNU make.

```bash
git clone git@github.com:kosmosis12/r64-db-engine.git
cd r64-db-engine
pip install -e ".[dev]"

# One-shot: start ephemeral postgres, seed 50K rows across 5 tables,
# run the daemon once, verify the ramdb file exists, tear down.
make demo
```

On success the last line of `make demo` is a `ls` of the produced ramdb
files under `/tmp/r64-demo/ramdb/PostgresSource/`.

### Step-by-step (if you want to keep the test DB running)

```bash
make dev-up       # docker run postgres:16 on localhost:5433 (db=analytics)
make seed         # 50K rows per table covering every type category in SPEC §6.1
r64-db-engine validate --config examples/minimal.yaml
r64-db-engine run --once --config examples/minimal.yaml
ls -l /tmp/r64-demo/ramdb/PostgresSource/   # Customers.ramdb
make clean        # stop docker, remove /tmp/r64-demo
```

`examples/minimal.yaml` and `examples/incremental.yaml` are hard-coded for the
seeded test DB (`localhost:5433`, user `postgres`, password `row64dev`,
database `analytics`) so they run as-is once `make dev-up && make seed` has
completed. `examples/production.yaml` is an env-driven template; copy it and
edit for real deployments.

---

## Configuration reference

The full config schema is `r64_db_engine.core.config.Config` — every field
below maps to a pydantic model in [`src/r64_db_engine/core/config.py`](src/r64_db_engine/core/config.py).
Defaults shown are what the engine uses if you omit the field.

### Top-level

| Field | Required | Default | Notes |
|---|---|---|---|
| `dialect` | yes | — | `postgres` (only option in v0.1) |
| `postgres` | yes | — | see [Postgres](#postgres) |
| `row64` | yes | — | see [Row64](#row64) |
| `defaults` | no | (see below) | per-table fallbacks |
| `tables` | yes | — | list of tables to pull |
| `telemetry` | no | (see below) | logs, health, metrics |
| `runtime` | no | (see below) | worker pool, state, shutdown grace |

### `postgres`

| Field | Required | Default | Notes |
|---|---|---|---|
| `host` | no | `localhost` | |
| `port` | no | `5432` | |
| `database` | yes | — | |
| `user` | no | none | falls back to `PGUSER`/`.pgpass` |
| `password` | no | none | env-substituted; see [Auth fallback](#auth-fallback) |
| `sslmode` | no | `prefer` | `disable` \| `allow` \| `prefer` \| `require` \| `verify-ca` \| `verify-full` |
| `application_name` | no | `r64-db-engine` | shows up in `pg_stat_activity` |
| `connect_timeout` | no | `10` | seconds |
| `statement_timeout` | no | `300` | seconds, applied as `SET LOCAL` per pull |

### `row64`

| Field | Required | Default | Notes |
|---|---|---|---|
| `loading_dir` | yes | — | e.g. `/var/www/ramdb/loading/RAMDB.Row64` |
| `group` | no | `PostgresSource` | subdirectory under `loading_dir` |

Final path per table: `{loading_dir}/{group}/{target}.ramdb`. The daemon
creates `{group}/` if missing, but `loading_dir` must already exist (it
belongs to the Row64 Server install).

### `defaults`

Applied to every entry in `tables` that doesn't override the field.

| Field | Default | Notes |
|---|---|---|
| `cadence` | `60s` | Go-style duration: `5s`, `30s`, `5m`, `1h` — min `5s` |
| `mode` | `full_refresh` | `full_refresh` or `incremental` |
| `max_rows` | `null` | `null` = unbounded; integer caps each pull |
| `ascii_sanitize` | `true` | replace non-ASCII chars with `?` ([SPEC §6.2](SPEC.md)) |

### `tables[]`

| Field | Required | Default | Notes |
|---|---|---|---|
| `source` | yes | — | `schema.table` **or** an inline `SELECT …` |
| `target` | yes | — | ramdb filename (no extension) |
| `mode` | no | `defaults.mode` | `full_refresh` \| `incremental` |
| `incremental_key` | if `mode: incremental` | — | column for `WHERE > watermark` |
| `incremental_type` | no | `timestamp` | `timestamp` \| `int` |
| `cadence` | no | `defaults.cadence` | |
| `max_rows` | no | `defaults.max_rows` | |
| `ascii_sanitize` | no | `defaults.ascii_sanitize` | per-table override |

Inline SQL is detected when `source` starts with `SELECT` (case-insensitive)
or contains a newline. It runs inside `SELECT * FROM (<source>) sub`, so any
valid subquery works.

### `telemetry`

| Field | Default | Notes |
|---|---|---|
| `log_level` | `info` | `debug` \| `info` \| `warning` \| `error` |
| `log_format` | `json` | `json` or `text` |
| `health_port` | `8765` | `0` disables the HTTP health endpoint |
| `metrics_port` | `0` | non-zero starts a Prometheus exporter on that port |

### `runtime`

| Field | Default | Notes |
|---|---|---|
| `worker_pool_size` | `4` | concurrent table pulls (1–64) |
| `state_dir` | `~/.r64-db-engine` | watermark SQLite location |
| `shutdown_grace_seconds` | `30` | SIGTERM grace for in-flight pulls |

### Env-var substitution

Any `${VAR}` in the YAML is replaced from the process environment at
startup. Missing required vars fail fast with a clear list. There is no
`${VAR:-default}` syntax — set the var or hard-code the value.

### Auth fallback

In priority order (per [SPEC §4.4](SPEC.md)):

1. `password:` in config (env-substituted)
2. `PGPASSWORD` env var
3. `~/.pgpass` (libpq standard)
4. fail-fast pointing at all three

---

## Operating the daemon

### CLI surface

```
r64-db-engine run [--config PATH] [--once]
    Start the daemon. --once runs each table exactly once and exits
    (useful for cron-style one-shot integrations or for `make demo`).

r64-db-engine validate [--config PATH]
    Parse config, connect, check every table (or inline SQL) exists and
    that incremental_key is a sensible type. No data fetched.

r64-db-engine discover [--config PATH] [--schema SCHEMA]
    List source tables with row counts and suggested incremental keys.

r64-db-engine status [--health-url URL]
    Hit a running daemon's /health and print a human summary.

r64-db-engine install-systemd [--user USER] [--group GROUP] [--config PATH] [--dry-run]
    Write /etc/systemd/system/r64-db-engine.service. Does not enable
    or start; you do that with systemctl.

r64-db-engine version
```

### Systemd install

```bash
sudo r64-db-engine install-systemd \
    --user row64 \
    --group www-data \
    --config /etc/r64-db-engine/config.yaml
sudo systemctl daemon-reload
sudo systemctl enable --now r64-db-engine
```

The unit runs as `Type=simple` with `Restart=on-failure`, `RestartSec=5s`,
`NoNewPrivileges=true`, `ProtectSystem=full`, `ProtectHome=true`. The
`User=`/`Group=` need write access to `{loading_dir}/{group}/`; the usual
shape is a `row64` system user that's also a member of `www-data` (or
whichever group owns `/var/www/ramdb/`).

`--dry-run` prints the unit file to stdout without writing it.

### Logs

Default: structured JSON to stdout, one event per line. Under systemd these
land in journald — `journalctl -u r64-db-engine -f` follows them. Common
events: `daemon_start`, `pull_success`, `pull_error`, `pull_skipped_overlap`,
`schema_drift`, `postgres_connect_failed`, `full_refresh_large`,
`watermark_advanced`, `daemon_stop`.

Set `telemetry.log_format: text` for a human-readable single-line format
when debugging by eye.

### Health endpoint

```bash
curl http://localhost:8765/health
```

Returns JSON (schema in [SPEC §8.2](SPEC.md)) with overall status, postgres
connectivity, and per-table state including last success time, watermark,
consecutive failure count, and schema-drift flag.

- `200 ok` — every table succeeded inside 3× its cadence
- `200 degraded` — 1–2 consecutive failures on any table, or schema drift
- `503 error` — 3+ consecutive failures on any table, or Postgres unreachable

For a human-readable view from the shell:

```bash
r64-db-engine status
```

### Metrics (optional)

Set `telemetry.metrics_port: 9100` and install with the extras:

```bash
pip install "r64-db-engine[metrics]"
```

Prometheus scrape at `:9100/metrics`. Series exported: `r64_db_engine_pulls_total{target,status}`,
`r64_db_engine_pull_duration_seconds{target}`, `r64_db_engine_rows_pulled_total{target}`,
`r64_db_engine_table_last_success_timestamp_seconds{target}`, `r64_db_engine_postgres_up`,
`r64_db_engine_uptime_seconds`.

---

## Troubleshooting

**`config not found: /etc/r64-db-engine/config.yaml`** — the default config path
doesn't exist. Pass `--config /path/to/your.yaml` explicitly, or copy
`examples/production.yaml` to `/etc/r64-db-engine/config.yaml` and edit.

**`missing required environment variable(s): PG_PASSWORD`** — your config
references `${PG_PASSWORD}` (or similar) but the variable isn't set in the
daemon's environment. Under systemd, put credentials in
`/etc/r64-db-engine/env` and add `EnvironmentFile=/etc/r64-db-engine/env`
to the unit. The Makefile's `dev-up` target prints the exact `export …`
line for local use.

**`loading_dir does not exist: /var/www/ramdb/loading/RAMDB.Row64`** — Row64
Server isn't installed on this host, or it's installed somewhere else. The
daemon will create `{group}/` underneath, but not the loading directory
itself; that's the server's responsibility. Point `row64.loading_dir` at
the right path or stand up the server first.

**`table public.orders does not exist` (from `validate` or `pull_error`)** —
the source name is wrong or the role lacks `USAGE` on the schema /
`SELECT` on the table. Re-run `r64-db-engine discover` to see what the
daemon actually sees, then grant or correct as needed.

**`incremental_key 'updated_at' has type text; timestamp/int recommended`** —
a warning, not an error. The pull will run but watermark comparisons will
be lexicographic; cast to `timestamptz` or pick a different key.

**Daemon starts but `/health` shows every table `pending` and nothing pulls** —
you ran with `health_port: 8765` but `cadence: 60s`; you're probably looking
at the snapshot before the first tick. Wait a cadence interval, or run with
`--once` to force an immediate cycle.

**`PermissionError` writing `Target.ramdb.tmp.…`** — the daemon's `User=`
lacks write to `{loading_dir}/{group}/`. Either chown the directory or add
the daemon user to the group that owns `/var/www/ramdb/`. The atomic-write
protocol writes the tempfile in the **same directory** as the final file
(not `/tmp`) so directory perms are what matters, not `/tmp` perms.

**Watermarks look wrong / I want to re-pull from scratch** — stop the
daemon, delete `~/.r64-db-engine/state.db` (or whatever `runtime.state_dir`
points at), restart. The daemon treats every incremental table as if it's
running for the first time and re-establishes watermarks
([SPEC §9.4](SPEC.md)). No manual SQL needed.

---

## Architecture (overview)

```
src/r64_db_engine/
├── cli.py                          # argparse entry point (§10)
├── core/                           # source-agnostic engine
│   ├── driver.py                   # Driver ABC + dataclasses (§3.1)
│   ├── config.py                   # pydantic models + YAML loader (§4)
│   ├── coercion.py                 # NaN/ASCII/dtype framework (§6.2/§6.3)
│   ├── ramdb_writer.py             # atomic tempfile+rename (§7)
│   ├── state.py                    # SQLite watermarks + pull history (§5.3)
│   ├── daemon.py                   # async scheduler + worker pool (§3, §5, §9)
│   ├── health.py                   # HTTP /health (§8.2)
│   ├── metrics.py                  # Prometheus exposition (§8.3)
│   ├── logging.py                  # structured JSON events (§8.1)
│   └── systemd.py                  # install-systemd command (§10)
└── drivers/
    └── postgres/
        ├── driver.py               # PostgresDriver(Driver)
        └── coercion.py             # PG → pandas dtype table (§6.1)
```

**The Driver ABC is the firewall.** `core/` never imports from `drivers/`.
Adding a new driver (Redshift, ClickHouse, BigQuery, Snowflake, Databricks)
means dropping a directory under `drivers/`, subclassing `Driver`, and
registering it in `drivers/__init__.DRIVERS` — zero changes to `core/`.

Per-table concurrency: one in-flight pull per table; up to
`runtime.worker_pool_size` tables run concurrently. If a cadence tick fires
while a pull is still running for that table, the tick is skipped and logged
as `pull_skipped_overlap`.

Full design rationale, type-coercion table, failure-mode catalogue, and
acceptance criteria live in [`SPEC.md`](SPEC.md).

---

## Type coercion (highlights)

Full mapping in [SPEC §6.1](SPEC.md). The ones that bite people:

- **`numeric`/`decimal` → `float64`** with a precision-loss warning if the
  cast loses more than `0.0001`. Use `bigint`-as-microcents in source if
  exact decimal matters.
- **`jsonb` / arrays / `bytea` → string** (compact JSON or hex); values
  above 64 KB emit a warning.
- **`timestamptz` → `datetime64[ns]`** after UTC normalization and tz strip
  (ramdb has no tz-aware timestamp type).
- **NaN in integer columns** is filled with `0`. NaN forces float
  promotion, which crashes the ramdb serializer.
- **`interval` → `int64` microseconds.** Documented in SPEC so you don't
  rediscover it.
- **`uuid` → `string`** via `str()`.
- **ASCII sanitization** (default `true`) replaces em-dashes, smart quotes,
  accented characters, and emoji with `?`. Opt out per-table with
  `ascii_sanitize: false` if you accept the risk of a serializer crash.

---

## Development

```bash
pip install -e ".[dev]"

make dev-up               # start ephemeral postgres on :5433
make seed                 # 50K rows per table, all type categories

make test                 # unit tests (no docker required after install)
make test-integration     # also runs testcontainers-backed Postgres tests
make demo                 # full end-to-end, ~3-5 minutes

ruff check src tests
mypy src
```

The repo's `scripts/` directory is the source of truth for local
development tooling — `dev_postgres.sh` for the container, `seed_postgres.py`
for the dataset. The Makefile is a thin wrapper.

---

## Project status

**v0.1** — first cut. What works today:

- Postgres driver with the full type table in [SPEC §6.1](SPEC.md)
- `full_refresh` and `incremental` modes (timestamp + int watermarks)
- Inline SQL sources
- Atomic ramdb writes with tempfile cleanup on SIGTERM
- SQLite-backed watermarks, schema-drift detection, pull history
- `/health` JSON endpoint + optional Prometheus metrics
- structured JSON logging
- systemd unit generation
- testcontainers-backed integration + e2e tests

**Deferred** (see [SPEC §14](SPEC.md)):

- Additional drivers (Redshift, ClickHouse, BigQuery, Snowflake, Databricks)
- CDC mode via logical replication
- Multiple Postgres sources in a single daemon
- PyPI release with stability guarantees (currently installed from git)

Track open work and acceptance criteria in [SPEC §13](SPEC.md).

## License

MIT. See [`LICENSE`](LICENSE).
