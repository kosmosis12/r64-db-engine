# Phase 1 Ledger — DynamoDB close-out + Supabase profile

**Campaign:** CC Phase 1 · **Repo:** r64-db-engine · **Date:** 2026-08-13 · **Operator:** Kos

Filed, not fixed, except where a gate could not go green without the fix. Every
deviation is disclosed at the gate it occurred in. Ratification is Kos's call.

---

## Gate 0 — Repo-state audit

The 8/10 record was stale in both directions. Ground truth established before
anything moved; merge plan ratified in-session before the first merge.

| # | Finding | Disposition |
|---|---|---|
| L0-1 | DynamoDB branch was further along than recorded: tip `4e3a0e9` is *gate 4*, not "gates 0–3". 23 commits off `eeec158`. | Record corrected |
| L0-2 | `feat/clickhouse-driver` is a **dead lane** absent from the brief's listing. `git diff main..` is `.gitignore \| 2 ++` and nothing else; both its commits are titled `chore: gitignore local prompt scaffolding`. The CH driver was landed from a stash at `ecba7d9`, never from this branch. Local `e37742a` has also diverged from `origin/feat/clickhouse-driver` (`5e0acd0`). | **Excluded from merge plan** (ratified). Branch left untouched for Kos to delete or reconcile. |
| L0-3 | `.venv` lacked `boto3`/`botocore`, so `feat/dynamodb-driver` collected **0 tests** (13 collection errors). Not a code regression — `boto3==1.43.58` is declared in that branch's `pyproject`. | Installed the declared pin additively (`uv pip install`, no prune). Environment write during a no-writes phase — disclosed. |
| L0-4 | No `uv.lock` on `feat/dynamodb-driver`; the lockfile is a ClickHouse-lane artifact. `uv lock --check` failed post-rebase. | Fixed in Phase A (additive relock, no version drift) |
| L0-5 | `PG-011` and `RF-002` are brief shorthand, **not code identifiers** — zero grep hits repo-wide. Refusal of record is `Sink.supports_incremental` → `tests/core/test_sink.py::test_incremental_against_nonappendable_sink_fails_fast`. | Terminology mapped; no code change |
| L0-6 | Repo has a **single** `origin` (`github.com:kosmosis12/r64-db-engine`). There is no NAS remote and no partical mirror — the meshroad remote convention does **not** carry over. | Phase C push convention corrected. Verify before pushing. |

### L0-7 — DynamoDB retains an incremental capability the sink law makes unreachable

The DynamoDB driver ships a complete incremental path: `mode="incremental"`,
`incremental_key`, `incremental_mode` ∈ {`filter_scan`, `gsi_query`},
`incremental_gsi`, `incremental_gsi_partition_value`, and a returned
`new_watermark`. Five of the eleven integration proofs exercise it.

Those proofs call `driver.pull()` directly and bypass the daemon/sink wiring,
so they survive the rebase intact. But end-to-end the sink layer governs:
`_reject_incremental_on_nonappendable_sink` refuses an incremental config
against a non-appendable sink. `RamdbSink.supports_incremental()` returns
`True` (preserving v0.1 behaviour, with its own separately-tracked
read-your-own-output caveat), so ramdb still permits it; `ArrowIpcSink` does not.

**Net:** DynamoDB + incremental + ArrowIpcSink is refused, while DynamoDB +
incremental + ramdb is permitted. Whether that asymmetry is intended is a
strategic question, not a defect call. **Filed, untouched** — scope fence #2
forbids watermark/incremental work this campaign.

---

## Gate A — Merge, rebase, local re-prove

### L A-1 — `ascii_sanitize_series` crashed on an all-null column *(FIXED — gate blocker)*

**The one code defect found this campaign, and it is main's, not the driver's.**

```
AttributeError: Can only use .str accessor with string values, not floating
```

An interaction, not either half alone:

- The **sink split** made coercion null-preserving, so a source column with no
  values in the pulled page now arrives as an all-NaN `float64` column instead
  of being pre-filled.
- **pandas 3** leaves `.astype(str)` float-backed for such a column (pandas 2
  stringified NaN into a literal `"nan"`), and `.str` rejects a floating array.
- `pyproject` pins only `pandas>=2.0`; the resolved version is **3.0.5**.

Reproduces in two lines against `core/` alone — no DynamoDB involved. Postgres
or ClickHouse would hit it on any all-NULL column. DynamoDB merely triggers it
reliably, because items are sparse and an attribute absent across a whole scan
page produces exactly this column. It took out **4 of the 11** integration proofs.

Attribution was tested, not assumed: the pre-rebase commit `4e3a0e9` passes
**11/11 on the same pandas 3.0.5**, which rules out "pandas alone" and confirms
the interaction.

Fixed on `main` (`a7b1faf`) because that is where the defect lives, with an
early return for the all-null case. No existing test covered an all-null string
column — that is how it shipped. Two regression tests added, including the
`count(*)` vs `count(col)` discriminator against a fully-null column.

### L A-2 — botocore is invisible to `_is_permanent` *(FILED, not fixed — per brief)*

The CH campaign's suspicion is confirmed and quantified. `_is_permanent` reads
`exc.sqlstate` / `exc.diag.sqlstate` — psycopg attributes. botocore exceptions
carry neither, so every one falls through to `return False` (transient).

| botocore exception | should be | `_is_permanent` | verdict |
|---|---|---|---|
| `ResourceNotFoundException` (table gone) | permanent | `False` | **misclassified** |
| `AccessDeniedException` (bad IAM) | permanent | `False` | **misclassified** |
| `UnrecognizedClientException` (bad key) | permanent | `False` | **misclassified** |
| `ValidationException` (bad query) | permanent | `False` | **misclassified** |
| `NoCredentialsError` | permanent | `False` | **misclassified** |
| `ParamValidationError` | permanent | `False` | **misclassified** |
| `ProvisionedThroughputExceededException` | transient | `False` | ok |
| `ThrottlingException` | transient | `False` | ok |
| `InternalServerError` | transient | `False` | ok |
| `EndpointConnectionError` | transient | `False` | ok |

**6 of 10 misclassified.** The four "ok" rows are correct only by accident —
the function returns `False` unconditionally for botocore, so it has *zero*
discrimination, not partial. Operationally: a deleted table or a revoked IAM
key retries forever against the daemon's backoff instead of failing fast.

Not fixed: the brief directs filing this, and a correct fix means adding a
driver-owned classification hook rather than teaching `core/` about botocore.

### L A-3 — Local seed ceiling of ~35 items/s is obsolete *(record correction)*

Measured this session: **50,000 items in 3.80s ≈ 13,158 items/s** — ~376× the
recorded figure.

The 35 items/s ceiling was a disk-backed (SQLite write path), single-threaded
measurement. `scripts/dev_dynamodb.sh` now starts DynamoDB Local with
`-inMemory` (no SQLite write path at all) and `scripts/seed_dynamodb.py`
parallelizes across 8 workers (`5cdd731`).

This partially dissolves a stated premise of Phase B: the Local seed asymmetry
that phase exists to re-measure against real AWS has already been engineered
away locally. **The pagination, throttling, Decimal-dtype, scan-ordering and
sparse-attribute deltas remain fully valid reasons to run Phase B.**

### L A-4 — `dev_dynamodb.sh` publishes DynamoDB Local on all interfaces *(FILED)*

`scripts/dev_dynamodb.sh` starts the container with `-p "$PORT:8000"`, which
binds `0.0.0.0:8010`. The brief requires a loopback-only publish, matching the
meshroad-ch precedent.

Deviation taken: the container was started manually with
`-p 127.0.0.1:8010:8000` while keeping the repo helper's own semantics
(`ddb-test`, `-inMemory`, `--rm`), then `scripts/dev_dynamodb.sh env` emitted
the env file. Verified bound to `127.0.0.1:8010` only.

The helper itself is **unchanged** — this is a test-infrastructure change, and
the brief anticipates exactly this class as a ledger item rather than a
mid-campaign edit.

### Test-harness staleness carried onto the sink era *(fixed, disclosed)*

Both in the harness, not the driver:

1. `_real_daemon` handed `Daemon` a bare `RamdbWriter`; the sink-era daemon
   takes a `Sink` and calls `supports_incremental()`. Rebuilt through
   `RamdbSink` — the audited adapter over the same writer, so bytes are
   unchanged.
2. Sparse-attribute assertions expected `""` where the null contract now yields
   NULL. The empty-string fill moved to the ramdb format boundary, which is why
   the 50K round-trip proof still sees `""` after loading the artifact back.
   Asserting `""` pre-artifact would re-assert the fidelity loss the null
   contract removed. Strengthened into the discriminator.

Doctrine applied throughout: upstream hardening wins unless a regression can be
named. None could be — empty-string fill *is* the defect, because it makes a
missing attribute indistinguishable from one explicitly set to `""`.

---

## Gate A results

| Gate | Result |
|---|---|
| `main` after merge 1 (`feat/null-fidelity`, `--no-ff`) | **212 passed**, 20 skipped |
| `main` after merge 2 (`feat/clickhouse-e2e`, `--no-ff`) | **292 passed**, 27 skipped |
| `main` after coercion fix | **294 passed**, 27 skipped |
| Rebased `feat/dynamodb-driver` | **363 passed**, 38 skipped, 0 failed |
| Rebase conflicts | **1** — `drivers/__init__.py`, resolved as a three-way union |
| Integration proofs vs DynamoDB Local :8010 | **11 / 11** |
| Refusal test | executes, **passes**, not skipped |
| Discriminator | executes, **passes**, not skipped (5 tests) |
| Lint (`ruff`) | clean |

Baseline reconciliation: 244 (pre-rebase branch) and 292 (merged main) share
main's 175; union is 361, plus 2 coercion regression tests = **363**. No test
lost in either merge or the rebase.

---

## Phase B — not run

**Blocked on a Kos-side precondition.** `AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY` and `AWS_DEFAULT_REGION` are all unset, and the `aws`
CLI is not installed. Resequencing ratified in-session: A → D now, B and the
Phase C main-merge held.

No AWS resource was created, so the spend fence was never approached.
DynamoDB status language remains **local-proven only** — it has *not* earned
"e2e-proven vs real AWS, not reference-grade".
