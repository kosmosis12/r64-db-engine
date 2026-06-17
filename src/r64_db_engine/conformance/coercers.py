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
from decimal import Decimal
from typing import Any

import pandas as pd

from r64_db_engine.core.ramdb_writer import Row64CodecOverflowError

log = logging.getLogger(__name__)

_LARGE_VALUE_WARN_BYTES = 64 * 1024
_ROW64_INT_MIN = -(2**31)
_ROW64_INT_MAX = 2**31 - 1


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
        as_bytes = str(value).encode("utf-8")
    if len(as_bytes) > _LARGE_VALUE_WARN_BYTES:
        log.warning("coercers: bytea value > %dKB", _LARGE_VALUE_WARN_BYTES // 1024)
    return as_bytes.hex()


def to_array(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str, separators=(",", ":"))


# Registry keyed by canonical coercer name. A `SourceSpec.coercer_map` points
# each native type at one of these keys; the generated driver dispatches
# through this table.
REGISTRY: dict[str, Any] = {
    "int": to_int,
    "float": to_float,
    "numeric": to_numeric,
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
}


__all__ = [
    "NumericPrecisionLossError",
    "Row64CodecOverflowError",
    "REGISTRY",
    "to_int",
    "to_float",
    "to_numeric",
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
]
