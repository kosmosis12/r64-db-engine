"""Tests for the source-agnostic coercion framework. SPEC §6.2, §6.3."""

from __future__ import annotations

import numpy as np
import pandas as pd

from r64_db_engine.core import coercion

# ---- ASCII sanitization (§6.2) ----------------------------------------


def test_ascii_sanitize_replaces_smart_quotes():
    s = pd.Series(["hello “world”", "em—dash", "café"])
    out = coercion.ascii_sanitize_series(s)
    assert out.tolist() == ["hello ?world?", "em?dash", "caf?"]


def test_ascii_sanitize_passes_ascii_through():
    s = pd.Series(["plain ascii", "abc 123"])
    out = coercion.ascii_sanitize_series(s)
    assert out.tolist() == ["plain ascii", "abc 123"]


def test_ascii_sanitize_handles_emoji():
    s = pd.Series(["fire \U0001f525", "ok"])
    out = coercion.ascii_sanitize_series(s)
    assert out.tolist() == ["fire ?", "ok"]


# ---- NaN handling (§6.3) ---------------------------------------------


def test_int_column_fills_nan_with_zero():
    s = pd.Series([1.0, 2.0, np.nan, 4.0])
    out = coercion.coerce_int_column(s)
    assert out.tolist() == [1, 2, 0, 4]
    assert str(out.dtype) == "int64"


def test_float_column_preserves_nan():
    s = pd.Series([1.0, np.nan, 3.0])
    out = coercion.coerce_float_column(s)
    assert out.iloc[0] == 1.0
    assert np.isnan(out.iloc[1])
    assert out.iloc[2] == 3.0


def test_string_column_fills_nan_with_empty():
    s = pd.Series(["a", None, "b", np.nan])
    out = coercion.coerce_string_column(s, ascii_sanitize=True)
    assert out.tolist() == ["a", "", "b", ""]


def test_string_column_applies_ascii_when_enabled():
    s = pd.Series(["café", "plain"])
    out = coercion.coerce_string_column(s, ascii_sanitize=True)
    assert out.tolist() == ["caf?", "plain"]


def test_string_column_skips_ascii_when_disabled():
    s = pd.Series(["café", "plain"])
    out = coercion.coerce_string_column(s, ascii_sanitize=False)
    assert out.tolist() == ["café", "plain"]


def test_bool_column_fills_nan_with_false():
    s = pd.Series([True, False, np.nan, True])
    out = coercion.coerce_bool_column(s)
    assert out.tolist() == [True, False, False, True]


def test_datetime_column_preserves_nat():
    s = pd.Series(pd.to_datetime(["2026-01-01", None, "2026-01-03"]))
    out = coercion.coerce_datetime_column(s)
    assert out.iloc[0] == pd.Timestamp("2026-01-01")
    assert pd.isna(out.iloc[1])
    assert out.iloc[2] == pd.Timestamp("2026-01-03")


def test_datetime_column_strips_timezone():
    s = pd.Series(pd.to_datetime(["2026-01-01T12:00:00+02:00"]))
    out = coercion.coerce_datetime_column(s)
    assert out.iloc[0] == pd.Timestamp("2026-01-01T10:00:00")
    assert out.dt.tz is None


# ---- apply_coercion dispatch -----------------------------------------


def test_apply_coercion_dispatches_per_column():
    df = pd.DataFrame(
        {
            "i": pd.Series([1.0, np.nan, 3.0]),
            "f": pd.Series([1.5, np.nan, 3.5]),
            "s": pd.Series(["café", None, "plain"]),
            "b": pd.Series([True, np.nan, False]),
            "ts": pd.to_datetime(["2026-01-01", None, "2026-01-02"]),
        }
    )
    out = coercion.apply_coercion(
        df,
        column_dtypes={
            "i": "int64",
            "f": "float64",
            "s": "string",
            "b": "bool",
            "ts": "datetime64[ns]",
        },
    )
    assert out["i"].tolist() == [1, 0, 3]
    assert out["s"].tolist() == ["caf?", "", "plain"]
    assert out["b"].tolist() == [True, False, False]
    assert pd.isna(out["ts"].iloc[1])


def test_apply_coercion_passes_through_unmapped_columns():
    df = pd.DataFrame({"x": [1, 2, 3], "y": ["a", "b", "c"]})
    out = coercion.apply_coercion(df, column_dtypes={"x": "int64"})
    assert "y" in out.columns
    assert out["y"].tolist() == ["a", "b", "c"]


def test_apply_coercion_empty_dataframe():
    df = pd.DataFrame({"i": pd.Series([], dtype="float64")})
    out = coercion.apply_coercion(df, column_dtypes={"i": "int64"})
    assert len(out) == 0
    assert str(out["i"].dtype) == "int64"


# ---- schema drift detection ------------------------------------------


def test_compare_schemas_detects_added_columns():
    diff = coercion.compare_schemas(
        previous=[{"name": "a", "source_type": "bigint", "pandas_dtype": "int64"}],
        current=[
            {"name": "a", "source_type": "bigint", "pandas_dtype": "int64"},
            {"name": "b", "source_type": "text", "pandas_dtype": "string"},
        ],
    )
    assert diff == {"added": ["b"], "removed": [], "type_changed": []}


def test_compare_schemas_detects_removed_columns():
    diff = coercion.compare_schemas(
        previous=[
            {"name": "a", "source_type": "bigint", "pandas_dtype": "int64"},
            {"name": "b", "source_type": "text", "pandas_dtype": "string"},
        ],
        current=[{"name": "a", "source_type": "bigint", "pandas_dtype": "int64"}],
    )
    assert diff == {"added": [], "removed": ["b"], "type_changed": []}


def test_compare_schemas_detects_type_change():
    diff = coercion.compare_schemas(
        previous=[{"name": "a", "source_type": "integer", "pandas_dtype": "int64"}],
        current=[{"name": "a", "source_type": "text", "pandas_dtype": "string"}],
    )
    assert diff == {"added": [], "removed": [], "type_changed": ["a"]}


def test_compare_schemas_none_previous_returns_empty():
    diff = coercion.compare_schemas(
        previous=None,
        current=[{"name": "a", "source_type": "bigint", "pandas_dtype": "int64"}],
    )
    assert diff == {"added": [], "removed": [], "type_changed": []}
