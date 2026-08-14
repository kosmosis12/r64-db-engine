# CC CAMPAIGN BRIEF — Phase 2: Arrow-Native Lane + DuckDB/MotherDuck (v1.1)
**Repo:** /home/kos/builds/r64-db-engine · **Issued:** 2026-08-14 (v1.1, amended post fix/pg-010-dialect-registry) · **Operator:** Kos
**Style of record:** CLOSEOUT-phase1.md — phased, gated, deviations disclosed at their gate, ratification is Kos's, commits held until gates green.

---

## 0. Context you are walking into

Baseline is `main` AFTER the merge of `fix/pg-010-dialect-registry` (commits b86daa9 D-4 sink · 17b059c PG-010 config · adac7f0 ledger addendum; 325 passed / 40 skipped, ruff clean, mypy clean). That merge already delivered what this brief's original Phase 0 asked for, and part of original Phase A:

**Already DONE — do not redo:**
- **PG-010 (extensibility axis):** `Config.dialect` is a free-form str resolved against the driver registry; dialect-named block passes opaquely to `Driver.connect()`; `extra="allow"` with an explicit validator enforcing allow-set = {declared fields} ∪ {registered dialects}; error listing reads `DRIVERS` lazily (grows with registration); 8 tests incl. fake-driver registration via monkeypatch and `test_error_lists_the_dialects_registered_right_now`. Residuals of record: typed `PostgresConfig`/`ClickHouseConfig` residue documented as `_TYPED_BLOCKS` (NOT a supported-dialect list); the dynamodb branch's `unused-config-vessel` workaround dies at ITS merge, not before; `dialect: dynamodb` is refused on main until that merge (deliberate, refuse-loudly).
- **D-4 (fully closed):** ArrowIpcSink writes explicit Arrow IPC via `pa.ipc` with **explicit 65536-rows/block chunking**, byte-identical to the former feather output (boundary test: 1/65536/65537/200000 rows → 1/1/2/4 blocks; Phase D sha e674c8e6 survives). The silent-regression trap (whole-table `new_file` → one block → per-block cache granularity collapses while tests stay green) is known, tested, and closed.
- **Accepted coupling, filed:** validating a config imports every registered driver's third-party deps. Watch condition: if anything lightweight (candidate: meshroad Sources tab) ever needs config validation without driver runtimes, the fix is lazy per-dialect import at resolution time. Trigger-gated ledger item; no action this campaign.

**Phase 1 recap for orientation:** `feat/dynamodb-driver` (b729299) remains rebased, local-proven, unmerged — its real-AWS Phase B is parked on Kos-side credentials and is NOT in scope here. Sources roster: three live (postgres, clickhouse, supabase-profile).

**Environment discipline (8/14 incident, non-negotiable):**
- `export PYTHONNOUSERSITE=1` at session top (the ~/.local python3.14 user-site is a standing contamination source).
- Suite is ALWAYS `.venv/bin/pytest`, never bare `pytest`, never PATH fallback.
- If uv rebuilds the venv, `uv sync --all-extras` before running anything. Project pins 3.13.12; system python is 3.14.

**Planes of record — DO NOT TOUCH:**
- meshroad :8802 (meshroad-serve.service) and :8803 (meshroad-cockpit.service). Prove untouched at close (Active + Main PID before/after).
- meshroad `src/` is FROZEN. The one permitted meshroad touch is `gui/` Sources-tab config for the roster entry.
- All process management PID-explicit. No pattern-kills. Campaign dev serve on **:8902**, PID to `~/bench-ch/serve-8902.pid`, torn down at close, no serves.json entry left (persist-only schema, per b8bbb9b).

**Scope fences:**
1. No AWS work (separate resumed session).
2. No ADBC, no Iceberg, no Delta (Phase 3).
3. Do not vectorize `_pre_coerce_values` — the pandas lane keeps its filed item; this campaign's answer is the bypass. Measure both lanes; optimize neither.
4. No streaming-source semantics. The lane streams batches within ONE full refresh. Full-refresh-only law stands; PG-011-class refusal required on the new lane.
5. Dependency additions follow the boto3 precedent: declared in pyproject, lock regenerated, no drift on existing pins, disclosed.

Work on branch **`feat/arrow-lane`** off the merged main.

---

## Phase A — The Arrow-native lane

Motivation of record (Gate C, CH campaign): pandas coercion ≈ **94% of pull wall** (18.26s/19.48s at 1M; 188.61s/198.92s at 10M); `query_df` double-materialization → **peak RSS ≈4.7× artifact** (6,709MiB for a 1,428.6MiB artifact at 10M). The lane bypasses both.

**Pre-decision (deviate only with disclosure):** extend the existing Driver ABC with an optional **capability**, not a sibling ABC. A driver advertises Arrow-nativeness (e.g. implements `query_arrow(...) -> pyarrow.RecordBatchReader`); the daemon routes: capability present → Arrow path, absent → existing `query_df` pandas path. One registry, one daemon, one config schema. If the codebase forces a fork anyway, stop and disclose before building it.

**Sink side:** ArrowIpcSink gains a streaming entry point consuming a `RecordBatchReader`, **extending the writer that b86daa9 landed** — same explicit-chunk discipline (65536), same artifact contract, incremental batch-by-batch writes (this is where the RSS bound comes from). Requirements:

1. **Artifact contract identical to the batch path.** Same atomicity discipline (temp/fsync/rename per the existing swap-safety tests), uncompressed, mmap-readable, `timestamp_unit` policy honored (opt-in us-cast, safe cast, lossy raises). meshroad must not be able to tell which entry point wrote the file. Re-chunk incoming reader batches to the 65536 discipline — do not let source batch size dictate block structure (a source handing 1M-row batches must still land 65536-row blocks, or the per-block cache granularity claim silently changes shape).
2. **THE DICTIONARY CONSTRAINT (known landmine, do not rediscover):** Arrow IPC file format permits one dictionary per field; per-batch dictionaries cannot be naively appended. For `dictionary_columns` the streaming path must produce a unified dictionary — preference order: (a) unify/re-encode against a growing dictionary before write, (b) collect-then-write for dict columns only, (c) refuse `dictionary_columns` on the streaming path with a named error + full-collect fallback for that pull. A test proves a two-batch pull with a dict column produces a valid, meshroad-servable artifact whose dictionary covers both batches.
3. **Null + NaN law:** true Arrow nulls survive end-to-end (RF-002 armed e2e in Phase C). The CH-ledger NaN-vs-NULL item is LIVE: DuckDB float columns can carry genuine NaN as a value. The lane must NOT convert NaN→null or null→NaN; one unit test pins a batch containing both, distinctly.
4. **Full-refresh law:** the lane refuses incremental/watermark config exactly as the df path does. Regression test.

**Gate A:** capability + streaming sink landed; unit tests green incl. dictionary two-batch, NaN/null distinctness, refusal, artifact-contract parity (schema + swap safety + block-structure/re-chunk test); suite green; daemon fallback routing proven by running an existing pandas-lane driver through unchanged `--integration` e2e (postgres or supabase set, whichever stack is up). Commit.

---

## Phase B — DuckDB driver (local first, MotherDuck conditional)

1. `uv add duckdb` (fence 5). Driver module registers dialect `duckdb` — **zero core edits.** This is the registry's live proof: if anything in `core/` needs touching to make `dialect: duckdb` valid, that is a PG-010 regression and a stop-and-disclose.
2. Driver implements the Arrow capability: `to_arrow_reader()` (non-deprecated API; `fetch_record_batch` is deprecated). Batch size: driver config with a sane default; record the choice and why in findings. (Sink re-chunks regardless — see A.1.)
3. **Config surface:** `database:` is the DuckDB path — local `.duckdb` file, `:memory:` (test-only), or `md:...`. `read_only=True` for local files — this driver ingests; it does not own the database.
4. **Determinism doctrine carries over:** DuckDB parallel scans do not guarantee order. Bench/e2e pulls use `ORDER BY row_id`; byte-reproducibility via sha256 on consecutive pulls (verify-by-checksum).
5. **Dataset: reuse meshbench, do not regenerate.** Export from the live CH container to Parquet, load into a local `.duckdb`:
   - `docker exec meshroad-ch clickhouse-client --query "SELECT * FROM meshbench.perf_1m ORDER BY row_id FORMAT Parquet" > /home/kos/bench-ch/perf_1m.parquet` (same for `perf_10m`)
   - DuckDB: `CREATE TABLE perf_1m AS SELECT * FROM read_parquet(...)`
   - Identical rows ⇒ `bench/GROUND-TRUTH-clickhouse.json` carries over VERBATIM (aggregate parity, score nulls 20,039 / 200,407, scaled_amount_sum integer authority). Verify the transfer before any e2e: row counts + integer-authority sum inside DuckDB vs the JSON. If CH's Parquet export mangles a type (Nullable, DateTime64), that is a finding — record, fix the load, never patch the ground truth.
6. **e2e, v0.2c pattern, six proofs nothing stubbed:** DuckDB → driver (Arrow path) → sink → artifact. Schema exact (14/14 — int64 / large_string / dict-status / double / timestamp[us]); aggregate parity 10/10; RF-002 armed (score null_count exact); PG-011 refusal; dictionary artifact valid; checksum reproducibility.
7. **MotherDuck — conditional, AWS-precedent gating:** requires `MOTHERDUCK_TOKEN` in ambient env (never echoed, never written). Present: upload the 1M table via `md:`, run the SAME e2e set, record wire observations. Absent: **skip, do not block** — park it, and every claim fences to "local DuckDB only."

**Gate B:** suite green, 6/6 e2e green under `--integration`, ground-truth transfer verified, checksums stable, MotherDuck proven-or-parked. Commit.

---

## Phase C — The probe: coercion bypass + RSS bound (acceptance gate for the whole lane)

**Bench doctrine of record applies in full:** quiet-baseline check once at series start + foreign-contention gate per invocation; governor → performance BEFORE the series (evidenced contaminant); root timers stopped (partical-burstbox-index, partical-status, supabase-orphan-check, market-intel-ingest); agent-hud + signalscorer containers stopped; Brave closed; restore block staged. **Kos does the root-quiesce steps — surface the checklist and WAIT.** n=10, min+median, >20% spread raises n.

**Cells, 1M and 10M, same pull each:**
- **P (pandas lane):** CH driver → `query_df` → coercion → sink. Re-run TODAY under this quiesce — the 8/10 numbers are corroboration, not the comparator.
- **N (native lane):** DuckDB driver → Arrow path → streaming sink.
- **P′ (attribution cell):** DuckDB → `.df()` → existing coercion → batch sink. Same source, both lanes: **N vs P′ is the clean bypass attribution; N vs P is the workflow number. Publish both, labeled.** If `.df()` chokes on a type the Arrow path handles, that itself is a finding.
- Decompose all cells: source-query / transform-or-coerce / sink-write, Gate-C instrumentation style. For N, "coerce" should be structurally ~0 — anything nonzero there gets named.
- **RSS:** peak RSS, all cells, both scales, same method as the 4.7× finding. Hypothesis: N's peak is batch-bounded, not 4.7×. Report the measured multiple.

**Gate C (acceptance):** decomposition tables in `bench/FINDINGS-arrow-lane.md`; coercion-share collapse and RSS bound stated with measured numbers and fences; honest losses recorded. **If the bypass does NOT collapse the coercion share, the finding IS the result — write it up and stop for steering. No rescue engineering.** Commit.

---

## Phase D — Serve proof + roster

1. Dev serve the DuckDB-sourced 1M artifact on **:8902** (PID file per convention). Cold: `columns_decoded` = cols×blocks for the touched projection, `copied_columns=0`; warm: 0 decoded, 0.00% miss; counters from the server's own Stats via `get_flight_info` app_metadata (D-3 workaround of record — `meshroad stats` is schema-bound, src/ frozen). count(*) + count(score) parity vs ground truth through DataFusion. That is **zero-copy substrate #5** and RF-002 through an Arrow-native-sourced chain.
2. Sources tab (meshroad `gui/`, config-only): `duckdb` roster entry, creds/token masked pre-serialization, provenance wired as the existing three. NO DynamoDB entry — waits on its merge.
3. Tear down :8902 PID-explicit; serves.json untouched.

**Gate D:** counters recorded, parity exact, roster at four live entries, planes untouched proven, teardown clean. r64-db-engine commit; meshroad gui commit is separate, in the meshroad repo — mandate GRANTED this campaign, push NAS + partical.

---

## Ledger, deviations, close-out

- **Deviations protocol:** unchanged. Disclose at the gate, rationale attached, ratification is Kos's.
- **Expected ledger items** (file, don't fix): dictionary-streaming resolution if (b)/(c) taken (upgrade path to (a)); MotherDuck wire observations if run; CH-Parquet type mangling if any; batch-size sensitivity if observed (one probe rep at a second batch size permitted as observation, not tuning); the lazy-import watch condition (carried from the PG-010 merge).
- **Claims-register candidates** (draft as diff, NOT applied, fenced): AL-01 coercion bypass with N-vs-P′ attribution; RSS-01 streaming memory bound with measured multiple; ZC-05 substrate #5 (DuckDB); RF-002/D null chain through the Arrow-native lane; PG-010-CLOSED already earned its narrow claim at merge — extend only if this campaign's zero-core-edit proof adds evidence.
- **Restore block, verified table:** timers restarted, governor→powersave, containers restarted, :8902 gone, :8802/:8803 same PIDs same start times, meshroad-ch left UP, Parquet exports kept in ~/bench-ch/ (transfer artifacts of record — sha256s in findings).
- **Push at close:** `feat/arrow-lane` published; merge to main is Kos's call — present the diff summary first. meshroad gui change committed + pushed NAS + partical.

**Success:** Arrow lane proven with measured bypass + RSS bound, DuckDB live as roster entry #4 having registered with zero core edits, MotherDuck proven-or-parked, claims diff drafted. Phase 3 (ADBC + Iceberg) starts from this main and must register two more dialects without touching core — the test the registry has now passed twice.
