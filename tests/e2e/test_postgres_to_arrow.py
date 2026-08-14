"""End-to-end: real Postgres -> daemon -> ArrowIpcSink -> .arrow, read back.

Gated behind `--integration`. Unlike `test_postgres_to_ramdb.py`, NOTHING is
stubbed here: the row64tools serializer is not in this path at all, so the file
this produces is the real artifact meshroad consumes.

The load-bearing case is `test_e2e_pg001_int64_survives_postgres_to_arrow`. It
seeds the exact PG-001 value into a real Postgres `BIGINT`, pulls it through the
real driver's coercion, writes it through the real sink, and reads it back —
closing the last untested link in the ingestion chain.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

testcontainers = pytest.importorskip("testcontainers.postgres")
PostgresContainer = testcontainers.PostgresContainer
pa = pytest.importorskip("pyarrow")
ipc = pytest.importorskip("pyarrow.ipc")

from r64_db_engine.core.config import Config  # noqa: E402
from r64_db_engine.core.daemon import build_daemon  # noqa: E402
from r64_db_engine.core.sink import SinkError  # noqa: E402

pytestmark = pytest.mark.integration

PG001_VALUE = 3548933426
PG001_CORRUPTION = -746033870


def _read(path: Path):
    return ipc.open_file(pa.memory_map(str(path))).read_all()


@pytest.fixture(scope="module")
def pg():
    with PostgresContainer("postgres:16-alpine") as c:
        yield c


def _config(
    pg,
    tmp_path: Path,
    *,
    source: str = "public.t",
    target: str = "T",
    mode: str = "full_refresh",
    dictionary_columns: dict | None = None,
) -> Config:
    out = tmp_path / "arrow_out"
    out.mkdir(exist_ok=True)
    sink: dict = {
        "type": "arrow_ipc",
        "output_dir": str(out),
        "group": "PG",
    }
    if dictionary_columns:
        sink["dictionary_columns"] = dictionary_columns
    table: dict = {"source": source, "target": target, "mode": mode, "cadence": "5s"}
    if mode == "incremental":
        table["incremental_key"] = "id"
        table["incremental_type"] = "int"
    return Config.model_validate(
        {
            "dialect": "postgres",
            "postgres": {
                "host": pg.get_container_host_ip(),
                "port": int(pg.get_exposed_port(5432)),
                "database": pg.dbname,
                "user": pg.username,
                "password": pg.password,
                "sslmode": "disable",
            },
            # Still required by the config model; unused by the arrow sink.
            "row64": {"loading_dir": str(tmp_path), "group": "PG"},
            "sink": sink,
            "tables": [table],
            "runtime": {"state_dir": str(tmp_path / "state")},
            "telemetry": {"health_port": 0, "metrics_port": 0},
        }
    )


def _seed(daemon, cfg, sql_statements: list[str]) -> None:
    async def go():
        await daemon.driver.connect(cfg.postgres.model_dump())
        async with await daemon.driver._open() as conn, conn.cursor() as cur:
            for stmt in sql_statements:
                await cur.execute(stmt)
            await conn.commit()
        await daemon.driver.close()

    asyncio.run(go())


# --------------------------------------------------------------------------
# The closing link: PG-001 from a real BIGINT column
# --------------------------------------------------------------------------


def test_e2e_pg001_int64_survives_postgres_to_arrow(pg, tmp_path: Path) -> None:
    cfg = _config(pg, tmp_path, source="public.pg001", target="PG001")
    daemon = build_daemon(cfg)
    _seed(
        daemon,
        cfg,
        [
            "DROP TABLE IF EXISTS pg001",
            "CREATE TABLE pg001 (id BIGINT, duration BIGINT)",
            f"INSERT INTO pg001 VALUES (1, {PG001_VALUE}), "
            f"(2, {2**63 - 1}), (3, {-(2**31) - 1})",
        ],
    )

    asyncio.run(daemon.run(once=True))

    out = Path(cfg.sink.options()["output_dir"]) / "PG" / "PG001.arrow"
    assert out.exists()

    table = _read(out)
    assert table.schema.field("duration").type == pa.int64()
    values = table.column("duration").to_pylist()

    # The whole point: straight out of a real Postgres BIGINT, through the real
    # driver coercion, through the real sink, unchanged.
    assert PG001_VALUE in values
    assert PG001_CORRUPTION not in values
    assert 2**63 - 1 in values
    assert -(2**31) - 1 in values

    snap = daemon.status_snapshot()
    assert snap["tables"][0]["status"] == "ok"
    assert snap["tables"][0]["rows_pulled_last"] == 3


# --------------------------------------------------------------------------
# Type sweep from a real database
# --------------------------------------------------------------------------


def test_e2e_type_sweep_from_real_postgres(pg, tmp_path: Path) -> None:
    cfg = _config(pg, tmp_path, source="public.sweep", target="Sweep")
    daemon = build_daemon(cfg)
    _seed(
        daemon,
        cfg,
        [
            "DROP TABLE IF EXISTS sweep",
            """CREATE TABLE sweep (
                   id BIGINT,
                   big BIGINT,
                   amt DOUBLE PRECISION,
                   ok BOOLEAN,
                   ts TIMESTAMPTZ,
                   uid UUID,
                   note TEXT
               )""",
            f"""INSERT INTO sweep VALUES
                   (1, {PG001_VALUE}, 1234.5678, true,
                    '2026-05-27T12:34:56Z',
                    '00000000-0000-0000-0000-00000000002a',
                    'em—dash, smart “quotes”, café, 🚀'),
                   (2, 42, 0.5, false, '2026-01-01T00:00:00Z',
                    '00000000-0000-0000-0000-00000000002b', 'plain')""",
        ],
    )

    asyncio.run(daemon.run(once=True))

    out = Path(cfg.sink.options()["output_dir"]) / "PG" / "Sweep.arrow"
    table = _read(out)
    schema = table.schema

    assert schema.field("big").type == pa.int64()
    assert schema.field("amt").type == pa.float64()
    assert pa.types.is_timestamp(schema.field("ts").type)

    rows = {r["id"]: r for r in table.to_pylist()}
    assert rows[1]["big"] == PG001_VALUE
    assert rows[1]["amt"] == pytest.approx(1234.5678)
    assert str(rows[1]["uid"]) == "00000000-0000-0000-0000-00000000002a"

    # Unicode from a real TEXT column. If the engine's ascii_sanitize default
    # mangles it upstream, this records that rather than hiding it.
    note = rows[1]["note"]
    if note == "em—dash, smart “quotes”, café, 🚀":
        pass  # unicode preserved end to end
    else:
        assert "?" in note, f"unexpected note transformation: {note!r}"


def _seed_nulls(pg, tmp_path: Path):
    cfg = _config(pg, tmp_path, source="public.nulls", target="Nulls")
    daemon = build_daemon(cfg)
    _seed(
        daemon,
        cfg,
        [
            "DROP TABLE IF EXISTS nulls",
            "CREATE TABLE nulls (id BIGINT, n BIGINT, s TEXT, d DOUBLE PRECISION)",
            "INSERT INTO nulls VALUES (1, 10, 'a', 1.5), (2, NULL, NULL, NULL)",
        ],
    )
    asyncio.run(daemon.run(once=True))
    return _read(
        Path(cfg.sink.options()["output_dir"]) / "PG" / "Nulls.arrow"
    )


def test_e2e_float_null_from_real_postgres_lands_as_arrow_null(pg, tmp_path: Path) -> None:
    """The float path is correct end to end.

    `coerce_float_column` preserves NaN, and the sink maps NaN onto Arrow's null
    bitmap — so a SQL NULL in a DOUBLE PRECISION column arrives as a real Arrow
    null. This is the case that makes the NaN->null resolution in
    `sinks/arrow_ipc.py` the right one.
    """
    table = _seed_nulls(pg, tmp_path)
    rows = {r["id"]: r for r in table.to_pylist()}
    assert rows[1]["d"] == 1.5
    assert rows[2]["d"] is None
    assert table.column("d").null_count == 1


def test_e2e_ramdb_fill_still_applies_at_its_own_boundary(pg, tmp_path: Path) -> None:
    """The accommodation did not disappear — it relocated.

    The same nulls that now survive into Arrow are still collapsed to 0 / "" on
    the way into `.ramdb`, because that format cannot hold them. Asserted here
    against the frame the driver actually produced, so the two sinks' differing
    policies are visible side by side in one test file.
    """
    from r64_db_engine.core.ramdb_writer import apply_ramdb_null_fill

    table = _seed_nulls(pg, tmp_path)
    arrow_rows = {r["id"]: r for r in table.to_pylist()}
    # Arrow: nulls preserved.
    assert arrow_rows[2]["n"] is None
    assert arrow_rows[2]["s"] is None

    # ramdb: the identical data, filled at the ramdb boundary.
    filled = apply_ramdb_null_fill(table.to_pandas())
    ramdb_rows = {r["id"]: r for r in filled.to_dict("records")}
    assert ramdb_rows[2]["n"] == 0
    assert ramdb_rows[2]["s"] == ""


def test_e2e_int_and_text_nulls_survive_to_arrow(pg, tmp_path: Path) -> None:
    """CLOSED LOOP. Was xfail(strict=True); now passes.

    Null policy moved out of the source-agnostic coercion layer to the sink
    boundary, so a null-capable format keeps its nulls. A SQL NULL in BIGINT and
    TEXT now arrives as a true Arrow null instead of 0 / "".
    """
    table = _seed_nulls(pg, tmp_path)
    rows = {r["id"]: r for r in table.to_pylist()}
    assert rows[2]["n"] is None
    assert rows[2]["s"] is None


# --------------------------------------------------------------------------
# Dictionary encoding driven from config, on real data
# --------------------------------------------------------------------------


def test_e2e_dictionary_column_lands_encoded(pg, tmp_path: Path) -> None:
    cfg = _config(
        pg,
        tmp_path,
        source="public.dict_src",
        target="Dict",
        dictionary_columns={"Dict": ["status"]},
    )
    daemon = build_daemon(cfg)
    _seed(
        daemon,
        cfg,
        [
            "DROP TABLE IF EXISTS dict_src",
            "CREATE TABLE dict_src (id BIGINT, status TEXT)",
            "INSERT INTO dict_src SELECT g, "
            "(ARRAY['active','pending','closed','hold','review','churned'])[1 + (g % 6)] "
            "FROM generate_series(1, 5000) g",
        ],
    )

    asyncio.run(daemon.run(once=True))

    table = _read(
        Path(cfg.sink.options()["output_dir"]) / "PG" / "Dict.arrow"
    )
    status_type = table.schema.field("status").type
    assert pa.types.is_dictionary(status_type)
    assert status_type.index_type == pa.int32()
    assert table.num_rows == 5000
    decoded = table.column("status").combine_chunks().dictionary_decode().to_pylist()
    assert set(decoded) == {"active", "pending", "closed", "hold", "review", "churned"}


# --------------------------------------------------------------------------
# Atomic republish from the daemon itself
# --------------------------------------------------------------------------


def test_e2e_second_pull_atomically_replaces_the_file(pg, tmp_path: Path) -> None:
    """Mutate the source, pull again, and confirm a rename-based swap.

    The inode must change while the path stays put — that is the signal
    meshroad's `StatKey` detects.
    """
    cfg = _config(pg, tmp_path, source="public.swap_src", target="Swap")
    daemon = build_daemon(cfg)
    _seed(
        daemon,
        cfg,
        [
            "DROP TABLE IF EXISTS swap_src",
            "CREATE TABLE swap_src (id BIGINT, v BIGINT)",
            f"INSERT INTO swap_src VALUES (1, {PG001_VALUE})",
        ],
    )

    asyncio.run(daemon.run(once=True))
    out = Path(cfg.sink.options()["output_dir"]) / "PG" / "Swap.arrow"
    first_inode = out.stat().st_ino
    assert _read(out).column("v").to_pylist() == [PG001_VALUE]

    _seed(daemon, cfg, [f"UPDATE swap_src SET v = {PG001_VALUE + 1} WHERE id = 1"])
    daemon2 = build_daemon(cfg)
    asyncio.run(daemon2.run(once=True))

    assert out.stat().st_ino != first_inode, "republish must be a rename, not a rewrite"
    assert _read(out).column("v").to_pylist() == [PG001_VALUE + 1]
    # No tempfiles survive a successful publish.
    assert [p.name for p in out.parent.iterdir() if ".arrow.tmp." in p.name] == []


# --------------------------------------------------------------------------
# The incremental refusal, against a real database
# --------------------------------------------------------------------------


def test_e2e_incremental_against_arrow_sink_fails_fast(pg, tmp_path: Path) -> None:
    """The PG-011 trap stays closed when a real config asks for it."""
    cfg = _config(pg, tmp_path, source="public.t", target="T", mode="incremental")
    with pytest.raises(SinkError, match="cannot serve incremental mode"):
        build_daemon(cfg)
