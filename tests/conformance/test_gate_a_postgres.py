"""Gate A run against the hand-built Postgres driver.

This is the abstracted, DB-free version of the fidelity assertions that
previously lived behind `--integration` in tests/drivers/postgres/test_driver.py
(type round-trips, the numeric precision-loss reject, and the int32 codec-width
founding template). Each assertion class is parametrized so a failure names the
exact class that leaked.
"""

from __future__ import annotations

import pytest

from r64_db_engine.conformance.contract import ASSERTION_CLASSES, run_gate_a
from r64_db_engine.drivers.postgres.spec import POSTGRES_SPEC


@pytest.mark.parametrize("class_name", list(ASSERTION_CLASSES))
def test_gate_a_class_passes_for_hand_built_pg(class_name: str) -> None:
    ASSERTION_CLASSES[class_name](POSTGRES_SPEC)


def test_gate_a_report_all_green() -> None:
    report = run_gate_a(POSTGRES_SPEC)
    assert report.ok, "\n" + report.as_table()


def test_fixture_pack_covers_every_assertion_class() -> None:
    """The pack must exercise each class, or Gate A passes vacuously."""
    pack = POSTGRES_SPEC.fixture_pack
    assert pack.overflow_cases, "no WIDTH/overflow fixtures"
    assert pack.roundtrip_cases, "no RAMDB round-trip fixtures"
    assert any(c.expected_dtype == "datetime64[ns]" for c in pack.cases), "no TZ fixtures"
    assert any(c.nullable for c in pack.cases), "no NULL fixtures"


def test_founding_template_present_both_lanes() -> None:
    """The int32 codec-width case must be caught at both the coercer and the
    writer — the abstraction's reason for existing."""
    pack = POSTGRES_SPEC.fixture_pack
    stages = {c.raises_stage for c in pack.overflow_cases if c.expected_dtype == "int64"}
    assert {"coerce", "write"} <= stages, f"missing a codec lane: {stages}"
