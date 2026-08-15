"""DynamoDB coercion tests against real boto3 deserializer outputs."""

from __future__ import annotations

from decimal import Decimal

import pytest
from boto3.dynamodb.types import TypeDeserializer

from r64_db_engine.drivers.dynamodb import coercion

deserialize = TypeDeserializer().deserialize


@pytest.mark.parametrize(
    ("source_type", "attribute", "expected_dtype", "expected"),
    [
        ("S", {"S": "em—dash"}, "string", "em—dash"),
        ("N", {"N": "42"}, "int64", 42),
        ("N", {"N": "3.125"}, "float64", 3.125),
        ("BOOL", {"BOOL": True}, "bool", True),
        ("NULL", {"NULL": True}, "string", None),
        ("B", {"B": b"\x01\x02\xff"}, "string", "0102ff"),
        (
            "M",
            {"M": {"z": {"N": "2"}, "a": {"L": [{"BOOL": True}]}}},
            "string",
            '{"a":[true],"z":2}',
        ),
        ("L", {"L": [{"N": "1"}, {"S": "x"}]}, "string", '[1,"x"]'),
        ("SS", {"SS": ["z", "a", "m"]}, "string", '["a","m","z"]'),
        ("NS", {"NS": ["10", "2", "1"]}, "string", "[1,10,2]"),
        ("BS", {"BS": [b"\xff", b"\x01"]}, "string", '["01","ff"]'),
    ],
)
def test_deserialized_type_mapping(
    source_type: str,
    attribute: dict[str, object],
    expected_dtype: str,
    expected: object,
) -> None:
    value = deserialize(attribute)
    assert coercion.pandas_dtype_for(source_type, value) == expected_dtype
    assert coercion.coerce_value(value, source_type) == expected


def test_null_passthrough_for_every_type() -> None:
    for source_type in coercion.DYNAMODB_TYPES:
        assert coercion.coerce_value(None, source_type) is None


def test_integral_numeric_above_codec_lane_remains_int_for_writer_guard() -> None:
    value = deserialize({"N": str(2**31)})
    assert coercion.pandas_dtype_for("N", value) == "int64"
    assert coercion.coerce_value(value, "N") == 2**31


def test_integral_numeric_outside_int64_raises() -> None:
    value = deserialize({"N": str(2**63)})
    with pytest.raises(coercion.Row64CodecOverflowError, match="outside signed int64"):
        coercion.coerce_value(value, "N")


def test_fractional_numeric_precision_loss_raises() -> None:
    value = deserialize({"N": "12345678901234567890.123456789"})
    with pytest.raises(coercion.NumericPrecisionLossError, match="cannot round-trip exactly"):
        coercion.coerce_value(value, "N")


def test_nested_decimal_precision_loss_raises() -> None:
    value = deserialize({"M": {"bad": {"N": "12345678901234567890.123456789"}}})
    with pytest.raises(coercion.NumericPrecisionLossError, match="cannot round-trip exactly"):
        coercion.coerce_value(value, "M")


def test_schema_only_numeric_dtype_uses_declared_fallback() -> None:
    assert coercion.pandas_dtype_for("N") == "float64"


def test_unknown_type_has_safe_string_fallback() -> None:
    assert coercion.pandas_dtype_for("future") == "string"
    assert coercion.coerce_value(Decimal("1.5"), "future") == "1.5"


def test_mapping_completeness_guard() -> None:
    """Every native type has exactly one dtype and coercer decision."""
    expected = {"s", "n", "bool", "null", "b", "m", "l", "ss", "ns", "bs"}
    assert expected == coercion.DYNAMODB_TYPES
    assert set(coercion.DYNAMODB_TYPE_TO_PANDAS) == expected
    assert set(coercion.DYNAMODB_COERCER_MAP) == expected
    assert set(coercion.DYNAMODB_DTYPE_RESOLVER_MAP) <= expected
    assert set(coercion.DYNAMODB_COERCER_MAP.values()) <= set(coercion.coercers.REGISTRY)
    assert set(coercion.DYNAMODB_DTYPE_RESOLVER_MAP.values()) <= set(
        coercion.coercers.DTYPE_RESOLVERS
    )
