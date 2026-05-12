"""Health endpoint tests."""

from __future__ import annotations

import asyncio
import json
import socket
import urllib.request

import pytest

from r64_db_engine.core.health import HealthServer


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.asyncio
async def test_health_returns_ok():
    port = _free_port()
    snap = {
        "status": "ok",
        "uptime_seconds": 10,
        "version": "0.1.0",
        "postgres": {"connected": True, "host": "localhost", "database": "a"},
        "tables": [{"target": "T", "status": "ok", "mode": "full_refresh"}],
    }
    server = HealthServer(lambda: snap, port=port)
    await server.start()
    try:
        body = await asyncio.to_thread(_fetch, f"http://127.0.0.1:{port}/health")
        parsed = json.loads(body)
        assert parsed["status"] == "ok"
        assert parsed["tables"][0]["target"] == "T"
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_health_returns_503_on_error():
    port = _free_port()
    snap = {
        "status": "error",
        "uptime_seconds": 5,
        "version": "0.1.0",
        "postgres": {"connected": False, "host": "h", "database": "d"},
        "tables": [],
    }
    server = HealthServer(lambda: snap, port=port)
    await server.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            await asyncio.to_thread(_fetch, f"http://127.0.0.1:{port}/health")
        assert exc_info.value.code == 503
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_health_404_on_other_paths():
    port = _free_port()
    server = HealthServer(lambda: {"status": "ok"}, port=port)
    await server.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            await asyncio.to_thread(_fetch, f"http://127.0.0.1:{port}/nope")
        assert exc_info.value.code == 404
    finally:
        await server.stop()


def _fetch(url: str) -> str:
    with urllib.request.urlopen(url, timeout=3) as resp:
        return resp.read().decode("utf-8")
