"""The `rest` driver — a Driver ABC implementation whose "database" is a book.

PG-010, repeated on a non-database source class: this driver is admitted by
ONE entry in `drivers/__init__.py` and nothing else. Core does not know the
word `rest` exists, which is the property `git grep -rniE "\\brest\\b"
src/r64_db_engine/core/` returning nothing is there to prove.

Config shape — the `rest:` block:

    dialect: rest
    rest:
      recipe_book: factory/recipes/open-meteo.yaml

The block names a book by PATH. The book is the compiled artifact of a research
phase and is reviewed on its own, versioned on its own, and reused across
deployments; a deployment config that inlined it would make every environment a
separate copy of the thing that must not diverge.

A table entry's `source` names the book's `dataset`, so a config that points at
the wrong book is refused rather than quietly pulling something else.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from r64_db_engine.core.descriptor import DriverMetadata
from r64_db_engine.core.driver import (
    ColumnMetadata,
    Driver,
    PullResult,
    TableMetadata,
    ValidationResult,
)
from r64_db_engine.drivers.rest.engine import records_to_frame, run_book
from r64_db_engine.drivers.rest.recipes import RecipeBook, RecipeBookError, load_book

log = logging.getLogger(__name__)

# Declared output type -> the pandas dtype the sink will see. Stated here so
# `discover()` can answer without executing a pull.
_PANDAS_DTYPE = {
    "int64": "Int64",
    "double": "float64",
    "bool": "boolean",
    "string": "string",
    "timestamp[us]": "datetime64[us]",
}


class RestDriver(Driver):
    """Executes a compiled recipe book. Stateless between pulls by design.

    There is no connection pool to hold: each pull opens a client, runs the
    book, and closes it. A long-lived client would keep a socket to a
    third-party API alive across a cadence measured in hours for no benefit,
    and would hold DNS results past the point where re-resolving is the safer
    behaviour.
    """

    def __init__(self) -> None:
        self._book: RecipeBook | None = None
        self._config: dict[str, Any] = {}

    @classmethod
    def dialect_name(cls) -> str:
        return "rest"

    @classmethod
    def descriptor(cls) -> DriverMetadata:
        # Imported from a sibling module that pulls in no client library, so a
        # roster sweep over every registered driver stays free of heavy deps.
        from r64_db_engine.drivers.rest.descriptor import REST

        return REST

    async def connect(self, config: dict[str, Any]) -> None:
        """Load and validate the book. No network call is made here.

        The book is parsed at connect time so a malformed or insecure book
        fails at daemon startup rather than at the first cadence tick — the
        refuse-early half of refuse-loudly.
        """
        self._config = dict(config)
        unknown = set(config) - {"recipe_book"}
        if unknown:
            raise RecipeBookError(
                f"unknown key(s) in the `rest:` config block: {sorted(unknown)}. "
                f"Permitted: ['recipe_book']. The book itself carries recipes, "
                f"threading, output and limits."
            )
        recipe_book = config.get("recipe_book")
        if not recipe_book:
            raise RecipeBookError(
                "rest dialect requires `recipe_book`: a path to a compiled recipe book "
                "(e.g. factory/recipes/open-meteo.yaml)"
            )
        self._book = load_book(recipe_book)
        log.info(
            "rest_connect dataset=%s recipes=%d steps=%d book=%s",
            self._book.dataset,
            len(self._book.recipes),
            len(self._book.threading),
            Path(recipe_book).name,
        )

    async def close(self) -> None:
        self._book = None

    @property
    def book(self) -> RecipeBook:
        if self._book is None:
            raise RecipeBookError("rest driver used before connect()")
        return self._book

    async def discover(self, schema_filter: str | None = None) -> list[TableMetadata]:
        """One 'table' per book: its declared output schema.

        Answered from the book rather than by pulling. The output schema is a
        property the book DECLARES, so discovery does not need to observe the
        provider — and must not, since a discovery call that hit a rate-limited
        third-party API would be a poor way to fill in a dropdown.
        """
        book = self.book
        columns = [
            ColumnMetadata(
                name=column.name,
                source_type=column.type,
                # Declared columns are nullable: a record may omit a field, and
                # the artifact must carry that as a true null rather than a fill.
                nullable=True,
                pandas_dtype=_PANDAS_DTYPE[column.type],
            )
            for column in book.output
        ]
        return [
            TableMetadata(
                schema="rest",
                name=book.dataset,
                columns=columns,
                estimated_rows=None,
                # Nothing here is a safe incremental key. A recipe book has no
                # ordering guarantee across pulls, and claiming one would invite
                # exactly the watermark-on-unordered-source defect PG-011 covers.
                candidate_incremental_keys=[],
            )
        ]

    async def validate_table(self, table_config: dict[str, Any]) -> ValidationResult:
        """Pre-pull validation. No data fetched, no network touched."""
        errors: list[str] = []
        warnings: list[str] = []
        book = self.book

        source = table_config.get("source")
        if not source:
            errors.append("source is required")
        elif source != book.dataset:
            errors.append(
                f"source {source!r} does not name this recipe book's dataset "
                f"({book.dataset!r}). A config pointing at the wrong book is refused "
                f"rather than silently pulling something else."
            )

        if table_config.get("mode") == "incremental":
            errors.append(
                "the rest dialect does not support incremental mode: a recipe book has no "
                "ordering guarantee across pulls, so a watermark would silently skip "
                "records. Use mode: full_refresh."
            )
        return ValidationResult(ok=not errors, errors=errors, warnings=warnings)

    async def pull(
        self,
        table_config: dict[str, Any],
        previous_watermark: str | int | None,
    ) -> PullResult:
        import asyncio

        book = self.book
        started = time.monotonic()

        # httpx is synchronous here and the book may make several sequential
        # calls, so it runs off the event loop — matching the thread-offload
        # discipline the other drivers use for their blocking clients.
        records = await asyncio.to_thread(run_book, book)
        frame = records_to_frame(records, book)

        max_rows = table_config.get("max_rows")
        if max_rows:
            frame = frame.head(int(max_rows))

        duration_ms = int((time.monotonic() - started) * 1000)
        log.info(
            "rest_pull dataset=%s rows=%d duration_ms=%d", book.dataset, len(frame), duration_ms
        )
        return PullResult(
            dataframe=frame,
            # Always None: this driver never claims a watermark, because a book
            # that cannot promise an order cannot promise a resume point.
            new_watermark=None,
            rows_pulled=len(frame),
            duration_ms=duration_ms,
        )

    def coerce_value(self, value: Any, source_type: str) -> Any:
        """Single-value coercion against a DECLARED output type.

        The recipe lane has no source type system to map from — JSON gives
        objects, and the book states what each field must become. So this is a
        narrow, total function over the closed output vocabulary rather than a
        type table.
        """
        if value is None:
            return None
        if source_type == "int64":
            return int(value)
        if source_type == "double":
            return float(value)
        if source_type == "bool":
            return bool(value)
        if source_type == "timestamp[us]":
            import pandas as pd

            return pd.Timestamp(value, tz="UTC").tz_localize(None)
        if source_type == "string":
            return str(value)
        raise ValueError(
            f"unknown declared output type {source_type!r} (known: {sorted(_PANDAS_DTYPE)})"
        )


__all__ = ["RestDriver"]
