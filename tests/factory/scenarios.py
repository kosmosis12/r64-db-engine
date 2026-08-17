"""Complete conformance scenarios, and the harness that replays them.

# Why a whole scenario rather than one check's inputs

Round 1 asserted a triple per case: FAIL, the check name, the reason code. Codex
round 2 showed that is still weaker than it looks, because the harness HANDED
THE STUB THE ANSWER — the case knew which check it targeted and passed that in.
An adaptive stub given case metadata can echo it back and pass without ever
inspecting an input.

So two things change here.

**Isolation.** An oracle — real or stubbed — receives exactly what the real
battery receives: the scenario's inputs. It never sees the case id, the target
check, the expected code, or the manifest. `run_battery` is the only caller and
it passes nothing else.

**Full vector.** A fixture is a COMPLETE scenario, and its manifest declares the
verdict of every one of the ten checks — not merely the one that was broken. A
catch-all stub fails the vector on the nine checks that should have passed; an
echo stub cannot construct the vector at all without the metadata it no longer
receives.

# Anonymization

`make_scenario` takes its identifiers as a parameter, so the meta-replays run
against content-addressed names (`c0_9f2a…`, `t_5b31…`) instead of `score`,
`event_time`, `perf_1m`. A stub cannot pattern-match its way to a verdict on
names that are a hash of the run. The real battery must produce the SAME vector
on the anonymized copy, which is independently worth asserting: it proves the
battery reaches its verdicts structurally rather than by recognising meshbench.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from factory.battery import CHECK_NAMES, FAIL, PASS, SKIPPED, CheckResult

# ---------------------------------------------------------------------------
# Identifiers — realistic by default, content-addressed for meta-replays
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Names:
    """Every identifier a scenario mentions, in one place so it can be swapped."""

    dialect: str = "clickhouse"
    other_dialect: str = "postgres"
    table: str = "perf_1m"
    int_col: str = "row_id"
    str_col: str = "region"
    time_col: str = "event_time"
    null_col: str = "score"
    sum_col: str = "quantity"

    @classmethod
    def anonymized(cls, salt: str) -> Names:
        """Content-addressed names, so nothing is recognisable by spelling."""

        def h(tag: str) -> str:
            return hashlib.sha256(f"{salt}:{tag}".encode()).hexdigest()[:10]

        return cls(
            dialect=f"d_{h('dialect')}",
            other_dialect=f"d_{h('other')}",
            table=f"t_{h('table')}",
            int_col=f"c_{h('int')}",
            str_col=f"c_{h('str')}",
            time_col=f"c_{h('time')}",
            null_col=f"c_{h('null')}",
            sum_col=f"c_{h('sum')}",
        )


REAL = Names()


# ---------------------------------------------------------------------------
# The scenario
# ---------------------------------------------------------------------------


@dataclass
class Scenario:
    """Every input the ten checks consume. No expectations live here."""

    names: Names

    registered: list[str] = field(default_factory=list)
    resolve_fn: Callable[[str], Any] | None = None
    validate_fn: Callable[[str], Any] | None = None

    observed_schema: dict[str, str] = field(default_factory=dict)
    spec_columns: list[dict[str, str]] = field(default_factory=list)
    string_width_tolerant: bool = True

    computed: dict[str, Any] = field(default_factory=dict)
    ground_truth: dict[str, Any] = field(default_factory=dict)
    aggregate_specs: list[dict[str, Any]] = field(default_factory=list)

    row_count: int = 1_000_000
    null_counts: dict[str, int] = field(default_factory=dict)
    nan_counts: dict[str, int] = field(default_factory=dict)
    rf_spec: dict[str, Any] = field(default_factory=dict)

    artifact_bounds: dict[str, tuple[Any, Any]] = field(default_factory=dict)
    source_bounds: dict[str, tuple[Any, Any]] = field(default_factory=dict)
    boundary_columns: list[str] = field(default_factory=list)
    source_timezone: str = "UTC"

    build_fn: Callable[[], Any] | None = None

    observed_blocks: list[int] = field(default_factory=list)

    first_sha: str = ""
    second_sha: str = ""
    checksum_layers: dict[str, Any] = field(default_factory=dict)

    serve_cold: dict[str, Any] | None = None
    serve_warm: dict[str, Any] | None = None

    recipe_outcomes: list[tuple[str, bool, str]] | None = None


class _Driver:
    def __init__(self, name: str) -> None:
        self._name = name

    def dialect_name(self) -> str:
        return self._name


def make_scenario(names: Names = REAL) -> Scenario:
    """A complete, entirely healthy scenario. Every check PASSes on it.

    Negative fixtures are this, with exactly one thing broken — which is what
    makes the full verdict vector meaningful: nine PASSes are a claim about the
    mutation being surgical, not incidental.
    """
    registered = sorted([names.dialect, names.other_dialect])

    def resolve(dialect: str):
        if dialect not in registered:
            raise ValueError(
                f"unknown dialect '{dialect}' (available: {', '.join(registered)})"
            )
        return _Driver(dialect)

    def validate(dialect: str):
        if dialect not in registered:
            raise ValueError(
                f"unknown dialect '{dialect}' (registered: {', '.join(registered)})"
            )
        return object()

    def build():
        raise RuntimeError(
            f"sink 'arrow_ipc' cannot serve incremental mode (its output format is not "
            f"appendable in place), but these tables request it: {names.table}."
        )

    schema = {
        names.int_col: "int64",
        names.str_col: "large_string",
        names.null_col: "double",
        names.time_col: "timestamp[us]",
    }
    bounds = {names.time_col: ("2026-01-01 00:00:15.184566", "2026-06-29 23:59:30.942340")}
    ground_truth = {"count": 1_000_000, f"sum_{names.sum_col}": 250_480_021,
                    f"count_{names.null_col}_null": 20_039}

    return Scenario(
        names=names,
        registered=registered,
        resolve_fn=resolve,
        validate_fn=validate,
        observed_schema=dict(schema),
        spec_columns=[{"name": n, "type": t} for n, t in schema.items()],
        computed=dict(ground_truth),
        ground_truth=dict(ground_truth),
        aggregate_specs=[
            {"ground_truth_key": "count", "op": "row_count"},
            {"ground_truth_key": f"sum_{names.sum_col}", "op": "sum_int",
             "column": names.sum_col},
            {"ground_truth_key": f"count_{names.null_col}_null", "op": "null_count",
             "column": names.null_col},
        ],
        row_count=1_000_000,
        null_counts={names.null_col: 20_039},
        nan_counts={names.null_col: 0},
        rf_spec={
            "discriminators": [
                {
                    "column": names.null_col,
                    "ground_truth_key": f"count_{names.null_col}_null",
                    "expected_null_count": {names.table: 20_039},
                }
            ]
        },
        artifact_bounds=dict(bounds),
        source_bounds=dict(bounds),
        boundary_columns=[names.time_col],
        build_fn=build,
        observed_blocks=[65536] * 15 + [16960],
        first_sha="db2912df" * 8,
        second_sha="db2912df" * 8,
        checksum_layers={
            "first_rows": 1_000_000, "second_rows": 1_000_000,
            "first_schema": dict(schema), "second_schema": dict(schema),
            "first_blocks": [65536] * 15 + [16960],
            "second_blocks": [65536] * 15 + [16960],
            "first_aggregates": dict(ground_truth), "second_aggregates": dict(ground_truth),
        },
        serve_cold={"cache_hits": 0, "cache_misses": 32, "columns_decoded": 32,
                    "zero_copy_columns": 32, "copied_columns": 0},
        serve_warm={"cache_hits": 32, "cache_misses": 0, "columns_decoded": 0,
                    "zero_copy_columns": 0, "copied_columns": 0},
        recipe_outcomes=[("https->http downgrade", True, "RecipeSecurityError")],
    )


# ---------------------------------------------------------------------------
# The harness — an oracle sees ONLY these inputs
# ---------------------------------------------------------------------------


def run_battery(scenario: Scenario, oracle: Any, leak: Any = None) -> list[CheckResult]:
    """Run all ten checks against a scenario, through `oracle`.

    `oracle` is `factory.battery` for a real run and a stub for a meta-replay.
    Either way it receives exactly the scenario's inputs — no case id, no target
    check, no expected code, no manifest.

    `leak` exists solely for the fenced negative control that demonstrates the
    isolation is what stops the adaptive stub. It is handed over only if the
    oracle asks for it by implementing `receive_leak`, which the real battery
    does not and cannot: it is a module.
    """
    if leak is not None:
        receiver = getattr(oracle, "receive_leak", None)
        if receiver is not None:
            receiver(leak)

    s = scenario
    return [
        oracle.check_registry_admission(
            s.names.dialect, s.registered, s.resolve_fn, s.validate_fn
        ),
        oracle.check_schema_exactness(
            s.observed_schema, s.spec_columns,
            string_width_tolerant=s.string_width_tolerant,
        ),
        oracle.check_aggregate_parity(s.computed, s.ground_truth, s.aggregate_specs),
        oracle.check_rf002_discriminator(
            row_count=s.row_count,
            null_counts=s.null_counts,
            nan_value_counts=s.nan_counts,
            ground_truth=s.ground_truth,
            spec=s.rf_spec,
            table=s.names.table,
        ),
        oracle.check_b2_boundary(
            s.artifact_bounds, s.source_bounds, s.boundary_columns,
            source_timezone=s.source_timezone,
        ),
        oracle.check_pg011_refusal(s.build_fn),
        oracle.check_block_structure(s.observed_blocks, s.row_count),
        oracle.check_checksum(s.first_sha, s.second_sha, **s.checksum_layers),
        oracle.check_recipe_security(s.recipe_outcomes),
        oracle.check_serve_gate(s.serve_cold, s.serve_warm),
    ]


def verdict_vector(results: list[CheckResult]) -> dict[str, tuple[str, str]]:
    """The observable outcome of a run: per check, (status, reason_code)."""
    return {r.name: (r.status, r.reason_code) for r in results}


# ---------------------------------------------------------------------------
# Manifests
# ---------------------------------------------------------------------------


def all_pass() -> dict[str, tuple[str, str]]:
    return dict.fromkeys(CHECK_NAMES, (PASS, ""))


def vector_with(check: str, code: str, **others: tuple[str, str]) -> dict[str, tuple[str, str]]:
    """The healthy vector with one check failing for a declared reason.

    `others` overrides any check whose verdict the mutation also legitimately
    changes — declared explicitly, so a mutation with wider blast radius than
    intended shows up as a manifest that has to admit it.
    """
    vector = all_pass()
    vector[check] = (FAIL, code)
    vector.update(others)
    return vector


__all__ = [
    "FAIL",
    "PASS",
    "REAL",
    "SKIPPED",
    "Names",
    "Scenario",
    "all_pass",
    "make_scenario",
    "replace",
    "run_battery",
    "vector_with",
    "verdict_vector",
]
