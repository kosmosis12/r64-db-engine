---
name: r64-connector-factory
description: >
  Drive a first-party driver campaign on the r64-db-engine Driver ABC, from
  topology audit through conformance-green, with zero edits to core/. Encodes
  the full gate battery as a template: Phase 0 topology audit (gated on Kos
  ack) → research phase producing DRIVER-PLAN.md (fixed table with named trap
  rows: auth model, pagination, rate limits, int32/int64 ceiling, Decimal,
  timestamp + session timezone, null vs NaN semantics, scan-order determinism,
  sandbox strategy, teardown) → Gate A repo-green → Gate B seed + ground-truth
  transfer with mandatory B-2 min/max boundary assertion → Gate C conformance
  via factory/conformance.py → Gate D dev-serve zero-copy → Gate E bench
  (optional, full bench doctrine). The plan is ratified by Kos BEFORE any code.
  Use when adding a database or warehouse connector to the ingestion plane.
  Trigger on: add a <X> driver, new connector, build the <X> integration,
  driver campaign, factory a driver, DRIVER-PLAN, admit a dialect, Snowflake
  driver, BigQuery driver, Iceberg, ADBC, dialect registry, Gate A/B/C/D/E.
  Composes with r64-conformance (Gate C), r64-recipe-engine (promotion path),
  and meshforge (the Four Laws that bind this skill).
---

# r64-connector-factory

Repo: `/home/kos/builds/r64-db-engine`. Read `meshforge` first — the Four Laws
bind everything here, especially Law 4 (a driver is admitted only through the
conformance battery).

**The load-bearing property of this whole design: adding an integration touches
no shared code.** You will register a driver, never modify `core/`. Preserve
that in what you write, and prove it with a grep that returns nothing.

---

## Campaign shape

```
Phase 0  topology audit ........... GATED — report and WAIT for Kos ack
Phase R  research ................. produces DRIVER-PLAN.md — GATED on Kos ack
Gate A   repo green ............... suite green, driver registered, refuses loudly
Gate B   seed + ground truth ...... dataset at source, transfer proven (B-2 mandatory)
Gate C   conformance .............. factory/conformance.py green, evidence pack
Gate D   dev-serve zero-copy ...... copied_columns = 0 cold and warm
Gate E   bench (OPTIONAL) ......... full bench doctrine or not at all
         cross-agent QA ........... MANDATORY before merge. Builder ≠ auditor.
```

Two gates stop for a human: Phase 0 and the DRIVER-PLAN. Everything after runs
to conformance-green, then stops at the QA gate. **Do not merge to main.**

---

## Phase 0 — topology audit (GATED)

Report and wait. Do not write code.

1. **Git**: `git branch -a`, current `main` SHA, dirty state, remotes, whether
   the campaign branch already exists. Name any branch you must not disturb.
2. **Read, do not assume**, that the machinery your campaign depends on is
   present on the branch you are basing off — not on some other branch:
   - `Driver` ABC (`core/driver.py`) and the driver registry
     (`drivers/__init__.py`: `DRIVERS` + `resolve()`).
   - Registry-derived dialect resolution in `core/config.py` (PG-010): an
     unregistered dialect is refused at config time, listing the registry.
   - `ArrowIpcSink` explicit `pa.ipc` writer, `_BLOCK_ROWS = 65536`.
   - PG-011 refusal wired into `Daemon.__post_init__`, not merely defined.
   - The ground-truth file and dataset generator for your target.
3. **Source availability**: container exists (start by explicit name if Exited;
   leave UP at close), expected row counts, host connectivity actually tested.
4. **Report drift from the brief's premises, and WAIT.**

A brief written days earlier is a hypothesis about the repo. Check every
premise before building on it. Grep assertions in particular: verify a proposed
gate grep returns 0 *today* before adopting it, or you will adopt an
unfalsifiable one.

---

## Phase R — research → `DRIVER-PLAN.md` (GATED)

Research the source's real behaviour: official docs, the client library's
actual type mapping, and a live probe where a sandbox exists. Then write
`DRIVER-PLAN.md` at the repo root and **stop for ratification**.

The table below is fixed. Every row must be filled — "N/A" is an acceptable
answer, silence is not. The trap rows are named because each one has already
cost somebody a green-but-wrong artifact.

````markdown
# DRIVER-PLAN — <dialect>

Status: DRAFT — awaiting Kos ratification. No code until ratified.

| # | Row | Decision | Evidence |
|---|---|---|---|
| 1 | **Auth model** | | |
| 2 | **Pagination / fetch shape** | | |
| 3 | **Rate limits & retry** | | |
| 4 | **TRAP: int32/int64 ceiling** (RF-001 class) | | |
| 5 | **TRAP: Decimal handling** | | |
| 6 | **TRAP: timestamp + session timezone** (B-2 class) | | |
| 7 | **TRAP: null semantics vs NaN** (RF-002 class) | | |
| 8 | **TRAP: scan-order determinism** | | |
| 9 | **Type map (full)** | | |
| 10 | **Sandbox strategy** | | |
| 11 | **Teardown plan** | | |

## Row notes — what each row must actually answer

1. **Auth model.** Which mechanism, and where the secret lives. It lives in a
   0600 file referenced BY PATH. Never a config value, never in context.
   Prefer a design needing zero credentials if one exists.

2. **Pagination / fetch shape.** Cursor, offset, keyset, or a single streaming
   cursor. State the batch size and why. Note whether the source can change
   underneath a multi-page read, and what that does to consistency.

3. **Rate limits & retry.** Documented limits, the backoff, and the cap. A
   retry that reinterprets a response is Law 1 violation — retry the REQUEST,
   never the meaning.

4. **TRAP: int32/int64 ceiling (RF-001 class).** Does any integer type exceed
   int64, or get narrowed in transit? Lineage: `row64tools`' ramdb codec
   narrowed int64 to int32 SILENTLY — a seeded `3548933426` loaded back as
   `-746033870` (PG-001). Arrow's int64 is native, so on the Arrow lane the
   defect is unrepresentable. Name every source type that cannot round-trip
   and make it RAISE rather than narrow. UInt64 has no signed representation:
   refuse it.

5. **TRAP: Decimal handling.** Decimal→float64 loses precision for money.
   Decide explicitly, and if you take float, say what the exact-integer
   authority is for the aggregate check (see Gate B).

6. **TRAP: timestamp + session timezone (B-2 class).** THE most dangerous row.
   **Snowflake defaults to `America/Los_Angeles`.** DuckDB defaults to the
   machine's local zone — on the meshcave box that same LA, which shifted every
   `event_time` by eight hours through a naive `CAST(... AS TIMESTAMP)`, and
   **every ground-truth check still passed**, because count, sum, null count
   and cardinality are all invariant under a uniform translation. The artifact
   would have been eight hours wrong and fully green.
   State: the source's session TZ default, how you pin it (`SET TimeZone='UTC'`
   on the connection, in config, not in a script), and the target unit.
   Fix at the LOAD, never in the ground truth.

7. **TRAP: null semantics vs NaN (RF-002 class).** Does the source distinguish
   NULL from NaN, and does the client? Both ways of losing null-ness are
   silent: zero-fill drags every `mean()` down while totals stay plausible; a
   literal NaN sets `null_count = 0` and poisons every downstream `sum()`.
   Name the columns that will DISCRIMINATE (`count(col) != count(*)`) and their
   exact null counts — that goes into the conformance spec.
   Note: pandas conflates SQL NULL and `'NaN'::float8` into one float64 NaN
   upstream of any sink, so the distinction must be preserved in the driver's
   coercion layer or it is gone.

8. **TRAP: scan-order determinism.** Does a bare `SELECT` promise an order? On
   ClickHouse it does NOT — parallel part/granule reads returned the same
   1,000,000 rows in a different order on consecutive pulls (`row_id` starting
   at 196608, then 131072), which also permuted the dictionary encoding because
   dictionary values are assigned in first-seen order. Row count, schema, block
   layout and every aggregate were identical; only the bytes differed.
   **Decide an ORDER BY and VERIFY BY CHECKSUM.** Note the cost. A production
   config without a pinned order is still correct — only byte-reproducibility
   is lost — so say which you are choosing and why.

9. **Type map (full).** Source type → pandas/Arrow type, with `unsupported`
   spelled out for anything that must raise. Wrapper types (`Nullable(...)`,
   `LowCardinality(...)`) unwrapped before mapping.

10. **Sandbox strategy.** Local container preferred (no credentials, no
    egress, reproducible). If a hosted trial is unavoidable, say what data
    goes there — nothing real.

11. **Teardown plan.** What is created and how it is removed. Never mutate a
    ground-truth table: build a purpose-named probe table and drop it in a
    `finally`. Mutating the seeded dataset would invalidate the committed
    ground truth for every other check and break its regenerate-identically
    property.
````

---

## Gate A — repo green, driver registered

- New driver under `src/r64_db_engine/drivers/<dialect>/`, `driver.py` +
  `coercion.py`, subclassing `Driver`, `dialect_name()` returning the dialect.
- Registered in `drivers/__init__.py` — **one dict entry. That is the entire
  wiring.**
- Config: the dialect's block passes through opaquely; the driver validates its
  own keys and refuses bad ones itself.
- Unit tests for the coercion table, including every type that must RAISE.
- Suite green: `PYTHONNOUSERSITE=1 .venv/bin/pytest`.
- **Zero-core-edit proof:** `git grep -rniE "\b<dialect>\b" src/r64_db_engine/core/`
  returns nothing, and `git status --porcelain src/r64_db_engine/core/` is empty.

## Gate B — seed + ground-truth transfer

- Dataset generator committed and **deterministic by construction**: zero
  `rand()`/`now()`/uuid, every column derived from a seeded hash. This is what
  lets ground truth be a committed fixed expectation rather than a
  re-measurement. A parity miss is then unambiguously a pipeline defect.
- Ground truth captured **AT THE SOURCE**, into JSON. A pipeline checked
  against its own output proves only that it is self-consistent.
- Aggregates include an **exact-integer authority** for any float quantity:
  `SUM(CAST(ROUND(x * 100) AS BIGINT))` gates; `ROUND(SUM(x) * 100)` corroborates
  only. Float addition is not associative and sources sum in parallel.
- **B-2 boundary assertion is MANDATORY**, per the transfer doctrine:

  > Any cross-engine dataset transfer requires a min/max boundary assertion on
  > at least one timezone- or order-sensitive column. Aggregate parity is blind
  > to uniform shifts.

## Gate C — conformance

Use the `r64-conformance` skill. Write `factory/targets/<dialect>-<dataset>.yaml`
(a normal engine config — it must validate through `core.config.Config`
untouched) and `factory/specs/<dataset>-schema.json`, register a live-source
probe in `factory/probes.py`, then:

```bash
PYTHONNOUSERSITE=1 .venv/bin/python -m factory.conformance \
  --dialect <dialect> --config factory/targets/<dialect>-<dataset>.yaml \
  --ground-truth bench/GROUND-TRUTH-<dialect>.json \
  --table <table> --evidence-dir factory/evidence --serve-gate
```

Exit 0 is the gate. Evidence pack committed. If the battery cannot check a
property your driver needs proven, **extend the battery first** (Law 4).

## Gate D — dev-serve zero-copy

Covered by `--serve-gate`. `copied_columns = 0` cold and warm; warm miss rate
0% and warm `columns_decoded = 0`. Counters are CUMULATIVE — judge deltas.
Ephemeral serve on its own port, never 8802, torn down by PID.

## Gate E — bench (OPTIONAL)

Full bench doctrine or **nothing**. Persist-or-pass gating before every
invocation, `systemd-inhibit --what=idle:sleep` for the series, discard-and-
rerun on abort (never splice). Absent that, everything perf-flavoured is an
untimed observation, labelled as such.

---

## Close-out

`CLOSEOUT-driver-<dialect>.md`: per-gate results, numbered deviations for
ratification, deferred-items ledger, claims-register candidates **FENCED**.

Push the branch. **Do not merge** — cross-agent QA is mandatory between
conformance-green and merge, and the builder is never the auditor. Say so
explicitly and name what to audit.
