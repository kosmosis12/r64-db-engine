#!/usr/bin/env python3
"""Seed DynamoDB Local with deterministic Gate 3 fixtures."""

from __future__ import annotations

import argparse
import os
import time
from decimal import Decimal

import boto3

PRIMARY = "r64-ddb-primary"
STRING = "r64-ddb-string"
GUARDS = "r64-ddb-guards"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint-url", default=os.getenv("DYNAMODB_ENDPOINT_URL", "http://localhost:8000"))
    parser.add_argument("--rows", type=int, default=50_000)
    args = parser.parse_args()
    resource = boto3.resource("dynamodb", endpoint_url=args.endpoint_url, region_name="us-west-2")
    client = resource.meta.client
    for name in (PRIMARY, STRING, GUARDS):
        try:
            client.delete_table(TableName=name)
            client.get_waiter("table_not_exists").wait(TableName=name)
        except client.exceptions.ResourceNotFoundException:
            pass
    primary = _create(resource, PRIMARY, "event_n", "N", "by_bucket_event_n")
    string = _create(resource, STRING, "event_s", "S", "by_bucket_event_s")
    guards = resource.create_table(
        TableName=GUARDS,
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    guards.wait_until_exists()
    started = time.monotonic()
    with primary.batch_writer() as batch:
        for i in range(args.rows):
            item = {
                "pk": f"event-{i:06d}", "bucket": "all", "event_n": i,
                "text": "vehicle—event", "flag": i % 2 == 0, "nothing": None,
                "binary": bytes([i % 256, (i + 1) % 256]),
                "map": {"a": Decimal(i), "nested": {"ok": True}},
                "list": [Decimal(i), ["nested", Decimal("3.125")]],
                "strings": {"z", "a"}, "numbers": {Decimal("10"), Decimal("2")},
                "binaries": {b"\xff", b"\x01"},
            }
            if i % 2 == 0:
                item["sparse"] = f"present-{i}"
            batch.put_item(Item=item)
    with string.batch_writer() as batch:
        for i in range(8):
            batch.put_item(Item={
                "pk": f"string-{i}", "bucket": "all",
                "event_s": f"2026-01-01T00:00:{i:02d}Z", "value": i,
            })
    with guards.batch_writer() as batch:
        batch.put_item(Item={"pk": "int32-ok", "value": 2**31 - 1})
        batch.put_item(Item={"pk": "int32-over", "value": 2**31})
        batch.put_item(Item={"pk": "int64-ok", "value": 2**63 - 1})
        batch.put_item(Item={"pk": "int64-over", "value": Decimal(str(2**63))})
        batch.put_item(Item={"pk": "precision", "value": Decimal("12345678901234567890.123456789")})
    print(f"[seed_dynamodb] {args.rows} primary rows seeded in {time.monotonic() - started:.2f}s")
    return 0


def _create(resource, name: str, sort_name: str, sort_type: str, index_name: str):
    table = resource.create_table(
        TableName=name,
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "bucket", "AttributeType": "S"},
            {"AttributeName": sort_name, "AttributeType": sort_type},
        ],
        GlobalSecondaryIndexes=[{
            "IndexName": index_name,
            "KeySchema": [
                {"AttributeName": "bucket", "KeyType": "HASH"},
                {"AttributeName": sort_name, "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        }],
        BillingMode="PAY_PER_REQUEST",
    )
    table.wait_until_exists()
    return table


if __name__ == "__main__":
    raise SystemExit(main())
