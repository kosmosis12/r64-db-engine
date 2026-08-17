"""Proof that the oracle can FAIL.

Every check in `factory/battery.py` gets two tests here: one that it passes on
good input, and — the load-bearing one — one that it FAILS on input broken in
the specific way that check exists to catch.

**An oracle that cannot be shown to fail is not an oracle.** A battery of
checks that all return PASS unconditionally would produce exactly the same
green run as a correct one, and no amount of reading the conformance output
would tell the two apart. So the broken fixtures are not defensive extras; they
are what makes a green conformance run mean anything at all.

Every test here is pure: no container, no network, no artifact. That is possible
because the judges take gathered facts and return verdicts, with all I/O left in
`factory/conformance.py`.
"""

from __future__ import annotations

import pytest

from factory import battery
from factory.battery import FAIL, PASS, SKIPPED

# ---------------------------------------------------------------------------
# 1. registry_admission
# ---------------------------------------------------------------------------


class _Driver:
    def __init__(self, name: str) -> None:
        self._name = name

    def dialect_name(self) -> str:
        return self._name


def _good_resolve(dialect: str):
    registry = {"clickhouse", "postgres"}
    if dialect not in registry:
        raise ValueError(f"unknown dialect '{dialect}' (available: clickhouse, postgres)")
    return _Driver(dialect)


def _good_validate(dialect: str):
    registry = {"clickhouse", "postgres"}
    if dialect not in registry:
        raise ValueError(f"unknown dialect '{dialect}' (registered: clickhouse, postgres)")
    return object()


def test_registry_admission_passes_on_a_healthy_registry() -> None:
    result = battery.check_registry_admission(
        "clickhouse", ["clickhouse", "postgres"], _good_resolve, _good_validate
    )
    assert result.status == PASS


def test_registry_admission_fails_when_an_unknown_dialect_is_accepted() -> None:
    """The PG-010 regression: anything resolves, so nothing is really registered."""

    def permissive(dialect: str):
        return _Driver(dialect)

    result = battery.check_registry_admission(
        "clickhouse", ["clickhouse", "postgres"], permissive, _good_validate
    )
    assert result.status == FAIL
    assert any(c.actual == "accepted silently" for c in result.comparisons)


def test_registry_admission_fails_when_the_refusal_does_not_list_the_registry() -> None:
    """A raise alone is not enough: the message must say what WOULD be accepted.

    This is the whole operator-facing point of PG-010. "unknown dialect" sends
    the reader to the source; an enumerated registry answers in place.
    """

    def terse(dialect: str):
        if dialect not in {"clickhouse", "postgres"}:
            raise ValueError("unknown dialect")
        return _Driver(dialect)

    result = battery.check_registry_admission(
        "clickhouse", ["clickhouse", "postgres"], terse, _good_validate
    )
    assert result.status == FAIL


def test_registry_admission_fails_when_the_registry_is_filed_under_the_wrong_key() -> None:
    """Resolves fine, returns the wrong driver — invisible without the round-trip."""

    def mislabeled(dialect: str):
        if dialect not in {"clickhouse", "postgres"}:
            raise ValueError(f"unknown dialect '{dialect}' (available: clickhouse, postgres)")
        return _Driver("postgres")

    result = battery.check_registry_admission(
        "clickhouse", ["clickhouse", "postgres"], mislabeled, _good_validate
    )
    assert result.status == FAIL


# ---------------------------------------------------------------------------
# 2. schema_exactness
# ---------------------------------------------------------------------------

SPEC_COLUMNS = [
    {"name": "row_id", "type": "int64"},
    {"name": "region", "type": "large_string"},
    {"name": "event_time", "type": "timestamp[us]"},
]


def test_schema_exactness_passes_on_an_exact_match() -> None:
    observed = {"row_id": "int64", "region": "large_string", "event_time": "timestamp[us]"}
    assert battery.check_schema_exactness(observed, SPEC_COLUMNS).status == PASS


def test_schema_exactness_fails_on_a_widened_type() -> None:
    """int32 where int64 was promised is the RF-001 class of silent truncation."""
    observed = {"row_id": "int32", "region": "large_string", "event_time": "timestamp[us]"}
    assert battery.check_schema_exactness(observed, SPEC_COLUMNS).status == FAIL


def test_schema_exactness_fails_on_a_reordered_schema() -> None:
    """Same types, same names, different order — a different artifact to any
    consumer that indexes by position."""
    observed = {"region": "large_string", "row_id": "int64", "event_time": "timestamp[us]"}
    assert battery.check_schema_exactness(observed, SPEC_COLUMNS).status == FAIL


def test_schema_exactness_fails_on_a_missing_column() -> None:
    observed = {"row_id": "int64", "region": "large_string"}
    assert battery.check_schema_exactness(observed, SPEC_COLUMNS).status == FAIL


def test_schema_exactness_fails_on_a_timestamp_unit_drift() -> None:
    """timestamp[ns] where [us] was promised: the cast that B-4 exists about."""
    observed = {"row_id": "int64", "region": "large_string", "event_time": "timestamp[ns]"}
    assert battery.check_schema_exactness(observed, SPEC_COLUMNS).status == FAIL


def test_schema_exactness_fails_on_an_empty_spec() -> None:
    """A spec with no columns must not silently gate nothing."""
    assert battery.check_schema_exactness({"a": "int64"}, []).status == FAIL


# --- the B-3 string-width fence -------------------------------------------


def test_b3_fence_accepts_either_string_width() -> None:
    observed = {"row_id": "int64", "region": "string", "event_time": "timestamp[us]"}
    assert battery.check_schema_exactness(observed, SPEC_COLUMNS).status == PASS


def test_b3_fence_can_be_turned_off() -> None:
    observed = {"row_id": "int64", "region": "string", "event_time": "timestamp[us]"}
    result = battery.check_schema_exactness(observed, SPEC_COLUMNS, string_width_tolerant=False)
    assert result.status == FAIL


def test_b3_fence_reaches_inside_a_dictionary_type() -> None:
    assert battery.normalize_string_width(
        "dictionary<values=large_string, indices=int32, ordered=0>"
    ) == "dictionary<values=string, indices=int32, ordered=0>"


@pytest.mark.parametrize("other", ["large_binary", "large_list<item: int64>"])
def test_b3_fence_does_not_touch_other_large_types(other: str) -> None:
    """The fence is about string WIDTH only. Folding large_binary would hide a
    real type change behind a tolerance that was never meant to cover it."""
    assert battery.normalize_string_width(other) == other


# ---------------------------------------------------------------------------
# 3. aggregate_parity
# ---------------------------------------------------------------------------

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


def test_aggregate_parity_passes_when_everything_matches() -> None:
    computed = dict(GROUND_TRUTH)
    assert battery.check_aggregate_parity(computed, GROUND_TRUTH, AGG_SPECS).status == PASS


def test_aggregate_parity_fails_on_a_gating_mismatch() -> None:
    computed = {**GROUND_TRUTH, "sum_quantity": 499}
    assert battery.check_aggregate_parity(computed, GROUND_TRUTH, AGG_SPECS).status == FAIL


def test_aggregate_parity_fails_when_the_exact_int_authority_disagrees() -> None:
    computed = {**GROUND_TRUTH, "scaled_amount_sum_exact_int": 12344}
    assert battery.check_aggregate_parity(computed, GROUND_TRUTH, AGG_SPECS).status == FAIL


def test_corroborating_float_form_does_not_gate() -> None:
    """The documented exemption, pinned so it cannot silently widen.

    The float form is order-sensitive by construction. A driver that
    transported every row correctly must not be failed because the source
    summed float64 in a different parallel order.
    """
    computed = {**GROUND_TRUTH, "scaled_amount_sum": 12346}
    result = battery.check_aggregate_parity(computed, GROUND_TRUTH, AGG_SPECS)
    assert result.status == PASS
    assert "corroborating form disagreed" in result.detail


def test_aggregate_parity_fails_when_the_spec_names_a_missing_ground_truth_key() -> None:
    specs = [*AGG_SPECS, {"ground_truth_key": "nonexistent", "op": "row_count"}]
    computed = {**GROUND_TRUTH, "nonexistent": 1}
    assert battery.check_aggregate_parity(computed, GROUND_TRUTH, specs).status == FAIL


def test_aggregate_parity_fails_on_an_empty_aggregate_set() -> None:
    assert battery.check_aggregate_parity({}, GROUND_TRUTH, []).status == FAIL


# ---------------------------------------------------------------------------
# 4. rf002_null_discriminator
# ---------------------------------------------------------------------------

RF_SPEC = {
    "discriminators": [
        {
            "column": "score",
            "ground_truth_key": "count_score_null",
            "expected_null_count": {"perf_1m": 20039},
        }
    ]
}
RF_GT = {"count_score_null": 20039}


def _rf(**overrides):
    kwargs = {
        "row_count": 1000000,
        "null_counts": {"score": 20039},
        "nan_value_counts": {"score": 0},
        "ground_truth": RF_GT,
        "spec": RF_SPEC,
        "table": "perf_1m",
    }
    kwargs.update(overrides)
    return battery.check_rf002_discriminator(**kwargs)


def test_rf002_passes_when_the_discriminator_is_armed() -> None:
    assert _rf().status == PASS


def test_rf002_fails_when_nulls_were_filled_in_transit() -> None:
    """The zero-fill failure: count(col) == count(*), and every mean() is now wrong."""
    result = _rf(null_counts={"score": 0})
    assert result.status == FAIL


def test_rf002_fails_when_the_null_count_is_merely_close() -> None:
    """Exact counts, not 'roughly nullable'. One lost null is one lost null."""
    assert _rf(null_counts={"score": 20038}).status == FAIL


def test_rf002_fails_when_nan_is_smuggled_in_as_a_value() -> None:
    """null_count would look right while every downstream sum() is poisoned."""
    assert _rf(nan_value_counts={"score": 12}).status == FAIL


def test_rf002_fails_when_the_spec_and_ground_truth_disagree() -> None:
    """Two independent records of one number. Disagreement IS the finding."""
    assert _rf(ground_truth={"count_score_null": 20040}).status == FAIL


def test_rf002_fails_when_the_spec_declares_no_discriminators_key() -> None:
    """Not a skip. 'Nobody thought about it' must not render as 'nothing to check'."""
    assert _rf(spec={}).status == FAIL


def test_rf002_fails_on_an_empty_declaration_without_a_reason() -> None:
    assert _rf(spec={"discriminators": []}).status == FAIL


def test_rf002_skips_on_an_empty_declaration_with_a_stated_reason() -> None:
    """The Phase-3 shape: a source with genuinely no nullable column."""
    result = _rf(
        spec={
            "discriminators": [],
            "discriminators_absent_reason": "open-meteo hourly series has no nullable column",
        }
    )
    assert result.status == SKIPPED
    assert "open-meteo" in result.detail


def test_rf002_fails_when_the_declaration_omits_this_table() -> None:
    spec = {"discriminators": [{"column": "score", "expected_null_count": {"perf_10m": 200407}}]}
    assert _rf(spec=spec).status == FAIL


# ---------------------------------------------------------------------------
# 5. b2_boundary
# ---------------------------------------------------------------------------

ARTIFACT_BOUNDS = {"event_time": ("2026-01-01 00:00:15.184566", "2026-06-29 23:59:30.942340")}


def test_b2_passes_when_bounds_match_the_live_source() -> None:
    result = battery.check_b2_boundary(
        ARTIFACT_BOUNDS, dict(ARTIFACT_BOUNDS), ["event_time"], source_timezone="UTC"
    )
    assert result.status == PASS


def test_b2_fails_on_the_eight_hour_timezone_shift() -> None:
    """B-2 itself, reproduced as a fixture.

    This is the exact defect that every aggregate check passes through
    untouched: count, sum, null count and cardinality are all invariant under a
    uniform translation. Only the boundary assertion moves.
    """
    shifted = {"event_time": ("2025-12-31 16:00:15.184566", "2026-06-29 16:59:30.942340")}
    result = battery.check_b2_boundary(
        shifted, dict(ARTIFACT_BOUNDS), ["event_time"], source_timezone="America/Los_Angeles"
    )
    assert result.status == FAIL


def test_b2_fails_when_no_boundary_column_is_declared() -> None:
    """Declaring none must not be a free pass — that is the whole gap B-2 closed."""
    assert battery.check_b2_boundary({}, {}, []).status == FAIL


def test_b2_fails_when_the_source_was_never_probed() -> None:
    """A one-sided comparison is not a comparison."""
    assert battery.check_b2_boundary(ARTIFACT_BOUNDS, {}, ["event_time"]).status == FAIL


# ---------------------------------------------------------------------------
# 6. pg011_refusal
# ---------------------------------------------------------------------------


def test_pg011_passes_when_incremental_is_refused_by_name() -> None:
    def refuse():
        raise RuntimeError("sink 'arrow_ipc' cannot serve incremental mode (not appendable)")

    assert battery.check_pg011_refusal(refuse).status == PASS


def test_pg011_fails_when_incremental_is_accepted() -> None:
    """The silent-downgrade failure: a partial snapshot that looks complete."""
    assert battery.check_pg011_refusal(lambda: object()).status == FAIL


def test_pg011_fails_when_the_refusal_is_for_an_unrelated_reason() -> None:
    """A bare `raises` would pass this. Connection refused is not the guard firing."""

    def wrong_error():
        raise RuntimeError("connection refused")

    assert battery.check_pg011_refusal(wrong_error).status == FAIL


# ---------------------------------------------------------------------------
# 7. block_structure
# ---------------------------------------------------------------------------


def test_block_layout_for_one_million_rows() -> None:
    layout = battery.expected_block_rows(1_000_000)
    assert len(layout) == 16
    assert layout[:15] == [65536] * 15
    assert layout[15] == 16960
    assert sum(layout) == 1_000_000


@pytest.mark.parametrize(
    "rows,expected",
    [
        (0, []),
        (1, [1]),
        (65536, [65536]),
        (65537, [65536, 1]),
        (131072, [65536, 65536]),
    ],
)
def test_block_layout_boundaries(rows: int, expected: list[int]) -> None:
    assert battery.expected_block_rows(rows) == expected


def test_block_structure_passes_on_a_correct_layout() -> None:
    rows = 1_000_000
    assert battery.check_block_structure(battery.expected_block_rows(rows), rows).status == PASS


def test_block_structure_fails_on_a_single_block_file() -> None:
    """The regression that would surface as a PERFORMANCE symptom months later:
    one block collapses the consumer's per-block cache granularity, turning a
    warm-pass zero-decode into a whole-file decode."""
    assert battery.check_block_structure([1_000_000], 1_000_000).status == FAIL


def test_block_structure_fails_on_the_right_count_but_uneven_blocks() -> None:
    """16 blocks, correct total, wrong granularity — a block-count assertion
    would pass this."""
    uneven = [62_500] * 16
    assert sum(uneven) == 1_000_000
    assert battery.check_block_structure(uneven, 1_000_000).status == FAIL


def test_block_structure_fails_on_missing_rows() -> None:
    assert battery.check_block_structure([65536] * 15, 1_000_000).status == FAIL


# ---------------------------------------------------------------------------
# 8. checksum
# ---------------------------------------------------------------------------


def test_checksum_passes_on_two_identical_pulls() -> None:
    assert battery.check_checksum("abc123", "abc123").status == PASS


def test_checksum_fails_and_names_a_data_layer_difference() -> None:
    result = battery.check_checksum(
        "aaa", "bbb", first_rows=1_000_000, second_rows=999_999,
        first_schema={"a": "int64"}, second_schema={"a": "int64"},
    )
    assert result.status == FAIL
    assert "row count" in result.detail
    assert "fidelity defect" in result.detail


def test_checksum_fails_and_names_the_residual_when_only_bytes_differ() -> None:
    """The real ClickHouse case: same rows, same schema, same blocks, same
    aggregates, different bytes. The check must SAY what is left over rather
    than reporting an unexplained mismatch — scan order was the answer."""
    common = {
        "first_rows": 1_000_000, "second_rows": 1_000_000,
        "first_schema": {"a": "int64"}, "second_schema": {"a": "int64"},
        "first_blocks": [65536], "second_blocks": [65536],
        "first_aggregates": {"count": 1_000_000}, "second_aggregates": {"count": 1_000_000},
    }
    result = battery.check_checksum("aaa", "bbb", **common)
    assert result.status == FAIL
    assert "only the BYTES differ" in result.detail
    assert "row order" in result.detail


# ---------------------------------------------------------------------------
# 9. zero_copy_serve_gate
# ---------------------------------------------------------------------------

COLD_OK = {
    "cache_hits": 0, "cache_misses": 32, "columns_decoded": 32,
    "zero_copy_columns": 32, "copied_columns": 0,
}
WARM_OK = {
    "cache_hits": 32, "cache_misses": 0, "columns_decoded": 0,
    "zero_copy_columns": 0, "copied_columns": 0,
}


def test_serve_gate_passes_on_real_measured_counters() -> None:
    """These are the actual deltas measured against meshroad on the meshbench
    artifact, kept verbatim so a future counter-shape change breaks loudly."""
    assert battery.check_serve_gate(COLD_OK, WARM_OK).status == PASS


def test_serve_gate_fails_when_a_column_was_copied_cold() -> None:
    """The zero-copy claim itself: a heap copy instead of a view into the mmap."""
    assert battery.check_serve_gate({**COLD_OK, "copied_columns": 1}, WARM_OK).status == FAIL


def test_serve_gate_fails_when_a_column_was_copied_warm() -> None:
    assert battery.check_serve_gate(COLD_OK, {**WARM_OK, "copied_columns": 4}).status == FAIL


def test_serve_gate_fails_when_the_warm_pass_still_missed() -> None:
    warm = {**WARM_OK, "cache_hits": 16, "cache_misses": 16}
    assert battery.check_serve_gate(COLD_OK, warm).status == FAIL


def test_serve_gate_fails_when_the_warm_pass_decoded_anything() -> None:
    """Stronger than miss_rate, and the assertion that catches a collapsed block
    layout: a cache can report a 0% miss rate while still decoding."""
    warm = {**WARM_OK, "columns_decoded": 32}
    assert battery.check_serve_gate(COLD_OK, warm).status == FAIL


def test_serve_gate_fails_when_the_cold_pass_decoded_nothing() -> None:
    """A workload that touches no column would otherwise pass every other
    assertion trivially."""
    cold = {**COLD_OK, "columns_decoded": 0, "cache_misses": 0, "zero_copy_columns": 0}
    assert battery.check_serve_gate(cold, WARM_OK).status == FAIL


def test_serve_gate_skips_with_a_reason_when_not_requested() -> None:
    result = battery.check_serve_gate(None, None)
    assert result.status == SKIPPED
    assert "--serve-gate" in result.detail


# ---------------------------------------------------------------------------
# Cross-cutting: the pack must not be able to hide a failure
# ---------------------------------------------------------------------------


def test_a_single_failure_makes_the_overall_verdict_fail() -> None:
    from factory.evidence import build_pack

    pack = build_pack(
        dialect="x", table="t", source="s",
        checks=[
            battery.CheckResult("ok", PASS),
            battery.CheckResult("skipped", SKIPPED),
            battery.CheckResult("bad", FAIL),
        ],
        artifact={}, invocation={},
    )
    assert pack.verdict == FAIL


def test_skips_alone_do_not_fail_the_run() -> None:
    from factory.evidence import build_pack

    pack = build_pack(
        dialect="x", table="t", source="s",
        checks=[battery.CheckResult("ok", PASS), battery.CheckResult("skipped", SKIPPED)],
        artifact={}, invocation={},
    )
    assert pack.verdict == PASS
