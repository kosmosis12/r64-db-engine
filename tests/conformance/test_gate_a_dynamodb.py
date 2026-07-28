"""Gate A and scaffold-regeneration proofs for the DynamoDB SourceSpec."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from r64_db_engine.conformance.contract import ASSERTION_CLASSES, run_gate_a
from r64_db_engine.conformance.generator import regenerate
from r64_db_engine.drivers.dynamodb.spec import DYNAMODB_SPEC

_FIXTURES_REF = "r64_db_engine.drivers.dynamodb.spec:DYNAMODB_SPEC"


@pytest.mark.parametrize("class_name", list(ASSERTION_CLASSES))
def test_hand_built_dynamodb_passes_each_gate_a_class(class_name: str) -> None:
    ASSERTION_CLASSES[class_name](DYNAMODB_SPEC)


@pytest.fixture
def regenerated_spec(tmp_path: Path):
    regenerate(DYNAMODB_SPEC, tmp_path, _FIXTURES_REF)
    sys.path.insert(0, str(tmp_path))
    try:
        spec = importlib.import_module("dynamodb_driver.spec").SPEC
        driver = importlib.import_module("dynamodb_driver.driver")
        assert driver.DynamodbDriver.dialect_name() == "dynamodb"
        yield spec
    finally:
        sys.path.remove(str(tmp_path))
        for name in list(sys.modules):
            if name.startswith("dynamodb_driver"):
                del sys.modules[name]


@pytest.mark.parametrize("class_name", list(ASSERTION_CLASSES))
def test_regenerated_dynamodb_passes_each_gate_a_class(
    regenerated_spec, class_name: str
) -> None:
    ASSERTION_CLASSES[class_name](regenerated_spec)


def test_regenerated_report_matches_hand_built(regenerated_spec) -> None:
    hand = run_gate_a(DYNAMODB_SPEC)
    regenerated = run_gate_a(regenerated_spec)
    assert hand.ok, "\n" + hand.as_table()
    assert regenerated.ok, "\n" + regenerated.as_table()
    assert {result.name: result.passed for result in hand.results} == {
        result.name: result.passed for result in regenerated.results
    }


def test_regenerated_behavior_matches_every_fixture(regenerated_spec) -> None:
    leaks: list[str] = []
    for case in DYNAMODB_SPEC.fixture_pack.cases:
        hand_dtype = DYNAMODB_SPEC.pandas_dtype_for(case.source_type, case.raw_value)
        generated_dtype = regenerated_spec.pandas_dtype_for(case.source_type, case.raw_value)
        if hand_dtype != generated_dtype:
            leaks.append(f"{case.name}: dtype hand={hand_dtype} generated={generated_dtype}")
        hand_value = _outcome(DYNAMODB_SPEC, case)
        generated_value = _outcome(regenerated_spec, case)
        if hand_value != generated_value:
            leaks.append(f"{case.name}: value hand={hand_value} generated={generated_value}")
    assert not leaks, "behavioral leaks:\n  " + "\n  ".join(leaks)


def _outcome(spec, case):
    try:
        return "value", spec.coerce_value(case.raw_value, case.source_type)
    except Exception as exc:  # noqa: BLE001 - exception identity is the proof
        return "raise", type(exc).__name__
