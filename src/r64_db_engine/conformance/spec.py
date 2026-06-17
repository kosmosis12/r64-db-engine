"""Source-capability spec format + fixture-pack dataclasses.

A driver declares one `SourceSpec`. The spec is the single source of truth the
scaffold generator consumes and the conformance contract asserts against. It is
deliberately *declarative data* (type_map, widths, watermark, fixture_pack)
plus two *behavioral hooks* (`coerce_value`, `pandas_dtype_for`) so the same
spec object can describe either a hand-built driver (hooks point at the
hand-written coercion module) or a regenerated one (hooks point at generated
code). Diffing Gate A across those two wirings is the self-regeneration proof.

Forward-compat for Gate B (throughput/perf) is present as `watermark` +
`PushdownStub`, but no throughput logic is implemented this session.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

# Sentinel so a fixture can say "don't assert a coerced scalar" (e.g. the case
# only exercises an overflow raise, or its coerced form is non-deterministic).
UNSET: Any = object()


@dataclass(frozen=True)
class FixtureCase:
    """One canonical edge-case value for a source.

    `raw_value` is the value *as the source client library yields it* — a
    `Decimal`, a tz-aware `datetime`, `bytes`, a `timedelta`, etc. The contract
    feeds it straight into the driver's coercers, so no live database is needed
    to reproduce the source's behavior.

    Width/overflow cases (the founding template) set `raises` to the exception
    a value wider than the codec lane must trigger, and `raises_stage` to where
    it is caught:
      - "coerce" — the value coercer rejects it (e.g. interval µs > int32, or a
        Decimal that loses precision).
      - "write"  — the value coerces fine but `RamdbWriter` rejects it on the
        way into the int32 codec lane (e.g. a bigint > 2**31-1).
    The two stages share one error vocabulary precisely so that "a value wider
    than the codec's lane is caught" is a single assertion class regardless of
    which lane the value crosses.
    """

    name: str
    source_type: str
    raw_value: Any
    expected_dtype: str
    expected_coerced: Any = UNSET
    nullable: bool = True
    # Whether this case participates in the .ramdb frame round-trip. Overflow
    # cases and intentionally-rejected values are excluded (they never reach a
    # frame). Telemetry-only giant blobs are also excluded to keep frames lean.
    roundtrip: bool = True
    # Width / overflow declaration.
    raises: type[BaseException] | None = None
    raises_stage: str = "coerce"  # "coerce" | "write"

    def __post_init__(self) -> None:
        if self.raises_stage not in ("coerce", "write"):
            raise ValueError(f"raises_stage must be coerce|write, got {self.raises_stage!r}")
        if self.raises is not None and self.roundtrip:
            raise ValueError(
                f"Overflow case {self.name!r} cannot have roundtrip=True; "
                "cases with raises are excluded from round-trip frames"
            )


@dataclass(frozen=True)
class FixturePack:
    """The canonical edge-case table for a source."""

    cases: Sequence[FixtureCase]

    def of_class(self, predicate: Callable[[FixtureCase], bool]) -> list[FixtureCase]:
        return [c for c in self.cases if predicate(c)]

    @property
    def overflow_cases(self) -> list[FixtureCase]:
        return [c for c in self.cases if c.raises is not None]

    @property
    def roundtrip_cases(self) -> list[FixtureCase]:
        return [c for c in self.cases if c.roundtrip and c.raises is None]


@dataclass(frozen=True)
class WatermarkSpec:
    """Incremental-cursor capability.

    `cursor_types` are the native types valid as an incremental key.
    `monotonic` records whether the source guarantees a non-decreasing cursor
    (used by Gate B and watermark-safety reasoning; declared here for
    forward-compat).
    """

    cursor_types: tuple[str, ...]
    monotonic: bool = True


@dataclass(frozen=True)
class PushdownStub:
    """Forward-compat placeholder for Gate B. NOT implemented this session.

    Present only so the spec format does not need a breaking change when the
    throughput/pushdown harness lands. `supported` would enumerate predicate /
    projection / limit pushdown the driver can do; today it is informational.
    """

    supported: tuple[str, ...] = ()
    notes: str = "Gate B placeholder — not implemented in the Gate A session"


@dataclass(frozen=True)
class SourceSpec:
    """Everything a driver declares to sign the conformance contract."""

    dialect: str

    # --- declarative fidelity surface -------------------------------------
    # native (normalized) type -> row64/pandas dtype.
    type_map: Mapping[str, str]
    # native type-class -> max value width (codec lane). Drives the WIDTH
    # assertions: any fixture value wider than its class's width must be a
    # declared overflow case. The founding template is {"int": 2**31-1}.
    widths: Mapping[str, int]
    watermark: WatermarkSpec
    fixture_pack: FixturePack

    # --- type-map normalization rules (let the generator rebuild
    #     pandas_dtype_for purely from this spec) ----------------------------
    array_dtype: str = "string"
    unknown_dtype: str = "string"
    # native type -> canonical coercer key (see conformance.coercers.REGISTRY).
    coercer_map: Mapping[str, str] = field(default_factory=dict)
    array_coercer: str = "array"

    # --- behavioral hooks (wired to hand-built or generated code) ----------
    coerce_value: Callable[[Any, str], Any] | None = None
    pandas_dtype_for: Callable[[str], str] | None = None

    # --- Gate B forward-compat --------------------------------------------
    pushdown: PushdownStub = field(default_factory=PushdownStub)

    def require_hooks(self) -> None:
        if self.coerce_value is None or self.pandas_dtype_for is None:
            raise ValueError(
                f"SourceSpec({self.dialect!r}) is missing behavioral hooks; "
                "coerce_value and pandas_dtype_for must be wired before Gate A"
            )


__all__ = [
    "UNSET",
    "FixtureCase",
    "FixturePack",
    "WatermarkSpec",
    "PushdownStub",
    "SourceSpec",
]
