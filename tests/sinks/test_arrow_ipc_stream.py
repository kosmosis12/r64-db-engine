"""ArrowIpcSink streaming entry point — Phase A of the Arrow-native lane.

The contract under test is that a consumer CANNOT TELL which entry point wrote
the artifact. Everything here is aimed at that: same block structure, same
schema policy, same atomicity, and — where the inputs are equivalent — the same
bytes.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pandas as pd
import pytest

pa = pytest.importorskip("pyarrow")
ipc = pytest.importorskip("pyarrow.ipc")

from r64_db_engine.core.sink import SinkError  # noqa: E402
from r64_db_engine.sinks.arrow_ipc import _BLOCK_ROWS, ArrowIpcSink  # noqa: E402
from r64_db_engine.sinks.ramdb import RamdbSink  # noqa: E402


def _sink(tmp_path: Path, **opts) -> ArrowIpcSink:
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    sink = ArrowIpcSink()
    sink.open({"output_dir": str(out), "group": "PostgresSource", **opts})
    return sink


def _read(path: Path):
    return ipc.open_file(pa.memory_map(str(path))).read_all()


def _blocks(path: Path) -> list[int]:
    reader = ipc.open_file(pa.memory_map(str(path)))
    return [reader.get_batch(i).num_rows for i in range(reader.num_record_batches)]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _reader(batches, schema=None):
    schema = schema or batches[0].schema
    return pa.RecordBatchReader.from_batches(schema, batches)


def _ramp(start: int, count: int) -> pa.RecordBatch:
    return pa.record_batch(
        [
            pa.array(range(start, start + count), type=pa.int64()),
            pa.array([float(v) for v in range(start, start + count)], type=pa.float64()),
        ],
        names=["v", "f"],
    )


# --------------------------------------------------------------------------
# Re-chunk: source batch size must NOT dictate block structure
# --------------------------------------------------------------------------


def test_small_source_batches_are_merged_into_full_blocks(tmp_path: Path) -> None:
    """7 x 30000-row source batches must land as 65536-row blocks, not 30000.

    This is the half of the re-chunk requirement that a naive implementation
    fails silently: `Table.to_batches(max_chunksize=N)` SPLITS chunks larger
    than N but never MERGES smaller ones, so simply handing the source's own
    batches to the writer would produce 30000-row blocks while every data
    assertion still passed.
    """
    sink = _sink(tmp_path)
    batches = [_ramp(i, 30_000) for i in range(0, 210_000, 30_000)]

    result = sink.write_stream(_reader(batches), "Ramp")

    assert result.rows_written == 210_000
    assert _blocks(result.path) == [65_536, 65_536, 65_536, 13_392]


def test_one_huge_source_batch_is_split_into_full_blocks(tmp_path: Path) -> None:
    """The brief's case: a source handing 1M-row batches still lands 65536 blocks."""
    sink = _sink(tmp_path)

    result = sink.write_stream(_reader([_ramp(0, 200_000)]), "Huge")

    assert _blocks(result.path) == [65_536, 65_536, 65_536, 3_392]


def test_block_structure_matches_the_batch_path_exactly(tmp_path: Path) -> None:
    """Same logical data through both entry points -> identical block structure."""
    sink = _sink(tmp_path)
    rows = 150_000
    df = pd.DataFrame(
        {"v": pd.Series(range(rows), dtype="int64"), "f": [float(v) for v in range(rows)]}
    )

    batch_path = sink.write(df, "ViaBatch")
    stream_path = sink.write_stream(_reader([_ramp(i, 25_000) for i in range(0, rows, 25_000)]),
                                    "ViaStream").path

    assert _blocks(batch_path) == _blocks(stream_path) == [65_536, 65_536, 18_928]


def test_streaming_matches_the_writer_byte_for_byte(tmp_path: Path) -> None:
    """The strongest form of "meshroad cannot tell which path wrote this".

    Not merely equal data or equal block structure — the same bytes. Compared
    against `_write_ipc_file` on the equivalent whole table, i.e. against the
    batch path's writer given the identical schema, so any future divergence in
    the streaming path's buffering or casting surfaces as a hash mismatch
    rather than as a subtle downstream performance symptom.
    """
    from r64_db_engine.sinks.arrow_ipc import _write_ipc_file

    sink = _sink(tmp_path)
    rows = 150_000
    batches = [_ramp(i, 25_000) for i in range(0, rows, 25_000)]
    table = pa.Table.from_batches(batches)

    stream_path = sink.write_stream(_reader(batches), "S").path
    direct = tmp_path / "direct.arrow"
    _write_ipc_file(table, direct)

    assert _read(stream_path).equals(_read(direct))
    assert _sha(stream_path) == _sha(direct)


def test_pandas_entry_point_adds_schema_metadata_the_arrow_lane_does_not(
    tmp_path: Path,
) -> None:
    """Cross-LANE byte-identity does NOT hold, and the reason is benign.

    `pa.Table.from_pandas` attaches a `b'pandas'` schema-metadata blob
    recording index and dtype provenance. The Arrow lane never goes through
    pandas, so its artifacts carry no such blob. Data buffers, schema (modulo
    metadata) and block structure are identical; the files are not.

    Pinned because it has a direct consequence for later phases:
    verify-by-checksum is valid WITHIN a lane, never ACROSS lanes. An N-cell
    artifact and a P'-cell artifact over identical source rows will differ by
    this blob, and reading that as a fidelity failure would be wrong.
    """
    sink = _sink(tmp_path)
    table = pa.table({"v": pa.array(range(1000), type=pa.int64())})

    batch_path = sink.write(table.to_pandas(), "B")
    stream_path = sink.write_stream(_reader(table.to_batches()), "S").path

    batch_schema = ipc.open_file(pa.memory_map(str(batch_path))).schema
    stream_schema = ipc.open_file(pa.memory_map(str(stream_path))).schema

    assert batch_schema.metadata is not None and b"pandas" in batch_schema.metadata
    assert stream_schema.metadata is None
    # Everything that a consumer actually reads is identical...
    assert batch_schema.remove_metadata().equals(stream_schema.remove_metadata())
    assert _read(batch_path).equals(_read(stream_path))
    assert _blocks(batch_path) == _blocks(stream_path)
    # ...but the files are not byte-identical, and that is expected.
    assert _sha(batch_path) != _sha(stream_path)


def test_empty_stream_writes_a_readable_zero_row_artifact(tmp_path: Path) -> None:
    sink = _sink(tmp_path)
    schema = pa.schema([("v", pa.int64()), ("f", pa.float64())])

    result = sink.write_stream(_reader([], schema=schema), "Nothing")

    assert result.rows_written == 0
    table = _read(result.path)
    assert table.num_rows == 0
    assert table.schema.field("v").type == pa.int64()


def test_zero_row_batches_do_not_produce_empty_blocks(tmp_path: Path) -> None:
    """A source may emit empty batches; they must not become empty IPC blocks."""
    sink = _sink(tmp_path)
    schema = _ramp(0, 1).schema
    batches = [
        pa.record_batch([pa.array([], type=pa.int64()), pa.array([], type=pa.float64())],
                        names=["v", "f"]),
        _ramp(0, 10),
        pa.record_batch([pa.array([], type=pa.int64()), pa.array([], type=pa.float64())],
                        names=["v", "f"]),
    ]

    result = sink.write_stream(_reader(batches, schema=schema), "Sparse")

    assert result.rows_written == 10
    assert _blocks(result.path) == [10]


# --------------------------------------------------------------------------
# The dictionary constraint (known landmine)
# --------------------------------------------------------------------------


def test_two_batch_dictionary_pull_produces_one_unified_dictionary(tmp_path: Path) -> None:
    """Per-batch dictionaries cannot be appended to an IPC FILE.

    pyarrow raises `ArrowInvalid: Dictionary replacement detected` on a naive
    append, so the streaming path must unify. The artifact must carry ONE
    dictionary covering both batches, and every value must survive.
    """
    sink = _sink(tmp_path)
    dt = pa.dictionary(pa.int32(), pa.utf8())
    b1 = pa.record_batch([pa.array(["a", "b"], type=dt)], names=["s"])
    b2 = pa.record_batch([pa.array(["c", "a"], type=dt)], names=["s"])

    result = sink.write_stream(_reader([b1, b2]), "Dict")

    table = _read(result.path)
    assert result.rows_written == 4
    assert pa.types.is_dictionary(table.schema.field("s").type)
    assert table.column("s").to_pylist() == ["a", "b", "c", "a"]
    # One dictionary for the whole artifact, covering values from both batches.
    dictionaries = {tuple(chunk.dictionary.to_pylist()) for chunk in table.column("s").chunks}
    assert len(dictionaries) == 1
    assert set(next(iter(dictionaries))) == {"a", "b", "c"}


def test_configured_dictionary_column_encodes_on_the_streaming_path(tmp_path: Path) -> None:
    """`dictionary_columns` config applies to the streaming path too."""
    sink = _sink(tmp_path, dictionary_columns={"Status": ["status"]})
    batches = [
        pa.record_batch([pa.array(["ok", "fail"], type=pa.utf8())], names=["status"]),
        pa.record_batch([pa.array(["ok", "retry"], type=pa.utf8())], names=["status"]),
    ]

    result = sink.write_stream(_reader(batches), "Status")

    table = _read(result.path)
    assert pa.types.is_dictionary(table.schema.field("status").type)
    assert table.column("status").to_pylist() == ["ok", "fail", "ok", "retry"]


def test_dictionary_artifact_is_block_chunked_like_any_other(tmp_path: Path) -> None:
    """Collect mode must not cost the block discipline.

    Dictionary targets take the collect path, which is the one place the
    streaming buffer is bypassed — so it is exactly where a one-block file
    could sneak back in.
    """
    sink = _sink(tmp_path, dictionary_columns={"Big": ["s"]})
    values = ["a", "b", "c", "d"]
    batches = [
        pa.record_batch(
            [pa.array([values[i % 4] for i in range(start, start + 50_000)], type=pa.utf8())],
            names=["s"],
        )
        for start in range(0, 150_000, 50_000)
    ]

    result = sink.write_stream(_reader(batches), "Big")

    assert result.rows_written == 150_000
    assert _blocks(result.path) == [65_536, 65_536, 18_928]


# --------------------------------------------------------------------------
# Null and NaN must stay distinct on this lane
# --------------------------------------------------------------------------


def test_nan_and_null_survive_distinctly(tmp_path: Path) -> None:
    """The CH-ledger NaN-vs-NULL item, live on the Arrow lane.

    The pandas path CANNOT preserve this: `from_pandas` folds float NaN onto
    the null bitmap, and the module docstring records that as unrecoverable
    because pandas already conflated them upstream. On this lane the values
    arrive as Arrow, so the distinction still exists — and must not be
    destroyed. DuckDB float columns can carry genuine NaN as a value.
    """
    sink = _sink(tmp_path)
    batch = pa.record_batch(
        [pa.array([1.5, float("nan"), None, 4.5], type=pa.float64())], names=["f"]
    )

    result = sink.write_stream(_reader([batch]), "NaN")

    column = _read(result.path).column("f")
    assert column.null_count == 1  # the None only — NOT the NaN
    values = column.to_pylist()
    assert values[0] == 1.5
    assert values[1] != values[1]  # NaN is still NaN, not null
    assert values[2] is None
    assert values[3] == 4.5


def test_nan_is_not_introduced_where_a_null_was(tmp_path: Path) -> None:
    """The converse direction: nulls must not become NaN either."""
    sink = _sink(tmp_path)
    batch = pa.record_batch([pa.array([None, None, 2.0], type=pa.float64())], names=["f"])

    result = sink.write_stream(_reader([batch]), "Nulls")

    column = _read(result.path).column("f")
    assert column.null_count == 2
    assert column.to_pylist() == [None, None, 2.0]


def test_nulls_survive_across_the_rechunk_boundary(tmp_path: Path) -> None:
    """RF-002 through the buffer: re-chunking must not disturb null bitmaps."""
    sink = _sink(tmp_path)
    rows = 100_000
    values = [None if i % 3 == 0 else float(i) for i in range(rows)]
    batches = [
        pa.record_batch([pa.array(values[i : i + 20_000], type=pa.float64())], names=["f"])
        for i in range(0, rows, 20_000)
    ]

    result = sink.write_stream(_reader(batches), "Spanning")

    column = _read(result.path).column("f")
    assert column.null_count == len([v for v in values if v is None])
    assert column.to_pylist() == values


# --------------------------------------------------------------------------
# Schema policy parity
# --------------------------------------------------------------------------


def test_timestamp_unit_is_honored_on_the_streaming_path(tmp_path: Path) -> None:
    sink = _sink(tmp_path, timestamp_unit="us")
    batch = pa.record_batch(
        [pa.array([1_600_000_000_123_456_000, None], type=pa.timestamp("ns"))], names=["t"]
    )

    result = sink.write_stream(_reader([batch]), "Ts")

    table = _read(result.path)
    assert table.schema.field("t").type == pa.timestamp("us")
    assert table.column("t").null_count == 1


def test_timestamp_unit_refuses_to_lose_precision_on_the_streaming_path(
    tmp_path: Path,
) -> None:
    """The safe-cast law: lossy means loud, on both entry points."""
    sink = _sink(tmp_path, timestamp_unit="s")
    batch = pa.record_batch(
        [pa.array([1_600_000_000_123_456_789], type=pa.timestamp("ns"))], names=["t"]
    )

    with pytest.raises(SinkError, match="would lose precision"):
        sink.write_stream(_reader([batch]), "Lossy")


def test_streaming_write_is_atomic_and_leaves_no_tempfile(tmp_path: Path) -> None:
    sink = _sink(tmp_path)

    result = sink.write_stream(_reader([_ramp(0, 100)]), "Atomic")

    assert result.path.name == "Atomic.arrow"
    leftovers = [p.name for p in result.path.parent.iterdir() if ".tmp." in p.name]
    assert leftovers == []


def test_a_failed_streaming_write_leaves_the_previous_artifact_intact(
    tmp_path: Path,
) -> None:
    """Atomicity is the contract both paths owe; a mid-stream failure must not
    replace a good artifact with a partial one."""
    sink = _sink(tmp_path, timestamp_unit="s")
    good = pa.record_batch([pa.array([1, 2, 3], type=pa.int64())], names=["v"])
    path = sink.write_stream(_reader([good]), "Swap").path
    before = _sha(path)

    lossy = pa.record_batch(
        [pa.array([1_600_000_000_123_456_789], type=pa.timestamp("ns"))], names=["t"]
    )
    with pytest.raises(SinkError):
        sink.write_stream(_reader([lossy]), "Swap")

    assert _sha(path) == before
    assert _read(path).column("v").to_pylist() == [1, 2, 3]


# --------------------------------------------------------------------------
# Capability advertisement
# --------------------------------------------------------------------------


def test_arrow_sink_advertises_streaming() -> None:
    assert ArrowIpcSink().supports_streaming() is True


def test_ramdb_sink_does_not_advertise_streaming_and_refuses_by_name() -> None:
    """The default refusal must be loud, not a silent drain-and-write fallback."""
    sink = RamdbSink()
    assert sink.supports_streaming() is False

    with pytest.raises(SinkError, match="does not support streaming writes"):
        sink.write_stream(_reader([_ramp(0, 1)]), "Any")


def test_block_rows_constant_is_the_documented_65536() -> None:
    assert _BLOCK_ROWS == 65_536
