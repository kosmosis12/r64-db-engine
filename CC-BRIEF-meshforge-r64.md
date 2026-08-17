# CC DROP-IN BRIEF — MESHFORGE factory engine, r64 lane (full implementation)
Repo: /home/kos/builds/r64-db-engine
Branch: create `feat/meshforge-factory` off current `main`. Do NOT touch `feat/arrow-lane` (Phase C bench mid-flight), `feat/dynamodb-driver`, or any serve on :8802 (plane of record — PID-explicit ops only, no pattern-kills, ever).
Companion doc: MESHFORGE-SKILL-INDEX.md (place a copy at repo root as `docs/MESHFORGE-SKILL-INDEX.md`; it is the naming/composition authority for everything you build here).

## Standing discipline (non-negotiable)
- Env: `export PYTHONNOUSERSITE=1`; suite ONLY via `.venv/bin/pytest`; after any venv rebuild run `uv sync --all-extras`. Never bare `pytest`.
- Commit-before-handoff; artifact-before-handoff (nothing exists until it's a committed file). One commit per phase minimum, descriptive bodies.
- Deviation-disclosure protocol: any departure from this brief is executed only if reversible, and disclosed in a numbered list at close-out for ratification. Irreversible or contract-touching departures: STOP and ask.
- Do not modify `core/` except where a phase explicitly authorizes it. The load-bearing property of this whole build is that adding an integration touches no shared code — preserve it in the code you write, and prove it the way Gate B did (`grep -r <dialect> core/` empty).
- Credentials law: no secret is ever read into your context or echoed. Secrets live in 0600 files referenced by path. This session is designed to need ZERO credentials (targets: local ClickHouse, no-auth public API).
- No timing/benchmark claims this session. Bench doctrine requires root-quiesce Kos hasn't staged; anything perf-flavored is an untimed observation, labeled as such.

## Phase 0 — Topology audit (GATED: report and wait for Kos ack before writing code)
1. Record: `git -C /home/kos/builds/r64-db-engine branch -a --contains`, current main SHA, dirty state, remotes.
2. Verify present on main (read, don't assume): Driver ABC + registry-derived dialect resolution (PG-010 — config `dialect` resolved against driver registry, unknown dialect refused loudly listing registered dialects); ArrowIpcSink explicit `pa.ipc` writer with 65536-row block discipline; PG-011 watermark refusal; RF-002 null-fidelity machinery; `bench/GROUND-TRUTH-clickhouse.json`; `bench/make-dataset.sh`.
3. Verify docker container `meshroad-ch` exists (start by explicit name if Exited; leave UP at close per standing restore convention). Confirm `meshbench.perf_1m` present with expected row count 1,000,000. If the bridge-allow XML regressed (container recreated since 8/14), recover per the documented procedure: `docker cp` the committed `bench/ch-allow-docker-bridge.xml` into the container's **`users.d/`** as **`zz-allow-docker-bridge.xml`**, then `SYSTEM RELOAD CONFIG` — the failure mode presents as "password incorrect" from the host.

   > **Corrected 2026-08-16 (Phase 0 audit).** This step originally said `config.d`. The file is a `<users>` document that widens the `default` user's allowed source range, so it must land in `users.d/`, and it must sort AFTER the stock `default-user.xml` that restricts `default` to `::1`/`127.0.0.1` — `users.d` is applied in lexical order, which is why the `zz-` prefix is part of the filename and not decoration. The exact commands are in the header comment of `bench/ch-allow-docker-bridge.xml`. No recovery was needed this session: the container dates from 2026-08-10 and was never recreated, so the file was still in place.
4. Report findings + any drift from this brief's premises. WAIT for ack.

## Phase 1 — `factory/conformance.py`: the oracle (Gate F1)
New top-level package `factory/` (NOT under core/). Build the parameterized acceptance battery, generalizing the DuckDB Gate B / CH Gate C battery you can read in the repo's closeout and findings docs.

CLI contract:
```
.venv/bin/python -m factory.conformance \
  --dialect clickhouse \
  --config factory/targets/clickhouse-meshbench.yaml \
  --ground-truth bench/GROUND-TRUTH-clickhouse.json \
  --table perf_1m --evidence-dir factory/evidence
```
`factory/targets/<name>.yaml` = a normal engine Config document (dialect block + table). Create the clickhouse-meshbench target as part of this phase.

Battery (each check = named result with PASS/FAIL/SKIPPED-with-reason; overall exit code 0 only if all non-skipped pass):
1. **Registry admission** — dialect resolves via the driver registry; a mutated unregistered dialect refuses loudly (assert the error lists registered dialects).
2. **Schema exactness** — pulled artifact schema vs a spec file `factory/specs/meshbench-schema.json` (derive it in this phase from the ground-truth-era artifact: 14 columns; int64 / large_string / dictionary status / double / timestamp[us]). String-width tolerance per the B-3 cross-lane fence (large_string vs utf8 equivalent).
3. **Aggregate parity** — all ground-truth aggregates for the table; `scaled_amount` authority = SUM(CAST(ROUND(amount*100) AS BIGINT)) exact-int form GATES, float form corroborates only.
4. **RF-002 discriminator** — count(col) vs count(*) on nullable columns; assert ≥1 column actively discriminates on meshbench (score, 20,039 nulls at 1M) and that null_count matches ground truth exactly.
5. **B-2 boundary** — min/max on event_time vs source bounds (query the source live for its own min/max; assert artifact matches). This is the transfer doctrine: aggregate parity is blind to uniform shifts.
6. **PG-011 refusal** — mode:incremental (or watermark config) on the full-refresh sink is refused loudly.
7. **Block structure** — artifact Arrow IPC block sizes obey the 65536 discipline (1/1/2/4-blocks-style assertion scaled to row count).
8. **Checksum (lane-scoped)** — two consecutive pulls byte-identical within the same lane; on mismatch, name the residual nondeterminism instead of failing silently. Cross-lane comparisons use the data+schema-minus-metadata+block-structure equivalence, never bytes.
9. **Zero-copy serve gate (optional, `--serve-gate`)** — spin `/usr/local/bin/meshroad serve` on the artifact at 127.0.0.1:8903 (ephemeral, PID recorded to a pidfile, killed PID-explicit in a finally block even on failure), pull counters via get_flight_info app_metadata: assert copied_columns=0 cold and warm, warm miss_rate 0%.

Evidence pack output (Law 2 — this is the review artifact):
- `factory/evidence/EVIDENCE-<dialect>-<YYYYMMDD>.json` — machine form: every check, the actual values compared (both sides), source queries issued, artifact sha256, row counts, environment (python, package versions, container image tag).
- Sibling `.md` — human form, table-per-check, verdict line at top. A reviewer must be able to ratify the driver from this file without reading the diff.

Tests: unit-test the battery itself (each check has a deliberately-broken fixture proving it can fail — an oracle that can't fail is not an oracle). Integration marker consistent with existing `--integration` gating.

**Gate F1 acceptance:** battery green end-to-end against clickhouse/meshbench.perf_1m with `--serve-gate`; evidence pack committed; suite green via `.venv/bin/pytest`; `grep -r clickhouse factory/` is fine but `grep -ri factory core/` empty (core untouched). Note in close-out: duckdb conformance run is DEFERRED until feat/arrow-lane merges (driver not on main) — do not cherry-pick it.

## Phase 2 — Skills authoring (Gate F2)
Write the skill files per the index document. Structure: `<skilldir>/SKILL.md` with frontmatter name/description in the style of the repo's existing skill conventions; descriptions must be trigger-rich.
1. `~/.claude/skills/meshforge/SKILL.md` — core doctrine: the Four Laws verbatim, two-tier model, evidence-pack standard, credential law, cross-agent QA gate (mandatory between conformance-green and merge; builder ≠ auditor), deviation-disclosure protocol, refuse-loudly doctrine, commit/artifact-before-handoff.
2. `.claude/skills/r64-connector-factory/SKILL.md` — the campaign template: Phase 0 audit (gated) → research → DRIVER-PLAN.md → Gates A–E, mapping Gate C to `factory/conformance.py`. Embed the `DRIVER-PLAN.md` template inline with the fixed table and its named trap rows: auth model; pagination; rate limits; type map covering int32/int64 ceiling (RF-001 class), Decimal handling, timestamp + session-timezone defaults (B-2 class — call out that Snowflake defaults to America/Los_Angeles), null semantics vs NaN (RF-002 class), scan-order determinism (ORDER BY decision + verify-by-checksum); sandbox strategy; teardown plan. Plan is ratified by Kos BEFORE code.
3. `.claude/skills/r64-conformance/SKILL.md` — thin: when to run, CLI contract, how to read an evidence pack, how to extend the battery (Law 4: if the battery can't check it, extend the battery first).
4. `.claude/skills/r64-recipe-engine/SKILL.md` — authored now, implemented Phase 3; document the recipe/recipe-book schema from Phase 3 as the contract.
5. `.claude/skills/r64-factory-maintenance/SKILL.md` — drift response: conformance re-run → on failure, generate `REPAIR-BRIEF-<dialect>-<YYYYMMDD>.md` from an embedded template (symptom, evidence-pack diff vs last green, re-research directive, re-admission requirement).

**Gate F2 acceptance:** all five SKILL.md files exist, committed (user-global one noted in close-out with its path since it's outside the repo); a cold read of r64-connector-factory alone is sufficient to run a driver campaign without this brief.

## Phase 3 — Recipe engine: the `rest` dialect (Gate F3)
Implement `factory/rest_driver.py` (or drivers/ location matching existing driver layout) registering dialect `rest` — zero core edits, PG-010 pattern exactly.

Recipe-book config schema (the dialect block):
- `recipes[]`: name; method; **url pinned at creation** (runtime inputs may only populate declared body/query parameters — the URL template admits no host/path substitution); auth: `{type: none | header | query, env_file: <path>, key_name: <name>}` where env_file is a 0600 file the ENGINE reads at call time (never logged, never in config values); `params_schema` (declared inputs); `response_schema` (jsonschema, the per-pull validator); `pagination: {type: none | cursor | page | link-header, ...}`; `extract` (JSONPath/dotted path to the record array).
- `threading[]`: ordered recipe names + output→input bindings.
- `output`: column mapping → Arrow types (int64-native; timestamps normalized to UTC — B-2 applies to APIs too).

Engine invariants (enforced in code, each with a test):
- HTTPS-only; hostname must exactly match or be a proper subdomain of the recipe's recorded allowlist host (evil-checkr.com ≠ checkr.com — test this literally); resolved address rejected if private/loopback/link-local address space.
- Deterministic execution: no model anywhere in the pull path (Law 1). The recipe book is data; the engine is the only code.
- Per-pull `response_schema` validation. On failure: structured repair event (JSON line to `factory/evidence/drift/<source>-<ts>.json`) + ntfy via the fleet's existing `ntfy-fail@` conventions + non-zero exit. No auto-retry-with-reinterpretation.
- Response size and time caps, configurable, defaulted sanely.

End-to-end proof (no credentials by design): author `factory/recipes/open-meteo.yaml` pulling hourly temperature for a fixed lat/lon window from api.open-meteo.com (auth: none), threading two recipes if the API shape allows (e.g., geocoding → forecast) or one recipe with pagination-none if not — disclose which. Land it through the real sink to a real artifact; run a reduced conformance battery: registry admission, schema exactness vs a spec you write for it, RF-002 mechanics (nullable column preserved if present, else SKIPPED-with-reason), B-2 UTC boundary, block structure, checksum, refusal of an https→http mutated recipe and of a hostname-mutated recipe. Evidence pack emitted same as Phase 1.

**Gate F3 acceptance:** `grep -ri rest core/` empty (PG-010 proof repeated); recipe e2e green with evidence pack; all security-invariant tests present and failing-fixture-proven; suite green.

## Phase 4 — Maintenance loop (Gate F4)
1. `factory/bin/factory-conformance-sweep` (executable, repo-tracked): iterates `factory/targets/*.yaml`, runs the battery per target with `--evidence-dir`, aggregates verdicts, exit non-zero on any failure, and on failure writes the repair brief via the template from the r64-factory-maintenance skill.
2. Systemd units (files under `factory/systemd/`, install commands in the close-out for Kos to run with sudo — you do not sudo): `r64-factory-conformance.service` (oneshot, User=kos, WorkingDirectory=repo, ExecStart=the sweep via .venv python, `OnFailure=ntfy-fail@%n.service`) + `r64-factory-conformance.timer` (weekly, Persistent=true, RandomizedDelaySec=300). Timer must NOT fire during bench windows — note in the unit comment that Kos's root-quiesce checklist stops it, and add it to that checklist in the close-out.
3. Recipe drift path: the sweep also validates each recipe book's last-pull drift log and surfaces any accumulated repair events.

**Gate F4 acceptance:** sweep runs green manually end-to-end (clickhouse target + open-meteo target); a deliberately-poisoned target (mutated ground-truth copy in a tmp dir, never the real file) produces a repair brief with the correct evidence diff; unit files pass `systemd-analyze verify`.

## Close-out (required artifacts)
- `CLOSEOUT-meshforge-r64.md` at repo root: per-gate results, deviations numbered for ratification, install commands for the systemd units (copy-pasteable, no placeholders), the deferred items ledger — at minimum: duckdb conformance post-arrow-lane-merge; Snowflake as first factory-admitted driver (Phase 3 opener — DRIVER-PLAN.md to be generated by the new skill, session-TZ trap pre-named); Sources-tab intake (meshroad repo, separate session); recipe→driver promotion path exercised once; Partical lane after STR-03.
- Claims-register candidates drafted FENCED (Kos ratification pending), suggested IDs: MF-01 oracle-with-failing-fixtures (battery proven able to fail), MF-02 zero-core-edit rest dialect (registry pattern proven on a non-DB source class), MF-03 destination-pinning security invariants (test-proven). No external travel until ratified.
- Push branch to origin. Do NOT merge to main — merge happens after cross-agent QA (Codex audit of factory/ + skills), per Law 2 and the QA gate. State this explicitly in the close-out.

Begin with Phase 0 and stop for ack.
