"""End-to-end test: full daemon run against testcontainers Postgres.

Gated behind `--integration`. Drives a full pull cycle through the
real Driver, the writer (with row64tools stubbed), state, and the
daemon's scheduler.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

testcontainers = pytest.importorskip("testcontainers.postgres")
PostgresContainer = testcontainers.PostgresContainer

from r64_db_engine.core import ramdb_writer as rw
from r64_db_engine.core.config import Config
from r64_db_engine.core.daemon import build_daemon

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def pg():
    with PostgresContainer("postgres:16-alpine") as c:
        yield c


def _config(pg, tmp_path: Path) -> Config:
    loading = tmp_path / "loading"
    loading.mkdir()
    state_dir = tmp_path / "state"
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
            "row64": {"loading_dir": str(loading), "group": "PG"},
            "tables": [
                {
                    "source": "public.t",
                    "target": "T",
                    "mode": "full_refresh",
                    "cadence": "5s",
                }
            ],
            "runtime": {"state_dir": str(state_dir)},
            "telemetry": {"health_port": 0, "metrics_port": 0},
        }
    )


def test_e2e_full_refresh_writes_ramdb(pg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Stub the ramdb serializer so row64tools isn't required.
    monkeypatch.setattr(
        rw,
        "_save_ramdb",
        lambda df, path: Path(path).write_bytes(b"RAMDB" + str(len(df)).encode()),
    )

    cfg = _config(pg, tmp_path)
    daemon = build_daemon(cfg)

    # Seed the source.
    async def seed():
        await daemon.driver.connect(cfg.postgres.model_dump())
        async with await daemon.driver._open() as conn, conn.cursor() as cur:
            await cur.execute("CREATE TABLE IF NOT EXISTS t (id BIGINT, n TEXT)")
            await cur.execute("INSERT INTO t VALUES (1, 'a'), (2, 'b'), (3, 'café')")
            await conn.commit()
        await daemon.driver.close()

    asyncio.run(seed())

    asyncio.run(daemon.run(once=True))
    out = Path(cfg.row64.loading_dir) / "PG" / "T.ramdb"
    assert out.exists()
    snap = daemon.status_snapshot()
    assert snap["tables"][0]["status"] == "ok"
    assert snap["tables"][0]["rows_pulled_last"] == 3
