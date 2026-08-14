# FINDINGS — Supabase profile (Phase D)

**Campaign:** CC Phase 1 · **Date:** 2026-08-13 · **Machine:** cachyPC
**Status:** **e2e-proven vs the live local Supabase stack, not reference-grade.**
Hosted paths are **documented, not tested** — every untested claim is labelled as such below.

---

## 1. What was built

Supabase is Postgres, so the reference-grade `postgres` driver already speaks it.
The deliverable is therefore **not a driver** — it is a named connection profile
plus the doctrine for connecting to a hosted Supabase safely.

| Piece | Location |
|---|---|
| Profile ABC + `ProfileError` | `src/r64_db_engine/core/profile.py` |
| Profile registry | `src/r64_db_engine/profiles/__init__.py` |
| Supabase profile | `src/r64_db_engine/profiles/supabase.py` |
| Refusal + normalization tests (16) | `tests/core/test_profile_supabase.py` |
| Live e2e (13) | `tests/e2e/test_supabase_to_arrow.py` |

`profile:` is a **free-form string** resolved against a registry, not a
`Literal[...]`. Enumerating profile names in `core/config.py` would clone the
PG-010 dialect leak onto a third axis; `core/` imports no concrete profile.

### Scope deviation, ratified in-session

The brief expected **zero driver-code changes**. One was unavoidable and was
surfaced before any code was written.

`prepare_threshold` is a **psycopg `connect()` kwarg, not a libpq connection
option** — `conninfo_to_dict("... prepare_threshold=0")` raises
`ProgrammingError: invalid connection option "prepare_threshold"`. The driver
builds a conninfo *string*, so the value cannot be threaded through the existing
config path. `PostgresDriver._open()` now passes it explicitly.

The default is **5**, psycopg's own default, so a config that does not set it
behaves exactly as before. (An earlier sketch of this change used `0`, which
would have silently disabled prepared statements for every existing Postgres
pull — caught before it was written.)

---

## 2. Hosted-connection doctrine

> **Everything in this section except the Local row is UNTESTED.** It is derived
> from Supabase's documented pooler behaviour and from what this driver
> demonstrably requires, not from a hosted connection made during this campaign.

| Shape | Host / port | Status | Profile behaviour |
|---|---|---|---|
| **Local dev** | `127.0.0.1:54322` | **TESTED** | No-op. Loopback means no pooler and no wire to protect. |
| **Hosted direct** | `db.<ref>.supabase.co:5432` | *untested* | `sslmode` upgraded to `require`; prepared statements left on. |
| **Hosted pooled, session** | `<ref>.pooler.supabase.com:5432` | *untested* | Allowed. **`prepare_threshold` forced to `None`.** |
| **Hosted pooled, transaction** | `<ref>.pooler.supabase.com:6543` | *untested* | **REFUSED** with a named error. |

### Why transaction mode is refused rather than supported

Transaction-mode pooling multiplexes many clients onto few server connections
and returns the connection to the pool after **every transaction**. Server-side
prepared statements are per-session state, so a `PREPARE` issued in one
transaction is simply absent in the next. psycopg prepares automatically after
5 executions of the same query, so a pull does not fail at connect time — it
**fails partway through** with `prepared statement "_pg3_0" does not exist`.

That is the exact failure shape this profile exists to prevent, so it is refused
by name at config time, with an error that says what to use instead. PG-011
doctrine: refuse loudly, never degrade silently.

The refusal keys on the **port**, not on port-and-hostname, so a custom DNS name
in front of the pooler cannot smuggle 6543 past the gate. Pinned by
`test_transaction_mode_port_refused_even_behind_a_custom_hostname`.

### Why session mode still forces `prepare_threshold=None`

Session mode holds a server connection for the client's whole session, so
prepared statements survive in the normal case. But the pooler remains in the
path and a session can be recycled underneath a long-lived client. The profile
trades a little query-planning time for a guarantee. An operator **cannot** opt
back in: an explicit `prepare_threshold` is overridden when a pooler host is
detected (`test_pooler_overrides_an_explicit_prepare_threshold`).

### IPv6 note (untested)

Hosted direct connections are **IPv6-only by default**; IPv4 requires the pooler
or Supabase's paid IPv4 add-on. The profile does **not** attempt to detect or
enforce this — it is a network-reachability property, not a config property, and
a failure here surfaces immediately as a connection error rather than as silent
corruption. Documented so it is not rediscovered at 2am.

### Transport

Any non-loopback host gets `sslmode` upgraded from `prefer`/`allow` to
`require`, because `prefer` silently falls back to plaintext if the server
declines TLS — it reads as encrypted without being guaranteed to be. An explicit
`sslmode=disable` on a non-loopback host is **refused**, not upgraded: that is an
operator asserting something specific and wrong.

A private LAN address is treated as **remote**, not local. The question the
check answers is "can this be observed on the wire", not "is this host nearby".

---

## 3. Live e2e — what was actually proven

**Target:** `public.intel_logs` on the running `supabase_db_agent-hud` stack
(54322). Real sigdet production data, **4,599 rows × 14 columns**.

> **Deviation, ratified:** the brief named `company_signals`. That table exists in
> **none** of the three running Supabase stacks (agent-hud/54322,
> Canvas-Sales-OS/54622, halo-inv/54422). `intel_logs` was selected as the
> substitute: it is 14 columns — matching the brief's own "14-column-style"
> phrasing — and carries a richer uncharted-type surface than the named target.

Connection derived from the running stack via `supabase status -o env` in
`~/agent-hud`, not hardcoded.

### Schema map and RF-002 parity

Ground truth read **independently over psycopg**, never recomputed from the
pipeline's own output.

| # | column | pg type | landed dtype | psql `count(col)` | pulled non-null | |
|---|---|---|---|---|---|---|
| 1 | `id` | `int8` | `Int64` | 4599 | 4599 | ✓ |
| 2 | `content` | `text` | `string` | 4599 | 4599 | ✓ |
| 3 | `chunk_index` | `int4` | `Int64` | 4599 | 4599 | ✓ |
| 4 | `embedding` | **`vector`** | `string` | **4591** | **4591** | ✓ |
| 5 | `source_doc_id` | `text` | `string` | 4599 | 4599 | ✓ |
| 6 | `source_doc_title` | `text` | `string` | 4599 | 4599 | ✓ |
| 7 | `entry_date` | `date` | `datetime64[ns]` | **4076** | **4076** | ✓ |
| 8 | `section` | `text` | `string` | **3657** | **3657** | ✓ |
| 9 | `companies` | **`text[]`** | `string` | 4599 | 4599 | ✓ |
| 10 | `sectors` | **`text[]`** | `string` | 4599 | 4599 | ✓ |
| 11 | `deal_sizes` | **`numeric[]`** | `string` | 4599 | 4599 | ✓ |
| 12 | `metadata` | **`jsonb`** | `string` | 4599 | 4599 | ✓ |
| 13 | `created_at` | `timestamptz` | `datetime64[ns]` | 4599 | 4599 | ✓ |
| 14 | `updated_at` | `timestamptz` | `datetime64[ns]` | 4599 | 4599 | ✓ |

`count(*) = 4599`. **Three columns discriminate** — `section` (942 NULLs),
`entry_date` (523), `embedding` (8). RF-002 is therefore not a vacuous pass:
a pipeline that filled NULL with `""` or `0` would show `count(col) == count(*)`
on all three and fail.

### The previously uncharted types — all four landed, none excluded

The brief anticipated failure here and pre-authorised excluding the vector
column via projection. **That was not needed.**

- **`vector` (pgvector)** → JSON-array string, e.g. `[0.5341113,2.2912762,…]`.
  Lossless for the values, and its 8 NULLs stay distinct from a zero vector —
  which matters, because a zeroed embedding is a *valid* vector and would be
  indistinguishable from "never computed" if nulls were filled.
- **`text[]`** → JSON array of strings.
- **`numeric[]`** → JSON array of **strings**, e.g. `["7000000000.0"]`. Postgres
  `numeric` is exact; rendering it as a JSON *number* would hand it to a float64
  and lose the guarantee. Carrying it as a string preserves precision.
- **`jsonb`** → compact JSON string.

Ledger note: all four land as `string`, so the **type information is carried in
the value, not the Arrow schema**. A consumer must know to parse them. That is a
representability observation, not a defect — the same class as the `.ramdb`
representability finding from the ClickHouse campaign.

### Artifact reproducibility

Two consecutive full pulls, byte-identical:

```
sha256  e674c8e6bdaba9eadfcca65c04fc4ddc3b24f5b1cd770ef9f27281c6f84b9503
bytes   48,743,402
```

No residual nondeterminism to name. Notably this holds **without** the
`ORDER BY row_id` the ClickHouse campaign needed — Postgres returned a stable
scan order for this unmodified table. That is an observation about this table at
this moment, **not a guarantee**: the CH `ORDER BY` decision remains the
precedent if it ever stops holding.

### Refusals re-confirmed under the profile

- **PG-011 / full-refresh-only**: incremental against `ArrowIpcSink` is refused
  end to end with the profile in the path.
- **Transaction pooler**: refused at **config time**, before any connection is
  attempted — not partway through a pull.

---

## 4. Dogfood serve proof — zero-copy substrate #4

Artifact served over Arrow Flight on a fresh port, PID-explicit throughout.

```
meshroad serve --file /home/kos/bench-ch/supabase/IntelLogs.arrow \
               --table intel_logs --addr 127.0.0.1:8901 --refresh-ms 1000
```

PID captured to `~/bench-ch/serve-8901.pid`. **`:8802` and `:8803` untouched.**

Counters read from the server's own `Stats` snapshot, which meshroad attaches as
`app_metadata` on `get_flight_info` — the server's instrumentation, not the
harness's. Deltas computed across three snapshots (baseline → cold → warm).

Artifact geometry: **14 columns × 1 record batch → cols × blocks = 14**.

| counter | COLD | WARM | expected |
|---|---|---|---|
| `columns_decoded` | **14** | **0** | 14 cold, 0 warm ✓ |
| `copied_columns` | **0** | **0** | 0 ✓ |
| `zero_copy_columns` | 14 | 0 | — |
| `cache_hits` | 0 | 14 | — |
| `cache_misses` | 14 | 0 | — |
| `blocks_assembled` | 1 | 1 | — |
| **miss rate (this pass)** | 100.00% | **0.00%** | 0.00% warm ✓ |

Rows streamed: 4,599 cold and warm.

**All four gate conditions pass.** Every column was served zero-copy
(`copied_columns = 0` on the cold pass, `zero_copy_columns = 14`), and the warm
pass decoded nothing.

### Count parity over Flight vs psql

| check | Flight | psql | |
|---|---|---|---|
| `count(*)` | 4599 | 4599 | ✓ |
| `count(section)` | 3657 | 3657 | ✓ |
| `count(entry_date)` | 4076 | 4076 | ✓ |
| `count(embedding)` | 4591 | 4591 | ✓ |
| `count(metadata)` | 4599 | 4599 | ✓ |
| `count(DISTINCT source_doc_id)` | 271 | 271 | ✓ |
| `max(id)` | 148139 | 148139 | ✓ |
| `max(chunk_index)` | 1656 | 1656 | ✓ |

**RF-002 survives the whole chain: Supabase → pgsql driver → ArrowIpcSink →
mmap → DataFusion → Flight.**

---

## 5. Ledger items raised in Phase D

| # | Item | Disposition |
|---|---|---|
| D-1 | `prepare_threshold` needs a driver kwarg; not expressible in conninfo | **Fixed**, ratified in-session |
| D-2 | `vector` / `text[]` / `numeric[]` / `jsonb` all land as `string` — type carried in the value, not the Arrow schema | **Filed.** Representability observation |
| D-3 | `meshroad stats` is schema-bound to the meshbench `perf` table (hardcoded `amount`), so it cannot profile an arbitrary artifact | **Filed.** meshroad `src/` is frozen; counters obtained via `get_flight_info` app_metadata instead |
| D-4 | `pyarrow.feather.write_feather` deprecated as of pyarrow 24.0.0; `ArrowIpcSink` emits a `FutureWarning` on every write | **Filed.** Not touched — out of scope |
| D-5 | `company_signals` does not exist in any running stack | Substituted `intel_logs`, ratified |
| D-6 | Postgres scan order was stable without `ORDER BY`; artifact is byte-reproducible | Observation only — **not** promoted to a guarantee |

---

## 6. Status language

**Supabase profile: e2e-proven vs the live local stack, not reference-grade.**

Proven: local direct connection, full pull, all 14 columns including four
previously uncharted types, RF-002 parity end to end, byte-reproducible artifact,
zero-copy serve with cold/warm counters, both refusals.

Not proven: **every hosted path.** No hosted Supabase connection was made. The
transaction-mode refusal is proven as *config-layer behaviour* — that the profile
refuses the configuration — **not** as an observation of the pooler failure it
prevents.
