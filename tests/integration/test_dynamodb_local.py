"""Gate 3 proofs against real DynamoDB Local and real row64tools."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from importlib.metadata import version
from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError
from row64tools.ramdb import load_to_df

from r64_db_engine.core.ramdb_writer import RamdbWriter, Row64CodecOverflowError
from r64_db_engine.core.state import StateStore
from r64_db_engine.drivers.dynamodb.driver import DynamoDBDriver

pytestmark = pytest.mark.integration
ENDPOINT = os.getenv("DYNAMODB_ENDPOINT_URL", "http://localhost:8010")
PRIMARY = "r64-ddb-primary"
STRING = "r64-ddb-string"
GUARDS = "r64-ddb-guards"


@pytest.fixture(scope="module")
def client():
    return boto3.client("dynamodb", endpoint_url=ENDPOINT, region_name="us-west-2")


@pytest.fixture
async def driver() -> DynamoDBDriver:
    instance = DynamoDBDriver()
    await instance.connect({"region": "us-west-2", "endpoint_url": ENDPOINT, "scan_page_limit": 1000})
    yield instance
    await instance.close()


def _cfg(source: str, **kwargs: Any) -> dict[str, Any]:
    return {"source": source, "mode": "full_refresh", "ascii_sanitize": True, **kwargs}


async def test_full_refresh_50k_real_ramdb_roundtrip_records_wall_clock(
    driver: DynamoDBDriver, tmp_path: Path
) -> None:
    started = time.perf_counter()
    result = await driver.pull(_cfg(PRIMARY), None)
    loading = tmp_path / "loading"
    loading.mkdir()
    path = RamdbWriter(loading, "DDB").write(result.dataframe, "Primary")
    loaded = load_to_df(str(path))
    elapsed = time.perf_counter() - started
    assert result.rows_pulled == 50_000
    assert len(loaded) == 50_000
    assert loaded["pk"].nunique() == 50_000
    print(f"DYNAMODB_50K_E2E_SECONDS={elapsed:.6f}")
    print(f"DYNAMODB_50K_E2E_ROWS_PER_SECOND={50_000 / elapsed:.3f}")


@pytest.mark.parametrize(
    ("table", "key", "watermark", "incremental_mode", "gsi", "partition", "expected"),
    [
        (PRIMARY, "event_n", 49_997, "filter_scan", None, None, {49_998, 49_999}),
        (PRIMARY, "event_n", 49_997, "gsi_query", "by_bucket_event_n", "all", {49_998, 49_999}),
        (STRING, "event_s", "2026-01-01T00:00:05Z", "filter_scan", None, None,
         {"2026-01-01T00:00:06Z", "2026-01-01T00:00:07Z"}),
        (STRING, "event_s", "2026-01-01T00:00:05Z", "gsi_query", "by_bucket_event_s", "all",
         {"2026-01-01T00:00:06Z", "2026-01-01T00:00:07Z"}),
    ],
)
async def test_equal_watermark_boundary_two_consecutive_pulls_real_ramdb(
    driver: DynamoDBDriver,
    tmp_path: Path,
    table: str,
    key: str,
    watermark: str | int,
    incremental_mode: str,
    gsi: str | None,
    partition: str | None,
    expected: set,
) -> None:
    config = _cfg(
        table, mode="incremental", incremental_key=key,
        incremental_mode=incremental_mode, incremental_gsi=gsi,
        incremental_gsi_partition_value=partition,
    )
    first = await driver.pull(config, watermark)
    loading = tmp_path / "loading"
    loading.mkdir()
    writer = RamdbWriter(loading, "DDB")
    loaded = load_to_df(str(writer.write(first.dataframe, "Boundary")))
    second = await driver.pull(config, first.new_watermark)
    assert set(loaded[key]) == expected
    assert second.rows_pulled == 0
    assert second.new_watermark == first.new_watermark


async def test_gsi_returned_max_watermark_real_gsi(driver: DynamoDBDriver) -> None:
    result = await driver.pull(
        _cfg(
            STRING, mode="incremental", incremental_key="event_s",
            incremental_mode="gsi_query", incremental_gsi="by_bucket_event_s",
            incremental_gsi_partition_value="all",
        ),
        "2026-01-01T00:00:03Z",
    )
    assert result.new_watermark == result.dataframe["event_s"].max()
    assert result.new_watermark == "2026-01-01T00:00:07Z"


def test_sigterm_mid_real_write_leaves_no_tmp_file(tmp_path: Path) -> None:
    loading = tmp_path / "loading"
    loading.mkdir()
    code = """
import sys
import pandas as pd
from r64_db_engine.core.ramdb_writer import RamdbWriter
df = pd.DataFrame({'id': range(2000000), 'value': ['x' * 64] * 2000000})
RamdbWriter(sys.argv[1], 'DDB').write(df, 'Signal')
"""
    process = subprocess.Popen([sys.executable, "-c", code, str(loading)])
    target_dir = loading / "DDB"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not list(target_dir.glob(".Signal.ramdb.tmp.*")):
        time.sleep(0.005)
    assert list(target_dir.glob(".Signal.ramdb.tmp.*")), "writer never exposed a temp path"
    process.send_signal(signal.SIGTERM)
    process.wait(timeout=30)
    assert not list(target_dir.glob("*.tmp.*"))
    assert not (target_dir / "Signal.ramdb").exists()


async def test_deleted_state_recovery_overwrites_without_concatenation(
    driver: DynamoDBDriver, tmp_path: Path
) -> None:
    result = await driver.pull(_cfg(STRING), None)
    loading = tmp_path / "loading"
    loading.mkdir()
    writer = RamdbWriter(loading, "DDB")
    state_path = tmp_path / "state" / "state.db"
    StateStore(state_path).set_watermark("String", "2026-01-01T00:00:07Z", "timestamp", 8, 1)
    first = load_to_df(str(writer.write(result.dataframe, "String")))
    state_path.unlink()
    StateStore(state_path)
    second = load_to_df(str(writer.write((await driver.pull(_cfg(STRING), None)).dataframe, "String")))
    assert len(first) == len(second) == 8
    assert second["pk"].nunique() == 8


async def test_mid_pull_failure_preserves_state_and_no_partial_ramdb(
    driver: DynamoDBDriver, tmp_path: Path
) -> None:
    state = StateStore(tmp_path / "state" / "state.db")
    state.set_watermark("Primary", 123, "int", 1, 1)
    original = driver._client
    calls = 0

    class FailingClient:
        def __getattr__(self, name: str):
            nonlocal calls
            method = getattr(original, name)
            if name != "scan":
                return method

            def scan(**kwargs):
                nonlocal calls
                calls += 1
                if calls > 1:
                    raise ClientError(
                        {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "injected"}},
                        "Scan",
                    )
                return method(**kwargs)

            return scan

    driver._client = FailingClient()
    with pytest.raises(ClientError, match="ProvisionedThroughputExceededException"):
        await driver.pull(_cfg(PRIMARY), None)
    driver._client = original
    assert state.get_watermark("Primary")[0] == 123
    assert not list(tmp_path.rglob("*.ramdb"))
    assert not list(tmp_path.rglob("*.tmp.*"))


async def test_sparse_json_sets_binary_are_stable_real_roundtrip(
    driver: DynamoDBDriver, tmp_path: Path
) -> None:
    config = _cfg(PRIMARY, mode="incremental", incremental_key="event_n")
    first = await driver.pull(config, 49_997)
    second = await driver.pull(config, 49_997)
    loading = tmp_path / "loading"
    loading.mkdir()
    loaded = load_to_df(str(RamdbWriter(loading, "DDB").write(first.dataframe, "Types")))
    assert loaded["sparse"].tolist() == ["present-49998", ""]
    for column in ("map", "list", "strings", "numbers", "binaries"):
        assert first.dataframe[column].tolist() == second.dataframe[column].tolist()
        assert [json.loads(value) for value in loaded[column]]
    assert loaded["binary"].tolist() == ["4e4f", "4f50"]
    assert [json.loads(value) for value in loaded["binaries"]] == [["01", "ff"], ["01", "ff"]]


async def test_codec_overflow_real_row64tools_writes_nothing(
    driver: DynamoDBDriver, tmp_path: Path
) -> None:
    result = await driver.pull(_cfg(GUARDS), None)
    loading = tmp_path / "loading"
    loading.mkdir()
    with pytest.raises(Row64CodecOverflowError, match="outside signed int32 range"):
        RamdbWriter(loading, "DDB").write(result.dataframe, "Guards")
    assert version("row64tools") == "1.0.11"
    assert not list(loading.rglob("*.ramdb"))
    assert not list(loading.rglob("*.tmp.*"))
