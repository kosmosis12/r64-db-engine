"""PostgreSQL source-capability spec.

This is pg signing the Gate A conformance contract. It is the single artifact
the scaffold generator consumes to regenerate the pg driver, and the fixture
pack the contract asserts against. Everything here is data + references to pg's
existing coercion entrypoints — no new behavior.

The fixture pack is the DB-free stand-in for the testcontainers edge cases:
each `raw_value` is exactly what psycopg hands back (a `Decimal`, a tz-aware
`datetime`, `bytes`, a `timedelta`, ...), so the contract reproduces the
integration fidelity assertions without a live Postgres.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from r64_db_engine.conformance.coercers import (
    NumericPrecisionLossError,
    Row64CodecOverflowError,
)
from r64_db_engine.conformance.spec import (
    FixtureCase,
    FixturePack,
    PushdownStub,
    SourceSpec,
    WatermarkSpec,
)
from r64_db_engine.drivers.postgres import coercion as pg_coercion

# Native type -> canonical coercer key (conformance.coercers.REGISTRY). Mirrors
# the pg value-dispatch table; the generator wires the regenerated driver's
# coerce_value through this map so the proof exercises real value coercion.
PG_COERCER_MAP: dict[str, str] = {
    # integers
    "smallint": "int", "int2": "int", "integer": "int", "int": "int",
    "int4": "int", "bigint": "int", "int8": "int", "smallserial": "int",
    "serial": "int", "bigserial": "int", "oid": "int",
    # floats
    "real": "float", "float4": "float", "double precision": "float", "float8": "float",
    # numerics
    "numeric": "numeric", "decimal": "numeric",
    # strings
    "text": "str", "varchar": "str", "character varying": "str", "char": "str",
    "character": "str", "bpchar": "str", "name": "str", "citext": "str",
    # bools
    "boolean": "bool", "bool": "bool",
    # date / time
    "date": "date",
    "timestamp": "timestamp", "timestamp without time zone": "timestamp",
    "timestamptz": "timestamp", "timestamp with time zone": "timestamp",
    "time": "time", "time without time zone": "time",
    "timetz": "time", "time with time zone": "time",
    "interval": "interval",
    # uuid
    "uuid": "uuid",
    # json / bytea
    "json": "json", "jsonb": "json", "bytea": "bytea",
    # network / fts / geometry / xml / ranges -> str
    "inet": "str", "cidr": "str", "macaddr": "str", "macaddr8": "str",
    "tsvector": "str", "tsquery": "str", "geometry": "str", "geography": "str",
    "xml": "str", "int4range": "str", "int8range": "str", "numrange": "str",
    "tsrange": "str", "tstzrange": "str", "daterange": "str",
}


# The codec lane: row64tools 1.0.x narrows int64 -> signed int32 on store. Any
# integer-lane value beyond this is the founding overflow template.
_INT32_MAX = 2**31 - 1


def _fixture_pack() -> FixturePack:
    return FixturePack(
        cases=[
            # ---- TYPE_MAP + RAMDB representatives, one per coercion class ----
            FixtureCase("bigint_ok", "bigint", 42, "int64", expected_coerced=42),
            FixtureCase("double_ok", "double precision", 1.5, "float64",
                        expected_coerced=1.5),
            FixtureCase("numeric_ok", "numeric(20,5)", Decimal("3.14"), "float64",
                        expected_coerced=3.14),
            FixtureCase("text_ok", "text", "hello", "string", expected_coerced="hello"),
            FixtureCase("bool_ok", "boolean", True, "bool", expected_coerced=True),
            FixtureCase("uuid_ok", "uuid",
                        uuid.UUID("12345678-1234-5678-1234-567812345678"), "string",
                        expected_coerced="12345678-1234-5678-1234-567812345678"),
            FixtureCase("jsonb_ok", "jsonb", {"a": 1, "b": [2, 3]}, "string",
                        expected_coerced='{"a":1,"b":[2,3]}'),
            FixtureCase("array_ok", "integer[]", [1, 2, 3], "string",
                        expected_coerced="[1,2,3]"),
            FixtureCase("bytea_ok", "bytea", b"\x01\x02\xff", "string",
                        expected_coerced="0102ff"),
            FixtureCase("inet_ok", "inet", "192.168.0.1", "string",
                        expected_coerced="192.168.0.1"),
            FixtureCase("interval_ok", "interval",
                        dt.timedelta(seconds=30, microseconds=500), "int64",
                        expected_coerced=30_000_500),
            # ---- TZ / temporal ----
            FixtureCase("date_ok", "date", dt.date(2026, 5, 11), "datetime64[ns]",
                        expected_coerced=dt.datetime(2026, 5, 11)),
            FixtureCase("timestamp_naive", "timestamp",
                        dt.datetime(2026, 5, 11, 18, 23, 45), "datetime64[ns]",
                        expected_coerced=dt.datetime(2026, 5, 11, 18, 23, 45)),
            FixtureCase("timestamptz_utc", "timestamptz",
                        dt.datetime(2026, 5, 11, 12, 0, 0,
                                    tzinfo=dt.timezone(dt.timedelta(hours=2))),
                        "datetime64[ns]",
                        expected_coerced=dt.datetime(2026, 5, 11, 10, 0, 0)),
            FixtureCase("time_ok", "time", dt.time(14, 30, 5), "string",
                        expected_coerced="14:30:05"),
            # ---- WIDTH / overflow (founding template, both lanes) ----
            # bigint coerces fine, rejected on the way into the int32 codec lane.
            FixtureCase("bigint_over_int32", "bigint", 3_548_933_426, "int64",
                        roundtrip=False, raises=Row64CodecOverflowError,
                        raises_stage="write"),
            # interval µs exceeds int32 -> rejected at the coercer.
            FixtureCase("interval_over_int32", "interval",
                        dt.timedelta(seconds=3548, microseconds=933426), "int64",
                        roundtrip=False, raises=Row64CodecOverflowError,
                        raises_stage="coerce"),
            # numeric that cannot survive Decimal -> float64 -> Decimal.
            FixtureCase("numeric_precision_loss", "numeric(38,15)",
                        Decimal("12345678901234567890.123456789012345"), "float64",
                        roundtrip=False, raises=NumericPrecisionLossError,
                        raises_stage="coerce"),
        ]
    )


POSTGRES_SPEC = SourceSpec(
    dialect="postgres",
    type_map=dict(pg_coercion.PG_TYPE_TO_PANDAS),
    widths={"int": _INT32_MAX},
    watermark=WatermarkSpec(
        cursor_types=("timestamp", "timestamptz", "bigint", "integer", "smallint"),
        monotonic=True,
    ),
    fixture_pack=_fixture_pack(),
    array_dtype="string",
    unknown_dtype="string",
    coercer_map=PG_COERCER_MAP,
    array_coercer="array",
    coerce_value=pg_coercion.coerce_value,
    pandas_dtype_for=pg_coercion.pandas_dtype_for,
    pushdown=PushdownStub(
        supported=(),
        notes="pg predicate/limit pushdown deferred to Gate B",
    ),
)


__all__ = ["POSTGRES_SPEC", "PG_COERCER_MAP"]
