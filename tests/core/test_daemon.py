"""Daemon scheduler / pull pipeline tests using a stub Driver.

These exercise core/ end-to-end without Postgres — they prove the
abstraction holds, and they back the "zero changes to core/ when
adding a new driver" invariant from SPEC §15.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from r64_db_engine.core import ramdb_writer as rw
from r64_db_engine.core.config import Config
from r64_db_engine.core.daemon import Daemon
from r64_db_engine.core.driver import (
    ColumnMetadata,
    Driver,
    PullResult,
    TableMetadata,
    ValidationResult,
)
from r64_db_engine.core.ramdb_writer import RamdbWriter
from r64_db_engine.core.state import StateStore


class StubDriver(Driver):
    """In-memory driver. Returns scripted PullResults."""

    def __init__(self) -> None:
        self.connected = False
        self.scripted: dict[str, list[PullResult]] = {}
        self.call_count: dict[str, int] = {}
        self.fail_with: Exception | None = None

    @classmethod
    def dialect_name(cls) -> str:
        return "stub"

    async def connect(self, config: dict[str, Any]) -> None:
        self.connected = True

    async def close(self) -> None:
        self.connected = False

    async def discover(self, schema_filter: str | None = None) -> list[TableMetadata]:
        return [
            TableMetadata(
                schema="public",
                name="t",
                columns=[ColumnMetadata("id", "bigint", False, "int64")],
                estimated_rows=0,
                candidate_incremental_keys=["id"],
            )
        ]

    async def validate_table(self, table_config: dict[str, Any]) -> ValidationResult:
        return ValidationResult(ok=True)

    async def pull(
        self, table_config: dict[str, Any], previous_watermark: str | int | None
    ) -> PullResult:
        target = table_config["target"]
        self.call_count[target] = self.call_count.get(target, 0) + 1
        if self.fail_with is not None:
            raise self.fail_with
        script = self.scripted.get(target, [])
        if not script:
            return PullResult(
                dataframe=pd.DataFrame({"id": [1, 2, 3]}),
                new_watermark=None,
                rows_pulled=3,
                duration_ms=10,
            )
        return script.pop(0)

    def coerce_value(self, value, source_type):
        return value


@pytest.fixture
def fake_ramdb(monkeypatch: pytest.MonkeyPatch):
    def fake(df: pd.DataFrame, path: str) -> None:
        Path(path).write_bytes(b"RAMDB" + str(len(df)).encode())

    monkeypatch.setattr(rw, "_save_ramdb", fake)


def _config(tmp_path: Path, mode: str = "full_refresh") -> Config:
    loading = tmp_path / "loading"
    loading.mkdir()
    state_dir = tmp_path / "state"
    table: dict[str, Any] = {"source": "public.t", "target": "T", "mode": mode, "cadence": "5s"}
    if mode == "incremental":
        table["incremental_key"] = "id"
        table["incremental_type"] = "int"
    return Config.model_validate(
        {
            "dialect": "postgres",
            "postgres": {"database": "x"},
            "row64": {"loading_dir": str(loading), "group": "G"},
            "tables": [table],
            "runtime": {"state_dir": str(state_dir)},
            "telemetry": {"health_port": 0, "metrics_port": 0},
        }
    )


@pytest.mark.asyncio
async def test_daemon_once_writes_ramdb(tmp_path: Path, fake_ramdb) -> None:
    cfg = _config(tmp_path)
    driver = StubDriver()
    state = StateStore(tmp_path / "state" / "state.db")
    writer = RamdbWriter(cfg.row64.loading_dir, cfg.row64.group)
    d = Daemon(cfg, driver, state, writer)

    await d.run(once=True)
    out = Path(cfg.row64.loading_dir) / "G" / "T.ramdb"
    assert out.exists()
    assert driver.call_count["T"] == 1


@pytest.mark.asyncio
async def test_daemon_full_refresh_status_is_ok(tmp_path: Path, fake_ramdb) -> None:
    cfg = _config(tmp_path)
    driver = StubDriver()
    state = StateStore(tmp_path / "state" / "state.db")
    writer = RamdbWriter(cfg.row64.loading_dir, cfg.row64.group)
    d = Daemon(cfg, driver, state, writer)
    await d.run(once=True)
    snap = d.status_snapshot()
    assert snap["status"] == "ok"
    assert snap["postgres"]["connected"] is True
    assert snap["tables"][0]["target"] == "T"
    assert snap["tables"][0]["status"] == "ok"
    assert snap["tables"][0]["rows_pulled_last"] == 3


@pytest.mark.asyncio
async def test_daemon_incremental_advances_watermark(tmp_path: Path, fake_ramdb) -> None:
    cfg = _config(tmp_path, mode="incremental")
    driver = StubDriver()
    driver.scripted["T"] = [
        PullResult(pd.DataFrame({"id": [1, 2, 3]}), new_watermark=3, rows_pulled=3, duration_ms=5),
        PullResult(pd.DataFrame({"id": [4, 5]}), new_watermark=5, rows_pulled=2, duration_ms=5),
    ]
    state = StateStore(tmp_path / "state" / "state.db")
    writer = RamdbWriter(cfg.row64.loading_dir, cfg.row64.group)
    d = Daemon(cfg, driver, state, writer)

    await d._pull_once("T")
    val, _ = state.get_watermark("T")
    assert val == 3

    await d._pull_once("T")
    val, _ = state.get_watermark("T")
    assert val == 5


@pytest.mark.asyncio
async def test_daemon_pull_error_marks_status(tmp_path: Path, fake_ramdb) -> None:
    cfg = _config(tmp_path)
    driver = StubDriver()
    driver.fail_with = RuntimeError("simulated")
    state = StateStore(tmp_path / "state" / "state.db")
    writer = RamdbWriter(cfg.row64.loading_dir, cfg.row64.group)
    d = Daemon(cfg, driver, state, writer)
    d._pg_connected = True  # simulate connected
    await d._pull_once("T")
    snap = d.status_snapshot()
    assert snap["tables"][0]["status"] in ("degraded", "error")
    assert snap["tables"][0]["last_error"] == "simulated"


def test_core_does_not_import_postgres_driver() -> None:
    """Architectural firewall: core/ never imports drivers/postgres."""
    import importlib
    import pkgutil

    import r64_db_engine.core as core_pkg

    for mod_info in pkgutil.walk_packages(core_pkg.__path__, prefix="r64_db_engine.core."):
        mod = importlib.import_module(mod_info.name)
        for attr in dir(mod):
            obj = getattr(mod, attr, None)
            mod_name = getattr(obj, "__module__", "") or ""
            assert not mod_name.startswith("r64_db_engine.drivers.postgres"), (
                f"core module {mod_info.name} leaked a postgres dependency via {attr}"
            )
