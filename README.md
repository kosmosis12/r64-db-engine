# r64-db-engine

A supervised daemon that continuously materializes external-database
tables into Row64 Server's loading directory as `.ramdb` files.
One YAML config and a Postgres connection string is all it needs.

This repo is an **engine**, not a Postgres-only tool. Postgres is the
first driver (v0.1); Redshift, ClickHouse, BigQuery, Snowflake and
Databricks plug in behind the same `Driver` ABC in future versions.

See `SPEC.md` for the canonical contract.

## What it does

```
┌──────────────┐    SQL pulls    ┌──────────────────┐    .ramdb writes    ┌─────────────────┐
│  PostgreSQL  │ ◄────────────── │  r64-db-engine   │ ──────────────────► │  Row64 Server   │
│  (source)    │  watermarked    │     daemon       │  atomic rename      │  loading/ → live/│
└──────────────┘                 └──────────────────┘                     └─────────────────┘
```

Per configured table, on its own cadence:

1. Pull from Postgres (with `WHERE incremental_key > last_watermark` if incremental).
2. Apply ramdb-safe coercion: NaN-in-int → 0, optional ASCII sanitization,
   timezone strip, jsonb/array/bytea serialization (full table in `SPEC.md` §6.1).
3. Atomically write `loading_dir/group/Target.ramdb` (tempfile + rename).
4. Update SQLite-backed watermark + pull history; surface health on `/health`.

If the daemon dies mid-write, no half-written `.ramdb` ever appears in
`loading/`. If `state.db` is missing or corrupt, watermarks are
re-established on the next pull cycle.

## Quickstart (10 minutes)

```bash
# 1. Install
git clone git@github.com:kosmosis12/r64-db-engine.git
cd r64-db-engine
pip install -e .

# 2. Spin up a local Postgres for testing
./scripts/dev_postgres.sh start
# (prints connection details; defaults: localhost:55432, user=postgres, password=row64dev)

# 3. Seed it with a sample table
docker exec -i r64-db-engine-pg psql -U postgres -d analytics <<'SQL'
CREATE TABLE IF NOT EXISTS public.customers (
    id BIGINT PRIMARY KEY,
    name TEXT,
    region TEXT,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
INSERT INTO public.customers VALUES
    (1, 'Acme', 'us-west', NOW()),
    (2, 'Globex', 'us-east', NOW())
ON CONFLICT DO NOTHING;
SQL

# 4. Point examples/minimal.yaml at a writable loading dir
mkdir -p /tmp/r64-loading
cp examples/minimal.yaml /tmp/r64-config.yaml
sed -i 's|/var/www/ramdb/loading/RAMDB.Row64|/tmp/r64-loading|' /tmp/r64-config.yaml
sed -i 's|host: localhost|host: localhost\n  port: 55432|' /tmp/r64-config.yaml
sed -i 's|database: analytics|database: analytics|' /tmp/r64-config.yaml
sed -i 's|user: row64_reader|user: postgres|' /tmp/r64-config.yaml

# 5. Validate, then run once
PG_PASSWORD=row64dev r64-db-engine validate --config /tmp/r64-config.yaml
PG_PASSWORD=row64dev r64-db-engine run --once --config /tmp/r64-config.yaml

# 6. You should see Customers.ramdb in the loading dir
ls -l /tmp/r64-loading/PostgresSource/
```

On a real Row64 Server install, `loading_dir` points at
`/var/www/ramdb/loading/RAMDB.Row64`. The server promotes files from
`loading/` to `live/` on its `RAMDB_UPDATE` cycle (default 60 s) — the
daemon never writes to `live/` directly.

## Config

See `examples/production.yaml` for every option commented inline. The
short version:

```yaml
dialect: postgres

postgres:
  host: ${PG_HOST}
  database: analytics
  user: ${PG_USER}
  password: ${PG_PASSWORD}

row64:
  loading_dir: /var/www/ramdb/loading/RAMDB.Row64
  group: PostgresSource

tables:
  - source: public.orders
    target: Orders
    mode: incremental
    incremental_key: updated_at
    cadence: 60s
```

`${VAR}` references resolve from the process env at startup. Missing
required vars fail-fast with a clear error.

### Auth fallback

1. `password:` in config (with env substitution)
2. `PGPASSWORD` env var
3. `~/.pgpass`
4. Fail-fast pointing to all three.

## Operational modes

- **`full_refresh`** — pulls the entire result set every cadence tick,
  overwrites the target. Use for small dimensions, inline-SQL
  aggregations, and any table without a usable incremental key.
- **`incremental`** — pulls `WHERE incremental_key > last_watermark`
  and merges with the existing ramdb. Use for append-mostly fact
  tables. `incremental_type: int` for integer keys, `timestamp`
  (default) for `timestamp`/`timestamptz`.

For tables above ~5M rows where incremental merging gets expensive,
prefer `full_refresh` with a windowing `WHERE` clause in inline SQL.

## Health

```
GET http://localhost:8765/health
```

Returns 200 with a JSON body when `ok` or `degraded`, 503 when
`error`. Schema is documented in SPEC §8.2; quick summary:

- `ok` — every table succeeded within 3× its cadence
- `degraded` — 1–2 consecutive failures on any table, or schema drift
- `error` — 3+ consecutive failures on any table, or Postgres down

For a human-readable view:

```bash
r64-db-engine status
```

## CLI

```
r64-db-engine run [--config PATH] [--once]
r64-db-engine validate [--config PATH]
r64-db-engine discover [--config PATH] [--schema SCHEMA]
r64-db-engine status [--health-url URL]
r64-db-engine install-systemd [--user row64] [--group www-data] [--config PATH] [--dry-run]
r64-db-engine version
```

`install-systemd` writes a unit at
`/etc/systemd/system/r64-db-engine.service` but does not enable or
start it — you do that with `systemctl`.

## Type coercion

The full Postgres → pandas → ramdb mapping lives in `SPEC.md` §6.1.
Highlights:

- `numeric`/`decimal` → `float64` with a precision-loss warning if the
  cast loses more than 0.0001.
- `jsonb`/arrays/`bytea` → compact JSON or hex string; values above 64 KB
  emit a warning.
- `timestamptz` → `datetime64[ns]` after UTC normalization and tz strip
  (ramdb has no tz-aware timestamp type).
- NaN in integer columns is replaced with `0` (NaN forces float
  promotion which crashes ramdb serialization).
- ASCII sanitization (`ascii_sanitize: true`, default) replaces
  em-dashes, smart quotes, accented chars and emoji with `?`. Set
  `ascii_sanitize: false` per-table if you accept the risk.

## Adding a driver

The architectural rule: `core/` never imports from `drivers/`.
Subclass `r64_db_engine.core.driver.Driver`, register the class in
`r64_db_engine.drivers.__init__.DRIVERS`, and the daemon picks it up
by `dialect:` name. Postgres is the v0.1 reference implementation.

## Development

```bash
pip install -e ".[dev]"
ruff check src tests
mypy src
pytest                    # unit tests
pytest --integration      # also runs testcontainers-backed Postgres tests
```

## License

MIT.
