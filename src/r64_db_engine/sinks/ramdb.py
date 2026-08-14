"""RamDB sink — the v0.1 output path, behind the `Sink` interface.

This is an ADAPTER, not a rewrite. `core/ramdb_writer.RamdbWriter` is audited,
hardened code (SIGTERM lifecycle, orphan tempfile cleanup, the PG-001 int64
overflow guard) and is not touched by the sink work. This class only exposes it
through the interface the daemon now depends on, so that the default output
behaviour is byte-for-byte what it was before sinks existed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from r64_db_engine.core.ramdb_writer import RamdbWriter
from r64_db_engine.core.sink import Sink, SinkError


class RamdbSink(Sink):
    """Sink wrapper over the v0.1 `RamdbWriter`."""

    def __init__(self) -> None:
        self._writer: RamdbWriter | None = None

    @classmethod
    def sink_name(cls) -> str:
        return "ramdb"

    def open(self, config: dict[str, Any]) -> None:
        loading_dir = config.get("loading_dir")
        if not loading_dir:
            raise SinkError("ramdb sink requires 'loading_dir'")
        self._writer = RamdbWriter(loading_dir, str(config.get("group", "") or ""))

    @property
    def writer(self) -> RamdbWriter:
        if self._writer is None:
            raise SinkError("ramdb sink used before open()")
        return self._writer

    def ensure_ready(self) -> None:
        self.writer.ensure_dirs()

    def target_path(self, target: str) -> Path:
        return self.writer.target_path(target)

    def write(self, df: pd.DataFrame, target: str) -> Path:
        return self.writer.write(df, target)

    def cleanup_orphan_tempfiles(self) -> int:
        return self.writer.cleanup_orphan_tempfiles()

    def supports_incremental(self) -> bool:
        """True — preserving v0.1 behaviour exactly.

        The ramdb incremental path reads its own previous output back in to
        merge (PG-011). That is a known, separately-tracked invariant violation.
        It is reported as-is here rather than silently changed: tightening it
        would alter shipped behaviour under cover of a sink refactor, which is
        not what this change is for.
        """
        return True


__all__ = ["RamdbSink"]
