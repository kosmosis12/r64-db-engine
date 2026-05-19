"""Atomic ramdb writer. SPEC §7.

Writes to a tempfile in the destination directory, then `os.rename` to
the final path (POSIX-atomic). Cleans up tempfiles on exception or
SIGTERM mid-write. Never leaves partial `.ramdb` files visible to the
Row64 Server.
"""

from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)


class RamdbWriter:
    """Atomic per-target ramdb file writer."""

    def __init__(self, loading_dir: str | os.PathLike, group: str) -> None:
        self.loading_dir = Path(loading_dir).expanduser()
        self.group = group
        self.target_dir = self.loading_dir / group

    def ensure_dirs(self) -> None:
        if not self.loading_dir.exists():
            raise FileNotFoundError(
                f"loading_dir does not exist: {self.loading_dir} (check Row64 Server install path)"
            )
        self.target_dir.mkdir(parents=True, exist_ok=True, mode=0o755)

    def target_path(self, target: str) -> Path:
        return self.target_dir / f"{target}.ramdb"

    def write(self, df: pd.DataFrame, target: str) -> Path:
        """Write the DataFrame atomically. Returns the final path."""
        self.ensure_dirs()
        final = self.target_path(target)
        tmp = self.target_dir / f".{target}.ramdb.tmp.{uuid.uuid4().hex}"
        try:
            _save_ramdb(df, tmp)
            os.replace(tmp, final)
            log.debug("ramdb_write_ok target=%s path=%s rows=%d", target, final, len(df))
            return final
        except BaseException:
            # Includes SystemExit / KeyboardInterrupt — clean up before raising.
            _safe_unlink(tmp)
            raise

    def cleanup_orphan_tempfiles(self) -> int:
        """Remove any leftover `.{target}.ramdb.tmp.*` files in target_dir."""
        if not self.target_dir.exists():
            return 0
        n = 0
        for path in self.target_dir.iterdir():
            name = path.name
            if name.startswith(".") and ".ramdb.tmp." in name:
                _safe_unlink(path)
                n += 1
        if n:
            log.warning("ramdb_writer: removed %d orphan tempfile(s) in %s", n, self.target_dir)
        return n


def _save_ramdb(df: pd.DataFrame, path: Path) -> None:
    """Persist the DataFrame to the path using row64tools.

    Imported lazily so unit tests can monkeypatch without requiring
    row64tools at collection time.
    """
    from row64tools import ramdb  # type: ignore[import-not-found]

    ramdb.save_from_df(df, str(path))


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("ramdb_writer: failed to unlink %s: %s", path, exc)


__all__ = ["RamdbWriter"]
