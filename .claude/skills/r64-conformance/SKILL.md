---
name: r64-conformance
description: >
  Run and read the r64-db-engine conformance battery — the machine-checkable
  oracle that admits a driver or recipe book. Wraps factory/conformance.py:
  ten parameterized checks per registered dialect (registry admission,
  schema exactness with the B-3 string-width fence, aggregate parity vs
  source-captured ground truth with exact-int authority, RF-002
  dataset-declared null discriminators, B-2 min/max timezone boundary,
  PG-011 watermark refusal, 65536 Arrow IPC block structure, lane-scoped
  checksum, recipe-lane destination-pinning mutations, and an optional
  zero-copy dev-serve gate asserting copied_columns=0). Emits a two-form evidence pack; the exit code is the
  verdict. Use when running or extending the battery, adding a new target or
  spec, or reading an evidence pack to ratify a driver. Trigger on: run
  conformance, admit the driver, conformance battery, evidence pack for <X>,
  is <X> reference-grade, conformance failed, extend the battery, factory
  target, schema spec, serve gate, copied_columns. Composes with
  r64-connector-factory (Gate C), r64-factory-maintenance (weekly re-runs),
  and meshforge (Law 4).
---

# r64-conformance

Repo: `/home/kos/builds/r64-db-engine`. The battery lives in `factory/`,
deliberately outside `src/`, so core cannot import its own oracle.

## Run it

```bash
export PYTHONNOUSERSITE=1
.venv/bin/python -m factory.conformance \
  --dialect clickhouse \
  --config factory/targets/clickhouse-meshbench.yaml \
  --ground-truth bench/GROUND-TRUTH-clickhouse.json \
  --table perf_1m \
  --evidence-dir factory/evidence \
  --serve-gate
```

Exit **0** only if every non-skipped check passed. That exit code is the
verdict — do not re-interpret the output.

Optional flags: `--spec` (overrides the conventional path), `--work-dir`
(run-scoped artifact/state dir), `--date` (evidence filename stamp),
`--serve-addr` (default `127.0.0.1:8903` — **never 8802**),
`--meshroad-binary`, `--allow-dirty` (stamps ALLOW-DIRTY; the pack then
ratifies no commit), `--repair-store` (re-copy a corrupted content-addressed
store entry and record the repair — without it, corruption is a hard refusal).

## Three files, three jobs

- `--config` → `factory/targets/<dialect>-<dataset>.yaml`. A **normal engine
  config**. It validates through `core.config.Config` untouched and would run
  under the daemon as-is; a battery-only format would prove something about a
  configuration nobody ships. It therefore carries **no battery keys** — core
  refuses unknown top-level keys against the driver registry, so they would not
  even parse.
- `--ground-truth` → expectations captured **at the source**.
- the spec → `factory/specs/<dataset>-schema.json`. Everything the battery
  needs that a config cannot carry. Resolved by convention: the target stem
  with its `<dialect>-` prefix removed, so `targets/clickhouse-meshbench.yaml`
  → `specs/meshbench-schema.json`. A missing spec is refused loudly.

Spec keys: `columns` (name+type, order significant), `string_width_tolerant`,
`discriminators` (+ optional `discriminators_absent_reason`),
`boundary_columns`, `aggregates`, `serve_gate_sql`.

## The ten checks

| # | check | what a FAIL means |
|---|---|---|
| 1 | `registry_admission` | the dialect does not resolve, or an unregistered one is accepted, or the refusal does not LIST the registry (PG-010) |
| 2 | `schema_exactness` | column set, ORDER, or a type drifted from the spec |
| 3 | `aggregate_parity` | a gating aggregate disagrees with source-captured truth |
| 4 | `rf002_null_discriminator` | nulls were filled, miscounted, or smuggled through as NaN |
| 5 | `b2_boundary` | artifact min/max disagrees with the live source — a uniform shift |
| 6 | `pg011_refusal` | incremental on a non-appendable sink was accepted or silently downgraded |
| 7 | `block_structure` | Arrow IPC blocks are not the 65536 layout the consumer's cache is keyed on |
| 8 | `checksum` | two same-lane pulls are not byte-identical |
| 9 | `recipe_security_invariants` | a destination-pinning mutation of the shipped recipe book was ACCEPTED (recipe lane only; SKIPPED-with-reason elsewhere) |
| 10 | `zero_copy_serve_gate` | a column was copied instead of mapped, or the warm pass still decoded |

Three of these have subtleties worth knowing before you read a result:

- **Aggregate authority (3).** The exact-int form
  (`SUM(CAST(ROUND(x*100) AS BIGINT))`) **gates**; the float form corroborates
  only and its mismatch is reported without failing. Float addition is not
  associative and sources sum in parallel. If you find yourself wanting a
  second `corroborating: true`, fix the aggregate instead.
- **RF-002 is dataset-declared (4).** Not "≥N nullable columns" — that is
  vacuous on a wide dataset and unsatisfiable on a narrow one. The spec names
  the columns and their **exact** null counts, cross-checked against the
  ground-truth file; disagreement between the two is itself the finding.
  Floor: one declared. Zero is allowed only with
  `discriminators_absent_reason`, which yields SKIPPED-with-reason.
- **Checksum is lane-scoped (8).** Byte-identity is asserted only WITHIN a
  lane. Cross-lane equivalence is **data + schema-minus-metadata + block
  structure**, tolerating string width (`string` vs `large_string`, the B-3
  fence). **Never compare an N sha to a P' sha.** On mismatch the check
  decomposes the difference and NAMES the residual instead of reporting an
  unexplained failure — scan order is the usual answer.

## Reading an evidence pack

`factory/evidence/EVIDENCE-<dialect>-<YYYYMMDD>.{json,md}`. Read the `.md`.

1. **Verdict line, line 3.** `VERDICT: PASS — n passed, n failed, n skipped`.
   A verdict reading `PASS (ALLOW-DIRTY)` ratifies NO commit — the tree did not
   match HEAD — and must not be used to admit a driver.
2. **Summary table.** Ten rows. If there are not ten, the battery shrank —
   that is a finding, not a convenience.
3. **Skips.** Every skip states a reason. A skip without one is a bug in the
   check.
4. **Per-check tables.** Both sides of every comparison, passing ones included.
   The bar: *ratify the driver from this file without reading the diff.*
5. **Environment.** `pyarrow` owns the IPC block layout and `pandas` decides
   string width; a cross-run difference that is not explained by these two is
   worth chasing.
6. **Provenance and CLOSURE BOUNDARY.** Confirm `ratifies_head: true`, that
   the six pinned inputs carry sha256s, and that `artifact.store_verified` is
   true — a content-addressed filename is a claim about bytes, and the pack
   asserts it only after re-hashing what is stored. The CLOSURE BOUNDARY
   section names what the pack deliberately does NOT establish.
7. **Sanity floor.** Confirm `artifact.rows` and `artifact.blocks` are what you
   expect. Every check can pass on an empty artifact.

## Extending the battery

**Law 4: if the battery can't check it, the factory can't ship it — extend the
battery first.** Never widen a tolerance to make a run green.

Architecture to preserve: **judges are pure.** `factory/battery.py` takes
gathered facts and returns verdicts with no I/O; `factory/conformance.py` does
all gathering. That split is what makes the oracle provably able to fail.

To add a check:

1. Write the judge in `battery.py` as a pure function returning `CheckResult`.
2. Gather its inputs in `conformance.py`; add declarative keys to the spec if
   it needs per-dataset parameters. Keep the vocabulary CLOSED — an unknown op
   is refused, not guessed at.
3. **Write the negative fixture in `tests/factory/test_battery.py`.** A check
   with no proof it can FAIL is a green light with no bulb behind it. The
   fixture is a complete SCENARIO with one thing broken, and its manifest
   declares the verdict of every check — nine PASSes are a claim that the
   mutation was surgical. Adversarial stubs are replayed against the same
   scenario with identifiers anonymized, so a fixture that could be satisfied
   by pattern-matching is caught. This step is not optional and is not
   follow-up work.
4. Add it to `battery.CHECK_NAMES`, and declare each of its reason codes in
   `battery.MECHANISMS` with the mechanism that owns it. The fixture guards are
   DERIVED from those registries, so a check added without a negative case
   turns `tests/factory/test_battery.py` red on its own — there is no separate
   list to remember to update.

To add a **target**: write the yaml + spec, and register a live-source probe in
`factory/probes.py`. A dialect with no probe cannot be admitted — B-2 compares
against the LIVE source, and an unprobed boundary is exactly the gap B-2 exists
to close. The probe deliberately does not use the driver under test: a fixture
that verified the driver with the driver could hide a fault in both directions.

## Tests

```bash
.venv/bin/pytest tests/factory -q                 # pure, fast, no container
.venv/bin/pytest tests/factory --integration -q   # live ClickHouse, ~2.5 min
```
