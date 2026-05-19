"""Async HTTP health endpoint. SPEC §8.2.

Stdlib only: minimal HTTP/1.1 parser/responder via asyncio.start_server.
We don't expose POST or anything beyond GET /health, so a real HTTP
library would be overkill.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable

log = logging.getLogger(__name__)


class HealthServer:
    def __init__(self, snapshot: Callable[[], dict], port: int = 8765) -> None:
        self._snapshot = snapshot
        self._port = port
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, host="0.0.0.0", port=self._port)
        log.info("health endpoint listening on :%d", self._port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if not line:
                return
            try:
                method, path, _proto = line.decode("ascii", errors="replace").split(" ", 2)
            except ValueError:
                await self._respond(writer, 400, "Bad Request", b"{}")
                return

            # drain the headers (we ignore them)
            while True:
                hdr = await reader.readline()
                if hdr in (b"\r\n", b"\n", b""):
                    break

            if method.upper() != "GET":
                await self._respond(writer, 405, "Method Not Allowed", b"{}")
                return

            if path.startswith("/health"):
                snap = self._snapshot()
                body = json.dumps(snap, default=str).encode("utf-8")
                status = 200 if snap.get("status") in ("ok", "degraded") else 503
                await self._respond(
                    writer, status, "OK" if status == 200 else "Service Unavailable", body
                )
            else:
                await self._respond(writer, 404, "Not Found", b"{}")
        except Exception as exc:
            log.debug("health request error: %s", exc)
            with contextlib.suppress(Exception):
                await self._respond(writer, 500, "Internal Server Error", b"{}")
        finally:
            with _suppress_oserror():
                writer.close()
                await writer.wait_closed()

    async def _respond(
        self, writer: asyncio.StreamWriter, code: int, reason: str, body: bytes
    ) -> None:
        headers = (
            f"HTTP/1.1 {code} {reason}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("ascii")
        writer.write(headers + body)
        await writer.drain()


def _suppress_oserror():
    return contextlib.suppress(OSError)


__all__ = ["HealthServer"]
