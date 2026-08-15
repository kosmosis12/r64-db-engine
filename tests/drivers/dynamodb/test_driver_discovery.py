from __future__ import annotations

from unittest.mock import MagicMock

from botocore.exceptions import ClientError

from r64_db_engine.drivers.dynamodb.driver import DynamoDBDriver, DynamoDBTableMetadata


def _description(*, status: str = "ACTIVE", gsis: list[dict] | None = None) -> dict:
    table = {
        "TableName": "events",
        "TableStatus": status,
        "ItemCount": 12,
        "KeySchema": [
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "ts", "KeyType": "RANGE"},
        ],
        "AttributeDefinitions": [
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "ts", "AttributeType": "N"},
            {"AttributeName": "bucket", "AttributeType": "S"},
        ],
        "BillingModeSummary": {"BillingMode": "PAY_PER_REQUEST"},
    }
    if gsis is not None:
        table["GlobalSecondaryIndexes"] = gsis
    return {"Table": table}


def _driver(client: MagicMock, sample: int = 1000) -> DynamoDBDriver:
    driver = DynamoDBDriver()
    driver._client = client
    driver._schema_sample_items = sample
    return driver


async def test_discover_paginates_and_orders_keys_then_attributes() -> None:
    client = MagicMock()
    client.list_tables.side_effect = [
        {"TableNames": ["events"], "LastEvaluatedTableName": "events"},
        {"TableNames": []},
    ]
    client.describe_table.return_value = _description()
    client.scan.return_value = {
        "Items": [
            {"pk": {"S": "a"}, "ts": {"N": "1"}, "z": {"S": "x"}},
            {"pk": {"S": "b"}, "ts": {"N": "2.5"}, "a": {"BOOL": True}},
        ]
    }
    tables = await _driver(client).discover()
    table = tables[0]
    assert isinstance(table, DynamoDBTableMetadata)
    assert [column.name for column in table.columns] == ["pk", "ts", "a", "bucket", "z"]
    assert table.columns[1].pandas_dtype == "float64"
    assert table.status == "ACTIVE"
    assert table.billing_mode == "PAY_PER_REQUEST"
    assert table.estimated_rows == 12


async def test_discover_bounded_sample_paginates_to_exact_limit() -> None:
    client = MagicMock()
    client.describe_table.return_value = _description()
    client.scan.side_effect = [
        {"Items": [{"pk": {"S": "a"}}], "LastEvaluatedKey": {"pk": {"S": "a"}}},
        {"Items": [{"pk": {"S": "b"}}]},
    ]
    await _driver(client, sample=2).discover("events")
    assert client.scan.call_args_list[0].kwargs["Limit"] == 2
    assert client.scan.call_args_list[1].kwargs["Limit"] == 1


async def test_discover_widens_conflicting_type_and_logs_warning(caplog) -> None:
    client = MagicMock()
    client.describe_table.return_value = _description()
    client.scan.return_value = {
        "Items": [{"x": {"S": "one"}}, {"x": {"N": "2"}}]
    }
    table = (await _driver(client).discover("events"))[0]
    column = next(item for item in table.columns if item.name == "x")
    assert column.source_type == "mixed(N,S)"
    assert column.pandas_dtype == "string"
    assert "schema_type_conflict" in caplog.text


async def test_validate_requires_active_table_and_sampled_key() -> None:
    client = MagicMock()
    client.describe_table.return_value = _description(status="CREATING")
    client.scan.return_value = {"Items": [{"pk": {"S": "a"}}]}
    result = await _driver(client).validate_table(
        {"source": "events", "incremental_key": "missing"}
    )
    assert not result.ok
    assert any("not ACTIVE" in error for error in result.errors)
    assert any("not in sampled schema" in error for error in result.errors)


async def test_validate_reports_missing_table() -> None:
    client = MagicMock()
    client.describe_table.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "missing"}},
        "DescribeTable",
    )
    result = await _driver(client).validate_table({"source": "missing"})
    assert not result.ok
    assert result.errors == ["table missing does not exist"]


async def test_validate_gsi_query_requires_partition_value() -> None:
    client = MagicMock()
    client.describe_table.return_value = _description(
        gsis=[{
            "IndexName": "by_bucket_ts",
            "KeySchema": [
                {"AttributeName": "bucket", "KeyType": "HASH"},
                {"AttributeName": "ts", "KeyType": "RANGE"},
            ],
        }]
    )
    client.scan.return_value = {
        "Items": [{"pk": {"S": "a"}, "ts": {"N": "1"}, "bucket": {"S": "all"}}]
    }
    result = await _driver(client).validate_table(
        {
            "source": "events",
            "incremental_key": "ts",
            "incremental_mode": "gsi_query",
            "incremental_gsi": "by_bucket_ts",
        }
    )
    assert not result.ok
    assert any("incremental_gsi_partition_value" in error for error in result.errors)


async def test_validate_gsi_query_accepts_single_partition_shape() -> None:
    client = MagicMock()
    client.describe_table.return_value = _description(
        gsis=[{
            "IndexName": "by_bucket_ts",
            "KeySchema": [
                {"AttributeName": "bucket", "KeyType": "HASH"},
                {"AttributeName": "ts", "KeyType": "RANGE"},
            ],
        }]
    )
    client.scan.return_value = {
        "Items": [{"pk": {"S": "a"}, "ts": {"N": "1"}, "bucket": {"S": "all"}}]
    }
    result = await _driver(client).validate_table(
        {
            "source": "events",
            "incremental_key": "ts",
            "incremental_mode": "gsi_query",
            "incremental_gsi": "by_bucket_ts",
            "incremental_gsi_partition_value": "all",
        }
    )
    assert result.ok, result.errors
