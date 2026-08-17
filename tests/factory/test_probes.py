"""Probe registry and SQL shaping.

The probe exists so the B-2 boundary check can ask the SOURCE what it holds
without going through the driver under test. These tests pin the two properties
that make that guarantee real: the registry refuses an unknown dialect loudly
(a campaign that forgot its probe gets an instruction, not a skipped check), and
the probe's own SQL survives an inline-SQL source without silently emitting
something malformed.

No network here — `_from_clause` and `resolve` are both pure.
"""

from __future__ import annotations

import pytest

from factory import probes


def test_registry_resolves_a_registered_dialect() -> None:
    assert probes.resolve("clickhouse") is probes.ClickHouseHttpProbe


def test_registry_refuses_an_unknown_dialect_and_lists_what_it_has() -> None:
    with pytest.raises(probes.ProbeError) as exc:
        probes.resolve("nope")
    message = str(exc.value)
    assert "clickhouse" in message
    assert "registered:" in message


def test_a_missing_probe_is_an_instruction_not_a_permission_to_skip() -> None:
    """B-2 is mandatory, so 'no probe' must read as 'write one', never as 'skip'."""
    with pytest.raises(probes.ProbeError) as exc:
        probes.resolve("snowflake")
    assert "cannot be admitted" in str(exc.value)


@pytest.mark.parametrize(
    "source,expected",
    [
        ("meshbench.perf_1m", "meshbench.perf_1m"),
        ("perf_1m", "perf_1m"),
        (
            "SELECT * FROM meshbench.perf_1m ORDER BY row_id",
            "(SELECT * FROM meshbench.perf_1m ORDER BY row_id) AS sub",
        ),
        (
            "  select a from t  ",
            "(select a from t) AS sub",
        ),
    ],
)
def test_from_clause_wraps_inline_sql_and_leaves_table_names_alone(
    source: str, expected: str
) -> None:
    """The meshbench target pins its scan order with an inline SELECT, so a probe
    that did not wrap it would emit `FROM SELECT * FROM ...` and fail the
    boundary check for a reason that has nothing to do with the boundary."""
    assert probes._from_clause(source) == expected


def test_from_clause_agrees_with_the_driver_on_what_inline_sql_is() -> None:
    """If the probe and the driver disagreed about which sources are inline SQL,
    they would be reading two different things and the comparison would be
    meaningless."""
    from r64_db_engine.drivers.clickhouse.driver import _is_inline_sql

    for source in (
        "meshbench.perf_1m",
        "SELECT * FROM meshbench.perf_1m ORDER BY row_id",
        "select a from t",
        "meshbench.perf_1m\n",
    ):
        wrapped = probes._from_clause(source) != source.strip()
        assert wrapped == _is_inline_sql(source), source


def test_describe_never_contains_a_password() -> None:
    """Credential law: the endpoint description goes into the evidence pack."""
    probe = probes.ClickHouseHttpProbe(
        {"host": "h", "port": 9999, "database": "db", "user": "u", "password": "hunter2"}
    )
    assert "hunter2" not in probe.describe()
    assert "h:9999" in probe.describe()


def test_recorded_queries_never_contain_a_password() -> None:
    probe = probes.ClickHouseHttpProbe(
        {"host": "127.0.0.1", "port": 1, "database": "db", "user": "u", "password": "hunter2"}
    )
    with pytest.raises(probes.ProbeError):
        probe.session_timezone()
    assert probe.queries() == ["SELECT timezone()"]
    assert all("hunter2" not in q for q in probe.queries())


def test_probe_failure_raises_rather_than_returning_a_sentinel() -> None:
    """A probe that returned an empty bound on failure would make B-2 compare
    nothing against nothing and pass."""
    probe = probes.ClickHouseHttpProbe({"host": "127.0.0.1", "port": 1, "database": "db"})
    with pytest.raises(probes.ProbeError):
        probe.bounds("t", "c")
