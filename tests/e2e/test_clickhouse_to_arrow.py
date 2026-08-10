"""ClickHouse -> daemon -> ArrowIpcSink, end to end against a real server.

Nothing is stubbed. These run against the live `meshroad-ch` container holding
the meshbench dataset built by `bench/make-dataset.sh`, and every value check is
made against `bench/GROUND-TRUTH-clickhouse.json` — expectations captured AT THE
SOURCE, not recomputed from the pipeline's own output. A pipeline that checks
itself proves only that it is self-consistent.

That is affordable here only because the dataset is deterministic by
construction (zero rand(); every column cityHash64(number, k)-derived), so a
parity miss is unambiguously a pipeline defect and never dataset drift.

Run with:  .venv/bin/pytest tests/e2e/test_clickhouse_to_arrow.py --integration -s
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import numpy as np
import pytest

pa = pytest.importorskip("pyarrow")
ipc = pytest.importorskip("pyarrow.ipc")

from r64_db_engine.core.config import Config  # noqa: E402
from r64_db_engine.core.daemon import build_daemon  # noqa: E402
from r64_db_engine.core.sink import SinkError  # noqa: E402

pytestmark = pytest.mark.integration

CH = {
    "host": "127.0.0.1",
    "port": 8123,
    "database": "meshbench",
    "user": "default",
    "secure": False,
}

GROUND_TRUTH = json.loads(
    (Path(__file__).resolve().parents[2] / "bench" / "GROUND-TRUTH-clickhouse.json").read_text()
)

# The schema the meshroad reference artifact (perf_1m.arrow) carries. Pinned
# field-for-field: this is the contract meshroad's zero-copy reader is built
# against, so "close enough" is not a category here.
EXPECTED_SCHEMA: dict[str, str] = {
    "row_id": "int64",
    "account_id": "int64",
    "user_id": "int64",
    "region": "large_string",
    "city": "large_string",
    "category": "large_string",
    "segment": "large_string",
    "product_name": "large_string",
    "status": "dictionary<values=string, indices=int32, ordered=0>",
    "amount": "double",
    "quantity": "int64",
    "price": "double",
    "score": "double",
    "event_time": "timestamp[us]",
}


def _config(
    tmp_path: Path,
    *,
    source: str = "meshbench.perf_1m",
    target: str = "perf",
    mode: str = "full_refresh",
    max_rows: int | None = None,
) -> Config:
    out = tmp_path / "arrow_out"
    out.mkdir(exist_ok=True)
    table: dict = {"source": source, "target": target, "mode": mode, "cadence": "5s"}
    if mode == "incremental":
        table["incremental_key"] = "row_id"
        table["incremental_type"] = "int"
    if max_rows is not None:
        table["max_rows"] = max_rows
    return Config.model_validate(
        {
            "dialect": "clickhouse",
            "clickhouse": CH,
            # Required by the config model; unused once an explicit sink is set.
            "row64": {"loading_dir": str(tmp_path), "group": "CH"},
            "sink": {
                "type": "arrow_ipc",
                "output_dir": str(out),
                "dictionary_columns": {target: ["status"]},
                # DateTime64(6) is microsecond data; pandas widens it to ns in
                # transit, so the sink casts it back. See ArrowIpcSink.open.
                "timestamp_unit": "us",
            },
            "tables": [table],
            "runtime": {"state_dir": str(tmp_path / "state")},
            "telemetry": {"health_port": 0, "metrics_port": 0},
        }
    )


def _pull(cfg: Config) -> Path:
    daemon = build_daemon(cfg)
    asyncio.run(daemon.run(once=True))
    return daemon.writer.target_path(cfg.tables[0].target)


def _read(path: Path):
    return ipc.open_file(pa.memory_map(str(path))).read_all()


def _ch(sql: str) -> str:
    """Talk to ClickHouse over its HTTP interface, no client library involved.

    Deliberately not routed through the driver under test: a fixture that used
    the driver to verify the driver could hide a defect in both directions at
    once.

    Sent as POST with the statement in the body: ClickHouse treats an HTTP GET
    as implicitly readonly (`Code: 164 READONLY`), so the DDL/DML this fixture
    needs must go by POST. POST serves SELECT equally well, so there is one
    path rather than two.
    """
    import urllib.request

    req = urllib.request.Request(
        "http://127.0.0.1:8123/", data=sql.encode(), method="POST"
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read().decode().strip()


@pytest.fixture(scope="module")
def pulled(tmp_path_factory) -> tuple[Path, object]:
    """One full-refresh pull of all 1,000,000 rows, shared across the checks."""
    tmp_path = tmp_path_factory.mktemp("ch_e2e")
    cfg = _config(tmp_path)
    path = _pull(cfg)
    return path, _read(path)


# --------------------------------------------------------------------------
# (a) count parity
# --------------------------------------------------------------------------


def test_a_count_parity(pulled) -> None:
    _, table = pulled
    assert table.num_rows == GROUND_TRUTH["tables"]["perf_1m"]["count"]


# --------------------------------------------------------------------------
# Schema: field for field against the meshroad reference artifact
# --------------------------------------------------------------------------


def test_schema_matches_reference_artifact_field_for_field(pulled) -> None:
    _, table = pulled
    actual = {f.name: str(f.type) for f in table.schema}

    print("\n--- ClickHouse -> Arrow schema ---")
    print(f"{'column':15s} {'actual':46s} {'expected':46s} ok")
    for name, want in EXPECTED_SCHEMA.items():
        got = actual.get(name, "<MISSING>")
        print(f"{name:15s} {got:46s} {want:46s} {'ok' if got == want else 'MISMATCH'}")

    assert list(actual) == list(EXPECTED_SCHEMA), "column set or order drifted"
    assert actual == EXPECTED_SCHEMA


# --------------------------------------------------------------------------
# (b) six-for-six against source-captured ground truth
# --------------------------------------------------------------------------


def test_b_aggregates_match_ground_truth(pulled) -> None:
    _, table = pulled
    gt = GROUND_TRUTH["tables"]["perf_1m"]
    df = table.to_pandas()

    status = df["status"].astype(str)
    amount_scaled = int(np.rint(df["amount"].to_numpy() * 100).astype("int64").sum())

    actual = {
        "count": len(df),
        "uniq_status": int(status.nunique()),
        "uniq_region": int(df["region"].nunique()),
        "uniq_product_name": int(df["product_name"].nunique()),
        "count_status_active": int((status == "active").sum()),
        "count_region_west": int((df["region"] == "West").sum()),
        "sum_quantity": int(df["quantity"].sum()),
        "max_account_id": int(df["account_id"].max()),
        # The order-independent integer form is THE authority for the amount
        # check. round(sum(float)) is order-sensitive because ClickHouse sums
        # in parallel and float addition is not associative; it stays in the
        # ground-truth file as a corroborating observation only.
        "scaled_amount_sum": amount_scaled,
        "count_score_null": int(df["score"].isna().sum()),
    }

    print("\n--- aggregate parity vs source-captured ground truth ---")
    print(f"{'metric':22s} {'pipeline':>16s} {'ground truth':>16s} ok")
    for k, v in actual.items():
        expected = gt["scaled_amount_sum_exact_int"] if k == "scaled_amount_sum" else gt[k]
        print(f"{k:22s} {v:>16d} {expected:>16d} {'ok' if v == expected else 'MISMATCH'}")

    assert actual["scaled_amount_sum"] == gt["scaled_amount_sum_exact_int"]
    for key, value in actual.items():
        if key == "scaled_amount_sum":
            continue
        assert value == gt[key], f"{key}: pipeline {value} != source {gt[key]}"


# --------------------------------------------------------------------------
# (c) RF-002: the NULL discriminator is armed end to end
# --------------------------------------------------------------------------


def test_c_null_discriminator_survives_to_arrow(pulled) -> None:
    """A NULL score must arrive as a true Arrow null, not as 0.0 and not as NaN.

    Both failure modes are silent: filled zeros drag every mean() toward zero,
    and a literal NaN sets null_count=0 while poisoning every sum(). The
    discriminator is that count(*) and count(score) DISAGREE — if they agree,
    null-ness was destroyed somewhere in transit no matter what the totals say.
    """
    _, table = pulled
    gt = GROUND_TRUTH["tables"]["perf_1m"]
    score = table.column("score")

    assert score.null_count == gt["count_score_null"]

    count_star = table.num_rows
    count_score = count_star - score.null_count
    assert count_score != count_star, "count(*) == count(score): nulls were filled in transit"
    assert count_score == gt["count"] - gt["count_score_null"]

    # Not merely absent — absent AND not smuggled in as a float NaN value.
    values = score.to_pylist()
    assert sum(1 for v in values if v is None) == gt["count_score_null"]
    assert not any(v is not None and v != v for v in values), "NaN present as a value"


# --------------------------------------------------------------------------
# (d) dictionary encoding lands on disk
# --------------------------------------------------------------------------


def test_d_status_lands_dictionary_encoded(pulled) -> None:
    _, table = pulled
    status_type = table.schema.field("status").type

    assert pa.types.is_dictionary(status_type)
    assert status_type.index_type == pa.int32()
    assert pa.types.is_string(status_type.value_type)

    decoded = set(table.column("status").combine_chunks().dictionary_decode().to_pylist())
    assert decoded == set(GROUND_TRUTH["status_values"])
    assert len(decoded) == GROUND_TRUTH["tables"]["perf_1m"]["uniq_status"] == 6

    # Only what was configured. `region` is equally low-cardinality (8 values)
    # and must stay plain, or the sink is auto-detecting — which would make the
    # output schema data-dependent across pulls.
    assert not pa.types.is_dictionary(table.schema.field("region").type)


# --------------------------------------------------------------------------
# (e) atomic swap: a live reader never sees a partial file
# --------------------------------------------------------------------------


def test_e_second_pull_swaps_atomically(tmp_path: Path) -> None:
    """Re-pull after a source UPDATE: new inode, same path, no tempfiles left.

    Deliberately run against a PURPOSE-BUILT table rather than perf_1m. Mutating
    perf_1m would invalidate the committed ground truth for every other test and
    break the dataset's regenerate-byte-identically property. Atomic swap is a
    property of the sink, so a small table proves it identically.
    """
    _ch("CREATE TABLE IF NOT EXISTS meshbench.swap_probe "
        "(row_id Int64, status LowCardinality(String), amount Float64) "
        "ENGINE = MergeTree ORDER BY row_id")
    _ch("TRUNCATE TABLE meshbench.swap_probe")
    _ch("INSERT INTO meshbench.swap_probe SELECT number, 'active', 1.5 FROM numbers(1000)")

    cfg = _config(tmp_path, source="meshbench.swap_probe", target="swap")
    try:
        path = _pull(cfg)
        first_inode = os.stat(path).st_ino
        assert _read(path).to_pandas().set_index("row_id").loc[7, "amount"] == 1.5

        # Mutations are asynchronous; wait for it to actually apply.
        _ch("ALTER TABLE meshbench.swap_probe UPDATE amount = 99.25 WHERE row_id = 7 "
            "SETTINGS mutations_sync = 2")
        assert _ch("SELECT amount FROM meshbench.swap_probe WHERE row_id = 7").startswith("99.25")

        path2 = _pull(cfg)
        table2 = _read(path2)

        assert path2 == path, "output path must be stable across pulls"
        assert os.stat(path2).st_ino != first_inode, "file was rewritten in place, not swapped"
        assert table2.to_pandas().set_index("row_id").loc[7, "amount"] == 99.25

        leftovers = [p.name for p in path.parent.iterdir() if ".arrow.tmp." in p.name]
        assert leftovers == [], f"tempfiles left behind: {leftovers}"
    finally:
        _ch("DROP TABLE IF EXISTS meshbench.swap_probe")


# --------------------------------------------------------------------------
# (f) incremental against a non-appendable sink is refused at construction
# --------------------------------------------------------------------------


def test_f_incremental_against_arrow_sink_is_refused(tmp_path: Path) -> None:
    """PG-011, proven for a clickhouse-dialect config.

    Refused when the daemon is BUILT, not on the first pull, and refused rather
    than silently downgraded to full_refresh — a silent downgrade would write a
    partial snapshot indistinguishable, to the consumer, from a complete one.
    """
    cfg = _config(tmp_path, mode="incremental")

    with pytest.raises(SinkError, match="cannot serve incremental mode"):
        build_daemon(cfg)
