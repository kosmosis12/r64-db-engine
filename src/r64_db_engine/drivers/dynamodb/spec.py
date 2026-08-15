"""DynamoDB Gate A source-capability spec.

Gate A models coercion fidelity with a synthetic fixed schema containing every
native DynamoDB value type. Real tables are schemaless; bounded sampling,
attribute union, type-conflict widening, and deterministic discovery ordering
are driver discovery behavior and are intentionally tested at Gates 2 and 3,
not represented in the shared SourceSpec format.

Fixture raw values come directly from boto3 TypeDeserializer, so this remains a
network-free client-boundary proof rather than a hand-shaped Python facsimile.
"""

from __future__ import annotations

from boto3.dynamodb.types import TypeDeserializer

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
from r64_db_engine.drivers.dynamodb import coercion

_deserialize = TypeDeserializer().deserialize


def _fixture_pack() -> FixturePack:
    return FixturePack(
        cases=[
            FixtureCase("string", "S", _deserialize({"S": "hello"}), "string",
                        expected_coerced="hello"),
            FixtureCase("number_integral", "N", _deserialize({"N": "42"}), "int64",
                        expected_coerced=42),
            FixtureCase("number_fractional", "N", _deserialize({"N": "3.125"}),
                        "float64", expected_coerced=3.125),
            FixtureCase("boolean", "BOOL", _deserialize({"BOOL": True}), "bool",
                        expected_coerced=True),
            FixtureCase("null", "NULL", _deserialize({"NULL": True}), "string"),
            FixtureCase("binary", "B", _deserialize({"B": b"\x01\xff"}), "string",
                        expected_coerced="01ff"),
            FixtureCase(
                "map", "M",
                _deserialize({"M": {"z": {"N": "2"}, "a": {"BOOL": True}}}),
                "string", expected_coerced='{"a":true,"z":2}',
            ),
            FixtureCase("list", "L", _deserialize({"L": [{"N": "1"}, {"S": "x"}]}),
                        "string", expected_coerced='[1,"x"]'),
            FixtureCase("string_set", "SS", _deserialize({"SS": ["z", "a"]}),
                        "string", expected_coerced='["a","z"]'),
            FixtureCase("number_set", "NS", _deserialize({"NS": ["10", "2", "1"]}),
                        "string", expected_coerced="[1,10,2]"),
            FixtureCase("binary_set", "BS", _deserialize({"BS": [b"\xff", b"\x01"]}),
                        "string", expected_coerced='["01","ff"]'),
            FixtureCase(
                "number_over_codec_lane", "N", _deserialize({"N": str(2**31)}), "int64",
                roundtrip=False, raises=Row64CodecOverflowError, raises_stage="write",
            ),
            FixtureCase(
                "number_over_int64", "N", _deserialize({"N": str(2**63)}), "float64",
                roundtrip=False, raises=Row64CodecOverflowError, raises_stage="coerce",
            ),
            FixtureCase(
                "number_precision_loss", "N",
                _deserialize({"N": "12345678901234567890.123456789"}), "float64",
                roundtrip=False, raises=NumericPrecisionLossError, raises_stage="coerce",
            ),
        ]
    )


DYNAMODB_SPEC = SourceSpec(
    dialect="dynamodb",
    type_map=dict(coercion.DYNAMODB_TYPE_TO_PANDAS),
    widths={"int": 2**31 - 1},
    watermark=WatermarkSpec(cursor_types=("n", "s"), monotonic=True),
    fixture_pack=_fixture_pack(),
    unknown_dtype="string",
    coercer_map=dict(coercion.DYNAMODB_COERCER_MAP),
    dtype_resolver_map=dict(coercion.DYNAMODB_DTYPE_RESOLVER_MAP),
    coerce_value=coercion.coerce_value,
    pandas_dtype_for=coercion.pandas_dtype_for,
    pushdown=PushdownStub(
        supported=(),
        notes="filter-scan and GSI query behavior are driver tests, not Gate A fidelity",
    ),
)


__all__ = ["DYNAMODB_SPEC"]
