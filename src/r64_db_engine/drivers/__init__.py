"""Driver registry. Resolves config `dialect:` to a Driver class."""

from __future__ import annotations

from r64_db_engine.core.driver import Driver
from r64_db_engine.drivers.clickhouse.driver import ClickHouseDriver
from r64_db_engine.drivers.dynamodb.driver import DynamoDBDriver
from r64_db_engine.drivers.postgres.driver import PostgresDriver

DRIVERS: dict[str, type[Driver]] = {
    ClickHouseDriver.dialect_name(): ClickHouseDriver,
    PostgresDriver.dialect_name(): PostgresDriver,
    DynamoDBDriver.dialect_name(): DynamoDBDriver,
}


def resolve(dialect: str) -> type[Driver]:
    try:
        return DRIVERS[dialect]
    except KeyError as exc:
        available = ", ".join(sorted(DRIVERS)) or "(none)"
        raise ValueError(f"unknown dialect '{dialect}' (available: {available})") from exc


__all__ = ["DRIVERS", "resolve"]
