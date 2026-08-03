"""Abstract base for output sinks. SPEC §3.1 discipline, applied to the write side.

The engine is source-agnostic behind `Driver`; it is destination-agnostic behind
`Sink`. The two abstractions are deliberately shaped the same way, for the same
reason: `core/` must be able to grow a second sink without editing a line of
`core/`, exactly as it must be able to grow a second dialect.

# The naming law

This module names ZERO concrete sinks. There is no `Literal["ramdb", "arrow_ipc"]`
here and there must never be one. Sink-specific configuration arrives as an
opaque `dict[str, Any]` and is interpreted by the sink itself — the same
indirection `Driver.connect(config: dict[str, Any])` uses, and for the same
reason.

This matters more than it looks. PG-010 records that `core/` already bakes the
first *dialect* into core validation (`PostgresConfig`, `Literal["postgres"]`,
Postgres-specific health and metrics). That leak is deliberately deferred, not
endorsed. Introducing `Literal["ramdb", "arrow_ipc"]` here would clone the same
mistake onto a second axis and double the surface a future refactor has to
unwind. So the sink registry resolves a free-form `str` at runtime and core
stays ignorant of what sinks exist.

# What a sink owes its consumer

`write()` must be atomic with respect to any concurrent reader: a reader either
sees the entire previous output or the entire new output, never a partial file.
The concrete mechanism is the sink's business (the POSIX implementations use
tempfile-then-`rename(2)`), but the guarantee is part of this interface, not an
implementation detail a sink may opt out of.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import pandas as pd


class SinkError(RuntimeError):
    """A sink could not complete a write, or was configured incompatibly."""


class Sink(ABC):
    """Abstract base for output sinks.

    One Sink instance per running daemon, constructed at startup and reused
    across pulls. Implementations hold configuration, not per-write state.
    """

    @classmethod
    @abstractmethod
    def sink_name(cls) -> str:
        """Short identifier for this sink. Used in config (e.g. 'ramdb')."""

    @abstractmethod
    def open(self, config: dict[str, Any]) -> None:
        """Apply sink-specific configuration. Called once at daemon startup.

        `config` is opaque to core — see the naming law above. Raises
        `SinkError` on configuration that cannot produce valid output.
        """

    @abstractmethod
    def ensure_ready(self) -> None:
        """Verify the destination is writable. Called before the first write.

        Raises rather than creating anything the operator did not intend: a
        missing output root is a misconfiguration, not something to mkdir past.
        """

    @abstractmethod
    def target_path(self, target: str) -> Path:
        """Final path this sink would write `target` to. No side effects."""

    @abstractmethod
    def write(self, df: pd.DataFrame, target: str) -> Path:
        """Write `df` atomically as `target`. Returns the final path.

        Atomicity is a contract, not an implementation note — see the module
        docstring. Must never leave a partially written file at the path a
        consumer reads.
        """

    @abstractmethod
    def cleanup_orphan_tempfiles(self) -> int:
        """Remove leftover tempfiles from an interrupted write. Returns the count."""

    def supports_incremental(self) -> bool:
        """Whether this sink can accept a partial (watermarked) batch.

        Defaults to False, which is the safe answer for any format whose file
        layout is not appendable in place.

        A sink that returns False is not merely unoptimized for incremental
        mode — it is INCORRECT under it, because the daemon's incremental path
        merges by reading the sink's own previous output back in
        (`Daemon._merge_incremental`). PG-011 records that read-your-own-output
        pattern as an invariant violation on the ramdb side; it must not be
        re-imported here. The daemon fails fast on this rather than silently
        writing a partial snapshot that looks complete to the consumer, which
        would be strictly worse than refusing.
        """
        return False


__all__ = ["Sink", "SinkError"]
