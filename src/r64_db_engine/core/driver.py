"""Driver ABC and shared dataclasses. See SPEC §3.1."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from r64_db_engine.core.descriptor import Capabilities, DriverMetadata


@dataclass(frozen=True)
class ColumnMetadata:
    name: str
    source_type: str
    nullable: bool
    pandas_dtype: str


@dataclass(frozen=True)
class TableMetadata:
    schema: str
    name: str
    columns: list[ColumnMetadata]
    estimated_rows: int | None
    candidate_incremental_keys: list[str]


@dataclass(frozen=True)
class PullResult:
    dataframe: pd.DataFrame
    new_watermark: str | int | None
    rows_pulled: int
    duration_ms: int


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class Driver(ABC):
    """Abstract base for source-database drivers.

    One Driver instance per running daemon. Drivers are stateful — they
    hold a connection pool and reuse it across pulls. Drivers are
    expected to be async-safe.
    """

    #: Capabilities a driver gets without saying anything.
    #:
    #: Superset's Principle 4 — only three of its ~fifty engine specs override
    #: column types, because the base absorbs the common case. The same applies
    #: here: an all-native, password-authenticated, non-streaming driver should
    #: declare its name, its auth, its env keys, its config profile and its
    #: prose, and nothing else. Everything defaultable is pushed down here.
    #:
    #: These defaults are deliberately the conservative reading. `False` for a
    #: capability means "not claimed", which is the safe direction: a driver
    #: that forgets to declare streaming loses an optimization, whereas one that
    #: inherited a `True` it never implemented would fail at pull time on a
    #: promise nobody made.
    default_capabilities: Capabilities = Capabilities()

    @classmethod
    @abstractmethod
    def dialect_name(cls) -> str:
        """Short identifier for this driver (e.g., 'postgres')."""

    @classmethod
    @abstractmethod
    def descriptor(cls) -> DriverMetadata:
        """This driver's declarative identity. See `core.descriptor`.

        A **classmethod on purpose**, and the two words carry most of the design:

        *class*, not instance — resolving a descriptor must not construct a
        driver, because constructing one implies a config, and the roster wants
        to name every registered connector including the ones nobody has
        configured on this machine.

        *method*, resolved through the registry — so the sweep that reads every
        descriptor never imports a database client. That is the D-2/a lazy
        enumeration requirement, and it is why every driver module keeps its
        heavy third-party import inside the function that needs it rather than
        at module scope. Before this, asking `core.config` whether a dialect
        string was registered pulled psycopg and clickhouse_connect into the
        process just to validate a name.

        Two things follow that implementations must respect:

        * **Law 1.** The returned value is static data, computed from the source
          tree alone. No connection, no environment read, no network.
        * **Nothing here is a verdict.** A descriptor declares shape. Whether
          this driver is conformance-green is a separate, checksum-backed fact
          the oracle owns, joined in downstream. A descriptor that existed and
          therefore rendered green would be a proxy for the thing we measure.
        """

    @abstractmethod
    async def connect(self, config: dict[str, Any]) -> None:
        """Establish connection pool. Called once at daemon startup."""

    @abstractmethod
    async def close(self) -> None:
        """Cleanly close all connections. Called on daemon shutdown."""

    @abstractmethod
    async def discover(self, schema_filter: str | None = None) -> list[TableMetadata]:
        """List available tables with column metadata and incremental-key candidates."""

    @abstractmethod
    async def validate_table(self, table_config: dict[str, Any]) -> ValidationResult:
        """Pre-pull validation. No data fetched."""

    @abstractmethod
    async def pull(
        self,
        table_config: dict[str, Any],
        previous_watermark: str | int | None,
    ) -> PullResult:
        """Execute the pull. Returns coerced DataFrame and the new watermark."""

    @abstractmethod
    def coerce_value(self, value: Any, source_type: str) -> Any:
        """Dialect-specific single-value coercion. Used by tests."""
