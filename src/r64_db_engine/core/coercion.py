"""Source-agnostic dataframe coercion rules. See SPEC §6.2 and §6.3.

Drivers produce a raw DataFrame with intended target dtypes per column.
This module applies the universal rules on top:

  - Nulls are PRESERVED in every column type.
  - String columns with ascii_sanitize=True -> ASCII-replaced (non-null only).
  - Integers use pandas' nullable `Int64`, booleans `boolean`.

Pure functions; never raises on a row, logs at debug for telemetry only.

# Null policy lives at the SINK boundary, not here

This module used to fill NaN with 0 in integer columns and "" in string
columns. That was a `.ramdb` FORMAT limitation — pandas' numpy `int64` cannot
hold NA and the row64tools codec has no null representation — enforced in the
source-agnostic layer, so it silently degraded fidelity for EVERY sink,
including formats that represent null natively.

The cost was real: a SQL `NULL` in a `BIGINT` became `0`, indistinguishable
downstream from a legitimate zero. That is the same class of defect as PG-001
(int64 narrowing) — one output format's constraint imposed on data destined for
formats that do not share it.

So the rule moved rather than changed: this layer now preserves nulls, and
`core/ramdb_writer.py` applies the legacy fill explicitly at write time as a
documented row64tools accommodation. `.ramdb` output is byte-identical to
before — asserted against golden files in `tests/core/test_ramdb_golden.py` —
and `ArrowIpcSink` now carries true Arrow nulls.

# Why pandas nullable dtypes rather than Arrow-backed ones

`Int64`/`boolean` are pandas-native, so this layer stays free of any sink's
library — putting `import pyarrow` in the source-agnostic core would be the
same category of leak the driver firewall exists to prevent. They also map
cleanly both ways: `Int64` -> Arrow `int64` + null bitmap for the Arrow sink,
and `.fillna(0).astype("int64")` reproduces the exact legacy numpy dtype for
the ramdb sink.

Plain numpy `int64` was not an option: assigning NA to it silently promotes the
whole column to `float64`, which is its own fidelity trap (every value above
2^53 starts rounding).
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
    if series.isna().all():
        # An all-null column has nothing to transliterate, and no inferable
        # string storage to transliterate it in: pandas 3 leaves `.astype(str)`
        # float-backed for an all-NaN float column, and the `.str` accessor
        # rejects a floating array outright. Returning the nullable string dtype
        # early keeps the null-preserving contract intact for a column whose
        # values are, by definition, all null.
        return series.astype("string")
    return series.astype(str).str.encode("ascii", errors="replace").str.decode("ascii")


def coerce_int_column(series: pd.Series, target_dtype: str = "Int64") -> pd.Series:
    """Cast to a nullable integer dtype, PRESERVING nulls.

    `Int64` rather than `int64`: numpy integers cannot hold NA, and assigning
    one promotes the column to `float64` — silently lossy above 2^53. The
    legacy fill-with-0 now lives in `core/ramdb_writer.py`, at the boundary of
    the format that actually requires it.
    """
    if series.isna().any():
        log.debug("coerce_int: preserved %d null(s) in column", int(series.isna().sum()))
    return series.astype(target_dtype)


def coerce_float_column(series: pd.Series, target_dtype: str = "float64") -> pd.Series:
    """Preserve NaN; only cast dtype."""
    return series.astype(target_dtype)


def coerce_string_column(series: pd.Series, ascii_sanitize: bool = True) -> pd.Series:
    """Optionally ASCII-sanitize, PRESERVING nulls.

    Sanitization applies to non-null values only — a null is not a string to
    transliterate. The legacy fill-with-"" lives in `core/ramdb_writer.py`.
    """
    null_mask = series.isna()
    series = series.astype(str)
    # `astype(str)` stringifies NA into a literal "nan"/"<NA>"; restore real
    # nulls from the mask captured before the cast rather than string-matching.
    if ascii_sanitize:
        series = ascii_sanitize_series(series)
    series = series.astype("string")
    return series.mask(null_mask, pd.NA)


def coerce_bool_column(series: pd.Series) -> pd.Series:
    """Cast to nullable `boolean`, PRESERVING nulls.

    numpy `bool` has no NA and coerces null to False — which reads downstream
    as a definite negative rather than "unknown". The legacy fill-with-False
    lives in `core/ramdb_writer.py`.
    """
    if series.isna().any():
        log.debug("coerce_bool: preserved %d null(s)", int(series.isna().sum()))
    return series.astype("boolean")


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
        return coerce_int_column(series, target_dtype="Int64")
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
