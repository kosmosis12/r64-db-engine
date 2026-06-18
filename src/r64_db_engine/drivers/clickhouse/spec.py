"""ClickHouse source-capability spec (fan-out driver #1, Phase 1: spec only).

Written by analogy to drivers/postgres/spec.py but deliberately NOT inheriting
pg's assumptions. ClickHouse is the stress test for whether the conformance spec
format is source-general or secretly pg-shaped.

Behavioral hooks (`coerce_value`, `pandas_dtype_for`) are intentionally left
unset here — there is no hand-written ClickHouse coercion module. They are wired
to the GENERATED coercion in Phase 2 (the generator emits them from this spec),
exactly the "regenerated spec" pattern proven for pg. So this module is purely
declarative.

⚠️ This spec covers only the ClickHouse types that the CURRENT spec format can
express. The transparent type wrappers `Nullable(T)` and `LowCardinality(T)`
are NOT expressible without a minimal format extension — see the STRAIN REPORT
in the Gate 1 summary and the PENDING-EXTENSION block at the bottom of this file.
Generation must wait on the Gate 1 decision about that extension.
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

# The codec lane is unchanged across sources: row64tools 1.0.11 narrows int64 ->
# signed int32 on store, and RamdbWriter rejects |value| > 2**31-1. This is a
# property of the codec, not of the source, so ClickHouse declares the same lane.
_INT32_MAX = 2**31 - 1


# ---------------------------------------------------------------------------
# type_map: native (normalized, paren-stripped, lowercased) type -> row64 dtype.
#
# The generated normalizer lowercases, strips `(...)` params, and collapses
# whitespace. So Decimal(18,4)->decimal, DateTime64(3,'UTC')->datetime64,
# FixedString(16)->fixedstring, Enum8('a'=1)->enum8, Array(Int32)->array.
# ---------------------------------------------------------------------------
CLICKHOUSE_TYPE_MAP: dict[str, str] = {
    # --- integers that FIT pandas int64 (ride the int lane) ---
    "int8": "int64", "int16": "int64", "int32": "int64", "int64": "int64",
    "uint8": "int64", "uint16": "int64", "uint32": "int64",
    # --- integers that EXCEED pandas int64 -> string (see STRAIN 3) ---
    # UInt64 max 1.8e19 > int64 max 9.2e18; Int128/UInt128/Int256/UInt256 dwarf
    # int64. float64 would lose precision and int64 would overflow, so the only
    # lossless mapping is exact decimal text.
    "uint64": "string",
    "int128": "string", "uint128": "string",
    "int256": "string", "uint256": "string",
    # --- floats ---
    "float32": "float64", "float64": "float64",
    # --- decimal (float64 + precision-loss guard, like pg numeric) ---
    "decimal": "float64",
    # --- bool ---
    "bool": "bool",
    # --- string-family ---
    "string": "string", "fixedstring": "string",
    "enum8": "string", "enum16": "string",
    "uuid": "string",
    "ipv4": "string", "ipv6": "string",
    # --- temporal ---
    "date": "datetime64[ns]", "date32": "datetime64[ns]",
    "datetime": "datetime64[ns]", "datetime64": "datetime64[ns]",
    # --- composite -> JSON string (top-level only; see STRAIN 2) ---
    "array": "string", "map": "string", "tuple": "string",
}


# native (normalized) type -> canonical coercer key in conformance.coercers.REGISTRY.
CLICKHOUSE_COERCER_MAP: dict[str, str] = {
    "int8": "int", "int16": "int", "int32": "int", "int64": "int",
    "uint8": "int", "uint16": "int", "uint32": "int",
    # wide ints stringified losslessly (str(int) -> exact decimal text)
    "uint64": "str", "int128": "str", "uint128": "str",
    "int256": "str", "uint256": "str",
    "float32": "float", "float64": "float",
    "decimal": "numeric",
    "bool": "bool",
    "string": "str", "fixedstring": "str",
    "enum8": "str", "enum16": "str",
    "uuid": "uuid",
    "ipv4": "str", "ipv6": "str",
    "date": "date", "date32": "date",
    "datetime": "timestamp", "datetime64": "timestamp",
    "array": "array", "tuple": "array", "map": "json",
}


def _fixture_pack() -> FixturePack:
    return FixturePack(
        cases=[
            # ---- TYPE_MAP + RAMDB representatives (ride the int lane) ----
            FixtureCase("int32_ok", "Int32", 42, "int64", expected_coerced=42),
            FixtureCase("int64_ok", "Int64", 1000, "int64", expected_coerced=1000),
            FixtureCase("uint16_ok", "UInt16", 65535, "int64", expected_coerced=65535),
            FixtureCase("float64_ok", "Float64", 3.14, "float64", expected_coerced=3.14),
            FixtureCase("float32_ok", "Float32", 1.5, "float64", expected_coerced=1.5),
            FixtureCase("decimal_ok", "Decimal(18,4)", Decimal("3.14"), "float64",
                        expected_coerced=3.14),
            FixtureCase("string_ok", "String", "hello", "string", expected_coerced="hello"),
            FixtureCase("bool_ok", "Bool", True, "bool", expected_coerced=True),
            FixtureCase("uuid_ok", "UUID",
                        uuid.UUID("12345678-1234-5678-1234-567812345678"), "string",
                        expected_coerced="12345678-1234-5678-1234-567812345678"),
            FixtureCase("fixedstring_ok", "FixedString(4)", "abcd", "string",
                        expected_coerced="abcd"),
            FixtureCase("enum8_ok", "Enum8('active'=1,'inactive'=2)", "active", "string",
                        expected_coerced="active"),
            FixtureCase("ipv4_ok", "IPv4", "1.2.3.4", "string", expected_coerced="1.2.3.4"),
            FixtureCase("array_ok", "Array(Int32)", [1, 2, 3], "string",
                        expected_coerced="[1,2,3]"),
            FixtureCase("map_ok", "Map(String, UInt64)", {"a": 1}, "string",
                        expected_coerced='{"a":1}'),
            # ---- TZ / temporal ----
            FixtureCase("date_ok", "Date", dt.date(2026, 5, 11), "datetime64[ns]",
                        expected_coerced=dt.datetime(2026, 5, 11)),
            FixtureCase("datetime_ok", "DateTime",
                        dt.datetime(2026, 5, 11, 18, 23, 45), "datetime64[ns]",
                        expected_coerced=dt.datetime(2026, 5, 11, 18, 23, 45)),
            FixtureCase("datetime64_tz", "DateTime64(3, 'UTC')",
                        dt.datetime(2026, 5, 11, 12, 0, 0,
                                    tzinfo=dt.timezone(dt.timedelta(hours=2))),
                        "datetime64[ns]",
                        expected_coerced=dt.datetime(2026, 5, 11, 10, 0, 0)),
            # ---- WIDE INTEGERS -> string (STRAIN 3 finding: they cannot ride
            #      the int lane; stringified losslessly, so they do NOT overflow) ----
            FixtureCase("uint64_to_string", "UInt64", 18446744073709551615, "string",
                        expected_coerced="18446744073709551615"),
            FixtureCase("int128_to_string", "Int128", 2**100, "string",
                        expected_coerced=str(2**100)),
            FixtureCase("uint256_to_string", "UInt256", 2**200, "string",
                        expected_coerced=str(2**200)),
            # ---- WIDTH / overflow on the int lane (the founding template) ----
            # UInt32 max 4.29e9 fits int64 but exceeds the int32 codec lane.
            FixtureCase("uint32_over_int32", "UInt32", 4_000_000_000, "int64",
                        roundtrip=False, raises=Row64CodecOverflowError,
                        raises_stage="write"),
            # Int64 value > int32, same lane as pg bigint.
            FixtureCase("int64_over_int32", "Int64", 3_548_933_426, "int64",
                        roundtrip=False, raises=Row64CodecOverflowError,
                        raises_stage="write"),
            # High-precision Decimal cannot survive float64 (Decimal128/256 land).
            FixtureCase("decimal_precision_loss", "Decimal(76, 20)",
                        Decimal("12345678901234567890.123456789012345"), "float64",
                        roundtrip=False, raises=NumericPrecisionLossError,
                        raises_stage="coerce"),
        ]
    )


CLICKHOUSE_SPEC = SourceSpec(
    dialect="clickhouse",
    type_map=CLICKHOUSE_TYPE_MAP,
    widths={"int": _INT32_MAX},
    watermark=WatermarkSpec(
        cursor_types=(
            "DateTime", "DateTime64", "Date", "Date32",
            "Int8", "Int16", "Int32", "Int64",
            "UInt8", "UInt16", "UInt32", "UInt64",
        ),
        # Per-table assumption (operator asserts the cursor column is
        # non-decreasing for new rows), same posture as pg. Not Gate-A-exercised.
        monotonic=True,
    ),
    fixture_pack=_fixture_pack(),
    array_dtype="string",
    unknown_dtype="string",
    coercer_map=CLICKHOUSE_COERCER_MAP,
    array_coercer="array",
    # hooks wired at generation time (Phase 2)
    coerce_value=None,
    pandas_dtype_for=None,
    pushdown=PushdownStub(
        supported=(),
        notes="ClickHouse predicate/limit pushdown deferred to Gate B",
    ),
)


# ===========================================================================
# PENDING-EXTENSION block — DO NOT WIRE until Gate 1 clears the format change.
#
# ClickHouse's transparent type wrappers cannot be expressed by the current
# flat type_map + paren-stripping normalizer (STRAIN 1):
#
#     Nullable(Int32)         -> normalizes to "nullable"  (inner Int32 LOST)
#     Nullable(String)        -> normalizes to "nullable"  (inner String LOST)
#     LowCardinality(String)  -> normalizes to "lowcardinality"  (inner LOST)
#
# Both collapse to the same key, so a flat type_map cannot route them to the
# inner type's dtype. They require the normalizer to UNWRAP transparent wrappers
# before lookup. Proposed minimal, source-general extension (see strain report):
#
#     SourceSpec gains:  wrapper_types: tuple[str, ...] = ()
#     ClickHouse sets:   wrapper_types=("nullable", "lowcardinality")
#     generated _normalize() strips wrapper_types recursively, e.g.
#         Nullable(Int32) -> Int32 -> int32 -> type_map["int32"]="int64"
#     pg declares no wrappers (wrapper_types=()), so its behavior is unchanged.
#
# Once that lands, ADD these fixtures (they prove the unwrap works):
#     FixtureCase("nullable_int_ok", "Nullable(Int32)", 7, "int64", expected_coerced=7)
#     FixtureCase("nullable_str_none", "Nullable(String)", None, "string")  # NULL class
#     FixtureCase("lowcard_str_ok", "LowCardinality(String)", "x", "string",
#                 expected_coerced="x")
# ===========================================================================


__all__ = ["CLICKHOUSE_SPEC", "CLICKHOUSE_TYPE_MAP", "CLICKHOUSE_COERCER_MAP"]
