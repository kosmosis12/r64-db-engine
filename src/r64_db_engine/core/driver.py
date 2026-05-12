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
