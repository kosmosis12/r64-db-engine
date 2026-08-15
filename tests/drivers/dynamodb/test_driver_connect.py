from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError, NoCredentialsError

from r64_db_engine.core.driver import Driver
from r64_db_engine.drivers import resolve
from r64_db_engine.drivers.dynamodb import driver as driver_module
from r64_db_engine.drivers.dynamodb.driver import DynamoDBAuthError, DynamoDBDriver


def _session(monkeypatch: pytest.MonkeyPatch, client: MagicMock) -> MagicMock:
    session = MagicMock()
    session.client.return_value = client
    constructor = MagicMock(return_value=session)
    monkeypatch.setattr(driver_module.boto3, "Session", constructor)
    return constructor


async def test_connect_constructs_client_with_chain_endpoint_and_adaptive_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MagicMock()
    client.list_tables.return_value = {"TableNames": []}
    constructor = _session(monkeypatch, client)
    driver = DynamoDBDriver()
    await driver.connect(
        {
            "profile": "analytics",
            "region": "us-west-2",
            "endpoint_url": "http://localhost:8000",
            "consistent_read": True,
            "scan_segments": 4,
            "scan_page_limit": 25,
            "schema_sample_items": 50,
        }
    )

    constructor.assert_called_once_with(profile_name="analytics", region_name="us-west-2")
    kwargs = constructor.return_value.client.call_args.kwargs
    assert kwargs["endpoint_url"] == "http://localhost:8000"
    assert kwargs["config"].retries["mode"] == "adaptive"
    assert kwargs["config"].retries["max_attempts"] == 10
    client.list_tables.assert_called_once_with(Limit=1)


async def test_connect_uses_standard_chain_when_profile_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MagicMock()
    client.list_tables.return_value = {"TableNames": []}
    constructor = _session(monkeypatch, client)
    await DynamoDBDriver().connect({"region": "us-east-1"})
    constructor.assert_called_once_with(profile_name=None, region_name="us-east-1")


@pytest.mark.parametrize("error", [NoCredentialsError()])
async def test_connect_fails_fast_when_credentials_missing(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    client = MagicMock()
    client.list_tables.side_effect = error
    _session(monkeypatch, client)
    with pytest.raises(DynamoDBAuthError, match="credentials were not found"):
        await DynamoDBDriver().connect({})


@pytest.mark.parametrize("code", ["UnrecognizedClientException", "AccessDeniedException"])
async def test_connect_fails_fast_when_credentials_rejected(
    monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    client = MagicMock()
    client.list_tables.side_effect = ClientError(
        {"Error": {"Code": code, "Message": "denied"}}, "ListTables"
    )
    _session(monkeypatch, client)
    with pytest.raises(DynamoDBAuthError, match=code):
        await DynamoDBDriver().connect({})


async def test_connect_does_not_relabel_non_auth_client_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MagicMock()
    error = ClientError(
        {"Error": {"Code": "InternalServerError", "Message": "retry exhausted"}},
        "ListTables",
    )
    client.list_tables.side_effect = error
    _session(monkeypatch, client)
    with pytest.raises(ClientError) as raised:
        await DynamoDBDriver().connect({})
    assert raised.value is error


def test_registry_resolves_dynamodb_driver() -> None:
    assert resolve("dynamodb") is DynamoDBDriver


def test_bare_driver_satisfies_unchanged_abc() -> None:
    driver: Driver = DynamoDBDriver()
    assert isinstance(driver, Driver)
    assert not DynamoDBDriver.__abstractmethods__


@pytest.mark.parametrize("segments", [0, 33])
async def test_connect_rejects_invalid_segment_count(segments: int) -> None:
    with pytest.raises(ValueError, match="between 1 and 32"):
        await DynamoDBDriver().connect({"scan_segments": segments})
