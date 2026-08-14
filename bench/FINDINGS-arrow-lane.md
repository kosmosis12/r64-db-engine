# FINDINGS — Arrow-native lane (Phase 2)

**Campaign:** CC Phase 2 · **Date:** 2026-08-14 · **Machine:** cachyPC
**Branch:** `feat/arrow-lane` off `main` @ `7820d18`
**Status at this point:** **Phase A complete. No performance work done, no
performance claim made.** Phases B (DuckDB), C (probe), D (serve) not started.

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
