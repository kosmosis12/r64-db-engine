"""Supabase -> daemon -> ArrowIpcSink, end to end against the live local stack.

Nothing is stubbed. These run against the running `supabase_db_agent-hud`
container (the local Supabase stack on 54322) and pull `public.intel_logs` —
real production-shaped sigdet data, not a fixture: 4,599 rows, 14 columns, and
genuinely nullable columns with genuinely missing values.

Every value check is made against ground truth read independently over psycopg,
never recomputed from the pipeline's own output. A pipeline that checks itself
proves only that it is self-consistent. Ground truth is read at test time rather
than pinned to a constant, because this is a live table that grows: pinning
counts would turn ordinary data arrival into a test failure. The invariant under
test is PARITY between two independent reads of the same table, which holds at
any row count.

`intel_logs` is the interesting case on purpose. It carries three column types
this engine had never been proven against:

    embedding   vector(768)   pgvector — uncharted before this campaign
    companies   text[]        Postgres array
    deal_sizes  numeric[]     Postgres array of exact numerics
    metadata    jsonb

Run with:  .venv/bin/pytest tests/e2e/test_supabase_to_arrow.py --integration -s
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

pa = pytest.importorskip("pyarrow")
ipc = pytest.importorskip("pyarrow.ipc")
psycopg = pytest.importorskip("psycopg")

from r64_db_engine.core.config import Config  # noqa: E402
from r64_db_engine.core.daemon import build_daemon  # noqa: E402
from r64_db_engine.core.profile import ProfileError  # noqa: E402

pytestmark = pytest.mark.integration

SOURCE = "public.intel_logs"
TARGET = "IntelLogs"

# The local Supabase stack: direct Postgres, loopback, no pooler in the path.
# Derived from `supabase status -o env` in ~/agent-hud, not invented here.
SUPABASE_LOCAL = {
    "host": "127.0.0.1",
    "port": 54322,
    "database": "postgres",
    "user": "postgres",
    "password": "postgres",
    "sslmode": "disable",
}

CONNINFO = (
    f"host={SUPABASE_LOCAL['host']} port={SUPABASE_LOCAL['port']} "
    f"dbname={SUPABASE_LOCAL['database']} user={SUPABASE_LOCAL['user']} "
    f"password={SUPABASE_LOCAL['password']}"
)

# Columns that are nullable in the schema. `section` and `entry_date` carry real
# NULLs; `embedding` is the pgvector column and carries a few. These are the
# RF-002 discriminator's material: count(*) vs count(col) must disagree, or NULL
# collapsed into a sentinel somewhere in the pull.
NULLABLE_COLUMNS = (
    "embedding",
    "entry_date",
    "section",
    "companies",
    "sectors",
    "deal_sizes",
    "metadata",
    "created_at",
    "updated_at",
)


def _ground_truth() -> dict[str, Any]:
    """Read expectations straight from Postgres, bypassing the driver.

    Deliberately not routed through `PostgresDriver`: a fixture that used the
    driver to verify the driver could hide a defect in both directions at once.
    """
    counts = ", ".join(f"count({c})" for c in NULLABLE_COLUMNS)
    with psycopg.connect(CONNINFO) as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT count(*), {counts}, "
            f"count(DISTINCT source_doc_id), max(id), max(chunk_index) "
            f"FROM {SOURCE}"
        )
        row = cur.fetchone()
    assert row is not None
    return {
        "count": row[0],
        "notnull": dict(
            zip(NULLABLE_COLUMNS, row[1 : 1 + len(NULLABLE_COLUMNS)], strict=True)
        ),
        "distinct_source_doc_id": row[-3],
        "max_id": row[-2],
        "max_chunk_index": row[-1],
    }


def _config(tmp_path: Path, *, mode: str = "full_refresh", profile: str | None = "supabase") -> Config:
    out = tmp_path / "arrow_out"
    out.mkdir(parents=True, exist_ok=True)
    table: dict[str, Any] = {"source": SOURCE, "target": TARGET, "mode": mode, "cadence": "5s"}
    if mode == "incremental":
        table["incremental_key"] = "id"
        table["incremental_type"] = "int"
    payload: dict[str, Any] = {
        "dialect": "postgres",
        "postgres": dict(SUPABASE_LOCAL),
        # Required by the config model; unused once an explicit sink is set.
        "row64": {"loading_dir": str(tmp_path), "group": "Supabase"},
        "sink": {"type": "arrow_ipc", "output_dir": str(out)},
        "tables": [table],
        "runtime": {"state_dir": str(tmp_path / "state")},
        "telemetry": {"health_port": 0, "metrics_port": 0},
    }
    if profile is not None:
        payload["profile"] = profile
    return Config.model_validate(payload)


def _pull(cfg: Config) -> Path:
    daemon = build_daemon(cfg)
    asyncio.run(daemon.run(once=True))
    return daemon.writer.target_path(cfg.tables[0].target)


def _read(path: Path):
    return ipc.open_file(pa.memory_map(str(path))).read_all()


@pytest.fixture(scope="module")
def truth() -> dict[str, Any]:
    return _ground_truth()


@pytest.fixture(scope="module")
def pulled(tmp_path_factory) -> tuple[Path, Any]:
    """One full-refresh pull of the whole table, shared across the checks."""
    tmp_path = tmp_path_factory.mktemp("supabase_e2e")
    path = _pull(_config(tmp_path))
    return path, _read(path)


# ---- the profile is actually in the path ------------------------------


def test_local_stack_is_direct_postgres_untouched_by_the_profile(tmp_path: Path):
    """Loopback means no pooler and no TLS to enforce. Profile is a no-op here.

    Worth pinning: if the profile ever started rewriting loopback configs, the
    local dev path would diverge from the thing operators actually run.
    """
    resolved = _config(tmp_path).driver_config()
    assert resolved["sslmode"] == "disable"
    assert resolved["prepare_threshold"] == 5
    assert resolved["port"] == 54322


# ---- row-level parity against independent ground truth ----------------


def test_row_count_matches_postgres(pulled, truth):
    _, table = pulled
    assert table.num_rows == truth["count"]


def test_column_count_is_the_full_fourteen(pulled):
    _, table = pulled
    assert table.num_columns == 14


def test_rf002_discriminator_holds_for_every_nullable_column(pulled, truth):
    """count(*) vs count(col), per column, against psql's own answer.

    This is the standing proof that SQL NULL survived Supabase -> driver ->
    sink. It is only meaningful because the two sides disagree: `section` is
    NULL for hundreds of rows here, so a pipeline that filled NULL with "" or 0
    would show count(col) == count(*) and fail loudly.
    """
    _, table = pulled
    total = table.num_rows
    discriminating = 0
    for column in NULLABLE_COLUMNS:
        expected = truth["notnull"][column]
        actual = table.column(column).length() - table.column(column).null_count
        assert actual == expected, (
            f"{column}: arrow non-null={actual} but postgres count({column})={expected}"
        )
        if expected != total:
            discriminating += 1
    # Guard against a vacuous pass: if nothing in this table were ever NULL the
    # assertions above would hold for a pipeline that erases nulls entirely.
    assert discriminating >= 2, (
        "no nullable column actually contained a NULL — the discriminator proved nothing"
    )


def test_null_columns_are_true_arrow_nulls_not_sentinels(pulled, truth):
    """A null must be an Arrow null, not the string 'None'/'nan'/''."""
    _, table = pulled
    section = table.column("section").to_pylist()
    nulls = [v for v in section if v is None]
    assert len(nulls) == truth["count"] - truth["notnull"]["section"]
    assert not any(v in ("", "None", "nan", "<NA>", "NULL") for v in section)


def test_distinct_and_max_match_postgres(pulled, truth):
    _, table = pulled
    ids = table.column("id").to_pylist()
    docs = table.column("source_doc_id").to_pylist()
    assert max(ids) == truth["max_id"]
    assert len(set(docs)) == truth["distinct_source_doc_id"]
    assert max(table.column("chunk_index").to_pylist()) == truth["max_chunk_index"]


# ---- the previously uncharted column types ----------------------------


def test_pgvector_column_survives_the_pull(pulled, truth):
    """pgvector through psycopg -> pandas -> Arrow was uncharted before this.

    It lands as a JSON array string. That is lossless for the values and keeps
    the null distinct from an all-zero vector — which matters, because a zeroed
    embedding is a *valid* vector and would be indistinguishable from "we never
    computed one" if nulls were filled.
    """
    _, table = pulled
    embedding = table.column("embedding")
    assert embedding.null_count == truth["count"] - truth["notnull"]["embedding"]
    sample = next(v for v in embedding.to_pylist() if v is not None)
    parsed = json.loads(sample)
    assert isinstance(parsed, list)
    assert len(parsed) > 0
    assert all(isinstance(x, (int, float)) for x in parsed)


def test_text_array_columns_land_as_json(pulled):
    _, table = pulled
    sample = next(v for v in table.column("companies").to_pylist() if v is not None)
    parsed = json.loads(sample)
    assert isinstance(parsed, list)
    assert all(isinstance(x, str) for x in parsed)


def test_numeric_array_keeps_exact_precision_as_strings(pulled):
    """numeric[] must not round-trip through float.

    Postgres `numeric` is exact; rendering it as a JSON number would hand it to
    a float64 and lose the guarantee. It is carried as a string instead.
    """
    _, table = pulled
    sample = next(v for v in table.column("deal_sizes").to_pylist() if v is not None)
    parsed = json.loads(sample)
    assert isinstance(parsed, list)
    assert all(isinstance(x, str) for x in parsed)


def test_jsonb_column_lands_as_parseable_json(pulled):
    _, table = pulled
    sample = next(v for v in table.column("metadata").to_pylist() if v is not None)
    assert isinstance(json.loads(sample), dict)


# ---- artifact reproducibility -----------------------------------------


def test_two_consecutive_pulls_are_byte_identical(tmp_path: Path):
    """Verify by checksum, not by eyeballing the frame.

    `intel_logs` has no ORDER BY applied by the driver, so this also asserts
    that Postgres returns a stable order for an unmodified table — the CH
    campaign's ORDER BY row_id decision is the precedent for what to do if this
    ever stops holding.
    """
    first = _pull(_config(tmp_path / "a"))
    second = _pull(_config(tmp_path / "b"))
    digest_a = hashlib.sha256(first.read_bytes()).hexdigest()
    digest_b = hashlib.sha256(second.read_bytes()).hexdigest()
    assert digest_a == digest_b, "artifact is not byte-reproducible across pulls"


# ---- the refusal, re-confirmed under the profile ----------------------


def test_incremental_against_arrow_ipc_is_refused_under_the_profile(tmp_path: Path):
    """PG-011 class: full-refresh only. The profile must not weaken it.

    Worth re-proving here rather than trusting the unit test: the profile sits
    between config and driver, and a profile that rewrote the table block could
    in principle smuggle an incremental config past the sink gate.
    """
    with pytest.raises(Exception) as raised:
        _pull(_config(tmp_path, mode="incremental"))
    assert "incremental" in str(raised.value).lower()


def test_transaction_pooler_config_is_refused_before_any_connection(tmp_path: Path):
    """The refusal must fire at config time, not after a half-finished pull."""
    cfg = _config(tmp_path)
    cfg.postgres.port = 6543  # type: ignore[union-attr]
    cfg.postgres.host = "proj.pooler.supabase.com"  # type: ignore[union-attr]
    with pytest.raises(ProfileError, match="transaction-mode"):
        cfg.driver_config()
