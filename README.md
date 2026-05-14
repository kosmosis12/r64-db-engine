# r64-db-engine — Postgres Connector

[![status](https://img.shields.io/badge/status-v0.1-blue)]()
[![driver](https://img.shields.io/badge/driver-postgres-336791)]()
[![license](https://img.shields.io/badge/license-MIT-green)]()
[![python](https://img.shields.io/badge/python-3.11%2B-yellow)]()

A driver-agnostic database ingestion engine that pulls from external sources and lands typed `.ramdb` files for [Row64 Server](https://row64.com) to serve. The Postgres driver is the v0.1 reference implementation; ClickHouse, Redshift, Snowflake, BigQuery, and Databricks slot in as siblings against the same `Driver` ABC.

**Verified throughput:** 50,000 rows of mixed-type data (bigint, jsonb, bytea, timestamptz, numeric(20,5), interval, uuid, double precision[], text) ingested in ~230ms → 5.5MB `.ramdb` → queryable by Row64 Server immediately on write. Byte-identical reproduction across sessions.

---

## Architecture

```
┌──────────────┐      ┌────────────────┐      ┌──────────────────────┐      ┌──────────────┐
│  Postgres    │──►   │ r64-db-engine  │──►   │  /var/www/ramdb/     │──►   │  Row64       │
│  (source)    │      │  + Driver ABC  │      │  live/RAMDB.Row64/   │      │  Server      │
└──────────────┘      └────────────────┘      └──────────────────────┘      └──────────────┘
     psycopg            schema discovery        atomic .ramdb writes          GPU compute,
                        type coercion           direct serving directory      dashboard queries
                        scheduler
```

The engine has **one job**: drop valid `.ramdb` files where Row64 Server can read them. It never reads from the server, never writes back to the source, never queries — pure unidirectional materialization.

The `Driver` ABC is a firewall: `core/` contains zero knowledge of Postgres. Adding ClickHouse means writing `drivers/clickhouse/` against the same contract, with zero changes to `core/`.

---

## Quick Start

### Prerequisites

| Component | Required |
|---|---|
| Python | 3.11+ |
| Row64 Server | Running, with `/var/www/ramdb/` writable |
| Source Postgres | Reachable on TCP (network + auth) |
| OS | Linux (POSIX `os.rename` atomicity assumed) |

### Install

```bash
git clone https://github.com/kosmosis12/r64-db-engine
cd r64-db-engine

python3 -m venv .venv
source .venv/bin/activate

pip install -e .

# Verify
which r64-db-engine
r64-db-engine --help
```

### Test with included dev Postgres

The repo ships a Docker-based dev Postgres on port 5433, seedable with 5 tables × 50K rows covering every major type category:

```bash
bash scripts/dev_postgres.sh start
make seed

# Verify seed
PGPASSWORD=row64dev psql -h localhost -p 5433 -U postgres -d analytics \
  -c "\dt public.*"
```

> ⚠ **Do not `source scripts/dev_postgres.sh`** — it kills the sourced shell. Always run with `bash`.

---

## End-to-End Setup: Postgres → Row64 Server

### Step 1 — Add producer user to the `row64` group

The engine writes as `kos` (or whichever user runs the daemon); Row64 Server reads as `row64`. Both need access via the `row64` group.

```bash
sudo usermod -aG row64 $USER
newgrp row64   # activate in current shell, or log out and back in

id   # confirm row64 appears in groups
```

### Step 2 — Pre-create the target group directory

Row64 Server's serving directory has a fixed layout: `/var/www/ramdb/live/RAMDB.Row64/<Group>/<Table>.ramdb`. Pre-create your group directory with the correct ownership and `setgid` bit so new files inherit the `row64` group:

```bash
GROUP="PostgresSource"   # change to your customer/namespace

sudo mkdir -p /var/www/ramdb/live/RAMDB.Row64/$GROUP
sudo chown $USER:row64   /var/www/ramdb/live/RAMDB.Row64/$GROUP
sudo chmod 2775           /var/www/ramdb/live/RAMDB.Row64/$GROUP

ls -ld /var/www/ramdb/live/RAMDB.Row64/$GROUP
# Expected: drwxrwsr-x  <user>  row64
```

### Step 3 — Register the database in Row64 Server's config

> ⚠ **This is the step most likely to be missed.** Filesystem presence alone isn't sufficient — Row64 Server only serves databases registered in `Connections[].DATABASES`.

```bash
# Always back up first
sudo cp /opt/row64server/conf/config.json \
        /opt/row64server/conf/config.json.bak.$(date +%s)

sudo nano /opt/row64server/conf/config.json
```

Add an entry to the `DATABASES` array inside the `Row64` connection:

```json
"DATABASES": [
  { "DATABASE_NAME": "Examples",       "ALL_TABLES": "TRUE", "TABLES": [] },
  ...existing entries...
  { "DATABASE_NAME": "PostgresSource", "ALL_TABLES": "TRUE", "TABLES": [] }
]
```

Validate JSON **before restarting** — an invalid file will fail server startup:

```bash
sudo python3 -c "import json; json.load(open('/opt/row64server/conf/config.json'))" \
  && echo "JSON OK"
```

Restart Row64 Server:

```bash
sudo systemctl restart row64server.service
sleep 5
sudo systemctl is-active row64server.service
# Expected: active
```

### Step 4 — Write the engine config

The repo ships three reference configs in `examples/`:

| File | Use |
|---|---|
| `examples/minimal.yaml` | Smallest config; dev container only |
| `examples/production.yaml` | Env-driven template with every feature exercised |
| `examples/cachyos-live.yaml` | **Canonical config for real Row64 Server installs** |
| `examples/cachyos-live-utf8.yaml` | Regression test for non-ASCII codec issue |

Either copy `examples/cachyos-live.yaml` and edit, or write your own:

```yaml
dialect: postgres

postgres:
  host: localhost
  port: 5433
  database: analytics
  user: postgres
  password: row64dev
  sslmode: disable

row64:
  loading_dir: /var/www/ramdb/live/RAMDB.Row64   # see note below
  group: PostgresSource                            # subdirectory under live/

tables:
  - source: public.customers
    target: Customers
    mode: full_refresh
    cadence: 60s

telemetry:
  log_level: info
  log_format: json
  health_port: 8765

runtime:
  worker_pool_size: 4
  state_dir: /tmp/r64-real-demo-state
  shutdown_grace_seconds: 30
```

> 💡 **Config gotcha — `loading_dir` is misnamed.** The field name implies a `loading/` → `live/` staging model, but on every Row64 Server install observed in the field, the server reads `.ramdb` files from `live/` directly. Point `loading_dir` at `/var/www/ramdb/live/RAMDB.Row64`. A future config version will rename this to `target_dir`.

### Step 5 — Validate the config

```bash
r64-db-engine validate --config examples/cachyos-live.yaml
# Expected: clean exit, no errors
```

Common schema errors:
- `source:` / `target:` as top-level keys instead of `postgres:` / `row64:`
- Missing `dialect:` field
- Credentials inside `tables[]` instead of `postgres:`

### Step 6 — Run the ingest

**One-shot mode** (single pull, exit — useful for testing):

```bash
r64-db-engine run --once --config examples/cachyos-live.yaml 2>&1 | tee /tmp/r64-engine.log
```

Expected log events (JSON, one per line):
- `postgres_connected` — driver connected to source
- `daemon_start` — orchestrator initialized
- `pull_success` with `rows: 50000` and `duration_ms: <250` — table pulled

**Daemon mode** (continuous, honors per-table `cadence:`):

```bash
r64-db-engine run --config examples/cachyos-live.yaml 2>&1 | tee -a /tmp/r64-engine.log
# Ctrl-C to stop; shutdown_grace_seconds governs clean exit
```

### Step 7 — Verify the file landed

```bash
ls -la /var/www/ramdb/live/RAMDB.Row64/PostgresSource/
# Expected: Customers.ramdb at ~5.5MB, owner <user>:row64
```

**No 70-second wait, no promotion cycle.** The file is queryable by Row64 Server the moment the producer finishes writing.

### Step 8 — Verify byte-clean round-trip (optional)

```bash
python3 -c "
from row64tools import ramdb
df = ramdb.load_to_df('/var/www/ramdb/live/RAMDB.Row64/PostgresSource/Customers.ramdb')
print(f'shape: {df.shape}')
print(df.dtypes)
print(df.head(3))
"
```

Expected: shape `(50000, 6)`, schema preserved (id int32, name str, region str, plan str, notes str, updated_at datetime64[ns]).

---

## Production Deployment

### Run as a systemd service

`/etc/systemd/system/r64-db-engine.service`:

```ini
[Unit]
Description=Row64 DB Engine — Postgres ingestion
After=network.target row64server.service
Wants=row64server.service

[Service]
Type=simple
User=kos
Group=row64
WorkingDirectory=/home/kos/builds/r64-db-engine
EnvironmentFile=/etc/r64-db-engine/postgres.env
ExecStart=/home/kos/builds/r64-db-engine/.venv/bin/r64-db-engine run --config /etc/r64-db-engine/config.yaml
Restart=on-failure
RestartSec=10
KillSignal=SIGTERM
TimeoutStopSec=45

[Install]
WantedBy=multi-user.target
```

Enable + start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now r64-db-engine.service
sudo systemctl status r64-db-engine.service
journalctl -u r64-db-engine.service -f
```

### Env-driven secrets

For production, never commit credentials. Use `${VAR}` interpolation in YAML and provide env via `EnvironmentFile=`:

```yaml
postgres:
  host: ${PG_HOST}
  port: ${PG_PORT}
  database: ${PG_DATABASE}
  user: ${PG_USER}
  password: ${PG_PASSWORD}
```

`/etc/r64-db-engine/postgres.env` (mode 0600):
```
PG_HOST=postgres.internal
PG_PORT=5432
PG_DATABASE=production
PG_USER=ingest_reader
PG_PASSWORD=...
```

### Health endpoint

The engine exposes a JSON health endpoint on `telemetry.health_port` (default 8765):

```bash
curl http://localhost:8765/health
```

Returns last-pull timestamps, watermark state, and rolling error counts per table.

### Prometheus metrics

Set `telemetry.metrics_port: 9100` to enable Prometheus exposition. Metrics include pull duration histograms, rows-per-pull counters, and error counts by table.

---

## Configuration Reference

### Top-level structure

```yaml
dialect: postgres        # required; only "postgres" in v0.1
postgres: { ... }        # required; driver-specific connection block
row64:    { ... }        # required; target directory + group namespace
defaults: { ... }        # optional; per-table defaults
tables:   [ ... ]        # required; list of source→target mappings
telemetry: { ... }       # optional
runtime:  { ... }        # optional
```

### `postgres` block

```yaml
postgres:
  host: localhost                       # default: localhost
  port: 5432                            # default: 5432
  database: analytics                   # required
  user: postgres                        # required
  password: ${PG_PASSWORD}              # optional; falls back to PGPASSWORD env or ~/.pgpass
  sslmode: disable                      # disable|allow|prefer|require|verify-ca|verify-full
  application_name: r64-db-engine       # shows up in pg_stat_activity
  connect_timeout: 10                   # seconds
  statement_timeout: 300                # seconds, per query
```

### `row64` block

```yaml
row64:
  loading_dir: /var/www/ramdb/live/RAMDB.Row64   # target directory; see config gotcha above
  group: PostgresSource                          # subdirectory under loading_dir
```

### `tables` array

Each entry defines one source → target mapping.

**Full refresh:**
```yaml
- source: public.customers       # schema.table OR inline SELECT (see below)
  target: Customers              # name of the .ramdb file (without extension)
  mode: full_refresh             # full_refresh | incremental
  cadence: 60s                   # Ns | Nm | Nh; minimum 5s
```

**Incremental (timestamp-keyed):**
```yaml
- source: public.orders
  target: Orders
  mode: incremental
  incremental_key: updated_at
  cadence: 60s
```

**Incremental (integer-keyed for append-only streams):**
```yaml
- source: public.events
  target: Events
  mode: incremental
  incremental_key: event_id
  incremental_type: int
  cadence: 30s
```

**Inline SQL aggregation:**
```yaml
- source: |
    SELECT region, plan, COUNT(*)::BIGINT AS n, MAX(updated_at) AS last_seen
    FROM public.customers
    GROUP BY 1, 2
  target: CustomersByRegion
  mode: full_refresh
  cadence: 15m
```

### `defaults` block

Applied to any table that doesn't override the field.

```yaml
defaults:
  cadence: 60s                # default pull frequency
  mode: full_refresh          # full_refresh | incremental
  max_rows: null              # null = uncapped; integer to cap each pull
  ascii_sanitize: true        # see "Known Limitations" below
```

### `telemetry` block

```yaml
telemetry:
  log_level: info             # debug | info | warning | error
  log_format: json            # json | text
  health_port: 8765           # 0 to disable
  metrics_port: 9100          # 0 to disable Prometheus exposition
```

### `runtime` block

```yaml
runtime:
  worker_pool_size: 4         # concurrent table pulls (1..64)
  state_dir: ~/.r64-db-engine # SQLite state location
  shutdown_grace_seconds: 30  # SIGTERM grace period before SIGKILL
```

---

## Known Limitations

### Non-ASCII text characters are corrupted or crash the pull

⚠ **Structural limitation in the `.ramdb` binary format codec.**

The `.ramdb` format is ASCII-only at the codec level (~50 hardcoded `.encode('ascii')` and `str(..., 'ascii')` call sites in `row64tools/bytestream.py` and `ramdb.py`). The engine's `ascii_sanitize` setting controls how this surfaces:

| Setting | Behavior |
|---|---|
| `ascii_sanitize: true` (default) | Engine pre-replaces non-ASCII characters with `?` before reaching codec. No crash, but **silent data loss**. |
| `ascii_sanitize: false` | Engine passes UTF-8 through; codec crashes on first non-ASCII character: `'ascii' codec can't encode character '\u2014'`. **No data loss, but no output.** |

Affected characters: em-dashes (`—`), smart quotes (`'` `"` `"` `'`), accented names (José, François, Müller), currency (€, £, ¥), units (°, ², ³, ½), bullet points (•), copyright/trademark (©, ™), and **all** non-Latin scripts (CJK, Cyrillic, Arabic, Hebrew, Hindi, Vietnamese, Thai, Greek).

**Regression test:** `examples/cachyos-live-utf8.yaml` reproduces the crash deterministically. Once the codec gains UTF-8 support, that config should pass with non-ASCII characters preserved.

**Status:** Triage queued with Row64 platform team. Tracking under v1.0 milestone.

### Row64 Server promotion model

The public Row64 documentation describes a `loading/` → `live/` promotion model where producers write to `loading/` and Row64 Server promotes files via the `RAMDB_UPDATE` cycle. **Field validation found this model not operational** on tested installs — Row64 Server reads `.ramdb` files from `live/` directly, with no observable promotion daemon. The engine writes to `live/` accordingly (see config gotcha in Step 4).

### `dev_postgres.sh` source-poisoning bug

`source scripts/dev_postgres.sh` kills the sourced shell. Always run with `bash scripts/dev_postgres.sh`. Fix queued.

### `pandas.read_sql` SQLAlchemy warning

A DeprecationWarning about SQLAlchemy connection objects surfaces during pulls. Warning only; no functional impact. Fix queued.

---

## Adding a New Driver

The Driver firewall pattern means new sources are isolated additions:

```
src/r64_db_engine/
├── core/                       # source-agnostic, never changes
│   ├── config.py               # add <NewSource>Config Pydantic model
│   ├── daemon.py
│   └── driver.py               # Driver ABC
├── drivers/
│   ├── __init__.py             # DRIVERS registry
│   ├── postgres/               # v0.1 reference implementation
│   │   ├── driver.py
│   │   └── coercion.py
│   └── clickhouse/             # new driver lives here, fully self-contained
│       ├── driver.py
│       └── coercion.py
```

A new driver:
1. Subclasses `Driver` from `core/driver.py`
2. Adds its config block to `core/config.py` (e.g., `clickhouse:` parallel to `postgres:`)
3. Implements `connect`, `discover`, `validate_table`, `pull` for its source type
4. Builds `coercion.py` mapping the source's type system to ramdb-compatible pandas dtypes
5. Registers itself in `drivers/__init__.py` keyed by `dialect:`

`core/` should never gain a `if dialect == "postgres" else ...` branch. The firewall audit:

```bash
grep -rn "from r64_db_engine.drivers" src/r64_db_engine/core/ tests/core/ \
  && echo "❌ FIREWALL LEAK" || echo "✅ FIREWALL HOLDS"
```

### Roadmap

| Driver | Status | Notes |
|---|---|---|
| Postgres | ✅ v0.1 shipped | This connector |
| ClickHouse | 🚧 queued (driver #2) | Performance-buyer ICP overlap; local testing trivial |
| Redshift | 📋 planned | Postgres-wire-compatible, ~80% reuse |
| Snowflake | 📋 planned | High-priority enterprise gap |
| BigQuery | 📋 planned | High-priority enterprise gap |
| Databricks | 📋 planned | Most complex auth (IAM + token + PAT variants) |

---

## Repository Structure

```
r64-db-engine/
├── README.md                              # this file
├── SPEC.md                                # canonical architectural contract
├── Makefile                               # dev-up, seed, test, demo, clean
├── pyproject.toml
├── src/r64_db_engine/
│   ├── cli.py
│   ├── core/                              # source-agnostic; no driver imports
│   │   ├── driver.py                      # Driver ABC + dataclasses
│   │   ├── config.py                      # Pydantic models + YAML loader
│   │   ├── coercion.py                    # generic NaN/ASCII framework
│   │   ├── ramdb_writer.py                # atomic write
│   │   ├── state.py                       # SQLite watermark + pull history
│   │   ├── daemon.py                      # async scheduler, worker pool
│   │   ├── health.py                      # HTTP health endpoint
│   │   ├── metrics.py                     # Prometheus (optional)
│   │   └── logging.py                     # structured JSON logging
│   └── drivers/
│       ├── __init__.py                    # DRIVERS registry
│       └── postgres/
│           ├── driver.py                  # PostgresDriver(Driver)
│           └── coercion.py                # PG type → pandas dtype mapping
├── examples/
│   ├── minimal.yaml                       # smallest config; dev container only
│   ├── production.yaml                    # env-driven template
│   ├── incremental.yaml                   # watermark-keyed pulls
│   ├── cachyos-live.yaml                  # canonical config for real Row64 Server
│   └── cachyos-live-utf8.yaml             # regression test for non-ASCII codec
├── scripts/
│   ├── dev_postgres.sh                    # ephemeral test Postgres on :5433
│   └── seed_postgres.py                   # 5 tables × 50K rows
└── tests/
    ├── core/                              # unit tests (no Docker required)
    └── e2e/                               # integration tests via testcontainers
```

---

## Testing

### Unit tests (no Docker required)

```bash
source .venv/bin/activate
pytest -v --ignore=tests/e2e
```

148 tests cover: config parsing, coercion, ramdb writing, state management, daemon scheduling, health endpoint, atomic write semantics, SIGTERM cleanup, corruption recovery.

### Integration tests (requires Docker)

```bash
pytest -v tests/e2e --integration
```

Uses `testcontainers` to spin up ephemeral Postgres instances per test.

### Firewall audit

```bash
grep -rn "import.*drivers" src/r64_db_engine/core/ && echo LEAK || echo HOLDS
```

### Demo loop

```bash
make demo
# dev-up → seed → run --once → verify .ramdb file → clean
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `command not found: r64-db-engine` | venv not activated | `source .venv/bin/activate` |
| `permission denied` writing target dir | User not in `row64` group, or dir not pre-created with mode 2775 | Re-run Step 1 + 2 of setup; verify `newgrp row64` |
| `validate` errors mentioning `source/target` | YAML uses wrong top-level keys | Use `dialect`, `postgres`, `row64`, `tables` — not `source`, `target` |
| File lands but server doesn't see it | Database not registered in `config.json` `Connections.DATABASES` | Re-run Step 3, then `systemctl restart row64server.service` |
| Server fails to restart after config edit | Invalid JSON in `config.json` | `sudo cp config.json.bak.<timestamp> config.json`, validate JSON, restart |
| `'ascii' codec can't encode character` | `ascii_sanitize: false` + non-ASCII in source | Set `ascii_sanitize: true` (lossy) OR wait for codec UTF-8 support |
| `dev_postgres.sh` kills shell | Used `source` instead of `bash` | `bash scripts/dev_postgres.sh start` |
| Postgres container won't start | Stale container with same name | `docker rm -f r64-db-engine-pg` then re-run script |
| em-dashes appear as `?` in live data | `ascii_sanitize: true` default; codec is ASCII-only | Known limitation; see above |
| `loading_dir` errors as path | Field name is misleading | Point at `/var/www/ramdb/live/RAMDB.Row64`, not `/loading/...` |

---

## Performance

Verified on cachyos-kos (Intel i7-14700K, Crucial DDR5 32GB, dual MSI RTX 3060 12GB, NVMe SSD):

| Metric | Value |
|---|---|
| Throughput | ~211,000 rows/sec sustained |
| Pull duration (50K mixed types) | ~230ms |
| Output size (50K rows, 6 columns mixed types) | 5,522,861 bytes (5.27 MiB) |
| Memory footprint (steady state, 1 table) | ~80MB |
| Cold start to first pull | <500ms |
| Reproducibility | Byte-identical across sessions, server restarts, reboots |

Scaling characteristics:
- Pulls are **embarrassingly parallel** across tables (bounded by `worker_pool_size`)
- Type coercion is the hot path (~60% of pull duration on mixed-type tables); pure-numeric tables can hit 400K+ rows/sec
- Memory grows with row count × column width; full refresh of 1M rows × 10 columns typical = ~600MB peak
- Network bandwidth and source DB throughput are the practical ceilings, not the engine

---

## License

MIT — see `LICENSE` file.

---

## Contributing

PRs welcome. Before submitting:

1. Run the firewall audit — `core/` must not import from `drivers/`
2. Add unit tests for any new coercion logic
3. Update `references/coercion.md` if extending the Postgres type table
4. Run `ruff` and `mypy` clean
5. Ensure `pytest -v --ignore=tests/e2e` passes

For new driver contributions, follow the templated process in [Adding a New Driver](#adding-a-new-driver) and consult `SPEC.md` for the canonical Driver ABC contract.

---

## Acknowledgments

The Driver ABC pattern draws from Apache Superset's `db_engine_specs/` architecture. The `.ramdb` format and `row64tools` Python bindings are products of [Row64](https://row64.com).
