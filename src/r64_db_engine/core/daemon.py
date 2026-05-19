"""Async daemon: per-table scheduler + bounded worker pool. SPEC §3, §5, §9.

Source-agnostic. Resolves `dialect:` -> Driver via the drivers registry
and consumes everything else through the Driver ABC.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from r64_db_engine.core import coercion
from r64_db_engine.core import logging as r64log
from r64_db_engine.core.config import Config
from r64_db_engine.core.driver import Driver
from r64_db_engine.core.ramdb_writer import RamdbWriter
from r64_db_engine.core.state import StateStore

log = logging.getLogger(__name__)

_TRANSIENT_SQLSTATES = frozenset({"08000", "08001", "08003", "08004", "08006", "08007", "57P01"})
_RETRY_DELAYS = (1, 4, 16)
_RECONNECT_INITIAL = 5
_RECONNECT_MAX = 60


@dataclass
class TableRuntimeState:
    target: str
    status: str = "pending"  # pending | ok | error | degraded
    mode: str = "full_refresh"
    last_success_at: str | None = None
    last_error: str | None = None
    last_error_at: str | None = None
    rows_pulled_last: int = 0
    rows_pulled_total: int = 0
    watermark: str | int | None = None
    consecutive_failures: int = 0
    schema_drift_detected: bool = False
    cadence_seconds: int = 60
    in_flight: bool = False
    last_started: float = field(default_factory=lambda: 0.0)


class Daemon:
    """The supervised engine. One instance per running process."""

    def __init__(
        self,
        config: Config,
        driver: Driver,
        state: StateStore,
        writer: RamdbWriter,
    ) -> None:
        self.config = config
        self.driver = driver
        self.state = state
        self.writer = writer
        self.started_at: float = 0.0
        self._shutdown = asyncio.Event()
        self._pg_connected: bool = False
        self._semaphore: asyncio.Semaphore = asyncio.Semaphore(config.runtime.worker_pool_size)
        self.tables: dict[str, TableRuntimeState] = {}
        for t in config.tables:
            resolved = config.resolve_table(t)
            self.tables[t.target] = TableRuntimeState(
                target=t.target,
                mode=resolved["mode"],
                cadence_seconds=resolved["cadence_seconds"],
            )

    # ---- lifecycle ---------------------------------------------------

    async def run(self, once: bool = False) -> None:
        self.started_at = time.monotonic()
        self.writer.cleanup_orphan_tempfiles()
        await self._connect_loop()
        r64log.event(log, "daemon_start", tables=len(self.tables), once=once)

        if once:
            await asyncio.gather(*[self._pull_once(t.target) for t in self.config.tables])
            return

        tasks = [asyncio.create_task(self._table_loop(t.target)) for t in self.config.tables]
        try:
            await self._shutdown.wait()
        finally:
            r64log.event(log, "daemon_stop")
            for t in tasks:
                t.cancel()
            with suppress(asyncio.CancelledError):
                await asyncio.gather(*tasks, return_exceptions=True)
            await self._await_in_flight()
            await self.driver.close()

    def request_shutdown(self) -> None:
        self._shutdown.set()

    def install_signal_handlers(self, loop: asyncio.AbstractEventLoop) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            with suppress(NotImplementedError):
                loop.add_signal_handler(sig, self.request_shutdown)

    async def _await_in_flight(self) -> None:
        grace = self.config.runtime.shutdown_grace_seconds
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline and any(t.in_flight for t in self.tables.values()):
            await asyncio.sleep(0.1)

    async def _connect_loop(self) -> None:
        delay = _RECONNECT_INITIAL
        while True:
            try:
                await self.driver.connect(self.config.postgres.model_dump())
                self._pg_connected = True
                return
            except Exception as exc:
                self._pg_connected = False
                r64log.event(log, "postgres_connect_failed", level=logging.ERROR, error=str(exc))
                if self._shutdown.is_set():
                    return
                await asyncio.sleep(delay)
                delay = min(delay * 2, _RECONNECT_MAX)

    # ---- per-table scheduling ---------------------------------------

    async def _table_loop(self, target: str) -> None:
        cadence = self.tables[target].cadence_seconds
        while not self._shutdown.is_set():
            await self._pull_once(target)
            with suppress(TimeoutError):
                await asyncio.wait_for(self._shutdown.wait(), timeout=cadence)

    async def _pull_once(self, target: str) -> None:
        rt = self.tables[target]
        if rt.in_flight:
            self.state.record_pull(target, _iso_now(), _iso_now(), "skipped_overlap", None, None)
            r64log.event(log, "pull_skipped_overlap", target=target)
            return

        rt.in_flight = True
        rt.last_started = time.monotonic()
        started_at = _iso_now()
        tcfg = self._find_table_config(target)
        if tcfg is None:
            rt.in_flight = False
            return

        prev_value, _ = self.state.get_watermark(target)
        prev_schema = self.state.get_schema(target)

        try:
            async with self._semaphore:
                result = await self._run_with_retries(tcfg, prev_value)
            await self._handle_success(target, rt, tcfg, result, prev_schema, started_at)
        except _PermanentError as exc:
            self._handle_failure(target, rt, str(exc), started_at, permanent=True)
        except Exception as exc:
            self._handle_failure(target, rt, str(exc), started_at, permanent=False)
        finally:
            rt.in_flight = False

    async def _run_with_retries(self, tcfg: dict[str, Any], prev_watermark: str | int | None):
        last_exc: Exception | None = None
        for attempt in range(len(_RETRY_DELAYS) + 1):
            try:
                return await self.driver.pull(tcfg, prev_watermark)
            except Exception as exc:
                if _is_permanent(exc):
                    raise _PermanentError(str(exc)) from exc
                last_exc = exc
                if attempt < len(_RETRY_DELAYS):
                    await asyncio.sleep(_RETRY_DELAYS[attempt])
        assert last_exc is not None
        raise last_exc

    async def _handle_success(
        self,
        target: str,
        rt: TableRuntimeState,
        tcfg: dict[str, Any],
        result,
        prev_schema: list[dict[str, str]] | None,
        started_at: str,
    ) -> None:
        df: pd.DataFrame = result.dataframe
        mode = tcfg["mode"]

        if mode == "incremental":
            df = self._merge_incremental(df, target)

        self.writer.write(df, target)

        # Watermark
        if result.new_watermark is not None:
            self.state.set_watermark(
                target,
                result.new_watermark,
                tcfg["incremental_type"],
                rows_pulled=result.rows_pulled,
                duration_ms=result.duration_ms,
            )
            rt.watermark = result.new_watermark

        # Schema drift
        current_schema = [
            {"name": col, "source_type": "", "pandas_dtype": str(df.dtypes[col])}
            for col in df.columns
        ]
        diff = coercion.compare_schemas(prev_schema, current_schema)
        if any(diff.values()) and prev_schema is not None:
            rt.schema_drift_detected = True
            r64log.event(
                log,
                "schema_drift",
                level=logging.WARNING,
                target=target,
                added=diff["added"],
                removed=diff["removed"],
                type_changed=diff["type_changed"],
            )
        self.state.set_schema(target, current_schema)

        finished_at = _iso_now()
        self.state.record_pull(target, started_at, finished_at, "success", result.rows_pulled, None)
        rt.status = "ok"
        rt.last_success_at = finished_at
        rt.rows_pulled_last = result.rows_pulled
        rt.rows_pulled_total += result.rows_pulled
        rt.consecutive_failures = 0
        r64log.event(
            log,
            "pull_success",
            target=target,
            rows=result.rows_pulled,
            duration_ms=result.duration_ms,
            mode=mode,
            watermark_after=result.new_watermark,
        )

        if mode == "full_refresh" and (
            result.duration_ms > 60_000 or result.rows_pulled > 1_000_000
        ):
            r64log.event(
                log,
                "full_refresh_large",
                level=logging.WARNING,
                target=target,
                rows=result.rows_pulled,
                duration_ms=result.duration_ms,
            )

    def _handle_failure(
        self, target: str, rt: TableRuntimeState, msg: str, started_at: str, *, permanent: bool
    ) -> None:
        finished_at = _iso_now()
        self.state.record_pull(target, started_at, finished_at, "error", None, msg)
        rt.consecutive_failures = self.state.consecutive_failures(target)
        rt.last_error = msg
        rt.last_error_at = finished_at
        rt.status = "error" if rt.consecutive_failures >= 3 or permanent else "degraded"
        r64log.event(
            log,
            "pull_error",
            level=logging.ERROR,
            target=target,
            permanent=permanent,
            error=msg,
        )

    def _merge_incremental(self, new_df: pd.DataFrame, target: str) -> pd.DataFrame:
        """SPEC §5.2: read existing ramdb, concat new rows, write back."""
        existing_path = self.writer.target_path(target)
        if not existing_path.exists():
            return new_df
        try:
            from row64tools import ramdb  # type: ignore[import-not-found]

            existing = ramdb.load_to_df(str(existing_path))
            return pd.concat([existing, new_df], ignore_index=True)
        except Exception as exc:
            log.warning("incremental_merge_failed target=%s err=%s — using new only", target, exc)
            return new_df

    def _find_table_config(self, target: str) -> dict[str, Any] | None:
        for t in self.config.tables:
            if t.target == target:
                return self.config.resolve_table(t)
        return None

    # ---- health introspection ---------------------------------------

    def status_snapshot(self) -> dict[str, Any]:
        from r64_db_engine import __version__

        any_error = any(t.status == "error" for t in self.tables.values())
        any_degraded = any(t.status == "degraded" for t in self.tables.values())
        any_drift = any(t.schema_drift_detected for t in self.tables.values())
        overall = "ok"
        if any_error or not self._pg_connected:
            overall = "error"
        elif any_degraded or any_drift:
            overall = "degraded"

        now = time.monotonic()
        return {
            "status": overall,
            "uptime_seconds": int(now - self.started_at) if self.started_at else 0,
            "version": __version__,
            "postgres": {
                "connected": self._pg_connected,
                "host": self.config.postgres.host,
                "database": self.config.postgres.database,
            },
            "tables": [self._table_status(t) for t in self.tables.values()],
        }

    def _table_status(self, t: TableRuntimeState) -> dict[str, Any]:
        out: dict[str, Any] = {
            "target": t.target,
            "status": t.status if t.status != "pending" else "ok",
            "mode": t.mode,
            "last_success_at": t.last_success_at,
            "rows_pulled_last": t.rows_pulled_last,
            "rows_pulled_total": t.rows_pulled_total,
            "watermark": t.watermark,
            "schema_drift_detected": t.schema_drift_detected,
        }
        if t.last_error is not None:
            out["last_error"] = t.last_error
            out["last_error_at"] = t.last_error_at
            out["consecutive_failures"] = t.consecutive_failures
        return out


# ---- helpers --------------------------------------------------------


class _PermanentError(RuntimeError):
    """Driver raised something we should not retry (auth, missing table, syntax)."""


def _is_permanent(exc: Exception) -> bool:
    sqlstate = getattr(exc, "sqlstate", None)
    diag = getattr(exc, "diag", None)
    code = sqlstate or (getattr(diag, "sqlstate", None) if diag else None)
    if code in {"28000", "28P01", "42501", "42P01", "42601"}:
        return True
    if code in _TRANSIENT_SQLSTATES:
        return False
    # Default: psycopg.OperationalError without sqlstate is treated transient.
    return False


def _iso_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def build_daemon(config: Config) -> Daemon:
    """Wire up daemon + driver + state + writer from a Config."""
    from r64_db_engine.drivers import resolve

    driver_cls = resolve(config.dialect)
    driver = driver_cls()
    state = StateStore(Path(config.runtime.state_dir).expanduser() / "state.db")
    writer = RamdbWriter(config.row64.loading_dir, config.row64.group)
    return Daemon(config=config, driver=driver, state=state, writer=writer)


__all__ = ["Daemon", "TableRuntimeState", "build_daemon"]
