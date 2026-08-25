# Changelog

All notable changes to r64-db-engine.

## [Unreleased]

### Added
- **Connector descriptor** (`core/descriptor.py`) — one declarative `DriverMetadata`
  per driver, returned from `Driver.descriptor()`. Single source for the registry,
  the cockpit roster, the FORGE-VIEW status projection and the generated docs.
  Superset `db_engine_specs`-derived; see SPEC §3.2 and `docs/conformance/gate-mf-desc.md`.
- `factory/generate_descriptor_artifacts.py` — emits `factory/artifacts/connector-roster.json`,
  `factory/artifacts/factory-status.json` and `docs/connectors/*.md` deterministically.
  `--check` fails on staleness without writing.
- Gate **MF-DESC**: ten acceptance checks, each paired with a fixture that makes it red.

### Changed
- **The driver registry is lazy.** `DRIVERS` answers membership, iteration and length
  from a pure-data manifest; a driver module is imported only on value lookup. Each
  driver's descriptor lives in its own `descriptor.py` importing no client library,
  and the driver modules defer psycopg / clickhouse-connect into the functions that
  use them. Discharges the D-2/a precondition: validating a config's dialect string
  and sweeping every descriptor now import zero database clients.
- `drivers/clickhouse/__init__.py` and `drivers/rest/__init__.py` no longer re-export
  their driver class — the re-export defeated lazy descriptor imports.

### Fixed
- The historical firewall check `grep -rn "import.*drivers" src/r64_db_engine/core/`
  **could not fail** on the import form anyone writes: the regex requires `import`
  before `drivers` on the line, so `from r64_db_engine.drivers.postgres.driver import X`
  never matched and it printed `HOLDS` regardless. Replaced by an AST walk in
  Gate MF-DESC check 1, which also requires the sanctioned registry import to be
  function-scoped. See `docs/conformance/gate-mf-desc.md` finding MF-DESC/1.

## [0.1.0] - 2026-05-18

### Added
- Postgres driver (v0.1 reference implementation against `Driver` ABC)
- Full-refresh and incremental (watermark) pull modes per table
- Daemon mode with `/health` endpoint, SIGTERM graceful shutdown
- Atomic `.ramdb` writes (temp-then-rename pattern)
- Type coercion for all tested Postgres types (bigint, numeric, jsonb, bytea, uuid, timestamptz, intervals, arrays)
- `examples/` directory with annotated configs for dev and real Row64 Server deployment
- `make demo` workflow — ephemeral Postgres + 50K rows × 5 tables → `.ramdb` in ~250ms
- `dev_postgres.sh` with safe sourcing pattern via `env` subcommand
- Documentation tree under `docs/` with operator and developer paths

### Known Limitations
- `.ramdb` codec is ASCII-only at the `row64tools` layer (~50 hardcoded `encode('ascii')` call sites in `bytestream.py` and `ramdb.py`)
- Engine defaults `ascii_sanitize: true` to prevent codec crashes at cost of silent data loss for non-ASCII characters
- UTF-8 codec support tracked under v1.0 milestone

### Engineering Notes
- Release readiness validated via multi-pass CodeRabbit adversarial verification loop
- End-to-end ingest verified against real Row64 Server install (cachyos-kos)
