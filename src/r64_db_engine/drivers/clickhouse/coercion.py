"""ClickHouse type coercion.

Implements the policy in references/coercion-clickhouse.md. Wrapper types
(`Nullable`, `LowCardinality`) are unwrapped before mapping. Types with no
clean ramdb scalar representation raise instead of silently losing data.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from decimal import Decimal
from typing import Any

import pandas as pd

log = logging.getLogger(__name__)


class UnsupportedClickHouseType(ValueError):
    """Raised when a ClickHouse type has no clean ramdb representation."""


CLICKHOUSE_TYPE_TO_PANDAS: dict[str, str] = {
    "DateTime64": "datetime64[ns]",
    "DateTime": "datetime64[ns]",
    "Date": "datetime64[ns]",
    "Date32": "datetime64[ns]",
    "Decimal": "float64",
    "Decimal32": "float64",
    "Decimal64": "float64",
    "Decimal128": "float64",
    "Decimal256": "float64",
    "Int8": "int64",
    "Int16": "int64",
    "Int32": "int64",
    "Int64": "int64",
    "Int128": "unsupported",
    "Int256": "unsupported",
    "UInt8": "int64",
    "UInt16": "int64",
    "UInt32": "int64",
    "UInt64": "unsupported",
    "UInt128": "unsupported",
    "UInt256": "unsupported",
    "Float32": "float64",
    "Float64": "float64",
    "String": "string",
    "FixedString": "string",
    "UUID": "string",
    "Enum8": "string",
    "Enum16": "string",
    "Array": "string",
    "Map": "unsupported",
    "Tuple": "unsupported",
    "Bool": "bool",
}

ORDERABLE_BASE_TYPES = frozenset(
    {
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
        "UInt8",
        "UInt16",
        "UInt32",
        "Float32",
        "Float64",
        "String",
        "FixedString",
        "UUID",
        "Enum8",
        "Enum16",
        "Bool",
    }
)

_CANONICAL: dict[str, str] = {k.lower(): k for k in CLICKHOUSE_TYPE_TO_PANDAS}
_PRECISION_LOSS_THRESHOLD = Decimal("0.0001")
_LARGE_VALUE_WARN_BYTES = 64 * 1024


def pandas_dtype_for(source_type: str) -> str:
    base = base_type(source_type)
    dtype = CLICKHOUSE_TYPE_TO_PANDAS.get(base)
    if dtype is None:
        log.debug("clickhouse_coercion: unknown source_type=%r, defaulting to string", source_type)
        return "string"
    if dtype == "unsupported":
        raise UnsupportedClickHouseType(
            f"ClickHouse type {source_type!r} has no clean ramdb representation"
        )
    return dtype


def base_type(source_type: str) -> str:
    """Return the canonical logical type, unwrapping CH encoding/nullability."""
    expr = _unwrap_type(source_type.strip())
    name = expr.split("(", 1)[0].strip()
    return _CANONICAL.get(name.lower(), name)


def is_orderable_type(source_type: str) -> bool:
    return base_type(source_type) in ORDERABLE_BASE_TYPES


def coerce_value(value: Any, source_type: str) -> Any:
    if _is_missing(value):
        return None

    base = base_type(source_type)
    if CLICKHOUSE_TYPE_TO_PANDAS.get(base) == "unsupported":
        raise UnsupportedClickHouseType(
            f"ClickHouse type {source_type!r} has no clean ramdb representation"
        )

    handler = _DISPATCH.get(base)
    if handler is None:
        return str(value)
    return handler(value)


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, (list, tuple, dict, set)):
        return False
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _unwrap_type(source_type: str) -> str:
    expr = re.sub(r"\s+", " ", source_type.strip())
    while True:
        match = re.match(r"(?i)^(Nullable|LowCardinality)\((.*)\)$", expr)
        if match is None:
            return expr
        expr = match.group(2).strip()


def _coerce_int(value: Any) -> int:
    return int(value)


def _coerce_float(value: Any) -> float:
    return float(value)


def _coerce_decimal(value: Any) -> float:
    decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
    out = float(decimal_value)
    if _precision_loss(decimal_value, out):
        log.warning(
            "clickhouse_coerce_value: precision loss converting decimal %s -> %r",
            decimal_value,
            out,
        )
    return out


def _precision_loss(value: Decimal, as_float: float) -> bool:
    try:
        return abs(Decimal(repr(as_float)) - value) > _PRECISION_LOSS_THRESHOLD
    except Exception:
        return False


def _coerce_string(value: Any) -> str:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).rstrip(b"\x00").decode("utf-8", errors="replace")
    return str(value)


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.lower() in {"1", "t", "true", "y", "yes"}
    return bool(value)


def _coerce_datetime(value: Any) -> dt.datetime:
    if isinstance(value, dt.datetime):
        return _strip_tz(value)
    if isinstance(value, dt.date):
        return dt.datetime(value.year, value.month, value.day)
    return pd.to_datetime(value, errors="coerce", utc=True).to_pydatetime().replace(tzinfo=None)


def _strip_tz(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is not None:
        return value.astimezone(dt.UTC).replace(tzinfo=None)
    return value


def _coerce_array(value: Any) -> str:
    if isinstance(value, str):
        return value
    encoded = json.dumps(value, default=str, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > _LARGE_VALUE_WARN_BYTES:
        log.warning(
            "clickhouse_coerce_value: array value > %dKB",
            _LARGE_VALUE_WARN_BYTES // 1024,
        )
    return encoded


_DISPATCH: dict[str, Any] = {
    "DateTime64": _coerce_datetime,
    "DateTime": _coerce_datetime,
    "Date": _coerce_datetime,
    "Date32": _coerce_datetime,
    "Decimal": _coerce_decimal,
    "Decimal32": _coerce_decimal,
    "Decimal64": _coerce_decimal,
    "Decimal128": _coerce_decimal,
    "Decimal256": _coerce_decimal,
    "Int8": _coerce_int,
    "Int16": _coerce_int,
    "Int32": _coerce_int,
    "Int64": _coerce_int,
    "UInt8": _coerce_int,
    "UInt16": _coerce_int,
    "UInt32": _coerce_int,
    "Float32": _coerce_float,
    "Float64": _coerce_float,
    "String": _coerce_string,
    "FixedString": _coerce_string,
    "UUID": _coerce_string,
    "Enum8": _coerce_string,
    "Enum16": _coerce_string,
    "Array": _coerce_array,
    "Bool": _coerce_bool,
}


__all__ = [
    "CLICKHOUSE_TYPE_TO_PANDAS",
    "ORDERABLE_BASE_TYPES",
    "UnsupportedClickHouseType",
    "base_type",
    "coerce_value",
    "is_orderable_type",
    "pandas_dtype_for",
]
