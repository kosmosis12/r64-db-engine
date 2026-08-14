# CLOSE-OUT — CC Phase 1

**Date:** 2026-08-13 · **Operator:** Kos · **Repo:** r64-db-engine
**Gates reached:** 0 ✅ · A ✅ · B ⛔ blocked · C ⏸ held · D ✅

Gate 0/A detail lives in `bench/LEDGER-phase1.md` (on `feat/dynamodb-driver`).
Phase D detail lives in `bench/FINDINGS-supabase-profile.md` (this branch).
This document is the campaign-level summary, the Phase D ledger, and the
proposed claims-register diff.

---

## 1. What shipped

| Branch | Head | State |
|---|---|---|
| `main` | `a7b1faf` | CH stack merged (2 × `--no-ff`) + coercion fix. **294 passed** |
| `feat/dynamodb-driver` | `b729299` | Rebased onto new main, +26. **363 passed**, 11/11 local |
| `feat/supabase-profile` | `19901f2` | New. **310 passed**, 13/13 live e2e |
| `feat/clickhouse-driver` | `e37742a` | **Untouched dead lane** — see L0-2 |

Merge plan executed exactly as ratified at Gate 0. Suite green after **each**
merge, not just at the end. `feat/clickhouse-driver` excluded.

---

## 2. Deviations taken (all disclosed at their gate; ratification is Kos's)

| # | Deviation | Ratified |
|---|---|---|
| 1 | Excluded `feat/clickhouse-driver` from the merge plan — it is `main` + a 2-line `.gitignore` change | Gate 0, in-session |
| 2 | Installed `boto3==1.43.58` into `.venv` during a no-writes phase, to make the branch's own suite runnable at all | Gate 0, disclosed |
| 3 | Resequenced to A → D, holding B and the Gate C merge, because AWS credentials **and** the `aws` CLI are both absent | Gate 0, in-session |
| 4 | Fixed `core/coercion.py` on `main` rather than filing it — Gate A could not go green otherwise, and the defect is main's, not the driver's | Gate A, disclosed |
| 5 | Started DynamoDB Local with a **loopback-only** publish, deviating from the repo helper which binds `0.0.0.0` | Gate A, disclosed |
| 6 | One driver change in Phase D (`prepare_threshold` passthrough) — not expressible in config | Gate D, in-session |
| 7 | Substituted `public.intel_logs` for the named `company_signals`, which exists in no running stack | Gate D, in-session |

---

## 3. Phase D ledger

Full detail in `bench/FINDINGS-supabase-profile.md` §5. Summary:

| # | Item | Disposition |
|---|---|---|
| D-1 | `prepare_threshold` is a psycopg kwarg, not a libpq option — cannot be enforced from config alone | **Fixed**, ratified. Default 5 = psycopg's own, so existing behaviour is unchanged |
| D-2 | `vector`, `text[]`, `numeric[]`, `jsonb` all land as `string` — type carried in the value, not the Arrow schema | **Filed.** Representability observation, same class as the `.ramdb` finding |
| D-3 | `meshroad stats` is schema-bound to the meshbench `perf` table (hardcoded `amount`) and cannot profile an arbitrary artifact | **Filed.** meshroad `src/` frozen; counters obtained via `get_flight_info` app_metadata instead |
| D-4 | `pyarrow.feather.write_feather` deprecated as of pyarrow 24.0.0; `ArrowIpcSink` emits a `FutureWarning` per write | **Filed**, untouched |
| D-5 | Postgres scan order stable without `ORDER BY`; artifact byte-reproducible | Observation, **not** promoted to a guarantee |

### PG-010 finding raised in Phase D, applies to Phase C

**DynamoDB cannot be declared in a config file.** `Config.dialect` is
`Literal["postgres", "clickhouse"]` — there is no `dynamodb`. The integration
tests work around it by declaring `"dialect": "postgres"` with
`"database": "unused-config-vessel"`.

The driver is proven, but nothing can *configure* it through the normal YAML
path. **This blocks the Phase C Sources-tab roster flip**, which needs a real
DynamoDB source entry. Filed, not fixed — closing it means widening the core
`Literal`, which is the PG-010 leak itself and deserves its own decision.

---

## 4. Proposed claims-register entries — DIFF, NOT APPLIED

> No claims-register file exists at a canonical path in either repo, so these
> are presented as a proposed diff rather than written anywhere.
> **Register entries are Kos-ratified only. Nothing below has been applied.**

```diff
+ ## ZC-04 — Zero-copy substrate #4: Supabase
+ Status: e2e-proven vs the live LOCAL Supabase stack, not reference-grade.
+ Claim:  A full pull of Supabase Postgres `public.intel_logs` (4,599 rows x 14
+         columns) through the pgsql driver and ArrowIpcSink produces an artifact
+         that meshroad serves over Arrow Flight with columns_decoded = 14
+         (= cols x blocks = 14 x 1) and copied_columns = 0 on the cold pass, and
+         columns_decoded = 0 at a 0.00% miss rate on the warm pass.
+ Evidence: bench/FINDINGS-supabase-profile.md §4; counters read from the server's
+         own Stats snapshot via get_flight_info app_metadata, not from the harness.
+ Fence:  Local stack only. No hosted Supabase connection was made.
+
+ ## RF-002/S — Null fidelity on production sigdet data
+ Status: proven end to end.
+ Claim:  SQL NULL survives Supabase -> pgsql driver -> ArrowIpcSink -> mmap ->
+         DataFusion -> Flight. count(*) vs count(col) parity is exact against psql
+         for all 9 nullable columns, with THREE columns actively discriminating
+         (section 942 NULLs, entry_date 523, embedding 8).
+ Evidence: tests/e2e/test_supabase_to_arrow.py; Flight parity table in
+         bench/FINDINGS-supabase-profile.md §4.
+ Note:   The test asserts >= 2 discriminating columns, so it cannot pass vacuously
+         against a pipeline that erases nulls.
+
+ ## PGV-01 — pgvector through the ingestion plane
+ Status: charted (first time).
+ Claim:  A Postgres `vector` column round-trips as a JSON-array string with its
+         NULLs preserved as true Arrow nulls, keeping "no embedding" distinct from
+         a zero vector. text[], numeric[] and jsonb likewise land losslessly;
+         numeric[] is carried as strings, so exact numerics do not pass through a
+         float64. No column needed projection exclusion.
+ Evidence: bench/FINDINGS-supabase-profile.md §3.
+ Fence:  Type information is carried in the VALUE, not the Arrow schema. A
+         consumer must know to parse. Not a typed-vector claim.
+
+ ## SBP-01 — Supabase connection profile
+ Status: config-layer behaviour proven; the failure it prevents is NOT observed.
+ Claim:  Transaction-mode pooling (port 6543) is refused at config time with a
+         named error, before any connection is attempted. Session-mode pooling
+         forces prepare_threshold=None and an operator cannot opt back in.
+         Non-loopback hosts get sslmode upgraded to require; sslmode=disable is
+         refused.
+ Evidence: tests/core/test_profile_supabase.py (16 tests).
+ Fence:  Proven as CONFIG-LAYER behaviour only. No hosted pooler was contacted,
+         so the prepared-statement failure it prevents was not reproduced.
+
+ ## DDB-01 — DynamoDB driver, local
+ Status: LOCAL-PROVEN ONLY. Has NOT earned "e2e-proven vs real AWS".
+ Claim:  11/11 integration proofs green against DynamoDB Local with real
+         row64tools 1.0.11, on a branch rebased onto the sink-era main; 363 unit
+         tests green; the full-refresh refusal and the null discriminator both
+         execute and pass.
+ Evidence: bench/LEDGER-phase1.md "Gate A results".
+ Fence:  Phase B did not run — no AWS credentials and no aws CLI. No real-AWS
+         behavioural-delta table exists. Do NOT quote real-AWS language.
```

**Deliberately NOT proposed:** any DynamoDB real-AWS claim, any hosted-Supabase
claim, and any performance claim. No performance work was done this campaign
(scope fence #1); the one throughput number recorded (L A-3) is a *correction of
a stale record*, not a benchmark.

---

## 5. Restore block — verified

| Item | Found state | Left state | |
|---|---|---|---|
| DynamoDB Local container | **none existed** | none — `ddb-test` created and removed | ✅ |
| Port 8010 | free | free | ✅ |
| AWS table `meshbench-perf` | n/a | **never created** — Phase B did not run | ✅ |
| `:8901` dev serve | n/a | started, proven, killed **PID-explicit** (1846094); pid file removed | ✅ |
| `meshroad-serve` | PID **1064** | PID **1064**, same start time | ✅ untouched |
| `meshroad-cockpit` | PID **1198** | PID **1198**, same start time | ✅ untouched |
| `:8802` / `:8803` | listening | listening | ✅ |
| Timers / CPU governor | — | **not touched, none authorized** | ✅ |

**No pattern-kill was used at any point.** Every process termination in this
campaign named an explicit PID read from a file I wrote.

### Two states deliberately NOT restored (disclosed)

1. **`.venv` gained `boto3`, `botocore`, `jmespath`, `s3transfer`.** Installed
   additively (no prune, so the ClickHouse deps survived). These are declared
   dependencies of the merged tree; removing them would break `main`'s own
   suite. `uv.lock` was regenerated to match, additively, with no version drift
   on any existing pin.
2. **`/home/kos/dev/meshroad/sources/serves.json` is modified and UNCOMMITTED.**
   The meshroad working tree was clean before this edit. D4 is closed — the dead
   `:8899` mapping is removed and `mappings` is now `[]`, which
   `gui/sources.py::_serves()` already handles. **I have no mandate to commit in
   the meshroad repo, so the commit is left to you.** `gui/` and `src/` were not
   touched; nothing serves `:8899`, and the referenced log was a stale 245-byte
   relic from Aug 11.

---

## 6. What Phase 2 inherits, and what it does not

**Ready:** `main` at `a7b1faf` with the sink split, the null contract, the
ClickHouse driver, and the all-null coercion fix. Two feature branches green and
gate-passed, awaiting their merges.

**Not ready — Phase 1 is NOT complete:**

- **Gate B never ran.** DynamoDB has no real-AWS proof and no behavioural-delta
  table. Needs `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` /
  `AWS_DEFAULT_REGION` exported **and** the `aws` CLI installed.
- **Gate C is held** behind Gate B, so `feat/dynamodb-driver` is unmerged and the
  Sources roster is at **three** live entries, not four.
- **The roster flip is additionally blocked** by the PG-010 finding above:
  DynamoDB cannot be expressed in a config file today.
- Push convention needs deciding: this repo has **one** remote
  (`github.com:kosmosis12/r64-db-engine`), no NAS and no partical mirror. Nothing
  has been pushed.

One correction worth carrying forward: **the ~35 items/s DynamoDB Local seed
ceiling is obsolete** (measured 13,158 items/s — `-inMemory` plus an 8-worker
parallel seed). Phase B's premise is narrower than written, though its
pagination, throttling, Decimal-dtype, scan-order and sparse-attribute questions
all remain fully valid.
