"""Per-type coercion tests covering every row of SPEC §6.1.

This is the gate: every type in the §6.1 table has at least one unit
test pinning the (source psycopg-return-shape -> pandas dtype + value)
mapping. If you change PG_TYPE_TO_PANDAS or coerce_value, these break.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from decimal import Decimal

import pytest

from r64_db_engine.drivers.postgres.coercion import (
    PG_TYPE_TO_PANDAS,
    NumericPrecisionLossError,
    coerce_value,
    pandas_dtype_for,
)

# ----------------------------------------------------------------------
# pandas_dtype_for: every row of §6.1 mapped correctly.
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source_type", "expected_dtype"),
    [
        # integers
        ("smallint", "int64"),
        ("integer", "int64"),
        ("bigint", "int64"),
        ("int4", "int64"),
        ("int8", "int64"),
        ("serial", "int64"),
        ("bigserial", "int64"),
        # floats
        ("real", "float64"),
        ("double precision", "float64"),
        # numeric / decimal
        ("numeric", "float64"),
        ("numeric(20,5)", "float64"),
        ("decimal(10,2)", "float64"),
        # strings
        ("text", "string"),
        ("varchar", "string"),
        ("varchar(64)", "string"),
        ("character varying", "string"),
        ("char", "string"),
        ("bpchar", "string"),
        # boolean
        ("boolean", "bool"),
        ("bool", "bool"),
        # date / time / interval
        ("date", "datetime64[ns]"),
        ("timestamp", "datetime64[ns]"),
        ("timestamp without time zone", "datetime64[ns]"),
        ("timestamptz", "datetime64[ns]"),
        ("timestamp with time zone", "datetime64[ns]"),
        ("time", "string"),
        ("timetz", "string"),
        ("interval", "int64"),
        # uuid
        ("uuid", "string"),
        # json / jsonb
        ("json", "string"),
        ("jsonb", "string"),
        # bytea
        ("bytea", "string"),
        # arrays
        ("integer[]", "string"),
        ("text[]", "string"),
        # network
        ("inet", "string"),
        ("cidr", "string"),
        ("macaddr", "string"),
        # full text search
        ("tsvector", "string"),
        ("tsquery", "string"),
        # geometry
        ("geometry", "string"),
        ("geography", "string"),
        # xml
        ("xml", "string"),
        # ranges
        ("int4range", "string"),
        ("int8range", "string"),
        ("numrange", "string"),
        ("tsrange", "string"),
        ("tstzrange", "string"),
        ("daterange", "string"),
    ],
)
def test_pandas_dtype_for_known_types(source_type: str, expected_dtype: str) -> None:
    assert pandas_dtype_for(source_type) == expected_dtype


def test_pandas_dtype_for_uppercase_is_case_insensitive():
    assert pandas_dtype_for("BIGINT") == "int64"
    assert pandas_dtype_for("Timestamp Without Time Zone") == "datetime64[ns]"


def test_pandas_dtype_for_unknown_falls_back_to_string():
    assert pandas_dtype_for("hstore") == "string"
    assert pandas_dtype_for("some_user_defined_type") == "string"


# ----------------------------------------------------------------------
# coerce_value: per-type behaviour from §6.1.
# ----------------------------------------------------------------------


@pytest.mark.parametrize("source_type", ["smallint", "integer", "bigint", "int4", "int8"])
def test_coerce_integer(source_type: str) -> None:
    assert coerce_value(42, source_type) == 42
    assert coerce_value(0, source_type) == 0
    assert coerce_value(-1, source_type) == -1


@pytest.mark.parametrize("source_type", ["real", "double precision", "float4", "float8"])
def test_coerce_float(source_type: str) -> None:
    assert coerce_value(1.5, source_type) == 1.5
    assert isinstance(coerce_value(1, source_type), float)


def test_coerce_numeric_from_decimal() -> None:
    out = coerce_value(Decimal("3.14"), "numeric")
    assert out == pytest.approx(3.14)
    assert isinstance(out, float)


def test_coerce_numeric_high_precision_raises() -> None:
    huge = Decimal("12345678901234567890.123456789012345")
    with pytest.raises(NumericPrecisionLossError, match="cannot round-trip exactly"):
        coerce_value(huge, "numeric(38,15)")


@pytest.mark.parametrize("source_type", ["text", "varchar", "char", "bpchar", "name", "citext"])
def test_coerce_text(source_type: str) -> None:
    assert coerce_value("hello", source_type) == "hello"
    assert coerce_value(123, source_type) == "123"


@pytest.mark.parametrize("source_type", ["boolean", "bool"])
def test_coerce_boolean(source_type: str) -> None:
    assert coerce_value(True, source_type) is True
    assert coerce_value(False, source_type) is False
    assert coerce_value("t", source_type) is True
    assert coerce_value("false", source_type) is False
    assert coerce_value(1, source_type) is True


def test_coerce_date_from_python_date() -> None:
    d = dt.date(2026, 5, 11)
    out = coerce_value(d, "date")
    assert out == dt.datetime(2026, 5, 11)


def test_coerce_timestamp_naive() -> None:
    ts = dt.datetime(2026, 5, 11, 18, 23, 45)
    assert coerce_value(ts, "timestamp") == ts


def test_coerce_timestamptz_converts_to_utc_naive() -> None:
    tzinfo = dt.timezone(dt.timedelta(hours=2))
    ts = dt.datetime(2026, 5, 11, 12, 0, 0, tzinfo=tzinfo)
    out = coerce_value(ts, "timestamptz")
    assert out == dt.datetime(2026, 5, 11, 10, 0, 0)
    assert out.tzinfo is None


def test_coerce_time_serializes_to_iso_string() -> None:
    t = dt.time(14, 30, 5)
    assert coerce_value(t, "time") == "14:30:05"


def test_coerce_interval_to_microseconds() -> None:
    td = dt.timedelta(seconds=30, microseconds=500)
    out = coerce_value(td, "interval")
    assert out == 30_000_000 + 500
    assert isinstance(out, int)


def test_coerce_uuid_str() -> None:
    u = uuid.UUID("12345678-1234-5678-1234-567812345678")
    assert coerce_value(u, "uuid") == "12345678-1234-5678-1234-567812345678"


def test_coerce_jsonb_dict_to_compact_json() -> None:
    out = coerce_value({"k": 1, "nested": [1, 2]}, "jsonb")
    assert json.loads(out) == {"k": 1, "nested": [1, 2]}
    assert " " not in out  # compact, no spaces


def test_coerce_jsonb_list_to_json_string() -> None:
    out = coerce_value([1, 2, 3], "jsonb")
    assert json.loads(out) == [1, 2, 3]


def test_coerce_jsonb_large_value_warns(caplog: pytest.LogCaptureFixture) -> None:
    big = {"data": "x" * (65 * 1024)}
    with caplog.at_level("WARNING"):
        coerce_value(big, "jsonb")
    assert any(">64KB" in r.message for r in caplog.records) or any(
        "64KB" in r.message for r in caplog.records
    )


def test_coerce_bytea_to_hex() -> None:
    out = coerce_value(b"\x01\x02\xff", "bytea")
    assert out == "0102ff"


def test_coerce_bytea_memoryview() -> None:
    out = coerce_value(memoryview(b"\xde\xad\xbe\xef"), "bytea")
    assert out == "deadbeef"


def test_coerce_bytea_large_warns(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING"):
        coerce_value(b"x" * (65 * 1024), "bytea")
    assert any("64KB" in r.message for r in caplog.records)


def test_coerce_array_serializes_to_json() -> None:
    out = coerce_value([1, 2, 3], "integer[]")
    assert json.loads(out) == [1, 2, 3]


def test_coerce_text_array() -> None:
    out = coerce_value(["a", "b", "c"], "text[]")
    assert json.loads(out) == ["a", "b", "c"]


@pytest.mark.parametrize("source_type", ["inet", "cidr", "macaddr"])
def test_coerce_network(source_type: str) -> None:
    assert coerce_value("192.168.0.1", source_type) == "192.168.0.1"


@pytest.mark.parametrize("source_type", ["tsvector", "tsquery"])
def test_coerce_fts(source_type: str) -> None:
    assert coerce_value("'quick' & 'brown'", source_type) == "'quick' & 'brown'"


def test_coerce_geometry_wkt() -> None:
    wkt = "POINT(-122.4194 37.7749)"
    assert coerce_value(wkt, "geometry") == wkt


def test_coerce_xml() -> None:
    assert coerce_value("<a><b/></a>", "xml") == "<a><b/></a>"


def test_coerce_range_int4range() -> None:
    assert coerce_value("[1,10)", "int4range") == "[1,10)"


def test_coerce_value_none_passes_through() -> None:
    assert coerce_value(None, "bigint") is None
    assert coerce_value(None, "text") is None
    assert coerce_value(None, "jsonb") is None


# ----------------------------------------------------------------------
# Spec §6.1 table coverage assertion: every row mapped.
# ----------------------------------------------------------------------


def test_every_spec_61_type_present_in_dtype_map() -> None:
    required = {
        "smallint",
        "integer",
        "bigint",
        "real",
        "double precision",
        "numeric",
        "decimal",
        "text",
        "varchar",
        "char",
        "boolean",
        "date",
        "timestamp",
        "timestamptz",
        "time",
        "timetz",
        "interval",
        "uuid",
        "json",
        "jsonb",
        "bytea",
        "inet",
        "cidr",
        "macaddr",
        "tsvector",
        "tsquery",
        "geometry",
        "xml",
    }
    missing = required - set(PG_TYPE_TO_PANDAS.keys())
    assert not missing, f"types in SPEC §6.1 missing from PG_TYPE_TO_PANDAS: {missing}"
