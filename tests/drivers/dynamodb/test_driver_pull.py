from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from r64_db_engine.drivers.dynamodb.driver import DynamoDBDriver


def _description() -> dict:
    return {
        "Table": {
            "TableName": "events",
            "TableStatus": "ACTIVE",
            "KeySchema": [
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "ts", "KeyType": "RANGE"},
            ],
            "AttributeDefinitions": [
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "ts", "AttributeType": "N"},
                {"AttributeName": "bucket", "AttributeType": "S"},
            ],
            "GlobalSecondaryIndexes": [{
                "IndexName": "by_bucket_ts",
                "KeySchema": [
                    {"AttributeName": "bucket", "KeyType": "HASH"},
                    {"AttributeName": "ts", "KeyType": "RANGE"},
                ],
            }],
        }
    }


def _driver(client: MagicMock, *, segments: int = 1, limit: int | None = None):
    driver = DynamoDBDriver()
    driver._client = client
    driver._scan_segments = segments
    driver._scan_page_limit = limit
    return driver


async def test_full_refresh_scan_paginates_and_coerces_sparse_rows() -> None:
    client = MagicMock()
    client.describe_table.return_value = _description()
    client.scan.side_effect = [
        {
            "Items": [{"pk": {"S": "a"}, "ts": {"N": "1"}, "note": {"S": "em—dash"}}],
            "LastEvaluatedKey": {"pk": {"S": "a"}},
        },
        {"Items": [{"pk": {"S": "b"}, "ts": {"N": "2"}}]},
    ]
    result = await _driver(client, limit=1).pull(
        {"source": "events", "mode": "full_refresh"}, None
    )
    assert result.rows_pulled == 2
    assert result.new_watermark is None
    note = result.dataframe["note"]
    # A DynamoDB item is sparse: row "b" carries no `note` attribute at all.
    # Post sink-split, a missing attribute lands as a true SQL NULL on a pandas
    # nullable dtype -- NOT the empty string this test asserted in the pre-sink
    # era. Empty-string fill would make a missing attribute indistinguishable
    # from an attribute explicitly set to "", which is the exact fidelity loss
    # the null contract exists to prevent.
    assert note.dtype == "string"
    assert note[0] == "em?dash"
    assert note.isna().tolist() == [False, True]
    # Discriminator: count(*) vs count(col) must disagree, or NULL has collapsed
    # into a sentinel value somewhere in the coercion path.
    assert len(note) == 2
    assert note.notna().sum() == 1
    assert client.scan.call_args_list[1].kwargs["ExclusiveStartKey"] == {"pk": {"S": "a"}}


async def test_filter_scan_uses_strict_numeric_watermark() -> None:
    client = MagicMock()
    client.describe_table.return_value = _description()
    client.scan.return_value = {"Items": [{"pk": {"S": "b"}, "ts": {"N": "3"}}]}
    result = await _driver(client).pull(
        {
            "source": "events",
            "mode": "incremental",
            "incremental_key": "ts",
            "incremental_mode": "filter_scan",
        },
        2,
    )
    kwargs = client.scan.call_args.kwargs
    assert kwargs["FilterExpression"] == "#k > :watermark"
    assert kwargs["ExpressionAttributeValues"] == {":watermark": {"N": "2"}}
    assert result.new_watermark == 3


async def test_filter_scan_uses_strict_string_watermark() -> None:
    description = _description()
    description["Table"]["AttributeDefinitions"][1]["AttributeType"] = "S"
    client = MagicMock()
    client.describe_table.return_value = description
    client.scan.return_value = {
        "Items": [{"pk": {"S": "b"}, "ts": {"S": "2026-01-02"}}]
    }
    result = await _driver(client).pull(
        {"source": "events", "mode": "incremental", "incremental_key": "ts"},
        "2026-01-01",
    )
    assert client.scan.call_args.kwargs["ExpressionAttributeValues"] == {
        ":watermark": {"S": "2026-01-01"}
    }
    assert result.new_watermark == "2026-01-02"


async def test_parallel_scan_uses_all_segments() -> None:
    client = MagicMock()
    client.describe_table.return_value = _description()
    client.scan.side_effect = lambda **kwargs: {
        "Items": [{"pk": {"S": str(kwargs["Segment"])}, "ts": {"N": str(kwargs["Segment"])}}]
    }
    result = await _driver(client, segments=3).pull(
        {"source": "events", "mode": "full_refresh"}, None
    )
    assert result.rows_pulled == 3
    assert {call.kwargs["Segment"] for call in client.scan.call_args_list} == {0, 1, 2}
    assert all(call.kwargs["TotalSegments"] == 3 for call in client.scan.call_args_list)


async def test_gsi_query_builds_partition_and_watermark_condition() -> None:
    client = MagicMock()
    client.describe_table.return_value = _description()
    client.query.return_value = {
        "Items": [{"pk": {"S": "a"}, "bucket": {"S": "all"}, "ts": {"N": "11"}}]
    }
    result = await _driver(client).pull(
        {
            "source": "events",
            "mode": "incremental",
            "incremental_key": "ts",
            "incremental_mode": "gsi_query",
            "incremental_gsi": "by_bucket_ts",
            "incremental_gsi_partition_value": "all",
        },
        10,
    )
    kwargs = client.query.call_args.kwargs
    assert kwargs["KeyConditionExpression"] == (
        "#p = :partition AND #k > :watermark"
    )
    assert kwargs["ExpressionAttributeValues"] == {
        ":partition": {"S": "all"}, ":watermark": {"N": "10"}
    }
    assert result.new_watermark == 11


async def test_gsi_watermark_advances_only_to_max_returned_not_wall_clock() -> None:
    client = MagicMock()
    client.describe_table.return_value = _description()
    client.query.return_value = {
        "Items": [
            {"pk": {"S": "a"}, "ts": {"N": "101"}},
            {"pk": {"S": "b"}, "ts": {"N": "103"}},
        ]
    }
    result = await _driver(client).pull(
        {
            "source": "events",
            "mode": "incremental",
            "incremental_key": "ts",
            "incremental_mode": "gsi_query",
            "incremental_gsi": "by_bucket_ts",
            "incremental_gsi_partition_value": "all",
        },
        100,
    )
    assert result.new_watermark == 103


async def test_empty_incremental_result_preserves_previous_watermark() -> None:
    client = MagicMock()
    client.describe_table.return_value = _description()
    client.scan.return_value = {"Items": []}
    result = await _driver(client).pull(
        {"source": "events", "mode": "incremental", "incremental_key": "ts"}, 9
    )
    assert result.rows_pulled == 0
    assert result.new_watermark == 9


async def test_throttling_after_client_retries_propagates_cleanly() -> None:
    client = MagicMock()
    client.describe_table.return_value = _description()
    error = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "slow"}},
        "Scan",
    )
    client.scan.side_effect = error
    with pytest.raises(ClientError) as raised:
        await _driver(client).pull(
            {"source": "events", "mode": "full_refresh"}, None
        )
    assert raised.value is error
