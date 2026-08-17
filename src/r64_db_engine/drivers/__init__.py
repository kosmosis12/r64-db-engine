"""Driver registry. Resolves config `dialect:` to a Driver class."""

from __future__ import annotations

from r64_db_engine.core.driver import Driver
from r64_db_engine.drivers.clickhouse.driver import ClickHouseDriver
from r64_db_engine.drivers.postgres.driver import PostgresDriver
from r64_db_engine.drivers.rest.driver import RestDriver

DRIVERS: dict[str, type[Driver]] = {
    ClickHouseDriver.dialect_name(): ClickHouseDriver,
    PostgresDriver.dialect_name(): PostgresDriver,
    # `rest` is not a database. It is the recipe lane: a compiled recipe book
    # executed by a hand-written engine. That it registers here, in one line,
    # with no other change anywhere, is PG-010's claim extended past the class
    # of source it was originally made about.
    RestDriver.dialect_name(): RestDriver,
}


def resolve(dialect: str) -> type[Driver]:
    try:
        return DRIVERS[dialect]
    except KeyError as exc:
        available = ", ".join(sorted(DRIVERS)) or "(none)"
        raise ValueError(f"unknown dialect '{dialect}' (available: {available})") from exc


__all__ = ["DRIVERS", "resolve"]
