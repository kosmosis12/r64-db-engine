---
name: r64-factory-maintenance
description: >
  The "maintains" half of the r64-db-engine factory — drift detection and
  repair. Weekly systemd-timed conformance re-runs per registered dialect
  against live sources via factory/bin/factory-conformance-sweep, plus
  recipe-lane per-pull response-schema validation. Any failure triggers ntfy
  and an auto-instantiated REPAIR-BRIEF-<dialect>-<YYYYMMDD>.md from the
  embedded template: symptom, evidence-pack diff against the last green pack,
  a re-research directive, and a re-admission requirement through the full
  battery. Use when a connector breaks, a provider changes an API or schema, a
  scheduled conformance run fails, or when installing/troubleshooting the sweep
  timer. Trigger on: driver drift, conformance failed, repair brief, connector
  broke, provider changed, schema drift, sweep failed, factory timer,
  r64-factory-conformance.service, ntfy-fail, drift log, re-admit the driver.
  Composes with r64-conformance (the battery it re-runs),
  r64-connector-factory (the campaign a repair brief re-enters), and meshforge.
---

# r64-factory-maintenance

Repo: `/home/kos/builds/r64-db-engine`.

A factory that only builds is half a factory. Integrations rot because sources
change underneath them, and the failure is usually **silent** — a column that
became nullable, a timestamp that changed zone, an enum that gained a value.
This lane exists so that the first person to notice is a timer, not a customer.

**Drift triggers re-research, never runtime interpretation (Law 1).** The
response to a broken connector is a repair brief and a rebuild, never an engine
that adapts to what it now sees.

---

## The sweep

```bash
factory/bin/factory-conformance-sweep          # all targets
factory/bin/factory-conformance-sweep --target clickhouse-meshbench
```

Iterates `factory/targets/*.yaml`, runs the battery per target with
`--evidence-dir`, aggregates verdicts, and **exits non-zero on any failure**.
On failure it writes `REPAIR-BRIEF-<dialect>-<YYYYMMDD>.md` from the template
below. It also validates each recipe book's drift log and surfaces any
accumulated repair events.

## Timer

Units in `factory/systemd/`. Install commands are in the campaign close-out —
**you do not sudo**; hand Kos copy-pasteable commands.

- `r64-factory-conformance.service` — oneshot, `User=kos`,
  `WorkingDirectory=` the repo, `ExecStart=` the sweep via the venv python,
  `OnFailure=ntfy-fail@%n.service`.
- `r64-factory-conformance.timer` — weekly, `Persistent=true`,
  `RandomizedDelaySec=300`.

> **The timer must NOT fire during a bench window.** A conformance sweep pulls
> a million rows and spins a Flight server; a bench series measuring an idle
> machine would report that as a lane effect. Stopping this timer belongs on
> Kos's root-quiesce checklist:
> `sudo systemctl stop r64-factory-conformance.timer` before a series,
> `start` after.

## Triage when it fires

1. **Read the evidence pack, not the log.** `factory/evidence/EVIDENCE-<dialect>-<date>.md`,
   verdict line first, then the failed check's table — both sides are recorded.
2. **Diff against the last green pack.** The `.json` files are diffable and the
   environment block is included, which is what separates "the source changed"
   from "our environment changed".
3. **Classify:**
   - *Source drift* — schema, semantics, or bounds moved at the source →
     repair brief, re-research, re-admit.
   - *Environment drift* — a package version moved (`pyarrow` owns the IPC
     block layout; `pandas` decides `string` vs `large_string`) → pin, do not
     widen the check.
   - *Real regression* — our code broke → fix, and add the check that would
     have caught it earlier.
   - *Battery gap* — the failure is real but the check is imprecise → extend
     the battery (Law 4). **Never widen a tolerance to make a run green.**
4. **Environmental prerequisites are FAILURES, not skips.** A stopped container
   or an unreachable source is a red sweep. Silently skipping a target whose
   source is down turns the timer into decoration.

---

## `REPAIR-BRIEF-<dialect>-<YYYYMMDD>.md` template

````markdown
# REPAIR BRIEF — <dialect> — <YYYY-MM-DD>

Auto-instantiated by `factory-conformance-sweep`. Status: OPEN.
Target: `factory/targets/<target>.yaml` · Table: `<table>`

## 1. Symptom

| field | value |
|---|---|
| Failing check(s) | `<name>` … |
| First failing run | `factory/evidence/EVIDENCE-<dialect>-<date>.json` |
| Last green run | `factory/evidence/EVIDENCE-<dialect>-<date>.json` |
| Verdict | n passed / n failed / n skipped |

Failure detail, verbatim from the pack:

> <detail line>

## 2. Evidence-pack diff vs last green

| comparison | last green | now | moved? |
|---|---|---|---|
| | | | |

Environment delta (this is where "the source changed" and "we changed" separate):

| | last green | now |
|---|---|---|
| python | | |
| pyarrow | | |
| pandas | | |
| container image / digest | | |
| git commit | | |

## 3. Re-research directive

Law 1 — the fix is re-research at build time, NOT a runtime adaptation.

- [ ] Re-read the provider's current documentation for the affected surface.
- [ ] Probe the live source directly, WITHOUT the driver (an independent probe;
      verifying the driver with the driver hides faults in both directions).
- [ ] Re-fill the affected `DRIVER-PLAN.md` rows, especially the trap rows:
      int32/int64 ceiling · Decimal · timestamp + session timezone ·
      null vs NaN · scan-order determinism.
- [ ] State what CHANGED at the source, in one sentence, with evidence.

## 4. Re-admission requirement

A repaired driver re-enters through the **full** battery. Not the failed check
alone — a source change that moved one property has usually moved others, and a
targeted re-run would confirm only what you already suspected.

- [ ] `DRIVER-PLAN.md` rows updated and **ratified by Kos** before code.
- [ ] Fix authored; zero core edits
      (`git grep -rniE "\b<dialect>\b" src/r64_db_engine/core/` empty).
- [ ] If the battery could not have caught this earlier: **extend the battery
      first**, with a failing fixture proving the new check can fail.
- [ ] `.venv/bin/python -m factory.conformance … --serve-gate` → exit 0.
- [ ] New evidence pack committed; suite green via `.venv/bin/pytest`.
- [ ] Cross-agent QA before merge. Builder ≠ auditor.

## 5. Disposition

- [ ] Repaired and re-admitted — closing pack: `<path>`
- [ ] Ground truth legitimately changed — new capture committed, with the
      reason recorded (NEVER edit ground truth to match a failing pipeline;
      that converts an oracle into a mirror)
- [ ] Accepted as a known limit — fenced, with scope stated
````

---

## Recipe-lane drift

Recipe books validate their `response_schema` on **every** pull. A failure
writes a structured repair event to `factory/evidence/drift/<source>-<ts>.json`,
fires ntfy, and exits non-zero. The sweep surfaces accumulated events even when
the battery itself is green — a recipe that has been quietly failing validation
between sweeps is exactly the case that must not stay quiet.

There is no auto-retry-with-reinterpretation, by design.
