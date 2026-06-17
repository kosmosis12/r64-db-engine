"""The hard go/no-go: regenerate the pg driver from its own spec and prove the
regenerated driver passes the *same* Gate A assertions as the hand-built one.

If regenerated-pg != hand-built-pg on any fixture or any type_map entry, the
abstraction leaked. This test surfaces exactly which assertion/type-class
diverged so the leak is the loud, actionable output.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from r64_db_engine.conformance.contract import ASSERTION_CLASSES, run_gate_a
from r64_db_engine.conformance.generator import regenerate
from r64_db_engine.drivers.postgres.spec import POSTGRES_SPEC

_FIXTURES_REF = "r64_db_engine.drivers.postgres.spec:POSTGRES_SPEC"


@pytest.fixture
def regenerated_spec(tmp_path: Path):
    """Generate the pg driver into a temp dir, import it, return its SourceSpec."""
    regenerate(POSTGRES_SPEC, tmp_path, _FIXTURES_REF)
    sys.path.insert(0, str(tmp_path))
    # Drop any cached generated modules from a previous run.
    for name in list(sys.modules):
        if name.startswith("postgres_driver"):
            del sys.modules[name]
    try:
        spec = importlib.import_module("postgres_driver.spec").SPEC
        # the skeleton driver must at least import and expose its dialect.
        drv = importlib.import_module("postgres_driver.driver")
        assert drv.PostgresDriver.dialect_name() == "postgres"
        yield spec
    finally:
        sys.path.remove(str(tmp_path))
        for name in list(sys.modules):
            if name.startswith("postgres_driver"):
                del sys.modules[name]


@pytest.mark.parametrize("class_name", list(ASSERTION_CLASSES))
def test_regenerated_pg_passes_each_gate_a_class(regenerated_spec, class_name: str) -> None:
    ASSERTION_CLASSES[class_name](regenerated_spec)


def test_regenerated_report_matches_hand_built(regenerated_spec) -> None:
    hand = run_gate_a(POSTGRES_SPEC)
    regen = run_gate_a(regenerated_spec)
    hand_status = {r.name: r.passed for r in hand.results}
    regen_status = {r.name: r.passed for r in regen.results}
    assert regen_status == hand_status, (
        f"per-class divergence\nhand={hand_status}\nregen={regen_status}\n"
        + regen.as_table()
    )
    assert regen.ok, "\n" + regen.as_table()


def test_no_behavioral_leak_per_fixture(regenerated_spec) -> None:
    """Finer than the class report: diff dtype + coerce outcome on every fixture
    so a silent divergence can't hide behind a passing class."""
    leaks: list[str] = []
    for case in POSTGRES_SPEC.fixture_pack.cases:
        hd = POSTGRES_SPEC.pandas_dtype_for(case.source_type)
        gd = regenerated_spec.pandas_dtype_for(case.source_type)
        if hd != gd:
            leaks.append(f"{case.name}: dtype hand={hd!r} regen={gd!r}")
        if _coerce_outcome(POSTGRES_SPEC, case) != _coerce_outcome(regenerated_spec, case):
            leaks.append(
                f"{case.name}: coerce hand={_coerce_outcome(POSTGRES_SPEC, case)} "
                f"regen={_coerce_outcome(regenerated_spec, case)}"
            )
    assert not leaks, "behavioral leak(s):\n  " + "\n  ".join(leaks)


def test_no_leak_across_full_type_map(regenerated_spec) -> None:
    """Every declared type, not just the fixtures, must map identically."""
    leaks = [
        f"{k}: hand={POSTGRES_SPEC.pandas_dtype_for(k)!r} "
        f"regen={regenerated_spec.pandas_dtype_for(k)!r}"
        for k in POSTGRES_SPEC.type_map
        if POSTGRES_SPEC.pandas_dtype_for(k) != regenerated_spec.pandas_dtype_for(k)
    ]
    assert not leaks, "type_map leak(s):\n  " + "\n  ".join(leaks)


def _coerce_outcome(spec, case):
    try:
        return ("val", spec.coerce_value(case.raw_value, case.source_type))
    except Exception as exc:  # noqa: BLE001 - identity of the raise is the signal
        return ("raise", type(exc).__name__)
