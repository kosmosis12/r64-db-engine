"""DuckDB type coercion for the DataFrame lane.

Only the DataFrame lane needs this. The Arrow-native lane carries DuckDB's own
Arrow types straight through to the sink and never asks pandas what a type
should become — which is the entire point of that lane, and the reason this
module is deliberately small.

Policy follows `references/coercion-clickhouse.md` in spirit: unwrap the
decorations, map to a ramdb-safe scalar dtype, and RAISE rather than silently
lose data for anything with no clean representation.
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


class UnsupportedDuckDBType(ValueError):
    """Raised when a DuckDB type has no clean ramdb representation."""


# Keys are DuckDB's canonical `information_schema` / `duckdb_columns()` names.
DUCKDB_TYPE_TO_PANDAS: dict[str, str] = {
    "BOOLEAN": "int64",
    "TINYINT": "int64",
    "SMALLINT": "int64",
    "INTEGER": "int64",
    "BIGINT": "int64",
    "HUGEINT": "unsupported",  # 128-bit: no lossless int64 landing
    "UHUGEINT": "unsupported",
    "UTINYINT": "int64",
    "USMALLINT": "int64",
    "UINTEGER": "int64",
    "UBIGINT": "unsupported",  # exceeds int64 at the top of its range
    "FLOAT": "float64",
    "DOUBLE": "float64",
    "DECIMAL": "float64",
    "VARCHAR": "string",
    "UUID": "string",
    "BLOB": "unsupported",
    "BIT": "unsupported",
    "DATE": "datetime64[ns]",
    "TIME": "string",
    "TIMESTAMP": "datetime64[ns]",
    "TIMESTAMP WITH TIME ZONE": "datetime64[ns]",
    "TIMESTAMP_S": "datetime64[ns]",
    "TIMESTAMP_MS": "datetime64[ns]",
    "TIMESTAMP_NS": "datetime64[ns]",
    "INTERVAL": "string",
    "JSON": "string",
    "LIST": "string",
    "STRUCT": "string",
    "MAP": "string",
    "ENUM": "string",
}

_ORDERABLE = {"int64", "float64", "datetime64[ns]"}
_DECIMAL_RE = re.compile(r"^DECIMAL\s*\(", re.IGNORECASE)
_LIST_RE = re.compile(r"\[\]$")


def base_type(source_type: str) -> str:
    """Strip parameters and decorations down to the mapped key.

    `DECIMAL(18,3)` -> `DECIMAL`, `INTEGER[]` -> `LIST`,
    `STRUCT(a INTEGER)` -> `STRUCT`.
    """
    t = (source_type or "").strip()
    if not t:
        return ""
    if _LIST_RE.search(t):
        return "LIST"
    upper = t.upper()
    for prefix in ("STRUCT", "MAP", "UNION", "ENUM"):
        if upper.startswith(prefix):
            return prefix if prefix != "UNION" else "UNION"
    if _DECIMAL_RE.match(upper):
        return "DECIMAL"
    # TIMESTAMP WITH TIME ZONE and friends keep their spaces; parameterized
    # types like VARCHAR(10) lose theirs.
    if "(" in upper:
        upper = upper.split("(", 1)[0].strip()
    return upper


def pandas_dtype_for(source_type: str) -> str:
    key = base_type(source_type)
    dtype = DUCKDB_TYPE_TO_PANDAS.get(key)
    if dtype is None:
        raise UnsupportedDuckDBType(f"unmapped DuckDB type: {source_type!r}")
    if dtype == "unsupported":
        raise UnsupportedDuckDBType(
            f"DuckDB type {source_type!r} has no lossless ramdb representation"
        )
    return dtype


def is_orderable_type(source_type: str) -> bool:
    try:
        return pandas_dtype_for(source_type) in _ORDERABLE
    except UnsupportedDuckDBType:
        return False


def coerce_value(value: Any, source_type: str) -> Any:
    """Single-value coercion. NULL stays NULL — never a sentinel."""
    if _is_missing(value):
        return None
    dtype = pandas_dtype_for(source_type)
    if dtype == "int64":
        return _coerce_int(value)
    if dtype == "float64":
        return _coerce_float(value, source_type)
    if dtype == "datetime64[ns]":
        return _coerce_datetime(value)
    return _coerce_string(value)


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    # NaN is NOT missing for a float column on the Arrow lane, but this helper
    # only serves the pandas lane, where the conflation already happened.
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _coerce_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    return int(value)


def _coerce_float(value: Any, source_type: str) -> float:
    if isinstance(value, Decimal):
        as_float = float(value)
        if Decimal(repr(as_float)) != value:
            log.warning(
                "duckdb_decimal_precision_loss type=%s value=%s", source_type, value
            )
        return as_float
    return float(value)


def _coerce_datetime(value: Any) -> dt.datetime:
    ts = pd.Timestamp(value)
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC").tz_localize(None)
    return ts.to_pydatetime()


def _coerce_string(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (list, dict)):
        return json.dumps(value, default=str, separators=(",", ":"))
    return str(value)


__all__ = [
    "DUCKDB_TYPE_TO_PANDAS",
    "UnsupportedDuckDBType",
    "base_type",
    "coerce_value",
    "is_orderable_type",
    "pandas_dtype_for",
]
