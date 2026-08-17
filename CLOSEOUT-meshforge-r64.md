# CLOSEOUT — MESHFORGE factory engine, r64 lane

Branch: `feat/meshforge-factory` (off `main` @ `7820d18`)
Session: 2026-08-16 → 2026-08-17 · Brief: `CC-BRIEF-meshforge-r64.md` · Index: `docs/MESHFORGE-SKILL-INDEX.md`

**Status: all four gates met. Pushed, NOT merged — cross-agent QA is mandatory
between conformance-green and merge (Law 2). See §6.**

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
| Baseline (`main` @ `7820d18`) | 325 | 40 | 365 |
| This branch | **557** | 66 | 623 |

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
> see **F-8** and **D-11**.
>
> **Note:** this branch's CI has not run. `.github/workflows/ci.yml` triggers on
> `push` only for `main` and `claude/**`, plus `pull_request`; `feat/meshforge-factory`
> matches neither and has no PR. The lint/type/test results above are all local.

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
| D-11 | **CI red on `main`: golden `.ramdb` fixtures are gitignored** | `tests/core/test_ramdb_golden.py` compares against `tests/golden/ramdb/*.ramdb`, which `.gitignore:29` (`*.ramdb`) excludes. The four fixtures exist only on this machine, so CI has failed since 2026-08-14 and the `.ramdb` byte contract — which those tests call "release-blocking" — has never actually been guarded in CI. Pre-existing, unrelated to this branch, and un-ignoring files is a repo-hygiene call I did not make unilaterally. One-line fix: add `!tests/golden/ramdb/*.ramdb` to `.gitignore` and `git add -f` the four files (~1.9 KB total). | **Before the Codex audit ideally** — otherwise the auditor sees a red CI and cannot tell pre-existing failures from mine. |
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
| **MF-01** | *An oracle proven able to fail.* The conformance battery carries a deliberately-broken fixture per check proving each can FAIL, and the real pipeline is proven able to fail too — a poisoned ground-truth copy turns a green run red, with exactly the poisoned aggregate mismatching. | `tests/factory/test_battery.py` (one fixture per check); `test_a_poisoned_ground_truth_makes_the_battery_fail` on both lanes |
| **MF-02** | *Zero-core-edit `rest` dialect.* The registry pattern holds on a non-database source class: adding a whole new class of source cost **six lines** in `drivers/__init__.py` and nothing else outside the driver's own directory. | `git grep -rniE "\brest\b" src/r64_db_engine/core/` → 0; `git diff --stat main..HEAD -- src/` |
| **MF-03** | *Destination-pinning security invariants, test-proven.* https-only, label-boundary host matching (`evil-checkr.com` ≠ `checkr.com`, tested literally), resolution-time private/loopback/link-local rejection, no redirects, closed parameter vocabulary — each with a failing fixture, and re-asserted against the shipped book's real hosts as a battery check. | `tests/drivers/rest/test_security.py`; `recipe_security_invariants` in `EVIDENCE-rest-20260817.md` |

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
