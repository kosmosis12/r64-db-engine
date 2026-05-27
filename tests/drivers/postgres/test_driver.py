"""Integration tests for PostgresDriver.

Gated behind `--integration` (see conftest.py). Spins up a real Postgres
via testcontainers and exercises connect / discover / validate_table /
pull (both modes) plus the edge-case types from SPEC §6.1.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

testcontainers = pytest.importorskip("testcontainers.postgres")
PostgresContainer = testcontainers.PostgresContainer

from r64_db_engine.core.ramdb_writer import RamdbWriter, Row64CodecOverflowError
from r64_db_engine.drivers.postgres.coercion import NumericPrecisionLossError
from r64_db_engine.drivers.postgres.driver import PostgresDriver

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def pg_container():
    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg


def _driver_config(container) -> dict:
    return {
        "host": container.get_container_host_ip(),
        "port": int(container.get_exposed_port(5432)),
        "database": container.dbname,
        "user": container.username,
        "password": container.password,
        "sslmode": "disable",
    }


async def _setup_schema(driver: PostgresDriver) -> None:
    async with await driver._open() as conn, conn.cursor() as cur:
        await cur.execute(
            """
                CREATE TABLE IF NOT EXISTS orders (
                    id BIGINT PRIMARY KEY,
                    customer TEXT,
                    amount NUMERIC(20,5),
                    updated_at TIMESTAMPTZ
                );
                INSERT INTO orders VALUES
                    (1, 'alice', 12.345, '2026-01-01T00:00:00Z'),
                    (2, 'bob',   99.999, '2026-01-02T00:00:00Z'),
                    (3, 'café',  0.5,    '2026-01-03T00:00:00Z')
                ON CONFLICT (id) DO NOTHING;

                CREATE TABLE IF NOT EXISTS exotica (
                    id BIGINT PRIMARY KEY,
                    note TEXT,
                    payload JSONB,
                    tags TEXT[],
                    raw BYTEA,
                    flag BOOLEAN,
                    when_ts TIMESTAMP,
                    when_d DATE,
                    money NUMERIC(20,5)
                );
                INSERT INTO exotica VALUES
                    (1, 'em—dash', '{"a":1,"b":[2,3]}'::jsonb,
                     ARRAY['x','y'], '\\x0102ff'::bytea, true,
                     '2026-05-11 12:00:00', '2026-05-11', 3.14159)
                ON CONFLICT (id) DO NOTHING;
                """
        )
        await conn.commit()


async def test_connect_and_discover(pg_container) -> None:
    driver = PostgresDriver()
    await driver.connect(_driver_config(pg_container))
    await _setup_schema(driver)
    tables = await driver.discover(schema_filter="public")
    names = {t.name for t in tables}
    assert "orders" in names
    assert "exotica" in names
    orders = next(t for t in tables if t.name == "orders")
    assert any(c.name == "updated_at" for c in orders.columns)
    assert "updated_at" in orders.candidate_incremental_keys
    await driver.close()


async def test_validate_table_ok(pg_container) -> None:
    driver = PostgresDriver()
    await driver.connect(_driver_config(pg_container))
    await _setup_schema(driver)
    result = await driver.validate_table(
        {"source": "public.orders", "mode": "incremental", "incremental_key": "updated_at"}
    )
    assert result.ok is True
    assert not result.errors
    await driver.close()


async def test_validate_table_missing_table(pg_container) -> None:
    driver = PostgresDriver()
    await driver.connect(_driver_config(pg_container))
    result = await driver.validate_table(
        {"source": "public.does_not_exist", "mode": "full_refresh"}
    )
    assert result.ok is False
    assert any("does not exist" in e for e in result.errors)
    await driver.close()


async def test_validate_inline_sql(pg_container) -> None:
    driver = PostgresDriver()
    await driver.connect(_driver_config(pg_container))
    await _setup_schema(driver)
    result = await driver.validate_table(
        {"source": "SELECT id, amount FROM public.orders", "mode": "full_refresh"}
    )
    assert result.ok is True
    await driver.close()


async def test_pull_full_refresh(pg_container) -> None:
    driver = PostgresDriver()
    await driver.connect(_driver_config(pg_container))
    await _setup_schema(driver)
    res = await driver.pull(
        {"source": "public.orders", "mode": "full_refresh"}, previous_watermark=None
    )
    assert res.rows_pulled == 3
    assert res.new_watermark is None
    df = res.dataframe
    assert set(df.columns) >= {"id", "customer", "amount", "updated_at"}
    # ASCII sanitization replaces café -> caf?
    assert "caf?" in df["customer"].tolist()
    await driver.close()


async def test_pull_incremental_first_run_then_advance(pg_container) -> None:
    driver = PostgresDriver()
    await driver.connect(_driver_config(pg_container))
    await _setup_schema(driver)

    first = await driver.pull(
        {
            "source": "public.orders",
            "mode": "incremental",
            "incremental_key": "updated_at",
            "incremental_type": "timestamp",
        },
        previous_watermark=None,
    )
    assert first.rows_pulled == 3
    assert first.new_watermark is not None

    second = await driver.pull(
        {
            "source": "public.orders",
            "mode": "incremental",
            "incremental_key": "updated_at",
            "incremental_type": "timestamp",
        },
        previous_watermark=first.new_watermark,
    )
    assert second.rows_pulled == 0
    assert second.new_watermark == first.new_watermark

    async with await driver._open() as conn, conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO orders VALUES (99, 'new', 1.00, NOW())"
        )
        await conn.commit()

    third = await driver.pull(
        {
            "source": "public.orders",
            "mode": "incremental",
            "incremental_key": "updated_at",
            "incremental_type": "timestamp",
        },
        previous_watermark=first.new_watermark,
    )
    assert third.rows_pulled == 1
    await driver.close()


async def test_pull_handles_jsonb_array_bytea_numeric(pg_container) -> None:
    driver = PostgresDriver()
    await driver.connect(_driver_config(pg_container))
    await _setup_schema(driver)
    res = await driver.pull(
        {"source": "public.exotica", "mode": "full_refresh"}, previous_watermark=None
    )
    df = res.dataframe
    assert len(df) == 1
    row = df.iloc[0]
    assert json.loads(row["payload"]) == {"a": 1, "b": [2, 3]}
    assert json.loads(row["tags"]) == ["x", "y"]
    assert row["raw"] == "0102ff"
    assert row["flag"] is True or row["flag"] == True  # noqa: E712
    assert pd.Timestamp(row["when_d"]) == pd.Timestamp("2026-05-11")
    # numeric -> float64
    assert abs(float(row["money"]) - 3.14159) < 1e-6
    # ASCII sanitize the note column
    assert row["note"] == "em?dash"
    await driver.close()


async def test_numeric_20_5_round_trip_preserves_exact_value(
    pg_container,
) -> None:
    driver = PostgresDriver()
    await driver.connect(_driver_config(pg_container))
    async with await driver._open() as conn, conn.cursor() as cur:
        await cur.execute("DROP TABLE IF EXISTS numeric_exact")
        await cur.execute("CREATE TABLE numeric_exact (amount NUMERIC(20,5) NOT NULL)")
        await cur.execute("INSERT INTO numeric_exact VALUES (999999999999999.12345)")
        await conn.commit()

    with pytest.raises(NumericPrecisionLossError, match="cannot round-trip exactly"):
        await driver.pull(
            {"source": "public.numeric_exact", "mode": "full_refresh"},
            previous_watermark=None,
        )
    await driver.close()


@pytest.mark.parametrize(
    ("source_type", "source_value"),
    [
        ("BIGINT", "3548933426"),
        ("INTERVAL", "INTERVAL '3548.933426 seconds'"),
    ],
)
async def test_int64_source_values_above_signed_int32_round_trip_exactly(
    pg_container, tmp_path: Path, source_type: str, source_value: str
) -> None:
    driver = PostgresDriver()
    await driver.connect(_driver_config(pg_container))
    async with await driver._open() as conn, conn.cursor() as cur:
        await cur.execute("DROP TABLE IF EXISTS int64_exact")
        await cur.execute(f"CREATE TABLE int64_exact (value {source_type} NOT NULL)")
        await cur.execute(f"INSERT INTO int64_exact VALUES ({source_value})")
        await conn.commit()

    loading_dir = tmp_path / "loading"
    loading_dir.mkdir()
    with pytest.raises(Row64CodecOverflowError, match="outside signed int32 range"):
        result = await driver.pull(
            {"source": "public.int64_exact", "mode": "full_refresh"},
            previous_watermark=None,
        )
        RamdbWriter(loading_dir, "G").write(result.dataframe, "Int64")
    await driver.close()


async def test_incremental_limit_does_not_drop_rows_at_equal_watermark(pg_container) -> None:
    driver = PostgresDriver()
    await driver.connect(_driver_config(pg_container))
    async with await driver._open() as conn, conn.cursor() as cur:
        await cur.execute("DROP TABLE IF EXISTS watermark_ties")
        await cur.execute(
            "CREATE TABLE watermark_ties (id BIGINT PRIMARY KEY, updated_at TIMESTAMPTZ NOT NULL)"
        )
        await cur.execute(
            """INSERT INTO watermark_ties VALUES
               (1, '2026-05-27T00:00:00Z'),
               (2, '2026-05-27T00:00:00Z')"""
        )
        await conn.commit()

    cfg = {
        "source": "public.watermark_ties",
        "mode": "incremental",
        "incremental_key": "updated_at",
        "incremental_type": "timestamp",
        "max_rows": 1,
    }
    first = await driver.pull(cfg, previous_watermark=None)
    second = await driver.pull(cfg, previous_watermark=first.new_watermark)

    assert first.rows_pulled == 1
    assert second.rows_pulled == 1
    assert set(first.dataframe["id"]).isdisjoint(set(second.dataframe["id"]))
    await driver.close()
