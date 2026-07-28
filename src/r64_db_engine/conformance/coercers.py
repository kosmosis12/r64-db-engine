"""Canonical, source-agnostic scalar coercers.

These are the contract-level coercers — the single source of truth for value
fidelity. They operate purely on the Python objects a DB-API client yields
(`Decimal`, `datetime`, `date`, `time`, `timedelta`, `bytes`, `dict`/`list`,
`UUID`) — nothing Postgres-specific — so any source wires its native types onto
them via a `coercer_map`.

The Postgres reference driver dispatches its `coerce_value` *through* this
registry (`drivers/postgres/coercion.py` owns only the pg type -> coercer-key
map, no value logic of its own). A driver regenerated from a spec wires through
the very same registry, so hand-built and regenerated pg are one implementation
instantiated twice — not two implementations kept in sync. The self-regeneration
proof confirms they remain identical on pg's fixture pack.

Two fidelity error types live at this contract level because they are not
source-specific concerns:
  - `Row64CodecOverflowError` (re-exported from `core.ramdb_writer`) — a value
    wider than the codec's signed-int32 lane.
  - `NumericPrecisionLossError` — a Decimal that cannot round-trip through the
    float64 the codec stores.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
from decimal import Decimal
from typing import Any

import pandas as pd

from r64_db_engine.core.ramdb_writer import Row64CodecOverflowError

log = logging.getLogger(__name__)

_LARGE_VALUE_WARN_BYTES = 64 * 1024
_ROW64_INT_MIN = -(2**31)
_ROW64_INT_MAX = 2**31 - 1
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1


class NumericPrecisionLossError(ValueError):
    """A numeric value cannot be represented exactly as the output float64."""


# ---- scalar coercers ---------------------------------------------------


def to_int(value: Any) -> int:
    return int(value)


def to_float(value: Any) -> float:
    return float(value)


def to_numeric(value: Any) -> float:
    if isinstance(value, Decimal):
        as_float = float(value)
        if _precision_loss(value, as_float):
            raise NumericPrecisionLossError(
                f"numeric value {value} cannot round-trip exactly through float64"
            )
        return as_float
    return float(value)


def to_decimal_number(value: Any) -> int | float:
    """Preserve integral Decimals as int64 and exact fractions as float64."""
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    if value.is_finite() and value == value.to_integral_value():
        integer = int(value)
        if integer < _INT64_MIN or integer > _INT64_MAX:
            raise Row64CodecOverflowError(
                f"integral numeric value {value} is outside signed int64 range"
            )
        return integer
    return to_numeric(value)


def _precision_loss(d: Decimal, f: float) -> bool:
    try:
        return Decimal(str(f)) != d
    except Exception:
        return True


def to_str(value: Any) -> str:
    return str(value)


def to_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.lower() in ("t", "true", "1", "y", "yes")
    return bool(value)


def to_date(value: Any) -> dt.datetime:
    if isinstance(value, dt.datetime):
        return _strip_tz(value)
    if isinstance(value, dt.date):
        return dt.datetime(value.year, value.month, value.day)
    return pd.to_datetime(value, utc=True).to_pydatetime().replace(tzinfo=None)


def to_timestamp(value: Any) -> dt.datetime:
    if isinstance(value, dt.datetime):
        return _strip_tz(value)
    return pd.to_datetime(value, utc=True).to_pydatetime().replace(tzinfo=None)


def _strip_tz(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is not None:
        return value.astimezone(dt.UTC).replace(tzinfo=None)
    return value


def to_time(value: Any) -> str:
    if isinstance(value, dt.time):
        return value.isoformat()
    return str(value)


def to_interval(value: Any) -> int:
    """timedelta -> microseconds (int64), guarded against the int32 codec lane."""
    if isinstance(value, dt.timedelta):
        result = (
            value.days * 86_400_000_000
            + value.seconds * 1_000_000
            + value.microseconds
        )
    else:
        result = int(value)
    if result < _ROW64_INT_MIN or result > _ROW64_INT_MAX:
        raise Row64CodecOverflowError(
            "row64 codec cannot safely store interval conversion: "
            f"value {result} is outside signed int32 range"
        )
    return result


def to_uuid(value: Any) -> str:
    return str(value)


def to_json(value: Any) -> str:
    if isinstance(value, str):
        encoded = value
    else:
        encoded = json.dumps(value, default=str, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > _LARGE_VALUE_WARN_BYTES:
        log.warning("coercers: json value > %dKB", _LARGE_VALUE_WARN_BYTES // 1024)
    return encoded


def to_bytea(value: Any) -> str:
    if isinstance(value, (bytes, bytearray, memoryview)):
        as_bytes = bytes(value)
    else:
        try:
            as_bytes = bytes(value)
        except (TypeError, ValueError):
            as_bytes = str(value).encode("utf-8")
    if len(as_bytes) > _LARGE_VALUE_WARN_BYTES:
        log.warning("coercers: bytea value > %dKB", _LARGE_VALUE_WARN_BYTES // 1024)
    return as_bytes.hex()


def to_array(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str, separators=(",", ":"))


def _json_compatible(value: Any) -> Any:
    if isinstance(value, Decimal):
        return to_decimal_number(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if hasattr(value, "__bytes__"):
        try:
            return bytes(value).hex()
        except (TypeError, ValueError):
            pass
    if isinstance(value, dict):
        return {str(key): _json_compatible(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_compatible(item) for item in value]
    if isinstance(value, (set, frozenset)):
        converted = [_json_compatible(item) for item in value]
        return sorted(converted, key=_canonical_sort_key)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite floats are not valid deterministic JSON")
    return value


def _canonical_sort_key(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def to_deterministic_json(value: Any) -> str:
    """Encode nested values as stable compact JSON with Decimal fidelity."""
    if isinstance(value, str):
        return value
    return json.dumps(
        _json_compatible(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def to_deterministic_set_json(value: Any) -> str:
    """Encode an unordered collection as a deterministically ordered JSON array."""
    if not isinstance(value, (set, frozenset)):
        raise TypeError(f"expected set or frozenset, got {type(value).__name__}")
    return to_deterministic_json(value)


def decimal_numeric_dtype(value: Any) -> str:
    """Select int64 for in-range integral Decimal values, float64 otherwise."""
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    if value.is_finite() and value == value.to_integral_value():
        integer = int(value)
        if _INT64_MIN <= integer <= _INT64_MAX:
            return "int64"
    return "float64"


# Registry keyed by canonical coercer name. A `SourceSpec.coercer_map` points
# each native type at one of these keys; the generated driver dispatches
# through this table.
REGISTRY: dict[str, Any] = {
    "int": to_int,
    "float": to_float,
    "numeric": to_numeric,
    "decimal_number": to_decimal_number,
    "str": to_str,
    "bool": to_bool,
    "date": to_date,
    "timestamp": to_timestamp,
    "time": to_time,
    "interval": to_interval,
    "uuid": to_uuid,
    "json": to_json,
    "bytea": to_bytea,
    "array": to_array,
    "deterministic_json": to_deterministic_json,
    "deterministic_set_json": to_deterministic_set_json,
}

DTYPE_RESOLVERS: dict[str, Any] = {
    "decimal_numeric": decimal_numeric_dtype,
}


__all__ = [
    "NumericPrecisionLossError",
    "Row64CodecOverflowError",
    "REGISTRY",
    "DTYPE_RESOLVERS",
    "to_int",
    "to_float",
    "to_numeric",
    "to_decimal_number",
    "to_str",
    "to_bool",
    "to_date",
    "to_timestamp",
    "to_time",
    "to_interval",
    "to_uuid",
    "to_json",
    "to_bytea",
    "to_array",
    "to_deterministic_json",
    "to_deterministic_set_json",
    "decimal_numeric_dtype",
]
