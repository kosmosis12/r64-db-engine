"""The conformance battery: named checks, each PASS / FAIL / SKIPPED-with-reason.

# Why the checks are pure functions

Every check in this module is a *judge*: it takes already-gathered facts and
returns a verdict. It performs no I/O. `factory/conformance.py` does the
gathering (pull the artifact, query the live source, spin the serve) and hands
the result here.

That split is what makes the oracle testable. An oracle that cannot be shown to
FAIL is not an oracle — it is a green light with no bulb behind it. Because a
judge is pure, a unit test proves it can fail by handing it a deliberately
corrupted `Observation`, with no container, no network and no artifact
involved. `tests/factory/test_battery.py` carries one such broken fixture per
check, and that file is as load-bearing as this one.

# Verdict vocabulary

- **PASS** — the property was checked and holds.
- **FAIL** — the property was checked and does not hold.
- **SKIPPED** — the property could not be checked *here*, with a reason that
  names why. A skip is never silent and never implicit: a check that cannot
  find its own inputs FAILs, because "I could not look" and "there is nothing
  to look at" are different answers and only the second one is a skip.

Overall exit code is 0 only if every non-skipped check passed.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# Rows per Arrow IPC record batch, mirrored from
# `r64_db_engine.sinks.arrow_ipc._BLOCK_ROWS`. Deliberately re-stated as a
# literal rather than imported: the battery is an INDEPENDENT check of the
# block discipline, and importing the constant from the code under test would
# make the assertion tautological — a sink that changed its block size would
# silently drag the expectation along with it and stay green.
BLOCK_ROWS = 65536

PASS = "PASS"
FAIL = "FAIL"
SKIPPED = "SKIPPED"


@dataclass(frozen=True)
class Comparison:
    """One actual-vs-expected pair, recorded whether or not it passed.

    Both sides are always kept. An evidence pack that recorded only failures
    would let a reviewer confirm nothing — Law 2 is that the pack, not the
    diff, is the review artifact, so the pack has to carry what was compared.
    """

    label: str
    actual: Any
    expected: Any
    ok: bool
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "actual": _jsonable(self.actual),
            "expected": _jsonable(self.expected),
            "ok": self.ok,
            "note": self.note,
        }


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str = ""
    comparisons: list[Comparison] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    observations: dict[str, Any] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return self.status == FAIL

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "comparisons": [c.as_dict() for c in self.comparisons],
            "queries": list(self.queries),
            "observations": _jsonable(self.observations),
        }


def _jsonable(value: Any) -> Any:
    """Coerce numpy/pyarrow/datetime scalars into JSON-serializable form."""
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    for attr in ("item", "as_py", "isoformat"):
        fn = getattr(value, attr, None)
        if callable(fn):
            try:
                return _jsonable(fn())
            except Exception:  # noqa: BLE001 - fall through to str()
                pass
    return str(value)


def _verdict(comparisons: list[Comparison]) -> str:
    return PASS if all(c.ok for c in comparisons) else FAIL


# ---------------------------------------------------------------------------
# Observation — the gathered facts every judge reads
# ---------------------------------------------------------------------------


@dataclass
class Observation:
    """Facts gathered from one conformance run. No behaviour, just evidence.

    `conformance.py` fills this in; the judges below only read it. Tests build
    one by hand and corrupt a field to prove the matching judge can fail.
    """

    dialect: str = ""
    table: str = ""
    source: str = ""

    # Artifact facts (first pull)
    row_count: int = 0
    schema: dict[str, str] = field(default_factory=dict)
    block_rows: list[int] = field(default_factory=list)
    null_counts: dict[str, int] = field(default_factory=dict)
    nan_value_counts: dict[str, int] = field(default_factory=dict)
    aggregates: dict[str, Any] = field(default_factory=dict)
    column_bounds: dict[str, tuple[Any, Any]] = field(default_factory=dict)
    artifact_sha256: str = ""
    artifact_bytes: int = 0

    # Second pull, same lane — checksum determinism
    second_sha256: str = ""
    second_row_count: int = 0
    second_schema: dict[str, str] = field(default_factory=dict)
    second_block_rows: list[int] = field(default_factory=list)
    second_aggregates: dict[str, Any] = field(default_factory=dict)

    # Live source facts
    source_bounds: dict[str, tuple[Any, Any]] = field(default_factory=dict)
    source_queries: list[str] = field(default_factory=list)
    source_timezone: str = ""


# ---------------------------------------------------------------------------
# 1. Registry admission (PG-010)
# ---------------------------------------------------------------------------


def check_registry_admission(
    dialect: str,
    registered: list[str],
    resolve_fn: Callable[[str], Any],
    validate_config_fn: Callable[[str], Any],
    *,
    bogus_dialect: str = "definitely-not-a-registered-dialect",
) -> CheckResult:
    """The dialect resolves through the driver registry; an unknown one is refused.

    Two arms, because they are two different surfaces and a driver can be
    admitted on one while broken on the other:

    - **Positive** — `drivers.resolve(dialect)` returns a class whose own
      `dialect_name()` agrees with the config. A registry entry filed under the
      wrong key resolves fine and pulls the wrong driver, so the round-trip
      through `dialect_name()` is the check, not mere presence in the dict.
    - **Negative** — a mutated, unregistered dialect is refused *loudly*, at
      both `drivers.resolve()` and `Config` validation, and the refusal message
      LISTS THE REGISTERED DIALECTS. That listing is the part that matters:
      PG-010 was a config that could not name a driver that existed, and the
      operator-facing cure is an error that says what it would have accepted.

    A raise alone is not enough to pass. An error that says only "unknown
    dialect" sends the operator to the source; an error that enumerates the
    registry answers the question in place.
    """
    comparisons: list[Comparison] = []

    try:
        driver_cls = resolve_fn(dialect)
        resolved = driver_cls.dialect_name()
    except Exception as exc:  # noqa: BLE001 - any failure here is a FAIL
        return CheckResult(
            name="registry_admission",
            status=FAIL,
            detail=f"registry refused the configured dialect {dialect!r}: {exc}",
            comparisons=[Comparison("resolve(dialect)", f"raised {exc!r}", dialect, False)],
        )

    comparisons.append(
        Comparison("resolved driver dialect_name()", resolved, dialect, resolved == dialect)
    )
    comparisons.append(
        Comparison(
            "dialect present in registry listing",
            sorted(registered),
            f"contains {dialect!r}",
            dialect in registered,
        )
    )

    for label, fn in (("drivers.resolve", resolve_fn), ("Config validation", validate_config_fn)):
        try:
            fn(bogus_dialect)
        except Exception as exc:  # noqa: BLE001 - the refusal is the pass condition
            message = str(exc)
            comparisons.append(
                Comparison(f"{label}: refuses unregistered dialect", "raised", "raised", True)
            )
            listed = [d for d in registered if d in message]
            comparisons.append(
                Comparison(
                    f"{label}: refusal lists registered dialects",
                    sorted(listed),
                    sorted(registered),
                    sorted(listed) == sorted(registered),
                    note=f"message: {message[:400]}",
                )
            )
        else:
            comparisons.append(
                Comparison(
                    f"{label}: refuses unregistered dialect",
                    "accepted silently",
                    "raised",
                    False,
                    note=f"{bogus_dialect!r} was not refused",
                )
            )

    return CheckResult(
        name="registry_admission",
        status=_verdict(comparisons),
        detail=f"dialect {dialect!r} against registry {sorted(registered)}",
        comparisons=comparisons,
    )


# ---------------------------------------------------------------------------
# 2. Schema exactness (with the B-3 cross-lane string-width fence)
# ---------------------------------------------------------------------------

_LARGE_STRING = re.compile(r"\blarge_string\b")


def normalize_string_width(arrow_type: str) -> str:
    """Fold `large_string` onto `string` — the B-3 fence, applied to one type.

    Both are valid Arrow strings and meshroad reads both. Which one you get is
    a property of the LANE, not of fidelity: pandas 3.0's string dtype yields
    `large_string`, DuckDB's native Arrow export yields plain utf8. Comparing
    them literally would fail an artifact that is correct in every way that
    matters to a consumer.

    Applied by regex rather than by string equality so it also reaches inside a
    composite type — `dictionary<values=large_string, indices=int32>` normalizes
    to `dictionary<values=string, indices=int32>`. `\\blarge_string\\b` cannot
    touch `large_binary` or `large_list`, which are NOT covered by the fence.
    """
    return _LARGE_STRING.sub("string", arrow_type)


def check_schema_exactness(
    observed: dict[str, str],
    spec_columns: list[dict[str, str]],
    *,
    string_width_tolerant: bool = True,
) -> CheckResult:
    """Artifact schema vs the committed spec — column set, ORDER, and types.

    Column order is asserted, not just the set. The consumer's zero-copy reader
    is built against a field layout; a reordered schema with identical types is
    a different artifact to anything that indexes by position.
    """
    if not spec_columns:
        return CheckResult(
            name="schema_exactness",
            status=FAIL,
            detail="spec declares no columns; a schema spec with no columns cannot gate anything",
        )

    norm = normalize_string_width if string_width_tolerant else (lambda s: s)
    expected_names = [c["name"] for c in spec_columns]
    actual_names = list(observed)

    comparisons = [
        Comparison("column count", len(actual_names), len(expected_names),
                   len(actual_names) == len(expected_names)),
        Comparison("column names and order", actual_names, expected_names,
                   actual_names == expected_names),
    ]

    for col in spec_columns:
        name = col["name"]
        want = col["type"]
        got = observed.get(name, "<MISSING>")
        ok = norm(got) == norm(want)
        note = ""
        if ok and got != want:
            note = f"string-width fence applied (B-3): {got} accepted for {want}"
        comparisons.append(Comparison(f"type[{name}]", got, want, ok, note=note))

    return CheckResult(
        name="schema_exactness",
        status=_verdict(comparisons),
        detail=(
            f"{len(expected_names)} columns, string-width tolerance "
            f"{'ON (B-3)' if string_width_tolerant else 'OFF'}"
        ),
        comparisons=comparisons,
    )


# ---------------------------------------------------------------------------
# 3. Aggregate parity vs source-captured ground truth
# ---------------------------------------------------------------------------


def check_aggregate_parity(
    computed: dict[str, Any],
    ground_truth: dict[str, Any],
    aggregate_specs: list[dict[str, Any]],
) -> CheckResult:
    """Every declared aggregate against ground truth captured AT THE SOURCE.

    The expectations are a committed file, not a re-measurement of the
    pipeline's own output. A pipeline checked against itself proves only that
    it is self-consistent.

    # The authority rule

    An aggregate marked `corroborating: true` is recorded but does NOT gate.
    That exists for exactly one situation, and `scaled_amount_sum` is it:
    `round(sum(amount) * 100)` sums float64 in parallel at the source, and float
    addition is not associative, so the value is in principle order-sensitive.
    `sum(cast(round(amount * 100) as bigint))` computes the same quantity with
    order-independent integer arithmetic. The integer form GATES; the float form
    is kept alongside it because agreement between the two is real evidence, and
    disagreement is a finding worth surfacing rather than a reason to fail a
    driver that transported every row correctly.

    A corroborating mismatch is therefore reported in the pack as a note, and
    the check still passes. If you find yourself wanting to mark a second
    aggregate corroborating, that is a signal to fix the aggregate, not to widen
    the exemption.
    """
    if not aggregate_specs:
        return CheckResult(
            name="aggregate_parity",
            status=FAIL,
            detail="spec declares no aggregates; parity cannot be gated on an empty set",
        )

    comparisons: list[Comparison] = []
    gating_ok = True
    corroboration_notes: list[str] = []

    for spec in aggregate_specs:
        key = spec["ground_truth_key"]
        corroborating = bool(spec.get("corroborating", False))
        tolerance = float(spec.get("tolerance", 0.0))

        if key not in ground_truth:
            comparisons.append(
                Comparison(f"{key}", "<not in ground truth>", "<declared by spec>", False,
                           note="spec names a ground-truth key the ground-truth file lacks")
            )
            gating_ok = False
            continue
        if key not in computed:
            comparisons.append(
                Comparison(f"{key}", "<not computed>", ground_truth[key], False,
                           note=f"op {spec.get('op')!r} produced no value")
            )
            gating_ok = False
            continue

        actual = computed[key]
        expected = ground_truth[key]
        if tolerance > 0 and isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
            ok = math.isclose(float(actual), float(expected), abs_tol=tolerance)
        else:
            ok = actual == expected

        note = ""
        if corroborating:
            note = "corroborating only — does not gate (float-order sensitivity)"
            if not ok:
                corroboration_notes.append(
                    f"{key}: pipeline {actual} vs ground truth {expected} "
                    f"(corroborating form disagreed; the exact-int form is the authority)"
                )
        elif not ok:
            gating_ok = False

        comparisons.append(Comparison(key, actual, expected, ok, note=note))

    detail = f"{len(aggregate_specs)} aggregates vs ground truth"
    if corroboration_notes:
        detail += " | " + "; ".join(corroboration_notes)

    return CheckResult(
        name="aggregate_parity",
        status=PASS if gating_ok else FAIL,
        detail=detail,
        comparisons=comparisons,
    )


# ---------------------------------------------------------------------------
# 4. RF-002 null discriminator
# ---------------------------------------------------------------------------


def check_rf002_discriminator(
    row_count: int,
    null_counts: dict[str, int],
    nan_value_counts: dict[str, int],
    ground_truth: dict[str, Any],
    spec: dict[str, Any],
    table: str,
) -> CheckResult:
    """NULL survives to the artifact as a true Arrow null — dataset-declared.

    # Why a bare "at least one column has nulls" is too weak

    Both ways of destroying null-ness are SILENT. Filling with 0.0 drags every
    `mean()` toward zero while every count and sum still looks plausible;
    smuggling a literal NaN through sets `null_count = 0` and poisons every
    `sum()` downstream. Neither shows up in a row count.

    The discriminator is that `count(*)` and `count(col)` DISAGREE. If they
    agree, null-ness was destroyed in transit no matter what the totals say.

    # Dataset-declared, not a floating threshold

    The spec must NAME the columns expected to discriminate and their exact
    null counts for this table. A generic "≥ N nullable columns" rule silently
    weakens as datasets change — and it does not generalize: meshbench has
    exactly one nullable column, so a floor of 2 would be unsatisfiable here
    while a floor of 1 is vacuous on a dataset with fifty.

    So the gate is: **every declared discriminator discriminates, with exact
    counts, and at least one is declared.** The spec's count is additionally
    cross-checked against the ground-truth file — two independently maintained
    records of the same number, where disagreement is itself the finding.

    A spec may declare zero discriminators ONLY with an explicit
    `discriminators_absent_reason`, which yields SKIPPED-with-reason. Omitting
    the key entirely is a FAIL, because "there is nothing nullable here" and
    "nobody thought about it" must not produce the same verdict.
    """
    if "discriminators" not in spec:
        return CheckResult(
            name="rf002_null_discriminator",
            status=FAIL,
            detail=(
                "spec has no 'discriminators' key. RF-002 requires the dataset to declare "
                "its expected discriminating columns (floor: 1), or to declare "
                "'discriminators': [] together with 'discriminators_absent_reason'."
            ),
        )

    declared = spec["discriminators"] or []
    if not declared:
        reason = spec.get("discriminators_absent_reason")
        if not reason:
            return CheckResult(
                name="rf002_null_discriminator",
                status=FAIL,
                detail=(
                    "spec declares zero discriminators without "
                    "'discriminators_absent_reason'. A silent zero is not a skip."
                ),
            )
        return CheckResult(
            name="rf002_null_discriminator",
            status=SKIPPED,
            detail=f"no nullable column to discriminate on: {reason}",
        )

    comparisons: list[Comparison] = []
    for entry in declared:
        col = entry["column"]
        expected_map = entry.get("expected_null_count", {})
        expected = expected_map.get(table)

        if expected is None:
            comparisons.append(
                Comparison(f"{col}: declared for table {table}", "<absent>", "<an integer>", False,
                           note="spec declares this discriminator but not for this table")
            )
            continue

        gt_key = entry.get("ground_truth_key")
        if gt_key is not None:
            gt_value = ground_truth.get(gt_key)
            comparisons.append(
                Comparison(
                    f"{col}: spec vs ground-truth null count",
                    expected,
                    gt_value,
                    expected == gt_value,
                    note=(
                        "two independent records of the same number; disagreement means one "
                        "of the two files is stale"
                    ),
                )
            )

        actual_nulls = null_counts.get(col)
        comparisons.append(
            Comparison(f"{col}: artifact null_count", actual_nulls, expected, actual_nulls == expected)
        )

        count_col = row_count - (actual_nulls or 0)
        comparisons.append(
            Comparison(
                f"{col}: count(col) vs count(*)",
                f"{count_col} vs {row_count}",
                "must differ",
                count_col != row_count,
                note="if equal, nulls were filled in transit",
            )
        )

        nan_values = nan_value_counts.get(col, 0)
        comparisons.append(
            Comparison(
                f"{col}: NaN smuggled as a value",
                nan_values,
                0,
                nan_values == 0,
                note="a literal NaN sets null_count=0 and poisons every downstream sum()",
            )
        )

    return CheckResult(
        name="rf002_null_discriminator",
        status=_verdict(comparisons),
        detail=f"{len(declared)} declared discriminator(s) on {table}: "
        f"{', '.join(e['column'] for e in declared)}",
        comparisons=comparisons,
    )


# ---------------------------------------------------------------------------
# 5. B-2 boundary
# ---------------------------------------------------------------------------


def check_b2_boundary(
    artifact_bounds: dict[str, tuple[Any, Any]],
    source_bounds: dict[str, tuple[Any, Any]],
    boundary_columns: list[str],
    *,
    source_timezone: str = "",
    queries: list[str] | None = None,
) -> CheckResult:
    """min/max on a tz- or order-sensitive column, artifact vs the LIVE source.

    # The transfer doctrine, and why this check is not optional

    > Any cross-engine dataset transfer requires a min/max boundary assertion on
    > at least one timezone- or order-sensitive column. Aggregate parity is
    > blind to uniform shifts.

    Counts, sums, null counts and cardinalities are all invariant under a
    uniform translation of a column. Shift every timestamp by eight hours and
    every one of them still matches — which is exactly what B-2 found on the
    DuckDB lane, where a default session TimeZone of `America/Los_Angeles`
    moved every `event_time` by the local UTC offset and *every ground-truth
    check still passed*. The artifact would have been eight hours wrong and
    fully green.

    A boundary assertion is the cheapest check that is NOT invariant under that
    shift, which is precisely why it must be present.

    The source's session timezone is recorded in the pack alongside the values,
    because a matching pair of bounds means something different when the source
    session is UTC than when it is not.
    """
    if not boundary_columns:
        return CheckResult(
            name="b2_boundary",
            status=FAIL,
            detail=(
                "spec declares no boundary columns. The transfer doctrine requires at least "
                "one timezone- or order-sensitive column; aggregate parity is blind to "
                "uniform shifts."
            ),
        )

    comparisons: list[Comparison] = []
    for col in boundary_columns:
        if col not in source_bounds:
            comparisons.append(
                Comparison(f"{col}: source bounds", "<not probed>", "<min,max from source>", False,
                           note="the live source was not queried for this column")
            )
            continue
        if col not in artifact_bounds:
            comparisons.append(
                Comparison(f"{col}: artifact bounds", "<column absent>", source_bounds[col], False)
            )
            continue

        a_min, a_max = artifact_bounds[col]
        s_min, s_max = source_bounds[col]
        comparisons.append(Comparison(f"{col}: min", a_min, s_min, str(a_min) == str(s_min)))
        comparisons.append(Comparison(f"{col}: max", a_max, s_max, str(a_max) == str(s_max)))

    return CheckResult(
        name="b2_boundary",
        status=_verdict(comparisons),
        detail=(
            f"boundary columns {boundary_columns} vs live source"
            + (f" (source session timezone: {source_timezone})" if source_timezone else "")
        ),
        comparisons=comparisons,
        queries=list(queries or []),
        observations={"source_timezone": source_timezone},
    )


# ---------------------------------------------------------------------------
# 6. PG-011 refusal
# ---------------------------------------------------------------------------


def check_pg011_refusal(
    build_fn: Callable[[], Any],
    *,
    expected_substring: str = "cannot serve incremental mode",
) -> CheckResult:
    """Incremental mode against a non-appendable sink is refused, loudly and early.

    The daemon's incremental path merges by reading the sink's OWN previous
    output back in. For a format whose layout is not appendable in place — an
    Arrow IPC file ends with a footer carrying the block table — that merge
    cannot be done correctly.

    Two properties are asserted, and the second is the one that matters:

    1. It raises rather than proceeding.
    2. It raises rather than being SILENTLY DOWNGRADED to full_refresh. A silent
       downgrade writes a partial snapshot that is indistinguishable, to the
       consumer, from a complete one — strictly worse than refusing to start,
       because it fails later and somewhere else.

    Proving (2) from the outside is why the expected message substring is part
    of the contract: an exception of some other kind, from some other cause,
    would satisfy a bare `pytest.raises` and tell us nothing.
    """
    try:
        build_fn()
    except Exception as exc:  # noqa: BLE001 - the refusal is the pass condition
        message = str(exc)
        matched = expected_substring in message
        return CheckResult(
            name="pg011_refusal",
            status=PASS if matched else FAIL,
            detail=f"refused with: {message[:400]}",
            comparisons=[
                Comparison("incremental on non-appendable sink", "raised", "raised", True),
                Comparison("refusal names the cause", message[:400], expected_substring, matched),
            ],
        )
    return CheckResult(
        name="pg011_refusal",
        status=FAIL,
        detail=(
            "incremental mode was ACCEPTED on a non-appendable sink. Either the guard is "
            "gone or the mode was silently downgraded to full_refresh."
        ),
        comparisons=[
            Comparison("incremental on non-appendable sink", "accepted", "raised", False)
        ],
    )


# ---------------------------------------------------------------------------
# 7. Block structure
# ---------------------------------------------------------------------------


def expected_block_rows(row_count: int, block_rows: int = BLOCK_ROWS) -> list[int]:
    """The block layout a row count must produce: full blocks, then a remainder.

    An empty table yields NO batches at all — a valid IPC file carrying just the
    schema, and what the pre-D-4 `write_feather` produced for that case too.
    """
    if row_count <= 0:
        return []
    full, remainder = divmod(row_count, block_rows)
    layout = [block_rows] * full
    if remainder:
        layout.append(remainder)
    return layout


def check_block_structure(
    observed_blocks: list[int],
    row_count: int,
    *,
    block_rows: int = BLOCK_ROWS,
) -> CheckResult:
    """Arrow IPC block sizes obey the 65536 discipline.

    Block granularity is not a tuning knob: it is the granularity the
    consumer's per-block column cache is keyed on. A single one-block file
    would collapse that granularity and turn a warm-pass `columns_decoded = 0`
    into a whole-file decode — a *performance* symptom surfacing long after the
    change that caused it, which is the hardest kind of regression to trace.

    Asserted as the full layout rather than as a block count, so a file with
    the right number of unevenly-sized blocks cannot pass.
    """
    expected = expected_block_rows(row_count, block_rows)
    comparisons = [
        Comparison("block count", len(observed_blocks), len(expected),
                   len(observed_blocks) == len(expected)),
        Comparison("rows across blocks", sum(observed_blocks), row_count,
                   sum(observed_blocks) == row_count),
        Comparison("block layout", observed_blocks, expected, observed_blocks == expected,
                   note=f"{block_rows}-row blocks, final block carries the remainder"),
    ]
    return CheckResult(
        name="block_structure",
        status=_verdict(comparisons),
        detail=f"{len(expected)} blocks expected for {row_count} rows at {block_rows}/block",
        comparisons=comparisons,
    )


# ---------------------------------------------------------------------------
# 8. Checksum (lane-scoped)
# ---------------------------------------------------------------------------


def check_checksum(
    first_sha: str,
    second_sha: str,
    *,
    first_rows: int = 0,
    second_rows: int = 0,
    first_schema: dict[str, str] | None = None,
    second_schema: dict[str, str] | None = None,
    first_blocks: list[int] | None = None,
    second_blocks: list[int] | None = None,
    first_aggregates: dict[str, Any] | None = None,
    second_aggregates: dict[str, Any] | None = None,
) -> CheckResult:
    """Two consecutive pulls, SAME LANE, must be byte-identical.

    # The lane fence

    Byte-identity is asserted only WITHIN a lane. Cross-lane equivalence is
    **data + schema-minus-metadata + block structure**, additionally tolerating
    string width (`string` vs `large_string`) — never bytes. The same DuckDB
    source through the Arrow lane and through the DataFrame lane produces
    different shas and both artifacts are correct. Never compare an N sha to a
    P' sha.

    # On mismatch, name the nondeterminism

    A checksum that just says "differs" is a dead end. When the shas disagree
    this check decomposes the difference — row count, schema, block layout,
    aggregates — so the residual nondeterminism is NAMED rather than left as a
    mystery. If every decomposed layer agrees and only the bytes differ, that is
    itself the finding: something outside the data (metadata, ordering within a
    block, a writer version) is varying, and the pack says so instead of
    failing silently.
    """
    if first_sha and first_sha == second_sha:
        return CheckResult(
            name="checksum",
            status=PASS,
            detail=f"two consecutive same-lane pulls are byte-identical (sha256 {first_sha[:16]}…)",
            comparisons=[Comparison("sha256 (pull 1 vs pull 2)", first_sha, second_sha, True)],
            observations={
                "lane_scope": (
                    "byte-identity asserted WITHIN this lane only; cross-lane comparison uses "
                    "data + schema-minus-metadata + block structure, with string-width tolerance"
                )
            },
        )

    layers = [
        Comparison("row count", first_rows, second_rows, first_rows == second_rows),
        Comparison("schema", first_schema or {}, second_schema or {},
                   (first_schema or {}) == (second_schema or {})),
        Comparison("block layout", first_blocks or [], second_blocks or [],
                   (first_blocks or []) == (second_blocks or [])),
        Comparison("aggregates", first_aggregates or {}, second_aggregates or {},
                   (first_aggregates or {}) == (second_aggregates or {})),
    ]
    differing = [c.label for c in layers if not c.ok]
    if differing:
        residual = (
            "the artifacts differ in DATA-BEARING layers: "
            + ", ".join(differing)
            + ". This is a fidelity defect, not a serialization nuance."
        )
    else:
        residual = (
            "data, schema and block structure are all identical; only the BYTES differ. "
            "The residual nondeterminism is therefore outside the data — candidates, in "
            "order of likelihood: embedded schema metadata (the pandas metadata block "
            "carries library versions), dictionary ordering, or row order within a block "
            "from an unordered source scan. Pin a scan order and re-run to discriminate."
        )

    return CheckResult(
        name="checksum",
        status=FAIL,
        detail=residual,
        comparisons=[Comparison("sha256 (pull 1 vs pull 2)", first_sha, second_sha, False), *layers],
    )


# ---------------------------------------------------------------------------
# 9. Zero-copy serve gate (optional)
# ---------------------------------------------------------------------------


def check_serve_gate(
    cold: dict[str, Any] | None,
    warm: dict[str, Any] | None,
    *,
    skipped_reason: str = "",
) -> CheckResult:
    """`copied_columns = 0` cold and warm; the warm pass misses nothing.

    Counters are read from the server's OWN instrumentation via
    `get_flight_info` app_metadata, not inferred by the harness from timings.

    They are CUMULATIVE over the server's lifetime, so the values judged here
    are DELTAS between snapshots taken around each pass — a raw warm snapshot
    reports the cold pass's misses too and would show a 50% miss rate on a
    perfectly warm cache.

    Three assertions:

    - `copied_columns == 0` on BOTH passes. Every cached array is a view into
      the mmap, not a heap copy. This is the zero-copy claim itself.
    - Cold: `zero_copy_columns == columns_decoded`, i.e. everything the cold
      pass decoded, it decoded in place.
    - Warm: `miss_rate == 0%` AND `columns_decoded == 0`. The second is the
      stronger statement and the one that would catch a collapsed block layout:
      a cache can report a 0% miss rate while still decoding, but a warm pass
      that decodes nothing at all has genuinely served from the mmap.
    """
    if cold is None or warm is None:
        return CheckResult(
            name="zero_copy_serve_gate",
            status=SKIPPED,
            detail=skipped_reason or "serve gate not requested (pass --serve-gate to run it)",
        )

    def miss_rate(delta: dict[str, Any]) -> float:
        total = delta.get("cache_hits", 0) + delta.get("cache_misses", 0)
        return 0.0 if total == 0 else 100.0 * delta["cache_misses"] / total

    comparisons = [
        Comparison("cold: copied_columns", cold.get("copied_columns"), 0,
                   cold.get("copied_columns") == 0),
        Comparison("warm: copied_columns", warm.get("copied_columns"), 0,
                   warm.get("copied_columns") == 0),
        Comparison("cold: zero_copy_columns == columns_decoded",
                   f"{cold.get('zero_copy_columns')} vs {cold.get('columns_decoded')}",
                   "equal",
                   cold.get("zero_copy_columns") == cold.get("columns_decoded")),
        Comparison("cold: columns actually decoded", cold.get("columns_decoded"), "> 0",
                   (cold.get("columns_decoded") or 0) > 0,
                   note="a cold pass that decodes nothing did not exercise the reader"),
        Comparison("warm: miss_rate %", round(miss_rate(warm), 2), 0.0,
                   miss_rate(warm) == 0.0),
        Comparison("warm: columns_decoded", warm.get("columns_decoded"), 0,
                   warm.get("columns_decoded") == 0,
                   note="stronger than miss_rate: a warm pass must decode nothing at all"),
    ]

    return CheckResult(
        name="zero_copy_serve_gate",
        status=_verdict(comparisons),
        detail="counter deltas from the server's own get_flight_info app_metadata",
        comparisons=comparisons,
        observations={"cold_delta": cold, "warm_delta": warm},
    )


__all__ = [
    "BLOCK_ROWS",
    "FAIL",
    "PASS",
    "SKIPPED",
    "CheckResult",
    "Comparison",
    "Observation",
    "check_aggregate_parity",
    "check_b2_boundary",
    "check_block_structure",
    "check_checksum",
    "check_pg011_refusal",
    "check_registry_admission",
    "check_rf002_discriminator",
    "check_schema_exactness",
    "check_serve_gate",
    "expected_block_rows",
    "normalize_string_width",
]
