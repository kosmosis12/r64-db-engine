"""Source-agnostic dataframe coercion rules. See SPEC §6.2 and §6.3.

Drivers produce a raw DataFrame with intended target dtypes per column.
This module applies the universal ramdb-safety rules on top:

  - NaN in integer columns -> filled with 0 (NaN forces float promotion).
  - NaN in string columns -> filled with "".
  - NaN in boolean columns -> filled with False.
  - NaN/NaT in float and datetime columns -> preserved.
  - String columns with ascii_sanitize=True -> ASCII-replaced.

Pure functions; never raises on a row, logs at debug for telemetry only.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

# Pandas dtypes the engine treats as integer-like targets.
INT_DTYPES = frozenset({"int8", "int16", "int32", "int64", "Int8", "Int16", "Int32", "Int64"})
FLOAT_DTYPES = frozenset({"float32", "float64", "Float32", "Float64"})
STRING_DTYPES = frozenset({"string", "object"})
BOOL_DTYPES = frozenset({"bool", "boolean"})
DATETIME_DTYPE_PREFIX = "datetime64"


def ascii_sanitize_series(series: pd.Series) -> pd.Series:
    """Drop non-ASCII characters from a string series, replacing them with '?'.

    Matches Row64's historic preprocessor. Lossy and intentional: ramdb's
    serializer crashes on certain non-ASCII bytes.
    """
    if series.empty:
        return series
    return series.astype(str).str.encode("ascii", errors="replace").str.decode("ascii")


def coerce_int_column(series: pd.Series, target_dtype: str = "int64") -> pd.Series:
    """Fill NaN with 0 then cast. Logs the fill count at debug."""
    if series.isna().any():
        n_filled = int(series.isna().sum())
        log.debug("coerce_int: filled %d NaN(s) with 0 in column", n_filled)
        series = series.fillna(0)
    return series.astype(target_dtype)


def coerce_float_column(series: pd.Series, target_dtype: str = "float64") -> pd.Series:
    """Preserve NaN; only cast dtype."""
    return series.astype(target_dtype)


def coerce_string_column(series: pd.Series, ascii_sanitize: bool = True) -> pd.Series:
    """Fill NaN with "" and optionally ASCII-sanitize."""
    series = series.where(~series.isna(), "")
    series = series.astype(str)
    # Replace literal "nan" strings produced by astype(str) on lingering floats.
    series = series.where(series != "nan", "")
    if ascii_sanitize:
        series = ascii_sanitize_series(series)
    return series.astype("string")


def coerce_bool_column(series: pd.Series) -> pd.Series:
    """Fill NaN with False then cast to bool."""
    if series.isna().any():
        n_filled = int(series.isna().sum())
        log.debug("coerce_bool: filled %d NaN(s) with False", n_filled)
        series = series.fillna(False)
    return series.astype(bool)


def coerce_datetime_column(series: pd.Series) -> pd.Series:
    """Normalize to datetime64[ns] naive (UTC). NaT preserved."""
    out = pd.to_datetime(series, errors="coerce", utc=True)
    if getattr(out.dt, "tz", None) is not None:
        out = out.dt.tz_convert("UTC").dt.tz_localize(None)
    return out.astype("datetime64[ns]")


def apply_coercion(
    df: pd.DataFrame,
    column_dtypes: dict[str, str],
    ascii_sanitize: bool = True,
) -> pd.DataFrame:
    """Apply the universal coercion rules to a DataFrame.

    `column_dtypes` is name -> intended pandas dtype string. Columns absent
    from `column_dtypes` are passed through unchanged.
    """
    if df.empty:
        return _empty_with_dtypes(df, column_dtypes)

    out = df.copy()
    for col, target in column_dtypes.items():
        if col not in out.columns:
            continue
        out[col] = _coerce_one(out[col], target, ascii_sanitize)
    return out


def _coerce_one(series: pd.Series, target_dtype: str, ascii_sanitize: bool) -> pd.Series:
    if target_dtype in INT_DTYPES:
        return coerce_int_column(series, target_dtype="int64")
    if target_dtype in FLOAT_DTYPES:
        return coerce_float_column(series, target_dtype="float64")
    if target_dtype in BOOL_DTYPES:
        return coerce_bool_column(series)
    if target_dtype in STRING_DTYPES:
        return coerce_string_column(series, ascii_sanitize=ascii_sanitize)
    if target_dtype.startswith(DATETIME_DTYPE_PREFIX):
        return coerce_datetime_column(series)
    return series


def _empty_with_dtypes(df: pd.DataFrame, column_dtypes: dict[str, str]) -> pd.DataFrame:
    out = df.copy()
    for col, target in column_dtypes.items():
        if col not in out.columns:
            continue
        if target.startswith(DATETIME_DTYPE_PREFIX):
            out[col] = pd.Series([], dtype="datetime64[ns]")
        elif target in INT_DTYPES:
            out[col] = pd.Series([], dtype="int64")
        elif target in FLOAT_DTYPES:
            out[col] = pd.Series([], dtype="float64")
        elif target in BOOL_DTYPES:
            out[col] = pd.Series([], dtype="bool")
        elif target in STRING_DTYPES:
            out[col] = pd.Series([], dtype="string")
    return out


def compare_schemas(
    previous: Iterable[dict[str, str]] | None,
    current: Iterable[dict[str, str]],
) -> dict[str, list[str]]:
    """Return a dict with 'added', 'removed', 'type_changed' column lists.

    Each input item is {"name": ..., "source_type": ..., "pandas_dtype": ...}.
    `previous` may be None on first pull, in which case all current columns
    are reported as new (initial baseline; caller decides whether to log).
    """
    cur_map = {c["name"]: c for c in current}
    if previous is None:
        return {"added": [], "removed": [], "type_changed": []}

    prev_map = {c["name"]: c for c in previous}
    added = [n for n in cur_map if n not in prev_map]
    removed = [n for n in prev_map if n not in cur_map]
    type_changed = [
        n
        for n in cur_map
        if n in prev_map
        and (
            cur_map[n].get("source_type") != prev_map[n].get("source_type")
            or cur_map[n].get("pandas_dtype") != prev_map[n].get("pandas_dtype")
        )
    ]
    return {"added": added, "removed": removed, "type_changed": type_changed}


__all__ = [
    "INT_DTYPES",
    "FLOAT_DTYPES",
    "STRING_DTYPES",
    "BOOL_DTYPES",
    "DATETIME_DTYPE_PREFIX",
    "ascii_sanitize_series",
    "coerce_int_column",
    "coerce_float_column",
    "coerce_string_column",
    "coerce_bool_column",
    "coerce_datetime_column",
    "apply_coercion",
    "compare_schemas",
]


# Silence unused-import on numpy in tooling — used implicitly via pandas dtypes.
_ = np
