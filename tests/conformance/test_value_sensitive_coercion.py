"""Framework proofs for value-sensitive dtypes and deterministic containers."""

from __future__ import annotations

import dataclasses
import importlib
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from r64_db_engine.conformance import coercers
from r64_db_engine.conformance.coercers import NumericPrecisionLossError
from r64_db_engine.conformance.generator import regenerate
from r64_db_engine.drivers.postgres.spec import POSTGRES_SPEC


class BytesWrapper:
    def __init__(self, value: bytes) -> None:
        self.value = value

    def __bytes__(self) -> bytes:
        return self.value


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("0"), "int64"),
        (Decimal(str(2**63 - 1)), "int64"),
        (Decimal(str(-(2**63))), "int64"),
        (Decimal("3.125"), "float64"),
    ],
)
def test_decimal_numeric_dtype(value: Decimal, expected: str) -> None:
    assert coercers.decimal_numeric_dtype(value) == expected


def test_decimal_numeric_dtype_uses_float_fallback_outside_int64() -> None:
    assert coercers.decimal_numeric_dtype(Decimal(str(2**63))) == "float64"


def test_decimal_number_rejects_fractional_precision_loss() -> None:
    with pytest.raises(NumericPrecisionLossError, match="cannot round-trip exactly"):
        coercers.to_decimal_number(Decimal("12345678901234567890.123456789"))


def test_deterministic_json_sorts_keys_and_preserves_nested_decimal() -> None:
    value = {"z": [Decimal("2"), {"b": Decimal("3.125")}], "a": True}
    assert coercers.to_deterministic_json(value) == '{"a":true,"z":[2,{"b":3.125}]}'


def test_deterministic_set_json_sorts_scalars() -> None:
    assert coercers.to_deterministic_set_json({"z", "a", "m"}) == '["a","m","z"]'
    assert coercers.to_deterministic_set_json(
        {Decimal("10"), Decimal("2"), Decimal("1")}
    ) == "[1,10,2]"


def test_deterministic_set_json_encodes_binary_as_hex() -> None:
    assert coercers.to_deterministic_set_json({b"\xff", b"\x01"}) == '["01","ff"]'


def test_bytes_protocol_is_canonical_binary_input() -> None:
    wrapped = BytesWrapper(b"\x01\xff")
    assert coercers.to_bytea(wrapped) == "01ff"
    assert coercers.to_deterministic_json({"value": wrapped}) == '{"value":"01ff"}'


def test_deterministic_set_json_rejects_ordered_collection() -> None:
    with pytest.raises(TypeError, match="expected set or frozenset"):
        coercers.to_deterministic_set_json(["a", "b"])


def test_generated_scaffold_dispatches_declared_dtype_resolver(tmp_path: Path) -> None:
    spec = dataclasses.replace(
        POSTGRES_SPEC,
        dialect="decimal_fixture",
        type_map={**dict(POSTGRES_SPEC.type_map), "number": "float64"},
        coercer_map={**dict(POSTGRES_SPEC.coercer_map), "number": "decimal_number"},
        dtype_resolver_map={"number": "decimal_numeric"},
    )
    regenerate(spec, tmp_path, "r64_db_engine.drivers.postgres.spec:POSTGRES_SPEC")
    sys.path.insert(0, str(tmp_path))
    try:
        generated = importlib.import_module("decimal_fixture_driver.coercion")
        assert generated.pandas_dtype_for("number") == "float64"
        assert generated.pandas_dtype_for("number", Decimal("4")) == "int64"
        assert generated.pandas_dtype_for("number", Decimal("4.5")) == "float64"
    finally:
        sys.path.remove(str(tmp_path))
        for name in list(sys.modules):
            if name.startswith("decimal_fixture_driver"):
                del sys.modules[name]


def test_generated_scaffold_rejects_missing_dtype_resolver(tmp_path: Path) -> None:
    spec = dataclasses.replace(
        POSTGRES_SPEC,
        dialect="bad_resolver",
        dtype_resolver_map={"numeric": "missing"},
    )
    regenerate(spec, tmp_path, "r64_db_engine.drivers.postgres.spec:POSTGRES_SPEC")
    sys.path.insert(0, str(tmp_path))
    try:
        generated = importlib.import_module("bad_resolver_driver.coercion")
        with pytest.raises(ValueError, match="not in coercers.DTYPE_RESOLVERS"):
            generated.pandas_dtype_for("numeric", Decimal("1"))
    finally:
        sys.path.remove(str(tmp_path))
        for name in list(sys.modules):
            if name.startswith("bad_resolver_driver"):
                del sys.modules[name]
