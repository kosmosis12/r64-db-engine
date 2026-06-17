"""Gate A — the source-agnostic fidelity contract.

Five assertion classes, each a pure function of a `SourceSpec` + its fixture
pack. No live database, no driver internals: every assertion reduces to the
driver's `coerce_value` / `pandas_dtype_for` hooks, the source-agnostic
`core.coercion.apply_coercion`, and the real `RamdbWriter` + `row64tools`
round-trip. That is exactly the reduction justified in the Task 1
classification — live Postgres only *manufactured* the Python objects, which
the fixture pack now supplies directly.

Assertion classes:
  TYPE_MAP — native type -> row64 dtype, and the declared type_map agrees with
             the behavioral pandas_dtype_for hook.
  WIDTH    — a value wider than the codec lane is caught (founding template:
             the int32 lane), whether at the coercer or at the writer.
  NULL     — None passthrough + per-dtype NaN/NaT sentinel fill rules.
  TZ       — tz-aware temporal -> UTC-naive; date/timestamp/time normalization.
  RAMDB    — write -> load_to_df -> value-equal (codec-aware).

Each `check_*` raises AssertionError on failure (pytest-native). `run_gate_a`
wraps them to produce a per-class go/no-go report for the proof + SUMMARY.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Any

import pandas as pd

from r64_db_engine.conformance.spec import UNSET, FixtureCase, SourceSpec
from r64_db_engine.core.coercion import apply_coercion
from r64_db_engine.core.ramdb_writer import RamdbWriter

_ROW64_INT_LANE = "int"


# ---- TYPE_MAP ----------------------------------------------------------


def check_type_map(spec: SourceSpec) -> None:
    spec.require_hooks()
    assert spec.pandas_dtype_for is not None  # for type-checkers
    # 1. Every fixture pins (native type -> dtype).
    for case in spec.fixture_pack.cases:
        got = spec.pandas_dtype_for(case.source_type)
        assert got == case.expected_dtype, (
            f"[{spec.dialect}] TYPE_MAP: pandas_dtype_for({case.source_type!r}) "
            f"= {got!r}, fixture {case.name!r} expects {case.expected_dtype!r}"
        )
    # 2. The declared type_map agrees with the behavioral hook. This is what
    #    catches a regenerated driver whose generated mapping silently drifts
    #    from the declared spec.
    for native, dtype in spec.type_map.items():
        got = spec.pandas_dtype_for(native)
        assert got == dtype, (
            f"[{spec.dialect}] TYPE_MAP: declared type_map[{native!r}]={dtype!r} "
            f"but pandas_dtype_for returns {got!r}"
        )


# ---- WIDTH (value-width / overflow) ------------------------------------


def check_value_width(spec: SourceSpec) -> None:
    spec.require_hooks()
    assert spec.coerce_value is not None
    lane = spec.widths.get(_ROW64_INT_LANE)

    # 1. Declared overflow cases raise where declared.
    for case in spec.fixture_pack.overflow_cases:
        if case.raises_stage == "coerce":
            _assert_raises(
                partial(spec.coerce_value, case.raw_value, case.source_type),
                case.raises,
                f"[{spec.dialect}] WIDTH: coerce({case.name}) should raise "
                f"{case.raises.__name__}",  # type: ignore[union-attr]
            )
        else:  # "write" — coerces fine, rejected on the way into the codec lane
            coerced = spec.coerce_value(case.raw_value, case.source_type)
            df = pd.DataFrame({case.name: pd.Series([coerced], dtype=case.expected_dtype)})
            with tempfile.TemporaryDirectory() as d:
                writer = RamdbWriter(d, "G")
                _assert_raises(
                    partial(writer.write, df, case.name),
                    case.raises,
                    f"[{spec.dialect}] WIDTH: writing {case.name} should raise "
                    f"{case.raises.__name__}",  # type: ignore[union-attr]
                )

    # 2. widths drive the assertion: any non-overflow int fixture wider than the
    #    declared lane is an *undeclared* leak and must fail here.
    if lane is not None:
        for case in spec.fixture_pack.cases:
            if case.raises is not None:
                continue
            if case.expected_dtype.lower() in ("int64", "int32") and isinstance(
                case.raw_value, int
            ) and not isinstance(case.raw_value, bool):
                assert abs(case.raw_value) <= lane, (
                    f"[{spec.dialect}] WIDTH: fixture {case.name!r} value "
                    f"{case.raw_value} exceeds declared int lane {lane} but is "
                    "not declared as an overflow case"
                )


# ---- NULL / sentinel ---------------------------------------------------


def check_null_sentinel(spec: SourceSpec) -> None:
    spec.require_hooks()
    assert spec.coerce_value is not None
    # 1. None passes through coercion untouched (the framework fills later).
    for case in spec.fixture_pack.cases:
        if not case.nullable:
            continue
        assert spec.coerce_value(None, case.source_type) is None, (
            f"[{spec.dialect}] NULL: coerce_value(None, {case.source_type!r}) "
            "should pass None through"
        )
    # 2. Per-dtype sentinel fills, parameterized by the dtype classes this
    #    source actually emits. Uses the source-agnostic framework directly.
    dtypes = {c.expected_dtype for c in spec.fixture_pack.cases}
    cols: dict[str, pd.Series] = {}
    expected: dict[str, tuple[str, object]] = {}
    if "int64" in dtypes:
        cols["i"] = pd.Series([1.0, float("nan")])
        expected["i"] = ("int64", [1, 0])
    if "float64" in dtypes:
        cols["f"] = pd.Series([1.5, float("nan")])
        expected["f"] = ("float64", "nan_preserved")
    if "string" in dtypes:
        cols["s"] = pd.Series(["a", None])
        expected["s"] = ("string", ["a", ""])
    if "bool" in dtypes:
        cols["b"] = pd.Series([True, float("nan")])
        expected["b"] = ("bool", [True, False])
    if "datetime64[ns]" in dtypes:
        cols["t"] = pd.to_datetime(["2026-01-01", None])
        expected["t"] = ("datetime64[ns]", "nat_preserved")
    if not cols:
        return
    df = pd.DataFrame(cols)
    out = apply_coercion(df, {c: expected[c][0] for c in cols}, ascii_sanitize=False)
    for col, (_dtype, want) in expected.items():
        series = out[col]
        if want == "nan_preserved":
            assert pd.isna(series.iloc[1]) and series.iloc[0] == 1.5, (
                f"[{spec.dialect}] NULL: float NaN not preserved"
            )
        elif want == "nat_preserved":
            assert pd.isna(series.iloc[1]), f"[{spec.dialect}] NULL: NaT not preserved"
        else:
            assert series.tolist() == want, (
                f"[{spec.dialect}] NULL: {col} sentinel fill = {series.tolist()}, "
                f"expected {want}"
            )


# ---- TZ / temporal -----------------------------------------------------


def check_timezone_temporal(spec: SourceSpec) -> None:
    spec.require_hooks()
    assert spec.coerce_value is not None
    seen_tz_aware = False
    for case in spec.fixture_pack.cases:
        if not _is_temporal(case) or case.expected_coerced is UNSET:
            continue
        got = spec.coerce_value(case.raw_value, case.source_type)
        assert got == case.expected_coerced, (
            f"[{spec.dialect}] TZ: coerce({case.name}) = {got!r}, "
            f"expected {case.expected_coerced!r}"
        )
        if _is_tz_aware(case.raw_value):
            seen_tz_aware = True
            assert getattr(got, "tzinfo", None) is None, (
                f"[{spec.dialect}] TZ: {case.name} should be tz-naive after coercion"
            )
    # A source that declares timestamp support but ships no tz-aware fixture has
    # an untested timezone path — surface it rather than passing vacuously.
    if any(d == "datetime64[ns]" for d in (c.expected_dtype for c in spec.fixture_pack.cases)):
        assert seen_tz_aware, (
            f"[{spec.dialect}] TZ: source maps a datetime64 dtype but the fixture "
            "pack has no timezone-aware case — the tz path is untested"
        )


# ---- RAMDB round-trip --------------------------------------------------


def check_ramdb_round_trip(spec: SourceSpec) -> None:
    spec.require_hooks()
    assert spec.coerce_value is not None
    cases = spec.fixture_pack.roundtrip_cases
    if not cases:
        return
    # Mirror the driver pull pipeline: value-coerce each scalar, build a frame,
    # then apply the framework's dtype + sentinel rules, write, and load back.
    row = {c.name: [spec.coerce_value(c.raw_value, c.source_type)] for c in cases}
    df = pd.DataFrame(row)
    df = apply_coercion(
        df, {c.name: c.expected_dtype for c in cases}, ascii_sanitize=False
    )
    with tempfile.TemporaryDirectory() as d:
        writer = RamdbWriter(d, "G")
        path = writer.write(df, "roundtrip")
        from row64tools.ramdb import load_to_df  # lazy: matches writer's contract

        loaded = load_to_df(str(path))
    for case in cases:
        if case.expected_coerced is UNSET:
            continue
        _assert_roundtrip_value(spec.dialect, case, loaded[case.name].iloc[0])


def _assert_roundtrip_value(dialect: str, case: FixtureCase, loaded: Any) -> None:
    """Codec-aware value equality after a real .ramdb round-trip.

    The row64 codec narrows int64->int32 and bool->int32(0/1) and returns
    strings as object dtype, so the contract compares *values* per the source
    dtype rather than demanding strict dtype equality (which the codec would
    never satisfy).
    """
    dtype = case.expected_dtype.lower()
    want = case.expected_coerced
    if dtype in ("int64", "int32"):
        assert int(loaded) == int(want), _mismatch(dialect, case, loaded)
    elif dtype == "bool":
        assert int(loaded) == int(bool(want)), _mismatch(dialect, case, loaded)
    elif dtype in ("float64", "float32"):
        assert abs(float(loaded) - float(want)) < 1e-9, _mismatch(dialect, case, loaded)
    elif dtype.startswith("datetime64"):
        assert pd.Timestamp(loaded) == pd.Timestamp(want), _mismatch(dialect, case, loaded)
    else:  # string-family
        assert str(loaded) == str(want), _mismatch(dialect, case, loaded)


def _mismatch(dialect: str, case: FixtureCase, loaded: object) -> str:
    return (
        f"[{dialect}] RAMDB: {case.name!r} round-tripped to {loaded!r}, "
        f"expected {case.expected_coerced!r}"
    )


# ---- runner / report ---------------------------------------------------


ASSERTION_CLASSES: dict[str, Callable[[SourceSpec], None]] = {
    "TYPE_MAP": check_type_map,
    "WIDTH": check_value_width,
    "NULL": check_null_sentinel,
    "TZ": check_timezone_temporal,
    "RAMDB": check_ramdb_round_trip,
}


@dataclass
class ClassResult:
    name: str
    passed: bool
    detail: str


@dataclass
class GateAReport:
    dialect: str
    results: list[ClassResult]

    @property
    def ok(self) -> bool:
        return all(r.passed for r in self.results)

    def as_table(self) -> str:
        width = max(len(r.name) for r in self.results)
        lines = [f"  {r.name.ljust(width)}  {'PASS' if r.passed else 'FAIL'}  {r.detail}"
                 for r in self.results]
        return "\n".join(lines)


def run_gate_a(spec: SourceSpec) -> GateAReport:
    """Run every assertion class, capturing pass/fail per class."""
    results: list[ClassResult] = []
    for name, check in ASSERTION_CLASSES.items():
        try:
            check(spec)
            n = len(spec.fixture_pack.cases)
            results.append(ClassResult(name, True, f"{n} fixture case(s)"))
        except AssertionError as exc:
            results.append(ClassResult(name, False, str(exc).splitlines()[0]))
        except Exception as exc:  # an unexpected error is also a failure to report
            results.append(ClassResult(name, False, f"ERROR: {type(exc).__name__}: {exc}"))
    return GateAReport(spec.dialect, results)


def _assert_raises(fn: Callable[[], object], exc_type, message: str) -> None:
    try:
        fn()
    except BaseException as exc:  # noqa: BLE001 - we re-assert the type below
        assert isinstance(exc, exc_type), (
            f"{message}; raised {type(exc).__name__} instead"
        )
        return
    raise AssertionError(f"{message}; nothing was raised")


def _is_temporal(case: FixtureCase) -> bool:
    import datetime as _dt

    return isinstance(case.raw_value, (_dt.date, _dt.datetime, _dt.time))


def _is_tz_aware(value: object) -> bool:
    import datetime as _dt

    return isinstance(value, _dt.datetime) and value.tzinfo is not None


__all__ = [
    "ASSERTION_CLASSES",
    "ClassResult",
    "GateAReport",
    "run_gate_a",
    "check_type_map",
    "check_value_width",
    "check_null_sentinel",
    "check_timezone_temporal",
    "check_ramdb_round_trip",
]
