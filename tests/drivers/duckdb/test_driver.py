"""DuckDB driver unit tests. No `--integration`, no built dataset.

Uses `:memory:` throughout, so these run on a clean checkout.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pa = pytest.importorskip("pyarrow")
duckdb = pytest.importorskip("duckdb")

from r64_db_engine.core.driver import ArrowPullResult  # noqa: E402
from r64_db_engine.drivers.duckdb import coercion as ddb_coercion  # noqa: E402
from r64_db_engine.drivers.duckdb.driver import DuckDBDriver  # noqa: E402


def _seeded(tmp_path: Path, rows: int = 1000) -> Path:
    db = tmp_path / "seed.duckdb"
    con = duckdb.connect(str(db))
    con.execute(
        "CREATE TABLE t AS SELECT i AS row_id, "
        "CASE WHEN i % 7 = 0 THEN NULL ELSE i * 1.5 END AS score, "
        "'s' || (i % 3) AS status, "
        "TIMESTAMP '2026-01-01 00:00:00' + INTERVAL (i) SECOND AS ts "
        f"FROM range({rows}) t(i)"
    )
    con.close()
    return db


async def _connected(db: Path, **overrides) -> DuckDBDriver:
    driver = DuckDBDriver()
    await driver.connect({"database": str(db), **overrides})
    return driver


# --------------------------------------------------------------------------
# Registration and capability
# --------------------------------------------------------------------------


def test_dialect_name_is_duckdb() -> None:
    assert DuckDBDriver.dialect_name() == "duckdb"


def test_driver_is_registered_without_any_core_edit() -> None:
    """PG-010's live proof: a new dialect needs a registry entry and nothing else."""
    from r64_db_engine.drivers import DRIVERS, resolve

    assert "duckdb" in DRIVERS
    assert resolve("duckdb") is DuckDBDriver


def test_arrow_capability_is_advertised_and_switchable(tmp_path: Path) -> None:
    """The toggle exists so Phase C's P' cell is a CONFIG change, not a patch."""

    async def go() -> tuple[bool, bool]:
        db = _seeded(tmp_path)
        on = await _connected(db)
        off = await _connected(db, arrow=False)
        try:
            return on.supports_arrow(), off.supports_arrow()
        finally:
            await on.close()
            await off.close()

    assert asyncio.run(go()) == (True, False)


# --------------------------------------------------------------------------
# The Arrow lane
# --------------------------------------------------------------------------


def test_pull_arrow_returns_an_undrained_reader(tmp_path: Path) -> None:
    """The reader must arrive undrained — that is the whole memory bound."""

    async def go() -> ArrowPullResult:
        driver = await _connected(_seeded(tmp_path))
        try:
            return await driver.pull_arrow({"source": "main.t"}, None)
        finally:
            await driver.close()

    result = asyncio.run(go())
    assert isinstance(result, ArrowPullResult)
    assert isinstance(result.reader, pa.RecordBatchReader)
    assert result.new_watermark is None
    # Schema is readable without consuming rows.
    assert result.reader.schema.names == ["row_id", "score", "status", "ts"]
    assert result.reader.read_all().num_rows == 1000


def test_batch_size_is_honoured_and_defaults_to_the_block_size(tmp_path: Path) -> None:
    """Default batch == artifact block, so the sink's re-chunk buffer stays tight."""
    from r64_db_engine.sinks.arrow_ipc import _BLOCK_ROWS

    async def go(**overrides) -> list[int]:
        driver = await _connected(_seeded(tmp_path, rows=2500), **overrides)
        try:
            result = await driver.pull_arrow({"source": "main.t"}, None)
            return [b.num_rows for b in result.reader]
        finally:
            await driver.close()

    assert DuckDBDriver()._batch_rows == _BLOCK_ROWS
    assert asyncio.run(go(batch_size=1000)) == [1000, 1000, 500]


def test_arrow_lane_refuses_incremental(tmp_path: Path) -> None:
    async def go() -> None:
        driver = await _connected(_seeded(tmp_path))
        try:
            await driver.pull_arrow(
                {"source": "main.t", "mode": "incremental", "incremental_key": "row_id"},
                None,
            )
        finally:
            await driver.close()

    with pytest.raises(ValueError, match="full-refresh only"):
        asyncio.run(go())


def test_nulls_arrive_as_arrow_nulls_not_sentinels(tmp_path: Path) -> None:
    async def go() -> pa.Table:
        driver = await _connected(_seeded(tmp_path))
        try:
            result = await driver.pull_arrow({"source": "main.t"}, None)
            return result.reader.read_all()
        finally:
            await driver.close()

    table = asyncio.run(go())
    assert table.column("score").null_count == len(range(0, 1000, 7))


def test_inline_sql_is_passed_through_verbatim_to_preserve_ordering(
    tmp_path: Path,
) -> None:
    """Wrapping the source would bury ORDER BY in a subquery the planner may reorder."""
    driver = DuckDBDriver()
    source = "SELECT * FROM main.t ORDER BY row_id"

    assert driver._build_query(source=source, columns=None, max_rows=None) == source
    # A projection or limit forces a wrap, which is fine — the caller asked.
    assert driver._build_query(source=source, columns=["row_id"], max_rows=None) == (
        f'SELECT "row_id" FROM ({source}) AS sub'
    )


def test_ordered_inline_source_round_trips_in_order(tmp_path: Path) -> None:
    async def go() -> list[int]:
        driver = await _connected(_seeded(tmp_path, rows=500))
        try:
            result = await driver.pull_arrow(
                {"source": "SELECT * FROM main.t ORDER BY row_id DESC"}, None
            )
            return result.reader.read_all().column("row_id").to_pylist()
        finally:
            await driver.close()

    assert asyncio.run(go()) == list(range(499, -1, -1))


# --------------------------------------------------------------------------
# The DataFrame lane (P' cell)
# --------------------------------------------------------------------------


def test_dataframe_lane_returns_a_coerced_frame(tmp_path: Path) -> None:
    async def go():
        driver = await _connected(_seeded(tmp_path), arrow=False)
        try:
            return await driver.pull({"source": "main.t"}, None)
        finally:
            await driver.close()

    result = asyncio.run(go())
    assert result.rows_pulled == 1000
    assert list(result.dataframe.columns) == ["row_id", "score", "status", "ts"]
    assert result.dataframe["score"].isna().sum() == len(range(0, 1000, 7))


# --------------------------------------------------------------------------
# Config surface
# --------------------------------------------------------------------------


def test_database_is_required() -> None:
    with pytest.raises(ValueError, match="duckdb.database is required"):
        asyncio.run(DuckDBDriver().connect({}))


def test_batch_size_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="batch_size must be positive"):
        asyncio.run(
            DuckDBDriver().connect({"database": str(_seeded(tmp_path)), "batch_size": 0})
        )


def test_settings_must_be_a_mapping(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="settings must be a mapping"):
        asyncio.run(
            DuckDBDriver().connect({"database": str(_seeded(tmp_path)), "settings": "UTC"})
        )


def test_local_file_defaults_to_read_only(tmp_path: Path) -> None:
    """This driver ingests; it does not own the database."""

    async def go() -> None:
        db = _seeded(tmp_path)
        driver = await _connected(db)
        try:
            with pytest.raises(Exception, match="read-only|read only"):
                await driver._fetch("CREATE TABLE nope (i INTEGER)")
        finally:
            await driver.close()

    asyncio.run(go())


def test_in_memory_is_exempt_from_read_only() -> None:
    """A read-only fresh in-memory database would be permanently empty."""

    async def go() -> list[tuple]:
        driver = DuckDBDriver()
        await driver.connect({"database": ":memory:"})
        try:
            await driver._fetch("CREATE TABLE ok (i INTEGER)")
            return await driver._fetch("SELECT count(*) FROM ok")
        finally:
            await driver.close()

    assert asyncio.run(go()) == [(0,)]


def test_motherduck_dsn_token_is_never_logged() -> None:
    from r64_db_engine.drivers.duckdb.driver import _redact

    assert _redact("md:mydb?motherduck_token=SECRET") == "md:mydb?<redacted>"
    assert "SECRET" not in _redact("md:mydb?motherduck_token=SECRET")
    assert _redact("/local/path.duckdb") == "/local/path.duckdb"


# --------------------------------------------------------------------------
# Discovery / validation
# --------------------------------------------------------------------------


def test_discover_reports_columns_and_incremental_candidates(tmp_path: Path) -> None:
    async def go():
        driver = await _connected(_seeded(tmp_path))
        try:
            return await driver.discover("main")
        finally:
            await driver.close()

    tables = asyncio.run(go())
    assert [t.name for t in tables] == ["t"]
    table = tables[0]
    assert table.estimated_rows == 1000
    assert [c.name for c in table.columns] == ["row_id", "score", "status", "ts"]
    assert "row_id" in table.candidate_incremental_keys
    assert "status" not in table.candidate_incremental_keys  # VARCHAR is not orderable


def test_validate_table_rejects_a_missing_table(tmp_path: Path) -> None:
    async def go():
        driver = await _connected(_seeded(tmp_path))
        try:
            return await driver.validate_table({"source": "main.nope"})
        finally:
            await driver.close()

    result = asyncio.run(go())
    assert not result.ok
    assert "does not exist" in result.errors[0]


# --------------------------------------------------------------------------
# Coercion (DataFrame lane only)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source_type", "expected"),
    [
        ("BIGINT", "int64"),
        ("INTEGER", "int64"),
        ("BOOLEAN", "int64"),
        ("DOUBLE", "float64"),
        ("DECIMAL(18,3)", "float64"),
        ("VARCHAR", "string"),
        ("VARCHAR(10)", "string"),
        ("UUID", "string"),
        ("DATE", "datetime64[ns]"),
        ("TIMESTAMP", "datetime64[ns]"),
        ("TIMESTAMP WITH TIME ZONE", "datetime64[ns]"),
        ("INTEGER[]", "string"),
        ("STRUCT(a INTEGER)", "string"),
        ("JSON", "string"),
    ],
)
def test_type_map(source_type: str, expected: str) -> None:
    assert ddb_coercion.pandas_dtype_for(source_type) == expected


@pytest.mark.parametrize("source_type", ["HUGEINT", "UBIGINT", "BLOB"])
def test_types_with_no_lossless_landing_are_refused(source_type: str) -> None:
    """Refuse rather than silently narrow — the PG-001 lesson."""
    with pytest.raises(ddb_coercion.UnsupportedDuckDBType):
        ddb_coercion.pandas_dtype_for(source_type)


def test_unmapped_type_is_refused_by_name() -> None:
    with pytest.raises(ddb_coercion.UnsupportedDuckDBType, match="unmapped"):
        ddb_coercion.pandas_dtype_for("NOT_A_TYPE")


def test_null_coerces_to_none_never_a_sentinel() -> None:
    assert ddb_coercion.coerce_value(None, "BIGINT") is None
    assert ddb_coercion.coerce_value(None, "VARCHAR") is None
    assert ddb_coercion.coerce_value(None, "TIMESTAMP") is None
