"""Async daemon: per-table scheduler + bounded worker pool. SPEC §3, §5, §9.

Source-agnostic. `Daemon` consumes everything through the Driver ABC and never
names a dialect; `build_daemon` resolves `dialect:` -> Driver (and `sink.type:`
-> Sink) through the registries, imported function-locally so that no core
module exposes a driver- or sink-derived attribute.
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
from r64_db_engine.core.driver import ArrowPullResult, Driver
from r64_db_engine.core.sink import Sink, SinkError
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
        writer: Sink,
    ) -> None:
        self.config = config
        self.driver = driver
        self.state = state
        self.writer = writer
        _reject_incremental_on_nonappendable_sink(config, writer)
        self.started_at: float = 0.0
        self._shutdown = asyncio.Event()
        self._pg_connected: bool = False
        self._semaphore: asyncio.Semaphore = asyncio.Semaphore(
            config.runtime.worker_pool_size
        )
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
        while time.monotonic() < deadline and any(
            t.in_flight for t in self.tables.values()
        ):
            await asyncio.sleep(0.1)

    async def _connect_loop(self) -> None:
        delay = _RECONNECT_INITIAL
        while True:
            try:
                await self.driver.connect(self.config.driver_config())
                self._pg_connected = True
                return
            except Exception as exc:
                self._pg_connected = False
                r64log.event(
                    log, "driver_connect_failed", level=logging.ERROR, error=str(exc)
                )
                if _is_permanent(exc):
                    raise
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
            await self._handle_success(
                target,
                rt,
                tcfg,
                result,
                prev_schema,
                started_at,
                reset_incremental=prev_value is None,
            )
        except _PermanentError as exc:
            self._handle_failure(target, rt, str(exc), started_at, permanent=True)
        except Exception as exc:
            self._handle_failure(target, rt, str(exc), started_at, permanent=False)
        finally:
            rt.in_flight = False

    async def _run_with_retries(
        self, tcfg: dict[str, Any], prev_watermark: str | int | None
    ):
        last_exc: Exception | None = None
        pull = self.driver.pull_arrow if self.uses_arrow_lane() else self.driver.pull
        for attempt in range(len(_RETRY_DELAYS) + 1):
            try:
                return await pull(tcfg, prev_watermark)
            except Exception as exc:
                if _is_permanent(exc):
                    raise _PermanentError(str(exc)) from exc
                if _is_disconnection(exc):
                    self._pg_connected = False
                    r64log.event(
                        log,
                        "postgres_disconnected",
                        level=logging.ERROR,
                        error=str(exc),
                    )
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
        *,
        reset_incremental: bool,
    ) -> None:
        mode = tcfg["mode"]

        if isinstance(result, ArrowPullResult):
            rows_pulled, current_schema = self._write_arrow(target, tcfg, result)
        else:
            rows_pulled, current_schema = self._write_dataframe(
                target, tcfg, result, reset_incremental=reset_incremental
            )

        # Watermark
        if result.new_watermark is not None:
            self.state.set_watermark(
                target,
                result.new_watermark,
                tcfg["incremental_type"],
                rows_pulled=rows_pulled,
                duration_ms=result.duration_ms,
            )
            rt.watermark = result.new_watermark

        # Schema drift
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
        self.state.record_pull(target, started_at, finished_at, "success", rows_pulled, None)
        rt.status = "ok"
        rt.last_success_at = finished_at
        rt.rows_pulled_last = rows_pulled
        rt.rows_pulled_total += rows_pulled
        rt.consecutive_failures = 0
        r64log.event(
            log,
            "pull_success",
            target=target,
            rows=rows_pulled,
            duration_ms=result.duration_ms,
            mode=mode,
            lane="arrow" if isinstance(result, ArrowPullResult) else "dataframe",
            watermark_after=result.new_watermark,
        )

        if mode == "full_refresh" and (
            result.duration_ms > 60_000 or rows_pulled > 1_000_000
        ):
            r64log.event(
                log,
                "full_refresh_large",
                level=logging.WARNING,
                target=target,
                rows=rows_pulled,
                duration_ms=result.duration_ms,
            )

    def uses_arrow_lane(self) -> bool:
        """Whether pulls route through the Arrow-native lane.

        BOTH ends must advertise the capability. An Arrow-capable driver
        against a sink that cannot stream (ramdb, whose codec must see the
        whole frame) falls back to the DataFrame lane, which is correct rather
        than merely convenient: the alternative is draining the reader into
        memory to rebuild a DataFrame, which costs more than the pandas lane
        and buys nothing.
        """
        return self.driver.supports_arrow() and self.writer.supports_streaming()

    def _write_dataframe(
        self,
        target: str,
        tcfg: dict[str, Any],
        result,
        *,
        reset_incremental: bool,
    ) -> tuple[int, list[dict[str, str]]]:
        df: pd.DataFrame = result.dataframe
        if tcfg["mode"] == "incremental" and not reset_incremental:
            df = self._merge_incremental(df, target)
        self.writer.write(df, target)
        schema = [
            {"name": col, "source_type": "", "pandas_dtype": str(df.dtypes[col])}
            for col in df.columns
        ]
        return result.rows_pulled, schema

    def _write_arrow(
        self, target: str, tcfg: dict[str, Any], result: ArrowPullResult
    ) -> tuple[int, list[dict[str, str]]]:
        """Stream the pull straight to the sink. Nothing materializes here.

        The full-refresh law is re-asserted rather than assumed. A non-appendable
        sink is already refused at construction
        (`_reject_incremental_on_nonappendable_sink`), but that guard keys on the
        SINK. This one keys on the LANE: incremental merging works by reading the
        previous artifact back and concatenating DataFrames, which no streaming
        write can do without materializing exactly what this lane exists to
        avoid. A future appendable streaming sink must not silently inherit a
        merge path that cannot serve it.
        """
        if tcfg["mode"] == "incremental":
            raise _PermanentError(
                f"table '{target}': incremental mode is not supported on the "
                f"Arrow-native lane — it requires reading the previous artifact "
                f"back and merging, which defeats the streaming memory bound. "
                f"Use mode: full_refresh."
            )

        # Read the schema BEFORE the sink drains the reader; afterwards there is
        # no reader left to ask.
        schema = [
            {"name": field.name, "source_type": "", "pandas_dtype": str(field.type)}
            for field in result.reader.schema
        ]
        written = self.writer.write_stream(result.reader, target)
        return written.rows_written, schema

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
            from row64tools.ramdb import load_to_df  # type: ignore[import-not-found]

            existing = load_to_df(str(existing_path))
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
                "host": self.config.postgres.host if self.config.postgres else None,
                "database": self.config.postgres.database if self.config.postgres else None,
            },
            "source": {
                "dialect": self.config.dialect,
                # `None` until connected: a driver reads its capability from
                # config in connect(), so any lane reported before then is a
                # guess, and a guess here reads as fact on /health.
                "lane": (
                    ("arrow" if self.uses_arrow_lane() else "dataframe")
                    if self._pg_connected
                    else None
                ),
                "connected": self._pg_connected,
                "host": self.config.driver_config().get("host"),
                "database": self.config.driver_config().get("database"),
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
    # A missing capability implementation is a programming error, not a blip.
    # Retrying it three times with backoff burns 21s to reach the same answer,
    # and reports "degraded" for something that will never recover on its own.
    if isinstance(exc, NotImplementedError):
        return True
    sqlstate = getattr(exc, "sqlstate", None)
    diag = getattr(exc, "diag", None)
    code = sqlstate or (getattr(diag, "sqlstate", None) if diag else None)
    if code in {"28000", "28P01", "42501", "42P01", "42601"}:
        return True
    if code in _TRANSIENT_SQLSTATES:
        return False
    # Default: psycopg.OperationalError without sqlstate is treated transient.
    return False


def _is_disconnection(exc: Exception) -> bool:
    sqlstate = getattr(exc, "sqlstate", None)
    diag = getattr(exc, "diag", None)
    code = sqlstate or (getattr(diag, "sqlstate", None) if diag else None)
    return isinstance(code, str) and code.startswith("08")


def _iso_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _reject_incremental_on_nonappendable_sink(config: Config, sink: Sink) -> None:
    """Fail fast when a table asks for incremental against a non-appendable sink.

    The incremental path merges by reading the sink's OWN previous output back
    in (`Daemon._merge_incremental`) — the PG-011 read-your-own-output pattern.
    For a format whose layout is not appendable in place, that merge cannot be
    done correctly.

    The failure is raised at construction rather than at the first pull, and it
    is raised rather than silently downgraded to full_refresh. A silent
    downgrade would write a partial snapshot that is indistinguishable, to the
    consumer, from a complete one — strictly worse than refusing to start.
    """
    if sink.supports_incremental():
        return
    offenders = [
        t.target for t in config.tables if config.resolve_table(t)["mode"] == "incremental"
    ]
    if offenders:
        raise SinkError(
            f"sink '{type(sink).sink_name()}' cannot serve incremental mode "
            f"(its output format is not appendable in place), but these tables "
            f"request it: {', '.join(sorted(offenders))}. Use mode: full_refresh."
        )


def build_daemon(config: Config) -> Daemon:
    """Wire up daemon + driver + state + sink from a Config."""
    from r64_db_engine.drivers import resolve
    from r64_db_engine.sinks import default_sink_name
    from r64_db_engine.sinks import resolve as resolve_sink

    driver_cls = resolve(config.dialect)
    driver = driver_cls()
    state = StateStore(Path(config.runtime.state_dir).expanduser() / "state.db")

    # Core names zero sinks: the default comes from the registry, and the
    # options are either the sink's own opaque block or — for a config written
    # before sinks existed — the legacy `row64:` output block.
    if config.sink is not None:
        sink_name = config.sink.type
        sink_options = config.sink.options()
    else:
        sink_name = default_sink_name()
        sink_options = {
            "loading_dir": config.row64.loading_dir,
            "group": config.row64.group,
        }

    sink = resolve_sink(sink_name)()
    sink.open(sink_options)
    return Daemon(config=config, driver=driver, state=state, writer=sink)


__all__ = ["Daemon", "TableRuntimeState", "build_daemon"]
