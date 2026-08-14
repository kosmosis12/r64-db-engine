"""Driver ABC and shared dataclasses. See SPEC §3.1."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import pandas as pd


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
class ArrowPullResult:
    """Result of an Arrow-native pull.

    Deliberately NOT a `PullResult` with an extra field. `PullResult` carries a
    materialized `DataFrame` and a known `rows_pulled`; this carries a
    *reader that has not been drained yet*, so the row count does not exist
    until the sink consumes it. Conflating the two would mean inventing a row
    count before it is knowable, and the whole point of this lane is that
    nothing materializes the full result.

    `reader` is a `pyarrow.RecordBatchReader`. It is typed `Any` because
    `core/` must not import pyarrow — pyarrow is a sink-side dependency, and a
    driver that cannot produce Arrow never needs it installed.
    """

    reader: Any
    new_watermark: str | int | None
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

    @classmethod
    @abstractmethod
    def dialect_name(cls) -> str:
        """Short identifier for this driver (e.g., 'postgres')."""

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

    # -- Arrow-native lane: an optional CAPABILITY, not a second ABC --------
    #
    # A driver that can hand back Arrow directly overrides both methods below.
    # Everything else inherits the defaults and keeps working untouched — this
    # is why the lane is a capability rather than a sibling ABC: one registry,
    # one daemon, one config schema, and no driver is forced to care.
    #
    # The daemon routes on `supports_arrow()`; see `Daemon._handle_success`.

    def supports_arrow(self) -> bool:
        """Whether this driver implements `pull_arrow`.

        Default False. Overriding this without overriding `pull_arrow` is a
        programming error the daemon surfaces loudly rather than silently
        falling back, because a silent fallback would erase the very thing the
        Arrow lane is measured for.
        """
        return False

    async def pull_arrow(
        self,
        table_config: dict[str, Any],
        previous_watermark: str | int | None,
    ) -> ArrowPullResult:
        """Execute the pull, returning an undrained `RecordBatchReader`.

        No pandas, no coercion pass, and no full materialization: the point of
        this lane is that peak memory is bounded by the batch, not the result.
        Implementations must NOT drain the reader before returning it.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement the Arrow lane "
            f"(supports_arrow() returned True without pull_arrow())"
        )
