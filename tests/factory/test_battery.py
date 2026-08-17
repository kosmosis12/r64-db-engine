"""Proof that the oracle fails when and only when a specific mechanism breaks.

# What Codex round 2 found wrong with round 1

Round 1 asserted a triple per case — FAIL, check name, reason code — and that
was a real improvement. But **the harness handed the stub the answer key**: the
case knew which check it targeted and passed that in. An adaptive stub given
case metadata can echo it back and pass without ever looking at an input. The
suite was measuring the stub's access to metadata, not the battery's behaviour.

# What this file does now

**Isolation.** An oracle — real or stubbed — receives exactly what the real
battery receives: a scenario's inputs, through `scenarios.run_battery`. It never
sees the case id, the target check, the expected code, or the manifest.

**Full verdict vector.** A fixture is a COMPLETE scenario with one thing broken,
and its manifest declares the verdict of all ten checks. Nine PASSes are a claim
that the mutation was surgical; a catch-all stub fails on those nine.

**Anonymized replay.** Meta-replays run against content-addressed identifiers
(`c_9f2a…`, `t_5b31…`), so name pattern-matching is unavailable. The real
battery must produce the SAME vector on the anonymized copy — which independently
proves it reaches verdicts structurally rather than by recognising meshbench.

**Three adversaries, and two fenced controls.** Unconditional, adaptive-echo and
name-pattern stubs must all fail. The echo stub is additionally shown to PASS
when deliberately leaked the manifest, and the name matcher is shown to behave
differently on real names — those controls are what prove the isolation and the
anonymization are load-bearing rather than incidental.

**Registry-derived guards.** Coverage is checked against `battery.CHECK_NAMES`
and codes against `battery.MECHANISMS`. There is no duplicated list here; a
check added without a negative case turns this file red.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, NamedTuple

import pytest

from factory import battery
from factory.battery import FAIL, PASS, SKIPPED, CheckResult
from tests.factory import scenarios
from tests.factory.scenarios import (
    Names,
    Scenario,
    all_pass,
    make_scenario,
    run_battery,
    vector_with,
    verdict_vector,
)


class _D:
    def __init__(self, n: str) -> None:
        self._n = n

    def dialect_name(self) -> str:
        return self._n


# ---------------------------------------------------------------------------
# Mutations — each breaks exactly one thing in a healthy scenario
# ---------------------------------------------------------------------------


def _permissive_registry(s: Scenario) -> Scenario:
    return replace(s, resolve_fn=lambda d: _D(d))


def _terse_refusal(s: Scenario) -> Scenario:
    registered = list(s.registered)

    def resolve(d):
        if d not in registered:
            raise ValueError("unknown dialect")
        return _D(d)
    return replace(s, resolve_fn=resolve)


def _mislabeled_registry(s: Scenario) -> Scenario:
    registered = list(s.registered)
    other = s.names.other_dialect

    def resolve(d):
        if d not in registered:
            raise ValueError(f"unknown dialect '{d}' (available: {', '.join(registered)})")
        return _D(other)
    return replace(s, resolve_fn=resolve)


def _unresolvable(s: Scenario) -> Scenario:
    def resolve(d):
        raise ValueError("driver import failed")
    return replace(s, resolve_fn=resolve)


def _type_drift_int(s: Scenario) -> Scenario:
    return replace(s, observed_schema={**s.observed_schema, s.names.int_col: "int32"})


def _type_drift_timestamp(s: Scenario) -> Scenario:
    return replace(s, observed_schema={**s.observed_schema, s.names.time_col: "timestamp[ns]"})


def _reorder_columns(s: Scenario) -> Scenario:
    items = list(s.observed_schema.items())
    return replace(s, observed_schema=dict([items[1], items[0], *items[2:]]))


def _drop_column(s: Scenario) -> Scenario:
    schema = dict(s.observed_schema)
    del schema[s.names.str_col]
    return replace(s, observed_schema=schema)


def _empty_schema_spec(s: Scenario) -> Scenario:
    return replace(s, spec_columns=[])


def _string_width_without_the_fence(s: Scenario) -> Scenario:
    return replace(
        s,
        observed_schema={**s.observed_schema, s.names.str_col: "string"},
        string_width_tolerant=False,
    )


def _aggregate_mismatch(s: Scenario) -> Scenario:
    computed = dict(s.computed)
    computed[f"sum_{s.names.sum_col}"] += 7
    return replace(s, computed=computed)


def _aggregate_missing_key(s: Scenario) -> Scenario:
    return replace(
        s,
        aggregate_specs=[*s.aggregate_specs, {"ground_truth_key": "not_in_truth", "op": "row_count"}],
        computed={**s.computed, "not_in_truth": 1},
    )


def _empty_aggregate_spec(s: Scenario) -> Scenario:
    return replace(s, aggregate_specs=[])


def _nulls_filled(s: Scenario) -> Scenario:
    return replace(s, null_counts={s.names.null_col: 0})


def _null_count_off_by_one(s: Scenario) -> Scenario:
    return replace(s, null_counts={s.names.null_col: 20_038})


def _nan_as_value(s: Scenario) -> Scenario:
    return replace(s, nan_counts={s.names.null_col: 12})


def _spec_truth_disagree(s: Scenario) -> Scenario:
    """Moves the ground truth, which legitimately breaks TWO checks.

    Declared in the manifest rather than hidden: aggregate_parity gates on the
    same number, so this mutation has a wider blast radius than the RF-002
    cross-record check alone, and the fixture has to admit it.
    """
    truth = dict(s.ground_truth)
    truth[f"count_{s.names.null_col}_null"] = 20_040
    return replace(s, ground_truth=truth)


def _no_rf_declaration(s: Scenario) -> Scenario:
    return replace(s, rf_spec={})


def _empty_rf_without_reason(s: Scenario) -> Scenario:
    return replace(s, rf_spec={"discriminators": []})


def _rf_table_not_declared(s: Scenario) -> Scenario:
    return replace(s, rf_spec={"discriminators": [
        {"column": s.names.null_col, "expected_null_count": {"some_other_table": 1}}]})


def _shift_bounds(s: Scenario) -> Scenario:
    return replace(
        s,
        artifact_bounds={
            s.names.time_col: ("2025-12-31 16:00:15.184566", "2026-06-29 16:59:30.942340")
        },
        source_timezone="America/Los_Angeles",
    )


def _no_boundary_columns(s: Scenario) -> Scenario:
    return replace(s, boundary_columns=[])


def _source_not_probed(s: Scenario) -> Scenario:
    return replace(s, source_bounds={})


def _incremental_accepted(s: Scenario) -> Scenario:
    return replace(s, build_fn=lambda: object())


def _wrong_refusal(s: Scenario) -> Scenario:
    def build():
        raise RuntimeError("connection refused")
    return replace(s, build_fn=build)


def _single_block(s: Scenario) -> Scenario:
    return replace(s, observed_blocks=[1_000_000])


def _uneven_blocks(s: Scenario) -> Scenario:
    return replace(s, observed_blocks=[62_500] * 16)


def _blocks_missing_rows(s: Scenario) -> Scenario:
    return replace(s, observed_blocks=[65536] * 15)


def _bytes_differ_only(s: Scenario) -> Scenario:
    return replace(s, second_sha="ffffffff" * 8)


def _checksum_data_layer(s: Scenario) -> Scenario:
    return replace(
        s,
        second_sha="ffffffff" * 8,
        checksum_layers={**s.checksum_layers, "second_rows": 999_999},
    )


def _copied_cold(s: Scenario) -> Scenario:
    return replace(s, serve_cold={**(s.serve_cold or {}), "copied_columns": 1})


def _copied_warm(s: Scenario) -> Scenario:
    return replace(s, serve_warm={**(s.serve_warm or {}), "copied_columns": 4})


def _warm_missed(s: Scenario) -> Scenario:
    return replace(s, serve_warm={**(s.serve_warm or {}), "cache_hits": 16, "cache_misses": 16})


def _warm_decoded(s: Scenario) -> Scenario:
    return replace(s, serve_warm={**(s.serve_warm or {}), "columns_decoded": 32})


def _cold_decoded_nothing(s: Scenario) -> Scenario:
    return replace(s, serve_cold={**(s.serve_cold or {}), "columns_decoded": 0,
                                  "cache_misses": 0, "zero_copy_columns": 0})


def _mutation_accepted(s: Scenario) -> Scenario:
    return replace(s, recipe_outcomes=[("https->http downgrade", False, "accepted")])


def _no_mutations_attempted(s: Scenario) -> Scenario:
    return replace(s, recipe_outcomes=[])


# ---------------------------------------------------------------------------
# The case table — manifests declare the FULL vector
# ---------------------------------------------------------------------------


class Case(NamedTuple):
    id: str
    mutate: Any                       # Scenario -> Scenario
    manifest: dict[str, tuple[str, str]]
    why: str

    @property
    def target(self) -> tuple[str, str]:
        """(check, code) of the check this case is written to break."""
        failing = [(k, v[1]) for k, v in self.manifest.items() if v[0] == FAIL]
        assert failing, f"{self.id}: a negative case must declare at least one FAIL"
        return failing[0]


C = vector_with

NEGATIVE_CASES: list[Case] = [
    Case("registry: an unregistered dialect is accepted", _permissive_registry,
         C("registry_admission", "registry.unregistered_accepted"),
         "the PG-010 regression: anything resolves, so nothing is really registered"),
    Case("registry: the refusal does not list the registry", _terse_refusal,
         C("registry_admission", "registry.refusal_omits_registry"),
         "a raise alone is not enough; 'unknown dialect' sends the operator to the source"),
    Case("registry: filed under the wrong key", _mislabeled_registry,
         C("registry_admission", "registry.dialect_name_mismatch"),
         "resolves fine, returns the wrong driver"),
    Case("registry: the configured dialect does not resolve", _unresolvable,
         C("registry_admission", "registry.dialect_unresolvable"),
         "the registry cannot produce the driver the config names"),

    Case("schema: int32 where int64 was promised", _type_drift_int,
         C("schema_exactness", "schema.type_mismatch"),
         "the RF-001 class of silent truncation"),
    Case("schema: timestamp[ns] where [us] was promised", _type_drift_timestamp,
         C("schema_exactness", "schema.type_mismatch"),
         "the cast B-4 exists about — same MECHANISM as the int drift, legally sharing a code"),
    Case("schema: columns reordered", _reorder_columns,
         C("schema_exactness", "schema.column_set_or_order"),
         "a different artifact to any positional reader"),
    Case("schema: a column is missing", _drop_column,
         C("schema_exactness", "schema.column_count"),
         "fewer columns than the spec declares"),
    Case("schema: the spec itself is empty", _empty_schema_spec,
         C("schema_exactness", "schema.empty_spec"),
         "a spec with no columns must not silently gate nothing"),
    Case("schema: string width differs with the B-3 fence OFF",
         _string_width_without_the_fence,
         C("schema_exactness", "schema.type_mismatch"),
         "turning the fence off must actually tighten the check"),

    Case("aggregates: a gating aggregate disagrees", _aggregate_mismatch,
         C("aggregate_parity", "aggregate.mismatch"),
         "source-captured truth and the pipeline disagree"),
    Case("aggregates: the spec names a key ground truth lacks", _aggregate_missing_key,
         C("aggregate_parity", "aggregate.missing_ground_truth_key"),
         "a spec and a ground-truth file that have drifted apart"),
    Case("aggregates: the aggregate set is empty", _empty_aggregate_spec,
         C("aggregate_parity", "aggregate.empty_spec"),
         "parity cannot be gated on an empty set"),

    Case("rf002: nulls were filled in transit", _nulls_filled,
         C("rf002_null_discriminator", "rf002.null_count_mismatch"),
         "the zero-fill failure: every mean() wrong while totals look plausible"),
    Case("rf002: the null count is merely close", _null_count_off_by_one,
         C("rf002_null_discriminator", "rf002.null_count_mismatch"),
         "exact counts — one lost null is one lost null; same mechanism as above"),
    Case("rf002: NaN smuggled in as a value", _nan_as_value,
         C("rf002_null_discriminator", "rf002.nan_as_value"),
         "null_count looks right while every downstream sum() is poisoned"),
    Case("rf002: spec and ground truth disagree", _spec_truth_disagree,
         C("rf002_null_discriminator", "rf002.spec_ground_truth_disagree",
           aggregate_parity=(FAIL, "aggregate.mismatch")),
         "moving ground truth breaks TWO checks — the manifest admits the blast radius"),
    Case("rf002: no discriminators key at all", _no_rf_declaration,
         C("rf002_null_discriminator", "rf002.no_declaration"),
         "'nobody thought about it' must not render as 'nothing to check'"),
    Case("rf002: empty declaration with no stated reason", _empty_rf_without_reason,
         C("rf002_null_discriminator", "rf002.empty_without_reason"),
         "a silent zero is not a skip"),
    Case("rf002: the declaration omits this table", _rf_table_not_declared,
         C("rf002_null_discriminator", "rf002.table_not_declared"),
         "declared for a different table is not declared for this one"),

    Case("b2: the eight-hour timezone shift", _shift_bounds,
         C("b2_boundary", "b2.bounds_diverged"),
         "every aggregate check passes through this untouched"),
    Case("b2: no boundary column declared", _no_boundary_columns,
         C("b2_boundary", "b2.no_boundary_columns"),
         "declaring none must not be a free pass"),
    Case("b2: the live source was never probed", _source_not_probed,
         C("b2_boundary", "b2.source_not_probed"),
         "a one-sided comparison is not a comparison"),

    Case("pg011: incremental was accepted", _incremental_accepted,
         C("pg011_refusal", "pg011.accepted"),
         "the silent-downgrade failure"),
    Case("pg011: refused for an unrelated reason", _wrong_refusal,
         C("pg011_refusal", "pg011.wrong_error"),
         "a bare `raises` would pass this"),

    Case("blocks: a single-block file", _single_block,
         C("block_structure", "blocks.count_mismatch"),
         "collapses the consumer's per-block cache granularity"),
    Case("blocks: right count, wrong granularity", _uneven_blocks,
         C("block_structure", "blocks.layout_mismatch"),
         "a block-COUNT assertion would pass this"),
    Case("blocks: rows are missing", _blocks_missing_rows,
         C("block_structure", "blocks.count_mismatch"),
         "same mechanism as the single-block case"),

    Case("checksum: only the bytes differ", _bytes_differ_only,
         C("checksum", "checksum.bytes_only"),
         "the real ClickHouse case — scan order was the answer"),
    Case("checksum: a data-bearing layer differs", _checksum_data_layer,
         C("checksum", "checksum.data_layer_differs"),
         "a fidelity defect, not a serialization nuance"),

    Case("serve: a column was copied cold", _copied_cold,
         C("zero_copy_serve_gate", "serve.copied_columns_cold"),
         "the zero-copy claim itself"),
    Case("serve: a column was copied warm", _copied_warm,
         C("zero_copy_serve_gate", "serve.copied_columns_warm"),
         "same claim, other pass"),
    Case("serve: the warm pass still missed", _warm_missed,
         C("zero_copy_serve_gate", "serve.warm_miss"),
         "a warm cache that misses is not warm"),
    Case("serve: the warm pass decoded", _warm_decoded,
         C("zero_copy_serve_gate", "serve.warm_decoded"),
         "stronger than miss_rate"),
    Case("serve: the cold pass decoded nothing", _cold_decoded_nothing,
         C("zero_copy_serve_gate", "serve.cold_no_decode"),
         "a workload touching no column passes everything else trivially"),

    Case("recipe security: a mutation was accepted", _mutation_accepted,
         C("recipe_security_invariants", "recipe_security.mutation_accepted"),
         "the fence did not fire on a malicious shape"),
    Case("recipe security: no mutation attempted", _no_mutations_attempted,
         C("recipe_security_invariants", "recipe_security.no_mutations"),
         "a security check that tried nothing must not report success"),
]


def scenario_for(case: Case, names: Names) -> Scenario:
    return case.mutate(make_scenario(names))


# ---------------------------------------------------------------------------
# The real assertions — full vector, on real AND anonymized identifiers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", NEGATIVE_CASES, ids=[c.id for c in NEGATIVE_CASES])
def test_the_battery_reproduces_the_declared_verdict_vector(case: Case) -> None:
    """The whole vector, not just the targeted failure.

    Nine PASSes are a claim that the mutation was surgical. A catch-all oracle
    fails here on those nine even though it "correctly" fails the tenth.
    """
    assert verdict_vector(run_battery(scenario_for(case, scenarios.REAL), battery)) == case.manifest


@pytest.mark.parametrize("case", NEGATIVE_CASES, ids=[c.id for c in NEGATIVE_CASES])
def test_the_vector_is_identical_under_anonymized_identifiers(case: Case) -> None:
    """The battery reaches its verdicts STRUCTURALLY, not by recognising names.

    Every column, table and dialect name is replaced with a content-addressed
    string. If any verdict moved, some check was pattern-matching on `score` or
    `event_time` — and a name-matching oracle would then be indistinguishable
    from a real one on meshbench.
    """
    anon = Names.anonymized(salt=case.id)
    assert verdict_vector(run_battery(scenario_for(case, anon), battery)) == case.manifest


def test_the_healthy_scenario_passes_every_check() -> None:
    assert verdict_vector(run_battery(make_scenario(), battery)) == all_pass()


def test_the_healthy_scenario_also_passes_under_anonymized_identifiers() -> None:
    anon = Names.anonymized(salt="healthy")
    assert verdict_vector(run_battery(make_scenario(anon), battery)) == all_pass()


# ---------------------------------------------------------------------------
# The adversaries — metadata-denied
# ---------------------------------------------------------------------------


class UnconditionalFailOracle:
    """Fails at everything, inspects nothing.

    What a broken-but-plausible battery looks like from outside: every run red,
    nothing verified. Round 1's triple caught it; the full vector catches it
    harder, on the nine checks it wrongly fails.
    """

    def _r(self, name: str) -> CheckResult:
        return CheckResult(name=name, status=FAIL, reason_code="always.fails",
                           detail="fails unconditionally")

    def check_registry_admission(self, *a, **k): return self._r("registry_admission")
    def check_schema_exactness(self, *a, **k): return self._r("schema_exactness")
    def check_aggregate_parity(self, *a, **k): return self._r("aggregate_parity")
    def check_rf002_discriminator(self, *a, **k): return self._r("rf002_null_discriminator")
    def check_b2_boundary(self, *a, **k): return self._r("b2_boundary")
    def check_pg011_refusal(self, *a, **k): return self._r("pg011_refusal")
    def check_block_structure(self, *a, **k): return self._r("block_structure")
    def check_checksum(self, *a, **k): return self._r("checksum")
    def check_recipe_security(self, *a, **k): return self._r("recipe_security_invariants")
    def check_serve_gate(self, *a, **k): return self._r("zero_copy_serve_gate")


class AdaptiveEchoOracle(UnconditionalFailOracle):
    """Would pass IF handed the manifest — and is never handed it.

    This is the adversary round 1 could not have caught, because round 1's
    harness passed the target check into the assertion. Here it can only
    succeed through `receive_leak`, which `run_battery` calls solely in the
    fenced negative control below. Its failure under the real harness IS the
    isolation, demonstrated rather than asserted.
    """

    def __init__(self) -> None:
        self._leak: dict[str, tuple[str, str]] | None = None
        # Counted, not just stored: a harness that called receive_leak(None)
        # would leave `_leak` falsy and look identical to never calling it.
        self.leak_calls = 0

    def receive_leak(self, manifest: dict[str, tuple[str, str]]) -> None:
        self.leak_calls += 1
        self._leak = manifest

    def _r(self, name: str) -> CheckResult:
        if self._leak and name in self._leak:
            status, code = self._leak[name]
            return CheckResult(name=name, status=status, reason_code=code,
                               detail="echoed from leaked metadata")
        return CheckResult(name=name, status=FAIL, reason_code="echo.no_metadata",
                           detail="no metadata was leaked, so nothing to echo")


class NamePatternOracle(UnconditionalFailOracle):
    """Guesses from identifier spelling — useless once names are anonymized.

    Recognises the meshbench vocabulary and produces a verdict from it, which is
    exactly the shortcut anonymization removes.
    """

    MESHBENCH = ("score", "event_time", "perf_1m", "row_id", "region", "quantity")

    def _looks_like_meshbench(self, *args: Any) -> bool:
        blob = repr(args)
        return any(token in blob for token in self.MESHBENCH)

    def check_rf002_discriminator(self, *a, **k):
        if self._looks_like_meshbench(a, k):
            return CheckResult(name="rf002_null_discriminator", status=FAIL,
                               reason_code="rf002.null_count_mismatch", detail="guessed")
        return CheckResult(name="rf002_null_discriminator", status=PASS)

    def check_b2_boundary(self, *a, **k):
        if self._looks_like_meshbench(a, k):
            return CheckResult(name="b2_boundary", status=FAIL,
                               reason_code="b2.bounds_diverged", detail="guessed")
        return CheckResult(name="b2_boundary", status=PASS)

    def _r(self, name: str) -> CheckResult:
        return CheckResult(name=name, status=PASS)


ADVERSARIES = [
    ("unconditional catch-all", UnconditionalFailOracle),
    ("adaptive echo (metadata denied)", AdaptiveEchoOracle),
    ("name pattern matcher", NamePatternOracle),
]


@pytest.mark.parametrize("label,make_oracle", ADVERSARIES, ids=[a[0] for a in ADVERSARIES])
@pytest.mark.parametrize("case", NEGATIVE_CASES, ids=[c.id for c in NEGATIVE_CASES])
def test_no_adversarial_oracle_reproduces_the_vector(
    label: str, make_oracle: Any, case: Case
) -> None:
    """Every adversary must FAIL every case, under anonymized identifiers.

    They receive exactly what the real battery receives — the scenario's
    inputs — and nothing else. No case id, no target check, no manifest.
    """
    anon = Names.anonymized(salt=case.id)
    observed = verdict_vector(run_battery(scenario_for(case, anon), make_oracle()))
    assert observed != case.manifest, (
        f"the {label} oracle reproduced the declared vector for {case.id!r}, "
        f"which means the fixture proves nothing about the real battery"
    )


@pytest.mark.parametrize("case", NEGATIVE_CASES[:6], ids=[c.id for c in NEGATIVE_CASES[:6]])
def test_NEGATIVE_CONTROL_the_echo_oracle_passes_when_the_manifest_is_leaked(case: Case) -> None:
    """FENCED NEGATIVE CONTROL — the test that proves isolation matters.

    Handed the manifest, the echo oracle reproduces the vector exactly while
    inspecting no input whatsoever. That it passes HERE and fails above is the
    whole argument: the only difference between the two is whether the harness
    leaked case metadata.

    If this ever fails, the echo oracle has become too weak to be a meaningful
    adversary, and the isolation claim above is no longer supported by it.
    """
    anon = Names.anonymized(salt=case.id)
    observed = verdict_vector(
        run_battery(scenario_for(case, anon), AdaptiveEchoOracle(), leak=case.manifest)
    )
    assert observed == case.manifest


def test_NEGATIVE_CONTROL_the_name_matcher_depends_on_names() -> None:
    """The name-pattern oracle behaves differently on real vs anonymized names.

    That difference is the signal anonymization removes. Without this control,
    "the name matcher failed" would be equally consistent with it being a stub
    too weak to matter.
    """
    case = next(c for c in NEGATIVE_CASES if c.target[0] == "b2_boundary")
    real = verdict_vector(run_battery(scenario_for(case, scenarios.REAL), NamePatternOracle()))
    anon = verdict_vector(
        run_battery(scenario_for(case, Names.anonymized(salt="x")), NamePatternOracle())
    )
    assert real != anon, "the name matcher is not actually name-sensitive"
    assert real["b2_boundary"] == (FAIL, "b2.bounds_diverged")
    assert anon["b2_boundary"] == (PASS, "")


def test_the_real_battery_is_not_an_adversary() -> None:
    """Sanity floor: the assertions above are not vacuously strict."""
    case = NEGATIVE_CASES[0]
    anon = Names.anonymized(salt=case.id)
    assert verdict_vector(run_battery(scenario_for(case, anon), battery)) == case.manifest


def test_the_harness_never_leaks_metadata_unless_asked() -> None:
    """`run_battery` passes nothing beyond the scenario unless `leak` is given.

    The real battery is a module and cannot implement `receive_leak`, so even a
    leaked call cannot reach it — but the property asserted here is the harness
    side: no leak argument, no leak.
    """
    assert not hasattr(battery, "receive_leak")
    oracle = AdaptiveEchoOracle()
    run_battery(make_scenario(), oracle)
    assert oracle.leak_calls == 0, "the harness offered metadata without being asked to"
    assert oracle._leak is None


# ---------------------------------------------------------------------------
# Guards — derived from the battery's own registries, never duplicated here
# ---------------------------------------------------------------------------


def test_every_check_in_the_battery_registry_has_a_negative_case() -> None:
    """Derived from `battery.CHECK_NAMES`, not from a list kept in this file.

    A duplicated hard-coded set would happily stay green while a new check
    arrived unproven — which is Law 4 read backwards.
    """
    covered = {case.target[0] for case in NEGATIVE_CASES}
    missing = set(battery.CHECK_NAMES) - covered
    assert not missing, f"checks with no negative case: {sorted(missing)}"


def test_the_manifests_cover_exactly_the_registered_checks() -> None:
    for case in NEGATIVE_CASES:
        assert set(case.manifest) == set(battery.CHECK_NAMES), (
            f"{case.id}: manifest does not describe every registered check"
        )


def test_every_declared_reason_code_is_in_the_mechanism_registry() -> None:
    for case in NEGATIVE_CASES:
        for check, (status, code) in case.manifest.items():
            if status == FAIL:
                battery.mechanism_of(check, code)


def test_a_shared_reason_code_means_a_shared_mechanism() -> None:
    """Two cases may share a code ONLY if they are the same mechanism.

    `schema.type_mismatch` is legally shared by the int-drift and
    timestamp-drift cases because both are the schema/type-drift mechanism. A
    code owned by two DIFFERENT mechanisms would mean it no longer pins what
    failed, and the fixture triple would have weakened back toward `assert FAIL`.
    """
    by_code: dict[str, set[str]] = {}
    for (_check, code), mechanism in battery.MECHANISMS.items():
        by_code.setdefault(code, set()).add(mechanism)
    for code, mechanisms in by_code.items():
        assert len(mechanisms) == 1, (
            f"reason code {code!r} is owned by more than one mechanism: {sorted(mechanisms)}"
        )

    seen: dict[str, str] = {}
    for case in NEGATIVE_CASES:
        check, code = case.target
        mechanism = battery.mechanism_of(check, code)
        if code in seen:
            assert seen[code] == mechanism, (
                f"{case.id!r} shares code {code!r} across mechanisms "
                f"{seen[code]!r} and {mechanism!r}"
            )
        seen[code] = mechanism


def test_shared_codes_are_actually_exercised() -> None:
    """The shared-mechanism rule is not vacuous — some code IS shared."""
    codes = [case.target[1] for case in NEGATIVE_CASES]
    assert any(codes.count(c) > 1 for c in codes), "no code is shared; the rule is untested"


# ---------------------------------------------------------------------------
# Mechanism-level units that are not verdicts
# ---------------------------------------------------------------------------


def test_b3_fence_reaches_inside_a_dictionary_type() -> None:
    assert battery.normalize_string_width(
        "dictionary<values=large_string, indices=int32, ordered=0>"
    ) == "dictionary<values=string, indices=int32, ordered=0>"


@pytest.mark.parametrize("other", ["large_binary", "large_list<item: int64>"])
def test_b3_fence_does_not_touch_other_large_types(other: str) -> None:
    assert battery.normalize_string_width(other) == other


def test_block_layout_for_one_million_rows() -> None:
    layout = battery.expected_block_rows(1_000_000)
    assert len(layout) == 16
    assert layout[:15] == [65536] * 15
    assert layout[15] == 16960


@pytest.mark.parametrize(
    "rows,expected",
    [(0, []), (1, [1]), (65536, [65536]), (65537, [65536, 1]), (131072, [65536, 65536])],
)
def test_block_layout_boundaries(rows: int, expected: list[int]) -> None:
    assert battery.expected_block_rows(rows) == expected


def test_an_undeclared_mechanism_is_refused() -> None:
    with pytest.raises(KeyError, match="not declared in battery.MECHANISMS"):
        battery.mechanism_of("b2_boundary", "b2.invented_code")


def test_rf002_skips_with_a_stated_reason_when_nothing_is_nullable() -> None:
    result = battery.check_rf002_discriminator(
        row_count=10, null_counts={}, nan_value_counts={}, ground_truth={},
        spec={"discriminators": [], "discriminators_absent_reason": "no nullable column"},
        table="t",
    )
    assert result.status == SKIPPED
    assert result.reason_code == ""


def test_serve_gate_skips_with_a_reason_when_not_requested() -> None:
    result = battery.check_serve_gate(None, None)
    assert result.status == SKIPPED
    assert "--serve-gate" in result.detail


def test_recipe_security_skips_for_a_non_recipe_dialect() -> None:
    assert battery.check_recipe_security(None).status == SKIPPED


# ---------------------------------------------------------------------------
# Pack-level: a failure cannot hide
# ---------------------------------------------------------------------------


def test_a_single_failure_makes_the_overall_verdict_fail() -> None:
    from factory.evidence import build_pack

    pack = build_pack(
        dialect="x", table="t", source="s",
        checks=[CheckResult("ok", PASS), CheckResult("skipped", SKIPPED),
                CheckResult("bad", FAIL)],
        artifact={}, invocation={},
    )
    assert pack.verdict == FAIL


def test_skips_alone_do_not_fail_the_run() -> None:
    from factory.evidence import build_pack

    pack = build_pack(
        dialect="x", table="t", source="s",
        checks=[CheckResult("ok", PASS), CheckResult("skipped", SKIPPED)],
        artifact={}, invocation={},
    )
    assert pack.verdict == PASS
