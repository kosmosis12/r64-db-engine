"""Driver registry. Resolves config `dialect:` to a Driver class."""

from __future__ import annotations

from r64_db_engine.core.driver import Driver
from r64_db_engine.drivers.postgres.driver import PostgresDriver

DRIVERS: dict[str, type[Driver]] = {
    PostgresDriver.dialect_name(): PostgresDriver,
}


def resolve(dialect: str) -> type[Driver]:
    try:
        return DRIVERS[dialect]
    except KeyError as exc:
        available = ", ".join(sorted(DRIVERS)) or "(none)"
        raise ValueError(f"unknown dialect '{dialect}' (available: {available})") from exc


__all__ = ["DRIVERS", "resolve"]
