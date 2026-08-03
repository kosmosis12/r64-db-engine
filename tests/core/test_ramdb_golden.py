"""Golden byte-identity for `.ramdb` output across the null-policy move.

THIS IS THE NO-BEHAVIOUR-CHANGE CONTRACT. Null handling moved out of the
source-agnostic `core/coercion.py` and into `core/ramdb_writer.py`, where the
format that actually requires it lives. Customer-facing `.ramdb` semantics must
not have moved with it.

The fixtures in `tests/golden/ramdb/` were produced by the REAL row64tools
codec (`row64tools==1.0.11`) running the pre-change pipeline
`raw df -> apply_coercion -> RamdbWriter.write`. These tests run the post-change
pipeline over the identical inputs and compare bytes.

If one of these fails, `.ramdb` output changed. That is a release-blocking
regression, not a fixture to refresh — regenerate the goldens only with a
deliberate, reviewed decision to change ramdb output.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pandas as pd
import pytest

pytest.importorskip("row64tools")

from r64_db_engine.core.coercion import apply_coercion  # noqa: E402
from r64_db_engine.core.ramdb_writer import RamdbWriter, apply_ramdb_null_fill  # noqa: E402

GOLDEN_DIR = Path(__file__).parent.parent / "golden" / "ramdb"

# Same cases, same order, same dtypes as the capture script that produced the
# fixtures. The `nulls` case is the e2e row that exposed the defect.
CASES: dict[str, tuple[pd.DataFrame, dict[str, str]]] = {
    "nulls": (
        pd.DataFrame(
            {
                "id": [1, 2, 3],
                "n": [10, None, 30],
                "s": ["a", None, "café"],
                "d": [1.5, None, 3.5],
                "b": [True, None, False],
                "t": pd.to_datetime(["2026-01-01", None, "2026-01-03"]),
            }
        ),
        {
            "id": "int64",
            "n": "int64",
            "s": "object",
            "d": "float64",
            "b": "bool",
            "t": "datetime64[ns]",
        },
    ),
    "no_nulls": (
        pd.DataFrame(
            {
                "id": [1, 2, 3],
                "n": [10, 20, 30],
                "s": ["a", "b", "c"],
                "d": [1.5, 2.5, 3.5],
                "b": [True, False, True],
                "t": pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-03"]),
            }
        ),
        {
            "id": "int64",
            "n": "int64",
            "s": "object",
            "d": "float64",
            "b": "bool",
            "t": "datetime64[ns]",
        },
    ),
    "all_null_int": (
        pd.DataFrame({"id": [1, 2], "n": [None, None]}),
        {"id": "int64", "n": "int64"},
    ),
    "unicode": (
        pd.DataFrame({"id": [1, 2], "s": ["em—dash café 🚀", None]}),
        {"id": "int64", "s": "object"},
    ),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_ramdb_bytes_are_unchanged_by_the_null_policy_move(
    case: str, tmp_path: Path
) -> None:
    golden = GOLDEN_DIR / f"{case}.ramdb"
    assert golden.exists(), f"missing golden fixture: {golden}"

    df, dtypes = CASES[case]
    loading = tmp_path / "loading"
    loading.mkdir()
    coerced = apply_coercion(df, dtypes, ascii_sanitize=True)
    produced = RamdbWriter(loading, "G").write(coerced, case)

    got = Path(produced).read_bytes()
    want = golden.read_bytes()

    assert hashlib.sha256(got).hexdigest() == hashlib.sha256(want).hexdigest(), (
        f"`.ramdb` output changed for case {case!r}: "
        f"{len(want)} golden bytes vs {len(got)} produced. "
        f"Null policy moved to the sink boundary; ramdb bytes must not move with it."
    )


def test_coercion_now_yields_nullable_dtypes() -> None:
    """The other half of the contract: the layer that fed ramdb changed.

    Byte-identity above is only meaningful if the input dtypes actually moved —
    otherwise the goldens would pass trivially.
    """
    df, dtypes = CASES["nulls"]
    coerced = apply_coercion(df, dtypes, ascii_sanitize=True)

    assert str(coerced["n"].dtype) == "Int64"
    assert str(coerced["b"].dtype) == "boolean"
    assert str(coerced["s"].dtype) == "string"
    # ...and nulls survived the source-agnostic layer.
    assert coerced["n"].isna().sum() == 1
    assert coerced["s"].isna().sum() == 1
    assert coerced["b"].isna().sum() == 1


def test_ramdb_null_fill_collapses_to_legacy_numpy_dtypes() -> None:
    """The accommodation itself, asserted directly rather than only via bytes."""
    filled = apply_ramdb_null_fill(
        pd.DataFrame(
            {
                "n": pd.Series([1, None, 3], dtype="Int64"),
                "s": pd.Series(["a", None, "c"], dtype="string"),
                "b": pd.Series([True, None, False], dtype="boolean"),
                "d": pd.Series([1.5, None, 3.5], dtype="float64"),
            }
        )
    )

    assert str(filled["n"].dtype) == "int64"
    assert filled["n"].tolist() == [1, 0, 3]
    assert filled["s"].tolist() == ["a", "", "c"]
    assert str(filled["b"].dtype) == "bool"
    assert filled["b"].tolist() == [True, False, False]
    # Float NaN is NOT filled — ramdb represents it.
    assert filled["d"].isna().sum() == 1


def test_ramdb_null_fill_does_not_mutate_the_caller_frame() -> None:
    original = pd.DataFrame({"n": pd.Series([1, None], dtype="Int64")})
    apply_ramdb_null_fill(original)
    assert str(original["n"].dtype) == "Int64"
    assert original["n"].isna().sum() == 1
