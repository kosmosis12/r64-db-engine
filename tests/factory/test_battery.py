"""Proof that the oracle can fail — FOR THE RIGHT REASON.

# What Codex found wrong with the first version of this file

Every negative fixture asserted `result.status == FAIL` and nothing else. That
assertion is satisfied by an oracle that fails for an unrelated reason, and —
fatally — by an oracle that fails at *everything*. A stub returning FAIL
unconditionally would have turned this entire suite green while checking
nothing, and the suite would have looked exactly as green as a correct one.
"The oracle can fail" is not the claim worth making. "The oracle fails **when
and only when** the specific mechanism it names is broken" is.

# The shape now

Every negative case asserts a TRIPLE:

1. the run FAILed,
2. the **named check** that failed is the one under test, and
3. the **reason_code** is the one specific to that mechanism —
   `b2.bounds_diverged` for a shifted boundary, not merely "something failed".

The cases live in one table, `NEGATIVE_CASES`, which is the single source of
truth. It is parametrized into the real assertion, and — this is the point —
into `test_a_catch_all_failing_oracle_does_not_pass_the_fixture_suite`, which
replays every case against a stub that returns FAIL for everything and proves
each assertion REJECTS it.

That meta-fixture is the test of the tests. **MF-01's claim stands or falls on
it**, not on how many negative cases the table happens to hold.
"""

from __future__ import annotations

from typing import Any, NamedTuple

import pytest

from factory import battery
from factory.battery import FAIL, PASS, SKIPPED, CheckResult

# ---------------------------------------------------------------------------
# The assertion under test
# ---------------------------------------------------------------------------


def assert_failed_for_reason(result: CheckResult, check: str, code: str) -> None:
    """The triple. Used by the real cases AND by the meta-fixture.

    Factored out precisely so the meta-fixture exercises the SAME assertion the
    real cases do. A meta-fixture that re-implemented a weaker check would
    prove nothing about this file.
    """
    assert result.status == FAIL, f"expected FAIL, got {result.status}"
    assert result.name == check, f"expected check {check!r}, got {result.name!r}"
    assert result.reason_code == code, (
        f"expected reason_code {code!r}, got {result.reason_code!r} "
        f"(detail: {result.detail[:200]})"
    )


def assert_passed(result: CheckResult, check: str) -> None:
    assert result.status == PASS, f"expected PASS, got {result.status}: {result.detail[:200]}"
    assert result.name == check
    assert result.reason_code == "", f"a PASS must carry no reason_code, got {result.reason_code!r}"


# ---------------------------------------------------------------------------
# Fixtures shared by the case table
# ---------------------------------------------------------------------------


class _Driver:
    def __init__(self, name: str) -> None:
        self._name = name

    def dialect_name(self) -> str:
        return self._name


def _good_resolve(dialect: str):
    if dialect not in {"clickhouse", "postgres"}:
        raise ValueError(f"unknown dialect '{dialect}' (available: clickhouse, postgres)")
    return _Driver(dialect)


def _good_validate(dialect: str):
    if dialect not in {"clickhouse", "postgres"}:
        raise ValueError(f"unknown dialect '{dialect}' (registered: clickhouse, postgres)")
    return object()


REGISTERED = ["clickhouse", "postgres"]

SPEC_COLUMNS = [
    {"name": "row_id", "type": "int64"},
    {"name": "region", "type": "large_string"},
    {"name": "event_time", "type": "timestamp[us]"},
]
GOOD_SCHEMA = {"row_id": "int64", "region": "large_string", "event_time": "timestamp[us]"}

AGG_SPECS = [
    {"ground_truth_key": "count", "op": "row_count"},
    {"ground_truth_key": "sum_quantity", "op": "sum_int", "column": "quantity"},
    {"ground_truth_key": "scaled_amount_sum_exact_int", "op": "scaled_sum_exact_int"},
    {"ground_truth_key": "scaled_amount_sum", "op": "scaled_sum_float", "corroborating": True},
]
GROUND_TRUTH = {
    "count": 1000,
    "sum_quantity": 500,
    "scaled_amount_sum_exact_int": 12345,
    "scaled_amount_sum": 12345,
}

RF_SPEC: dict[str, Any] = {
    "discriminators": [
        {
            "column": "score",
            "ground_truth_key": "count_score_null",
            "expected_null_count": {"perf_1m": 20039},
        }
    ]
}
RF_GT = {"count_score_null": 20039}


def _rf(**overrides) -> CheckResult:
    kwargs: dict[str, Any] = {
        "row_count": 1000000,
        "null_counts": {"score": 20039},
        "nan_value_counts": {"score": 0},
        "ground_truth": RF_GT,
        "spec": RF_SPEC,
        "table": "perf_1m",
    }
    kwargs.update(overrides)
    return battery.check_rf002_discriminator(**kwargs)


BOUNDS = {"event_time": ("2026-01-01 00:00:15.184566", "2026-06-29 23:59:30.942340")}
SHIFTED = {"event_time": ("2025-12-31 16:00:15.184566", "2026-06-29 16:59:30.942340")}

COLD_OK = {"cache_hits": 0, "cache_misses": 32, "columns_decoded": 32,
           "zero_copy_columns": 32, "copied_columns": 0}
WARM_OK = {"cache_hits": 32, "cache_misses": 0, "columns_decoded": 0,
           "zero_copy_columns": 0, "copied_columns": 0}

CHECKSUM_SAME_DATA = {
    "first_rows": 1_000_000, "second_rows": 1_000_000,
    "first_schema": {"a": "int64"}, "second_schema": {"a": "int64"},
    "first_blocks": [65536], "second_blocks": [65536],
    "first_aggregates": {"count": 1_000_000}, "second_aggregates": {"count": 1_000_000},
}


# ---------------------------------------------------------------------------
# The case table — single source of truth
# ---------------------------------------------------------------------------


class Case(NamedTuple):
    id: str
    run: Any            # () -> CheckResult
    check: str
    code: str
    why: str


NEGATIVE_CASES: list[Case] = [
    # --- 1. registry_admission (PG-010) ---
    Case(
        "registry: an unregistered dialect is accepted",
        lambda: battery.check_registry_admission(
            "clickhouse", REGISTERED, lambda d: _Driver(d), _good_validate),
        "registry_admission", "registry.unregistered_accepted",
        "the PG-010 regression: anything resolves, so nothing is really registered",
    ),
    Case(
        "registry: the refusal does not list the registry",
        lambda: battery.check_registry_admission(
            "clickhouse", REGISTERED,
            lambda d: (_ for _ in ()).throw(ValueError("unknown dialect"))
            if d not in REGISTERED else _Driver(d),
            _good_validate),
        "registry_admission", "registry.refusal_omits_registry",
        "a raise alone is not enough; 'unknown dialect' sends the operator to the source",
    ),
    Case(
        "registry: filed under the wrong key",
        lambda: battery.check_registry_admission(
            "clickhouse", REGISTERED,
            lambda d: _Driver("postgres") if d in REGISTERED
            else (_ for _ in ()).throw(ValueError(f"unknown dialect '{d}' (available: clickhouse, postgres)")),
            _good_validate),
        "registry_admission", "registry.dialect_name_mismatch",
        "resolves fine, returns the wrong driver — invisible without the round-trip",
    ),
    Case(
        "registry: the configured dialect does not resolve at all",
        lambda: battery.check_registry_admission(
            "clickhouse", REGISTERED,
            lambda d: (_ for _ in ()).throw(ValueError("boom")), _good_validate),
        "registry_admission", "registry.dialect_unresolvable",
        "the registry cannot produce the driver the config names",
    ),

    # --- 2. schema_exactness ---
    Case(
        "schema: a widened type (int32 where int64 was promised)",
        lambda: battery.check_schema_exactness({**GOOD_SCHEMA, "row_id": "int32"}, SPEC_COLUMNS),
        "schema_exactness", "schema.type_mismatch",
        "the RF-001 class of silent truncation",
    ),
    Case(
        "schema: a timestamp unit drift (ns where us was promised)",
        lambda: battery.check_schema_exactness(
            {**GOOD_SCHEMA, "event_time": "timestamp[ns]"}, SPEC_COLUMNS),
        "schema_exactness", "schema.type_mismatch",
        "the cast B-4 exists about",
    ),
    Case(
        "schema: columns reordered",
        lambda: battery.check_schema_exactness(
            {"region": "large_string", "row_id": "int64", "event_time": "timestamp[us]"},
            SPEC_COLUMNS),
        "schema_exactness", "schema.column_set_or_order",
        "same names and types, different order — a different artifact to a positional reader",
    ),
    Case(
        "schema: a column is missing",
        lambda: battery.check_schema_exactness(
            {"row_id": "int64", "region": "large_string"}, SPEC_COLUMNS),
        "schema_exactness", "schema.column_count",
        "fewer columns than the spec declares",
    ),
    Case(
        "schema: the spec itself is empty",
        lambda: battery.check_schema_exactness({"a": "int64"}, []),
        "schema_exactness", "schema.empty_spec",
        "a spec with no columns must not silently gate nothing",
    ),
    Case(
        "schema: string width differs with the B-3 fence OFF",
        lambda: battery.check_schema_exactness(
            {**GOOD_SCHEMA, "region": "string"}, SPEC_COLUMNS, string_width_tolerant=False),
        "schema_exactness", "schema.type_mismatch",
        "the fence is opt-out, and turning it off must actually tighten the check",
    ),

    # --- 3. aggregate_parity ---
    Case(
        "aggregates: a gating aggregate disagrees",
        lambda: battery.check_aggregate_parity(
            {**GROUND_TRUTH, "sum_quantity": 499}, GROUND_TRUTH, AGG_SPECS),
        "aggregate_parity", "aggregate.mismatch",
        "source-captured truth and the pipeline disagree",
    ),
    Case(
        "aggregates: the exact-int AUTHORITY disagrees",
        lambda: battery.check_aggregate_parity(
            {**GROUND_TRUTH, "scaled_amount_sum_exact_int": 12344}, GROUND_TRUTH, AGG_SPECS),
        "aggregate_parity", "aggregate.mismatch",
        "the order-independent form is the one that gates",
    ),
    Case(
        "aggregates: the spec names a ground-truth key that does not exist",
        lambda: battery.check_aggregate_parity(
            {**GROUND_TRUTH, "nonexistent": 1}, GROUND_TRUTH,
            [*AGG_SPECS, {"ground_truth_key": "nonexistent", "op": "row_count"}]),
        "aggregate_parity", "aggregate.missing_ground_truth_key",
        "a spec and a ground-truth file that have drifted apart",
    ),
    Case(
        "aggregates: the aggregate set is empty",
        lambda: battery.check_aggregate_parity({}, GROUND_TRUTH, []),
        "aggregate_parity", "aggregate.empty_spec",
        "parity cannot be gated on an empty set",
    ),

    # --- 4. rf002 ---
    Case(
        "rf002: nulls were filled in transit",
        lambda: _rf(null_counts={"score": 0}),
        "rf002_null_discriminator", "rf002.null_count_mismatch",
        "the zero-fill failure: every mean() is now wrong while totals look plausible",
    ),
    Case(
        "rf002: the null count is merely close",
        lambda: _rf(null_counts={"score": 20038}),
        "rf002_null_discriminator", "rf002.null_count_mismatch",
        "exact counts, not 'roughly nullable' — one lost null is one lost null",
    ),
    Case(
        "rf002: NaN smuggled in as a value",
        lambda: _rf(nan_value_counts={"score": 12}),
        "rf002_null_discriminator", "rf002.nan_as_value",
        "null_count looks right while every downstream sum() is poisoned",
    ),
    Case(
        "rf002: spec and ground truth disagree",
        lambda: _rf(ground_truth={"count_score_null": 20040}),
        "rf002_null_discriminator", "rf002.spec_ground_truth_disagree",
        "two independent records of one number; disagreement IS the finding",
    ),
    Case(
        "rf002: the spec declares no discriminators key at all",
        lambda: _rf(spec={}),
        "rf002_null_discriminator", "rf002.no_declaration",
        "'nobody thought about it' must not render as 'nothing to check'",
    ),
    Case(
        "rf002: an empty declaration with no stated reason",
        lambda: _rf(spec={"discriminators": []}),
        "rf002_null_discriminator", "rf002.empty_without_reason",
        "a silent zero is not a skip",
    ),
    Case(
        "rf002: the declaration omits this table",
        lambda: _rf(spec={"discriminators": [
            {"column": "score", "expected_null_count": {"perf_10m": 200407}}]}),
        "rf002_null_discriminator", "rf002.table_not_declared",
        "declared for a different table is not declared for this one",
    ),

    # --- 5. b2 boundary ---
    Case(
        "b2: the eight-hour timezone shift",
        lambda: battery.check_b2_boundary(SHIFTED, dict(BOUNDS), ["event_time"],
                                          source_timezone="America/Los_Angeles"),
        "b2_boundary", "b2.bounds_diverged",
        "B-2 itself: every aggregate check passes through this untouched",
    ),
    Case(
        "b2: no boundary column is declared",
        lambda: battery.check_b2_boundary({}, {}, []),
        "b2_boundary", "b2.no_boundary_columns",
        "declaring none must not be a free pass — that is the gap B-2 closed",
    ),
    Case(
        "b2: the live source was never probed",
        lambda: battery.check_b2_boundary(BOUNDS, {}, ["event_time"]),
        "b2_boundary", "b2.source_not_probed",
        "a one-sided comparison is not a comparison",
    ),

    # --- 6. pg011 ---
    Case(
        "pg011: incremental was accepted",
        lambda: battery.check_pg011_refusal(lambda: object()),
        "pg011_refusal", "pg011.accepted",
        "the silent-downgrade failure: a partial snapshot that looks complete",
    ),
    Case(
        "pg011: refused, but for an unrelated reason",
        lambda: battery.check_pg011_refusal(
            lambda: (_ for _ in ()).throw(RuntimeError("connection refused"))),
        "pg011_refusal", "pg011.wrong_error",
        "a bare `raises` would pass this; connection refused is not the guard firing",
    ),

    # --- 7. block structure ---
    Case(
        "blocks: a single-block file",
        lambda: battery.check_block_structure([1_000_000], 1_000_000),
        "block_structure", "blocks.count_mismatch",
        "collapses the consumer's per-block cache granularity",
    ),
    Case(
        "blocks: right count, wrong granularity",
        lambda: battery.check_block_structure([62_500] * 16, 1_000_000),
        "block_structure", "blocks.layout_mismatch",
        "a block-COUNT assertion would pass this",
    ),
    Case(
        "blocks: rows are missing",
        lambda: battery.check_block_structure([65536] * 15, 1_000_000),
        "block_structure", "blocks.count_mismatch",
        "fewer blocks than the row count requires",
    ),

    # --- 8. checksum ---
    Case(
        "checksum: a data-bearing layer differs",
        lambda: battery.check_checksum("aaa", "bbb", first_rows=1_000_000, second_rows=999_999,
                                       first_schema={"a": "int64"}, second_schema={"a": "int64"}),
        "checksum", "checksum.data_layer_differs",
        "a fidelity defect, not a serialization nuance",
    ),
    Case(
        "checksum: only the bytes differ",
        lambda: battery.check_checksum("aaa", "bbb", **CHECKSUM_SAME_DATA),
        "checksum", "checksum.bytes_only",
        "the real ClickHouse case — scan order was the answer",
    ),

    # --- 9. serve gate ---
    Case(
        "serve: a column was copied on the cold pass",
        lambda: battery.check_serve_gate({**COLD_OK, "copied_columns": 1}, WARM_OK),
        "zero_copy_serve_gate", "serve.copied_columns_cold",
        "the zero-copy claim itself: a heap copy instead of a view into the mmap",
    ),
    Case(
        "serve: a column was copied on the warm pass",
        lambda: battery.check_serve_gate(COLD_OK, {**WARM_OK, "copied_columns": 4}),
        "zero_copy_serve_gate", "serve.copied_columns_warm",
        "same claim, other pass",
    ),
    Case(
        "serve: the warm pass still missed",
        lambda: battery.check_serve_gate(
            COLD_OK, {**WARM_OK, "cache_hits": 16, "cache_misses": 16}),
        "zero_copy_serve_gate", "serve.warm_miss",
        "a warm cache that misses is not warm",
    ),
    Case(
        "serve: the warm pass decoded anything",
        lambda: battery.check_serve_gate(COLD_OK, {**WARM_OK, "columns_decoded": 32}),
        "zero_copy_serve_gate", "serve.warm_decoded",
        "stronger than miss_rate: a cache can report 0% and still decode",
    ),
    Case(
        "serve: the cold pass decoded nothing",
        lambda: battery.check_serve_gate(
            {**COLD_OK, "columns_decoded": 0, "cache_misses": 0, "zero_copy_columns": 0},
            WARM_OK),
        "zero_copy_serve_gate", "serve.cold_no_decode",
        "a workload touching no column passes every other assertion trivially",
    ),

    # --- 10. recipe security ---
    Case(
        "recipe security: a mutation was accepted",
        lambda: battery.check_recipe_security(
            [("https->http downgrade", False, "accepted without error")]),
        "recipe_security_invariants", "recipe_security.mutation_accepted",
        "the fence did not fire on a malicious shape",
    ),
    Case(
        "recipe security: no mutation was attempted",
        lambda: battery.check_recipe_security([]),
        "recipe_security_invariants", "recipe_security.no_mutations",
        "a security check that tried nothing must not report success",
    ),
]


POSITIVE_CASES: list[Case] = [
    Case("registry: a healthy registry",
         lambda: battery.check_registry_admission(
             "clickhouse", REGISTERED, _good_resolve, _good_validate),
         "registry_admission", "", ""),
    Case("schema: an exact match",
         lambda: battery.check_schema_exactness(GOOD_SCHEMA, SPEC_COLUMNS),
         "schema_exactness", "", ""),
    Case("schema: either string width, fence ON (B-3)",
         lambda: battery.check_schema_exactness({**GOOD_SCHEMA, "region": "string"}, SPEC_COLUMNS),
         "schema_exactness", "", ""),
    Case("aggregates: everything matches",
         lambda: battery.check_aggregate_parity(dict(GROUND_TRUTH), GROUND_TRUTH, AGG_SPECS),
         "aggregate_parity", "", ""),
    Case("aggregates: a corroborating mismatch does NOT gate",
         lambda: battery.check_aggregate_parity(
             {**GROUND_TRUTH, "scaled_amount_sum": 12346}, GROUND_TRUTH, AGG_SPECS),
         "aggregate_parity", "", ""),
    Case("rf002: the discriminator is armed",
         _rf, "rf002_null_discriminator", "", ""),
    Case("b2: bounds match the live source",
         lambda: battery.check_b2_boundary(BOUNDS, dict(BOUNDS), ["event_time"],
                                           source_timezone="UTC"),
         "b2_boundary", "", ""),
    Case("pg011: refused by name",
         lambda: battery.check_pg011_refusal(
             lambda: (_ for _ in ()).throw(
                 RuntimeError("sink 'arrow_ipc' cannot serve incremental mode"))),
         "pg011_refusal", "", ""),
    Case("blocks: a correct layout",
         lambda: battery.check_block_structure(
             battery.expected_block_rows(1_000_000), 1_000_000),
         "block_structure", "", ""),
    Case("checksum: two identical pulls",
         lambda: battery.check_checksum("abc123", "abc123"), "checksum", "", ""),
    Case("serve: the real measured counters",
         lambda: battery.check_serve_gate(COLD_OK, WARM_OK), "zero_copy_serve_gate", "", ""),
    Case("recipe security: every mutation refused",
         lambda: battery.check_recipe_security([("https->http", True, "RecipeSecurityError")]),
         "recipe_security_invariants", "", ""),
]


# ---------------------------------------------------------------------------
# The real assertions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", NEGATIVE_CASES, ids=[c.id for c in NEGATIVE_CASES])
def test_the_check_fails_for_its_own_specific_reason(case: Case) -> None:
    assert_failed_for_reason(case.run(), case.check, case.code)


@pytest.mark.parametrize("case", POSITIVE_CASES, ids=[c.id for c in POSITIVE_CASES])
def test_the_check_passes_on_good_input(case: Case) -> None:
    assert_passed(case.run(), case.check)


def test_every_check_in_the_battery_has_at_least_one_negative_case() -> None:
    """A check with no reason-specific negative case is unproven.

    Guards against the battery growing a tenth or eleventh check that nobody
    ever demonstrated can fail — which is Law 4 read backwards.
    """
    covered = {c.check for c in NEGATIVE_CASES}
    expected = {
        "registry_admission", "schema_exactness", "aggregate_parity",
        "rf002_null_discriminator", "b2_boundary", "pg011_refusal",
        "block_structure", "checksum", "recipe_security_invariants",
        "zero_copy_serve_gate",
    }
    assert covered == expected, f"checks with no negative case: {sorted(expected - covered)}"


def test_reason_codes_are_distinct_per_mechanism() -> None:
    """Two different mechanisms must not share a code.

    If they did, a fixture asserting the code would no longer pin the mechanism
    and the triple would quietly weaken back towards `assert FAIL`.
    """
    by_code: dict[str, set[str]] = {}
    for case in NEGATIVE_CASES:
        by_code.setdefault(case.code, set()).add(case.check)
    for code, checks in by_code.items():
        assert len(checks) == 1, f"code {code!r} is used by more than one check: {sorted(checks)}"


# ---------------------------------------------------------------------------
# THE META-FIXTURE — the test of the tests
# ---------------------------------------------------------------------------


class CatchAllFailingOracle:
    """An oracle that FAILS at everything, checking nothing.

    This is the adversary. It is what a broken-but-plausible battery looks like
    from the outside: every negative fixture "passes", every run is red, and
    nothing whatsoever has been verified. If the fixture suite cannot tell this
    apart from the real battery, then a green fixture suite means nothing and
    MF-01 is unsupported.
    """

    @staticmethod
    def result(check: str = "always_fails") -> CheckResult:
        return CheckResult(
            name=check,
            status=FAIL,
            reason_code="always.fails",
            detail="this oracle fails unconditionally and inspects nothing",
        )


@pytest.mark.parametrize("case", NEGATIVE_CASES, ids=[c.id for c in NEGATIVE_CASES])
def test_a_catch_all_failing_oracle_does_not_pass_the_fixture_suite(case: Case) -> None:
    """Every negative case must REJECT the catch-all stub.

    Note it is handed the correct check NAME — so this is not passing merely
    because the name is wrong. It is rejected on the reason code: the stub
    cannot say *why* it failed, because it never looked.
    """
    stub = CatchAllFailingOracle.result(case.check)
    assert stub.status == FAIL  # the stub really is "failing", as the adversary would be
    with pytest.raises(AssertionError, match="reason_code"):
        assert_failed_for_reason(stub, case.check, case.code)


@pytest.mark.parametrize("case", POSITIVE_CASES, ids=[c.id for c in POSITIVE_CASES])
def test_a_catch_all_failing_oracle_also_fails_every_positive_case(case: Case) -> None:
    """And it breaks the other direction too, which is the cheaper tell.

    A catch-all FAIL oracle cannot produce a PASS, so the positive cases catch
    it immediately. Both directions are asserted because an adversary that
    always PASSED would be caught only by the negative cases, and one that
    always FAILED only by these — a suite needs both to be sound.
    """
    with pytest.raises(AssertionError):
        assert_passed(CatchAllFailingOracle.result(case.check), case.check)


def test_a_catch_all_passing_oracle_does_not_pass_the_fixture_suite() -> None:
    """The mirror adversary: an oracle that PASSES everything.

    This is the failure mode the original version of this file *did* guard
    against, kept explicit so the guard cannot be lost while strengthening the
    other direction.
    """
    always_pass = CheckResult(name="anything", status=PASS)
    for case in NEGATIVE_CASES:
        with pytest.raises(AssertionError):
            assert_failed_for_reason(always_pass, case.check, case.code)


def test_the_helper_accepts_a_correct_result() -> None:
    """The assertion is not vacuously strict — it does accept the real thing."""
    case = NEGATIVE_CASES[0]
    assert_failed_for_reason(case.run(), case.check, case.code)


# ---------------------------------------------------------------------------
# Mechanism-level units that are not verdicts
# ---------------------------------------------------------------------------


def test_b3_fence_reaches_inside_a_dictionary_type() -> None:
    assert battery.normalize_string_width(
        "dictionary<values=large_string, indices=int32, ordered=0>"
    ) == "dictionary<values=string, indices=int32, ordered=0>"


@pytest.mark.parametrize("other", ["large_binary", "large_list<item: int64>"])
def test_b3_fence_does_not_touch_other_large_types(other: str) -> None:
    """The fence is about string WIDTH only. Folding large_binary would hide a
    real type change behind a tolerance never meant to cover it."""
    assert battery.normalize_string_width(other) == other


def test_block_layout_for_one_million_rows() -> None:
    layout = battery.expected_block_rows(1_000_000)
    assert len(layout) == 16
    assert layout[:15] == [65536] * 15
    assert layout[15] == 16960
    assert sum(layout) == 1_000_000


@pytest.mark.parametrize(
    "rows,expected",
    [(0, []), (1, [1]), (65536, [65536]), (65537, [65536, 1]), (131072, [65536, 65536])],
)
def test_block_layout_boundaries(rows: int, expected: list[int]) -> None:
    assert battery.expected_block_rows(rows) == expected


def test_rf002_skips_with_a_stated_reason_when_nothing_is_nullable() -> None:
    """The Phase-3 shape. A SKIP is not a FAIL and not a PASS, and it carries
    no reason_code — the property was not checked, so nothing failed."""
    result = _rf(spec={
        "discriminators": [],
        "discriminators_absent_reason": "open-meteo hourly series has no nullable column",
    })
    assert result.status == SKIPPED
    assert "open-meteo" in result.detail
    assert result.reason_code == ""


def test_serve_gate_skips_with_a_reason_when_not_requested() -> None:
    result = battery.check_serve_gate(None, None)
    assert result.status == SKIPPED
    assert "--serve-gate" in result.detail


def test_recipe_security_skips_for_a_non_recipe_dialect() -> None:
    result = battery.check_recipe_security(None)
    assert result.status == SKIPPED
    assert "recipe" in result.detail


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
