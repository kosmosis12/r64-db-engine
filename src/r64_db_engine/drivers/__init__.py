"""Driver registry. Resolves config `dialect:` to a Driver class.

This module is the ONE place a dialect name appears outside its own driver
package. `core/` names zero dialects (PG-010): a config's `dialect:` is a
free-form string checked against this registry rather than a `Literal[...]`,
which is why a complete driver can be named in a config file the day it lands
instead of waiting on a core edit.

**The registry is lazy, and that is load-bearing.** It used to import all three
driver classes at module scope, which meant that asking a name-level question —
"is `postgres` a registered dialect?", "what does every driver declare?" —
dragged psycopg and clickhouse_connect into the process to answer it. Two paths
ask exactly those questions and neither needs a database client:

  * `core.config` validates every config against the set of registered dialect
    names. Loading a YAML file should not import a Postgres driver.
  * the roster/doc generator sweeps `descriptor()` across every registered
    driver. Emitting a JSON projection should not import anything at all.

That coupling is the D-2/a defect. It is fixed structurally rather than by
care: `_MANIFEST` below is pure data, `DRIVERS` answers membership, iteration
and length from it without importing, and a module is imported only when
someone actually asks for a class — `resolve()`, or a `DRIVERS[key]` lookup.
Each driver package also keeps its descriptor in a `descriptor.py` that imports
no client library, so a full descriptor sweep touches no heavy dependency at
all. `tests/drivers/test_lazy_registry.py` asserts that in anger by checking
`sys.modules` after a sweep, because a lazy-import claim that nothing verifies
is the kind that quietly regresses on the next driver.
"""

from __future__ import annotations

from collections.abc import Iterator, MutableMapping
from dataclasses import dataclass
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from r64_db_engine.core.descriptor import DriverMetadata
    from r64_db_engine.core.driver import Driver


@dataclass(frozen=True)
class _Entry:
    """Where a driver lives. Pure data — importing this module imports nothing."""

    driver_module: str
    driver_class: str
    descriptor_module: str
    descriptor_attr: str


#: dialect -> where to find it. Adding a driver is one entry here and nothing
#: else anywhere; that single-line admission is PG-010's claim, and `rest`
#: proves it extends past the class of source it was first made about — `rest`
#: is not a database at all, but a compiled recipe book executed by a
#: hand-written engine, and it registers exactly like the others.
_MANIFEST: dict[str, _Entry] = {
    "clickhouse": _Entry(
        "r64_db_engine.drivers.clickhouse.driver",
        "ClickHouseDriver",
        "r64_db_engine.drivers.clickhouse.descriptor",
        "CLICKHOUSE",
    ),
    "postgres": _Entry(
        "r64_db_engine.drivers.postgres.driver",
        "PostgresDriver",
        "r64_db_engine.drivers.postgres.descriptor",
        "POSTGRES",
    ),
    "rest": _Entry(
        "r64_db_engine.drivers.rest.driver",
        "RestDriver",
        "r64_db_engine.drivers.rest.descriptor",
        "REST",
    ),
}


#: Driver classes registered directly, by object rather than by manifest entry.
#:
#: Two callers want this. Tests register a fake driver to prove that core
#: special-cases no dialect — the PG-010 regression test literally cannot be
#: written without a way to add a dialect at runtime. And an out-of-tree driver
#: has a class but no entry in the manifest above.
#:
#: The overlay shadows the manifest, so a test can also replace a real driver
#: for the duration of a case without touching the file on disk.
_OVERLAY: dict[str, type[Driver]] = {}


class _DriverRegistry(MutableMapping[str, "type[Driver]"]):
    """A `MutableMapping[str, type[Driver]]` that imports only on value access.

    Presenting as a Mapping is deliberate: every existing call site — the
    `frozenset(DRIVERS)` in `core.config`, the `sorted(DRIVERS)` in error
    messages, the direct subscript in the conformance battery — keeps working
    unchanged. What changes is the cost of the cheap operations. `in`, `iter`
    and `len` are answered from the manifest and the overlay; only an actual
    `DRIVERS[key]` lookup imports anything.

    Mutable because it always was: registering a driver object directly is the
    supported way to admit an out-of-tree driver, and it is the only way to
    write the test that proves core hardcodes no dialect. Laziness is about
    *when* a module is imported, not about freezing the set of drivers.
    """

    __slots__ = ()

    def __getitem__(self, dialect: str) -> type[Driver]:
        if dialect in _OVERLAY:
            return _OVERLAY[dialect]
        try:
            entry = _MANIFEST[dialect]
        except KeyError:
            raise KeyError(dialect) from None
        module = import_module(entry.driver_module)
        return getattr(module, entry.driver_class)  # type: ignore[no-any-return]

    def __setitem__(self, dialect: str, driver_cls: type[Driver]) -> None:
        _OVERLAY[dialect] = driver_cls

    def __delitem__(self, dialect: str) -> None:
        if dialect in _OVERLAY:
            del _OVERLAY[dialect]
        elif dialect in _MANIFEST:
            # Shadow it with a tombstone rather than mutating the manifest: the
            # manifest is the source of truth about what ships, and a caller
            # removing a driver for the duration of a test should not be able to
            # edit that.
            raise KeyError(
                f"'{dialect}' is a shipped driver and cannot be removed from the registry; "
                f"register over it instead"
            )
        else:
            raise KeyError(dialect)

    def __iter__(self) -> Iterator[str]:
        # Sorted, and deduplicated across the two layers. The generator consumes
        # this and commits its output, so iteration order must be a property of
        # the names rather than of insertion.
        return iter(sorted(set(_MANIFEST) | set(_OVERLAY)))

    def __len__(self) -> int:
        return len(set(_MANIFEST) | set(_OVERLAY))

    def __contains__(self, key: object) -> bool:
        return key in _OVERLAY or key in _MANIFEST

    def __repr__(self) -> str:
        # Names only. Rendering this must not import anything, which a default
        # Mapping repr — it materializes every value — would do.
        return f"<DriverRegistry {sorted(self)}>"


#: The registry. Not a plain dict: membership and iteration stay import-free.
DRIVERS: MutableMapping[str, type[Driver]] = _DriverRegistry()


def resolve(dialect: str) -> type[Driver]:
    """Return the driver class for `dialect`, importing it now.

    This is the boundary where a heavy dependency is finally paid for. Anything
    that only needs to know *which* dialects exist, or what one *declares*,
    should use `DRIVERS` or `descriptors()` and stay free of the import.
    """
    try:
        return DRIVERS[dialect]
    except KeyError as exc:
        available = ", ".join(sorted(DRIVERS)) or "(none)"
        raise ValueError(f"unknown dialect '{dialect}' (available: {available})") from exc


def descriptor(dialect: str) -> DriverMetadata:
    """Return one driver's declarative identity WITHOUT importing the driver.

    The import goes to the driver package's `descriptor.py`, which by contract
    pulls in no client library. That is the difference between this and
    `resolve(dialect).descriptor()`: both return the same object, but only this
    one is free.
    """
    if dialect in _OVERLAY:
        # A directly-registered class is already imported; ask it. There is no
        # cheaper path to take and nothing to defer.
        return _OVERLAY[dialect].descriptor()
    try:
        entry = _MANIFEST[dialect]
    except KeyError as exc:
        available = ", ".join(sorted(DRIVERS)) or "(none)"
        raise ValueError(f"unknown dialect '{dialect}' (available: {available})") from exc
    module = import_module(entry.descriptor_module)
    return getattr(module, entry.descriptor_attr)  # type: ignore[no-any-return]


def descriptors() -> dict[str, DriverMetadata]:
    """Every registered driver's descriptor, in sorted dialect order.

    The sort is not cosmetic. This is the generator's input, and the generator's
    output is committed to git and diffed by reviewers, so the iteration order
    has to be a property of the data rather than of dict construction order.
    An unsorted registry here is how a "regenerated, no changes" claim turns
    into a spurious diff — Gate MF-DESC asserts byte-identical output across
    two runs precisely to keep this honest.
    """
    return {dialect: descriptor(dialect) for dialect in sorted(DRIVERS)}


__all__ = ["DRIVERS", "descriptor", "descriptors", "resolve"]
