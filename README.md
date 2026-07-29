# r64-db-engine - Database Ingestion for Row64

[![Build status](https://img.shields.io/github/actions/workflow/status/kosmosis12/r64-db-engine/ci.yml?branch=main&label=build)](https://github.com/kosmosis12/r64-db-engine/actions)
[![Driver: postgres](https://img.shields.io/badge/driver-postgres-336791)](src/r64_db_engine/drivers/postgres)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](#license)
[![Python: 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](pyproject.toml)
[![Latest audit: 2026-05-27](https://img.shields.io/badge/latest%20audit-2026--05--27-orange)](REVIEW-postgres-2026-05-27.md)

`r64-db-engine` is the Postgres driver of a driver-agnostic ingestion engine that materializes external database tables into typed Row64 `.ramdb` files. The v0.1 Postgres path is audit-hardened and production-ready for supervised Postgres-to-Row64 ingestion.

## What's Verified

- Tests: 155 unit tests passing, 106 integration tests passing, 0 xfails.
- Throughput: 137,670 rows/sec end-to-end on the audit demo path, serialization-bound.
- Postgres type coverage: `smallint`, `int2`, `integer`, `int`, `int4`, `bigint`, `int8`, `smallserial`, `serial`, `bigserial`, `oid`, `real`, `float4`, `double precision`, `float8`, `numeric`, `decimal`, `text`, `varchar`, `character varying`, `char`, `character`, `bpchar`, `name`, `citext`, `boolean`, `bool`, `date`, `timestamp`, `timestamp without time zone`, `timestamptz`, `timestamp with time zone`, `time`, `time without time zone`, `timetz`, `time with time zone`, `interval`, `uuid`, `json`, `jsonb`, `bytea`, arrays including `integer[]` and `text[]`, `inet`, `cidr`, `macaddr`, `macaddr8`, `tsvector`, `tsquery`, `geometry`, `geography`, `xml`, `int4range`, `int8range`, `numrange`, `tsrange`, `tstzrange`, and `daterange`.
- Guarded failure modes: `Row64CodecOverflowError` for codec-unsafe integers and intervals, `NumericPrecisionLossError` for non-exact decimals, fail-fast auth failures including `psycopg.errors.InvalidPassword`, SIGTERM tempfile cleanup, identifier-quote escaping, array prepass coercion, equal-watermark pagination, state reset recovery, and connection-loss health degradation.

## Installation

Prerequisites: Docker, Python 3.11+, and `make`.

```bash
git clone git@github.com:kosmosis12/r64-db-engine.git
cd r64-db-engine
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/r64-db-engine version
make help
```

HTTPS clone works too:

## ClickHouse Config Example

```yaml
dialect: clickhouse

clickhouse:
  host: ${CLICKHOUSE_HOST}
  # `port` and `secure` must agree: 8443 is the HTTPS interface, 8123 is the
  # PLAINTEXT one. `secure: true` with 8123 (as this example used to read) sends
  # a TLS handshake to a cleartext listener and fails at connect time. For a
  # local/loopback ClickHouse use `port: 8123` with `secure: false`.
  port: 8443
  database: analytics
  user: ${CLICKHOUSE_USER}
  password: ${CLICKHOUSE_PASSWORD}
  secure: true
  connect_timeout: 10

row64:
  loading_dir: /var/www/ramdb/loading/RAMDB.Row64
  group: ClickHouseSource

defaults:
  cadence: 60s
  mode: full_refresh
  ascii_sanitize: true

tables:
  - source: analytics.orders
    target: Orders
    mode: incremental
    incremental_key: updated_at
    cadence: 60s
```

## What you get

```bash
git clone https://github.com/kosmosis12/r64-db-engine.git
```

## Quickstart

Spin up the ephemeral Postgres used by the examples:

```bash
scripts/dev_postgres.sh start
```

Run the demo. It seeds 50K rows per table, validates `examples/minimal.yaml`, runs one pull, and writes under `/tmp/r64-demo`.

```bash
. .venv/bin/activate
make demo
```

Verify the `.ramdb` landed and can be loaded:

```bash
ls -lh /tmp/r64-demo/ramdb/PostgresSource/
.venv/bin/python -c "from row64tools.ramdb import load_to_df; df = load_to_df('/tmp/r64-demo/ramdb/PostgresSource/Customers.ramdb'); print(df.shape); print(df.head(1).to_dict('records')[0])"
```

Clean up local artifacts:

```bash
make clean
```

## Configure Your Own Postgres

Start from `examples/minimal.yaml`:

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
  loading_dir: /tmp/r64-demo/ramdb
  group: PostgresSource

tables:
  - source: public.customers
    target: Customers
    mode: full_refresh
    cadence: 60s

runtime:
  state_dir: /tmp/r64-demo/state
```

Validate and run once:

```bash
cp examples/minimal.yaml /tmp/r64-db-engine-config.yaml
make seed
r64-db-engine validate --config /tmp/r64-db-engine-config.yaml
r64-db-engine run --once --config /tmp/r64-db-engine-config.yaml
```

Run continuously under a supervisor:

```bash
r64-db-engine run --config /tmp/r64-db-engine-config.yaml
```

The CLI on trunk does not have a separate `daemon` subcommand; continuous daemon mode is `run` without `--once`.

## DynamoDB driver

The DynamoDB driver performs read-only Scan or Query operations and materializes
each configured table as a Row64 `.ramdb`. It uses the standard boto3 credential
chain; a profile is optional, and raw access keys are not required in config.

```yaml
dialect: dynamodb

dynamodb:
  region: us-west-2
  endpoint_url: null       # http://localhost:8010 for DynamoDB Local
  profile: null
  consistent_read: false
  scan_segments: 1         # 1-32 parallel Scan segments
  scan_page_limit: null    # optional per-request RCU pacing
  schema_sample_items: 1000

row64:
  loading_dir: /var/lib/row64/loading
  group: DynamoDBSource

tables:
  - source: vehicle_events
    target: VehicleEvents
    mode: incremental
    incremental_key: event_ts
    incremental_type: int
    incremental_mode: gsi_query      # filter_scan or gsi_query
    incremental_gsi: by_bucket_event_ts
    incremental_gsi_partition_value: all

runtime:
  state_dir: /var/lib/r64-db-engine
```

`filter_scan` is correctness-incremental, not cost-incremental. Its filter
reduces returned rows, but DynamoDB charges read capacity for every item read by
the full Scan. It does not reduce consumed RCU. Use `gsi_query` when read cost
matters and the table has the required index shape.

`gsi_query` supports only a single-partition time-series GSI: a constant bucket
is the GSI partition key and the incremental attribute is its sort key.
`incremental_gsi_partition_value` supplies that mandatory equality value.
Enumerating multiple partition values—for example one partition per fleet—is
out of scope, and the driver never silently falls back to Scan.

GSI reads are eventually consistent. The driver therefore advances state to
the maximum watermark actually returned by Query, not to a separately observed
source maximum. A record still propagating into the index remains eligible for
a later pull. Full Scan reads are also eventually consistent unless
`consistent_read: true` is selected; strong reads double the RCU cost.

The minimum table-scoped read policy is:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["dynamodb:DescribeTable", "dynamodb:Scan", "dynamodb:Query",
               "dynamodb:DescribeTimeToLive"],
    "Resource": ["arn:aws:dynamodb:*:*:table/<TableName>",
                 "arn:aws:dynamodb:*:*:table/<TableName>/index/*"]
  }, {
    "Effect": "Allow",
    "Action": "dynamodb:ListTables",
    "Resource": "*"
  }]
}
```

`ListTables` cannot be table-scoped. Omit its separate `Resource: "*"`
statement when account-wide discovery is not allowed and configure tables
explicitly.

### DynamoDB Local quickstart

The helper defaults to port 8010, refuses to start when another process owns
the selected port, and writes the authoritative endpoint and local credentials
to `~/.r64-db-engine/dynamodb-dev.env`.

```bash
make dev-dynamodb-up
. ~/.r64-db-engine/dynamodb-dev.env
make seed-dynamodb
.venv/bin/python -m pytest --integration -s tests/integration/test_dynamodb_local.py
```

Or run the 50K full-refresh proof alone:

```bash
make demo-dynamodb
```

The fixtures are intentionally ephemeral and reseeded on restart. On the Gate
4 verification host, eight workers seeded and exact-verified 50K in-memory
items in 4.78 seconds. The old SQLite-backed `-sharedDb` mode took 1429.86
seconds (~35 rows/sec), because its single writer serialized the workers.
Seeding remains setup work; connector performance is measured separately.

The byte-correct 50K connector proof took 2.242335 seconds, or 22,298.186
rows/sec, measured end-to-end from pull start through real `row64tools` write
and `load_to_df` verification. This number includes serialization and is not a
producer-only Scan rate.

DynamoDB Streams and Kinesis CDC belong in `row64stream` and are out of scope.
Write-back, PartiQL, multi-account assume-role orchestration, and point-in-time
export through S3 are also not implemented; the S3 bulk path is a possible
future enhancement for very large tables.

## Production Operation

- Health: the daemon exposes `GET /health` on `telemetry.health_port` and `r64-db-engine status --health-url http://localhost:8765/health` prints the same status. Healthy or degraded states return HTTP 200; error states return HTTP 503.
- SIGTERM: `run` installs SIGTERM/SIGINT handlers, waits up to `runtime.shutdown_grace_seconds` for in-flight pulls, and the writer removes mid-write tempfiles before exit.
- systemd: generate the unit with `r64-db-engine install-systemd --dry-run --config /etc/r64-db-engine/config.yaml`, or install it with `sudo r64-db-engine install-systemd --user row64 --group row64 --config /etc/r64-db-engine/config.yaml`. See [docs/operator/row64-server-deployment.md](docs/operator/row64-server-deployment.md).
- Password precedence: config `postgres.password` wins, then `PGPASSWORD`, then libpq `~/.pgpass`; missing or invalid credentials fail fast instead of looping forever. See [SPEC.md](SPEC.md#44-auth-fallback-chain).

## Error Semantics

- `Row64CodecOverflowError`: raised before serialization when an integer-like output, including `bigint` or `interval` microseconds, would overflow the installed Row64 codec's signed-int32-safe range.
- `NumericPrecisionLossError`: raised when a Postgres `numeric`/`decimal` value cannot round-trip exactly through the current float64-compatible output path.
- `psycopg.errors.InvalidPassword`: surfaced as a permanent startup failure through the Postgres driver's fail-fast connection check.
- Connection loss during daemon pulls: marked as source disconnected; `/health` reports overall `status: "error"` and `postgres.connected: false` until the process reconnects or is restarted.

## Audit Evidence

This driver was hardened by an adversarial audit on 2026-05-27. See [REVIEW-postgres-2026-05-27.md](REVIEW-postgres-2026-05-27.md) for full findings, reproducers, and verification results. 11 of 13 findings closed in PR #5; PG-010 and PG-011, the architecture firewall refactor, were deferred.

## Where To Go Next

| If you want to... | Read |
|---|---|
| Run against your own Postgres | [docs/quickstart.md](docs/quickstart.md) |
| Deploy to real Row64 Server | [docs/operator/row64-server-deployment.md](docs/operator/row64-server-deployment.md) |
| Understand the architecture | [docs/architecture.md](docs/architecture.md) |
| Hit a snag | [docs/operator/troubleshooting.md](docs/operator/troubleshooting.md) |
| Add a new driver | [docs/adding-a-driver.md](docs/adding-a-driver.md) |

## Roadmap

Postgres is the v0.1 driver. Planned sibling drivers are ClickHouse, Redshift, BigQuery, Snowflake, and Databricks; start with [docs/adding-a-driver.md](docs/adding-a-driver.md) if you want to build one against the same Driver ABC.

## License

MIT
