"""Postgres driver. SPEC §3.1, §6.1.

Connects with psycopg 3 async, discovers tables via information_schema,
applies dialect coercion plus the universal framework rules from
core.coercion.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import pandas as pd
import psycopg
from psycopg.rows import dict_row

from r64_db_engine.core.coercion import apply_coercion
from r64_db_engine.core.driver import (
    ColumnMetadata,
    Driver,
    PullResult,
    TableMetadata,
    ValidationResult,
)
from r64_db_engine.drivers.postgres import coercion as pg_coercion

log = logging.getLogger(__name__)

_DEFAULT_PORT = 5432
_DEFAULT_CONNECT_TIMEOUT = 10
_DEFAULT_STATEMENT_TIMEOUT_S = 300
_DEFAULT_APP_NAME = "r64-db-engine"

# Object types that come back from psycopg as Python objects (not str/int/...)
# and need per-value coercion before the framework's string-column rules run.
_OBJECT_RETURN_TYPES = frozenset(
    {
        "uuid",
        "json",
        "jsonb",
        "bytea",
        "inet",
        "cidr",
        "macaddr",
        "macaddr8",
        "interval",
        "time",
        "timetz",
        "time without time zone",
        "time with time zone",
    }
)


class PostgresDriver(Driver):
    def __init__(self) -> None:
        self._conninfo: str | None = None
        self._app_name: str = _DEFAULT_APP_NAME
        self._statement_timeout_ms: int = _DEFAULT_STATEMENT_TIMEOUT_S * 1000
        self._database: str | None = None
        self._host: str | None = None

    # ---- ABC required ------------------------------------------------

    @classmethod
    def dialect_name(cls) -> str:
        return "postgres"

    async def connect(self, config: dict[str, Any]) -> None:
        host = config.get("host") or "localhost"
        port = int(config.get("port") or _DEFAULT_PORT)
        database = config.get("database")
        if not database:
            raise ValueError("postgres.database is required")
        user = config.get("user")
        password = config.get("password")
        sslmode = config.get("sslmode", "prefer")
        connect_timeout = int(config.get("connect_timeout") or _DEFAULT_CONNECT_TIMEOUT)
        app_name = config.get("application_name") or _DEFAULT_APP_NAME
        statement_timeout = int(
            config.get("statement_timeout") or _DEFAULT_STATEMENT_TIMEOUT_S
        )

        parts = [
            f"host={host}",
            f"port={port}",
            f"dbname={database}",
            f"sslmode={sslmode}",
            f"connect_timeout={connect_timeout}",
            f"application_name={app_name}",
        ]
        if user:
            parts.append(f"user={user}")
        if password:
            parts.append(f"password={password}")
        self._conninfo = " ".join(parts)
        self._app_name = app_name
        self._statement_timeout_ms = statement_timeout * 1000
        self._database = database
        self._host = host

        # Validate by opening + closing a connection now (fail-fast).
        async with await self._open() as conn, conn.cursor() as cur:
            await cur.execute("SELECT 1")
            await cur.fetchone()
        log.info("postgres_connected host=%s db=%s", host, database)

    async def close(self) -> None:
        # psycopg async connections in this driver are per-pull; nothing
        # pooled long-term in v0.1.
        self._conninfo = None

    async def discover(self, schema_filter: str | None = None) -> list[TableMetadata]:
        sql = """
            SELECT table_schema, table_name
            FROM information_schema.tables
            WHERE table_type = 'BASE TABLE'
              AND table_schema NOT IN ('pg_catalog', 'information_schema')
              AND (%s::text IS NULL OR table_schema = %s)
            ORDER BY table_schema, table_name
        """
        async with await self._open() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, (schema_filter, schema_filter))
                rows = await cur.fetchall()

            tables: list[TableMetadata] = []
            for schema, name in rows:
                cols = await _fetch_columns(conn, schema, name)
                rowcount = await _estimate_rowcount(conn, schema, name)
                incr_keys = [
                    c.name
                    for c in cols
                    if c.source_type
                    in {
                        "timestamp",
                        "timestamp without time zone",
                        "timestamptz",
                        "timestamp with time zone",
                        "bigint",
                        "integer",
                        "smallint",
                    }
                ]
                tables.append(
                    TableMetadata(
                        schema=schema,
                        name=name,
                        columns=cols,
                        estimated_rows=rowcount,
                        candidate_incremental_keys=incr_keys,
                    )
                )
            return tables

    async def validate_table(self, table_config: dict[str, Any]) -> ValidationResult:
        source = table_config.get("source")
        if not source:
            return ValidationResult(ok=False, errors=["source is required"])

        # Inline SQL: smoke-test with a LIMIT 0 wrapper.
        if _is_inline_sql(source):
            sql = f"SELECT * FROM ({source}) sub LIMIT 0"
            try:
                async with await self._open() as conn, conn.cursor() as cur:
                    await cur.execute(sql)
                    await cur.fetchall()
                return ValidationResult(ok=True)
            except Exception as exc:
                return ValidationResult(ok=False, errors=[f"inline SQL failed: {exc}"])

        # Table reference: confirm it exists.
        schema, name = _split_qualified(source)
        async with await self._open() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT 1
                    FROM information_schema.tables
                    WHERE table_schema = %s AND table_name = %s
                    """,
                    (schema, name),
                )
                row = await cur.fetchone()
                if not row:
                    return ValidationResult(
                        ok=False,
                        errors=[f"table {schema}.{name} does not exist"],
                    )

            errors: list[str] = []
            warnings: list[str] = []
            incr_key = table_config.get("incremental_key")
            if table_config.get("mode") == "incremental":
                if not incr_key:
                    errors.append("incremental mode requires incremental_key")
                else:
                    cols = await _fetch_columns(conn, schema, name)
                    match = next((c for c in cols if c.name == incr_key), None)
                    if not match:
                        errors.append(f"incremental_key '{incr_key}' not in {schema}.{name}")
                    elif match.source_type not in {
                        "timestamp",
                        "timestamp without time zone",
                        "timestamptz",
                        "timestamp with time zone",
                        "bigint",
                        "integer",
                        "smallint",
                    }:
                        warnings.append(
                            f"incremental_key '{incr_key}' has type "
                            f"{match.source_type}; timestamp/int recommended"
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
        incr_type = table_config.get("incremental_type", "timestamp")
        max_rows = table_config.get("max_rows")
        ascii_sanitize = table_config.get("ascii_sanitize", True)

        sql, params = _build_query(
            source=source,
            mode=mode,
            incr_key=incr_key,
            incr_type=incr_type,
            previous_watermark=previous_watermark,
            max_rows=max_rows,
        )

        started = time.monotonic()
        async with await self._open() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(f"SET LOCAL statement_timeout = {self._statement_timeout_ms}")
            await cur.execute(sql, params)
            rows = await cur.fetchall()
            # column metadata for dtype inference
            col_types = await _fetch_inline_column_types(conn, source, sql)

        df = _rows_to_dataframe(rows, col_types)
        df = apply_coercion(
            df,
            column_dtypes={
                col: pg_coercion.pandas_dtype_for(t) for col, t in col_types.items()
            },
            ascii_sanitize=ascii_sanitize,
        )

        new_wm = _compute_new_watermark(df, mode, incr_key, incr_type, previous_watermark)
        duration_ms = int((time.monotonic() - started) * 1000)
        return PullResult(
            dataframe=df,
            new_watermark=new_wm,
            rows_pulled=len(df),
            duration_ms=duration_ms,
        )

    def coerce_value(self, value: Any, source_type: str) -> Any:
        return pg_coercion.coerce_value(value, source_type)

    # ---- internals --------------------------------------------------

    async def _open(self) -> psycopg.AsyncConnection:
        if not self._conninfo:
            raise RuntimeError("PostgresDriver.connect() not called")
        return await psycopg.AsyncConnection.connect(self._conninfo, autocommit=False)


# ---- module-level helpers ------------------------------------------


async def _fetch_columns(
    conn: psycopg.AsyncConnection, schema: str, name: str
) -> list[ColumnMetadata]:
    sql = """
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position
    """
    async with conn.cursor() as cur:
        await cur.execute(sql, (schema, name))
        rows = await cur.fetchall()
    cols = []
    for col_name, data_type, is_nullable in rows:
        cols.append(
            ColumnMetadata(
                name=col_name,
                source_type=data_type,
                nullable=(is_nullable == "YES"),
                pandas_dtype=pg_coercion.pandas_dtype_for(data_type),
            )
        )
    return cols


async def _estimate_rowcount(
    conn: psycopg.AsyncConnection, schema: str, name: str
) -> int | None:
    sql = """
        SELECT reltuples::bigint
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %s AND c.relname = %s
    """
    try:
        async with conn.cursor() as cur:
            await cur.execute(sql, (schema, name))
            row = await cur.fetchone()
            return int(row[0]) if row and row[0] is not None else None
    except Exception:
        return None


async def _fetch_inline_column_types(
    conn: psycopg.AsyncConnection, source: str, sql: str
) -> dict[str, str]:
    """Return {column_name: postgres_type_name} for the result set."""
    if not _is_inline_sql(source):
        schema, name = _split_qualified(source)
        cols = await _fetch_columns(conn, schema, name)
        return {c.name: c.source_type for c in cols}

    # Inline SQL: prepare and inspect the cursor description.
    probe_sql = f"SELECT * FROM ({source}) sub LIMIT 0"
    async with conn.cursor() as cur:
        await cur.execute(probe_sql)
        if not cur.description:
            return {}
        oids = [(d.name, d.type_code) for d in cur.description]

        # Resolve OIDs -> type names.
        type_oids = list({oid for _, oid in oids})
        if not type_oids:
            return {n: "text" for n, _ in oids}
        await cur.execute(
            "SELECT oid, typname FROM pg_type WHERE oid = ANY(%s)", (type_oids,)
        )
        oid_to_name = dict(await cur.fetchall())

    result: dict[str, str] = {}
    for name, oid in oids:
        result[name] = oid_to_name.get(oid, "text")
    return result


def _rows_to_dataframe(
    rows: list[dict[str, Any]], col_types: dict[str, str]
) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame({c: pd.Series([], dtype="object") for c in col_types})

    # Pre-process object-shaped columns to their string representation so
    # apply_coercion's string-column rules can run cleanly.
    pre = []
    for row in rows:
        new_row: dict[str, Any] = {}
        for col, val in row.items():
            stype = col_types.get(col, "text")
            new_row[col] = (
                pg_coercion.coerce_value(val, stype)
                if _needs_value_prepass(stype, val)
                else val
            )
        pre.append(new_row)
    return pd.DataFrame(pre)


def _needs_value_prepass(source_type: str, value: Any) -> bool:
    if value is None:
        return False
    norm = source_type.strip().lower().split("(")[0].strip()
    if norm.endswith("[]"):
        return True
    return norm in _OBJECT_RETURN_TYPES


def _build_query(
    source: str,
    mode: str,
    incr_key: str | None,
    incr_type: str,
    previous_watermark: str | int | None,
    max_rows: int | None,
) -> tuple[str, list[Any]]:
    """Compose the SELECT for a pull, plus parameters."""
    base = f"({source}) sub" if _is_inline_sql(source) else _quote_ident(source)
    sql = f"SELECT * FROM {base}"
    params: list[Any] = []

    if mode == "incremental" and previous_watermark is not None and incr_key:
        sql += f' WHERE {_quote_column(incr_key)} > %s'
        params.append(_cast_watermark(previous_watermark, incr_type))
        sql += f" ORDER BY {_quote_column(incr_key)} ASC"

    if max_rows:
        sql += f" LIMIT {int(max_rows)}"

    return sql, params


def _quote_ident(qualified: str) -> str:
    parts = qualified.split(".")
    return ".".join(f'"{p}"' for p in parts)


def _quote_column(name: str) -> str:
    return f'"{name}"'


def _cast_watermark(value: str | int, incr_type: str) -> Any:
    if incr_type == "int":
        return int(value)
    return value  # ISO8601 string; psycopg will parse


def _compute_new_watermark(
    df: pd.DataFrame,
    mode: str,
    incr_key: str | None,
    incr_type: str,
    previous_watermark: str | int | None,
) -> str | int | None:
    if mode != "incremental" or not incr_key:
        return None
    if df.empty or incr_key not in df.columns:
        return previous_watermark
    max_val = df[incr_key].max()
    if pd.isna(max_val):
        return previous_watermark
    if incr_type == "int":
        return int(max_val)
    if isinstance(max_val, pd.Timestamp):
        return max_val.isoformat()
    return str(max_val)


def _is_inline_sql(source: str) -> bool:
    s = source.strip().lower()
    return s.startswith("select ") or "\n" in source.strip()


def _split_qualified(source: str) -> tuple[str, str]:
    if "." not in source:
        return ("public", source)
    schema, _, name = source.partition(".")
    return schema, name


__all__ = ["PostgresDriver"]


# Used only by tests for serialization assertions; keep imports tidy.
_ = json
