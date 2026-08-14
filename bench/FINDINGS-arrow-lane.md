# FINDINGS — Arrow-native lane (Phase 2)

**Campaign:** CC Phase 2 · **Date:** 2026-08-14 · **Machine:** cachyPC
**Branch:** `feat/arrow-lane` off `main` @ `7820d18`
**Status at this point:** **Phases A and B complete. No performance work done,
no performance claim made** — that is Phase C, which has not started. Phase D
(serve + roster) has not started.

---

## Phase A — the lane exists, end to end, with no driver behind it yet

The deliverable is a **capability on the existing ABCs**, not a second stack:

| Piece | Location |
|---|---|
| `Driver.supports_arrow()` / `pull_arrow()` / `ArrowPullResult` | `core/driver.py` |
| `Sink.supports_streaming()` / `write_stream()` / `StreamWriteResult` | `core/sink.py` |
| Streaming writer, re-chunk, dictionary unification | `sinks/arrow_ipc.py` |
| Lane routing + full-refresh law | `core/daemon.py` |
| Streaming sink tests (20) | `tests/sinks/test_arrow_ipc_stream.py` |
| Lane routing tests (11) | `tests/core/test_arrow_lane_routing.py` |

Suite **356 passed / 40 skipped** (was 325), ruff clean, mypy clean.
Supabase `--integration` set **13/13** green — the pandas lane runs unchanged
with the new capability code in the path (Gate A fallback proof).

The Phase D artifact reproduces **byte-for-byte** through all of this:

```
sha256  e674c8e6bdaba9eadfcca65c04fc4ddc3b24f5b1cd770ef9f27281c6f84b9503
bytes   48,743,402      blocks [4599]      cols 14      lane "dataframe"
```

---

## A-1 — a defect in my own D-4 work, found by building on it

**`Table.to_batches(max_chunksize=N)` SPLITS chunks larger than N. It never
MERGES smaller ones.**

`_write_ipc_file` as committed in `b86daa9` therefore only produced 65536-row
blocks *because* `pa.Table.from_pandas` happens to yield single-chunk columns.
Handed an already-chunked table it would have written the input's chunking and
called it the block discipline:

```
multi-chunk table (7 x 30000) -> to_batches(65536) -> [30000 x 7]   WRONG
combine_chunks() first        -> to_batches(65536) -> [65536, 65536, 65536, 13392]
```

The D-4 claim was true of every artifact this repo has ever written, and would
have stayed true right up until a source handed the sink pre-chunked data —
which is exactly what the Arrow lane does. **Fixed** with an explicit
`combine_chunks()` in `_write_ipc_file`, so the guarantee no longer depends on
an accident of the pandas converter. Phase D's sha is unchanged (verified
above), because that path was always single-chunk.

Recorded rather than quietly patched: it is the same class of trap as the D-4
one-block collapse — correct output, silently wrong *structure*, invisible to
every data assertion.

## A-2 — the dictionary constraint: option (b) taken

Confirmed empirically, not from documentation:

```
naive append of per-batch dictionaries -> ArrowInvalid:
  "Dictionary replacement detected when writing IPC file format.
   Arrow IPC files only support a single non-delta dictionary."
```

The failure is **loud**, which is the good outcome. But a unified dictionary
must exist before the first batch is written, and that cannot be known without
seeing every batch. So:

- **No dictionary columns** → true streaming. Buffer holds one block plus one
  source batch.
- **Any dictionary column** (configured via `dictionary_columns`, or already
  dictionary-typed in the reader's schema) → **collect, `unify_dictionaries()`,
  then write through the same `_write_ipc_file` the batch path uses.**

This is the brief's option **(b)**, and it trades the memory bound away **for
dictionary targets only**. Option (a) — a growing dictionary written
incrementally — is not expressible in the IPC *file* format without
delta-dictionary support in the writer. **Filed with (a) as the upgrade path.**

Consequence to carry into Phase C: an **N-cell whose artifact has a dictionary
column will not show the streaming RSS bound.** The meshbench `status` column is
exactly such a column. Either measure RSS on a projection without it, or report
the dictionary cell separately and labelled — do not average them together and
call it the lane's memory profile.

## A-3 — cross-lane byte-identity does NOT hold (checksum doctrine)

`pa.Table.from_pandas` attaches a `b'pandas'` schema-metadata blob recording
index and dtype provenance. The Arrow lane never touches pandas, so its
artifacts carry none. For identical source rows:

| | pandas entry point | Arrow lane |
|---|---|---|
| data buffers | identical | identical |
| schema (minus metadata) | identical | identical |
| block structure | identical | identical |
| `schema.metadata` | `[b'pandas']` | `None` |
| **sha256** | **differs** | **differs** |

**Verify-by-checksum is valid WITHIN a lane, never ACROSS lanes.** This lands
directly on two later commitments: Phase B item 4 (byte-reproducibility on
consecutive pulls — fine, same lane) and Phase C's **N vs P′** attribution
(same source, different lanes — a sha mismatch there is this blob, not a
fidelity failure). Pinned by
`test_pandas_entry_point_adds_schema_metadata_the_arrow_lane_does_not`.

Streaming output *is* byte-identical to `_write_ipc_file` given the same
schema, which is the parity that matters — pinned by
`test_streaming_matches_the_writer_byte_for_byte`.

## A-4 — schema-drift records are lane-flavoured

Drift detection stores `pandas_dtype` per column. On the Arrow lane there is no
pandas dtype, so the field carries the Arrow type string (`int64`, not
`Int64`). Self-consistent within a lane; **switching a target between lanes
would report spurious drift once.** Observation, not fixed — switching lanes is
a config change, and a one-shot drift warning on a deliberate migration is
arguably correct. Filed.

## A-5 — `NotImplementedError` is now a permanent error

A driver advertising `supports_arrow()` without implementing `pull_arrow()` was
being retried three times with 1/4/16s backoff before reporting `degraded`. A
missing implementation never recovers on its own. `_is_permanent` now classifies
`NotImplementedError` as permanent: one attempt, status `error`, named message.
**This is a core behaviour change affecting all drivers, not just the lane** —
disclosed rather than slipped in.

---

## Deviations from the brief (disclosed; ratification is Kos's)

| # | Brief said | Built | Why |
|---|---|---|---|
| A-d1 | "implements `query_arrow(...) -> pyarrow.RecordBatchReader`" | `pull_arrow(...) -> ArrowPullResult(reader, new_watermark, duration_ms)` | Mirrors `pull`/`PullResult` so the daemon's watermark and timing bookkeeping is lane-agnostic. `rows_pulled` is deliberately absent: it is not knowable until the reader is drained, and inventing it early is exactly the materialization this lane avoids. The sink returns it via `StreamWriteResult`. |
| A-d2 | "capability present → Arrow path, absent → pandas path" | Arrow path requires **both** `driver.supports_arrow()` and `sink.supports_streaming()` | An Arrow driver against ramdb would otherwise have no route. Draining the reader to rebuild a DataFrame costs more than the pandas lane and buys nothing, so it falls back — and `status_snapshot()["source"]["lane"]` says which lane ran, so the fallback is never silent. |
| A-d3 | (not in scope) | `_is_permanent` treats `NotImplementedError` as permanent | See A-5. Beyond strict Phase A scope; called out. |

**No scope fence was crossed.** No AWS work, no ADBC/Iceberg/Delta, no
`_pre_coerce_values` vectorization, no streaming-source semantics (the lane
streams batches within ONE full refresh; incremental is refused on the lane by
name), no dependency added.

---

## Gate A — status

| Condition | |
|---|---|
| Capability + streaming sink landed | ✅ |
| Dictionary two-batch test (valid artifact, unified dictionary) | ✅ |
| NaN/null distinctness pinned, both directions | ✅ |
| Full-refresh refusal on the lane | ✅ (sink-keyed *and* lane-keyed) |
| Artifact-contract parity: schema, swap safety, block structure, re-chunk | ✅ |
| Suite green | ✅ 356 passed / 40 skipped |
| Fallback routing proven via unchanged `--integration` e2e | ✅ Supabase 13/13 |

**No driver implements the capability yet** — that is Phase B (DuckDB). The
lane is proven against fake drivers and the real sink, which is the most that
can be proven before a real Arrow source exists.

---

## Planes of record — untouched this phase

| | Before | Now | |
|---|---|---|---|
| `meshroad-serve` (:8802) | active, MainPID **1123** | active, MainPID **1123** | ✅ |
| `meshroad-cockpit` (:8803) | active, MainPID **1315** | active, MainPID **1315** | ✅ |
| `:8902` dev serve | not started | not started (Phase D) | — |
| meshroad `src/` / `gui/` | untouched | untouched | ✅ |
| `.venv` | — | unmodified; no dependency added | ✅ |

No process was started or killed in this phase.

---

# Phase B — DuckDB driver (local), Gate B

**Status: local DuckDB proven. MotherDuck PARKED — no `MOTHERDUCK_TOKEN` in the
environment, so every claim below fences to LOCAL DuckDB only.**

`duckdb==1.5.5` added under fence 5: declared in `pyproject.toml`, `uv.lock`
regenerated, and the regeneration verified to add exactly one package with
**zero version changes on any existing pin and nothing removed**.

## B-0 — PG-010's live proof, stated exactly

`dialect: duckdb` became configurable by **registering the driver and nothing
else**. Precisely:

- `core/` contains **no reference to `duckdb`** (`grep -rn duckdb src/.../core/`
  is empty).
- The only `core/` change in Phase B is a 7-line fix to *Gate A's own*
  `status_snapshot()` field — reporting `lane: None` before connect instead of
  guessing — which has nothing to do with this driver.

This is the test the registry was built to pass, and it passed without being
bent. Phase 3 must clear the same bar twice more.

## B-1 — `LowCardinality(String)` does not survive Parquet

`status` is `LowCardinality(String)` at ClickHouse and lands as plain `VARCHAR`
in DuckDB: Parquet has no LowCardinality. **Not a defect and not fixed at
load** — dictionary encoding is a property of the ARTIFACT, not of the source,
and the sink already re-applies it via `dictionary_columns: {Perf: [status]}`.
The artifact carries `dictionary<values=string, indices=int32>` as required.

## B-2 — the timezone trap: an 8-hour silent shift that every check would pass

**The most dangerous thing found this campaign.**

`event_time` is `DateTime64(6)` at ClickHouse and lands as `TIMESTAMP WITH TIME
ZONE`, because ClickHouse marks it adjusted-to-UTC in the Parquet metadata.
**DuckDB's default session TimeZone is the machine's local zone** — here
`America/Los_Angeles`. A naive `CAST(event_time AS TIMESTAMP)` therefore shifts
every value by the local UTC offset:

```
naive cast @ default LA : 2025-12-31 16:00:15.184566 .. 2026-06-29 16:59:30.942340
naive cast @ UTC        : 2026-01-01 00:00:15.184566 .. 2026-06-29 23:59:30.942340
ClickHouse source truth : 2026-01-01 00:00:15.184566 .. 2026-06-29 23:59:30.942340
```

**Every ground-truth check would still have passed**, because not one of them
touches `event_time` — count, sums, null counts and cardinalities are all blind
to it. The artifact would have been eight hours wrong and fully green.

**Fixed at the LOAD, never in the ground truth**: `bench/load-duckdb.py` issues
`SET TimeZone='UTC'` before the cast, and asserts the resulting min/max against
the ClickHouse source bounds. The e2e config pins `settings: {TimeZone: UTC}`
on the connection for the same reason.

## B-3 — cross-lane schema divergence: `string` vs `large_string`

Extends the ratified checksum doctrine with a second lane-dependent property.
Same DuckDB source, same rows, both lanes, real meshbench data:

| column | N (Arrow lane) | P' (DataFrame lane) |
|---|---|---|
| `region`, `city`, `category`, `segment`, `product_name` | `string` | **`large_string`** |
| `status` | `dictionary<string, int32>` | `dictionary<string, int32>` |
| `event_time` | `timestamp[us]` | `timestamp[us]` |
| everything else | identical | identical |
| **sha256** | **differs** | **differs** |

pandas 3.0's `string` dtype yields `large_string` (the ClickHouse campaign
recorded the same); DuckDB's Arrow export yields plain utf8. Both are valid
Arrow strings and meshroad reads both.

**Doctrine, per Finding 1 as ratified, now with a second exception named:**

> Cross-lane equivalence = **data** + **schema-minus-metadata** + **block
> structure**. Schema comparison must additionally tolerate **string width**
> (`string` vs `large_string`), which is lane-dependent, not fidelity-relevant.
> Checksums are lane-scoped: **never compare an N sha to a P' sha.**

Pinned by `test_arrow_and_dataframe_lanes_agree_on_data_but_not_on_bytes`.
**Phase C must apply this fence wherever N-vs-P' shas appear.**

## B-4 — `timestamp[us]` arrives natively on the Arrow lane

The df lane needs `timestamp_unit: us` configured to reach the meshroad
reference shape, because pandas forces `datetime64[ns]` and the sink casts back
down. DuckDB's `TIMESTAMP` is microsecond natively, so the **Arrow lane needs
no timestamp configuration at all** — the e2e config sets none. One fewer knob
between source and artifact, and one fewer opportunity for a lossy cast.

## B-5 — driver-specific PER-TABLE options are not expressible (PG-010, table axis)

Determinism needs `ORDER BY row_id`. The natural expression is a per-table
`order_by:` key — but `core.config.TableConfig` is `extra="forbid"`, so a
driver-specific table option cannot be written in YAML. **This is the PG-010
leak class again, on the table axis rather than the top-level one.**

**Not fixed, and no core edit made.** Inline SQL covers it completely:

```yaml
source: "SELECT * FROM main.perf_1m ORDER BY row_id"
```

The driver passes an unprojected inline source through **verbatim** rather than
wrapping it as `SELECT * FROM (<source>) AS sub`, because a wrap would bury the
ORDER BY in a subquery where SQL does not oblige the engine to preserve it —
the ordering would hold by luck and stop holding without warning. Pinned by
`test_inline_sql_is_passed_through_verbatim_to_preserve_ordering`.

Filed for a future decision: widening `TableConfig` is the same call PG-010
was, and deserves the same deliberation rather than being slipped in here.

## B-6 — `batch_size: 0` was silently swallowed

`int(config.get("batch_size") or _DEFAULT)` folds an explicit `0` into the
default, so the "must be positive" guard never fired on the one value most
likely to be a mistake. Caught by its own unit test before it shipped. Fixed.

## B-7 — batch size: the choice and why

Default **65,536 rows**, matching `sinks/arrow_ipc._BLOCK_ROWS` exactly. The
sink re-chunks regardless, so this does not change the artifact; it changes how
much the re-chunk buffer has to hold. One source batch per artifact block keeps
that buffer at its tightest (one block plus one batch). Larger batches raise the
memory floor for no artifact benefit; much smaller ones pay per-batch overhead
and make the buffer do merging the source could have avoided. Configurable via
`duckdb.batch_size`; a second value is permitted in Phase C as an observation,
not as tuning.

---

## Dataset transfer — verified, ground truth carried over VERBATIM

Exported from the live `meshroad-ch` container, `ORDER BY row_id`, Parquet:

| file | bytes | sha256 |
|---|---|---|
| `~/bench-ch/perf_1m.parquet` | 39,298,826 | `7e23115e79c8727f889bd0fe5a693bb882aa9f374fde17e6ac56d5c55d1b0ad4` |
| `~/bench-ch/perf_10m.parquet` | 392,671,684 | `5b6b466c816dc77f1961772e5f20e914d2804db01ee74e2849f7f7c645fae209` |

Loaded by `bench/load-duckdb.py` into `~/bench-ch/meshbench.duckdb`. **10/10
aggregates match `bench/GROUND-TRUTH-clickhouse.json` on both tables**,
including the integer-authority sum (`11,994,337,292` / `120,020,064,468`) and
the null counts (`20,039` / `200,407`), plus `event_time` bounds and a
`row_id` monotonicity assertion. The ground-truth file was **not modified**.

## Gate B — status

| Condition | |
|---|---|
| Suite green | ✅ **393 passed / 51 skipped** (was 356) |
| e2e green under `--integration` | ✅ **11/11**, covering all six required proofs |
| — schema exact 14/14 | ✅ int64 / **string** (not large_string, B-3) / dict-status / double / timestamp[us] |
| — aggregate parity 10/10 | ✅ vs ClickHouse ground truth |
| — RF-002 armed | ✅ `score` null_count = 20,039 exact; no other column gained nulls; no NULL became NaN |
| — PG-011 refusal | ✅ incremental refused at config time |
| — dictionary artifact valid | ✅ `dictionary<values=string, indices=int32>`, 16 blocks |
| — checksum reproducibility | ✅ two consecutive pulls byte-identical (lane-scoped) |
| Ground-truth transfer verified | ✅ 10/10 both tables, before any e2e |
| MotherDuck | ⏸ **PARKED** — no `MOTHERDUCK_TOKEN`. Not attempted, not blocked. |
| `core/` untouched by the driver | ✅ no `duckdb` reference in `core/` |

## Deviations (disclosed; ratification is Kos's)

| # | Deviation | Why |
|---|---|---|
| B-d1 | Added a `duckdb.arrow: bool` config knob (default true) toggling `supports_arrow()` | Phase C's **P' cell** requires the same driver against the same source through the DataFrame lane. Without this it would need monkeypatching, which is not a configuration and could not be reproduced from a config file. |
| B-d2 | **Started the `meshroad-ch` container**, which was `Exited (255)` for 41 hours | The brief requires exporting from the live CH container, and its restore block specifies meshroad-ch is left UP. Started explicitly by name, not by pattern. `:8802`/`:8803` untouched. |
| B-d3 | No conformance `spec.py` for duckdb | The Phase 2 brief does not require one; the conformance generator is a Gate-A-of-Phase-1 artifact. Not in scope, called out so it is not assumed present. |

## Planes of record

| | Before | Now | |
|---|---|---|---|
| `meshroad-serve` (:8802) | active, MainPID **1123** | active, MainPID **1123** | ✅ |
| `meshroad-cockpit` (:8803) | active, MainPID **1315** | active, MainPID **1315** | ✅ |
| `meshroad-ch` | Exited (255), 41h | **Up** (per restore block) | ⚠️ B-d2 |
| `:8902` dev serve | not started | not started (Phase D) | — |
| meshroad `src/` / `gui/` | untouched | untouched | ✅ |
