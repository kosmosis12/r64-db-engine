"""Structured JSON logging. SPEC §8.1."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "level": record.levelname.lower(),
            "event": record.getMessage(),
            "logger": record.name,
        }
        extras = getattr(record, "extras", None)
        if isinstance(extras, dict):
            payload.update(extras)
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure(log_level: str = "info", log_format: str = "json") -> None:
    """Reset root logger to emit either JSON (default) or text to stdout."""
    level = getattr(logging, log_level.upper(), logging.INFO)
    handler = logging.StreamHandler(stream=sys.stdout)
    if log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)


def event(
    logger: logging.Logger, event_name: str, level: int = logging.INFO, **fields: Any
) -> None:
    """Emit a structured event with arbitrary key/value fields."""
    logger.log(level, event_name, extra={"extras": fields})


__all__ = ["configure", "event", "JsonFormatter"]
