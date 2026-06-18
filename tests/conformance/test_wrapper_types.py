"""Tests for the general transparent-wrapper mechanism (SourceSpec.wrapper_types).

The generated normalizer unwraps declared transparent wrappers to their inner
type before lookup (e.g. ClickHouse Nullable(T)/LowCardinality(T) -> T),
recursively. A source that declares no wrappers is unaffected (proven elsewhere
by the unchanged pg self-regeneration).

Per the "untested abstraction = blind-gate pathology" doctrine, this ships with
both a positive proof (the unwrap works, including composed wrappers) and a
negative guard test (a malformed wrapper spec is rejected, not silently baked
into generated code).
"""

from __future__ import annotations

import dataclasses
import importlib
import sys
from pathlib import Path

import pytest

from r64_db_engine.conformance.generator import regenerate
from r64_db_engine.conformance.spec import (
    FixtureCase,
    FixturePack,
    SourceSpec,
    WatermarkSpec,
)

_FIXTURES_REF = "r64_db_engine.drivers.clickhouse.spec:CLICKHOUSE_SPEC"


def _synthetic_spec(dialect: str, wrapper_types: tuple[str, ...]) -> SourceSpec:
    """A tiny wrapper-bearing spec, independent of any real driver."""
    return SourceSpec(
        dialect=dialect,
        type_map={"int32": "int64", "string": "string"},
        widths={"int": 2**31 - 1},
        watermark=WatermarkSpec(cursor_types=("int32",)),
        fixture_pack=FixturePack(cases=[FixtureCase("i", "int32", 1, "int64", expected_coerced=1)]),
        coercer_map={"int32": "int", "string": "str"},
        wrapper_types=wrapper_types,
    )


def _load_generated_coercion(spec: SourceSpec, tmp_path: Path):
    regenerate(spec, tmp_path, _FIXTURES_REF)
    sys.path.insert(0, str(tmp_path))
    mod = f"{spec.dialect}_driver"
    for name in list(sys.modules):
        if name.startswith(mod):
            del sys.modules[name]
    coercion = importlib.import_module(f"{mod}.coercion")
    return coercion


def _cleanup(spec: SourceSpec, tmp_path: Path) -> None:
    p = str(tmp_path)
    if p in sys.path:
        sys.path.remove(p)
    for name in list(sys.modules):
        if name.startswith(f"{spec.dialect}_driver"):
            del sys.modules[name]


def test_wrapper_unwrap_dtype_and_coerce(tmp_path: Path) -> None:
    """Nullable/LowCardinality unwrap to the inner type's dtype + coercer."""
    spec = _synthetic_spec("wraptest", ("nullable", "lowcardinality"))
    coercion = _load_generated_coercion(spec, tmp_path)
    try:
        # dtype resolves through the wrapper to the inner type
        assert coercion.pandas_dtype_for("Nullable(Int32)") == "int64"
        assert coercion.pandas_dtype_for("LowCardinality(String)") == "string"
        # recursive: composed wrappers unwrap to the innermost type
        assert coercion.pandas_dtype_for("LowCardinality(Nullable(Int32))") == "int64"
        # value coercion dispatches through the unwrapped inner coercer
        assert coercion.coerce_value(7, "Nullable(Int32)") == 7
        assert coercion.coerce_value(None, "Nullable(Int32)") is None
        assert coercion.coerce_value("x", "LowCardinality(String)") == "x"
    finally:
        _cleanup(spec, tmp_path)


def test_no_wrapper_types_is_noop(tmp_path: Path) -> None:
    """With wrapper_types=(), a wrapper string is NOT unwrapped (stays unknown)."""
    spec = _synthetic_spec("nowrap", ())
    coercion = _load_generated_coercion(spec, tmp_path)
    try:
        # "nullable(int32)" -> params stripped -> "nullable" -> not in type_map
        assert coercion.pandas_dtype_for("Nullable(Int32)") == spec.unknown_dtype
    finally:
        _cleanup(spec, tmp_path)


@pytest.mark.parametrize("bad", ["Bad Wrapper!", "Nullable", "has space", "", "a(b"])
def test_malformed_wrapper_spec_rejected(tmp_path: Path, bad: str) -> None:
    """regenerate() refuses wrapper_types entries that aren't lowercase
    identifiers, rather than baking them into generated code."""
    spec = dataclasses.replace(_synthetic_spec("badwrap", ()), wrapper_types=(bad,))
    with pytest.raises(ValueError, match="wrapper_types entries must be lowercase identifiers"):
        regenerate(spec, tmp_path, _FIXTURES_REF)
