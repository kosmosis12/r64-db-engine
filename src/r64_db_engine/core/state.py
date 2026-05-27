"""SQLite state store: watermarks, pull history, schema baselines. SPEC §5.3, §6.4."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_HISTORY_RETENTION_PER_TARGET = 100

_SCHEMA = """
CREATE TABLE IF NOT EXISTS watermarks (
    target TEXT PRIMARY KEY,
    watermark_value TEXT NOT NULL,
    watermark_type TEXT NOT NULL,
    last_success_at TEXT NOT NULL,
    rows_pulled INTEGER NOT NULL,
    last_pull_duration_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS pull_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    rows_pulled INTEGER,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_pull_history_target_started
    ON pull_history(target, started_at DESC);

CREATE TABLE IF NOT EXISTS schemas (
    target TEXT PRIMARY KEY,
    columns_json TEXT NOT NULL,
    observed_at TEXT NOT NULL
);
"""


class StateStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_or_recover()

    def _init_or_recover(self) -> None:
        try:
            with self._conn() as c:
                c.executescript(_SCHEMA)
        except sqlite3.DatabaseError as exc:
            log.warning("state.db corrupted (%s); re-creating", exc)
            self.path.unlink(missing_ok=True)
            with self._conn() as c:
                c.executescript(_SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---- watermarks ----------------------------------------------------

    def get_watermark(self, target: str) -> tuple[str | int | None, str | None]:
        with self._conn() as c:
            row = c.execute(
                "SELECT watermark_value, watermark_type FROM watermarks WHERE target = ?",
                (target,),
            ).fetchone()
            if not row:
                return None, None
            value, wm_type = row
            if wm_type == "int" and not value.startswith("{"):
                return int(value), wm_type
            return value, wm_type

    def set_watermark(
        self,
        target: str,
        value: str | int,
        wm_type: str,
        rows_pulled: int,
        duration_ms: int,
    ) -> None:
        now = _iso_now()
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO watermarks(target, watermark_value, watermark_type,
                                       last_success_at, rows_pulled, last_pull_duration_ms)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(target) DO UPDATE SET
                    watermark_value = excluded.watermark_value,
                    watermark_type  = excluded.watermark_type,
                    last_success_at = excluded.last_success_at,
                    rows_pulled     = excluded.rows_pulled,
                    last_pull_duration_ms = excluded.last_pull_duration_ms
                """,
                (target, str(value), wm_type, now, int(rows_pulled), int(duration_ms)),
            )

    def get_watermark_summary(self, target: str) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute(
                """SELECT watermark_value, watermark_type, last_success_at,
                          rows_pulled, last_pull_duration_ms
                   FROM watermarks WHERE target = ?""",
                (target,),
            ).fetchone()
        if not row:
            return None
        return {
            "watermark_value": row[0],
            "watermark_type": row[1],
            "last_success_at": row[2],
            "rows_pulled": row[3],
            "last_pull_duration_ms": row[4],
        }

    # ---- pull history --------------------------------------------------

    def record_pull(
        self,
        target: str,
        started_at: str,
        finished_at: str | None,
        status: str,
        rows_pulled: int | None,
        error_message: str | None,
    ) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO pull_history(target, started_at, finished_at,
                                            status, rows_pulled, error_message)
                   VALUES(?, ?, ?, ?, ?, ?)""",
                (target, started_at, finished_at, status, rows_pulled, error_message),
            )
            c.execute(
                """DELETE FROM pull_history
                   WHERE target = ?
                     AND id NOT IN (
                       SELECT id FROM pull_history
                       WHERE target = ?
                       ORDER BY started_at DESC
                       LIMIT ?
                     )""",
                (target, target, _HISTORY_RETENTION_PER_TARGET),
            )

    def recent_history(self, target: str, limit: int = 10) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                """SELECT started_at, finished_at, status, rows_pulled, error_message
                   FROM pull_history
                   WHERE target = ?
                   ORDER BY started_at DESC
                   LIMIT ?""",
                (target, limit),
            ).fetchall()
        return [
            {
                "started_at": r[0],
                "finished_at": r[1],
                "status": r[2],
                "rows_pulled": r[3],
                "error_message": r[4],
            }
            for r in rows
        ]

    def consecutive_failures(self, target: str) -> int:
        with self._conn() as c:
            rows = c.execute(
                """SELECT status FROM pull_history
                   WHERE target = ?
                   ORDER BY started_at DESC
                   LIMIT 20""",
                (target,),
            ).fetchall()
        n = 0
        for (status,) in rows:
            if status == "error":
                n += 1
            elif status == "success":
                break
        return n

    # ---- schema baseline -----------------------------------------------

    def get_schema(self, target: str) -> list[dict[str, str]] | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT columns_json FROM schemas WHERE target = ?", (target,)
            ).fetchone()
        if not row:
            return None
        return json.loads(row[0])

    def set_schema(self, target: str, columns: list[dict[str, str]]) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO schemas(target, columns_json, observed_at)
                   VALUES(?, ?, ?)
                   ON CONFLICT(target) DO UPDATE SET
                     columns_json = excluded.columns_json,
                     observed_at = excluded.observed_at""",
                (target, json.dumps(columns), _iso_now()),
            )


def _iso_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


__all__ = ["StateStore"]
