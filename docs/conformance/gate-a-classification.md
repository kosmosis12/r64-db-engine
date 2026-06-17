# Gate A — Test Inventory & Fidelity Classification (Task 1)

> Produced before any abstraction work. This is the reasoning that drives
> which assertions get pulled into the source-agnostic conformance contract
> and which stay as pg-specific plumbing tests.

## Method

Every test in the existing suite was read and classified as either:

- **FIDELITY** — asserts something about *the data that lands in the `.ramdb`*:
  type mapping, value coercion, null/sentinel filling, temporal/timezone
  normalization, codec-width/overflow rejection, or the binary round-trip.
  These are the contract every future driver must sign.
- **NON-FIDELITY** — asserts connection/discovery, config parsing, watermark
  *bookkeeping*, atomic-write *mechanics*, scheduler/daemon behavior, health
  endpoint, SQL-injection escaping, or schema-drift detection. Driver-shaped
  or core-infra, but not part of the "what bytes land" contract.

A handful are **MIXED** (an integration test that exercises plumbing *and*
makes one fidelity assertion). For those the *fidelity assertion* is what gets
abstracted; the plumbing stays where it is.

Counts are at test-function granularity with parametrize expansion noted in
parentheses. Totals: **155 collected non-integration + 12 integration = 167**
(`pytest` parametrize expansion of the "148" hand-count).

## Assertion classes (target taxonomy)

The five fidelity assertion classes the contract will expose:

| Class | Code | What it pins |
|---|---|---|
| Type-map round-trip | `TYPE_MAP` | native source type → row64/pandas dtype |
| Value-width / overflow | `WIDTH` | a value wider than the codec lane is *caught* (founding template: int32 lane) |
| Null / sentinel | `NULL` | None passthrough + per-dtype NaN/NaT fill rules |
| Timezone-aware temporal | `TZ` | tz-aware → UTC-naive; date/timestamp/time normalization |
| `.ramdb` round-trip | `RAMDB` | write → `load_to_df` → frame-equal |

## tests/drivers/postgres/test_coercion.py — 93 tests (unit, no DB)

| Test | Params | Class | Verdict | Note |
|---|---|---|---|---|
| `test_pandas_dtype_for_known_types` | 48 | TYPE_MAP | FIDELITY | the §6.1 mapping itself |
| `test_pandas_dtype_for_uppercase_is_case_insensitive` | 1 | TYPE_MAP | FIDELITY | normalization rule |
| `test_pandas_dtype_for_unknown_falls_back_to_string` | 1 | TYPE_MAP | FIDELITY | unknown→string fallback rule |
| `test_coerce_integer` | 5 | TYPE_MAP | FIDELITY | value-level int |
| `test_coerce_float` | 4 | TYPE_MAP | FIDELITY | value-level float |
| `test_coerce_numeric_from_decimal` | 1 | WIDTH | FIDELITY | Decimal→float64 |
| `test_coerce_numeric_high_precision_raises` | 1 | WIDTH | FIDELITY | precision-loss rejection |
| `test_coerce_text` | 6 | TYPE_MAP | FIDELITY | value-level str |
| `test_coerce_boolean` | 2 | TYPE_MAP | FIDELITY | value-level bool |
| `test_coerce_date_from_python_date` | 1 | TZ | FIDELITY | date→datetime64 |
| `test_coerce_timestamp_naive` | 1 | TZ | FIDELITY | naive timestamp |
| `test_coerce_timestamptz_converts_to_utc_naive` | 1 | TZ | FIDELITY | **tz→UTC-naive** |
| `test_coerce_time_serializes_to_iso_string` | 1 | TZ | FIDELITY | time→HH:MM:SS string |
| `test_coerce_interval_to_microseconds` | 1 | WIDTH | FIDELITY | interval→int64 µs |
| `test_coerce_uuid_str` | 1 | TYPE_MAP | FIDELITY | uuid→str |
| `test_coerce_jsonb_dict_to_compact_json` | 1 | TYPE_MAP | FIDELITY | dict→compact json |
| `test_coerce_jsonb_list_to_json_string` | 1 | TYPE_MAP | FIDELITY | list→json |
| `test_coerce_jsonb_large_value_warns` | 1 | — | NON-FIDELITY | telemetry (asserts a log record, not output) |
| `test_coerce_bytea_to_hex` | 1 | TYPE_MAP | FIDELITY | bytes→hex |
| `test_coerce_bytea_memoryview` | 1 | TYPE_MAP | FIDELITY | memoryview→hex |
| `test_coerce_bytea_large_warns` | 1 | — | NON-FIDELITY | telemetry |
| `test_coerce_array_serializes_to_json` | 1 | TYPE_MAP | FIDELITY | array→json |
| `test_coerce_text_array` | 1 | TYPE_MAP | FIDELITY | text[]→json |
| `test_coerce_network` | 3 | TYPE_MAP | FIDELITY | inet/cidr/macaddr→str |
| `test_coerce_fts` | 2 | TYPE_MAP | FIDELITY | tsvector/tsquery→str |
| `test_coerce_geometry_wkt` | 1 | TYPE_MAP | FIDELITY | geometry→WKT |
| `test_coerce_xml` | 1 | TYPE_MAP | FIDELITY | xml→str |
| `test_coerce_range_int4range` | 1 | TYPE_MAP | FIDELITY | range→str |
| `test_coerce_value_none_passes_through` | 1 | NULL | FIDELITY | None passthrough |
| `test_every_spec_61_type_present_in_dtype_map` | 1 | TYPE_MAP | FIDELITY | coverage gate |

**91 FIDELITY, 2 NON-FIDELITY (telemetry).** This file is the spine of the
contract — almost entirely the type-map and value-coercion surface.

## tests/core/test_coercion_framework.py — 18 tests (unit, no DB)

| Test | Class | Verdict |
|---|---|---|
| `test_ascii_sanitize_replaces_smart_quotes` | NULL/value | FIDELITY |
| `test_ascii_sanitize_passes_ascii_through` | value | FIDELITY |
| `test_ascii_sanitize_handles_emoji` | value | FIDELITY |
| `test_int_column_fills_nan_with_zero` | NULL | FIDELITY |
| `test_float_column_preserves_nan` | NULL | FIDELITY |
| `test_string_column_fills_nan_with_empty` | NULL | FIDELITY |
| `test_string_column_applies_ascii_when_enabled` | value | FIDELITY |
| `test_string_column_skips_ascii_when_disabled` | value | FIDELITY |
| `test_bool_column_fills_nan_with_false` | NULL | FIDELITY |
| `test_datetime_column_preserves_nat` | NULL/TZ | FIDELITY |
| `test_datetime_column_strips_timezone` | TZ | FIDELITY |
| `test_apply_coercion_dispatches_per_column` | TYPE_MAP/NULL | FIDELITY |
| `test_apply_coercion_passes_through_unmapped_columns` | TYPE_MAP | FIDELITY |
| `test_apply_coercion_empty_dataframe` | TYPE_MAP | FIDELITY |
| `test_compare_schemas_detects_added_columns` | — | NON-FIDELITY (drift) |
| `test_compare_schemas_detects_removed_columns` | — | NON-FIDELITY (drift) |
| `test_compare_schemas_detects_type_change` | — | NON-FIDELITY (drift) |
| `test_compare_schemas_none_previous_returns_empty` | — | NON-FIDELITY (drift) |

**14 FIDELITY, 4 NON-FIDELITY (schema-drift).** These are *already*
source-agnostic — the contract reuses `core.coercion` directly. Schema-drift
is real but it's change-detection bookkeeping, not a per-value fidelity claim.

## tests/drivers/postgres/test_driver.py — 11 tests (integration, needs DB)

| Test | Class | Verdict | Note |
|---|---|---|---|
| `test_connect_and_discover` | — | NON-FIDELITY | connection + discovery |
| `test_validate_table_ok` | — | NON-FIDELITY | validation plumbing |
| `test_validate_table_missing_table` | — | NON-FIDELITY | validation plumbing |
| `test_validate_inline_sql` | — | NON-FIDELITY | validation plumbing |
| `test_pull_full_refresh` | NULL/value | MIXED→FIDELITY | the `café→caf?` sanitize assertion |
| `test_pull_incremental_first_run_then_advance` | — | NON-FIDELITY | watermark bookkeeping |
| `test_pull_handles_jsonb_array_bytea_numeric` | TYPE_MAP/RAMDB | FIDELITY | end-to-end type round-trip |
| `test_numeric_20_5_round_trip_preserves_exact_value` | WIDTH | FIDELITY | numeric precision rejection |
| `test_int64_source_values_above_signed_int32_round_trip_exactly` | WIDTH | FIDELITY | **founding template** (int + interval, 2 params) |
| `test_incremental_limit_does_not_drop_rows_at_equal_watermark` | — | NON-FIDELITY | watermark tie-break |

**The fidelity content here is reachable without a DB.** Every fidelity
assertion is over the *coercion + writer* path; the live Postgres only
*produces* the Python objects (`Decimal`, `timedelta`, `bytes`, tz-aware
`datetime`). The contract reproduces those objects directly in the fixture
pack, so the founding-template overflow case and the type round-trips run
DB-free. The watermark/connection/validation tests stay as pg integration.

## tests/drivers/postgres/test_driver_security.py — 2 tests

SQL-identifier escaping. **NON-FIDELITY** (security/plumbing).

## tests/e2e/test_postgres_to_ramdb.py — 1 test (integration)

`test_e2e_full_refresh_writes_ramdb` — daemon wiring with a *stubbed* ramdb
serializer. **NON-FIDELITY** (e2e plumbing; the serializer is faked so it is
not a real round-trip).

## tests/core/test_ramdb_writer.py — 9 tests

All nine assert atomic-write *mechanics* (tempfile, rename, SIGTERM cleanup,
orphan sweep, missing dir). **NON-FIDELITY** (atomic-write plumbing). Note:
the codec-overflow guard *lives* in this module but is **not** tested here —
it is only hit via the integration driver test. Pulling that guard into a
DB-free fixture assertion is a primary goal of the contract (WIDTH class).

## tests/core/test_config.py (10), test_state.py (12), test_daemon.py (8), test_health.py (3)

All **NON-FIDELITY**: config parsing/cadence/env (10), SQLite watermark+history
+corruption recovery (12), scheduler/worker/error-status + the `core` import
firewall (8), health HTTP 200/503/404 (3). `test_daemon` and `test_state`
touch watermarks and even a real `load_to_df`, but the assertions are about
*scheduling/storage correctness*, not value fidelity.

## Rollup

| Bucket | FIDELITY | NON-FIDELITY | MIXED→FIDELITY |
|---|---|---|---|
| postgres/test_coercion | 91 | 2 | — |
| core/test_coercion_framework | 14 | 4 | — |
| postgres/test_driver (integration) | 4 | 5 | 1 (`test_pull_full_refresh`) |
| postgres/test_driver_security | 0 | 2 | — |
| e2e | 0 | 1 | — |
| core/test_ramdb_writer | 0 | 9 | — |
| core/test_config / state / daemon / health | 0 | 33 | — |
| **Total (function-level)** | **~109** | **56** | **1** |

## Decisions that fall out of this

1. **The contract is unit-grade, DB-free.** Every fidelity assertion reduces
   to `coerce_value` / `apply_coercion` / `RamdbWriter` over Python objects.
   Live Postgres only manufactures those objects, so the fixture pack can
   stand in for it. This is what lets Gate A run in CI without testcontainers
   and lets the self-regeneration proof run here (no Docker available).

2. **The codec-width founding template moves down a level.** Today
   `test_int64_source_values_above_signed_int32` needs a DB to make a
   `bigint`/`interval` value > int32. In the contract, the fixture pack
   *declares* such a value and the WIDTH assertion drives it through the
   coercer (interval) and the `RamdbWriter` (bigint) directly. Any source
   emitting a value wider than the codec lane is caught at the fixture level.

3. **Telemetry, schema-drift, watermark, atomic-write mechanics, security,
   config, daemon, health stay put.** They are real and must keep passing,
   but they are not the cross-driver fidelity contract and are not abstracted.

4. **`core.coercion` is already source-agnostic** and is consumed by the
   contract as-is — no changes, preserving the 14 framework fidelity tests.
