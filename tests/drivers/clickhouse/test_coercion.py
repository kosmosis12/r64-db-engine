"""ClickHouse coercion tests.

Every type from references/coercion-clickhouse.md is covered here. Unsupported
types are covered by explicit error assertions.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from decimal import Decimal

import pytest

from r64_db_engine.drivers.clickhouse.coercion import (
    CLICKHOUSE_TYPE_TO_PANDAS,
    UnsupportedClickHouseType,
    base_type,
    coerce_value,
    is_orderable_type,
    pandas_dtype_for,
)


@pytest.mark.parametrize(
    ("source_type", "expected_dtype"),
    [
        ("Nullable(Int32)", "int64"),
        ("LowCardinality(String)", "string"),
        ("Nullable(LowCardinality(String))", "string"),
        ("DateTime64(3, 'UTC')", "datetime64[ns]"),
        ("DateTime('America/Los_Angeles')", "datetime64[ns]"),
        ("Date", "datetime64[ns]"),
        ("Date32", "datetime64[ns]"),
        ("Decimal(18, 4)", "float64"),
        ("Decimal32(2)", "float64"),
        ("Decimal64(4)", "float64"),
        ("Decimal128(8)", "float64"),
        ("Decimal256(12)", "float64"),
        ("Int8", "int64"),
        ("Int16", "int64"),
        ("Int32", "int64"),
        ("Int64", "int64"),
        ("UInt8", "int64"),
        ("UInt16", "int64"),
        ("UInt32", "int64"),
        ("Float32", "float64"),
        ("Float64", "float64"),
        ("String", "string"),
        ("FixedString(16)", "string"),
        ("UUID", "string"),
        ("Enum8('draft' = 1, 'paid' = 2)", "string"),
        ("Enum16('draft' = 1, 'paid' = 2)", "string"),
        ("Array(Int32)", "string"),
        ("Bool", "bool"),
    ],
)
def test_pandas_dtype_for_known_types(source_type: str, expected_dtype: str) -> None:
    assert pandas_dtype_for(source_type) == expected_dtype


@pytest.mark.parametrize("source_type", ["Map(String, UInt64)", "Tuple(String, Int32)"])
def test_pandas_dtype_for_complex_unsupported_types(source_type: str) -> None:
    with pytest.raises(UnsupportedClickHouseType):
        pandas_dtype_for(source_type)


@pytest.mark.parametrize("source_type", ["Int128", "Int256", "UInt64", "UInt128", "UInt256"])
def test_pandas_dtype_for_overflow_risk_integer_types(source_type: str) -> None:
    with pytest.raises(UnsupportedClickHouseType):
        pandas_dtype_for(source_type)


def test_pandas_dtype_for_case_insensitive_and_unknown() -> None:
    assert pandas_dtype_for("nullable(datetime64(6))") == "datetime64[ns]"
    assert pandas_dtype_for("IPv4") == "string"


def test_base_type_unwraps_wrappers() -> None:
    assert base_type("Nullable(LowCardinality(Enum8('a' = 1)))") == "Enum8"


def test_coerce_nullable_none_passes_through() -> None:
    assert coerce_value(None, "Nullable(Int32)") is None


@pytest.mark.parametrize("source_type", ["Int8", "Int16", "Int32", "Int64", "UInt8", "UInt16", "UInt32"])
def test_coerce_integer(source_type: str) -> None:
    assert coerce_value("42", source_type) == 42
    assert coerce_value(-1, source_type) == -1


@pytest.mark.parametrize("source_type", ["Float32", "Float64"])
def test_coerce_float(source_type: str) -> None:
    assert coerce_value("1.5", source_type) == 1.5
    assert isinstance(coerce_value(1, source_type), float)


def test_coerce_decimal_to_float() -> None:
    out = coerce_value(Decimal("123.45"), "Decimal(9, 2)")
    assert out == pytest.approx(123.45)
    assert isinstance(out, float)


def test_coerce_decimal_high_precision_warns(caplog: pytest.LogCaptureFixture) -> None:
    huge = Decimal("123456789012345678901234567890.123456789")
    with caplog.at_level("WARNING"):
        coerce_value(huge, "Decimal256(9)")
    assert any("precision loss" in r.message for r in caplog.records)


def test_coerce_datetime64_timezone_to_utc_naive() -> None:
    tzinfo = dt.timezone(dt.timedelta(hours=2))
    value = dt.datetime(2026, 5, 23, 12, 0, 0, 123456, tzinfo=tzinfo)
    out = coerce_value(value, "DateTime64(6, 'Europe/Berlin')")
    assert out == dt.datetime(2026, 5, 23, 10, 0, 0, 123456)
    assert out.tzinfo is None


def test_coerce_datetime_string() -> None:
    out = coerce_value("2026-05-23T10:30:00Z", "DateTime('UTC')")
    assert out == dt.datetime(2026, 5, 23, 10, 30)


def test_coerce_date() -> None:
    assert coerce_value(dt.date(2026, 5, 23), "Date") == dt.datetime(2026, 5, 23)


def test_coerce_date32() -> None:
    assert coerce_value("1925-01-01", "Date32") == dt.datetime(1925, 1, 1)


@pytest.mark.parametrize("source_type", ["String", "LowCardinality(String)"])
def test_coerce_string(source_type: str) -> None:
    assert coerce_value(123, source_type) == "123"


def test_coerce_fixed_string_strips_nul_padding() -> None:
    assert coerce_value(b"abc\x00\x00", "FixedString(5)") == "abc"


def test_coerce_uuid() -> None:
    value = uuid.UUID("12345678-1234-5678-1234-567812345678")
    assert coerce_value(value, "UUID") == "12345678-1234-5678-1234-567812345678"


@pytest.mark.parametrize("source_type", ["Enum8('draft' = 1)", "Enum16('paid' = 2)"])
def test_coerce_enum_to_label_string(source_type: str) -> None:
    assert coerce_value("draft", source_type) == "draft"


def test_coerce_array_to_compact_json() -> None:
    out = coerce_value([1, 2, 3], "Array(Int32)")
    assert json.loads(out) == [1, 2, 3]
    assert " " not in out


@pytest.mark.parametrize("source_type", ["Map(String, UInt64)", "Tuple(String, Int32)"])
def test_coerce_unsupported_complex_type_raises(source_type: str) -> None:
    with pytest.raises(UnsupportedClickHouseType):
        coerce_value({"a": 1}, source_type)


@pytest.mark.parametrize("source_type", ["Int128", "Int256", "UInt64", "UInt128", "UInt256"])
def test_coerce_overflow_risk_integer_type_raises(source_type: str) -> None:
    with pytest.raises(UnsupportedClickHouseType):
        coerce_value(1, source_type)


@pytest.mark.parametrize("value", [True, False, "true", "0", 1])
def test_coerce_bool(value: object) -> None:
    out = coerce_value(value, "Bool")
    assert isinstance(out, bool)


def test_orderable_type_helper() -> None:
    assert is_orderable_type("Nullable(DateTime64(3))")
    assert not is_orderable_type("Array(Int32)")


def test_every_clickhouse_type_present_in_dtype_map() -> None:
    required = {
        "Nullable",
        "LowCardinality",
        "DateTime64",
        "DateTime",
        "Date",
        "Date32",
        "Decimal",
        "Decimal32",
        "Decimal64",
        "Decimal128",
        "Decimal256",
        "Int8",
        "Int16",
        "Int32",
        "Int64",
        "Int128",
        "Int256",
        "UInt8",
        "UInt16",
        "UInt32",
        "UInt64",
        "UInt128",
        "UInt256",
        "Float32",
        "Float64",
        "String",
        "FixedString",
        "UUID",
        "Enum8",
        "Enum16",
        "Array",
        "Map",
        "Tuple",
        "Bool",
    }
    wrapper_types = {"Nullable", "LowCardinality"}
    missing = required - wrapper_types - set(CLICKHOUSE_TYPE_TO_PANDAS)
    assert not missing, f"ClickHouse types missing from dtype map: {missing}"
