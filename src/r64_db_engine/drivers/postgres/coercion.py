"""PostgreSQL type coercion. Implements the table in SPEC §6.1.

Two layers:
  1. `PG_TYPE_TO_PANDAS` — map a Postgres source type (lowercased, no
     length spec) to the pandas dtype we target.
  2. `coerce_value(value, source_type)` — single-value coercion for tests
     and the driver's object-column row-fixup pass.

Layer 2 owns NO value logic of its own: it normalizes the source type, then
dispatches through `PG_COERCER_MAP` into the canonical registry in
`conformance.coercers`. That registry is the single source of truth for value
fidelity, shared verbatim by every generated/sibling driver, so there is one
implementation instantiated twice — not two implementations kept in sync.

This module's remaining job is the pg-specific wiring: (a) the type -> dtype
map, and (b) the type -> canonical-coercer-key map that tells the registry how
to treat each Postgres type (UUIDs/dicts/bytes/arrays -> their string form
BEFORE the framework's string-column rules run).
"""

from __future__ import annotations

import logging
import re
from typing import Any

import pandas as pd

from r64_db_engine.conformance import coercers
from r64_db_engine.conformance.coercers import NumericPrecisionLossError

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
    # numeric is represented as float64 for codec compatibility. Values that
    # do not survive Decimal -> float64 -> decimal exactly are rejected below.
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

# NumericPrecisionLossError lives in conformance.coercers (a contract-level
# fidelity error, not pg-specific) and is re-exported here so existing imports —
# `from ...postgres.coercion import NumericPrecisionLossError` — and the
# hand-built/regenerated proof share one error identity. See conformance/coercers.py.

# Native (normalized) Postgres type -> canonical coercer key in
# conformance.coercers.REGISTRY. This is the pg-specific half of the contract:
# the registry owns *how* each class of value is coerced; this map owns *which*
# class each Postgres type belongs to. The conformance SourceSpec and the
# scaffold generator both consume this map, so there is exactly one wiring.
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
    # json / bytea / array
    "json": "json", "jsonb": "json", "bytea": "bytea", "array": "array",
    # network / fts / geometry / xml / ranges -> str
    "inet": "str", "cidr": "str", "macaddr": "str", "macaddr8": "str",
    "tsvector": "str", "tsquery": "str", "geometry": "str", "geography": "str",
    "xml": "str", "int4range": "str", "int8range": "str", "numrange": "str",
    "tsrange": "str", "tstzrange": "str", "daterange": "str",
}

# Native array types ([]-suffixed) route here.
_ARRAY_COERCER = "array"


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

    Owns no value logic: it normalizes the type and dispatches through
    `PG_COERCER_MAP` into the canonical `conformance.coercers` registry.
    """
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None

    norm = _normalize_type(source_type)

    if norm.endswith("[]"):
        return coercers.REGISTRY[_ARRAY_COERCER](value)

    key = PG_COERCER_MAP.get(norm)
    if key is None:
        # Uncatalogued type — fall back to the registry's string coercer so the
        # str() conversion is still owned by the canonical registry.
        log.debug("pg_coerce_value: no handler for %r, casting to str", source_type)
        return coercers.to_str(value)
    return coercers.REGISTRY[key](value)


__all__ = [
    "PG_TYPE_TO_PANDAS",
    "PG_COERCER_MAP",
    "NumericPrecisionLossError",
    "pandas_dtype_for",
    "coerce_value",
]
