"""Unit coverage for the gathering layer: spec resolution and the aggregate ops.

The judges in `factory/battery.py` are only as good as the facts handed to
them, so the two places where gathering can go quietly wrong are pinned here:

- **Spec resolution.** A missing spec must be refused loudly. Silently running
  with no schema, no discriminators and no boundary columns would produce a
  green run that checked almost nothing.
- **The aggregate ops.** The `scaled_sum_exact_int` op is the AUTHORITY for the
  amount check, so its order-independence is asserted directly rather than
  assumed, and an unknown op is refused instead of guessed at.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from factory import conformance

# ---------------------------------------------------------------------------
# Spec resolution
# ---------------------------------------------------------------------------


def test_default_spec_path_strips_the_dialect_prefix() -> None:
    path = conformance.default_spec_path(Path("factory/targets/clickhouse-meshbench.yaml"), "clickhouse")
    assert path.name == "meshbench-schema.json"


def test_default_spec_path_leaves_an_unprefixed_stem_alone() -> None:
    path = conformance.default_spec_path(Path("factory/targets/meshbench.yaml"), "clickhouse")
    assert path.name == "meshbench-schema.json"


def test_the_shipped_meshbench_spec_resolves_by_convention() -> None:
    spec = conformance.resolve_spec(
        conformance.REPO_ROOT / "factory" / "targets" / "clickhouse-meshbench.yaml",
        "clickhouse",
        None,
    )
    assert spec["dataset"] == "meshbench"
    assert len(spec["columns"]) == 14
    assert spec["boundary_columns"] == ["event_time"]
    assert spec["discriminators"][0]["column"] == "score"


def test_a_missing_spec_is_refused_loudly(tmp_path: Path) -> None:
    """Not a skip and not a default-empty spec: a battery with no spec gates
    nothing while still printing a verdict."""
    with pytest.raises(SystemExit) as exc:
        conformance.resolve_spec(tmp_path / "clickhouse-nothing.yaml", "clickhouse", None)
    message = str(exc.value)
    assert "spec is not optional" in message
    assert "--spec" in message


def test_the_shipped_spec_declares_a_discriminator_for_every_ground_truth_table() -> None:
    """Guards the spec/ground-truth cross-check from going vacuous on one table."""
    import json

    spec = conformance.resolve_spec(
        conformance.REPO_ROOT / "factory" / "targets" / "clickhouse-meshbench.yaml",
        "clickhouse",
        None,
    )
    gt = json.loads((conformance.REPO_ROOT / "bench" / "GROUND-TRUTH-clickhouse.json").read_text())
    declared = spec["discriminators"][0]["expected_null_count"]
    for table, values in gt["tables"].items():
        assert table in declared, f"spec declares no discriminator count for {table}"
        assert declared[table] == values["count_score_null"]


# ---------------------------------------------------------------------------
# Aggregate ops
# ---------------------------------------------------------------------------


@pytest.fixture()
def sample_table():
    pa = pytest.importorskip("pyarrow")
    return pa.table(
        {
            "amount": pa.array([1.005, 2.5, 3.333, None], type=pa.float64()),
            "quantity": pa.array([1, 2, 3, 4], type=pa.int64()),
            "account_id": pa.array([10, 99, 5, 7], type=pa.int64()),
            "region": pa.array(["West", "East", "West", "North"]),
            "score": pa.array([1.0, None, None, 4.0], type=pa.float64()),
        }
    )


def test_row_count_and_simple_ops(sample_table) -> None:
    out = conformance.compute_aggregates(
        sample_table,
        [
            {"ground_truth_key": "count", "op": "row_count"},
            {"ground_truth_key": "sum_quantity", "op": "sum_int", "column": "quantity"},
            {"ground_truth_key": "max_account_id", "op": "max_int", "column": "account_id"},
            {"ground_truth_key": "uniq_region", "op": "nunique", "column": "region"},
            {"ground_truth_key": "count_region_west", "op": "count_equals", "column": "region", "value": "West"},
            {"ground_truth_key": "count_score_null", "op": "null_count", "column": "score"},
            {"ground_truth_key": "pct_score_null", "op": "null_pct", "column": "score"},
        ],
    )
    assert out == {
        "count": 4,
        "sum_quantity": 10,
        "max_account_id": 99,
        "uniq_region": 3,
        "count_region_west": 2,
        "count_score_null": 2,
        "pct_score_null": 50.0,
    }


def test_nunique_decodes_a_dictionary_column() -> None:
    """`status` ships dictionary-encoded; counting distinct indices instead of
    distinct values would still return a plausible-looking number."""
    pa = pytest.importorskip("pyarrow")
    table = pa.table(
        {"status": pa.array(["a", "b", "a", "b", "c"]).dictionary_encode()}
    )
    out = conformance.compute_aggregates(
        table,
        [
            {"ground_truth_key": "uniq_status", "op": "nunique", "column": "status"},
            {"ground_truth_key": "count_status_a", "op": "count_equals", "column": "status", "value": "a"},
        ],
    )
    assert out == {"uniq_status": 3, "count_status_a": 2}


def test_scaled_sum_exact_int_rounds_per_value_then_sums(sample_table) -> None:
    """1.005 -> 100 (banker's rounding at the .5 boundary), 2.5 -> 250,
    3.333 -> 333; the null is skipped rather than counted as zero."""
    out = conformance.compute_aggregates(
        sample_table,
        [{"ground_truth_key": "k", "op": "scaled_sum_exact_int", "column": "amount", "scale": 100}],
    )
    assert out["k"] == 100 + 250 + 333


def test_scaled_sum_exact_int_is_order_independent() -> None:
    """THE authority for the amount check. Integer addition is associative, so a
    different source scan order must not move this number — which is exactly
    what the float form cannot promise."""
    pa = pytest.importorskip("pyarrow")
    import random

    values = [round(random.uniform(0, 500), 2) for _ in range(2000)]
    spec = [{"ground_truth_key": "k", "op": "scaled_sum_exact_int", "column": "amount", "scale": 100}]

    first = conformance.compute_aggregates(pa.table({"amount": pa.array(values)}), spec)["k"]
    shuffled = values[:]
    random.shuffle(shuffled)
    second = conformance.compute_aggregates(pa.table({"amount": pa.array(shuffled)}), spec)["k"]
    assert first == second


def test_null_pct_on_an_empty_table_does_not_divide_by_zero() -> None:
    pa = pytest.importorskip("pyarrow")
    table = pa.table({"score": pa.array([], type=pa.float64())})
    out = conformance.compute_aggregates(
        table, [{"ground_truth_key": "p", "op": "null_pct", "column": "score"}]
    )
    assert out["p"] == 0.0


def test_an_unknown_op_is_refused_rather_than_guessed_at(sample_table) -> None:
    """The op vocabulary is closed on purpose: the recipe/spec is DATA and this
    function is the only code (Law 1). A spec must not be able to describe an
    operation nobody implemented."""
    with pytest.raises(SystemExit) as exc:
        conformance.compute_aggregates(
            sample_table, [{"ground_truth_key": "k", "op": "sum_of_squares", "column": "quantity"}]
        )
    assert "unknown aggregate op" in str(exc.value)


# ---------------------------------------------------------------------------
# Target document
# ---------------------------------------------------------------------------


def test_the_shipped_target_is_a_valid_engine_config() -> None:
    """The target must validate through core's own Config untouched. If it
    needed a battery-specific loader it would be proving something about a
    configuration no operator runs."""
    from r64_db_engine.core.config import Config

    doc = conformance.load_yaml(
        conformance.REPO_ROOT / "factory" / "targets" / "clickhouse-meshbench.yaml"
    )
    cfg = Config.model_validate(doc)
    assert cfg.dialect == "clickhouse"
    assert cfg.sink is not None and cfg.sink.type == "arrow_ipc"
    assert cfg.tables[0].target == "perf_1m"


def test_the_shipped_target_pins_its_scan_order() -> None:
    """Byte-reproducibility depends on it: a bare table scan returned the same
    rows in a different order on consecutive pulls, which also permuted the
    dictionary. Pinned here so a future edit that drops the ORDER BY fails a
    test instead of quietly failing the checksum check."""
    doc = conformance.load_yaml(
        conformance.REPO_ROOT / "factory" / "targets" / "clickhouse-meshbench.yaml"
    )
    assert "ORDER BY" in doc["tables"][0]["source"].upper()


def test_the_shipped_target_carries_no_credentials() -> None:
    """This whole target is designed to need zero secrets."""
    doc = conformance.load_yaml(
        conformance.REPO_ROOT / "factory" / "targets" / "clickhouse-meshbench.yaml"
    )
    assert "password" not in doc["clickhouse"]


def test_apply_work_dir_redirects_every_output_path(tmp_path: Path) -> None:
    doc = conformance.load_yaml(
        conformance.REPO_ROOT / "factory" / "targets" / "clickhouse-meshbench.yaml"
    )
    out = conformance.apply_work_dir(doc, tmp_path)
    assert out["sink"]["output_dir"] == str(tmp_path / "arrow_out")
    assert out["runtime"]["state_dir"] == str(tmp_path / "state")
    assert (tmp_path / "arrow_out").is_dir()
    # The original document must not be mutated — the same doc is reused to
    # build the mutated configs the registry and PG-011 checks need.
    assert doc["sink"]["output_dir"] != str(tmp_path / "arrow_out")
