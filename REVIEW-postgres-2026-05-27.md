# Postgres Reference Driver Adversarial Review - 2026-05-27

Branch reviewed: `review/postgres-audit` at `7f62ce3342a7c1918a8082e71ac51c918a5b6443`

## Executive Verdict

No. This driver is not airtight enough to become the reference copied into six sibling drivers. Real execution found silent value corruption for declared `bigint` and `interval` output, non-exact `numeric(20,5)` round trips, dropped incremental rows at tied watermark boundaries, duplicate data after documented state reset recovery, broken `TEXT[]` handling, injectable configured identifiers, and a SIGTERM tempfile leak. The core package also directly imports the driver registry, hardcodes Postgres throughout config/status/metrics/systemd, reads existing RamDB output during incremental operation, and an executed supplied config writes directly into `live/`. A passing mocked E2E test does not rebut any of these findings.

## Findings

| ID | P-level | Dimension | File:line | Description | Reproducing test or execution | Fix status |
|---|---:|---|---|---|---|---|
| PG-001 | P1 | Correctness | `src/r64_db_engine/drivers/postgres/coercion.py:33-43,74`; `core/ramdb_writer.py:40-53` | Declared `bigint` and `interval` `int64` values silently corrupt in actual RamDB round trip. `3548933426` returns as an overflowed signed `int32`; seeded `duration=3548933426` loaded as `-746033870`. The immediate codec defect is in installed `row64tools 1.0.10`, but this driver exposes it as supported output without guarding it. | `tests/drivers/postgres/test_driver.py::test_int64_source_values_above_signed_int32_round_trip_exactly` (`XFAIL` for `BIGINT` and `INTERVAL`) | Reproducer added; production fix open |
| PG-002 | P1 | Correctness | `drivers/postgres/coercion.py:49-51,170-187` | `numeric`/`decimal`, including `numeric(20,5)`, are intentionally converted to `float64`; exact decimal values cannot survive the declared round trip. | `test_numeric_20_5_round_trip_preserves_exact_value` (`XFAIL`) | Reproducer added; fix open |
| PG-003 | P1 | Correctness | `drivers/postgres/driver.py:405-411,431-449` | Incremental pulls use only `key > watermark` while allowing `LIMIT`. If two rows share the boundary timestamp and only one fits in a batch, the second is permanently skipped. | `test_incremental_limit_does_not_drop_rows_at_equal_watermark` (`XFAIL`, real Postgres) | Reproducer added; fix open |
| PG-004 | P1 | Correctness / Resilience | `core/daemon.py:164-170,206-209,288-300` | The documented `state.db` deletion recovery path re-pulls all incremental source rows, then concatenates them with existing RamDB output, duplicating data. | `tests/core/test_daemon.py::test_deleted_state_repull_does_not_duplicate_incremental_output` (`XFAIL`, real RamDB) | Reproducer added; fix open |
| PG-005 | P1 | Correctness | `drivers/postgres/driver.py:329-389`; `coercion.py:149-150,261-264` | Real `TEXT[]` pull does not enter the array prepass because `information_schema` reports `ARRAY`; output becomes Python repr (`['x', 'y']`) rather than JSON. | Pre-existing `test_pull_handles_jsonb_array_bytea_numeric` fails under `--integration` | Existing reproducer fails; fix open |
| PG-006 | P1 | Security | `drivers/postgres/driver.py:392-422` | Table and watermark-column identifiers are interpolated between quotes without escaping embedded `"`. A configured identifier can break out into executable SQL. Inline SQL is deliberately executable; this defect is in the supposed identifier path. | `tests/drivers/postgres/test_driver_security.py::test_pull_query_escapes_identifier_quotes` (2 `XFAIL`) | Reproducer added; fix open |
| PG-007 | P1 | Resilience | `core/ramdb_writer.py:40-53` | Cleanup is in `except BaseException`, not a signal-safe/finally lifecycle. A process receiving actual SIGTERM during synchronous serialization exits leaving `.{target}.ramdb.tmp.*`. Existing KeyboardInterrupt coverage did not prove SIGTERM. | `tests/core/test_ramdb_writer.py::test_write_sigterm_mid_write_cleans_up` (`XFAIL`) | Reproducer added; fix open |
| PG-008 | P1 | Resilience | `core/daemon.py:123-138,360-369` | Startup authentication failure is swallowed by an unbounded reconnect loop rather than failing fast as specified. | `test_daemon_startup_auth_failure_fails_fast` (`XFAIL`) | Reproducer added; fix open |
| PG-009 | P1 | Resilience | `core/daemon.py:167-176,313-330` | A transient connection-loss failure during a pull does not set source connectivity false; health can continue reporting Postgres connected after a dropped connection. | `test_daemon_marks_source_disconnected_after_connection_loss` (`XFAIL`) | Reproducer added; fix open |
| PG-010 | P1 | Architecture invariant | `core/daemon.py:378`; `core/config.py:18-34,81-82`; `core/metrics.py:45`; `core/systemd.py:10-13` | Core is not source-agnostic: it imports `r64_db_engine.drivers`, contains `PostgresConfig` and `Literal["postgres"]`, emits Postgres-specific health/metrics, and generates a PostgreSQL-specific unit. A stub sibling requires core edits. | Required grep and dialect-name scan shown below | Open |
| PG-011 | P1 | Architecture invariant | `core/daemon.py:288-300`; `examples/cachyos-demo.yaml:6-7,38-40` | Core reads an existing RamDB file in incremental mode, and the supplied demo config directs output into Row64 `live/`. The required command actually wrote `Customers.ramdb` under `/var/www/ramdb/live/RAMDB.Row64/PostgresSource/`. Both violate the stated unidirectional/loading-only invariant. | `r64-db-engine run --once --config examples/cachyos-demo.yaml` logged `pull_success`; resulting file size `5,522,861` bytes in `live/` | Open |
| PG-012 | P2 | Contract | `core/ramdb_writer.py:76-78`; `core/daemon.py:294-296` | The required audit contract is direct imports `from row64tools.ramdb import save_from_df, load_to_df`; implementation imports `ramdb` as a module and calls methods through it. No prohibited `r64.save()` variant was found, but it is not the stipulated shape. | `rg -n "from row64tools|save_from_df|load_to_df|r64\\.save|row64tools\\.save|RamDb\\.save" src/r64_db_engine` | Open |
| PG-013 | P2 | Security | `scripts/dev_postgres.sh:56-67,84-90` | The development bootstrap prints a password and `PGPASSWORD` export to stdout. This leaks dev credentials when command output is collected in logs or shared audit artifacts. | Executed `bash scripts/dev_postgres.sh`; output contained `password: row64dev` and `export PGPASSWORD=row64dev` | Open |

No P0 issue was proven during this review. The P1 issues are sufficient to reject reference-driver status.

## Firewall Result

Required import-leak command:

```bash
grep -rn "from r64_db_engine.drivers\|import.*drivers" src/r64_db_engine/core/
```

Source result:

```text
src/r64_db_engine/core/daemon.py:378:    from r64_db_engine.drivers import resolve
```

This is an explicit firewall violation. The existing test `test_core_does_not_import_postgres_driver` passes because it checks imported public objects for `drivers.postgres` module provenance; it does not scan the source or reject registry imports.

Weaker dialect-name scan:

```bash
rg -n -i "postgres|pgsql|psql|pg_" src/r64_db_engine/core/
```

Confirmed Postgres-specific core surfaces:

| File:line | Smell |
|---|---|
| `core/config.py:18-29,81-82` | `PostgresConfig`, `Literal["postgres"]`, and `postgres` field bake the first dialect into core validation. |
| `core/config.py:34` | Default output group is named `PostgresSource`. |
| `core/daemon.py:70,127,133,327-330` | Core maintains `_pg_connected`, logs `postgres_connect_failed`, and emits `postgres` status fields. |
| `core/metrics.py:45` | Metric name and snapshot contract are Postgres-specific. |
| `core/systemd.py:11-12` | Generated unit names Postgres and orders after `postgresql.service`. |
| `core/driver.py:55` | Docstring example mentions Postgres; documentation-only, still a literal in core under the requested audit rule. |

## Atomic Write And Directionality Audit

`RamdbWriter.write()` creates a tempfile in the final directory with the correct filename pattern at `core/ramdb_writer.py:42-44`. It calls `os.replace()` at line 47 rather than the specified `os.rename()`; both are same-filesystem atomic replacement on POSIX, so this is a contract deviation rather than a demonstrated corruption bug.

The cleanup guarantee is not met. `_save_ramdb()` followed by an actual SIGTERM can terminate the process without raising through the `except BaseException` block. `test_write_sigterm_mid_write_cleans_up` writes partial temp content, sends SIGTERM, and observes the tempfile still present.

Directionality also fails independently of atomicity:

- `core/daemon.py:294-297` loads existing output via `ramdb.load_to_df()` for incremental operation.
- `examples/cachyos-demo.yaml:39` uses `/var/www/ramdb/live/RAMDB.Row64`.
- Executing the supplied one-shot command created/overwrote `/var/www/ramdb/live/RAMDB.Row64/PostgresSource/Customers.ramdb`.

No code path referring to `locked/` was found.

## Test Execution Log

### Environment And Required Gates

| Command | Result |
|---|---|
| `python -m venv .venv` | PASS |
| `.venv/bin/pip install -e '.[dev]'` | First sandbox attempt failed on restricted DNS; approved network rerun PASS. Installed editable `r64-db-engine 0.1.0`, with `row64tools 1.0.10`. |
| `.venv/bin/ruff check .` | PASS: `All checks passed!` after added regression tests. |
| `.venv/bin/mypy src/` | PASS: `Success: no issues found in 17 source files`. |
| `.venv/bin/pytest -v --ignore=tests/e2e` before additions | PASS with skipped integration: `148 passed, 7 skipped in 21.73s`. |
| `.venv/bin/pytest -v --ignore=tests/e2e` after additions | PASS with recorded defects: `148 passed, 11 skipped, 6 xfailed in 21.54s`. |
| `.venv/bin/pytest -v tests/e2e --integration` | PASS: `1 passed in 8.02s`. This test mocks `_save_ramdb`; it is not a format round trip. |
| `.venv/bin/pytest -v tests/drivers/postgres --integration` after additions | FAIL: `1 failed, 99 passed, 6 xfailed in 8.40s`. Failure is real `TEXT[]` conversion. |

### Supplied Runtime Commands

| Command | Result |
|---|---|
| `bash scripts/dev_postgres.sh` | PASS; started Docker Postgres on `:5433`; printed the development password to stdout. |
| `.venv/bin/python scripts/seed_postgres.py` | PASS: `done in 2.9s - 50000 rows x 5 tables`. |
| `.venv/bin/r64-db-engine validate --config examples/cachyos-demo.yaml` | PASS: `[ok] Customers (public.customers)`. Initial sandbox run hung on local connection; approved local-network rerun completed. |
| `.venv/bin/r64-db-engine run --once --config examples/cachyos-demo.yaml` | Exited `0`; logged `rows=50000`, `duration_ms=225`; wrote directly into the configured `live/` subtree. |
| `.venv/bin/r64-db-engine validate --config examples/incremental.yaml` | PASS for all six configured targets. |
| `.venv/bin/r64-db-engine run --once --config examples/incremental.yaml` | Exited `0`, writing six real RamDB files under `/tmp/r64-demo/ramdb/PostgresSource/`. Loading `Measurements.ramdb` exposed interval overflow. |
| `bash scripts/dev_postgres.sh stop` | PASS; stopped the ephemeral container after verification. |

### Performance Reproduction

The demo log reported `50,000 / 225 ms = 222,222 rows/sec` for `public.customers`, comparable to the config comment's `219 ms` claim. That logged duration is not materialization throughput: `PostgresDriver.pull()` stops timing before `RamdbWriter.write()`.

Instrumented real pull plus real `save_from_df` on the same seeded `customers` table:

```text
rows=50000 logged_pull_ms=220 measured_pull_ms=223.664
write_ms=139.523 total_ms=363.187 total_rows_per_s=137670.2
```

`cProfile` run over connect, pull, and write (profiling overhead present) identified the principal application costs:

```text
ramdb.py:23(save_from_df)        0.614 s cumulative
driver.py:361(_rows_to_dataframe) 0.447 s cumulative
coercion.py:86(apply_coercion)  0.178 s cumulative
```

The advertised rate is a pull/coercion measurement, not end-to-end ingest throughput. In measured end-to-end execution the serializer added about `139.5 ms`, reducing throughput to `137.7K rows/sec`.

## Coercion Coverage Matrix

Legend: `unit` means a mapping or value unit test exists; `PG` means an executed real-Postgres pull exists; `RT` means a real `save_from_df`/`load_to_df` assertion or manual executed round trip. Aliases are grouped only where their evidence status is identical.

| Postgres type keys in `PG_TYPE_TO_PANDAS` | Target pandas dtype | Existing proof before review | Executed RT result | Gap |
|---|---|---|---|---|
| `smallint`, `int2`, `integer`, `int`, `int4`, `int8`, `smallserial`, `serial`, `bigserial`, `oid` | `int64` | mapping/value unit coverage is partial by alias | No exact boundary RT | Untested through RamDB |
| `bigint` | `int64` | unit; seeded PG small IDs | **FAIL** for `3548933426` (`XFAIL` added) | Silent signed-int32 overflow |
| `real`, `float4`, `double precision`, `float8` | `float64` | mapping/value unit coverage is partial by alias | No scalar exact RT | Untested through RamDB |
| `numeric`, `decimal` including `numeric(20,5)` | `float64` | unit and simple PG float comparison | **FAIL** for exact `numeric(20,5)` (`XFAIL` added) | Decimal precision is lost |
| `text`, `varchar`, `character varying`, `char`, `character`, `bpchar`, `name`, `citext` | `string` | unit coverage partial by alias; seeded customer text | PASS manually for sanitized seeded customer output | Several aliases have no PG/RT test |
| `boolean`, `bool` | `bool` | unit; seeded identifiers PG/output | Value observed (`True` serialized/load as `1`) | No explicit exact RT assertion |
| `date` | `datetime64[ns]` | unit and PG DataFrame assertion | No RamDB RT assertion | Untested RT |
| `timestamp`, `timestamp without time zone` | `datetime64[ns]` | unit; PG DataFrame field | No explicit RamDB RT assertion | Untested RT |
| `timestamptz`, `timestamp with time zone` | `datetime64[ns]` | UTC-naive unit; seeded output loaded | PASS manually for seeded timestamp values; no source-equality assertion | Formal RT test absent |
| `time`, `time without time zone`, `timetz`, `time with time zone` | `string` | unit only for `time` | No RT | Untested RT and aliases |
| `interval` | `int64` microseconds | unit and seeded PG output | **FAIL** (`XFAIL` added; seeded output visibly overflowed) | Silent corruption |
| `uuid` | `string` | unit; seeded identifiers | PASS manually for output form | No asserted source-to-RT test |
| `json`, `jsonb` | `string` | unit; PG JSONB DataFrame; seeded RamDB loaded | JSONB manually valid after RT | `json` RT absent |
| `bytea` | `string` | unit; PG DataFrame; seeded RamDB loaded | Hex value manually observed after RT | No explicit source-equality RT assertion |
| `array` plus concrete `integer[]`, `text[]`, `double precision[]` | `string` | unit direct coercer only; real test for `TEXT[]` | **FAIL** for `TEXT[]` in real pull; numeric double array happened to be JSON-readable in seeded output | Metadata/prepass defect and incomplete array RT |
| `inet`, `cidr`, `macaddr`, `macaddr8` | `string` | unit only for first three | No RT | Untested RT; `macaddr8` not value-tested |
| `tsvector`, `tsquery` | `string` | unit | No RT | Untested RT |
| `geometry`, `geography` | `string` | value unit only for `geometry` | No RT | Untested RT; PostGIS not exercised |
| `xml` | `string` | unit | No RT | Untested RT |
| `int4range`, `int8range`, `numrange`, `tsrange`, `tstzrange`, `daterange` | `string` | value unit only for `int4range`; mapping units | No RT | Untested RT for all; most lack value test |

Special coercion and watermark cases:

| Case | Evidence | Result |
|---|---|---|
| ASCII sanitization including em dash | `tests/core/test_coercion_framework.py`; executed seeded `Customers.ramdb` | Verified replacement path for enabled default |
| Per-table `ascii_sanitize: false` | Config resolution and generic string unit tests only | Not verified through Postgres -> RamDB |
| Integer NaN -> `0`; string NaN -> `""`; bool NaN -> `False`; datetime NaT preserved | Generic dataframe unit tests only | Not verified through Postgres -> RamDB |
| First-ever incremental pull | Existing real PG integration test | Verified when integration enabled |
| Equal watermark with no limit | Existing real PG integration test verifies zero subsequent rows | Verified only for no new ties |
| Equal watermark with limited tied rows | Added real PG test | **FAIL**, dropped row |
| NULL incremental key | None found or executed | Untested |
| Wrong-dtype incremental key / watermark | None found or executed | Untested |

## Resilience And Concurrency Coverage

| Surface | Verified result |
|---|---|
| Save exception and KeyboardInterrupt temp cleanup | Existing unit tests pass. |
| Actual SIGTERM mid-write temp cleanup | **FAIL**, added `XFAIL` reproducer. |
| Missing loading directory | Existing unit test raises. |
| Loading directory not writable / disk full | No executable coverage found or added; remains unproven. |
| Rename cross-filesystem | Construction uses same target directory, verified by existing unit test; supplied `os.replace` is atomic on same POSIX filesystem. |
| Corrupt `state.db` initialization | Existing state-store unit test passes. |
| Deleted/corrupt state plus incremental output semantics | **FAIL**, added duplicate-output reproducer. |
| Connection drop mid-pull | **FAIL** for health connectivity update, added reproducer. |
| Startup authentication failure | **FAIL** for fail-fast behavior, added reproducer. |
| Connect timeout | Not exercised against a timed-out endpoint. |
| Missing table | Existing real-PG validation test passes under `--integration`. |
| Missing incremental column | Static validation path exists; no executed integration assertion found. |
| Worker pool bound | Semaphore exists at `core/daemon.py:71-73`; no contention/race stress test found. |
| SQLite concurrent mutations | Pull tasks run in one asyncio loop, but no concurrency stress test found. |
| Duplicate target writes | Config rejects duplicate targets in existing unit tests. |
| `asyncio.create_task` tracking | Both call sites are tracked and awaited/cancelled: `core/daemon.py:95-104`, `cli.py:135-152`. |

## Documents And Claims Not Proven

- `references/coercion.md` and `references/v01-build-lessons.md`, explicitly requested for this review, do not exist in this checkout.
- `README.md:23` claims type coercion for every Postgres type is tested. Real integration fails on `TEXT[]`, and most listed types have no RamDB round-trip proof.
- `README.md:22` claims SIGTERM graceful shutdown/atomic temp handling. Before this review the test named for this behavior used `KeyboardInterrupt`; the added actual SIGTERM test fails.
- `SPEC.md:638` requires edge types to load back through `row64tools.ramdb.load_to_df()`. The existing E2E test stubs `_save_ramdb` and never loads a real file.
- `SPEC.md:637` claims delete-state recovery. State creation recovery is unit-tested, but the added end-to-end incremental-output test shows duplicate rows.
- The supplied `examples/cachyos-demo*.yaml` documents and implements direct writes to `live/`, contradicting `SPEC.md:678` and the review invariant.

## Reference-Driver Readiness Call

Do not copy this structure to any sibling driver. Before sibling work proceeds, the reference must:

1. Define a precision-safe policy for decimal data and enforce a serializer-safe policy for all declared `int64` outputs, including `bigint` and interval.
2. Repair array type discovery/coercion and require real RamDB round-trip tests for every supported type category.
3. Make incremental pagination lossless for non-unique watermarks and make state-reset recovery replace or rebuild output without duplicate concatenation.
4. Escape or compose identifiers with psycopg identifier APIs; retain inline SQL only as an explicitly trusted configuration feature.
5. Implement and test actual SIGTERM write cleanup semantics.
6. Move driver registration/config/status/metric/service dialect concerns out of `core/`; remove direct driver imports and Postgres enumeration from core.
7. Enforce output confinement to loading directories and remove the live-writing example path.
8. Make connection/auth behavior match fail-fast and connectivity-health requirements.

## Recommended Fixes, Ordered

1. **Block silent corruption first.** Until `row64tools` is fixed or guarded, reject values that cannot round-trip exactly (at minimum `int64` values in the unsafe range) rather than producing corrupt RamDB files. Change decimal handling to a documented exact representation or reject precision-bearing numerics that cannot be exact.
2. **Fix incremental correctness.** Use a stable composite cursor such as `(watermark, primary_key)` or prohibit bounded pulls unless the key is unique; on absent/reset state, replace the destination rather than merge with prior incremental output.
3. **Fix SQL identifier construction.** Use `psycopg.sql.Identifier` composition for table/schema/column identifier paths and keep values parameterized.
4. **Fix serialization lifecycle.** Define process signal behavior around synchronous `save_from_df`, use a cleanup guarantee that executes on graceful termination, and retain orphan cleanup as crash recovery rather than as the primary guarantee.
5. **Fix the architecture firewall and output invariant.** Move registry resolution/wiring above `core`, make connection config dialect-owned, generalize status/metrics, and reject live/locked output destinations.
6. **Upgrade acceptance coverage.** Replace mocked serializer E2E assertions with real `save_from_df`/`load_to_df` checks and add writable/disk-full/connect-timeout/null-watermark tests.

### Patches Implemented In This Review

Only reproducing tests were added; production behavior was intentionally not refactored during the audit:

| Test patch | Finding covered |
|---|---|
| `tests/core/test_ramdb_writer.py::test_write_sigterm_mid_write_cleans_up` | PG-007 |
| `tests/core/test_daemon.py::test_deleted_state_repull_does_not_duplicate_incremental_output` | PG-004 |
| `tests/core/test_daemon.py::test_daemon_marks_source_disconnected_after_connection_loss` | PG-009 |
| `tests/core/test_daemon.py::test_daemon_startup_auth_failure_fails_fast` | PG-008 |
| `tests/drivers/postgres/test_driver.py::test_numeric_20_5_round_trip_preserves_exact_value` | PG-002 |
| `tests/drivers/postgres/test_driver.py::test_int64_source_values_above_signed_int32_round_trip_exactly` | PG-001 |
| `tests/drivers/postgres/test_driver.py::test_incremental_limit_does_not_drop_rows_at_equal_watermark` | PG-003 |
| `tests/drivers/postgres/test_driver_security.py::test_pull_query_escapes_identifier_quotes` | PG-006 |

Each added test is marked `xfail(strict=True)` while the defect remains open: it records the currently demonstrated failure and will force attention if behavior unexpectedly changes.
