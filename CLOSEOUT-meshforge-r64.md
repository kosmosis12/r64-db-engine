# CLOSEOUT — MESHFORGE factory engine, r64 lane

Branch: `feat/meshforge-factory` (off `main` @ `7820d18`)
Session: 2026-08-16 → 2026-08-17 · Brief: `CC-BRIEF-meshforge-r64.md` · Index: `docs/MESHFORGE-SKILL-INDEX.md`

**Status: all four gates met. Codex rounds 1 (§9) and 2 (§10) remediated.
Round 2: Q2 CLEAN, Q1/Q3/Q4 BLOCK — all addressed. Re-pushed for round 3.
NOT merged: merge remains blocked on the audit verdict per Law 2.**

---

## 1. Gate results

| Gate | Requirement | Result |
|---|---|---|
| **F1** | Battery green end-to-end vs clickhouse/meshbench.perf_1m with `--serve-gate`; evidence pack committed; suite green; core untouched | **MET** — 9 PASS / 0 FAIL / 1 SKIPPED, exit 0 |
| **F2** | Five SKILL.md files, committed; cold read of `r64-connector-factory` sufficient to run a campaign | **MET** — 4 in-repo + 1 user-global |
| **F3** | `grep -rniE "\brest\b" core/` empty; recipe e2e green with evidence pack; security invariants failing-fixture-proven; suite green | **MET** — 9 PASS / 0 FAIL / 1 SKIPPED, exit 0 |
| **F4** | Sweep green end-to-end on both targets; poisoned target produces a repair brief with the correct diff; units pass `systemd-analyze verify` | **MET** |

### Zero-core-edit proof (the load-bearing property)

Both grep assertions, in the forms ratified at Phase 0, return **0** on this branch:

```
git grep -rnE "(^|[^_])[Ff]actory" src/r64_db_engine/core/    -> 0 lines
git grep -rniE "\brest\b"          src/r64_db_engine/core/    -> 0 lines
git status --porcelain             src/r64_db_engine/core/    -> empty
```

The whole `src/` diff for a **new source class** is:

```
 src/r64_db_engine/drivers/__init__.py      |   6 +      <-- the entire wiring
 src/r64_db_engine/drivers/rest/*           | 1347 +     <-- the new driver
```

Six lines in the registry. Nothing else outside the driver's own directory.

### Battery verdicts (committed packs, `factory/evidence/`)

| dialect | PASS | FAIL | SKIPPED (with reason) |
|---|---:|---:|---|
| `clickhouse` / `perf_1m` | 9 | 0 | `recipe_security_invariants` — not a recipe-lane dialect |
| `rest` / `open_meteo_berlin_hourly` | 9 | 0 | `rf002_null_discriminator` — window has zero nulls, verified at source |

### Suite

| | passed | skipped | collected |
|---|---:|---:|---:|
| Baseline (`main` @ `7820d18`) — local | 325 | 40 | 365 |
| This branch — local, pre-audit | 558 | 66 | 624 |
| This branch — local, **post-remediation** | **668** | 66 | 734 |
| This branch — **CI**, PR #9 (pre-audit) | 555 | 69 | 624 |

CI passes three fewer and skips three more than local: the two
`systemd-analyze verify` cases and the one deployment-host path check, each
skipped with an explicit reason because a CI runner is not the deployment host
(F-10).

Of the 66 integration-gated tests, the **26 new ones** were run green this
session with `--integration`: clickhouse conformance (13), rest conformance
(10), and the sweep (3, including the poisoned-target repair brief). The other
**40 are pre-existing** (postgres via testcontainers, supabase, the clickhouse
e2e suite) and were **not run** — they need sources this session did not stage,
and nothing on this branch touches them. Stating that rather than implying a
full green integration sweep.

`ruff check src tests factory` clean. `mypy src factory` clean (46 files,
0 issues). `systemd-analyze verify` clean on both units.

> **Correcting an earlier statement in this document.** An earlier draft said
> the local mypy/numpy failure mattered "because CI runs `mypy src`", implying
> CI was affected. **That was wrong.** CI's mypy step *passes* — it runs Python
> 3.11 and gets a numpy whose stub parses. The failure is **local only**, an
> artifact of this venv running 3.13 against `python_version = "3.11"`
> (`numpy/__init__.pyi:737`). See D-8, rescoped.
>
> That mattered more than a footnote: because `mypy src` is inert on this
> machine, I had no local signal that my own Phase 3 code was breaking CI's
> existing mypy step. It was. See **F-7**.
>
> **CI *is* red on `main`**, for a different and entirely pre-existing reason —
> see **F-8**. Fixed on this branch under D-11 (authorized 2026-08-17), so that
> the auditor sees a CI where every failure is attributable to this branch or to
> nothing. **`main` itself stays red until this branch merges** — the fix rides
> in with the merge rather than being back-ported, because a separate hotfix to
> `main` would diverge from the branch under audit for no benefit.

### CI on the PR — [#9](https://github.com/kosmosis12/r64-db-engine/pull/9)

`pull_request` is the only trigger this branch can hit (`push` fires only for
`main` and `claude/**`), so the PR *is* the first real CI execution — which is
why it was opened before the audit rather than after.

| run | sha | Ruff | Mypy | Unit tests | result |
|---|---|---|---|---|---|
| [32013443763](https://github.com/kosmosis12/r64-db-engine/actions/runs/32013443763) | `15438ac` | pass | **pass** | 3 failed, 554 passed | failure |
| [32014133500](https://github.com/kosmosis12/r64-db-engine/actions/runs/32014133500) | `f2638be` | pass | **pass** | **555 passed, 69 skipped** | **success** |

Three things that run proved, in order of how much they mattered:

1. **The healed `mypy` step passes with `factory/` on its path** — first green
   type-check of this code in CI, and the step that F-7 had silently broken.
2. **D-11 worked.** 554 tests passed where 321 did before: the golden `.ramdb`
   fixtures load, so the byte contract is guarded in CI for the first time.
3. **Three failures, all mine** — and a real defect, not a CI quirk. See F-10.

Every skip in the green run carries a stated reason; none is silent.

---

## 2. Findings (discoveries, not departures)

**F-1 · ClickHouse scan order is nondeterministic, and it defeats byte-identity.**
Found by the battery's own checksum check on its first run. Two consecutive
pulls of the same unchanged `meshbench.perf_1m` returned the same multiset of
rows in a **different order** — `row_id` began at 196608 on one pull and 131072
on the next, neither ascending — because ClickHouse reads parts and granules in
parallel and a bare `SELECT` promises no order. The permuted row order then
permuted the `status` dictionary, since dictionary values are assigned in
first-seen order. **Row count, schema, block layout and all twelve aggregates
were identical; only the bytes differed.** Cured by the documented route —
decide an `ORDER BY`, verify by checksum: with the order pinned via inline SQL,
three consecutive pulls produced one sha256 (`db2912df…`). Recorded at length in
`factory/targets/clickhouse-meshbench.yaml`.
*A production `full_refresh` without a pinned order is still CORRECT — every row
arrives, every aggregate matches. Only byte-reproducibility is lost.*

**F-2 · The meshroad Flight counters are CUMULATIVE, not per-pass.**
`get_flight_info` app_metadata reports over the server's lifetime. A raw warm
snapshot therefore carries the cold pass's misses and reports a **50% miss rate
on a perfectly warm cache** — the single most likely way to misread this
instrument, and it would have produced a plausible-looking failed gate. The gate
takes three snapshots and judges deltas. Measured: cold 32 misses / 32 decoded /
0 copied; warm 32 hits / 0 misses / **0 decoded** / 0 copied. Also verified that
`get_flight_info` itself does not perturb the counters.

**F-3 · A live API had to be chosen for determinism, not convenience.**
open-meteo's *forecast* endpoint cannot be checksum-gated: it is recomputed
continuously and its allowed date range **moves with the current date**. The
*archive* reanalysis of a fixed past window is stable — verified by hashing two
responses before committing. Choosing a source that can be pulled twice with the
same answer is part of authoring a recipe book, not an accident of which
endpoint was tried first.

**F-4 · The sweep died on its own refusal messages.** `SystemExit.code` is an
`int` when the battery exits with a verdict and a **string** when it refuses its
own inputs (missing spec, unknown table). `int(exc.code)` on the string form
raised `ValueError` out of the handler and killed the entire sweep, so one
misconfigured target took every later target down with it — the opposite of what
a sweep is for. Found by the "unreachable source" test. Fixed.

**F-5 · The repair brief's diff was filtering on the wrong side.** It listed only
comparisons whose *actual* value moved. But the most common triage case is the
**expected** side moving (a re-captured ground truth, an edited spec), where the
pipeline's own value is unchanged — so the poisoned-target run produced a repair
brief with an **empty diff under a FAIL verdict**. Now every failing comparison
is listed with both sides from both runs, plus a line telling the reader which
column to read first. This is the difference between a brief that diagnoses and
one that merely announces.

**F-6 · The sweep had no time bound, and an unreachable source hangs rather
than fails.** Found by the "unreachable source is a red sweep" test, which sat
there for **eleven minutes** before I killed it: the ClickHouse client retried
against a closed port and was still going. A dead endpoint does not fail fast,
and in-process there was nothing to stop it — so the sweep hung, which is
strictly worse than failing, because nothing alerts and the timer looks
healthy. Each target/table now runs as an **isolated, time-bounded subprocess**
(`--per-target-timeout`, default 1800s), which also fixes F-4's blast radius by
construction and guarantees the sweep exercises the same CLI an operator runs.
*Related, and left alone deliberately: the ClickHouse driver's own connect path
has no short timeout against a dead port. That is on `main`, predates this
branch, and fixing it is a `core`-adjacent driver change — filed as D-10.*

**F-7 · My own Phase 3 code broke CI's *existing* mypy step, and only the #11
extension made me look.** Wiring mypy to `factory/` surfaced 19 type errors —
and **three of them were in `src/`** (`drivers/rest/security.py`,
`drivers/rest/engine.py`), which CI's already-present `mypy src` step would have
failed on. I had not caught them because `mypy src` is inert *on this machine*
(F-9), so I had no local signal. All 19 are fixed; two were latent defects
rather than annotations: a `cursor_param`/`page_param` of `None` would have
become the literal key `"None"` in a query string, and a `sockaddr[0]` typed
`str | int` was flowing into a function whose signature promised `str`. Ruff on
`factory/` then caught a third: a closure over a loop variable that would have
named the **wrong spec entry** in an error whose entire job is to say which
entry is wrong.
*This is the strongest argument for the ratified change: the gap was not
hypothetical, and it was already in a pushed commit.*

**F-8 · CI has been RED on `main` since 2026-08-14, for an unrelated
pre-existing reason.** `gh run list` shows `main@7820d18` failing. It is **not**
mypy (that step passes in CI) and not lint — it is four failures in
`tests/core/test_ramdb_golden.py`, because the golden `.ramdb` fixtures those
tests compare against are caught by a blanket `*.ramdb` rule at `.gitignore:29`
and were never committed. They exist only on this machine.
The tests' own docstring calls them *"THE NO-BEHAVIOUR-CHANGE CONTRACT"* for
customer-facing `.ramdb` output and says a failure is *"a release-blocking
regression, not a fixture to refresh"* — so the guard on the ramdb byte contract
has been unable to run in CI at all. **Not fixed here:** un-ignoring those files
is a repo-hygiene policy call on a pre-existing condition, and it is outside
both this brief and the #11 ratification. Filed as D-11 with a one-line fix.

**F-10 · My systemd tests asserted where they were RUNNING, not what the unit
says.** The first PR CI run failed three tests, all mine. A systemd unit must
hardcode absolute paths on its deployment host, so it legitimately names
`/home/kos/...` while a CI runner checks the repo out at `/home/runner/work/...`.
Asserting the unit's paths equal `conformance.REPO_ROOT` therefore tested where
the tests happened to be running rather than whether the unit was correct — the
units were right, the tests were machine-dependent.

Replaced with the property that is both portable and the one with actual teeth:
**internal consistency** — whatever root the unit declares as
`WorkingDirectory`, its `ExecStart` must use *that* root's venv and *that*
root's sweep. A unit whose two halves drifted apart would install cleanly and
fail at 04:00 on a Sunday, and that is now what is checked. The stronger "and
that root is this checkout" claim is kept but fenced to its own test, skipped
off-host with a reason naming both paths; `systemd-analyze verify` likewise
skips when the declared interpreter is absent, since it resolves `ExecStart`
and cannot verify a unit whose binary does not exist. Both are honest
environmental skips — the units are still verified for real on the deployment
host, the only place the answer means anything.

*Worth noting as a pattern: this is the third defect this session found by
running the same assertion in a second environment (the others being F-2's
cumulative counters and F-6's missing time bound). A check that has only ever
run in one place has not been tested, it has been observed.*

**F-9 · Non-public address categories overlap.** `0.0.0.0`, `127.0.0.1` and
`169.254.169.254` all report `is_private` as well as their own more specific
category, so a naive check order labelled the cloud-metadata address merely
"private". Every branch refuses either way, but the message is what a reader
acts on. Ordered most-specific-first.

---

## 3. Deviations and decisions

**Ratification status (2026-08-17): all 13 ratified by Kos.** #2, #3, #1 and
#11 approved explicitly; #11 additionally ratified for immediate application
and is now applied (see below). The remaining eight ratified as disclosed.
Per #3's ratification, a ledger note: *revisit `httpx`/`jsonschema` as a
`[rest]` extra only if dependency footprint ever becomes a concern.*
MF-01/02/03 remain **FENCED** until the Codex verdict — claims are ratified
after audit, not before. MF-04 is withdrawn as a claim and filed as finding
**F-1**: ClickHouse substrate behaviour plus a cure, not a portable capability.


Reversible unless noted. Nothing contract-touching was done without asking.

1. **Battery has TEN checks, not nine.** Added `recipe_security_invariants` as
   check #9. The brief required the https→http and hostname-mutation refusals to
   be part of the reduced battery, not only unit tests. It is
   **SKIPPED-with-reason** for non-recipe dialects so the battery stays uniform
   — a missing check then reads as a shrunken battery rather than as a different
   lane. It mutates the **shipped book's real hosts**, because a fence correct in
   `security.py` but never wired into the loader would pass every unit test.

2. **`rest` driver lives at `src/r64_db_engine/drivers/rest/`, not
   `factory/rest_driver.py`.** The brief permitted either. `drivers/` is right
   because this driver *ships* and pulls production data, so it belongs in the
   installed package where CI's ruff and mypy already reach it — rather than in
   tooling importable only via CWD. Gate F3's proof is unaffected and in fact
   sharper: it is now exactly parallel to clickhouse and postgres.

3. **`httpx` and `jsonschema` added to base `[project.dependencies]`, not an
   extra.** (Group placement was left to my judgment; disclosing it.)
   `core/config.py::_registered_dialects` already documents that validating *any*
   config imports the whole driver registry, which is why psycopg and
   clickhouse-connect are base deps. An extra would turn `dialect: postgres` into
   an `ImportError` on a machine that never intended to use `rest`.

4. **`uv sync --all-extras` removed `duckdb==1.5.5` from the venv.** It is
   declared only on `feat/arrow-lane`, not on `main`. Nothing on this branch
   needs it. **Re-running `uv sync --all-extras` after switching back to
   `feat/arrow-lane` restores it** — already standing discipline, noted so the
   absence is not mistaken for damage.

5. **The recipe book is referenced by PATH, not inlined in the config.** The
   `rest:` block is `{recipe_book: <path>}`; the book at
   `factory/recipes/<source>.yaml` carries recipes/threading/output/limits. The
   book is the compiled artifact of a research phase — reviewed on its own,
   versioned on its own, reusable across deployments — and inlining it would make
   every environment a separate copy of the one thing that must not diverge.

6. **Threading shape, as the brief asked me to disclose: TWO recipes across TWO
   DIFFERENT HOSTS.** `geocode` (geocoding-api.open-meteo.com) resolves "Berlin"
   to coordinates; `archive` (archive-api.open-meteo.com) reads the hourly series
   for them, bound via `geocode.results[0].latitude/longitude`. Each URL pins its
   own host, so neither recipe can reach the other's. A third shape was also
   needed and is disclosed as part of this item: the extract is **columnar**
   (parallel arrays), not records, because that is what open-meteo returns.

7. **`bench/GROUND-TRUTH-openmeteo.json` added.** The brief's reduced battery did
   not require aggregate parity on the recipe lane, but the archive window is
   stable enough to have real source-captured ground truth, so aggregate parity
   gates there too. Captured with raw `urllib` — no engine, no httpx, no recipe
   machinery.

8. **Evidence-pack filenames use the LOCAL date**; the unambiguous instant is
   inside as `generated_utc`. A UTC stamp filed an evening run under tomorrow,
   and every other dated artifact in this repo is local-dated.

9. **`factory/repair.py` is code, not only a template in the skill.** The sweep
   has to *instantiate* a brief, so the template lives in both places: prose in
   `r64-factory-maintenance/SKILL.md` for a human authoring one by hand, and a
   renderer here for the sweep. They are kept in step deliberately.

10. **The sweep fails on accumulated recipe drift events even when every battery
    is green.** A green verdict sitting beside an unread repair log is the shape
    of a problem going unnoticed.

11. **CI/ruff/mypy path extension — RATIFIED AND APPLIED** (2026-08-17).
    `.github/workflows/ci.yml` now runs `ruff check src tests factory` and
    `mypy src factory`. `testpaths = ["tests"]` already covered `tests/factory`.
    **Applying it was not cosmetic** — it immediately found 19 mypy errors and
    one ruff error, three of the mypy errors being in `src/` and therefore
    already breaking CI's *existing* `mypy src` step from my Phase 3 commit
    (F-7). Two were latent defects, not annotations. All fixed; `mypy src
    factory` is clean over 46 files.
    *Caveat worth stating: the mypy verification was done with a one-off
    `--python-version 3.13` override, because the pinned 3.11 cannot parse this
    venv's numpy stub (D-8). CI type-checks under 3.11. The fixed code contains
    no version-specific syntax and uses `from __future__ import annotations`
    throughout, so the risk of a 3.11-only mypy complaint is low — but it is
    unverified locally, and the first PR will be the real test.*

12. **The sweep runs each target/table as an isolated, time-bounded
    subprocess** (`--per-target-timeout`, default 1800s), rather than
    in-process. Beyond what the brief specified, and added because of F-6: an
    unreachable source hung the sweep for eleven minutes with nothing to stop
    it. It also fixes F-4's blast radius structurally and keeps the sweep on the
    same CLI an operator would run by hand.

13. **Corrections to committed documents** (both ratified in your Phase 0 ack):
    `docs/MESHFORGE-SKILL-INDEX.md` — the RF-002 rule was "≥2 actively
    discriminating columns"; replaced with dataset-declared discriminators
    (floor ≥1, exact null counts gate), with a note recording where the ≥2 came
    from. `CC-BRIEF-meshforge-r64.md` — bridge-allow recovery path corrected from
    `config.d` to `users.d/zz-allow-docker-bridge.xml` with the merge-order
    rationale.

---

## 4. Systemd install — copy-pasteable, no placeholders

**You run these; I do not sudo.** Units verified with `systemd-analyze verify`
before commit.

```bash
sudo install -m 0644 -o root -g root \
  /home/kos/builds/r64-db-engine/factory/systemd/r64-factory-conformance.service \
  /etc/systemd/system/r64-factory-conformance.service

sudo install -m 0644 -o root -g root \
  /home/kos/builds/r64-db-engine/factory/systemd/r64-factory-conformance.timer \
  /etc/systemd/system/r64-factory-conformance.timer

sudo systemctl daemon-reload
sudo systemctl enable --now r64-factory-conformance.timer

# Verify
systemctl list-timers r64-factory-conformance.timer
systemctl cat r64-factory-conformance.service

# Dry run once, on demand, without waiting for Sunday
sudo systemctl start r64-factory-conformance.service
journalctl -u r64-factory-conformance.service -e
```

### ROOT-QUIESCE CHECKLIST ADDITION (required)

The sweep pulls a million rows twice per target and spins an ephemeral Arrow
Flight server. A bench series measuring an otherwise-idle machine would pick
that up and **report it as a lane effect**. Add to the checklist:

```bash
sudo systemctl stop  r64-factory-conformance.timer   # BEFORE a bench series
sudo systemctl start r64-factory-conformance.timer   # AFTER the series
```

`Persistent=true` means a sweep missed while stopped runs once on the next
start rather than being lost — so quiescing costs a delayed sweep, never a
skipped one. The flip side: it fires shortly after you re-enable it, so
re-enable **after** the series, not between reps. The same warning is in the
timer file itself, because the person reading it at 2am is not reading this.

---

## 5. Deferred items ledger

| # | Item | Why deferred | Trigger |
|---|---|---|---|
| D-1 | **DuckDB conformance run** | The duckdb driver is on `feat/arrow-lane`, not `main`. Not cherry-picked, per the brief. | After `feat/arrow-lane` merges: write `factory/targets/duckdb-meshbench.yaml` + a probe, run the battery. |
| D-2 | **Snowflake as the first factory-admitted driver** | Phase 3 opener of the rollout; needs a `DRIVER-PLAN.md` generated by `r64-connector-factory` and ratified before code. | Next driver session. **Trap pre-named: Snowflake's session timezone defaults to `America/Los_Angeles`** (B-2 class) — already written into the skill's trap row. |
| D-3 | **Sources-tab intake** | meshroad repo; separate session. | Cockpit "Request connector" → instantiates `CC-BRIEF-driver-<dialect>.md` from the factory template. |
| D-4 | **Recipe→driver promotion path exercised once** | The path is documented in `r64-recipe-engine` but has never been walked end to end. | Take `open-meteo.yaml` (or the first real recipe source) into a full driver campaign and confirm the book genuinely fills `DRIVER-PLAN.md` rows 1, 2 and 9. |
| D-5 | **Partical lane** (`partical-plane-factory`, `partical-tenant-factory`) | Prerequisite is STR-03 topology.env single-source. | After STR-03. |
| D-6 | **CI coverage for `factory/`** | Proposed, not applied — deviation 11. | Your call. |
| D-7 | **`src/r64_db_engine/factory/` relocation** *(alternative you asked to be recorded)* | Would ship the conformance battery packaged with the engine instead of as repo tooling. Trade-off: it would weaken the Gate F1 property that core cannot import its own oracle. **Explicitly not acted on this session — your decision.** | Your call. |
| D-8 | **mypy/numpy stub failure — LOCAL ONLY, rescoped** | `mypy src factory` cannot run in this 3.13 venv: numpy 2.5.2's stub uses PEP 695 syntax that mypy refuses to parse under `python_version = "3.11"`, and it aborts with "errors prevented further checking" — so locally mypy checks **nothing**. CI is unaffected (3.11 gets a parseable numpy). `follow_imports = "skip"` does not help: the stub is parsed to build the module either way. The real lever is `python_version`, and raising it changes what CI guarantees for a package whose `requires-python` is `>=3.11` — a judgment call, not a tidy-up, so **not taken**. Verified this session with a one-off `--python-version 3.13` override; config untouched. | Whenever the 3.11 floor is revisited, or a local mypy signal is wanted badly enough to justify the trade. |
| D-12 | **Bounded transport retry on the recipe lane** | The engine makes one attempt per page and fails. The skill previously claimed "retry the REQUEST on transport failure", which described code that does not exist (Codex round 2, Q4a); the claim was deleted rather than the behaviour assumed. Doctrine when it lands: retry the REQUEST, never the MEANING — a schema-invalid response is never retried differently. | Whenever a real source's flakiness makes one-shot pulls impractical. |
| D-11 | ~~**CI red on `main`: golden `.ramdb` fixtures are gitignored**~~ **AUTHORIZED AND APPLIED, 2026-08-17** | `tests/core/test_ramdb_golden.py` compares against `tests/golden/ramdb/*.ramdb`, which `.gitignore:29` (`*.ramdb`) excluded, so the four fixtures existed only on this machine and CI had failed since 2026-08-14 — the `.ramdb` byte contract those tests call "release-blocking" was never actually guarded there. Fixed by a scoped negation (`!tests/golden/ramdb/*.ramdb`) plus the four files, 1866 bytes total. The single `*` does not cross `/`, so generated output under `tests/golden/ramdb/loading/` stays ignored — verified: a plain `git add` stages exactly the four fixtures and nothing else. All 7 tests in that file pass against the tracked fixtures. | **Done.** See the note below on `main`. |
| D-9 | **DNS-rebinding window in the recipe lane** | Between resolve-and-validate and httpx's own resolution, DNS can change. Closing it means pinning the validated IP while carrying the hostname for TLS SNI — a transport change, not a tightening. **Stated in `security.py`'s docstring rather than papered over.** | If the recipe lane is ever pointed at a host whose DNS is not trusted. |
| D-10 | **ClickHouse driver connect has no short timeout against a dead endpoint** | Surfaced by F-6: the client retried against a closed port for 11+ minutes. The sweep now bounds it externally, so nothing is hanging today. Fixing it properly is a driver change on `main`, outside this brief's scope. | Next clickhouse-touching session, or whenever a connect hang bites interactively. |

---

## 6. Merge posture — READ THIS BEFORE MERGING

**Branch pushed to `origin/feat/meshforge-factory`. NOT merged to `main`, and
must not be merged yet.**

The cross-agent QA gate is mandatory between conformance-green and merge, and
**the builder is never the auditor** (Law 2). Conformance-green is *necessary
and not sufficient*. I wrote this code; I am the wrong reviewer for it.

Suggested audit scope for the second agent (Codex):

1. **`factory/battery.py`** — are the judges actually pure, and does each one
   fail for the reason it claims? The failing fixtures in
   `tests/factory/test_battery.py` are the thing to attack: a fixture that
   passes for an incidental reason gives false confidence in the whole battery.
2. **`drivers/rest/security.py`** — the destination fence. Specifically: is the
   label-boundary host match airtight, is resolution-time checking actually
   reached on *every* request including paginated link-header URLs, and is the
   documented DNS-rebinding limit the only residual one?
3. **`drivers/rest/engine.py`** — is there any path where a schema-invalid
   response is salvaged, coerced, or retried with a different interpretation?
   That would be a Law 1 breach and the most damaging possible defect here.
4. **The evidence packs themselves** — Law 2 says review the pack, not the diff.
   Can you ratify both drivers from `factory/evidence/EVIDENCE-*.md` alone? If
   not, that is a finding against the pack.

---

## 7. Claims-register candidates — **FENCED, pending Kos's ratification**

**No external travel until ratified.** Not in a README, a post, or a customer
conversation.

| ID | Claim | Evidence |
|---|---|---|
| **MF-01** *(rescoped after Codex round 2)* | *Every negative fixture is rejected by the real battery with its declared full verdict vector; metadata-denied adversarial stubs (unconditional, adaptive-echo, name-pattern) cannot pass; guards are registry-derived.* The real pipeline is separately proven able to fail: a poisoned ground-truth copy turns a green run red with exactly the poisoned aggregate mismatching. | `tests/factory/test_battery.py`, `tests/factory/scenarios.py`; `test_a_poisoned_ground_truth_makes_the_battery_fail` on both lanes |

> **Why this wording changed twice.** Round 1 said "an oracle proven able to
> fail" — too weak: an oracle that fails at *everything* satisfies it. Round 1's
> fix (assert the reason code) was still overclaimed, because the harness handed
> the stub the target check. The claim now states exactly the operational
> property the tests establish and nothing beyond it: **no stronger wording
> anywhere.**
| **MF-02** | *Zero-core-edit `rest` dialect.* The registry pattern holds on a non-database source class: adding a whole new class of source cost **six lines** in `drivers/__init__.py` and nothing else outside the driver's own directory. | `git grep -rniE "\brest\b" src/r64_db_engine/core/` → 0; `git diff --stat main..HEAD -- src/` |
| **MF-03** *(rescoped rounds 1 and 2)* | *Destination fixed at authoring; every request re-validated (HTTPS, host rule, public address); pagination confined to the pinned endpoint — scheme, port and canonicalized host must match exactly, the candidate URL never reaches the client, requests are rebuilt from pinned parts; redirects refused unread; rebinding closed by a pre-body peer-vs-vetted-set assertion whose residual window is that the request has already reached the socket.* Each with a failing fixture. | `tests/drivers/rest/test_security.py`; `tests/drivers/rest/test_engine.py`; `recipe_security_invariants` in `EVIDENCE-rest-*.md` |

**Which behaviours are battery-level vs unit-level — precision over reach.**
The `recipe_security_invariants` battery check runs **14 mutations against the
shipped book's real pinned URLs**, and covers exactly the statically-checkable
behaviours:

| behaviour | battery | unit | why |
|---|:--:|:--:|---|
| https→http downgrade refused | ✅ | ✅ | pure function of the book |
| lookalike / subdomain host on the AUTHORED url | ✅ | ✅ | pure function of the book |
| templated URL refused | ✅ | ✅ | pure function of the book |
| undeclared threading input refused | ✅ | ✅ | pure function of the book |
| private/loopback destination refused | ✅ | ✅ | resolution only, no server needed |
| **cross-path / undeclared-path / subdomain next-URL confinement** | ✅ | ✅ | `confine_next_url` is pure over (crafted Link, pinned URL) |
| **`allowed_next_paths` omitted vs explicitly empty** | ✅ | ✅ | book-level, decided by the loader |
| redirect (3xx) refused unread | ❌ | ✅ | needs a response, so it is fixture-driven |
| **live DNS rebinding / peer-vs-vetted under attack** | ❌ | ✅ | needs a HOSTILE SERVER answering one address to validation and another to connect — not constructible from a static book |

The last two are **unit-level only, and deliberately so**: a battery mutation
cannot manufacture a redirecting or rebinding server from a config file. They
are covered by fixtures that assert the property rather than the exception type
(`body_reads == 0` on every refused response). Claiming battery coverage for
them would be reach without precision.

> **Why the wording changed.** The original read "destination-pinning security
> invariants" with an unqualified *pinned*. Codex was right that this overclaimed
> on two counts: a provider-supplied pagination URL could move to any SUBDOMAIN
> of the pinned host, and the validated address was not the address connected to.
> Both are now closed in code (T3), and the claim states the mechanism and its
> residual window rather than a bare adjective. **No unqualified "pinned"
> anywhere in this claim.**

**MF-04 — WITHDRAWN as a claim (2026-08-17), filed as finding F-1.** Scan-order
nondeterminism is ClickHouse substrate behaviour plus a cure (`ORDER BY` pin,
verified by checksum-of-three), not a portable capability of ours. It stays in
the findings record, where it is still the strongest single piece of evidence
that the battery does real work rather than confirming what was already
believed.

**Status of MF-01/02/03: FENCED until the Codex verdict.** Claims are ratified
after audit, not before — so nothing here travels yet, including internally.

---

## 8. Artifact index

| Path | What it is |
|---|---|
| `factory/conformance.py` | The oracle's CLI + gathering layer |
| `factory/battery.py` | The ten pure judges |
| `factory/probes.py` | Live-source probes (independent of the driver under test) |
| `factory/serve_gate.py` | Ephemeral meshroad serve, PID-explicit teardown |
| `factory/evidence.py` | Evidence-pack writers (json + md) |
| `factory/repair.py` | Repair-brief renderer |
| `factory/bin/factory-conformance-sweep` | The weekly sweep (executable, tracked) |
| `factory/systemd/*` | Service + timer (verified, not installed) |
| `factory/targets/*.yaml` | Conformance targets — normal engine configs |
| `factory/specs/*.json` | Schema, discriminators, boundaries, aggregate map |
| `factory/recipes/open-meteo.yaml` | The recipe book |
| `factory/evidence/EVIDENCE-*.{json,md}` | The review artifacts of record |
| `bench/GROUND-TRUTH-openmeteo.json` | Source-captured expectations for the recipe lane |
| `src/r64_db_engine/drivers/rest/` | The `rest` driver |
| `~/.claude/skills/meshforge/SKILL.md` | **Outside this repo** — core doctrine, travels with the machine, not the branch |
| `.claude/skills/r64-*/SKILL.md` | The four in-repo lane skills |

---

## 9. Codex round 1 — verdicts and remediation

Audit received 2026-08-17. **BLOCK on targets 1, 3, 4 · clean on 2 · target 5
rejected with one caveat.** All five addressed; branch re-pushed for re-audit.

| # | Target | Verdict | Remediation | SHA |
|---|---|---|---|---|
| **1** | Negative fixtures | **BLOCK** | Reason-specific triple + adversarial meta-fixture | `9c8ec6b` |
| **2** | — | **CLEAN** | none required | — |
| **3** | Pagination steering + DNS rebinding | **BLOCK** | Default-deny confinement; pre-body peer assertion; MF-03 rescoped | `4e46cf0` |
| **4** | Pack provenance | **BLOCK** | Dirty-tree refusal, six pinned inputs, content-addressed artifacts | `6655b90`, `deb1710`, `5e0287a` |
| **5** | Defensive `None` branch | **REJECTED** *(caveat upheld)* | `EngineInvariantError` + hand-built-Recipe unit tests | `4e46cf0` |

### What each finding actually was

**T1 — the fixtures proved less than they appeared to.** Every negative fixture
asserted `status == FAIL` and nothing more, which is satisfied by an oracle
failing for an unrelated reason and, fatally, by one failing at *everything*. A
catch-all FAIL stub would have turned the whole suite green while checking
nothing, and it would have looked exactly as green as a correct suite.

Fixed by giving every FAIL a machine-checkable `reason_code`, asserting the
triple (FAIL, check name, reason code), and adding
`test_a_catch_all_failing_oracle_does_not_pass_the_fixture_suite` — which
replays every case against that stub through the *same* assertion helper and
requires each to reject it. **Proven by mutation:** weakening the helper back
to status-only makes all 38 meta cases fail. MF-01 now rests on that
meta-fixture rather than on the count of negative tests.

**T3 — two real holes, and the second one mattered more.**
*(a)* A `Link: rel="next"` URL is chosen by the remote end, and it was going
through the host rule written for the *authored* URL — which deliberately
permits proper subdomains. A provider, or a header injector, could move the
request to any subdomain of the pinned host. Now: **scheme, port, and
canonicalized host (case + trailing-dot normalized) must match exactly; the
candidate URL never reaches the client — requests are rebuilt from pinned
parts.** Only the query string is carried over, and any cross-path move
requires an authored `allowed_next_paths` declaration.
*(b)* The address validated was not the address connected to. Requests are now
sent streaming and the real peer is asserted public **and** in the vetted set
**before any body is read**, fail-closed. Tests assert `body_reads == 0` on a
refused peer — the property, not the exception type — and prove the check runs
on every page, since page-one-then-rebind is the interesting attack.

*Which form was implemented, and why:* the **minimum acceptable** peer
assertion, not the preferred connect-to-vetted-IP transport. httpx exposes no
first-class hook for pinning the IP while carrying the hostname for SNI; it
needs a custom transport wrapping httpcore's pool, which is disproportionate
here and version-fragile. **Residual window, stated in `security.py` and in the
claim:** the request has already reached the socket, so a rebound peer sees the
request line, headers and any API key. What is prevented is the *response*
being trusted, parsed, or turned into pulled data.

**T4 — packs named a commit whose content was not what ran.** Now a hard
refusal before any work (a dirty tree costs a second, not two million-row
pulls), naming the uncommitted paths. `--allow-dirty` stamps ALLOW-DIRTY into
the verdict line, into a header warning block, and sets `ratifies_head: false`.
Six inputs pinned by sha256: target config, recipe book (explicit `null` on the
DB lane), schema spec, ground truth, executed implementation (version + a
digest over 46 source files, because a git SHA identifies a *commit*, not an
*install*), and the produced artifact.

*One design flaw found by running it:* the first target's pack dirtied the tree,
so every later target refused. `factory/evidence/` is now exempt — the exemption
covers the **product**, never an input, and is recorded in provenance as
`dirty_exemption` rather than applied silently.

*Artifact storage, both routes exercised:* rest 36,090 bytes **copied** (bytes
re-verifiable in-repo); clickhouse 149,806,522 bytes **content-addressed
manifest** (committing 150 MB would bloat the repo without adding a check the
sha256 does not already give). Which route was taken is recorded, so a reader
never guesses whether bytes are present.

**T5 — caveat upheld.** Codex was right that the loader makes the branch
unreachable via any supported runtime path, so it is not dead code to delete
but an invariant to assert. It now raises `EngineInvariantError` rather than
returning `None`: silently ending pagination would truncate the pull to its
first page and report success, the worst outcome available to it. The invariant
lives in `tests/drivers/rest/test_engine.py`, constructed by hand, alongside a
test that the loader genuinely refuses such a book.

### Verification after remediation

| | result |
|---|---|
| Suite (local) | **668 passed / 66 skipped** |
| With `--integration` | **369 passed** (`tests/factory` + `tests/drivers/rest`) — the pre-audit 258 all still green, plus 111 added by the remediation |
| `ruff check src tests factory` | clean |
| `mypy src factory` | clean, 46 files |
| `git grep -rnE "(^\|[^_])[Ff]actory" src/r64_db_engine/core/` | **0** |
| `git grep -rniE "\brest\b" src/r64_db_engine/core/` | **0** |
| `git status --porcelain src/r64_db_engine/core/` | empty |
| Both packs | ratify `deb1710`, clean tree, `ratifies_head: true` |

### Claims status after round 1

**MF-03 rescoped** — the original said "destination-pinning" with an unqualified
*pinned*, which overclaimed on exactly the two counts Codex found. It now states
the mechanism and its residual window. **MF-01 strengthened** — its support is
now the meta-fixture. **MF-02 unchanged.** All three remain **FENCED** pending
the round-2 verdict.

Remediation commits:

```
5e0287a T4(evidence): regenerate both packs at the clean head (packs)
deb1710 T4(evidence): exempt the pack's own output from the dirtiness rule
6655b90 T4(evidence): packs refuse a dirty tree and pin every input (code)
4e46cf0 T3(rest): pagination steering and DNS rebinding, both default-deny
9c8ec6b T1(oracle): negative fixtures assert the REASON, not merely that something failed
```

---

## 10. Codex round 2 — verdicts and remediation

Audit received 2026-08-17. **BLOCK — Q2 CLEAN, Q1/Q3/Q4 exploitable.** All
addressed; branch re-pushed for round 3.

| # | Question | Verdict | Remediation | SHA |
|---|---|---|---|---|
| **Q1** | Meta-fixture leaked the answer key | **BLOCK** | Metadata-denied harness, full verdict vectors, registry-derived guards | `494ce87` |
| **Q2** | Wire-level encoding attacks | **CLEAN** | none required — see below | — |
| **Q3** | Provenance laundering | **BLOCK** | Inputs-outside-evidence rule, extended pins, CLOSURE BOUNDARY | `5991539` |
| **Q4** | Claims vs code | **BLOCK** | Retry claim deleted, pinning language, redirect fixture, 14 mutations | `143490f` |

### Q2 — CLEAN, recorded

**The wire-level surface held under encoding attacks.** Codex probed the
destination fence with encoded and malformed authorities and found no bypass:
the host comparison operates on `urlsplit().hostname`, which normalizes case and
performs IDNA/percent handling before the label-boundary test, so the encoding
tricks that defeat naive string matching do not reach it. Recorded here because
a clean verdict on an adversarial probe is evidence, and evidence that is not
written down did not happen.

### Q1 — the harness was grading its own answer

Round 1's fixtures asserted a triple (FAIL, check, code) — better than `assert
FAIL`, and still not enough, because **the case passed its target check into the
assertion**. A stub handed that metadata can echo it back and pass without
inspecting an input. The suite was measuring the stub's access to metadata.

Now: fixtures are complete SCENARIOS replayed through a harness that hands an
oracle exactly what the real battery receives and nothing else; manifests
declare the verdict of all ten checks, so a catch-all fails on the nine it
wrongly reds; meta-replays use content-addressed identifiers so name matching is
unavailable; and three adversaries (unconditional, adaptive-echo, name-pattern)
must all fail. **Two fenced negative controls** prove the fences are
load-bearing: the echo stub PASSES when deliberately leaked the manifest, and
the name matcher provably behaves differently on real names. Without those, "the
stub failed" would be equally consistent with a stub too weak to matter.

Guards are derived from `battery.CHECK_NAMES` and `battery.MECHANISMS`; the
duplicated hard-coded set is deleted. Proven by mutation three ways — adding an
unproven check, leaking unconditionally, and leaking in the adversary test (37
cases red).

### Q3 — the exemption was the laundering mechanism

`factory/evidence/` carries the dirty-tree exemption so a pack's own output
cannot invalidate it. An **input** placed there inherited that exemption: it
could be swapped between runs without ever making the tree dirty, and every pack
would keep reporting `ratifies_head: true` while its inputs moved underneath it.
Now two-sided — the exemption covers outputs only, and only paths outside the
resolved input set. A pinned input under `factory/evidence/` is refused, naming
the path, before any pull.

Extended pins: `pyproject.toml` + `uv.lock`, the meshroad binary's content
address (it is a build artifact from outside this repo, and the serve gate
reports *its* counters), python version/implementation/platform triple, and the
proxy environment **with values** — a pack naming a hostname while `HTTPS_PROXY`
silently rerouted every call would describe a run that did not happen.

**Secrets are not hashed.** A sha256 of a low-entropy API key is
offline-guessable and an evidence pack travels; publishing a digest of a short
credential hands an attacker an oracle to grind against. Path, size, mtime and
mode only, asserted negatively.

Every pack now carries a **CLOSURE BOUNDARY** section naming what it does *not*
establish: secret contents, native/runtime deps beyond the lockfiles, live
source state (**measured, not pinned** — the measurements are in the pack and
establish what the source held *during this run*), and the wall clock. The
overclaimed "every input the run consumed" is reworded to the bounded form, with
a test preventing the old phrasing from creeping back.

### Q4 — a claim with no implementation behind it

The skill claimed transport retry. **No retry of any kind exists** — the engine
makes one attempt per page and fails. The line is deleted rather than the
behaviour assumed; ledger **D-12** records that the claim returns when the code
does.

The redirect fixture exposed real code: the client did disable following, but
the engine had no explicit 3xx branch, so a redirect fell through into a JSON
parse error and reported the wrong cause. Now refused by name, naming the
declined Location, before the body is read.

Battery mutations went **6 → 14** against the shipped book's real pinned URLs.
The `allowed_next_paths` omitted-vs-empty pair is decided at the **book level**
through the real loader — passing `[]` twice at the call site would have tested
nothing about how an omitted key loads. A default-deny rule that only denies
when someone remembered to write `[]` is not default-deny.

§7's MF-03 entry now enumerates exactly which behaviours are battery-level and
which are unit-level, with the reason for each. Redirects and live rebinding are
unit-level **only**: a battery mutation cannot manufacture a redirecting or
rebinding server from a static config file.

### Verification after round 2

| | result |
|---|---|
| Suite (local) | **791 passed / 66 skipped** |
| With `--integration` | see below |
| `ruff check src tests factory` | clean |
| `mypy src factory` | clean, 46 files |
| Both greps | **0** · `core/` untouched |
| Both packs | ratify `246911c`, clean tree, `ratifies_head: true`, 14 security mutations |

### Claims after round 2

**MF-01 rescoped** to the operational property, verbatim per the audit.
**MF-03 rescoped** with the exact pinning wording and a battery-vs-unit
breakdown. **MF-02 unchanged.** All three remain **FENCED** pending round 3.

Remediation commits:

```
246911c docs(closeout): ledger D-12 — bounded transport retry is future work
143490f Q4(claims): make every claim match the code, exactly
5991539 Q3(provenance): close the laundering path, state the closure boundary
29b4b1e Q1(oracle): deny the stubs metadata, assert the full verdict vector
981ec53 docs(closeout): record 369 with --integration after remediation
```
