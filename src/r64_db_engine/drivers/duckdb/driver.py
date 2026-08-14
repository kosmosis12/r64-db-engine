"""DuckDB driver — the first driver to implement the Arrow-native lane.

Two lanes, deliberately both:

- `pull_arrow()` hands back DuckDB's own `RecordBatchReader`. No pandas, no
  coercion pass, no full materialization. This is the lane Phase C measures.
- `pull()` is the ordinary DataFrame path, kept so the SAME source can be run
  through the SAME coercion the pandas lane uses. That is the P' attribution
  cell: N vs P' isolates the bypass, N vs P is the workflow number.

# Read-only is the default, on purpose

This driver ingests; it does not own the database. A local `.duckdb` file is
opened `read_only=True` unless the operator explicitly says otherwise, because
DuckDB takes a WRITE lock on the file otherwise and a benchmark run would then
fight whatever else has it open. `:memory:` and MotherDuck (`md:`) cannot be
opened read-only in the same sense and are exempted.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import duckdb

from r64_db_engine.core.coercion import apply_coercion
from r64_db_engine.core.driver import (
    ArrowPullResult,
    ColumnMetadata,
    Driver,
    PullResult,
    TableMetadata,
    ValidationResult,
)
from r64_db_engine.drivers.duckdb import coercion as ddb_coercion

log = logging.getLogger(__name__)

# One source batch per artifact block. The sink re-chunks to 65536 regardless
# (see `sinks/arrow_ipc._BLOCK_ROWS`), so aligning the driver's batch size to it
# keeps the re-chunk buffer holding at most one block plus one batch — the
# tightest the streaming path can be. Larger batches raise the floor of the
# memory bound for no artifact benefit; much smaller ones pay per-batch overhead
# and make the buffer do merging work the source could have avoided.
_DEFAULT_BATCH_ROWS = 65_536

_MOTHERDUCK_PREFIX = "md:"
_IN_MEMORY = ":memory:"


class DuckDBDriver(Driver):
    def __init__(self) -> None:
        self._con: Any | None = None
        self._database: str | None = None
        self._batch_rows: int = _DEFAULT_BATCH_ROWS
        self._use_arrow: bool = True

    @classmethod
    def dialect_name(cls) -> str:
        return "duckdb"

    # ---- lifecycle ---------------------------------------------------

    async def connect(self, config: dict[str, Any]) -> None:
        database = config.get("database")
        if not database:
            raise ValueError("duckdb.database is required (path, ':memory:', or 'md:...')")
        database = str(database)

        # `or` would swallow an explicit 0 into the default, so the guard below
        # would never fire on the one value most likely to be a mistake.
        raw_batch = config.get("batch_size")
        batch_rows = _DEFAULT_BATCH_ROWS if raw_batch is None else int(raw_batch)
        if batch_rows < 1:
            raise ValueError("duckdb.batch_size must be positive")

        # Phase C's P' attribution cell needs the SAME driver against the SAME
        # source through the DataFrame lane, so the capability is switchable
        # from config rather than by monkeypatching the class. Default on.
        self._use_arrow = bool(config.get("arrow", True))

        managed = database == _IN_MEMORY or database.startswith(_MOTHERDUCK_PREFIX)
        read_only = bool(config.get("read_only", True)) and not managed

        kwargs: dict[str, Any] = {"database": database, "read_only": read_only}
        settings = config.get("settings")
        if settings is not None:
            if not isinstance(settings, dict):
                raise ValueError("duckdb.settings must be a mapping")
            kwargs["config"] = dict(settings)

        con = await asyncio.to_thread(duckdb.connect, **kwargs)
        await asyncio.to_thread(con.execute, "SELECT 1")

        self._con = con
        self._database = database
        self._batch_rows = batch_rows
        log.info(
            "duckdb_connected database=%s read_only=%s batch_rows=%d arrow=%s",
            _redact(database),
            read_only,
            batch_rows,
            self._use_arrow,
        )

    async def close(self) -> None:
        if self._con is not None:
            await asyncio.to_thread(self._con.close)
        self._con = None

    # ---- discovery / validation --------------------------------------

    async def discover(self, schema_filter: str | None = None) -> list[TableMetadata]:
        sql = (
            "SELECT table_schema, table_name FROM information_schema.tables "
            "WHERE table_type IN ('BASE TABLE', 'VIEW')"
        )
        params: list[Any] = []
        if schema_filter:
            sql += " AND table_schema = ?"
            params.append(schema_filter)
        sql += " ORDER BY table_schema, table_name"

        tables: list[TableMetadata] = []
        for schema, name in await self._fetch(sql, params):
            columns = await self._fetch_columns(schema, name)
            estimated = await self._estimated_rows(schema, name)
            tables.append(
                TableMetadata(
                    schema=schema,
                    name=name,
                    columns=columns,
                    estimated_rows=estimated,
                    candidate_incremental_keys=[
                        c.name
                        for c in columns
                        if ddb_coercion.is_orderable_type(c.source_type)
                    ],
                )
            )
        return tables

    async def validate_table(self, table_config: dict[str, Any]) -> ValidationResult:
        source = table_config.get("source")
        if not source:
            return ValidationResult(ok=False, errors=["source is required"])

        if _is_inline_sql(source):
            try:
                await self._fetch(f"SELECT * FROM ({source}) AS sub LIMIT 0", [])
                return ValidationResult(ok=True)
            except Exception as exc:
                return ValidationResult(ok=False, errors=[f"inline SQL failed: {exc}"])

        schema, table = self._split_qualified(source)
        columns = await self._fetch_columns(schema, table)
        if not columns:
            return ValidationResult(
                ok=False, errors=[f"table {schema}.{table} does not exist"]
            )

        col_map = {c.name: c for c in columns}
        errors: list[str] = []

        for col in table_config.get("columns") or []:
            if col not in col_map:
                errors.append(f"column '{col}' not in {schema}.{table}")

        for col in table_config.get("columns") or list(col_map):
            meta = col_map.get(col)
            if meta and meta.pandas_dtype == "unsupported":
                errors.append(
                    f"column '{col}' has unsupported DuckDB type {meta.source_type}"
                )

        if table_config.get("mode") == "incremental":
            incr_key = table_config.get("incremental_key")
            if not incr_key:
                errors.append("incremental mode requires incremental_key")
            elif incr_key not in col_map:
                errors.append(f"incremental_key '{incr_key}' not in {schema}.{table}")
            elif not ddb_coercion.is_orderable_type(col_map[incr_key].source_type):
                errors.append(
                    f"incremental_key '{incr_key}' has non-orderable type "
                    f"{col_map[incr_key].source_type}"
                )

        return ValidationResult(ok=not errors, errors=errors)

    # ---- the Arrow-native lane ---------------------------------------

    def supports_arrow(self) -> bool:
        return self._use_arrow

    async def pull_arrow(
        self,
        table_config: dict[str, Any],
        previous_watermark: str | int | None,
    ) -> ArrowPullResult:
        """Hand back an undrained reader. Nothing is materialized here.

        `to_arrow_reader()` is the non-deprecated API; `fetch_record_batch()`
        emits a DeprecationWarning as of duckdb 1.5.
        """
        if table_config.get("mode") == "incremental":
            raise ValueError(
                "duckdb: incremental mode is not supported on the Arrow lane "
                "(full-refresh only)"
            )

        sql = self._build_query(
            source=table_config["source"],
            columns=table_config.get("columns"),
            max_rows=table_config.get("max_rows"),
        )

        started = time.monotonic()
        con = self._require_con()
        result = await asyncio.to_thread(con.execute, sql)
        reader = await asyncio.to_thread(result.to_arrow_reader, self._batch_rows)
        return ArrowPullResult(
            reader=reader,
            new_watermark=None,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    # ---- the DataFrame lane (P' attribution cell) ---------------------

    async def pull(
        self,
        table_config: dict[str, Any],
        previous_watermark: str | int | None,
    ) -> PullResult:
        source = table_config["source"]
        ascii_sanitize = table_config.get("ascii_sanitize", True)

        col_types = await self._column_types_for_source(source)
        selected = table_config.get("columns") or list(col_types)
        column_dtypes: dict[str, str] = {}
        for col in selected:
            if col not in col_types:
                continue
            try:
                column_dtypes[col] = ddb_coercion.pandas_dtype_for(col_types[col])
            except ddb_coercion.UnsupportedDuckDBType as exc:
                raise ddb_coercion.UnsupportedDuckDBType(
                    f"column '{col}' has unsupported DuckDB type {col_types[col]}"
                ) from exc

        sql = self._build_query(
            source=source,
            columns=table_config.get("columns"),
            max_rows=table_config.get("max_rows"),
        )

        started = time.monotonic()
        con = self._require_con()
        result = await asyncio.to_thread(con.execute, sql)
        df = await asyncio.to_thread(result.df)
        df = apply_coercion(df, column_dtypes=column_dtypes, ascii_sanitize=ascii_sanitize)
        return PullResult(
            dataframe=df,
            new_watermark=None,
            rows_pulled=len(df),
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    def coerce_value(self, value: Any, source_type: str) -> Any:
        return ddb_coercion.coerce_value(value, source_type)

    # ---- internals ----------------------------------------------------

    def _require_con(self) -> Any:
        if self._con is None:
            raise RuntimeError("duckdb driver used before connect()")
        return self._con

    async def _fetch(self, sql: str, params: list[Any] | None = None) -> list[tuple]:
        con = self._require_con()
        result = await asyncio.to_thread(con.execute, sql, params or [])
        return list(result.fetchall())

    async def _fetch_columns(self, schema: str, table: str) -> list[ColumnMetadata]:
        rows = await self._fetch(
            "SELECT column_name, data_type, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_schema = ? AND table_name = ? "
            "ORDER BY ordinal_position",
            [schema, table],
        )
        columns: list[ColumnMetadata] = []
        for name, data_type, nullable in rows:
            columns.append(
                ColumnMetadata(
                    name=name,
                    source_type=data_type,
                    nullable=str(nullable).upper() == "YES",
                    pandas_dtype=_dtype_or_unsupported(data_type),
                )
            )
        return columns

    async def _estimated_rows(self, schema: str, table: str) -> int | None:
        try:
            rows = await self._fetch(
                f"SELECT count(*) FROM {_quote_ident(schema)}.{_quote_ident(table)}"
            )
        except Exception:
            return None
        return int(rows[0][0]) if rows else None

    async def _column_types_for_source(self, source: str) -> dict[str, str]:
        if _is_inline_sql(source):
            # Executed for its cursor description, not its rows.
            await self._fetch(f"SELECT * FROM ({source}) AS sub LIMIT 0")
            con = self._require_con()
            desc = con.description or []
            return {d[0]: str(d[1]) for d in desc}
        schema, table = self._split_qualified(source)
        return {c.name: c.source_type for c in await self._fetch_columns(schema, table)}

    def _split_qualified(self, source: str) -> tuple[str, str]:
        if "." in source:
            schema, table = source.split(".", 1)
            return schema, table
        return "main", source

    def _build_query(
        self,
        *,
        source: str,
        columns: list[str] | None,
        max_rows: int | None,
    ) -> str:
        """Build the SELECT.

        # Determinism is the caller's to state, and inline SQL is how

        DuckDB parallelizes scans and does NOT guarantee row order, so an
        artifact pulled twice can be byte-different while being row-identical.
        The ClickHouse campaign set the precedent: bench and e2e pulls carry an
        explicit `ORDER BY row_id`.

        There is no per-table `order_by` knob because `core.config.TableConfig`
        is `extra="forbid"` and driver-specific table options are therefore not
        expressible in YAML (filed — same leak class as PG-010, on the table
        axis). Inline SQL covers it without touching core:

            source: "SELECT * FROM main.perf_1m ORDER BY row_id"

        which is why an inline source with no projection and no limit is passed
        through VERBATIM rather than wrapped. Wrapping it as
        `SELECT * FROM (<source>) AS sub` would put the ORDER BY inside a
        subquery, where SQL does not oblige the engine to preserve it — the
        ordering would hold by luck and stop holding without warning.
        """
        projection = (
            ", ".join(_quote_ident(c) for c in columns) if columns else "*"
        )
        if _is_inline_sql(source):
            if projection == "*" and not max_rows:
                return source
            sql = f"SELECT {projection} FROM ({source}) AS sub"
        else:
            schema, table = self._split_qualified(source)
            sql = (
                f"SELECT {projection} FROM "
                f"{_quote_ident(schema)}.{_quote_ident(table)}"
            )
        if max_rows:
            sql += f" LIMIT {int(max_rows)}"
        return sql


def _dtype_or_unsupported(source_type: str) -> str:
    try:
        return ddb_coercion.pandas_dtype_for(source_type)
    except ddb_coercion.UnsupportedDuckDBType:
        return "unsupported"


def _is_inline_sql(source: str) -> bool:
    return source.strip().lower().startswith("select ")


def _quote_ident(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _redact(database: str) -> str:
    """MotherDuck DSNs can carry a token. Never log one."""
    if database.startswith(_MOTHERDUCK_PREFIX) and "?" in database:
        return database.split("?", 1)[0] + "?<redacted>"
    return database


__all__ = ["DuckDBDriver"]
