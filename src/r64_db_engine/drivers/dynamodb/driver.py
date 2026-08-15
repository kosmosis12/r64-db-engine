"""Read-only DynamoDB driver."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import boto3
import pandas as pd
from boto3.dynamodb.types import TypeDeserializer
from botocore.config import Config as BotocoreConfig
from botocore.exceptions import ClientError, NoCredentialsError

from r64_db_engine.core.coercion import apply_coercion
from r64_db_engine.core.driver import (
    ColumnMetadata,
    Driver,
    PullResult,
    TableMetadata,
    ValidationResult,
)
from r64_db_engine.drivers.dynamodb import coercion

log = logging.getLogger(__name__)

_AUTH_ERROR_CODES = frozenset({"UnrecognizedClientException", "AccessDeniedException"})


class DynamoDBAuthError(RuntimeError):
    """DynamoDB credentials are missing or rejected."""


@dataclass(frozen=True)
class DynamoDBTableMetadata(TableMetadata):
    status: str
    billing_mode: str
    partition_key: str
    sort_key: str | None


class DynamoDBDriver(Driver):
    def __init__(self) -> None:
        self._client: Any | None = None
        self._schema_sample_items = 1000
        self._consistent_read = False
        self._scan_segments = 1
        self._scan_page_limit: int | None = None

    @classmethod
    def dialect_name(cls) -> str:
        return "dynamodb"

    async def connect(self, config: dict[str, Any]) -> None:
        segments = int(config.get("scan_segments", 1))
        if not 1 <= segments <= 32:
            raise ValueError("dynamodb.scan_segments must be between 1 and 32")
        sample_items = int(config.get("schema_sample_items", 1000))
        if sample_items < 1:
            raise ValueError("dynamodb.schema_sample_items must be positive")

        session = boto3.Session(
            profile_name=config.get("profile") or None,
            region_name=config.get("region") or None,
        )
        self._client = session.client(
            "dynamodb",
            endpoint_url=config.get("endpoint_url") or None,
            config=BotocoreConfig(retries={"max_attempts": 10, "mode": "adaptive"}),
        )
        self._schema_sample_items = sample_items
        self._consistent_read = bool(config.get("consistent_read", False))
        self._scan_segments = segments
        page_limit = config.get("scan_page_limit")
        self._scan_page_limit = int(page_limit) if page_limit is not None else None

        try:
            await self._call("list_tables", Limit=1)
        except NoCredentialsError as exc:
            self._client = None
            raise DynamoDBAuthError("DynamoDB credentials were not found") from exc
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in _AUTH_ERROR_CODES:
                self._client = None
                raise DynamoDBAuthError(f"DynamoDB authentication failed: {code}") from exc
            raise
        log.info("dynamodb_connected region=%s", config.get("region") or "credential-chain")

    async def close(self) -> None:
        client, self._client = self._client, None
        close = getattr(client, "close", None)
        if close is not None:
            await asyncio.to_thread(close)

    async def discover(self, schema_filter: str | None = None) -> list[TableMetadata]:
        names = [schema_filter] if schema_filter else await self._list_tables()
        tables: list[TableMetadata] = []
        for name in names:
            description, columns = await self._describe_and_infer(name)
            table = description["Table"]
            partition_key, sort_key = _key_names(table)
            billing = table.get("BillingModeSummary", {}).get(
                "BillingMode", "PROVISIONED"
            )
            tables.append(
                DynamoDBTableMetadata(
                    schema="dynamodb",
                    name=name,
                    columns=columns,
                    estimated_rows=table.get("ItemCount"),
                    candidate_incremental_keys=[
                        column.name
                        for column in columns
                        if column.source_type in {"N", "S"}
                    ],
                    status=table.get("TableStatus", "UNKNOWN"),
                    billing_mode=billing,
                    partition_key=partition_key,
                    sort_key=sort_key,
                )
            )
        return tables

    async def validate_table(self, table_config: dict[str, Any]) -> ValidationResult:
        table_name = table_config.get("source") or table_config.get("name")
        if not table_name:
            return ValidationResult(ok=False, errors=["source is required"])
        try:
            description, columns = await self._describe_and_infer(table_name)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
                return ValidationResult(
                    ok=False, errors=[f"table {table_name} does not exist"]
                )
            raise

        table = description["Table"]
        errors: list[str] = []
        if table.get("TableStatus") != "ACTIVE":
            errors.append(
                f"table {table_name} is not ACTIVE (status={table.get('TableStatus')})"
            )
        incremental_key = table_config.get("incremental_key")
        column_names = {column.name for column in columns}
        if incremental_key and incremental_key not in column_names:
            errors.append(f"incremental_key '{incremental_key}' not in sampled schema")

        if table_config.get("incremental_mode", "filter_scan") == "gsi_query":
            gsi_name = table_config.get("incremental_gsi")
            partition_value = table_config.get("incremental_gsi_partition_value")
            if not gsi_name:
                errors.append("gsi_query requires incremental_gsi")
            if partition_value is None:
                errors.append("gsi_query requires incremental_gsi_partition_value")
            gsi = next(
                (item for item in table.get("GlobalSecondaryIndexes", [])
                 if item.get("IndexName") == gsi_name),
                None,
            )
            if gsi_name and gsi is None:
                errors.append(f"incremental_gsi '{gsi_name}' does not exist")
            elif gsi is not None:
                _, gsi_sort = _key_names(gsi)
                if gsi_sort != incremental_key:
                    errors.append(
                        f"incremental_gsi '{gsi_name}' sort key must be "
                        f"incremental_key '{incremental_key}'"
                    )
        return ValidationResult(ok=not errors, errors=errors)

    async def pull(
        self, table_config: dict[str, Any], previous_watermark: str | int | None
    ) -> PullResult:
        import time

        started = time.monotonic()
        table_name = table_config.get("source") or table_config.get("name")
        if not table_name:
            raise ValueError("source is required")
        description = await self._call("describe_table", TableName=table_name)
        table = description["Table"]
        partition_key, sort_key = _key_names(table)
        key_names = [partition_key]
        if sort_key is not None:
            key_names.append(sort_key)
        mode = table_config.get("mode", "full_refresh")
        incremental_mode = table_config.get("incremental_mode", "filter_scan")
        incremental_key = table_config.get("incremental_key")

        if mode == "incremental" and incremental_mode == "gsi_query":
            items = await self._query_incremental(
                table_name, table, table_config, previous_watermark
            )
        else:
            scan_kwargs: dict[str, Any] = {
                "TableName": table_name,
                "ConsistentRead": self._consistent_read,
            }
            if self._scan_page_limit is not None:
                scan_kwargs["Limit"] = self._scan_page_limit
            if mode == "incremental" and previous_watermark is not None:
                if not incremental_key:
                    raise ValueError("incremental mode requires incremental_key")
                attr_type = _attribute_type(table, incremental_key)
                scan_kwargs.update(
                    {
                        "FilterExpression": "#k > :watermark",
                        "ExpressionAttributeNames": {"#k": incremental_key},
                        "ExpressionAttributeValues": {
                            ":watermark": _serialize_scalar(previous_watermark, attr_type)
                        },
                    }
                )
            items = await self._scan_all(scan_kwargs)

        dataframe = _items_to_dataframe(
            items,
            key_names=key_names,
            ascii_sanitize=bool(table_config.get("ascii_sanitize", True)),
        )
        new_watermark: str | int | None = previous_watermark
        if mode != "incremental":
            new_watermark = None
        elif incremental_key and incremental_key in dataframe and not dataframe.empty:
            new_watermark = dataframe[incremental_key].max()
            if hasattr(new_watermark, "item"):
                new_watermark = new_watermark.item()
        return PullResult(
            dataframe=dataframe,
            new_watermark=new_watermark,
            rows_pulled=len(dataframe),
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    def coerce_value(self, value: Any, source_type: str) -> Any:
        return coercion.coerce_value(value, source_type)

    async def _call(self, operation: str, **kwargs: Any) -> dict[str, Any]:
        if self._client is None:
            raise RuntimeError("DynamoDBDriver.connect() not called")
        method = getattr(self._client, operation)
        return await asyncio.to_thread(method, **kwargs)

    async def _list_tables(self) -> list[str]:
        names: list[str] = []
        kwargs: dict[str, Any] = {}
        while True:
            page = await self._call("list_tables", **kwargs)
            names.extend(page.get("TableNames", []))
            last = page.get("LastEvaluatedTableName")
            if not last:
                return names
            kwargs = {"ExclusiveStartTableName": last}

    async def _describe_and_infer(
        self, table_name: str
    ) -> tuple[dict[str, Any], list[ColumnMetadata]]:
        description = await self._call("describe_table", TableName=table_name)
        table = description["Table"]
        partition_key, sort_key = _key_names(table)
        key_types = {
            item["AttributeName"]: item["AttributeType"]
            for item in table.get("AttributeDefinitions", [])
        }
        observed: dict[str, set[str]] = {}
        examples: dict[str, list[dict[str, Any]]] = {}
        remaining = self._schema_sample_items
        kwargs: dict[str, Any] = {
            "TableName": table_name,
            "Limit": remaining,
            "ConsistentRead": self._consistent_read,
        }
        while remaining > 0:
            page = await self._call("scan", **kwargs)
            for item in page.get("Items", []):
                for name, attribute in item.items():
                    source_type = next(iter(attribute))
                    observed.setdefault(name, set()).add(source_type)
                    examples.setdefault(name, []).append(attribute)
            remaining -= len(page.get("Items", []))
            last = page.get("LastEvaluatedKey")
            if not last or remaining <= 0:
                break
            kwargs["ExclusiveStartKey"] = last
            kwargs["Limit"] = remaining

        for name, source_type in key_types.items():
            observed.setdefault(name, set()).add(source_type)
        ordered = [partition_key]
        if sort_key is not None:
            ordered.append(sort_key)
        ordered.extend(sorted(set(observed) - set(ordered)))
        columns: list[ColumnMetadata] = []
        for name in ordered:
            types = observed[name]
            if len(types) > 1:
                source_type = "mixed(" + ",".join(sorted(types)) + ")"
                dtype = "string"
                log.warning(
                    "schema_type_conflict table=%s attribute=%s observed_types=%s",
                    table_name,
                    name,
                    sorted(types),
                )
            else:
                source_type = next(iter(types))
                values = examples.get(name, [])
                if source_type == "N" and values:
                    from boto3.dynamodb.types import TypeDeserializer

                    deserializer = TypeDeserializer()
                    dtypes = {
                        coercion.pandas_dtype_for("N", deserializer.deserialize(value))
                        for value in values
                    }
                    dtype = "float64" if "float64" in dtypes else "int64"
                else:
                    dtype = coercion.pandas_dtype_for(source_type)
            columns.append(
                ColumnMetadata(
                    name=name,
                    source_type=source_type,
                    nullable=name not in {partition_key, sort_key},
                    pandas_dtype=dtype,
                )
            )
        return description, columns

    async def _scan_all(self, kwargs: dict[str, Any]) -> list[dict[str, Any]]:
        if self._scan_segments == 1:
            return await asyncio.to_thread(self._paginate_sync, "scan", kwargs)
        pages = await asyncio.gather(
            *[
                asyncio.to_thread(
                    self._paginate_sync,
                    "scan",
                    {**kwargs, "Segment": segment, "TotalSegments": self._scan_segments},
                )
                for segment in range(self._scan_segments)
            ]
        )
        return [item for segment_items in pages for item in segment_items]

    async def _query_incremental(
        self,
        table_name: str,
        table: dict[str, Any],
        table_config: dict[str, Any],
        previous_watermark: str | int | None,
    ) -> list[dict[str, Any]]:
        gsi_name = table_config.get("incremental_gsi")
        partition_value = table_config.get("incremental_gsi_partition_value")
        incremental_key = table_config.get("incremental_key")
        if not gsi_name or partition_value is None or not incremental_key:
            raise ValueError(
                "gsi_query requires incremental_gsi, incremental_key, and "
                "incremental_gsi_partition_value"
            )
        gsi = next(
            (item for item in table.get("GlobalSecondaryIndexes", [])
             if item.get("IndexName") == gsi_name),
            None,
        )
        if gsi is None:
            raise ValueError(f"incremental_gsi '{gsi_name}' does not exist")
        partition_key, sort_key = _key_names(gsi)
        if sort_key != incremental_key:
            raise ValueError(
                f"incremental_gsi '{gsi_name}' sort key must be '{incremental_key}'"
            )
        values = {
            ":partition": _serialize_scalar(
                partition_value, _attribute_type(table, partition_key)
            )
        }
        expression = "#p = :partition"
        if previous_watermark is not None:
            expression += " AND #k > :watermark"
            values[":watermark"] = _serialize_scalar(
                previous_watermark, _attribute_type(table, incremental_key)
            )
        kwargs: dict[str, Any] = {
            "TableName": table_name,
            "IndexName": gsi_name,
            "KeyConditionExpression": expression,
            "ExpressionAttributeNames": {"#p": partition_key, "#k": incremental_key},
            "ExpressionAttributeValues": values,
        }
        if self._scan_page_limit is not None:
            kwargs["Limit"] = self._scan_page_limit
        return await asyncio.to_thread(self._paginate_sync, "query", kwargs)

    def _paginate_sync(
        self, operation: str, initial_kwargs: dict[str, Any]
    ) -> list[dict[str, Any]]:
        if self._client is None:
            raise RuntimeError("DynamoDBDriver.connect() not called")
        method = getattr(self._client, operation)
        kwargs = dict(initial_kwargs)
        items: list[dict[str, Any]] = []
        while True:
            page = method(**kwargs)
            items.extend(page.get("Items", []))
            last = page.get("LastEvaluatedKey")
            if not last:
                return items
            kwargs["ExclusiveStartKey"] = last


def _key_names(description: dict[str, Any]) -> tuple[str, str | None]:
    keys = {item["KeyType"]: item["AttributeName"] for item in description["KeySchema"]}
    return keys["HASH"], keys.get("RANGE")


def _attribute_type(table: dict[str, Any], name: str) -> str:
    for item in table.get("AttributeDefinitions", []):
        if item["AttributeName"] == name:
            return item["AttributeType"]
    raise ValueError(f"attribute '{name}' has no declared S/N type")


def _serialize_scalar(value: Any, source_type: str) -> dict[str, str]:
    if source_type == "N":
        return {"N": str(value)}
    if source_type == "S":
        return {"S": str(value)}
    raise ValueError(f"incremental attributes must be S or N, got {source_type}")


def _items_to_dataframe(
    items: list[dict[str, Any]], *, key_names: list[str], ascii_sanitize: bool
) -> pd.DataFrame:
    if not items:
        return pd.DataFrame()
    deserializer = TypeDeserializer()
    observed: dict[str, set[str]] = {}
    rows: list[dict[str, Any]] = []
    for item in items:
        row: dict[str, Any] = {}
        for name, attribute in item.items():
            source_type = next(iter(attribute))
            observed.setdefault(name, set()).add(source_type)
            row[name] = coercion.coerce_value(
                deserializer.deserialize(attribute), source_type
            )
        rows.append(row)
    ordered = [name for name in key_names if name in observed]
    ordered.extend(sorted(set(observed) - set(ordered)))
    frame = pd.DataFrame(rows).reindex(columns=ordered)
    dtypes: dict[str, str] = {}
    for name in ordered:
        types = observed[name]
        if len(types) > 1:
            dtypes[name] = "string"
        else:
            source_type = next(iter(types))
            if source_type == "N":
                values = frame[name].dropna()
                dtypes[name] = (
                    "float64"
                    if any(isinstance(value, float) for value in values)
                    else "int64"
                )
            else:
                dtypes[name] = coercion.pandas_dtype_for(source_type)
    return apply_coercion(frame, dtypes, ascii_sanitize=ascii_sanitize)


__all__ = ["DynamoDBAuthError", "DynamoDBDriver", "DynamoDBTableMetadata"]
