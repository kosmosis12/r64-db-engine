"""ClickHouse driver.

Uses clickhouse-connect against system.tables/system.columns and returns frames
coerced to the ramdb-safe dtypes described in references/coercion-clickhouse.md.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

import pandas as pd

from r64_db_engine.core.coercion import apply_coercion
from r64_db_engine.core.descriptor import DriverMetadata
from r64_db_engine.core.driver import (
    ColumnMetadata,
    Driver,
    PullResult,
    TableMetadata,
    ValidationResult,
)
from r64_db_engine.drivers.clickhouse import coercion as ch_coercion

log = logging.getLogger(__name__)

_DEFAULT_PORT = 8123
_DEFAULT_CONNECT_TIMEOUT = 10


class ClickHouseDriver(Driver):
    def __init__(self) -> None:
        self._client: Any | None = None
        self._database: str | None = None
        self._host: str | None = None

    @classmethod
    def dialect_name(cls) -> str:
        return "clickhouse"

    @classmethod
    def descriptor(cls) -> DriverMetadata:
        # Imported from a sibling module that pulls in no client library, so a
        # roster sweep over every registered driver stays free of heavy deps.
        from r64_db_engine.drivers.clickhouse.descriptor import CLICKHOUSE

        return CLICKHOUSE

    async def connect(self, config: dict[str, Any]) -> None:
        host = config.get("host") or "localhost"
        port = int(config.get("port") or _DEFAULT_PORT)
        database = config.get("database")
        if not database:
            raise ValueError("clickhouse.database is required")

        username = config.get("user") or config.get("username")
        password = config.get("password") or os.environ.get("CLICKHOUSE_PASSWORD") or ""
        secure = bool(config.get("secure", False))
        connect_timeout = int(config.get("connect_timeout") or _DEFAULT_CONNECT_TIMEOUT)

        kwargs: dict[str, Any] = {
            "host": host,
            "port": port,
            "database": database,
            "secure": secure,
            "connect_timeout": connect_timeout,
        }
        if username:
            kwargs["username"] = username
        if password:
            kwargs["password"] = password

        # Imported here, not at module scope: sweeping every registered
        # driver's descriptor and validating a dialect name are both name-level
        # questions, and neither should pull a database client into the process
        # to answer them (the D-2/a lazy-enumeration requirement).
        import clickhouse_connect

        client = await asyncio.to_thread(clickhouse_connect.get_client, **kwargs)
        await asyncio.to_thread(client.command, "SELECT 1")
        self._client = client
        self._database = database
        self._host = host
        log.info("clickhouse_connected host=%s db=%s secure=%s", host, database, secure)

    async def close(self) -> None:
        if self._client is not None:
            await asyncio.to_thread(self._client.close)
        self._client = None

    async def discover(self, schema_filter: str | None = None) -> list[TableMetadata]:
        database = schema_filter or self._database
        params: dict[str, Any] = {}
        where = "WHERE database NOT IN ('system', 'INFORMATION_SCHEMA', 'information_schema')"
        if database is not None:
            where += " AND database = %(database)s"
            params["database"] = database

        rows = await self._query_rows(
            f"""
            SELECT database, name, total_rows
            FROM system.tables
            {where}
            ORDER BY database, name
            """,
            params,
        )

        tables: list[TableMetadata] = []
        for database_name, table_name, total_rows in rows:
            cols = await self._fetch_columns(database_name, table_name)
            candidate_keys = [
                col.name for col in cols if ch_coercion.is_orderable_type(col.source_type)
            ]
            tables.append(
                TableMetadata(
                    schema=database_name,
                    name=table_name,
                    columns=cols,
                    estimated_rows=int(total_rows) if total_rows is not None else None,
                    candidate_incremental_keys=candidate_keys,
                )
            )
        return tables

    async def validate_table(self, table_config: dict[str, Any]) -> ValidationResult:
        source = table_config.get("source")
        if not source:
            return ValidationResult(ok=False, errors=["source is required"])

        if _is_inline_sql(source):
            try:
                await self._query_rows(f"SELECT * FROM ({source}) AS sub LIMIT 0")
                return ValidationResult(ok=True)
            except Exception as exc:
                return ValidationResult(ok=False, errors=[f"inline SQL failed: {exc}"])

        database, table = self._split_qualified(source)
        errors: list[str] = []
        warnings: list[str] = []

        if not await self._table_exists(database, table):
            return ValidationResult(
                ok=False,
                errors=[f"table {database}.{table} does not exist"],
            )

        cols = await self._fetch_columns(database, table)
        col_map = {c.name: c for c in cols}

        configured_cols = table_config.get("columns") or []
        missing_cols = [c for c in configured_cols if c not in col_map]
        for col in missing_cols:
            errors.append(f"column '{col}' not in {database}.{table}")
        cols_to_validate = configured_cols or list(col_map)
        for col in cols_to_validate:
            meta = col_map.get(col)
            if meta and meta.pandas_dtype == "unsupported":
                errors.append(f"column '{col}' has unsupported ClickHouse type {meta.source_type}")

        if table_config.get("mode") == "incremental":
            incr_key = table_config.get("incremental_key")
            if not incr_key:
                errors.append("incremental mode requires incremental_key")
            elif incr_key not in col_map:
                errors.append(f"incremental_key '{incr_key}' not in {database}.{table}")
            else:
                key_type = col_map[incr_key].source_type
                if not ch_coercion.is_orderable_type(key_type):
                    errors.append(f"incremental_key '{incr_key}' has non-orderable type {key_type}")
                sorting_key = await self._sorting_key(database, table)
                if sorting_key and incr_key not in _extract_sorting_key_columns(sorting_key):
                    warnings.append(
                        f"incremental_key '{incr_key}' is not part of ORDER BY key {sorting_key}"
                    )

        return ValidationResult(ok=not errors, errors=errors, warnings=warnings)

    async def pull(
        self,
        table_config: dict[str, Any],
        previous_watermark: str | int | None,
    ) -> PullResult:
        source = table_config["source"]
        mode = table_config.get("mode", "full_refresh")
        incr_key = table_config.get("incremental_key")
        max_rows = table_config.get("max_rows")
        ascii_sanitize = table_config.get("ascii_sanitize", True)

        col_types = await self._column_types_for_source(source)
        selected_cols = table_config.get("columns") or list(col_types)
        column_dtypes: dict[str, str] = {}
        for col in selected_cols:
            if col not in col_types:
                continue
            try:
                column_dtypes[col] = ch_coercion.pandas_dtype_for(col_types[col])
            except ch_coercion.UnsupportedClickHouseType as exc:
                raise ch_coercion.UnsupportedClickHouseType(
                    f"column '{col}' has unsupported ClickHouse type {col_types[col]}"
                ) from exc

        sql, params = self._build_query(
            source=source,
            columns=selected_cols,
            mode=mode,
            incr_key=incr_key,
            previous_watermark=previous_watermark,
            max_rows=max_rows,
        )

        started = time.monotonic()
        df = await self._query_df(sql, params)
        df = _pre_coerce_values(df, {col: col_types[col] for col in df.columns if col in col_types})
        df = apply_coercion(df, column_dtypes=column_dtypes, ascii_sanitize=ascii_sanitize)
        new_watermark = _compute_new_watermark(df, mode, incr_key, previous_watermark)
        return PullResult(
            dataframe=df,
            new_watermark=new_watermark,
            rows_pulled=len(df),
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    def coerce_value(self, value: Any, source_type: str) -> Any:
        return ch_coercion.coerce_value(value, source_type)

    async def _query_rows(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
    ) -> list[tuple[Any, ...]]:
        client = self._require_client()
        result = await asyncio.to_thread(client.query, sql, parameters=params or {})
        return list(result.result_rows)

    async def _query_df(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
    ) -> pd.DataFrame:
        client = self._require_client()
        return await asyncio.to_thread(client.query_df, sql, parameters=params or {})

    def _require_client(self) -> Any:
        if self._client is None:
            raise RuntimeError("ClickHouseDriver.connect() not called")
        return self._client

    def _split_qualified(self, source: str) -> tuple[str, str]:
        if "." not in source:
            if not self._database:
                raise ValueError("unqualified ClickHouse table requires configured database")
            return self._database, source
        database, _, table = source.partition(".")
        return database, table

    async def _table_exists(self, database: str, table: str) -> bool:
        rows = await self._query_rows(
            """
            SELECT 1
            FROM system.tables
            WHERE database = %(database)s AND name = %(table)s
            LIMIT 1
            """,
            {"database": database, "table": table},
        )
        return bool(rows)

    async def _fetch_columns(self, database: str, table: str) -> list[ColumnMetadata]:
        rows = await self._query_rows(
            """
            SELECT name, type, is_in_primary_key
            FROM system.columns
            WHERE database = %(database)s AND table = %(table)s
            ORDER BY position
            """,
            {"database": database, "table": table},
        )
        return [
            ColumnMetadata(
                name=name,
                source_type=source_type,
                nullable=_is_nullable(source_type),
                pandas_dtype=_pandas_dtype_or_unsupported(source_type),
            )
            for name, source_type, _is_in_primary_key in rows
        ]

    async def _sorting_key(self, database: str, table: str) -> str | None:
        rows = await self._query_rows(
            """
            SELECT sorting_key
            FROM system.tables
            WHERE database = %(database)s AND name = %(table)s
            LIMIT 1
            """,
            {"database": database, "table": table},
        )
        return str(rows[0][0]) if rows and rows[0][0] else None

    async def _column_types_for_source(self, source: str) -> dict[str, str]:
        if not _is_inline_sql(source):
            database, table = self._split_qualified(source)
            cols = await self._fetch_columns(database, table)
            return {col.name: col.source_type for col in cols}

        rows = await self._query_rows(f"DESCRIBE TABLE ({source})")
        return {str(row[0]): str(row[1]) for row in rows}

    def _build_query(
        self,
        *,
        source: str,
        columns: list[str],
        mode: str,
        incr_key: str | None,
        previous_watermark: str | int | None,
        max_rows: int | None,
    ) -> tuple[str, dict[str, Any]]:
        selected = ", ".join(_quote_ident(col) for col in columns) if columns else "*"
        base = (
            f"({source}) AS sub"
            if _is_inline_sql(source)
            else _quote_qualified(source, self._database)
        )
        sql = f"SELECT {selected} FROM {base}"
        params: dict[str, Any] = {}

        if mode == "incremental" and previous_watermark is not None and incr_key:
            sql += f" WHERE {_quote_ident(incr_key)} > %(watermark)s"
            params["watermark"] = previous_watermark
            sql += f" ORDER BY {_quote_ident(incr_key)} ASC"

        if max_rows:
            sql += " LIMIT %(max_rows)s"
            params["max_rows"] = int(max_rows)

        return sql, params


def _pre_coerce_values(df: pd.DataFrame, col_types: dict[str, str]) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    for col, source_type in col_types.items():
        out[col] = out[col].map(lambda value, st=source_type: ch_coercion.coerce_value(value, st))
    return out


def _pandas_dtype_or_unsupported(source_type: str) -> str:
    try:
        return ch_coercion.pandas_dtype_for(source_type)
    except ch_coercion.UnsupportedClickHouseType:
        return "unsupported"


def _compute_new_watermark(
    df: pd.DataFrame,
    mode: str,
    incr_key: str | None,
    previous_watermark: str | int | None,
) -> str | int | None:
    if mode != "incremental" or not incr_key:
        return None
    if df.empty or incr_key not in df.columns:
        return previous_watermark
    max_val = df[incr_key].max()
    if pd.isna(max_val):
        return previous_watermark
    if isinstance(max_val, pd.Timestamp):
        return max_val.isoformat()
    if hasattr(max_val, "item"):
        max_val = max_val.item()
    return max_val if isinstance(max_val, int) else str(max_val)


def _is_inline_sql(source: str) -> bool:
    stripped = source.strip()
    return stripped.lower().startswith("select ") or "\n" in stripped


def _is_nullable(source_type: str) -> bool:
    return source_type.strip().lower().startswith("nullable(")


def _quote_qualified(source: str, default_database: str | None) -> str:
    if "." not in source:
        if default_database:
            return f"{_quote_ident(default_database)}.{_quote_ident(source)}"
        return _quote_ident(source)
    database, _, table = source.partition(".")
    return f"{_quote_ident(database)}.{_quote_ident(table)}"


def _quote_ident(identifier: str) -> str:
    return "`" + identifier.replace("`", "``") + "`"


def _extract_sorting_key_columns(sorting_key: str) -> set[str]:
    cleaned = sorting_key.strip()
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = cleaned[1:-1]
    return {part.strip().strip("`") for part in cleaned.split(",") if part.strip()}


__all__ = ["ClickHouseDriver"]
