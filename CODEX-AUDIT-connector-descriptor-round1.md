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
