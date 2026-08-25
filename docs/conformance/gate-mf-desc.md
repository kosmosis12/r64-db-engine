# Gate MF-DESC — Connector Descriptor

**Landed:** 2026-08-25 · **Branch:** `feat/connector-descriptor` · **Suite:** 906 passed / 66 skipped (baseline 872/66)

Sequenced deliberately **before the next driver admission**. Every driver added
ahead of this gate is one more hand-authored cockpit chip and one more block of
SKILL prose to retrofit; landing it first means the next connector generates
its chip, its status card and its documentation from the same declaration that
registers it.

## What was proven

A driver's identity is declared **once**, in `Driver.descriptor()`, and the
registry, the cockpit roster, the FORGE-VIEW status projection and the
per-connector documentation are all *derived* from that declaration. Three
drift classes are retired by construction rather than by discipline:

| retired | was | now |
|---|---|---|
| ROSTER duplication | hardcoded chip list in `gui/sources.py`, flipped by hand | `factory/artifacts/connector-roster.json`, generated |
| SKILL-prose staleness | per-source prose maintained beside the code | `docs/connectors/*.md`, generated, banner-stamped |
| FV-1 hand-authoring | per-source facts hand-assembled into `factory-status.json` | derived from `descriptor()` |

It also discharges the standing **D-2/a lazy-registry precondition**: reading
every driver's declaration, and validating a config's dialect string, now
import zero database clients.

## The checks

Ten, in `tests/factory/test_gate_mf_desc.py`. Every check is paired with a
`_fixture_is_red` that builds the violation and asserts the check catches it —
a check nobody has seen go red is indistinguishable from one that cannot.

| # | check | red fixture |
|---|---|---|
| 1 | core imports no concrete driver; the registry import is function-scoped; the indirection is actually used | a concrete-driver import in a fake `core/` |
| 2 | a full descriptor sweep imports no heavy dep and no driver module (clean subprocess) | a real client import, observed in `sys.modules` |
| 3 | the modules this brief added to core name zero dialects | a dialect name in a fixture module |
| 4 | every registered driver returns a valid `DriverMetadata`; key, `dialect_name()` and `descriptor().dialect` agree; `extras_package` names a real pyproject extra | a missing descriptor and a key/dialect mismatch |
| 5 | `required_env_keys` are NAMES; no secret shape reaches an artifact; live sentinel VALUES never do | value-shaped keys refused at construction *and* at emit |
| 6 | no `operator_message` interpolates a value; `matches()` returns only a bool | four interpolation forms, each refused |
| 7 | the generator is byte-identical across runs; no wall clock reaches an artifact | unsorted iteration produces a diff, sorted does not |
| 8 | the roster projection is inert data — no module path, no live endpoint | a registry import smuggled into a projection |
| 9 | **a declared-but-unproven driver is never green**; the verdict join cannot see the descriptor | `passing` IS reachable — but only with evidence |
| 10 | every generated doc carries the banner; regeneration is idempotent; `--check` agrees | a hand-edited generated doc is detected |

Check 9 is the load-bearing one. `conformance_state()` takes a dialect string,
not a `DriverMetadata`, so there is nothing about a declaration it *could*
consult — the anti-proxy guard is in the signature rather than in a convention.
Its red fixture proves the green is reachable, because without that arm the
check would be satisfied by a generator that returned `pending` unconditionally
and told the operator nothing.

## Three states, never two

FV-2 could-not-observe discipline. The tree exercises all three right now with
no fixture required:

| dialect | state | why |
|---|---|---|
| `rest` | `passing` | last-green pack 2026-08-23, 9/0/1 |
| `clickhouse` | `drifted` | last green 2026-08-17, `REPAIR-BRIEF-clickhouse-20260823.md` open |
| `postgres` | `pending` | **no committed evidence pack** — see finding below |

`declared, pending conformance` is never rendered green and never rendered
blank.

## Findings

**MF-DESC/1 — the canonical firewall grep cannot fail.** The check this factory
has been running,

```
grep -rn "import.*drivers" src/r64_db_engine/core/
```

needs the literal `import` to appear *before* the literal `drivers` on the
line. In `from r64_db_engine.drivers.postgres.driver import PostgresDriver`,
`drivers` comes first — no match. It also misses
`from r64_db_engine.drivers import DRIVERS`, which core does twice today. It
prints `HOLDS` in every case. The only form it catches, `import
r64_db_engine.drivers`, is the one nobody writes. Surfaced by check 1's red
fixture. The enforcing check is now the AST walk; the grep is kept only as
documentation of what it actually covered, and the fixture asserts it stays
blind so that a future fix is noticed rather than silently absorbed.

**MF-DESC/2 — the reference driver has no committed evidence pack.** `postgres`
renders `pending` because `factory/evidence/last-green/` contains packs for
`clickhouse` and `rest` only. This is not a generator defect; it is the
generator reporting something true that nothing previously surfaced. The
reference-grade driver — the one every other connector is measured against — is
the one with no last-green pack on disk.

**MF-DESC/3 — check 3 could not be run in its specified form.** Core genuinely
still names two dialects, in `_TYPED_BLOCKS` and the typed `postgres:` /
`clickhouse:` config models. That is a pre-existing PG-010 residue `config.py`
documents and scopes for later removal, not something this brief introduced.
The check is scoped to what this brief owns.

**MF-DESC/4 — the descriptor cannot express a connection profile.** `supabase`
is a `ConnectionProfile` over the `postgres` driver, not a driver, so it has no
registry entry and therefore no chip. Profiles are a second identity axis the
roster does not model. Candidate widening for the next driver.

## Reproduce

```bash
pytest tests/factory/test_gate_mf_desc.py -v
python -m factory.generate_descriptor_artifacts --check
```
