# PARKED — Snowflake driver campaign

**Status: SUSPENDED, not abandoned.** Parked at the Phase 0 gate on 2026-08-17
by Kos's decision. Cause: the Snowflake trial account is unstable, so the
sandbox the campaign depends on cannot be relied on for a Gate B seed, a Gate C
conformance run, or the weekly re-runs that keep a driver admitted.

No code was written. No branch was created. `feat/snowflake-driver` does not
exist. The campaign stopped exactly where the `r64-connector-factory` skill
says to stop — at the Phase 0 ack, before Phase R — so resuming costs nothing
already spent.

This file exists because of D-2 in `CLOSEOUT-meshforge-r64.md`: Snowflake is
the intended first factory-admitted driver, and a deferred item whose research
lives only in a chat log has been deferred into nothing. What follows is the
audit verbatim, so the resume starts from findings rather than from scratch.

- Campaign skill: `.claude/skills/r64-connector-factory/SKILL.md`
- Doctrine: `.claude/skills/meshforge/SKILL.md`
- Origin ledger item: `CLOSEOUT-meshforge-r64.md` D-2

---

## Resume condition

Resume Phase 0 re-verification, then Phase R, when **both** hold:

1. **A working Snowflake account** — stable enough to survive a Gate B seed, a
   full Gate C battery, and the weekly `r64-factory-conformance.timer` re-runs
   that keep the driver admitted. A trial that expires mid-campaign is worse
   than no account: it produces a green pack that cannot be reproduced.
2. **An identifiers env file at `~/.r64-db-engine/sources/snowflake/` , mode
   0600**, carrying account identifier, user, role, warehouse and database, and
   the path to the keypair. Referenced BY PATH, never read into model context,
   per Law 3 and the convention `~/.r64-db-engine/sources/clickhouse-meshbench/`
   already follows.

**On resume, re-run Phase 0 before trusting anything below.** This audit is a
hypothesis about the repo as of `2d21343`; the skill is explicit that a brief
written days earlier is exactly that. In particular re-verify the core grep
still returns 0 and the battery still has 10 checks.

---

## Phase 0 topology audit — verbatim, as reported 2026-08-17

Audited at `main` @ `2d21343` (`bench: add r64-factory-conformance.timer
stop/start to root-quiesce checklist`).

### 1. Git

| | |
|---|---|
| Current branch | `main` @ `2d21343` |
| `origin/main` | `git@github.com:kosmosis12/r64-db-engine.git` |
| `feat/snowflake-driver` | **does not exist** — clean start |
| Working tree | **dirty**, 6 files, all under `factory/evidence/` |

The dirty tree was benign. The diff was entirely a sweep re-run: `generated_utc`
moved `15:34:34Z → 15:54:06Z` and provenance moved
`fae9c81`/`feat/meshforge-factory → 8339156`/`main`. That is
`r64-factory-conformance.timer` firing after the PR #9 merge and rewriting both
live packs plus both `last-green` packs. No check results changed.

> Resolved after the audit: this is the condition the sweep's new `--auto-commit`
> path exists to prevent. See "Riding item" at the foot of this file.

Branches not to disturb: all nine local feature branches, `main`.

### 2. Machinery premises — read, not assumed, on `main`

| Premise | Verdict | Location |
|---|---|---|
| `Driver` ABC | present, 6 abstract methods (`dialect_name`, `connect`, `close`, `discover`, `validate_table`, `pull`, `coerce_value`) | `core/driver.py:44` |
| Driver registry `DRIVERS` + `resolve()` | 3 entries: `clickhouse`, `postgres`, `rest`; refuses by name and enumerates | `drivers/__init__.py:10,21` |
| PG-010 registry-derived dialect resolution | `_registered_dialects()` reads `DRIVERS`, refuses at **config time** listing the registry | `core/config.py:122,210-215` |
| `ArrowIpcSink` explicit `pa.ipc` writer | `pa.ipc.new_file()` + `max_chunksize=_BLOCK_ROWS`, `_BLOCK_ROWS = 65536` | `sinks/arrow_ipc.py:102,320` |
| PG-011 refusal **wired**, not merely defined | called from the `Daemon` constructor body, not deferred to first pull | `core/daemon.py:70` → `:412` |
| Battery checks | 10: registry_admission, schema_exactness, aggregate_parity, rf002_null_discriminator, b2_boundary, pg011_refusal, block_structure, checksum, zero_copy_serve_gate, recipe_security_invariants | `factory/battery.py` |
| Probe registry `PROBES` + `resolve()` | same shape; `ClickHouseHttpProbe`, `RestRecipeProbe` | `factory/probes.py:293` |
| Ground truth + generators | **exist only for clickhouse and openmeteo** (`bench/GROUND-TRUTH-{clickhouse,openmeteo}.json`, `bench/make-dataset.sh`). Nothing for Snowflake — Gate B is greenfield. |

**Suite baseline at audit time: `861 passed, 66 skipped` in 22.9s** (skips all
`--integration`). Repo green before anything was touched.

**Gate grep verified falsifiable-and-clean:**

```bash
git grep -rniE "\bsnowflake\b" src/r64_db_engine/core/   # exit 1, zero hits
```

The word-boundary form is required and the current count was confirmed to be 0,
so the assertion is meaningful rather than vacuous. Note
`src/r64_db_engine/conformance/__init__.py:5` mentions Snowflake in a comment —
that is **not** `core/`, so it does not pollute the gate.

### 3. Source availability — the blocker

Snowflake has **no local container and no reference-grade emulator**. Every
other driver in this repo was proven against loopback with zero credentials;
`factory/targets/clickhouse-meshbench.yaml` says so in its header
(*"CREDENTIALS: none, by design"*). Snowflake cannot follow that pattern.
Law 3's preferred design — zero credentials — is **unavailable**, so the
campaign requires a hosted account and real egress. That is the structural fact
that makes trial-account instability fatal rather than annoying.

Found on the box, by metadata only. **No file contents were read.**

| Path | Perms | Size | Created | Read? |
|---|---|---|---|---|
| `~/.snowflake/r64_engine_key.p8` | `0600` | 1704 B | 2026-08-14 | **No** |
| `~/.cache/snowflake/` | `0700` | empty | 2026-04-10 | n/a |
| `~/.r64-db-engine/sources/` | — | only `clickhouse-meshbench/` | 2026-08-11 | n/a |

The `.p8` at 1704 bytes is consistent with an unencrypted PKCS#8 RSA-2048 key,
correctly at `0600`, named `r64_engine_key` — it reads as deliberate keypair-auth
setup. No `snow`/`snowsql` CLI is installed and there is no
`~/.snowflake/config.toml`, so the key path is the only artifact; every
identifier is missing.

---

## Drift findings

Two findings the D-2 brief did not anticipate. Both are recorded here because,
per the deviation-disclosure protocol, a discovery the brief did not predict is
worth more than a clean gate and burying it destroys it.

### D-2/a — driver deps land in the BASE dependency set → **standing factory defect**

`pyproject.toml:29-47` documents the coupling explicitly: validating *any*
config imports the whole driver registry (`core/config.py::_registered_dialects`),
so every registered dialect's third-party deps must be importable, or
`dialect: postgres` becomes an `ImportError` on a machine that never intended to
use the other dialect. That is why `psycopg`, `clickhouse-connect`, `httpx` and
`jsonschema` are all base deps rather than extras.

For Snowflake specifically this means registering the driver would add
`snowflake-connector-python[pandas]` to base deps, dragging in **boto3,
botocore, pyOpenSSL, cryptography, s3transfer** for every operator — including
the ones who only ever run Postgres.

**This is not a Snowflake problem. It is a standing factory defect**, and it gets
worse monotonically with each driver admitted: every new dialect taxes every
existing install. Snowflake is merely the first driver heavy enough to make it
visible.

**Disposition: fix lazily in `drivers/__init__.py` before the NEXT driver is
admitted, whichever driver that turns out to be — Snowflake or not.** It is not
a blocker for this campaign and does not justify a session of its own; it is a
precondition on the next admission.

Sketch of the fix, for whoever picks it up — **not ratified, and deliberately
not implemented here**: make registry entries lazy, so `DRIVERS` maps a dialect
name to an import path resolved only when `resolve()` is actually called, with
the module import deferred out of `_registered_dialects()`. That preserves the
two properties the registry currently buys — config-time refusal by name, and
the enumerate-the-registry error message — while letting a dialect's deps move
behind an extra. The constraint to respect: PG-010's refusal must still fire at
**config** time and must still be able to **list** every registered dialect
without importing any of them, so the names have to live in the registry
independent of the classes. Verify against `core/config.py:210-215` and the
PG-010 tests before changing anything.

### D-2/b — pyarrow pin conflict: **checked, retired**

This was the largest technical risk going in. `pyarrow==25.0.0` is an exact,
load-bearing pin: it owns Arrow IPC block layout, and meshroad's artifact
generator is pinned to the same version, so floating it would silently change
the block structure the consumer's per-block column cache is keyed on.

Resolved at Phase 0 rather than discovered at Gate A:

```
snowflake-connector-python[pandas]  +  pyarrow==25.0.0  +  pandas>=2.0
  → snowflake-connector-python==4.7.2, pyarrow==25.0.0, pandas==2.3.3
```

The 4.x line takes pyarrow via the `pandas` extra without an upper bound tight
enough to fight the pin. **No conflict. Risk retired.**

Caveat on resume: this was resolved against the index as of 2026-08-17 for
Python 3.13. Re-run the resolution before relying on it — the finding is that
there was no *structural* conflict, not that these exact versions are eternal.

---

## Blocking question set — unanswered, needed before Phase R

All non-secret identifiers. The key contents and any password are never to be
asked for, surfaced, or accepted into context.

1. Is there a live Snowflake account behind `~/.snowflake/r64_engine_key.p8`, or
   is it a leftover from an abandoned setup?
2. If live: **account identifier** (`org-account`), **user**, **role**,
   **warehouse**, **database** — and confirmation that the warehouse may be
   resumed and billed by conformance runs, including the weekly sweep.
3. If there is no live account, is a **trial** acceptable? A trial gives a clean
   teardown story but pins the campaign's re-run window to its expiry — which is
   the exact failure that caused this park, so a trial is now a known-bad answer
   unless the campaign can complete well inside it.
4. Confirm seeded synthetic data only. Nothing real goes to a hosted account.

---

## Phase R preview — trap rows already known to be load-bearing

Not a plan. A head start on the plan, so the resume does not re-derive these.
`DRIVER-PLAN.md` remains unwritten and must be ratified before any code.

- **Row 6 (B-2, session timezone).** Pre-named in the skill: Snowflake defaults
  to `America/Los_Angeles`. Pin `TIMEZONE='UTC'` **on the connection, in
  config**, never in a script. Snowflake adds a second axis the ClickHouse
  campaign never faced: `TIMESTAMP_NTZ` vs `_LTZ` vs `_TZ` must be decided
  per column, and `_LTZ` in particular renders differently per session — the
  precise shape of defect the B-2 assertion exists to catch, since aggregate
  parity is blind to a uniform shift.
- **Row 5 (Decimal) / Row 4 (int64 ceiling).** Snowflake's default `NUMBER(38,0)`
  **exceeds int64**. This is a live RF-001-class refusal case, not a
  hypothetical: the coercion table is expected to RAISE on `NUMBER` with
  precision > 18 rather than narrow. Name the exact-integer authority for any
  float quantity before Gate B.
- **Row 8 (scan-order determinism).** Snowflake micro-partitions give no
  bare-`SELECT` order guarantee — same class as the ClickHouse finding that a
  bare scan returned identical rows in a different order and permuted the
  dictionary encoding. Expect a pinned `ORDER BY` in the target's inline SQL,
  verified by checksum.
- **Rows 10/11 (sandbox, teardown).** Dominated entirely by the blocking
  questions above. This is the row the park is really about.

### Open design question carried forward

`factory/probes.py` needs a Snowflake entry, and **the probe must not use
`snowflake-connector-python`** — the driver under test uses it, and a probe
sharing the client would let a single defect satisfy both sides of the B-2
comparison. The file says so directly: `clickhouse_connect` is deliberately not
imported by the ClickHouse probe.

Candidate: Snowflake's SQL REST API over `urllib` with JWT keypair auth. This is
materially more work than ClickHouse's raw-HTTP probe and should be costed in
`DRIVER-PLAN.md` as a named risk, not discovered at Gate C.

---

## Riding item completed at park — sweep auto-commit

Closed in the same session, independent of the campaign: the sweep now commits
its own evidence packs when green (`--auto-commit`, passed by
`r64-factory-conformance.service`). Failures never auto-commit. This closes the
evidence-cadence ledger item, and it is what stops the audit's §1 finding — four
weeks of green packs sitting dirty and uncommitted, indistinguishable from a
local experiment — from recurring.

Not exercised end to end against a live sweep; the mechanism is covered by unit
tests against a real throwaway git repo, and the next timer firing is the
end-to-end proof.
