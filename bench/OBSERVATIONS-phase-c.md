# Phase C observations — ClickHouse → ArrowIpcSink

**These are OBSERVATIONS, not benchmark claims.** The machine was not quiesced,
no quiet-machine gate was applied, and n=1 per figure. They exist to attribute
cost to the right component and to size the pipeline, nothing more. Nothing here
may be quoted as a benchmark result.

Captured 2026-08-10. Loadavg at start of each run: 1.29 (1M), 0.71 (10M
unordered), 3.08 (10M ordered) — all above or around the `< 1.0` gate the
Phase E lanes require, which is another reason these are context only.

## Artifacts of record

Phase D and Phase E both run on these, and only these:

| artifact | rows | size | sha256 |
|---|---:|---:|---|
| `~/bench-ch/out/perf.arrow` | 1,000,000 | 149,806,522 B (142.9 MiB) | `db2912dfbd6a42337704e121f6872484f2e51eca7439cfe223d4bb72cc73ea4a` |
| `~/bench-ch/out/perf10m.arrow` | 10,000,000 | 1,497,969,210 B (1428.6 MiB) | `640a73894ca21be6a1ee8cad1b6fa329fad9755f4e41cec37cb1eaa4b035d473` |

Both pulled with an explicit `ORDER BY row_id` (see below), so both are sorted
by `row_id` and locality-comparable to the row_id-sorted meshroad lineage
artifact.

## Wall-time decomposition

Instrumentation wraps the real call sites on the real daemon path
(`bench/pull-observations.py`), so these are the pipeline's own timings, not a
reimplementation's.

Ordered pulls (the artifacts of record):

| stage | 1M | | 10M | |
|---|---:|---:|---:|---:|
| ClickHouse query | 1.11 s | 5.7% | 9.54 s | 4.9% |
| coercion | 18.37 s | 93.5% | 183.42 s | 94.5% |
| sink write | 0.11 s | 0.6% | 0.92 s | 0.5% |
| **wall** | **19.64 s** | 100% | **194.00 s** | 100% |
| peak RSS | 917 MiB | | 6697 MiB | |

Unordered pulls, for comparison — the `ORDER BY` cost lands entirely in the
already-decomposed ClickHouse-query stage and is negligible:

| stage | 1M unordered | 1M ordered | delta |
|---|---:|---:|---:|
| ClickHouse query | 1.06 s | 1.11 s | +0.05 s |
| coercion | 18.26 s | 18.37 s | +0.11 s |
| sink write | 0.10 s | 0.11 s | — |
| wall | 19.48 s | 19.64 s | +0.16 s |

Everything scales linearly in row count. No superlinear term appears between 1M
and 10M in any stage (coercion 10.0×, query 8.6×, sink 8.4×, size 10.0×).

## HARDENING ITEM — vectorize `_pre_coerce_values` — ~18 s of ~19.5 s at 1M

**Filed, deliberately NOT optimized in this campaign.**

`ClickHouseDriver.pull` runs `_pre_coerce_values`, which maps EVERY VALUE through
`ch_coercion.coerce_value` via `Series.map`. That is a Python-level call per
cell: 14M calls for the 1M pull (1M rows × 14 columns) and 140M for the 10M
pull. It is ~94% of wall time at both sizes, and it is why a pull that reads
9.5 s worth of ClickHouse data takes 194 s end to end.

> **The ~94% coercion cost is r64-db-engine's ingest cost, not ClickHouse's and
> not the sink's.** ClickHouse serves 10M rows in 9.5 s and the Arrow sink
> writes 1.43 GiB in 0.92 s. Any "ingest is slow" reading of these numbers that
> lands on either of those components is wrong.

The obvious remedy is vectorization — most `coerce_value` branches are per-dtype
operations pandas can do columnwise — but that is a behavioural change to the
coercion layer, which is exactly the layer the conformance suite pins. It
belongs in its own change with its own tests, not inside a benchmark campaign.

## Memory amplification

**Peak RSS is ≈ 4.7× the output artifact size** (6697 MiB peak for a 1428 MiB
file; 917 MiB for 143 MiB). `query_df` materializes the full result set on both
the clickhouse-connect side and the pandas side before coercion begins, so the
pull's memory ceiling scales with the whole table rather than with a batch. A
100M-row table of this shape would not fit in this machine's 31 GiB. Neither
driver streams — the Postgres reference materializes too — so this is parity,
not a ClickHouse-specific gap.

## RESOLVED — row order, and byte-reproducibility

Originally observed as an anomaly: two full-refresh pulls of a provably
unchanged `perf_1m` produced artifacts of **different byte length**
(149,806,586 vs 149,806,714). Row *sets* were identical — no rows lost or
duplicated — but row *order* differed between pulls, and neither pull was
sorted by `row_id`. A full refresh issued no `ORDER BY`, so ClickHouse returned
rows in whatever order its parallel scan produced, and that order varied run to
run.

No correctness impact was ever in question: every parity assertion is
order-independent (counts, sums, uniques, null counts), which is why all ten
matched exactly on both pulls. That is by design, not luck.

**Resolved by pulling with an explicit `ORDER BY row_id`.** Verified rather than
assumed, in three steps:

1. The driver wraps an inline-SQL source as `SELECT ... FROM (<source>) AS sub`,
   and ClickHouse does not guarantee that such a wrapper preserves an inner
   ORDER BY. Checked directly: 0 out-of-order adjacent pairs across all
   1,000,000 rows.
2. The written artifact is sorted — `row_id` runs 0 … 999,999 in order.
3. **Two ordered pulls produced byte-identical files** — same size and same
   sha256 (`db2912df…`). Verification is therefore upgraded from
   verify-by-content to **verify-by-checksum**.

The artifact is now locality-comparable to the meshroad lineage artifact, which
is also `row_id`-sorted. Note that the lineage artifact carries `row_id`
1..1,000,000 while meshbench carries 0..999,999: it is a SCHEMA reference — the
types the zero-copy reader expects — not the same data.

**Not benchmarked, and stated as such in the findings doc:** the unordered pull
shape — which is what a production full refresh actually emits — was not
measured for scan locality. Ordered was chosen for determinism and lineage
comparability.
