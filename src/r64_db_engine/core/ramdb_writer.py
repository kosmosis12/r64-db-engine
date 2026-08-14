"""Atomic ramdb writer. SPEC §7.

Writes to a tempfile in the destination directory, then `os.rename` to
the final path (POSIX-atomic). Cleans up tempfiles on exception or
SIGTERM mid-write. Never leaves partial `.ramdb` files visible to the
Row64 Server.

# Null policy: the row64tools accommodation lives HERE

`.ramdb` has no null representation. Integers must therefore arrive as numpy
`int64` with nulls already resolved, strings as plain `str`, booleans as
numpy `bool`.

That fill used to happen in `core/coercion.py`, in the SOURCE-AGNOSTIC layer,
where it silently degraded fidelity for every sink — including formats that
carry nulls natively. It now happens at this boundary, applied explicitly by
`apply_ramdb_null_fill` on the way into `save_from_df`, because it is a
property of THIS format and nothing else.

The resulting bytes are unchanged: `tests/core/test_ramdb_golden.py` asserts
byte-identity against `.ramdb` files captured before the move.

The fill is lossy and always was: a SQL `NULL` in a BIGINT becomes `0`,
indistinguishable downstream from a legitimate zero. That loss is now visible
at the point where the format forces it, rather than applied globally and
discovered later. It is recorded as a Bucket-A question for Row64 alongside the
int32 narrowing.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import uuid
from pathlib import Path

import pandas as pd
from pandas.api.types import is_integer_dtype

log = logging.getLogger(__name__)

_ROW64_INT_MIN = -(2**31)
_ROW64_INT_MAX = 2**31 - 1

# What each dtype family collapses a null to, for a format that cannot hold one.
_NULL_FILL_INT = 0
_NULL_FILL_STR = ""
_NULL_FILL_BOOL = False


class Row64CodecOverflowError(ValueError):
    """An integer value cannot be represented safely by the installed codec."""


class RamdbWriter:
    """Atomic per-target ramdb file writer."""

    def __init__(self, loading_dir: str | os.PathLike, group: str) -> None:
        self.loading_dir = Path(loading_dir).expanduser()
        self.group = group
        self.target_dir = self.loading_dir / group

    def ensure_dirs(self) -> None:
        if not self.loading_dir.exists():
            raise FileNotFoundError(
                f"loading_dir does not exist: {self.loading_dir} "
                f"(check Row64 Server install path)"
            )
        self.target_dir.mkdir(parents=True, exist_ok=True, mode=0o755)

    def target_path(self, target: str) -> Path:
        return self.target_dir / f"{target}.ramdb"

    def write(self, df: pd.DataFrame, target: str) -> Path:
        """Write the DataFrame atomically. Returns the final path."""
        self.ensure_dirs()
        df = apply_ramdb_null_fill(df)
        _raise_on_codec_unsafe_int64(df)
        final = self.target_path(target)
        tmp = self.target_dir / f".{target}.ramdb.tmp.{uuid.uuid4().hex}"
        previous_sigterm = None
        manages_sigterm = threading.current_thread() is threading.main_thread()

        def terminate(signum: int, frame: object) -> None:
            _safe_unlink(tmp)
            os._exit(128 + signum)

        if manages_sigterm:
            previous_sigterm = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, terminate)
        try:
            _save_ramdb(df, tmp)
            os.rename(tmp, final)
            log.debug("ramdb_write_ok target=%s path=%s rows=%d", target, final, len(df))
            return final
        finally:
            _safe_unlink(tmp)
            if manages_sigterm and previous_sigterm is not None:
                signal.signal(signal.SIGTERM, previous_sigterm)

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


def apply_ramdb_null_fill(df: pd.DataFrame) -> pd.DataFrame:
    """Resolve nulls the way the `.ramdb` format requires, and say so out loud.

    Integer -> 0, string -> "", boolean -> False, and the nullable pandas dtypes
    are collapsed back to their numpy equivalents so `save_from_df` sees exactly
    what it saw before this policy moved out of `core/coercion.py`.

    Float NaN and datetime NaT are left alone: ramdb represents both.

    Returns a new frame; the caller's DataFrame is never mutated.
    """
    out = df.copy()
    for column in out.columns:
        series = out[column]
        dtype = str(series.dtype)
        n_null = int(series.isna().sum())

        if is_integer_dtype(series.dtype):
            if n_null:
                log.debug("ramdb_null_fill: %d null(s) -> 0 in %r", n_null, column)
            out[column] = series.fillna(_NULL_FILL_INT).astype("int64")
        elif dtype in ("boolean", "bool"):
            if n_null:
                log.debug("ramdb_null_fill: %d null(s) -> False in %r", n_null, column)
            out[column] = series.fillna(_NULL_FILL_BOOL).astype(bool)
        elif dtype in ("string", "object"):
            if n_null:
                log.debug("ramdb_null_fill: %d null(s) -> '' in %r", n_null, column)
            out[column] = series.fillna(_NULL_FILL_STR).astype(dtype)
    return out


def _save_ramdb(df: pd.DataFrame, path: Path) -> None:
    """Persist the DataFrame to the path using row64tools.

    Imported lazily so unit tests can monkeypatch without requiring
    row64tools at collection time.
    """
    from row64tools.ramdb import save_from_df  # type: ignore[import-not-found]

    save_from_df(df, str(path))


def _raise_on_codec_unsafe_int64(df: pd.DataFrame) -> None:
    """Block row64tools 1.0.10's silent signed-int32 truncation."""
    for column in df.columns:
        series = df[column]
        if not is_integer_dtype(series.dtype):
            continue
        unsafe = series[(series < _ROW64_INT_MIN) | (series > _ROW64_INT_MAX)]
        if not unsafe.empty:
            value = int(unsafe.iloc[0])
            raise Row64CodecOverflowError(
                f"row64 codec cannot safely store int64 column {column!r}: "
                f"value {value} is outside signed int32 range"
            )


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("ramdb_writer: failed to unlink %s: %s", path, exc)


__all__ = ["RamdbWriter", "Row64CodecOverflowError", "apply_ramdb_null_fill"]
