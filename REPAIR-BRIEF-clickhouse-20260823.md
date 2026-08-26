# REPAIR BRIEF — clickhouse — 2026-08-23

Auto-instantiated by `factory-conformance-sweep`. Status: **OPEN**.
Target: `factory/targets/clickhouse-meshbench.yaml` · Table: `perf_1m`

## 1. Symptom

| field | value |
|---|---|
| Failing check(s) | (run errored) |
| This run | n/a |
| Last green run | 2026-08-17T15:54:00Z |
| Run error | the battery did not complete within 1800s and was killed. An unreachable source is the usual cause — a client retrying against a dead endpoint does not fail fast. This is a RED sweep, never a skip. |

> The battery did not complete. An environmental prerequisite (source down, container stopped, network unreachable) is a RED sweep and never a skip — the week the sweep quietly checks nothing is the week it was needed.

## 2. Evidence-pack diff vs last green

| check | last green | now |
|---|---|---|
| `aggregate_parity` | PASS | (absent)  **<-- moved** |
| `b2_boundary` | PASS | (absent)  **<-- moved** |
| `block_structure` | PASS | (absent)  **<-- moved** |
| `checksum` | PASS | (absent)  **<-- moved** |
| `pg011_refusal` | PASS | (absent)  **<-- moved** |
| `recipe_security_invariants` | SKIPPED | (absent)  **<-- moved** |
| `registry_admission` | PASS | (absent)  **<-- moved** |
| `rf002_null_discriminator` | PASS | (absent)  **<-- moved** |
| `schema_exactness` | PASS | (absent)  **<-- moved** |
| `zero_copy_serve_gate` | PASS | (absent)  **<-- moved** |

Environment delta — this is where *the source changed* and *we changed* separate:

| | last green | now |
|---|---|---|
| python | 3.13.12 | ? |
| platform | Linux-7.1.5-1-cachyos-x86_64-with-glibc2.44 | ? |
| pyarrow | 25.0.0 | ? |
| pandas | 3.0.5 | ? |
| pydantic | 2.13.4 | ? |
| clickhouse_connect | 1.6.0 | ? |
| httpx | 0.28.1 | ? |
| jsonschema | 4.26.0 | ? |
| container image | clickhouse/clickhouse-server:latest | ? |
| container digest | sha256:07afc18d8a9706eb9d85c5c5d2752e5270f91bbc2894caeaecb73e4d0f603bf5 | ? |
| git commit | 83391564824fd2ab5de487755fa355a9fe238f34 | ? |
| git branch | main | ? |

> `pyarrow` owns the Arrow IPC block layout and `pandas` decides `string` vs `large_string`. If either moved, suspect environment drift before source drift — and **pin**, do not widen the check.

## 3. Re-research directive

Law 1 — the fix is re-research at BUILD time, not a runtime adaptation.

- [ ] Re-read the provider's current documentation for the affected surface.
- [ ] Probe the live source directly, WITHOUT the driver. Verifying the driver with the driver hides faults in both directions at once.
- [ ] Re-fill the affected `DRIVER-PLAN.md` rows, especially the trap rows:
      int32/int64 ceiling · Decimal · timestamp + session timezone · null vs NaN · scan-order determinism.
- [ ] State what CHANGED at the source, in one sentence, with evidence.

## 4. Re-admission requirement

A repaired driver re-enters through the **full** battery — not the failed check alone. A source change that moved one property has usually moved others, and a targeted re-run would confirm only what you already suspected.

- [ ] `DRIVER-PLAN.md` rows updated and **ratified by Kos** before any code.
- [ ] Fix authored; zero core edits (`git grep -rniE "\bclickhouse\b" src/r64_db_engine/core/` empty).
- [ ] If the battery could not have caught this earlier: **extend the battery first** (Law 4), with a failing fixture proving the new check can fail. Never widen a tolerance to make a run green.
- [ ] `.venv/bin/python -m factory.conformance --dialect clickhouse --config factory/targets/clickhouse-meshbench.yaml --table perf_1m --serve-gate` → exit 0.
- [ ] New evidence pack committed; suite green via `.venv/bin/pytest`.
- [ ] Cross-agent QA before merge. Builder ≠ auditor.

## 5. Disposition

- [ ] Repaired and re-admitted — closing pack: `________`
- [ ] Ground truth legitimately changed — new capture committed, with the reason recorded. **NEVER edit ground truth to match a failing pipeline**: that converts an oracle into a mirror and every later run passes by construction.
- [ ] Accepted as a known limit — fenced, with scope stated.
