#!/usr/bin/env python3
"""Seed DynamoDB Local with deterministic Gate 3 fixtures.

Measured on the Gate 4 verification host with 50K rows and eight workers:
`-sharedDb` (SQLite writer) took 1429.86s (~35 rows/s); `-inMemory` took
4.78s (~10,460 rows/s). `scripts/dev_dynamodb.sh` therefore uses ephemeral
in-memory mode: fixtures are deliberately reseeded, so persistence has no
value and serializes the parallel writers.
"""

from __future__ import annotations

import argparse
import os
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import boto3

PRIMARY = "r64-ddb-primary"
STRING = "r64-ddb-string"
GUARDS = "r64-ddb-guards"
WIDE = "r64-ddb-wide-numbers"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint-url", default=os.getenv("DYNAMODB_ENDPOINT_URL"))
    parser.add_argument("--rows", type=int, default=50_000)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--start-row", type=int, default=0)
    parser.add_argument("--keep-tables", action="store_true")
    parser.add_argument("--repair", action="store_true")
    args = parser.parse_args()
    if not args.endpoint_url:
        parser.error(
            "DYNAMODB_ENDPOINT_URL is not set; source the env file emitted by "
            "scripts/dev_dynamodb.sh or pass --endpoint-url"
        )
    if args.workers < 1:
        parser.error("--workers must be positive")
    if args.start_row < 0:
        parser.error("--start-row must not be negative")
    if args.keep_tables and args.start_row == 0 and not args.repair:
        parser.error("--keep-tables requires a positive --start-row")
    if args.repair and not args.keep_tables:
        parser.error("--repair requires --keep-tables")
    resource = boto3.resource("dynamodb", endpoint_url=args.endpoint_url, region_name="us-west-2")
    client = resource.meta.client
    if args.keep_tables:
        string = resource.Table(STRING)
        guards = resource.Table(GUARDS)
        wide = resource.Table(WIDE)
    else:
        for name in (PRIMARY, STRING, GUARDS, WIDE):
            try:
                client.delete_table(TableName=name)
                client.get_waiter("table_not_exists").wait(TableName=name)
            except client.exceptions.ResourceNotFoundException:
                pass
        _create(resource, PRIMARY, "event_n", "N", "by_bucket_event_n")
        string = _create(resource, STRING, "event_s", "S", "by_bucket_event_s")
        guards = resource.create_table(
            TableName=GUARDS,
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        guards.wait_until_exists()
        wide = resource.create_table(
            TableName=WIDE,
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        wide.wait_until_exists()
    started = time.monotonic()
    existing = _primary_keys(client) if args.repair else set()

    def seed_range(start: int, stop: int) -> None:
        worker_resource = boto3.resource(
            "dynamodb", endpoint_url=args.endpoint_url, region_name="us-west-2"
        )
        with worker_resource.Table(PRIMARY).batch_writer() as batch:
            for i in range(start, stop):
                if i in existing:
                    continue
                item = _primary_item(i)
                batch.put_item(Item=item)

    workers = args.workers
    stop_row = args.start_row + args.rows
    chunk = (args.rows + workers - 1) // workers
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(seed_range, start, min(start + chunk, stop_row))
            for start in range(args.start_row, stop_row, chunk)
        ]
        for future in futures:
            future.result()
    with string.batch_writer() as batch:
        for i in range(8):
            batch.put_item(Item={
                "pk": f"string-{i}", "bucket": "all",
                "event_s": f"2026-01-01T00:00:{i:02d}Z", "value": i,
            })
    with guards.batch_writer() as batch:
        batch.put_item(Item={"pk": "int32-ok", "value": 2**31 - 1})
        batch.put_item(Item={"pk": "int32-over", "value": 2**31})
    with wide.batch_writer() as batch:
        batch.put_item(Item={"pk": "int64-ok", "value": 2**63 - 1})
        batch.put_item(Item={"pk": "int64-over", "value": Decimal(str(2**63))})
        batch.put_item(Item={"pk": "precision", "value": Decimal("12345678901234567890.123456789")})
    counts = {
        PRIMARY: _count(client, PRIMARY),
        STRING: _count(client, STRING),
        GUARDS: _count(client, GUARDS),
        WIDE: _count(client, WIDE),
    }
    expected = {PRIMARY: stop_row, STRING: 8, GUARDS: 2, WIDE: 3}
    if counts != expected:
        raise RuntimeError(f"fixture count verification failed: expected={expected}, actual={counts}")
    print(
        f"[seed_dynamodb] rows [{args.start_row}, {stop_row}) seeded with {workers} worker(s) "
        f"in {time.monotonic() - started:.2f}s; counts={counts}",
        flush=True,
    )
    return 0


def _primary_item(i: int) -> dict:
    item = {
        "pk": f"event-{i:06d}", "bucket": "all", "event_n": i,
    }
    if i % 2 == 0:
        item["sparse"] = f"present-{i}"
    if i in {0, 1, 49_998, 49_999}:
        item.update(
            {
                "text": "vehicle—event",
                "flag": i % 2 == 0,
                "nothing": None,
                "binary": bytes([i % 256, (i + 1) % 256]),
                "map": {"a": Decimal(i), "nested": {"ok": True}},
                "list": [Decimal(i), ["nested", Decimal("3.125")]],
                "strings": {"z", "a"},
                "numbers": {Decimal("10"), Decimal("2")},
                "binaries": {b"\xff", b"\x01"},
            }
        )
    return item


def _count(client, table_name: str) -> int:
    total = 0
    kwargs = {"TableName": table_name, "Select": "COUNT"}
    while True:
        page = client.scan(**kwargs)
        total += page["Count"]
        last = page.get("LastEvaluatedKey")
        if not last:
            return total
        kwargs["ExclusiveStartKey"] = last


def _primary_keys(client) -> set[int]:
    keys: set[int] = set()
    kwargs = {"TableName": PRIMARY, "ProjectionExpression": "event_n"}
    while True:
        page = client.scan(**kwargs)
        keys.update(int(item["event_n"]) for item in page["Items"])
        last = page.get("LastEvaluatedKey")
        if not last:
            return keys
        kwargs["ExclusiveStartKey"] = last


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
