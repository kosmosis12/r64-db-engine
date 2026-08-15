"""DynamoDB client-value coercion for pandas and ramdb fidelity."""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from r64_db_engine.conformance import coercers
from r64_db_engine.conformance.coercers import (
    NumericPrecisionLossError,
    Row64CodecOverflowError,
)

log = logging.getLogger(__name__)

DYNAMODB_TYPES = frozenset({"s", "n", "bool", "null", "b", "m", "l", "ss", "ns", "bs"})

# `n` uses float64 only as its schema-only fallback. Supplying the deserialized
# Decimal to pandas_dtype_for activates the canonical value-sensitive resolver.
DYNAMODB_TYPE_TO_PANDAS: dict[str, str] = {
    "s": "string",
    "n": "float64",
    "bool": "bool",
    "null": "string",
    "b": "string",
    "m": "string",
    "l": "string",
    "ss": "string",
    "ns": "string",
    "bs": "string",
}

DYNAMODB_COERCER_MAP: dict[str, str] = {
    "s": "str",
    "n": "decimal_number",
    "bool": "bool",
    "null": "str",
    "b": "bytea",
    "m": "deterministic_json",
    "l": "deterministic_json",
    "ss": "deterministic_set_json",
    "ns": "deterministic_set_json",
    "bs": "deterministic_set_json",
}

DYNAMODB_DTYPE_RESOLVER_MAP: dict[str, str] = {"n": "decimal_numeric"}

_VALUE_UNSET = object()


def _normalize_type(source_type: str) -> str:
    return source_type.strip().lower()


def pandas_dtype_for(source_type: str, value: Any = _VALUE_UNSET) -> str:
    """Resolve a native DynamoDB type and optional client value to a dtype."""
    normalized = _normalize_type(source_type)
    resolver_key = DYNAMODB_DTYPE_RESOLVER_MAP.get(normalized)
    if resolver_key is not None and value is not _VALUE_UNSET:
        return coercers.DTYPE_RESOLVERS[resolver_key](value)
    if normalized not in DYNAMODB_TYPE_TO_PANDAS:
        log.debug("unknown source_type=%r, defaulting to string", source_type)
    return DYNAMODB_TYPE_TO_PANDAS.get(normalized, "string")


def coerce_value(value: Any, source_type: str) -> Any:
    """Coerce one value exactly as returned by boto3 TypeDeserializer."""
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None

    normalized = _normalize_type(source_type)
    key = DYNAMODB_COERCER_MAP.get(normalized)
    if key is None:
        log.debug("unknown source_type=%r, casting to string", source_type)
        return coercers.to_str(value)

    if normalized == "b":
        value = _binary_bytes(value)
    elif normalized == "bs":
        value = {_binary_bytes(member) for member in value}
    return coercers.REGISTRY[key](value)


def _binary_bytes(value: Any) -> bytes:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    try:
        return bytes(value)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"binary value is not bytes-compatible: {type(value).__name__}") from exc


__all__ = [
    "DYNAMODB_TYPES",
    "DYNAMODB_TYPE_TO_PANDAS",
    "DYNAMODB_COERCER_MAP",
    "DYNAMODB_DTYPE_RESOLVER_MAP",
    "NumericPrecisionLossError",
    "Row64CodecOverflowError",
    "pandas_dtype_for",
    "coerce_value",
]
