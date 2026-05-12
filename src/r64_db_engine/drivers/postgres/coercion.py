"""PostgreSQL type coercion. Implements the table in SPEC §6.1.

Two layers:
  1. `PG_TYPE_TO_PANDAS` — map a Postgres source type (lowercased, no
     length spec) to the pandas dtype we target.
  2. `coerce_value(value, source_type)` — single-value coercion for tests.

The driver applies `apply_coercion(df, column_dtypes)` from
core.coercion AFTER psycopg has already produced Python objects; this
module's job is to (a) tell the framework the right target dtype per
column, and (b) handle the values psycopg returns that the framework
can't generically deal with (UUIDs, dicts for jsonb, bytes for bytea,
arrays, etc.) by converting them to their string representation BEFORE
the framework's string-column rules run.
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


PG_TYPE_TO_PANDAS: dict[str, str] = {
    # integers
    "smallint": "int64",
    "int2": "int64",
    "integer": "int64",
    "int": "int64",
    "int4": "int64",
    "bigint": "int64",
    "int8": "int64",
    "smallserial": "int64",
    "serial": "int64",
    "bigserial": "int64",
    "oid": "int64",
    # floats
    "real": "float64",
    "float4": "float64",
    "double precision": "float64",
    "float8": "float64",
    # numeric (possible precision loss -> warn in pull)
    "numeric": "float64",
    "decimal": "float64",
    # strings
    "text": "string",
    "varchar": "string",
    "character varying": "string",
    "char": "string",
    "character": "string",
    "bpchar": "string",
    "name": "string",
    "citext": "string",
    # bool
    "boolean": "bool",
    "bool": "bool",
    # date / time
    "date": "datetime64[ns]",
    "timestamp": "datetime64[ns]",
    "timestamp without time zone": "datetime64[ns]",
    "timestamptz": "datetime64[ns]",
    "timestamp with time zone": "datetime64[ns]",
    "time": "string",
    "time without time zone": "string",
    "timetz": "string",
    "time with time zone": "string",
    "interval": "int64",  # microseconds
    # uuid
    "uuid": "string",
    # json / arrays / bytea
    "json": "string",
    "jsonb": "string",
    "bytea": "string",
    "array": "string",
    # network types
    "inet": "string",
    "cidr": "string",
    "macaddr": "string",
    "macaddr8": "string",
    # full-text search
    "tsvector": "string",
    "tsquery": "string",
    # geometry / xml / range
    "geometry": "string",
    "geography": "string",
    "xml": "string",
    "int4range": "string",
    "int8range": "string",
    "numrange": "string",
    "tsrange": "string",
    "tstzrange": "string",
    "daterange": "string",
}

_LARGE_VALUE_WARN_BYTES = 64 * 1024
_NUMERIC_PRECISION_LOSS_THRESHOLD = 1e-4


def pandas_dtype_for(source_type: str) -> str:
    """Resolve a Postgres source type string to a target pandas dtype.

    Strips length/precision specs (`varchar(64)` -> `varchar`,
    `numeric(20,5)` -> `numeric`). Array types (`integer[]`,
    `text[][]`) collapse to "string" (JSON-serialized). Unknown types
    fall back to "string" with a debug log.
    """
    norm = _normalize_type(source_type)

    if norm.endswith("[]"):
        return "string"

    if norm in PG_TYPE_TO_PANDAS:
        return PG_TYPE_TO_PANDAS[norm]

    # range/composite/user-defined types we haven't catalogued — string is safe.
    log.debug("pg_coercion: unknown source_type=%r, defaulting to string", source_type)
    return "string"


def _normalize_type(source_type: str) -> str:
    """Lowercase, strip whitespace, strip `(...)` length/precision."""
    s = source_type.strip().lower()
    s = re.sub(r"\s*\([^)]*\)", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def coerce_value(value: Any, source_type: str) -> Any:
    """Per-value coercion to match the target pandas dtype.

    Used by tests and by the driver's row-fixup pass for object-typed
    columns (jsonb, uuid, bytea, arrays, etc.). NaN/None passes through
    so the framework's column-level rules can handle nullability.
    """
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None

    norm = _normalize_type(source_type)

    if norm.endswith("[]"):
        return _coerce_array(value)

    handler = _DISPATCH.get(norm)
    if handler is None:
        log.debug("pg_coerce_value: no handler for %r, casting to str", source_type)
        return str(value)
    return handler(value)


# ---- individual coercers ------------------------------------------------


def _coerce_int(value: Any) -> int:
    return int(value)


def _coerce_float(value: Any) -> float:
    return float(value)


def _coerce_numeric(value: Any) -> float:
    if isinstance(value, Decimal):
        as_float = float(value)
        if _precision_loss(value, as_float):
            log.warning(
                "pg_coerce_value: precision loss converting numeric %s -> %r",
                value,
                as_float,
            )
        return as_float
    return float(value)


def _precision_loss(d: Decimal, f: float) -> bool:
    try:
        return abs(Decimal(repr(f)) - d) > Decimal(str(_NUMERIC_PRECISION_LOSS_THRESHOLD))
    except Exception:
        return False


def _coerce_str(value: Any) -> str:
    return str(value)


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.lower() in ("t", "true", "1", "y", "yes")
    return bool(value)


def _coerce_date(value: Any) -> dt.datetime:
    if isinstance(value, dt.datetime):
        return _strip_tz(value)
    if isinstance(value, dt.date):
        return dt.datetime(value.year, value.month, value.day)
    return pd.to_datetime(value, utc=True).to_pydatetime().replace(tzinfo=None)


def _coerce_timestamp(value: Any) -> dt.datetime:
    if isinstance(value, dt.datetime):
        return _strip_tz(value)
    return pd.to_datetime(value, utc=True).to_pydatetime().replace(tzinfo=None)


def _strip_tz(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is not None:
        return value.astimezone(dt.UTC).replace(tzinfo=None)
    return value


def _coerce_time(value: Any) -> str:
    if isinstance(value, dt.time):
        return value.isoformat()
    return str(value)


def _coerce_interval(value: Any) -> int:
    """timedelta -> microseconds (int64)."""
    if isinstance(value, dt.timedelta):
        return (
            value.days * 86_400_000_000
            + value.seconds * 1_000_000
            + value.microseconds
        )
    return int(value)


def _coerce_uuid(value: Any) -> str:
    return str(value)


def _coerce_json(value: Any) -> str:
    if isinstance(value, str):
        encoded = value
    else:
        encoded = json.dumps(value, default=str, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > _LARGE_VALUE_WARN_BYTES:
        log.warning("pg_coerce_value: json value > %dKB", _LARGE_VALUE_WARN_BYTES // 1024)
    return encoded


def _coerce_bytea(value: Any) -> str:
    if isinstance(value, (bytes, bytearray, memoryview)):
        as_bytes = bytes(value)
    else:
        as_bytes = str(value).encode("utf-8")
    if len(as_bytes) > _LARGE_VALUE_WARN_BYTES:
        log.warning("pg_coerce_value: bytea value > %dKB", _LARGE_VALUE_WARN_BYTES // 1024)
    return as_bytes.hex()


def _coerce_array(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str, separators=(",", ":"))


_DISPATCH: dict[str, Any] = {
    # integers
    "smallint": _coerce_int,
    "int2": _coerce_int,
    "integer": _coerce_int,
    "int": _coerce_int,
    "int4": _coerce_int,
    "bigint": _coerce_int,
    "int8": _coerce_int,
    "smallserial": _coerce_int,
    "serial": _coerce_int,
    "bigserial": _coerce_int,
    "oid": _coerce_int,
    # floats
    "real": _coerce_float,
    "float4": _coerce_float,
    "double precision": _coerce_float,
    "float8": _coerce_float,
    # numerics
    "numeric": _coerce_numeric,
    "decimal": _coerce_numeric,
    # strings
    "text": _coerce_str,
    "varchar": _coerce_str,
    "character varying": _coerce_str,
    "char": _coerce_str,
    "character": _coerce_str,
    "bpchar": _coerce_str,
    "name": _coerce_str,
    "citext": _coerce_str,
    # bools
    "boolean": _coerce_bool,
    "bool": _coerce_bool,
    # date / time
    "date": _coerce_date,
    "timestamp": _coerce_timestamp,
    "timestamp without time zone": _coerce_timestamp,
    "timestamptz": _coerce_timestamp,
    "timestamp with time zone": _coerce_timestamp,
    "time": _coerce_time,
    "time without time zone": _coerce_time,
    "timetz": _coerce_time,
    "time with time zone": _coerce_time,
    "interval": _coerce_interval,
    # uuid
    "uuid": _coerce_uuid,
    # json / bytea
    "json": _coerce_json,
    "jsonb": _coerce_json,
    "bytea": _coerce_bytea,
    "array": _coerce_array,
    # network
    "inet": _coerce_str,
    "cidr": _coerce_str,
    "macaddr": _coerce_str,
    "macaddr8": _coerce_str,
    # text search
    "tsvector": _coerce_str,
    "tsquery": _coerce_str,
    # geometry / xml / ranges
    "geometry": _coerce_str,
    "geography": _coerce_str,
    "xml": _coerce_str,
    "int4range": _coerce_str,
    "int8range": _coerce_str,
    "numrange": _coerce_str,
    "tsrange": _coerce_str,
    "tstzrange": _coerce_str,
    "daterange": _coerce_str,
}


__all__ = [
    "PG_TYPE_TO_PANDAS",
    "pandas_dtype_for",
    "coerce_value",
]
