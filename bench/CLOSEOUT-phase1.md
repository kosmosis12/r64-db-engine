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

---

## 7. Addendum — 2026-08-14

Sections 1–6 are left as written on 2026-08-13. This section records work done
the following day and **supersedes specific rows above**; it does not rewrite
them.

### 7.1 Two premises in §6 were stale by the time they were read

| §6 claim | Actual state on 2026-08-14 | |
|---|---|---|
| "no AWS credentials **and** the `aws` CLI are both absent" | `aws` CLI **is installed** at `/usr/bin/aws`. Credentials still absent: no `~/.aws`, no `AWS_*` in env | ⚠️ half-corrected |
| "Nothing has been pushed" | `main`, `feat/dynamodb-driver` and `feat/supabase-profile` all **match `origin`**. Push convention is settled: one remote, already used | ✅ closed |

An `aws configure` was attempted and abandoned at the first prompt, so **Gate B
remains blocked, on credentials alone** — one of its two blockers is gone.

### 7.2 D-4 closed — feather → explicit Arrow IPC

`pyarrow.feather.write_feather` (deprecated at pyarrow 24.0.0) is replaced by
`pa.ipc.new_file()` with an explicit `max_chunksize=_BLOCK_ROWS` (65536).

The naive swap was a trap, and the sink's own docstring said so: feather's
*default* chunksize is what produced multi-block files, and `pa.ipc.new_file()`
writing a table in one shot emits **one** block. That would have collapsed the
consumer's per-block column-cache granularity — and collapsed it *silently*,
since a one-block file reads back identical row-for-row. The ZC-04 warm-pass
`columns_decoded = 0` result would have degraded into a whole-file decode with
every test still green.

So the block size is now stated rather than inherited, and it was verified
rather than assumed:

- feather's default chunking measured empirically at **65536 rows/block**
  (200k rows → 4 blocks of 65536/65536/65536/3392).
- The new writer's output is **byte-identical to `write_feather`'s** across a
  200k-row multi-block table, an exactly-65536-row boundary table, and the
  empty, null-bearing, dictionary-encoded and timestamp cases. **The Phase D
  artifact sha256 `e674c8e6…` therefore survives this change unchanged.**
- Pinned by `test_block_granularity_is_preserved_across_the_64k_boundary`
  (1 / 65536 / 65537 / 200000 → 1 / 1 / 2 / 4 blocks) and
  `test_empty_table_writes_a_zero_block_file`.

The 14 `FutureWarning`s are gone from the suite. Test-side `feather.read_table`
callers moved to the `ipc.open_file(pa.memory_map(...))` idiom the ClickHouse
and Supabase e2e files already used.

### 7.3 PG-010 closed on the extensibility axis — dialect is no longer a `Literal`

`Config.dialect` was `Literal["postgres", "clickhouse"]`. It is now a free-form
`str` resolved against the driver registry, and the block named after the
dialect is passed to `Driver.connect()` opaquely — the same shape `sink.type`
and `profile` already used. This is the registry pattern the repo had **already
ratified twice**; the dialect axis was the one place still enumerating.

`extra="forbid"` on `Config` had to become `extra="allow"` to admit an unknown
dialect's block, so `_reject_unknown_top_level_keys` restores the typo
protection. The permitted set is exactly:

    {declared config fields} u {registered dialect names}

Pydantic enforces the first half; the validator enforces the second, reading
the registry rather than any constant in `core/config.py`. Both halves are
pinned: a misspelled `telemtry:` is refused, and so is a dialect-shaped block
whose driver is **not registered** — the latter with the currently-registered
names in the message:

```
unknown dialect 'dynamodb' (registered: clickhouse, postgres)
unknown top-level config key(s): telemtry. Permitted keys are the declared
config fields plus a block named after a registered dialect
(registered: clickhouse, postgres).
```

An unregistered dialect is therefore refused at **config time**, not later at
driver resolution — PG-011 doctrine, refuse loudly and early. On `main` this
means a `dialect: dynamodb` config is refused until Gate C registers the
driver, which is the honest answer rather than a config that validates against
a driver that is not there.

**Downstream premise, still true.** `~/dev/meshroad/sources/serves.json`
documents that it keeps GUI/cockpit state out of the engine YAML because
"r64-db-engine's Config forbids extra top-level keys". That premise is now
**validator-enforced rather than pydantic-enforced — still true in effect**:
foreign top-level state is refused, since it is not a registered dialect name.
Pinned by `test_foreign_top_level_state_is_still_refused`.

**Cost accepted.** Validating a config now imports the driver registry, so it
pulls in every registered driver's third-party dependencies (psycopg,
clickhouse_connect, and boto3 after Gate C). `core.config` remains importable
standalone; only validation carries the coupling. Ratified as the price of
checking dialect names against the registry instead of against a constant — a
constant would have been PG-010 again.

Proven against the **real** driver, not a mock: in a throwaway worktree of
`feat/dynamodb-driver` carrying only the new `core/config.py`,

```
registry on this branch: ['clickhouse', 'dynamodb', 'postgres']
dialect      : dynamodb
driver_config: {'region': 'us-east-1', 'endpoint_url': 'http://127.0.0.1:8010',
                'scan_segments': 4, 'consistent_read': True}
resolved     : DynamoDBDriver
no vessel key: True
driver refuses its own bad key: dynamodb.scan_segments must be between 1 and 32
```

The first line is the load-bearing one: the driver's presence in the registry is
the *entire* difference between this config being refused on `main` and accepted
here. No `core/` edit separates the two.

**`"database": "unused-config-vessel"` is no longer needed.** The driver still
validates its own keys, which is the point: core gained a capability without
gaining knowledge of DynamoDB. `core/` imports no concrete driver at module
scope.

**What this does NOT close.** PG-010's other half stands: core still carries
typed `PostgresConfig`/`ClickHouseConfig` models and Postgres-specific health
and metrics. The claim earned here is narrow and exact — *adding a driver no
longer requires editing core validation*. `_TYPED_BLOCKS` is documented as
residue, not as a supported-dialect list.

Suite: **325 passed, 40 skipped** (was 310), `ruff check` clean, `mypy` clean.

### 7.4 The Sources-roster blocker is now Gate C alone

§3 listed **two** blockers on the Phase C roster flip: Gate B, and DynamoDB
being unconfigurable. The second is closed. The roster is still at three live
entries, because `feat/dynamodb-driver` is still unmerged behind Gate B.

### 7.5 Still open, unchanged

- **Gate B** — needs `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` /
  `AWS_DEFAULT_REGION`. Operator action; not automatable from here.
- **Gate C** — held behind B.
- **D-2** (type carried in the value, not the Arrow schema), **D-3**
  (`meshroad stats` schema-bound), **D-5** — filed, untouched.
- **`~/dev/meshroad/sources/serves.json`** — still modified and uncommitted in
  the meshroad repo. No mandate to commit there; unchanged by this session.
- Claims register §4 — still a **proposed diff**, still unapplied, still
  Kos-ratified only. Nothing in this addendum was written into it.

### 7.6 Environment note

`uv sync --all-groups --all-extras` on `main` **pruned `boto3`** (declared only
on `feat/dynamodb-driver`), partially reversing disclosed deviation #2. The
DynamoDB verification above therefore ran in an ephemeral `uv run --no-project`
environment; **`.venv` was not modified in this session.**
