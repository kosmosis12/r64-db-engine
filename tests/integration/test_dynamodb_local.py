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
import pandas as pd
import pytest
from botocore.exceptions import ClientError
from pandas.testing import assert_frame_equal
from row64tools.ramdb import load_to_df

from r64_db_engine.core import daemon as daemon_mod
from r64_db_engine.core.config import Config
from r64_db_engine.core.daemon import Daemon
from r64_db_engine.core.ramdb_writer import RamdbWriter, Row64CodecOverflowError
from r64_db_engine.core.state import StateStore
from r64_db_engine.drivers.dynamodb.driver import DynamoDBDriver

pytestmark = pytest.mark.integration
PRIMARY = "r64-ddb-primary"
STRING = "r64-ddb-string"
GUARDS = "r64-ddb-guards"
ROW64TOOLS_VERSION = "1.0.11"


def _endpoint() -> str:
    endpoint = os.getenv("DYNAMODB_ENDPOINT_URL")
    if not endpoint:
        pytest.fail(
            "DYNAMODB_ENDPOINT_URL is not set; source the env file emitted by "
            "scripts/dev_dynamodb.sh"
        )
    return endpoint


@pytest.fixture(scope="module")
def client():
    return boto3.client("dynamodb", endpoint_url=_endpoint(), region_name="us-west-2")


@pytest.fixture(scope="module", autouse=True)
def seeded_fixture_counts(client: Any) -> None:
    expected = {PRIMARY: 50_000, STRING: 8, GUARDS: 2}
    actual = {
        table: client.describe_table(TableName=table)["Table"]["ItemCount"]
        for table in expected
    }
    assert actual == expected, (
        "DynamoDB Local fixtures are incomplete; rerun scripts/seed_dynamodb.py: "
        f"expected={expected}, actual={actual}"
    )


@pytest.fixture
async def driver() -> DynamoDBDriver:
    instance = DynamoDBDriver()
    await instance.connect(
        {"region": "us-west-2", "endpoint_url": _endpoint(), "scan_page_limit": 1000}
    )
    yield instance
    await instance.close()


def _cfg(source: str, **kwargs: Any) -> dict[str, Any]:
    return {"source": source, "mode": "full_refresh", "ascii_sanitize": True, **kwargs}


def _daemon_config(
    tmp_path: Path, source: str, target: str, *, incremental_key: str
) -> Config:
    loading = tmp_path / "loading"
    loading.mkdir()
    return Config.model_validate(
        {
            "dialect": "postgres",
            "postgres": {"database": "unused-config-vessel"},
            "row64": {"loading_dir": str(loading), "group": "DDB"},
            "tables": [
                {
                    "source": source,
                    "target": target,
                    "mode": "incremental",
                    "incremental_key": incremental_key,
                    "incremental_type": "int" if incremental_key == "event_n" else "timestamp",
                }
            ],
            "runtime": {"state_dir": str(tmp_path / "state")},
            "telemetry": {"health_port": 0, "metrics_port": 0},
        }
    )


def _real_daemon(
    config: Config, driver: DynamoDBDriver
) -> tuple[Daemon, StateStore, RamdbWriter]:
    state = StateStore(Path(config.runtime.state_dir) / "state.db")
    writer = RamdbWriter(config.row64.loading_dir, config.row64.group)
    return Daemon(config, driver, state, writer), state, writer


def _expected_primary_frame(rows: int = 50_000) -> pd.DataFrame:
    records: list[dict[str, Any]] = []
    for i in range(rows):
        rich = i in {0, 1, 49_998, 49_999}
        records.append(
            {
                "pk": f"event-{i:06d}",
                "binaries": '["01","ff"]' if rich else "",
                "binary": f"{i % 256:02x}{(i + 1) % 256:02x}" if rich else "",
                "bucket": "all",
                "event_n": i,
                "flag": i % 2 == 0 if rich else False,
                "list": json.dumps([i, ["nested", 3.125]], separators=(",", ":"))
                if rich
                else "",
                "map": (
                    json.dumps(
                        {"a": i, "nested": {"ok": True}},
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    if rich
                    else ""
                ),
                "nothing": "",
                "numbers": "[10,2]" if rich else "",
                "sparse": f"present-{i}" if i % 2 == 0 else "",
                "strings": '["a","z"]' if rich else "",
                "text": "vehicle?event" if rich else "",
            }
        )
    return pd.DataFrame.from_records(records)


async def test_full_refresh_50k_byte_correct_real_ramdb_roundtrip_wall_clock(
    driver: DynamoDBDriver, tmp_path: Path
) -> None:
    started = time.perf_counter()
    result = await driver.pull(_cfg(PRIMARY), None)
    loading = tmp_path / "loading"
    loading.mkdir()
    path = RamdbWriter(loading, "DDB").write(result.dataframe, "Primary")
    loaded = load_to_df(str(path)).sort_values("pk").reset_index(drop=True)
    expected = _expected_primary_frame()
    assert_frame_equal(loaded, expected, check_dtype=False, check_like=False)
    elapsed = time.perf_counter() - started
    assert result.rows_pulled == 50_000
    print(f"DYNAMODB_50K_E2E_SECONDS={elapsed:.6f}")
    print(f"DYNAMODB_50K_E2E_ROWS_PER_SECOND={50_000 / elapsed:.3f}")


@pytest.mark.parametrize(
    ("table", "key", "watermark", "incremental_mode", "gsi", "partition", "expected"),
    [
        (PRIMARY, "event_n", 49_997, "filter_scan", None, None, {49_998, 49_999}),
        (PRIMARY, "event_n", 49_997, "gsi_query", "by_bucket_event_n", "all", {49_998, 49_999}),
        (
            STRING,
            "event_s",
            "2026-01-01T00:00:05Z",
            "filter_scan",
            None,
            None,
            {"2026-01-01T00:00:06Z", "2026-01-01T00:00:07Z"},
        ),
        (
            STRING,
            "event_s",
            "2026-01-01T00:00:05Z",
            "gsi_query",
            "by_bucket_event_s",
            "all",
            {"2026-01-01T00:00:06Z", "2026-01-01T00:00:07Z"},
        ),
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
    expected: set[Any],
) -> None:
    config = _cfg(
        table,
        mode="incremental",
        incremental_key=key,
        incremental_mode=incremental_mode,
        incremental_gsi=gsi,
        incremental_gsi_partition_value=partition,
    )
    first = await driver.pull(config, watermark)
    loading = tmp_path / "loading"
    loading.mkdir()
    loaded = load_to_df(str(RamdbWriter(loading, "DDB").write(first.dataframe, "Boundary")))
    second = await driver.pull(config, first.new_watermark)
    assert set(loaded[key]) == expected
    assert second.rows_pulled == 0
    assert second.new_watermark == first.new_watermark


async def test_gsi_returned_max_watermark_real_gsi(driver: DynamoDBDriver) -> None:
    result = await driver.pull(
        _cfg(
            STRING,
            mode="incremental",
            incremental_key="event_s",
            incremental_mode="gsi_query",
            incremental_gsi="by_bucket_event_s",
            incremental_gsi_partition_value="all",
        ),
        "2026-01-01T00:00:03Z",
    )
    assert result.new_watermark == result.dataframe["event_s"].max()
    assert result.new_watermark == "2026-01-01T00:00:07Z"


def test_sigterm_during_real_row64tools_write_leaves_no_tmp_file(tmp_path: Path) -> None:
    loading = tmp_path / "loading"
    loading.mkdir()
    entered = tmp_path / "serializer-entered"
    code = """
import sys
from pathlib import Path
import pandas as pd
from row64tools.ramdb import save_from_df
from r64_db_engine.core import ramdb_writer

def observed_real_save(df, path):
    Path(sys.argv[2]).touch()
    save_from_df(df, str(path))

ramdb_writer._save_ramdb = observed_real_save
df = pd.DataFrame({'id': range(3000000), 'value': ['x' * 64] * 3000000})
ramdb_writer.RamdbWriter(sys.argv[1], 'DDB').write(df, 'Signal')
"""
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(loading), str(entered)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    target_dir = loading / "DDB"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if entered.exists() and list(target_dir.glob(".Signal.ramdb.tmp.*")):
            break
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            pytest.fail(f"writer exited before SIGTERM hook: stdout={stdout!r} stderr={stderr!r}")
        time.sleep(0.005)
    else:
        process.kill()
        process.wait(timeout=10)
        pytest.fail("real row64tools serializer did not enter within 30 seconds")
    process.send_signal(signal.SIGTERM)
    process.wait(timeout=30)
    assert process.returncode == 128 + signal.SIGTERM
    assert not list(target_dir.glob(".Signal.ramdb.tmp.*"))
    assert not (target_dir / "Signal.ramdb").exists()


async def test_deleted_real_state_recovery_overwrites_real_ramdb_without_duplication(
    driver: DynamoDBDriver, tmp_path: Path
) -> None:
    config = _daemon_config(tmp_path, STRING, "String", incremental_key="event_s")
    daemon, state, writer = _real_daemon(config, driver)
    await daemon._pull_once("String")
    state_path = state.path
    assert state.get_watermark("String")[0] == "2026-01-01T00:00:07Z"
    first = load_to_df(str(writer.target_path("String")))
    first_bytes = writer.target_path("String").read_bytes()

    state_path.unlink()
    daemon.state = StateStore(state_path)
    await daemon._pull_once("String")

    second = load_to_df(str(writer.target_path("String")))
    assert len(first) == len(second) == 8
    assert second["pk"].nunique() == 8
    assert writer.target_path("String").read_bytes() == first_bytes
    assert daemon.state.get_watermark("String")[0] == "2026-01-01T00:00:07Z"


async def test_client_boundary_mid_pagination_fault_preserves_real_state_and_ramdb(
    driver: DynamoDBDriver, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _daemon_config(tmp_path, PRIMARY, "Primary", incremental_key="event_n")
    daemon, state, writer = _real_daemon(config, driver)
    await daemon._pull_once("Primary")
    assert state.get_watermark("Primary")[0] == 49_999
    output_before = writer.target_path("Primary").read_bytes()
    original = driver._client
    calls = 0

    class FailingClient:
        def __getattr__(self, name: str):
            method = getattr(original, name)
            if name != "scan":
                return method

            def scan(**kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise ClientError(
                        {
                            "Error": {
                                "Code": "ProvisionedThroughputExceededException",
                                "Message": "injected after a real first page",
                            }
                        },
                        "Scan",
                    )
                return method(**kwargs)

            return scan

    driver._client = FailingClient()
    monkeypatch.setattr(daemon_mod, "_RETRY_DELAYS", ())
    try:
        await daemon._pull_once("Primary")
    finally:
        driver._client = original

    assert calls == 2
    assert state.get_watermark("Primary")[0] == 49_999
    assert writer.target_path("Primary").read_bytes() == output_before
    assert not list(Path(config.row64.loading_dir).rglob(".Primary.ramdb.tmp.*"))
    assert daemon.tables["Primary"].status == "degraded"


async def test_sparse_nan_fill_json_sets_and_binary_are_deterministic(
    driver: DynamoDBDriver, tmp_path: Path, client: Any
) -> None:
    raw = [
        client.get_item(TableName=PRIMARY, Key={"pk": {"S": f"event-{i:06d}"}})["Item"]
        for i in (49_998, 49_999)
    ]
    raw_presence = {int(item["event_n"]["N"]): "sparse" in item for item in raw}
    sparse_input = pd.Series(
        [item.get("sparse", {}).get("S") for item in sorted(raw, key=lambda x: int(x["event_n"]["N"]))]
    )
    assert raw_presence == {49_998: True, 49_999: False}
    assert pd.isna(sparse_input.iloc[1])

    config = _cfg(PRIMARY, mode="incremental", incremental_key="event_n")
    first = await driver.pull(config, 49_997)
    second = await driver.pull(config, 49_997)
    first.dataframe.sort_values("event_n", inplace=True, ignore_index=True)
    second.dataframe.sort_values("event_n", inplace=True, ignore_index=True)
    assert first.dataframe["sparse"].tolist() == ["present-49998", ""]
    for column in ("map", "list", "strings", "numbers", "binaries"):
        assert first.dataframe[column].tolist() == second.dataframe[column].tolist()
        assert [value.encode() for value in first.dataframe[column]] == [
            value.encode() for value in second.dataframe[column]
        ]

    expected = {
        "map": [{"a": 49_998, "nested": {"ok": True}}, {"a": 49_999, "nested": {"ok": True}}],
        "list": [[49_998, ["nested", 3.125]], [49_999, ["nested", 3.125]]],
        "strings": [["a", "z"], ["a", "z"]],
        "numbers": [[10, 2], [10, 2]],
        "binaries": [["01", "ff"], ["01", "ff"]],
    }
    for column, values in expected.items():
        assert [json.loads(value) for value in first.dataframe[column]] == values

    loading = tmp_path / "loading"
    loading.mkdir()
    loaded = load_to_df(str(RamdbWriter(loading, "DDB").write(first.dataframe, "Types")))
    loaded.sort_values("event_n", inplace=True, ignore_index=True)
    assert loaded["sparse"].tolist() == ["present-49998", ""]
    for column, values in expected.items():
        assert [json.loads(value) for value in loaded[column]] == values
    assert loaded["binary"].tolist() == ["4e4f", "4f50"]


async def test_codec_overflow_real_row64tools_version_writes_nothing(
    driver: DynamoDBDriver, tmp_path: Path
) -> None:
    result = await driver.pull(_cfg(GUARDS), None)
    loading = tmp_path / "loading"
    loading.mkdir()
    with pytest.raises(Row64CodecOverflowError, match="outside signed int32 range"):
        RamdbWriter(loading, "DDB").write(result.dataframe, "Guards")
    assert version("row64tools") == ROW64TOOLS_VERSION
    assert not list(loading.rglob("*.ramdb"))
    assert not list(loading.rglob(".*.ramdb.tmp.*"))
