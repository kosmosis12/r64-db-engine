"""Arrow IPC (Feather v2) sink — mmap-ready output for meshroad.

Writes each target as an uncompressed Arrow IPC file that a consumer can
`mmap` and query in place, with no decode and no copy.

# Why this sink exists: the int64 lineage

`row64tools`' ramdb codec narrows int64 to signed int32. Before the guard in
`core/ramdb_writer.py` landed, that narrowing was SILENT: a seeded
`duration = 3548933426` loaded back as `-746033870` (PG-001). The guard turned
silent corruption into a loud `Row64CodecOverflowError`, which is strictly
better and still means the value cannot be stored at all.

Arrow's int64 is a native 64-bit type. There is no narrowing step to guard,
because there is no narrowing. `3548933426` is written and read as
`3548933426`. This is not a fix applied on top of a lossy codec — it is a
format in which that class of defect is unrepresentable, which is why the sink
carries NO integer-range guard: adding one would imply a risk that does not
exist here.

# Uncompressed is load-bearing

`compression="uncompressed"` is not a default that a config key may override —
this sink exposes no compression option at all, deliberately. A compressed IPC
body must be decompressed into fresh heap buffers before it can be read, which
destroys the consumer's zero-copy mmap path: meshroad's `copied_columns = 0`
assertion would start failing, and it would fail as a *performance* symptom
long after the config change that caused it. A knob whose only reachable effect
is to silently break the consumer is not a feature.

# Block granularity is load-bearing

The file is written as multi-block Arrow IPC at `_BLOCK_ROWS` (65536) rows per
record batch, because that is the granularity the consumer's per-block column
cache is keyed on. A single one-block file would collapse that granularity and
turn the warm-pass `columns_decoded = 0` result into a whole-file decode.

This used to be spelled `feather.write_feather(..., version=2)`, which produced
those blocks via its *default* chunksize rather than by saying so (matching
`tools/gen_arrow.py` in the meshroad repo). That default was never ours to rely
on, and `write_feather` is deprecated as of pyarrow 24.0.0 (D-4), so the
chunking is now explicit: same format, same blocks, stated rather than
inherited. Feather v2 *is* the Arrow IPC file format, so this is a change of
API, not of container.

The migration was proven byte-for-byte, not assumed: `pa.ipc.new_file()` with
`max_chunksize=_BLOCK_ROWS` reproduces `write_feather`'s exact bytes across a
200k-row multi-block table, an exactly-65536-row boundary table, and the empty,
null-bearing, dictionary-encoded and timestamp cases. Pinned by
`test_block_granularity_is_preserved_across_the_64k_boundary`.

# Null and NaN: a deliberate divergence from the ramdb path

SPEC §6 says float NaN is *preserved* — true of ramdb, and NOT true here.
`pa.Table.from_pandas` maps float64 NaN onto Arrow's null bitmap, so a NaN goes
in and a null comes out (`null_count` rises; `to_pylist()` yields `None`).

That is kept on purpose. By the time a DataFrame reaches any sink, pandas has
already conflated SQL `NULL` and SQL `'NaN'::float8` into the same float64 NaN
— the information is lost upstream of us and neither choice can recover it. The
question is only which way to resolve the ambiguity, and null is the safer
resolution: Arrow aggregates SKIP nulls, matching SQL semantics for the
overwhelmingly more common case (an actual NULL), whereas writing NaN would set
`null_count = 0` and poison every downstream `sum()`/`mean()` with NaN. Passing
`from_pandas=False` would preserve literal NaN and cause exactly that.

So: genuine NaNs are indistinguishable from NULLs here and land as null. Do not
"fix" this without first fixing the conflation in the driver's coercion layer,
where the distinction still exists.

# Atomicity

Write to `<target>.arrow.tmp.<uuid>` in the destination directory, fsync the
file, `rename(2)` onto the final path, then fsync the directory. The rename is
atomic on POSIX within a filesystem, so a concurrent reader sees either the
whole previous file or the whole new one. The directory fsync is what makes the
rename itself durable across power loss — `core/ramdb_writer.py` omits that
step; it is added here rather than retrofitted there, to keep audited code
untouched.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import uuid
from pathlib import Path
from typing import Any

import pandas as pd

from r64_db_engine.core.sink import Sink, SinkError

log = logging.getLogger(__name__)

# Rows per Arrow IPC record batch (== per block). This is the consumer's
# per-block column-cache granularity, not a tuning knob: see the module
# docstring. It matches the `write_feather` default this sink used to inherit,
# so artifacts written before and after the D-4 migration are byte-identical.
_BLOCK_ROWS = 65536


class ArrowIpcSink(Sink):
    """Atomic per-target Arrow IPC (Feather v2) writer."""

    def __init__(self) -> None:
        self.output_dir: Path | None = None
        self.group: str = ""
        self._dictionary_columns: dict[str, list[str]] = {}
        self._timestamp_unit: str | None = None

    @classmethod
    def sink_name(cls) -> str:
        return "arrow_ipc"

    def open(self, config: dict[str, Any]) -> None:
        output_dir = config.get("output_dir")
        if not output_dir:
            raise SinkError("arrow_ipc sink requires 'output_dir'")

        # Refuse the knob rather than accept-and-ignore it: a config that asks
        # for compression is a config written against a different set of
        # expectations, and silently producing an uncompressed file would leave
        # the operator believing something false about the output.
        if "compression" in config:
            raise SinkError(
                "arrow_ipc sink does not support 'compression': compressed IPC "
                "buffers must be decompressed before reading, which defeats the "
                "consumer's zero-copy mmap path. Remove the key."
            )

        self.output_dir = Path(output_dir).expanduser()
        self.group = str(config.get("group", "") or "")

        # Opt-in, never a blanket default. pandas carries datetime64[ns], so
        # from_pandas yields timestamp[ns] while the meshroad reference artifact
        # (perf_1m.arrow) carries timestamp[us]; a ClickHouse DateTime64(6)
        # source is microsecond data that only became nanoseconds by passing
        # through pandas. Casting it back is exact.
        #
        # It stays opt-in because a blanket ns->us cast would silently TRUNCATE
        # a source that genuinely has nanosecond resolution. The cast below is
        # additionally a SAFE cast, so even when configured, precision loss
        # raises instead of quietly rounding.
        unit = config.get("timestamp_unit")
        if unit is not None:
            if unit not in ("s", "ms", "us", "ns"):
                raise SinkError(
                    f"arrow_ipc 'timestamp_unit' must be one of s/ms/us/ns, got {unit!r}"
                )
            self._timestamp_unit = str(unit)

        raw = config.get("dictionary_columns") or {}
        if not isinstance(raw, dict):
            raise SinkError(
                "arrow_ipc 'dictionary_columns' must be a mapping of "
                "target name -> list of column names"
            )
        for target, columns in raw.items():
            if not isinstance(columns, list) or not all(isinstance(c, str) for c in columns):
                raise SinkError(
                    f"arrow_ipc 'dictionary_columns[{target}]' must be a list of column names"
                )
            self._dictionary_columns[str(target)] = list(columns)

    @property
    def target_dir(self) -> Path:
        if self.output_dir is None:
            raise SinkError("arrow_ipc sink used before open()")
        return self.output_dir / self.group if self.group else self.output_dir

    def ensure_ready(self) -> None:
        if self.output_dir is None:
            raise SinkError("arrow_ipc sink used before open()")
        if not self.output_dir.exists():
            raise FileNotFoundError(f"output_dir does not exist: {self.output_dir}")
        self.target_dir.mkdir(parents=True, exist_ok=True, mode=0o755)

    def target_path(self, target: str) -> Path:
        return self.target_dir / f"{target}.arrow"

    def write(self, df: pd.DataFrame, target: str) -> Path:
        self.ensure_ready()
        final = self.target_path(target)
        tmp = self.target_dir / f".{target}.arrow.tmp.{uuid.uuid4().hex}"

        previous_sigterm = None
        manages_sigterm = threading.current_thread() is threading.main_thread()

        def terminate(signum: int, frame: object) -> None:
            _safe_unlink(tmp)
            os._exit(128 + signum)

        if manages_sigterm:
            previous_sigterm = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, terminate)
        try:
            table = _to_arrow_table(
                df, self._dictionary_columns.get(target, []), self._timestamp_unit
            )
            _write_ipc_file(table, tmp)
            _fsync_path(tmp)
            os.rename(tmp, final)
            _fsync_dir(self.target_dir)
            log.debug("arrow_write_ok target=%s path=%s rows=%d", target, final, len(df))
            return final
        finally:
            _safe_unlink(tmp)
            if manages_sigterm and previous_sigterm is not None:
                signal.signal(signal.SIGTERM, previous_sigterm)

    def cleanup_orphan_tempfiles(self) -> int:
        if self.output_dir is None or not self.target_dir.exists():
            return 0
        n = 0
        for path in self.target_dir.iterdir():
            name = path.name
            if name.startswith(".") and ".arrow.tmp." in name:
                _safe_unlink(path)
                n += 1
        if n:
            log.warning("arrow_ipc: removed %d orphan tempfile(s) in %s", n, self.target_dir)
        return n

    def supports_incremental(self) -> bool:
        """False — and structurally, not just as a current limitation.

        An Arrow IPC file ends with a footer carrying the block table, so
        appending in place is not merely unsupported by the writer, it is wrong
        for the format. The daemon's incremental path would otherwise read this
        sink's own previous output back in to merge — the PG-011 pattern. See
        `Sink.supports_incremental`.
        """
        return False


def _to_arrow_table(
    df: pd.DataFrame,
    dictionary_columns: list[str],
    timestamp_unit: str | None = None,
) -> Any:
    import pyarrow as pa

    table = pa.Table.from_pandas(df, preserve_index=False)

    if timestamp_unit is not None:
        table = _cast_timestamps(table, timestamp_unit)

    # Dictionary-encode the configured columns. Explicit config, never
    # cardinality auto-detection: an auto threshold makes the OUTPUT SCHEMA
    # DATA-DEPENDENT, so a column could encode on one pull and land as plain
    # utf8 on the next when its distinct count drifts across the threshold.
    # A consumer that registered the first schema then breaks on the second.
    for name in dictionary_columns:
        if name not in table.column_names:
            raise SinkError(
                f"dictionary_columns names '{name}', which is not in the pulled "
                f"columns: {sorted(table.column_names)}"
            )
        idx = table.schema.get_field_index(name)
        field = table.schema.field(idx)
        if not pa.types.is_string(field.type) and not pa.types.is_large_string(field.type):
            raise SinkError(
                f"dictionary_columns names '{name}' of type {field.type}; "
                f"only string columns can be dictionary-encoded"
            )
        # dictionary(int32, utf8): int32 keys land 8-byte aligned inside the IPC
        # body, which is what lets the consumer point at them instead of copying.
        target_type = pa.dictionary(pa.int32(), pa.utf8())
        table = table.set_column(
            idx, pa.field(name, target_type), table.column(idx).cast(target_type)
        )
    return table


def _cast_timestamps(table: Any, unit: str) -> Any:
    """Normalize every timestamp column to `unit`, refusing to lose precision.

    The cast is SAFE (pyarrow's default), so a source carrying finer resolution
    than `unit` raises `ArrowInvalid` rather than silently truncating. A
    benchmark artifact that quietly dropped sub-microsecond detail would still
    compare equal on every aggregate we check, which is exactly the kind of loss
    that survives a green test run.
    """
    import pyarrow as pa

    for idx, field in enumerate(table.schema):
        if not pa.types.is_timestamp(field.type) or field.type.unit == unit:
            continue
        target = pa.timestamp(unit, tz=field.type.tz)
        try:
            cast = table.column(idx).cast(target)
        except pa.ArrowInvalid as exc:
            raise SinkError(
                f"timestamp_unit='{unit}' would lose precision on column "
                f"'{field.name}' (source {field.type}): {exc}"
            ) from exc
        table = table.set_column(idx, pa.field(field.name, target), cast)
    return table


def _write_ipc_file(table: Any, path: Path) -> None:
    """Persist as an uncompressed multi-block Arrow IPC file (== Feather v2).

    No compression is passed, and none is accepted — see the module docstring
    for why a compression knob would only ever break the consumer.

    `max_chunksize=_BLOCK_ROWS` is what keeps the file multi-block. An empty
    table yields no batches at all, which is a valid IPC file carrying just the
    schema, and is what `write_feather` produced for that case too.

    Imported lazily so unit tests can monkeypatch without requiring pyarrow at
    collection time — the same discipline `core/ramdb_writer._save_ramdb` uses
    for row64tools.
    """
    import pyarrow as pa

    with pa.ipc.new_file(str(path), table.schema) as writer:
        for batch in table.to_batches(max_chunksize=_BLOCK_ROWS):
            writer.write_batch(batch)


def _fsync_path(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(path: Path) -> None:
    """fsync the directory so the rename itself is durable, not just the data."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("arrow_ipc: failed to unlink %s: %s", path, exc)


__all__ = ["ArrowIpcSink"]
