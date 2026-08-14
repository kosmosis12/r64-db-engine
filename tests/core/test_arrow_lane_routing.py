"""Daemon routing between the Arrow-native lane and the DataFrame lane.

The lane is a CAPABILITY on the existing Driver/Sink ABCs, not a second stack.
These tests pin that a driver which knows nothing about Arrow keeps working
untouched, that an Arrow-capable driver is actually routed to the new path, and
that the fallback is never silent about which lane ran.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

pa = pytest.importorskip("pyarrow")
ipc = pytest.importorskip("pyarrow.ipc")

from r64_db_engine.core.config import Config  # noqa: E402
from r64_db_engine.core.daemon import Daemon  # noqa: E402
from r64_db_engine.core.driver import (  # noqa: E402
    ArrowPullResult,
    Driver,
    PullResult,
    ValidationResult,
)
from r64_db_engine.core.state import StateStore  # noqa: E402
from r64_db_engine.sinks.arrow_ipc import ArrowIpcSink  # noqa: E402
from r64_db_engine.sinks.ramdb import RamdbSink  # noqa: E402

ROWS = 120_000


class _PandasDriver(Driver):
    """A driver that has never heard of the Arrow lane. Must keep working."""

    def __init__(self) -> None:
        self.pull_calls = 0

    @classmethod
    def dialect_name(cls) -> str:
        return "fakedb"

    async def connect(self, config: dict[str, Any]) -> None:
        return None

    async def close(self) -> None:
        return None

    async def discover(self, schema_filter: str | None = None):
        return []

    async def validate_table(self, table_config: dict[str, Any]) -> ValidationResult:
        return ValidationResult(ok=True)

    async def pull(self, table_config, previous_watermark) -> PullResult:
        self.pull_calls += 1
        df = pd.DataFrame({"v": pd.Series(range(ROWS), dtype="int64")})
        return PullResult(dataframe=df, new_watermark=None, rows_pulled=ROWS, duration_ms=1)

    def coerce_value(self, value: Any, source_type: str) -> Any:
        return value


class _ArrowDriver(_PandasDriver):
    """Same driver, plus the capability. Nothing else changes."""

    def __init__(self, batch_rows: int = 20_000) -> None:
        super().__init__()
        self.batch_rows = batch_rows
        self.arrow_calls = 0

    def supports_arrow(self) -> bool:
        return True

    async def pull_arrow(self, table_config, previous_watermark) -> ArrowPullResult:
        self.arrow_calls += 1
        schema = pa.schema([("v", pa.int64())])
        batches = [
            pa.record_batch(
                [pa.array(range(i, min(i + self.batch_rows, ROWS)), type=pa.int64())],
                schema=schema,
            )
            for i in range(0, ROWS, self.batch_rows)
        ]
        return ArrowPullResult(
            reader=pa.RecordBatchReader.from_batches(schema, batches),
            new_watermark=None,
            duration_ms=1,
        )


class _LyingDriver(_PandasDriver):
    """Advertises the capability without implementing it. Must fail loudly."""

    def supports_arrow(self) -> bool:
        return True


def _daemon(tmp_path: Path, driver: Driver, sink, *, mode: str = "full_refresh") -> Daemon:
    out = tmp_path / "out"
    out.mkdir(parents=True, exist_ok=True)
    sink_type = type(sink).sink_name()
    options: dict[str, Any] = {"output_dir": str(out), "group": "G"}
    if sink_type == "ramdb":
        options = {"loading_dir": str(out), "group": "G"}
    config = Config.model_validate(
        {
            "dialect": "postgres",
            "postgres": {"database": "unused"},
            "row64": {"loading_dir": str(out), "group": "G"},
            "sink": {"type": sink_type, **options},
            "tables": [
                {
                    "source": "public.t",
                    "target": "T",
                    "mode": mode,
                    **({"incremental_key": "v"} if mode == "incremental" else {}),
                }
            ],
        }
    )
    sink.open(options)
    return Daemon(
        config=config,
        driver=driver,
        state=StateStore(str(tmp_path / "state")),
        writer=sink,
    )


def _artifact(tmp_path: Path) -> Path:
    return tmp_path / "out" / "G" / "T.arrow"


# --------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------


def test_arrow_driver_plus_streaming_sink_takes_the_arrow_lane(tmp_path: Path) -> None:
    driver = _ArrowDriver()
    daemon = _daemon(tmp_path, driver, ArrowIpcSink())

    assert daemon.uses_arrow_lane() is True
    asyncio.run(daemon.run(once=True))

    assert driver.arrow_calls == 1
    assert driver.pull_calls == 0  # the pandas path was never touched
    table = ipc.open_file(pa.memory_map(str(_artifact(tmp_path)))).read_all()
    assert table.num_rows == ROWS
    assert daemon.tables["T"].rows_pulled_last == ROWS
    assert daemon.tables["T"].status == "ok"


def test_pandas_driver_is_untouched_by_the_new_lane(tmp_path: Path) -> None:
    """A driver that never heard of Arrow keeps its exact previous behaviour."""
    driver = _PandasDriver()
    daemon = _daemon(tmp_path, driver, ArrowIpcSink())

    assert daemon.uses_arrow_lane() is False
    asyncio.run(daemon.run(once=True))

    assert driver.pull_calls == 1
    assert ipc.open_file(pa.memory_map(str(_artifact(tmp_path)))).read_all().num_rows == ROWS


def test_arrow_driver_falls_back_when_the_sink_cannot_stream(tmp_path: Path) -> None:
    """ramdb cannot stream, so an Arrow-capable driver still uses the df path.

    The fallback is a routing decision, not an error: the artifact must still
    be produced, by the lane that can produce it.
    """
    driver = _ArrowDriver()
    daemon = _daemon(tmp_path, driver, RamdbSink())

    assert daemon.uses_arrow_lane() is False
    asyncio.run(daemon.run(once=True))

    assert driver.pull_calls == 1
    assert driver.arrow_calls == 0


def test_status_reports_which_lane_is_in_use(tmp_path: Path) -> None:
    """An operator must be able to see the lane without reading the logs."""
    arrow = _daemon(tmp_path / "a", _ArrowDriver(), ArrowIpcSink())
    pandas_lane = _daemon(tmp_path / "b", _PandasDriver(), ArrowIpcSink())

    # Before connecting, the lane is unknown rather than guessed: a driver reads
    # its capability from config in connect(), so a value here would be fiction.
    assert arrow.status_snapshot()["source"]["lane"] is None

    asyncio.run(arrow.run(once=True))
    asyncio.run(pandas_lane.run(once=True))

    assert arrow.status_snapshot()["source"]["lane"] == "arrow"
    assert pandas_lane.status_snapshot()["source"]["lane"] == "dataframe"


def test_capability_without_implementation_fails_loudly(tmp_path: Path) -> None:
    """supports_arrow() without pull_arrow() must not silently fall back.

    A silent fallback would erase exactly the thing this lane is measured for:
    the benchmark would report pandas-lane numbers under an Arrow-lane label.
    """
    daemon = _daemon(tmp_path, _LyingDriver(), ArrowIpcSink())
    asyncio.run(daemon.run(once=True))

    assert daemon.tables["T"].status == "error"
    assert "does not implement the Arrow lane" in (daemon.tables["T"].last_error or "")


# --------------------------------------------------------------------------
# The full-refresh law, re-asserted on the lane
# --------------------------------------------------------------------------


def test_incremental_is_refused_on_the_arrow_lane(tmp_path: Path) -> None:
    """PG-011 class refusal. Incremental merging reads the previous artifact
    back and concatenates — which is precisely what streaming cannot do."""
    with pytest.raises(Exception, match="incremental"):
        _daemon(tmp_path, _ArrowDriver(), ArrowIpcSink(), mode="incremental")


def test_arrow_lane_refuses_incremental_even_if_the_sink_claims_appendable(
    tmp_path: Path,
) -> None:
    """The construction-time guard keys on the SINK; this one keys on the LANE.

    A future appendable streaming sink must not silently inherit a merge path
    that cannot serve it, so the refusal is re-asserted where the write happens.
    """

    class _AppendableArrowSink(ArrowIpcSink):
        def supports_incremental(self) -> bool:
            return True

    driver = _ArrowDriver()
    daemon = _daemon(tmp_path, driver, _AppendableArrowSink(), mode="incremental")
    assert daemon.uses_arrow_lane() is True

    asyncio.run(daemon.run(once=True))

    assert daemon.tables["T"].status == "error"
    assert "Arrow-native lane" in (daemon.tables["T"].last_error or "")


# --------------------------------------------------------------------------
# Source batch size must not leak into the artifact
# --------------------------------------------------------------------------


@pytest.mark.parametrize("batch_rows", [1_000, 20_000, 65_536, 200_000])
def test_artifact_block_structure_is_independent_of_driver_batch_size(
    tmp_path: Path, batch_rows: int
) -> None:
    """Whatever the driver hands over, the artifact looks the same.

    This is the daemon-level statement of the re-chunk requirement: block
    structure is a property of the ARTIFACT, not of the source's batching.
    """
    daemon = _daemon(tmp_path, _ArrowDriver(batch_rows=batch_rows), ArrowIpcSink())
    asyncio.run(daemon.run(once=True))

    reader = ipc.open_file(pa.memory_map(str(_artifact(tmp_path))))
    blocks = [reader.get_batch(i).num_rows for i in range(reader.num_record_batches)]
    assert blocks == [65_536, 54_464]
    assert reader.read_all().column("v").to_pylist() == list(range(ROWS))
