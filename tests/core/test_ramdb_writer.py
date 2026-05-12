"""Atomic write tests. SPEC §7."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from r64_db_engine.core import ramdb_writer as rw


@pytest.fixture
def loading_dir(tmp_path: Path) -> Path:
    d = tmp_path / "loading"
    d.mkdir()
    return d


@pytest.fixture
def mock_save(monkeypatch: pytest.MonkeyPatch):
    """Patch the row64tools save call to a plain file-write."""

    def _fake_save(df: pd.DataFrame, path: str) -> None:
        Path(path).write_bytes(b"RAMDB" + str(len(df)).encode())

    monkeypatch.setattr(rw, "_save_ramdb", _fake_save)


def test_write_creates_target_file(loading_dir: Path, mock_save) -> None:
    w = rw.RamdbWriter(loading_dir, "PostgresSource")
    df = pd.DataFrame({"x": [1, 2, 3]})
    path = w.write(df, "Orders")
    assert path == loading_dir / "PostgresSource" / "Orders.ramdb"
    assert path.exists()
    assert path.read_bytes() == b"RAMDB3"


def test_write_creates_group_directory(loading_dir: Path, mock_save) -> None:
    w = rw.RamdbWriter(loading_dir, "PostgresSource")
    assert not (loading_dir / "PostgresSource").exists()
    w.write(pd.DataFrame({"x": [1]}), "Orders")
    assert (loading_dir / "PostgresSource").is_dir()


def test_write_atomic_failure_cleans_up_tempfile(loading_dir: Path) -> None:
    """If the save itself raises, the tempfile must not linger."""
    w = rw.RamdbWriter(loading_dir, "G")
    w.ensure_dirs()

    def boom(df, path):
        Path(path).write_bytes(b"partial")
        raise RuntimeError("simulated mid-write failure")

    with patch.object(rw, "_save_ramdb", side_effect=boom), pytest.raises(RuntimeError):
        w.write(pd.DataFrame({"x": [1]}), "Orders")

    target_dir = loading_dir / "G"
    remaining = list(target_dir.iterdir())
    assert remaining == [], f"tempfile left behind: {remaining}"
    assert not (target_dir / "Orders.ramdb").exists()


def test_write_keyboard_interrupt_cleans_up(loading_dir: Path) -> None:
    w = rw.RamdbWriter(loading_dir, "G")

    def interrupt(df, path):
        Path(path).write_bytes(b"partial")
        raise KeyboardInterrupt

    with patch.object(rw, "_save_ramdb", side_effect=interrupt), pytest.raises(KeyboardInterrupt):
        w.write(pd.DataFrame({"x": [1]}), "Orders")

    assert list((loading_dir / "G").iterdir()) == []


def test_missing_loading_dir_raises(tmp_path: Path) -> None:
    w = rw.RamdbWriter(tmp_path / "does_not_exist", "G")
    with pytest.raises(FileNotFoundError):
        w.ensure_dirs()


def test_cleanup_orphan_tempfiles(loading_dir: Path) -> None:
    w = rw.RamdbWriter(loading_dir, "G")
    w.ensure_dirs()
    g = loading_dir / "G"
    (g / ".Orders.ramdb.tmp.abc123").write_bytes(b"orphan")
    (g / ".Other.ramdb.tmp.def456").write_bytes(b"orphan")
    (g / "Real.ramdb").write_bytes(b"real")
    n = w.cleanup_orphan_tempfiles()
    assert n == 2
    assert (g / "Real.ramdb").exists()
    assert not list(g.glob(".*tmp*"))


def test_tempfile_in_same_filesystem(loading_dir: Path, mock_save) -> None:
    """Tempfile must live in the same directory as the final file."""
    w = rw.RamdbWriter(loading_dir, "G")
    seen_tmps: list[str] = []

    def capture(df, path):
        seen_tmps.append(path)
        Path(path).write_bytes(b"ok")

    with patch.object(rw, "_save_ramdb", side_effect=capture):
        w.write(pd.DataFrame({"x": [1]}), "Orders")

    tmp_path = Path(seen_tmps[0])
    assert tmp_path.parent == loading_dir / "G"
    assert tmp_path.name.startswith(".Orders.ramdb.tmp.")


def test_overwrite_existing_file(loading_dir: Path, mock_save) -> None:
    w = rw.RamdbWriter(loading_dir, "G")
    w.write(pd.DataFrame({"x": [1]}), "Orders")
    w.write(pd.DataFrame({"x": [1, 2, 3]}), "Orders")
    assert (loading_dir / "G" / "Orders.ramdb").read_bytes() == b"RAMDB3"
