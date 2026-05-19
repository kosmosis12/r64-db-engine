# Changelog

All notable changes to r64-db-engine.

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
