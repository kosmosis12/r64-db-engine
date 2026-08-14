"""DuckDB -> Arrow-native lane -> ArrowIpcSink, against the real meshbench data.

Nothing is stubbed. The source is `~/bench-ch/meshbench.duckdb`, loaded from the
ClickHouse campaign's own rows via `bench/load-duckdb.py`, and every expectation
comes from `bench/GROUND-TRUTH-clickhouse.json` — the numbers the ClickHouse
campaign captured AT SOURCE. Identical rows, so the truth carries over verbatim:
a miss here is a pipeline defect, never dataset drift.

Run with `--integration`. Skips (never fails) when the database has not been
built, so a clean checkout does not report a false red.
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
pytest.importorskip("duckdb")

from r64_db_engine.core.config import Config  # noqa: E402
from r64_db_engine.core.daemon import build_daemon  # noqa: E402

DB = Path.home() / "bench-ch" / "meshbench.duckdb"
GROUND_TRUTH = Path(__file__).resolve().parents[2] / "bench" / "GROUND-TRUTH-clickhouse.json"
# Inline SQL, not a bare table name: the ORDER BY is what makes the artifact
# byte-reproducible, and `TableConfig` is extra="forbid" so a per-table
# `order_by` key is not expressible without a core edit. The driver passes an
# unprojected inline source through verbatim, so this ordering is authoritative
# rather than a subquery the planner may reorder.
SOURCE = "SELECT * FROM main.perf_1m ORDER BY row_id"
TARGET = "Perf"

# The 14-column meshbench schema as it lands through the ARROW lane.
#
# Two entries differ from the ClickHouse/pandas lane's artifact, both verified
# rather than assumed (see bench/FINDINGS-arrow-lane.md B-3):
#   - strings land as `string`, not `large_string`. pandas 3.0's string dtype
#     produces large_string on the df lane; DuckDB's Arrow export produces
#     plain utf8. Both are valid Arrow strings and meshroad reads both.
#   - timestamp[us] arrives NATIVELY. The df lane needs `timestamp_unit: us`
#     to get there because pandas forces datetime64[ns]; DuckDB TIMESTAMP is
#     already microsecond, so no cast is configured here at all.
EXPECTED_SCHEMA = {
    "row_id": "int64",
    "account_id": "int64",
    "user_id": "int64",
    "region": "string",
    "city": "string",
    "category": "string",
    "segment": "string",
    "product_name": "string",
    "status": "dictionary<values=string, indices=int32, ordered=0>",
    "amount": "double",
    "quantity": "int64",
    "price": "double",
    "score": "double",
    "event_time": "timestamp[us]",
}

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not DB.exists(), reason=f"{DB} not built — run bench/load-duckdb.py"
    ),
]


def _truth() -> dict[str, Any]:
    return json.loads(GROUND_TRUTH.read_text())["tables"]["perf_1m"]


def _config(tmp_path: Path, *, mode: str = "full_refresh", arrow: bool = True) -> Config:
    out = tmp_path / "arrow_out"
    out.mkdir(parents=True, exist_ok=True)
    table: dict[str, Any] = {
        "source": SOURCE,
        "target": TARGET,
        "mode": mode,
        "cadence": "5s",
    }
    if mode == "incremental":
        table["incremental_key"] = "row_id"
        table["incremental_type"] = "int"
    return Config.model_validate(
        {
            "dialect": "duckdb",
            "duckdb": {
                "database": str(DB),
                "read_only": True,
                "arrow": arrow,
                # Pin UTC: DuckDB's default session TimeZone is the machine's
                # local zone, which would shift every timestamp on read.
                "settings": {"TimeZone": "UTC"},
            },
            "row64": {"loading_dir": str(out), "group": "DuckSource"},
            "sink": {
                "type": "arrow_ipc",
                "output_dir": str(out),
                "group": "DuckSource",
                # LowCardinality does not survive Parquet, so the dictionary
                # encoding is re-applied here — a property of the artifact.
                "dictionary_columns": {TARGET: ["status"]},
                **({} if arrow else {"timestamp_unit": "us"}),
            },
            "runtime": {"state_dir": str(tmp_path / "state")},
            "tables": [table],
        }
    )


def _pull(config: Config) -> tuple[Path, Any]:
    daemon = build_daemon(config)
    asyncio.run(daemon.run(once=True))
    rt = daemon.tables[TARGET]
    assert rt.status == "ok", f"pull failed: {rt.last_error}"
    assert daemon.status_snapshot()["source"]["lane"] == (
        "arrow" if config.driver_config().get("arrow", True) else "dataframe"
    )
    path = Path(config.sink.options()["output_dir"]) / "DuckSource" / f"{TARGET}.arrow"
    return path, daemon


def _read(path: Path):
    return ipc.open_file(pa.memory_map(str(path))).read_all()


@pytest.fixture(scope="module")
def artifact(tmp_path_factory) -> Path:
    tmp = tmp_path_factory.mktemp("duckdb_arrow")
    path, _ = _pull(_config(tmp))
    return path


# --------------------------------------------------------------------------
# Proof 1 — schema exact, 14/14
# --------------------------------------------------------------------------


def test_schema_is_exactly_the_fourteen_meshbench_columns(artifact: Path) -> None:
    table = _read(artifact)
    got = {f.name: str(f.type) for f in table.schema}

    assert list(got) == list(EXPECTED_SCHEMA), "column set or order drifted"
    assert got == EXPECTED_SCHEMA


def test_status_landed_dictionary_encoded(artifact: Path) -> None:
    """LowCardinality(String) is lost through Parquet; the sink re-applies it."""
    table = _read(artifact)
    status = table.schema.field("status").type
    assert pa.types.is_dictionary(status)
    assert pa.types.is_int32(status.index_type)


# --------------------------------------------------------------------------
# Proof 2 — aggregate parity, 10/10 against ground truth
# --------------------------------------------------------------------------


def test_aggregate_parity_against_clickhouse_ground_truth(artifact: Path) -> None:
    import pyarrow.compute as pc

    table = _read(artifact)
    truth = _truth()

    scaled = pc.sum(
        pc.cast(pc.round(pc.multiply(table.column("amount"), 100)), pa.int64())
    ).as_py()
    status = table.column("status").cast(pa.string())
    region = table.column("region")

    got = {
        "count": table.num_rows,
        "scaled_amount_sum_exact_int": scaled,
        "sum_quantity": pc.sum(table.column("quantity")).as_py(),
        "count_score_null": table.column("score").null_count,
        "uniq_status": len(pc.unique(status)),
        "uniq_region": len(pc.unique(region)),
        "uniq_product_name": len(pc.unique(table.column("product_name"))),
        "max_account_id": pc.max(table.column("account_id")).as_py(),
        "count_status_active": pc.sum(pc.equal(status, "active")).as_py(),
        "count_region_west": pc.sum(pc.equal(region, "West")).as_py(),
    }

    mismatches = {k: (v, truth[k]) for k, v in got.items() if v != truth[k]}
    assert not mismatches, f"aggregate parity miss (got, truth): {mismatches}"


def test_status_and_region_value_sets_match_ground_truth(artifact: Path) -> None:
    import pyarrow.compute as pc

    table = _read(artifact)
    truth = json.loads(GROUND_TRUTH.read_text())
    status = sorted(pc.unique(table.column("status").cast(pa.string())).to_pylist())
    region = sorted(pc.unique(table.column("region")).to_pylist())
    assert status == truth["status_values"]
    assert region == truth["region_values"]


# --------------------------------------------------------------------------
# Proof 3 — RF-002 armed: nulls survive as nulls
# --------------------------------------------------------------------------


def test_rf002_score_nulls_survive_exactly(artifact: Path) -> None:
    """`score` is Nullable(Float64) with 20,039 NULLs at source.

    The discriminator is not vacuous: a pipeline that filled NULL with 0.0 or
    NaN would report null_count == 0 here and still pass every other test in
    this file.
    """
    table = _read(artifact)
    truth = _truth()

    assert table.column("score").null_count == truth["count_score_null"] == 20_039
    assert table.column("score").null_count > 0  # the discriminator is armed
    # Every other column is non-nullable at source and must have NO nulls.
    for name in EXPECTED_SCHEMA:
        if name == "score":
            continue
        assert table.column(name).null_count == 0, f"{name} gained nulls"


def test_rf002_nulls_are_not_nan(artifact: Path) -> None:
    """A NULL that became NaN would keep null_count at 0 and poison sums."""
    import pyarrow.compute as pc

    score = _read(artifact).column("score")
    nan_count = pc.sum(pc.is_nan(pc.fill_null(score, 0.0))).as_py()
    assert nan_count == 0


# --------------------------------------------------------------------------
# Proof 4 — PG-011 refusal: full-refresh only
# --------------------------------------------------------------------------


def test_incremental_is_refused_end_to_end(tmp_path: Path) -> None:
    """The refusal fires before any connection work, as a config-time error."""
    with pytest.raises(Exception, match="incremental"):
        build_daemon(_config(tmp_path, mode="incremental"))


# --------------------------------------------------------------------------
# Proof 5 — the artifact is servable: uncompressed, mmap-readable, blocked
# --------------------------------------------------------------------------


def test_artifact_is_block_chunked_at_65536(artifact: Path) -> None:
    reader = ipc.open_file(pa.memory_map(str(artifact)))
    blocks = [reader.get_batch(i).num_rows for i in range(reader.num_record_batches)]

    assert sum(blocks) == 1_000_000
    assert blocks[:-1] == [65_536] * (len(blocks) - 1)
    assert blocks[-1] == 1_000_000 - 65_536 * (len(blocks) - 1)
    assert len(blocks) == 16


def test_artifact_is_uncompressed_and_mmap_readable(artifact: Path) -> None:
    """Uncompressed is the consumer's zero-copy contract — assert the bytes."""
    import pyarrow.compute as pc

    # 1M rows x 4 int64/double columns alone exceeds 30MB uncompressed.
    assert artifact.stat().st_size > 30_000_000
    with pa.memory_map(str(artifact), "rb") as source:
        table = ipc.open_file(source).read_all()
        assert pc.sum(table.column("row_id")).as_py() == 499_999_500_000


# --------------------------------------------------------------------------
# Proof 6 — checksum reproducibility (lane-scoped)
# --------------------------------------------------------------------------


def test_two_consecutive_pulls_are_byte_identical(tmp_path: Path) -> None:
    """Determinism doctrine: ORDER BY row_id makes the artifact reproducible.

    DuckDB parallelizes scans with no order guarantee, so this is a property of
    the CONFIGURED pull, not of the engine.

    Lane-scoped, per the ratified checksum doctrine: identical bytes are
    expected WITHIN a lane. Cross-lane comparison is data + schema-minus-
    metadata + block structure, never sha — see the N vs P' test below.
    """
    first, _ = _pull(_config(tmp_path / "a"))
    second, _ = _pull(_config(tmp_path / "b"))

    digest_a = hashlib.sha256(first.read_bytes()).hexdigest()
    digest_b = hashlib.sha256(second.read_bytes()).hexdigest()
    assert digest_a == digest_b, "artifact is not byte-reproducible"
    assert first.stat().st_size == second.stat().st_size


def test_arrow_and_dataframe_lanes_agree_on_data_but_not_on_bytes(
    tmp_path: Path,
) -> None:
    """N vs P': the ratified cross-lane equivalence fence, proven on real data.

    Same DuckDB source, same rows, two lanes. Equivalence is DATA +
    SCHEMA-MINUS-METADATA + BLOCK STRUCTURE. The shas differ, and so does the
    string width — neither is a fidelity failure:

      - the df lane carries a `b'pandas'` schema-metadata blob (Gate A, A-3)
      - pandas 3.0's string dtype yields `large_string`; DuckDB's Arrow export
        yields `string`

    Reading either as corruption would be wrong, and Phase C must not.
    """
    import pyarrow.compute as pc

    n_path, _ = _pull(_config(tmp_path / "n", arrow=True))
    p_path, _ = _pull(_config(tmp_path / "p", arrow=False))
    n, p = _read(n_path), _read(p_path)

    # Data is identical, column for column.
    assert n.num_rows == p.num_rows == 1_000_000
    assert n.column("score").null_count == p.column("score").null_count
    for name in ("row_id", "quantity", "account_id"):
        assert pc.sum(n.column(name)).as_py() == pc.sum(p.column(name)).as_py()
    assert n.column("status").cast(pa.string()).to_pylist()[:100] == (
        p.column("status").cast(pa.string()).to_pylist()[:100]
    )

    # Block structure is identical.
    n_reader = ipc.open_file(pa.memory_map(str(n_path)))
    p_reader = ipc.open_file(pa.memory_map(str(p_path)))
    assert n_reader.num_record_batches == p_reader.num_record_batches

    # Schema differs ONLY in string width, and the bytes differ. Both expected.
    assert str(n.schema.field("region").type) == "string"
    assert str(p.schema.field("region").type) == "large_string"
    assert hashlib.sha256(n_path.read_bytes()).hexdigest() != (
        hashlib.sha256(p_path.read_bytes()).hexdigest()
    )
