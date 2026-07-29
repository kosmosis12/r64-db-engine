"""Read-only DynamoDB driver."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import boto3
from botocore.config import Config as BotocoreConfig
from botocore.exceptions import ClientError, NoCredentialsError

from r64_db_engine.core.driver import Driver, PullResult, TableMetadata, ValidationResult
from r64_db_engine.drivers.dynamodb import coercion

log = logging.getLogger(__name__)

_AUTH_ERROR_CODES = frozenset({"UnrecognizedClientException", "AccessDeniedException"})


class DynamoDBAuthError(RuntimeError):
    """DynamoDB credentials are missing or rejected."""


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
        raise NotImplementedError("discover is implemented in the next Gate 2 slice")

    async def validate_table(self, table_config: dict[str, Any]) -> ValidationResult:
        raise NotImplementedError("validate_table is implemented in the next Gate 2 slice")

    async def pull(
        self, table_config: dict[str, Any], previous_watermark: str | int | None
    ) -> PullResult:
        raise NotImplementedError("pull is implemented in the next Gate 2 slice")

    def coerce_value(self, value: Any, source_type: str) -> Any:
        return coercion.coerce_value(value, source_type)

    async def _call(self, operation: str, **kwargs: Any) -> dict[str, Any]:
        if self._client is None:
            raise RuntimeError("DynamoDBDriver.connect() not called")
        method = getattr(self._client, operation)
        return await asyncio.to_thread(method, **kwargs)


__all__ = ["DynamoDBAuthError", "DynamoDBDriver"]
