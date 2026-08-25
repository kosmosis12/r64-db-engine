"""Health endpoint tests.

Every case here binds a real loopback listener and talks HTTP to it, so the
substrate under test is the host's TCP stack rather than anything this repo
ships. That substrate is not universally present: a network namespace with no
configured loopback, a locked-down container, or an execution wrapper that
isolates the network all make the bind fail — and a bind that fails renders an
`OSError` out of `_free_port()` or a connection refusal out of `_fetch()`,
which reads as a REGRESSION in this driver rather than as a context that could
not run the check.

That is the COULD-NOT-OBSERVE doctrine inverted, and it is the failure this
guard exists to end: a check that cannot observe must record COULD-NOT-OBSERVE,
never absence and never presence. So the substrate is PROBED — with the same
operation the tests perform, because a probe that checks something adjacent is
a guess — and an unreachable one produces a skip with a reason, not a red.
"""

from __future__ import annotations

import asyncio
import functools
import json
import socket
import urllib.request

import pytest

from r64_db_engine.core.health import HealthServer


@functools.lru_cache(maxsize=1)
def _loopback_unreachable() -> str | None:
    """Why loopback TCP cannot carry these tests here, or None when it can.

    Bind AND connect, because the two fail independently: a namespace can offer
    an address to bind that nothing may then reach. Cached, so the probe costs
    one socket pair for the whole module rather than one per case.
    """
    try:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            with socket.create_connection(("127.0.0.1", listener.getsockname()[1]), timeout=2):
                pass
    except OSError as exc:
        return (
            f"loopback TCP is unreachable in this context ({type(exc).__name__}: {exc}). "
            f"These tests bind a real HealthServer and fetch from it; without a usable "
            f"loopback the check cannot be observed here, and an unobservable check is "
            f"recorded as a skip rather than asserted either way."
        )
    return None


@pytest.fixture(autouse=True)
def _require_loopback() -> None:
    """Skip, never fail, when the substrate these tests need is absent.

    Autouse and module-wide on purpose: a per-test decorator is a promise every
    future case in this file remembers to repeat, which is the same promise the
    round-4 outer boundary declined to rely on.
    """
    reason = _loopback_unreachable()
    if reason is not None:
        pytest.skip(reason)


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
