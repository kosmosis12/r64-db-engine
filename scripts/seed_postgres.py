#!/usr/bin/env python3
"""Seed the dev Postgres with realistic type variety from SPEC §6.1.

Creates five tables under the `public` schema, each hitting at least one
of the coercion categories r64-db-engine has to handle correctly:

  - public.customers     bigint, text (with em-dashes), text, timestamptz
  - public.orders        bigint, numeric(20,5), jsonb, timestamptz
  - public.events        bigint, text, jsonb, bytea, timestamptz
  - public.measurements  bigint, double precision[], interval, timestamptz
  - public.identifiers   uuid, text, timestamptz

Each table gets 50,000 rows by default (override with --rows). Inserts
are batched with executemany inside a single transaction per table so a
fresh seed runs in well under a minute on a laptop.

The connection defaults match `scripts/dev_postgres.sh`:
    host=localhost  port=5433  user=postgres  password=row64dev  db=analytics

Idempotent: drops and re-creates the tables on every run so re-seeding
gives a known dataset.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import string
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta

import psycopg

_BATCH = 5_000
_RNG_SEED = 42

_REGIONS = ["us-west", "us-east", "eu-west", "eu-central", "apac", "latam"]
_PLANS = ["free", "pro", "team", "enterprise"]
_EVENT_TYPES = ["page_view", "click", "signup", "purchase", "logout", "error"]
# Em-dashes, smart quotes, accented chars — exercises ascii_sanitize.
_NOTE_TEMPLATES = [
    "Premium customer — renewed 2025",
    "Flagged for review—high churn risk",
    "Standard tier · upgrade pending",
    "“VIP” — see comms thread",
    "café-owner segment, EMEA",
    "n/a",
]


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    rng = random.Random(_RNG_SEED)

    conninfo = (
        f"host={args.host} port={args.port} dbname={args.database} "
        f"user={args.user} password={args.password}"
    )

    print(f"[seed] connecting to {args.host}:{args.port}/{args.database} as {args.user}")
    started = time.monotonic()

    with psycopg.connect(conninfo, autocommit=False) as conn:
        _ensure_extensions(conn)
        _seed_customers(conn, args.rows, rng)
        _seed_orders(conn, args.rows, rng)
        _seed_events(conn, args.rows, rng)
        _seed_measurements(conn, args.rows, rng)
        _seed_identifiers(conn, args.rows, rng)

    elapsed = time.monotonic() - started
    print(f"[seed] done in {elapsed:.1f}s — {args.rows} rows × 5 tables")
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--host", default=os.environ.get("PG_HOST", "localhost"))
    try:
        _default_port = int(os.environ.get("PG_PORT", "5433"))
    except ValueError:
        print(f"warning: PG_PORT={os.environ['PG_PORT']!r} is not an integer, falling back to 5433", file=sys.stderr)
        _default_port = 5433
    p.add_argument("--port", type=int, default=_default_port)
    p.add_argument("--database", default=os.environ.get("PG_DATABASE", "analytics"))
    p.add_argument("--user", default=os.environ.get("PG_USER", "postgres"))
    p.add_argument(
        "--password",
        default=os.environ.get("PG_PASSWORD") or os.environ.get("PGPASSWORD", "row64dev"),
    )
    p.add_argument("--rows", type=int, default=50_000, help="rows per table (default 50000)")
    return p.parse_args(argv)


def _ensure_extensions(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS \"uuid-ossp\"")
    conn.commit()


# ---- per-table seeders ---------------------------------------------------


def _seed_customers(conn: psycopg.Connection, n: int, rng: random.Random) -> None:
    print(f"[seed] customers ({n} rows) — bigint, text-with-em-dashes, timestamptz")
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS public.customers")
        cur.execute(
            """
            CREATE TABLE public.customers (
                id          BIGINT PRIMARY KEY,
                name        TEXT NOT NULL,
                region      TEXT NOT NULL,
                plan        TEXT NOT NULL,
                notes       TEXT,
                updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        base = _now() - timedelta(days=180)

        def gen():
            for i in range(1, n + 1):
                yield (
                    i,
                    f"Customer {i:06d} {_random_word(rng, 6)}",
                    rng.choice(_REGIONS),
                    rng.choice(_PLANS),
                    rng.choice(_NOTE_TEMPLATES),
                    base + timedelta(seconds=rng.randint(0, 180 * 86_400)),
                )

        _batched_insert(
            cur,
            "INSERT INTO public.customers VALUES (%s, %s, %s, %s, %s, %s)",
            gen(),
            n,
        )
    conn.commit()


def _seed_orders(conn: psycopg.Connection, n: int, rng: random.Random) -> None:
    print(f"[seed] orders ({n} rows) — bigint, numeric(20,5), jsonb, timestamptz")
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS public.orders")
        cur.execute(
            """
            CREATE TABLE public.orders (
                id           BIGINT PRIMARY KEY,
                customer_id  BIGINT NOT NULL,
                amount       NUMERIC(20, 5) NOT NULL,
                currency     TEXT NOT NULL,
                metadata     JSONB NOT NULL,
                updated_at   TIMESTAMPTZ NOT NULL
            )
            """
        )
        base = _now() - timedelta(days=90)

        def gen():
            for i in range(1, n + 1):
                amount = round(rng.uniform(1.0, 9999.99), 5)
                meta = {
                    "source": rng.choice(["web", "ios", "android", "api"]),
                    "discount": round(rng.uniform(0, 0.3), 3),
                    "tags": rng.sample(["promo", "loyalty", "first", "ref"], k=rng.randint(0, 3)),
                }
                yield (
                    i,
                    rng.randint(1, max(n // 10, 1)),
                    amount,
                    rng.choice(["USD", "EUR", "GBP", "JPY"]),
                    json.dumps(meta),
                    base + timedelta(seconds=rng.randint(0, 90 * 86_400)),
                )

        _batched_insert(
            cur,
            "INSERT INTO public.orders VALUES (%s, %s, %s, %s, %s::jsonb, %s)",
            gen(),
            n,
        )
        cur.execute("CREATE INDEX idx_orders_updated_at ON public.orders(updated_at)")
    conn.commit()


def _seed_events(conn: psycopg.Connection, n: int, rng: random.Random) -> None:
    print(f"[seed] events ({n} rows) — bigint, jsonb, bytea, timestamptz")
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS public.events")
        cur.execute(
            """
            CREATE TABLE public.events (
                event_id     BIGINT PRIMARY KEY,
                event_type   TEXT NOT NULL,
                user_id      BIGINT,
                payload      JSONB NOT NULL,
                raw_blob     BYTEA,
                occurred_at  TIMESTAMPTZ NOT NULL
            )
            """
        )
        base = _now() - timedelta(days=30)

        def gen():
            for i in range(1, n + 1):
                payload = {
                    "path": "/" + _random_word(rng, 8),
                    "session": _random_word(rng, 12),
                    "elapsed_ms": rng.randint(0, 5000),
                }
                blob = bytes(rng.randint(0, 255) for _ in range(rng.randint(8, 32)))
                yield (
                    i,
                    rng.choice(_EVENT_TYPES),
                    rng.randint(1, max(n // 5, 1)) if rng.random() > 0.05 else None,
                    json.dumps(payload),
                    blob,
                    base + timedelta(seconds=rng.randint(0, 30 * 86_400)),
                )

        _batched_insert(
            cur,
            "INSERT INTO public.events VALUES (%s, %s, %s, %s::jsonb, %s, %s)",
            gen(),
            n,
        )
        cur.execute("CREATE INDEX idx_events_occurred_at ON public.events(occurred_at)")
    conn.commit()


def _seed_measurements(conn: psycopg.Connection, n: int, rng: random.Random) -> None:
    print(
        f"[seed] measurements ({n} rows) — bigint, double precision[], interval, timestamptz"
    )
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS public.measurements")
        cur.execute(
            """
            CREATE TABLE public.measurements (
                id           BIGINT PRIMARY KEY,
                sensor_id    BIGINT NOT NULL,
                readings     DOUBLE PRECISION[] NOT NULL,
                duration     INTERVAL NOT NULL,
                recorded_at  TIMESTAMPTZ NOT NULL
            )
            """
        )
        base = _now() - timedelta(days=7)

        def gen():
            for i in range(1, n + 1):
                readings = [round(rng.uniform(-1.0, 1.0), 6) for _ in range(rng.randint(4, 8))]
                duration = timedelta(
                    seconds=rng.randint(1, 3600), microseconds=rng.randint(0, 999_999)
                )
                yield (
                    i,
                    rng.randint(1, 256),
                    readings,
                    duration,
                    base + timedelta(seconds=rng.randint(0, 7 * 86_400)),
                )

        _batched_insert(
            cur,
            "INSERT INTO public.measurements VALUES (%s, %s, %s, %s, %s)",
            gen(),
            n,
        )
    conn.commit()


def _seed_identifiers(conn: psycopg.Connection, n: int, rng: random.Random) -> None:
    print(f"[seed] identifiers ({n} rows) — uuid, text, timestamptz")
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS public.identifiers")
        cur.execute(
            """
            CREATE TABLE public.identifiers (
                id          UUID PRIMARY KEY,
                label       TEXT NOT NULL,
                email       TEXT NOT NULL,
                active      BOOLEAN NOT NULL,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        base = _now() - timedelta(days=365)

        def gen():
            for i in range(1, n + 1):
                yield (
                    str(uuid.UUID(int=rng.getrandbits(128))),
                    f"label-{i:06d}",
                    f"user{i:06d}@example.com",
                    rng.random() > 0.1,
                    base + timedelta(seconds=rng.randint(0, 365 * 86_400)),
                )

        _batched_insert(
            cur,
            "INSERT INTO public.identifiers VALUES (%s, %s, %s, %s, %s)",
            gen(),
            n,
        )
    conn.commit()


# ---- helpers ------------------------------------------------------------


def _batched_insert(cur: psycopg.Cursor, sql: str, rows, total: int) -> None:
    batch: list[tuple] = []
    written = 0
    for row in rows:
        batch.append(row)
        if len(batch) >= _BATCH:
            cur.executemany(sql, batch)
            written += len(batch)
            batch.clear()
    if batch:
        cur.executemany(sql, batch)
        written += len(batch)
    assert written == total, f"expected {total} rows, wrote {written}"


def _random_word(rng: random.Random, n: int) -> str:
    return "".join(rng.choice(string.ascii_lowercase) for _ in range(n))


def _now() -> datetime:
    return datetime.now(UTC)


if __name__ == "__main__":
    sys.exit(main())
