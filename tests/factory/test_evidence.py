"""The evidence pack is the review artifact (Law 2), so it is tested like one.

The bar these tests hold the pack to: a reviewer must be able to ratify a
driver from the `.md` alone. That means the pack has to carry BOTH sides of
every comparison — including the passing ones, because "no failures" and
"nothing was checked" must never render identically — and it must not be
possible for a failing check to produce a passing verdict line.
"""

from __future__ import annotations

import json
from pathlib import Path

from factory import evidence
from factory.battery import FAIL, PASS, SKIPPED, CheckResult, Comparison


def _pack(checks=None):
    return evidence.build_pack(
        dialect="clickhouse",
        table="perf_1m",
        source="meshbench.perf_1m",
        checks=checks
        if checks is not None
        else [
            CheckResult(
                "schema_exactness",
                PASS,
                detail="14 columns",
                comparisons=[Comparison("type[row_id]", "int64", "int64", True)],
            ),
            CheckResult(
                "b2_boundary",
                FAIL,
                detail="bounds drifted",
                comparisons=[
                    Comparison("event_time: max", "2026-06-29 16:59:30", "2026-06-29 23:59:30", False)
                ],
                queries=["SELECT toString(max(event_time)) FROM meshbench.perf_1m"],
            ),
            CheckResult("zero_copy_serve_gate", SKIPPED, detail="not requested"),
        ],
        artifact={"sha256_pull1": "abc", "rows": 1000000},
        invocation={"serve_gate": False},
    )


def test_verdict_is_fail_if_any_check_failed() -> None:
    assert _pack().verdict == FAIL


def test_tally_counts_each_status() -> None:
    assert _pack().tally == {PASS: 1, FAIL: 1, SKIPPED: 1}


def test_markdown_leads_with_the_verdict() -> None:
    md = evidence.render_markdown(_pack())
    head = md.splitlines()[:4]
    assert any("VERDICT: FAIL" in line for line in head)


def test_markdown_records_both_sides_of_a_passing_comparison() -> None:
    """A pack that showed only failures would let a reviewer confirm nothing."""
    md = evidence.render_markdown(_pack())
    assert "type[row_id]" in md
    assert md.count("int64") >= 2


def test_markdown_records_the_source_queries_issued() -> None:
    md = evidence.render_markdown(_pack())
    assert "SELECT toString(max(event_time))" in md


def test_markdown_marks_a_failure_visibly() -> None:
    md = evidence.render_markdown(_pack())
    assert "**MISMATCH**" in md
    assert "**FAIL**" in md


def test_markdown_cells_cannot_break_the_table() -> None:
    """A pipe or a newline inside a compared value must not silently eat the
    rest of the row — a mangled table is a pack that hides a comparison."""
    pack = _pack(
        [
            CheckResult(
                "x",
                FAIL,
                comparisons=[Comparison("weird", "a|b\nc", "d|e", False)],
            )
        ]
    )
    md = evidence.render_markdown(pack)
    # Select the COMPARISON row by its escaped value, not by the label: a
    # failing check now also names its failing comparisons in the detail line,
    # so the label alone matches the summary row too.
    row = next(line for line in md.splitlines() if "a\\|b" in line and line.startswith("|"))
    # The embedded pipes must be escaped and the newline flattened, so the row
    # still has exactly the five cells the header declares (six delimiters).
    assert "\\|" in row
    assert "\n" not in row
    assert row.replace("\\|", "").count("|") == 6


def test_write_pack_emits_both_forms_with_the_same_stem(tmp_path: Path) -> None:
    json_path, md_path = evidence.write_pack(_pack(), tmp_path, date="20260816")
    assert json_path.name == "EVIDENCE-clickhouse-20260816.json"
    assert md_path.name == "EVIDENCE-clickhouse-20260816.md"
    assert json_path.exists() and md_path.exists()


def test_json_form_is_machine_readable_and_carries_every_comparison(tmp_path: Path) -> None:
    json_path, _ = evidence.write_pack(_pack(), tmp_path, date="20260816")
    doc = json.loads(json_path.read_text())
    assert doc["verdict"] == FAIL
    assert [c["name"] for c in doc["checks"]] == [
        "schema_exactness",
        "b2_boundary",
        "zero_copy_serve_gate",
    ]
    b2 = doc["checks"][1]
    assert b2["comparisons"][0]["actual"] == "2026-06-29 16:59:30"
    assert b2["comparisons"][0]["expected"] == "2026-06-29 23:59:30"


def test_environment_records_what_could_change_the_artifact() -> None:
    env = evidence.collect_environment()
    assert env["python"]
    # pyarrow owns the block layout and pandas decides string width (B-3); a
    # pack that did not pin them could not explain a cross-run difference.
    assert env["packages"]["pyarrow"] not in ("", "<not installed>")
    assert env["packages"]["pandas"] not in ("", "<not installed>")
    assert "commit" in env["git"]


def test_missing_package_is_reported_not_crashed(monkeypatch) -> None:
    """An optional dep absent from the venv must land as a recorded fact."""
    assert evidence._package_version("definitely_not_installed_xyz") == "<not installed>"
