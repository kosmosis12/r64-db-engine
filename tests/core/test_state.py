"""SQLite state store tests. SPEC §5.3, §6.4, §9.4 (corruption recovery)."""

from __future__ import annotations

from pathlib import Path

from r64_db_engine.core.state import StateStore


def test_set_and_get_watermark_timestamp(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.set_watermark("Orders", "2026-05-11T18:23:42Z", "timestamp", 100, 1500)
    value, wm_type = store.get_watermark("Orders")
    assert value == "2026-05-11T18:23:42Z"
    assert wm_type == "timestamp"


def test_set_and_get_watermark_int(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.set_watermark("Events", 999999, "int", 50, 200)
    value, wm_type = store.get_watermark("Events")
    assert value == 999999
    assert isinstance(value, int)
    assert wm_type == "int"


def test_int_watermark_preserves_bounded_cursor_encoding(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    cursor = '{"watermark":3,"tie_breaker":2}'
    store.set_watermark("Events", cursor, "int", 1, 10)
    value, wm_type = store.get_watermark("Events")
    assert value == cursor
    assert wm_type == "int"


def test_watermark_upsert(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.set_watermark("Orders", "2026-05-11T00:00:00Z", "timestamp", 10, 100)
    store.set_watermark("Orders", "2026-05-11T01:00:00Z", "timestamp", 20, 200)
    summary = store.get_watermark_summary("Orders")
    assert summary["watermark_value"] == "2026-05-11T01:00:00Z"
    assert summary["rows_pulled"] == 20


def test_missing_watermark_returns_none(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    value, wm_type = store.get_watermark("Never")
    assert value is None
    assert wm_type is None


def test_record_pull_and_recent_history(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.record_pull("Orders", "2026-05-11T00:00:00Z", "2026-05-11T00:00:01Z",
                      "success", 100, None)
    store.record_pull("Orders", "2026-05-11T00:01:00Z", "2026-05-11T00:01:01Z",
                      "error", None, "boom")
    hist = store.recent_history("Orders")
    assert len(hist) == 2
    assert hist[0]["status"] == "error"
    assert hist[0]["error_message"] == "boom"
    assert hist[1]["status"] == "success"


def test_history_retention_caps_at_100(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    for i in range(105):
        ts = f"2026-01-01T{i // 60:02d}:{i % 60:02d}:00Z"
        store.record_pull("T", ts, None, "success", i, None)
    hist = store.recent_history("T", limit=200)
    assert len(hist) == 100
    assert hist[0]["rows_pulled"] == 104


def test_consecutive_failures_counts_run(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.record_pull("T", "2026-01-01T00:00:00Z", None, "success", 1, None)
    store.record_pull("T", "2026-01-01T00:00:01Z", None, "error", None, "x")
    store.record_pull("T", "2026-01-01T00:00:02Z", None, "error", None, "x")
    store.record_pull("T", "2026-01-01T00:00:03Z", None, "error", None, "x")
    assert store.consecutive_failures("T") == 3


def test_consecutive_failures_resets_on_success(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.record_pull("T", "2026-01-01T00:00:00Z", None, "error", None, "x")
    store.record_pull("T", "2026-01-01T00:00:01Z", None, "success", 1, None)
    store.record_pull("T", "2026-01-01T00:00:02Z", None, "error", None, "x")
    assert store.consecutive_failures("T") == 1


def test_schema_round_trip(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    cols = [
        {"name": "id", "source_type": "bigint", "pandas_dtype": "int64"},
        {"name": "n", "source_type": "text", "pandas_dtype": "string"},
    ]
    store.set_schema("Orders", cols)
    out = store.get_schema("Orders")
    assert out == cols


def test_corruption_recovery(tmp_path: Path) -> None:
    """SPEC §9.4: a corrupt state.db is replaced, not crashed on."""
    path = tmp_path / "state.db"
    path.write_bytes(b"this is not a sqlite file")
    store = StateStore(path)
    # If recovery works, this round-trip succeeds.
    store.set_watermark("Orders", "2026-05-11T00:00:00Z", "timestamp", 10, 100)
    assert store.get_watermark("Orders")[0] == "2026-05-11T00:00:00Z"


def test_state_dir_autocreated(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b" / "c" / "state.db"
    assert not nested.parent.exists()
    StateStore(nested)
    assert nested.parent.is_dir()
