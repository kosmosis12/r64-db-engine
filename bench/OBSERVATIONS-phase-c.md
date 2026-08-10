# Phase C observations — ClickHouse → ArrowIpcSink

**These are OBSERVATIONS, not benchmark claims.** The machine was not quiesced,
no quiet-machine gate was applied, and n=1 per figure. They exist to attribute
cost to the right component and to size the pipeline, nothing more. Nothing here
may be quoted as a benchmark result.

Captured 2026-08-10. Loadavg at start: 0.71 (10M run), 1.29 (1M run) — both
above the `< 1.0` gate Phase E lanes require, which is another reason these are
context only.

## Wall-time decomposition

Instrumentation wraps the real call sites on the real daemon path
(`bench/pull-observations.py`), so these are the pipeline's own timings, not a
reimplementation's.

| stage | 1M | | 10M | | scaling |
|---|---:|---:|---:|---:|---:|
| ClickHouse query | 1.06 s | 5.5% | 9.20 s | 4.6% | 8.7× |
| coercion | 18.26 s | 93.7% | 188.61 s | 94.8% | 10.3× |
| sink write | 0.10 s | 0.5% | 0.95 s | 0.5% | 9.5× |
| **wall** | **19.48 s** | 100% | **198.92 s** | 100% | 10.2× |

| | 1M | 10M | scaling |
|---|---:|---:|---:|
| output size | 142.9 MiB | 1428.6 MiB | 10.0× |
| peak RSS (pull process) | 916.9 MiB | 6709.2 MiB | 7.3× |

Everything scales linearly in row count. No superlinear term appears between 1M
and 10M in any stage.

## HARDENING ITEM — coercion dominates, ~94% of wall

**Filed, deliberately NOT optimized in this campaign.**

`ClickHouseDriver.pull` runs `_pre_coerce_values`, which maps EVERY VALUE through
`ch_coercion.coerce_value` via `Series.map`. That is a Python-level call per
cell: 14M calls for the 1M pull (1M rows × 14 columns) and 140M for the 10M
pull. It is ~94% of wall time at both sizes, and it is why a pull that reads
9.2 s worth of ClickHouse data takes 199 s end to end.

The correct attribution matters for the campaign: **this is r64-db-engine's
ingest cost, not ClickHouse's and not the sink's.** ClickHouse serves 10M rows
in 9.2 s and the Arrow sink writes 1.43 GiB in 0.95 s. Any "ingest is slow"
reading of these numbers that lands on either of those components is wrong.

The obvious remedy is vectorization — most `coerce_value` branches are
per-dtype operations pandas can do columnwise — but that is a behavioural change
to the coercion layer, which is exactly the layer the conformance suite pins.
It belongs in its own change with its own tests, not inside a benchmark
campaign.

## Memory amplification

Peak RSS is ~4.7× the output artifact size (6709 MiB peak for a 1428 MiB file).
`query_df` materializes the full result set on both the clickhouse-connect side
and the pandas side before coercion begins, so the pull's memory ceiling scales
with the whole table rather than with a batch. A 100M-row table on this shape
would not fit in this machine's 31 GiB. Neither driver streams — the Postgres
reference materializes too, so this is parity, not a ClickHouse-specific gap.

## ANOMALY — row order is not stable across pulls

Two full-refresh pulls of the same, provably unchanged `perf_1m` produced
artifacts of **different byte length** (149,806,586 vs 149,806,714).
Investigated rather than rounded away:

- row **sets** are identical across pulls — no rows lost or duplicated
- row **order** differs between pulls
- neither pull is sorted by `row_id`; both happen to begin at 327680

The pull issues no `ORDER BY` (correctly — a full refresh has no reason to pay
for a sort), so ClickHouse returns rows in whatever order its parallel scan
produces, and that order varies run to run.

Consequences, stated precisely:

- **No correctness impact on anything checked here.** Every parity assertion is
  order-independent (counts, sums, uniques, null counts), which is why all ten
  matched exactly on both pulls. That is by design, not luck.
- **The artifact is not byte-reproducible.** A pull cannot be verified by
  checksum against a previous pull; it must be verified by content.
- **Scan locality differs from the reference artifact.** The meshroad reference
  `perf_1m.arrow` IS sorted by `row_id`. Ours is not. For Phase E this is worth
  keeping in view: any op whose cost depends on value locality (GROUP BY
  clustering, dictionary index runs) is measured on differently-ordered bytes
  than the lineage artifact was.

Note also that the reference artifact carries `row_id` 1..1,000,000 while the
meshbench dataset carries 0..999,999. The reference is a SCHEMA reference — the
types the meshroad zero-copy reader expects — not the same data.

**Open decision for Phase E:** add `ORDER BY row_id` to the benchmark pull to
make the artifact deterministic and locality-comparable to the reference, at
the cost of a ClickHouse-side sort. Not taken unilaterally, because it changes
the artifact the benchmark measures.
