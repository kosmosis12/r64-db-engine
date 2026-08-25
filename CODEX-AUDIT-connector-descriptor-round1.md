# Codex audit — `feat/connector-descriptor`, round 1

Audit base: `2d21343b73623387f1efbf67bcab1b3a8e242f3c`.

This file is the memory substrate for executed probes. Final ranking remains in
the audit handoff after the complete suite has run.

## Confirmed block reproducers

- `test_forged_last_green_pack_cannot_create_a_passing_state`: a JSON file whose
  only field is `"verdict": "PASS"` produces `conformance-passing`. No checksum,
  tally, source identity, commit, `ratifies_head`, battery results, or oracle
  attestation is validated by the verdict join.
- `test_environment_value_cannot_reach_any_generated_projection`: an uppercase
  alphanumeric environment value passes the env-key grammar and is copied into
  every descriptor projection.
- `test_provider_secret_cannot_reach_artifacts_through_operator_message`: a
  secret already present in static operator prose passes the interpolation check
  and is copied into generated status/docs.

## Confirmed note reproducer

- `test_generator_is_deterministic_when_registry_mapping_order_changes`:
  reversing the descriptor mapping changes `connector-roster.json` and the
  generated connector index. The JSON status projection remains identical only
  because its source map is serialized with `sort_keys=True`.
- `test_descriptor_cannot_claim_an_unrelated_install_extra`: validation accepts
  `postgres` claiming the existing but unrelated `metrics` extra and generates
  a false installation instruction. It verifies extra-name existence, not that
  the connector dependency belongs to that extra.

## Firewall inventory

- `src/r64_db_engine/core/config.py:134` — function-scoped
  `from r64_db_engine.drivers import DRIVERS`; inherited from `17b059cd`.
- `src/r64_db_engine/core/daemon.py:440` — function-scoped
  `from r64_db_engine.drivers import resolve`; inherited from the initial tree.

No concrete driver package import exists in `core/`, and the audited diff changes
neither inventory site.

## Final disposition

| Priority | Finding | Scope | Bar |
|---|---|---|---|
| P0 | A forged `last-green` JSON file creates `passing` without oracle validation | in diff | BLOCK (d) |
| P0 | Environment/credential values can be copied through env-key or prose fields into generated artifacts | in diff | BLOCK (c) |
| P1 | Shuffled descriptor mapping order changes the roster and connector index bytes | in diff | NOTE |
| P2 | `extras_package` validates name existence but not connector/dependency ownership | in diff | NOTE |
| P2 | Two function-scoped `core/` registry imports remain | inherited PG-010 | NOTE, forward-file |

Verdict: **BLOCK**.

The fix pass contract is that all five strict xfails in
`tests/audit/test_connector_descriptor_round1.py` convert to PASS while the
existing suite remains at least 906 passing / 66 skipped. In particular, green
must require validated oracle provenance rather than a PASS-shaped file, the
emit boundary must reject any live environment/credential value across every
emitted descriptor field, and ordering must be canonicalized inside the
generator rather than relying on the current registry implementation.

## Passing probes and replay axes

- Focused gate: 57 passed / 3 integration skips.
- Full suite before the last two audit-only xfails: 906 passed / 66 skipped / 3
  strict xfails. The final focused audit file reports five strict xfails.
- Cold descriptor sweep: fresh interpreter, `PYTHONNOUSERSITE=1`, empty prior
  import state; no database client or driver module imported.
- YAML config load: fresh interpreter and empty prior import state; no database
  client or driver module imported.
- Generator path replay: repo-root and `/tmp` working directories; `--check`
  succeeded in both. Interpreter and filesystem writability were unchanged.
- Existing-file regeneration: two writes were byte-identical, retained all
  generated banners, and left the worktree clean.
- Real state tree: `rest=passing`, `clickhouse=drifted`, `postgres=pending`,
  computed from the current evidence/brief tree.
- Error-map adversary: matching provider text containing a secret returned only
  `bool`, and a fixed clean operator message did not echo the match.
- Partial-output honesty: the gate test explicitly records that meshroad is not
  checked out and that only the emitting half is asserted; no cockpit claim was
  counted as observed.
