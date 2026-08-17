"""Repair-brief rendering, and the sweep's target/ground-truth resolution.

A repair brief is a WORK ORDER, not a report. The failure mode of automated
drift detection is a notice nobody can act on — "conformance failed" and stop —
so these tests assert that the brief carries the things a reader needs in order
to do something: which comparison moved, on which SIDE it moved, whether the
environment changed, and what re-admission requires.

The "which side" assertions are the ones with teeth. An expectation that moved
(a re-captured ground truth, an edited spec) and a source that moved are
different incidents with different fixes, and a brief that cannot tell them
apart sends the reader looking in the wrong place.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from factory import conformance, repair

SWEEP_PATH = conformance.REPO_ROOT / "factory" / "bin" / "factory-conformance-sweep"


def pack(**overrides):
    doc = {
        "verdict": "FAIL",
        "tally": {"PASS": 8, "FAIL": 1, "SKIPPED": 1},
        "generated_utc": "2026-08-17T03:00:00Z",
        "environment": {
            "python": "3.13.12",
            "platform": "Linux",
            "packages": {"pyarrow": "25.0.0", "pandas": "3.0.5"},
            "container": {"image": "clickhouse/clickhouse-server:latest", "image_id": "sha256:aaa"},
            "git": {"commit": "abc123", "branch": "main"},
        },
        "checks": [
            {"name": "schema_exactness", "status": "PASS", "detail": "", "comparisons": []},
            {
                "name": "aggregate_parity",
                "status": "FAIL",
                "detail": "12 aggregates | FAILED: sum_quantity",
                "comparisons": [
                    {"label": "count", "actual": 1000000, "expected": 1000000, "ok": True},
                    {"label": "sum_quantity", "actual": 250480021, "expected": 250480021, "ok": False},
                ],
            },
        ],
    }
    doc.update(overrides)
    return doc


def previous_pack(**overrides):
    doc = json.loads(json.dumps(pack()))
    doc["verdict"] = "PASS"
    doc["generated_utc"] = "2026-08-10T03:00:00Z"
    doc["checks"][1]["status"] = "PASS"
    doc["checks"][1]["comparisons"][1]["ok"] = True
    doc.update(overrides)
    return doc


def render(**kwargs):
    defaults = {
        "dialect": "clickhouse",
        "target": "clickhouse-meshbench",
        "table": "perf_1m",
        "pack": pack(),
        "previous": previous_pack(),
        "date": "20260817",
    }
    defaults.update(kwargs)
    return repair.render_repair_brief(**defaults)


# ---------------------------------------------------------------------------
# Symptom
# ---------------------------------------------------------------------------


def test_the_brief_names_the_failing_check() -> None:
    assert "`aggregate_parity`" in render()


def test_the_brief_is_dated_and_marked_open() -> None:
    text = render()
    assert "REPAIR BRIEF — clickhouse — 2026-08-17" in text
    assert "**OPEN**" in text


def test_the_brief_carries_the_failure_detail_verbatim() -> None:
    assert "FAILED: sum_quantity" in render()


# ---------------------------------------------------------------------------
# The diff — and which SIDE moved
# ---------------------------------------------------------------------------


def test_the_status_diff_marks_the_check_that_moved() -> None:
    text = render()
    # The status-diff row specifically — the symptom section also mentions the
    # check by name, and it is the earlier match.
    line = next(x for x in text.splitlines() if x.startswith("| `aggregate_parity` |"))
    assert "PASS" in line and "FAIL" in line and "moved" in line


def test_a_failing_comparison_is_listed_even_when_the_actual_value_did_not_move() -> None:
    """The regression this exists to prevent: filtering the diff to
    'actual changed' produced a repair brief with an EMPTY diff under a FAIL
    verdict, because a poisoned/re-captured ground truth moves the EXPECTED
    side while the pipeline's own value stays put."""
    text = render(
        pack=pack(checks=[{
            "name": "aggregate_parity", "status": "FAIL", "detail": "",
            "comparisons": [
                {"label": "sum_quantity", "actual": 250480021, "expected": 250480028, "ok": False}
            ],
        }])
    )
    assert "Failing comparisons in `aggregate_parity`" in text
    assert "250480028" in text


def test_the_diff_shows_expected_from_both_runs_so_the_side_is_visible() -> None:
    text = render()
    header = next(x for x in text.splitlines() if x.startswith("| comparison |"))
    assert "actual (last green)" in header
    assert "actual (now)" in header
    assert "expected (last green)" in header
    assert "expected (now)" in header


def test_the_brief_tells_the_reader_how_to_read_the_two_sides() -> None:
    assert "Read the two *expected* columns first" in render()


def test_passing_comparisons_are_not_listed_in_the_diff() -> None:
    """The diff section is for triage. Every comparison is still in the pack."""
    text = render()
    section = text.split("Failing comparisons in `aggregate_parity`")[1].split("##")[0]
    assert "sum_quantity" in section
    assert "| count |" not in section


# ---------------------------------------------------------------------------
# Environment delta
# ---------------------------------------------------------------------------


def test_the_environment_delta_pins_the_packages_that_change_artifacts() -> None:
    """pyarrow owns the IPC block layout; pandas decides string width. If either
    moved, that is environment drift and the answer is to PIN, not to widen."""
    text = render()
    assert "| pyarrow | 25.0.0 | 25.0.0 |" in text
    assert "| pandas | 3.0.5 | 3.0.5 |" in text
    assert "container digest" in text
    assert "git commit" in text


def test_an_environment_change_is_visible_in_the_delta() -> None:
    now = pack()
    now["environment"]["packages"]["pyarrow"] = "26.0.0"
    text = render(pack=now)
    assert "| pyarrow | 25.0.0 | 26.0.0 |" in text


# ---------------------------------------------------------------------------
# Directives
# ---------------------------------------------------------------------------


def test_the_brief_directs_re_research_not_runtime_adaptation() -> None:
    text = render()
    assert "re-research at BUILD time" in text
    assert "WITHOUT the driver" in text


def test_the_brief_requires_the_full_battery_not_just_the_failed_check() -> None:
    assert "**full** battery" in render()


def test_the_brief_carries_the_zero_core_edit_assertion_for_this_dialect() -> None:
    assert r'\bclickhouse\b" src/r64_db_engine/core/' in render()


def test_the_brief_forbids_editing_ground_truth_to_match_the_pipeline() -> None:
    """The rule that must never soften: editing ground truth to make a run
    green converts an oracle into a mirror, and every later run passes by
    construction."""
    text = render()
    assert "NEVER edit ground truth to match a failing pipeline" in text
    assert "converts" in text and "mirror" in text


def test_the_brief_requires_cross_agent_qa() -> None:
    assert "Builder ≠ auditor" in render()


def test_the_brief_says_extend_the_battery_rather_than_widen_a_tolerance() -> None:
    text = render()
    assert "extend the battery" in text
    assert "Never widen a tolerance" in text


# ---------------------------------------------------------------------------
# Degenerate inputs
# ---------------------------------------------------------------------------


def test_a_first_failure_with_no_baseline_says_so_instead_of_crashing() -> None:
    text = render(previous=None)
    assert "No last-green pack was recorded" in text


def test_an_errored_run_produces_a_brief_that_names_the_error() -> None:
    """Environmental prerequisites are FAILURES, not skips — so a run that
    never completed still has to produce an actionable brief."""
    text = render(pack=None, error="ConnectionError: container not running")
    assert "container not running" in text
    assert "RED sweep and never a skip" in text


def test_write_repair_brief_uses_the_dated_naming_convention(tmp_path: Path) -> None:
    path = repair.write_repair_brief(
        dialect="clickhouse", target="t", table="tbl", pack_path=None,
        previous=None, brief_dir=tmp_path, date="20260817",
    )
    assert path.name == "REPAIR-BRIEF-clickhouse-20260817.md"
    assert path.read_text().startswith("# REPAIR BRIEF")


# ---------------------------------------------------------------------------
# Sweep resolution logic
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def sweep():
    """Import the sweep script by path — it is an executable, not a module."""
    spec = importlib.util.spec_from_loader(
        "factory_sweep",
        importlib.machinery.SourceFileLoader("factory_sweep", str(SWEEP_PATH)),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_sweep_is_executable_and_tracked() -> None:
    """Executable everywhere; repo-tracked only where there is a repo.

    The tracked half is skipped off-checkout with a reason rather than failing:
    `git ls-files` in a tree with no `.git` reports "not tracked" for
    everything, which would be a finding about the environment dressed up as a
    finding about the sweep.
    """
    import os
    import subprocess

    assert os.access(SWEEP_PATH, os.X_OK), "the sweep must be executable"

    if not (conformance.REPO_ROOT / ".git").exists():
        pytest.skip("not a git checkout; tracked-ness is unknowable here")

    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", str(SWEEP_PATH.relative_to(conformance.REPO_ROOT))],
        cwd=conformance.REPO_ROOT, capture_output=True, check=False,
    )
    assert tracked.returncode == 0, "the sweep must be repo-tracked"


def test_ground_truth_resolves_by_dataset_name(sweep, tmp_path: Path) -> None:
    (tmp_path / "GROUND-TRUTH-openmeteo.json").write_text("{}")
    resolved = sweep.resolve_ground_truth(Path("rest-openmeteo.yaml"), "rest", tmp_path)
    assert resolved.name == "GROUND-TRUTH-openmeteo.json"


def test_ground_truth_falls_back_to_the_dialect_name(sweep, tmp_path: Path) -> None:
    """`GROUND-TRUTH-clickhouse.json` predates the factory and is named for its
    dialect. Renaming a committed evidence-bearing file for tidiness would be a
    contract-touching change, so both conventions are honoured."""
    (tmp_path / "GROUND-TRUTH-clickhouse.json").write_text("{}")
    resolved = sweep.resolve_ground_truth(Path("clickhouse-meshbench.yaml"), "clickhouse", tmp_path)
    assert resolved.name == "GROUND-TRUTH-clickhouse.json"


def test_a_target_without_ground_truth_is_refused_loudly(sweep, tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="no ground truth"):
        sweep.resolve_ground_truth(Path("rest-nothing.yaml"), "rest", tmp_path)


def test_an_empty_target_directory_exits_non_zero(sweep, tmp_path: Path) -> None:
    """A sweep with nothing to sweep must not report success over an empty set."""
    with pytest.raises(SystemExit, match="no targets found"):
        sweep.discover_targets(tmp_path, None)


def test_an_unknown_target_name_is_refused_with_the_available_list(sweep) -> None:
    with pytest.raises(SystemExit, match="matched nothing"):
        sweep.discover_targets(conformance.REPO_ROOT / "factory" / "targets", "no-such-target")


def test_both_shipped_targets_are_discovered(sweep) -> None:
    stems = [p.stem for p in sweep.discover_targets(
        conformance.REPO_ROOT / "factory" / "targets", None)]
    assert "clickhouse-meshbench" in stems
    assert "rest-openmeteo" in stems
