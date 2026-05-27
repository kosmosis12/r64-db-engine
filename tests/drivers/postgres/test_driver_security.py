"""Security regressions for Postgres SQL construction."""

from __future__ import annotations

import pytest

from r64_db_engine.drivers.postgres.driver import _build_query


@pytest.mark.parametrize(
    ("source", "incremental_key"),
    [
        ('public.orders"; DROP TABLE public.audit; --', None),
        ("public.orders", 'updated_at"; DROP TABLE public.audit; --'),
    ],
)
def test_pull_query_escapes_identifier_quotes(
    source: str, incremental_key: str | None
) -> None:
    query, _ = _build_query(
        source=source,
        mode="incremental" if incremental_key else "full_refresh",
        incr_key=incremental_key,
        incr_type="timestamp",
        previous_watermark="2026-05-27T00:00:00Z" if incremental_key else None,
        max_rows=None,
    )
    rendered = query.as_string()
    assert 'orders"; DROP TABLE' not in rendered
    assert 'updated_at"; DROP TABLE' not in rendered
