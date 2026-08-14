"""ArrowIpcSink — the corrected-lineage receipt, type fidelity, and atomicity."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pandas as pd
import pytest

pa = pytest.importorskip("pyarrow")
feather = pytest.importorskip("pyarrow.feather")

from r64_db_engine.core.sink import SinkError  # noqa: E402
from r64_db_engine.sinks.arrow_ipc import ArrowIpcSink  # noqa: E402

# The exact value from PG-001. Seeded as `duration = 3548933426`, it came back
# from the row64tools 1.0.10 ramdb codec as -746033870 — a silent signed-int32
# truncation. It is the reproducer for the whole reason this sink exists, so it
# is spelled out here rather than generated.
PG001_VALUE = 3548933426
PG001_CORRUPTION = -746033870


def _sink(tmp_path: Path, **opts) -> ArrowIpcSink:
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    sink = ArrowIpcSink()
    sink.open({"output_dir": str(out), "group": "PostgresSource", **opts})
    return sink


# --------------------------------------------------------------------------
# PG-001: the bug class is removed, not guarded
# --------------------------------------------------------------------------


def test_pg001_int64_round_trips_exactly(tmp_path: Path) -> None:
    """3548933426 survives the sink byte-for-byte, because int64 is native.

    The ramdb path cannot store this value at all: `RamdbWriter` raises
    `Row64CodecOverflowError` for it (that guard replaced the silent
    truncation to -746033870). Arrow has no narrowing step to guard, so the
    value simply round-trips.
    """
    sink = _sink(tmp_path)
    df = pd.DataFrame({"duration": pd.Series([PG001_VALUE], dtype="int64")})

    path = sink.write(df, "Int64Exact")

    table = feather.read_table(path)
    assert table.schema.field("duration").type == pa.int64()
    value = table.column("duration")[0].as_py()
    assert value == PG001_VALUE
    assert value != PG001_CORRUPTION


def test_pg001_int64_boundary_values_round_trip(tmp_path: Path) -> None:
    """The whole neighbourhood of the int32 boundary, not just the one value."""
    sink = _sink(tmp_path)
    values = [
        2**31 - 1,  # last value the ramdb codec could hold
        2**31,  # first value it could not
        PG001_VALUE,
        2**63 - 1,  # int64 max
        -(2**63),  # int64 min
        -(2**31) - 1,
    ]
    df = pd.DataFrame({"v": pd.Series(values, dtype="int64")})

    path = sink.write(df, "Bounds")

    assert feather.read_table(path).column("v").to_pylist() == values


def test_sink_carries_no_integer_range_guard(tmp_path: Path) -> None:
    """Writing a >int32 value must not raise. Guarding it would imply a risk
    that does not exist in this format."""
    sink = _sink(tmp_path)
    df = pd.DataFrame({"v": pd.Series([PG001_VALUE] * 1000, dtype="int64")})
    sink.write(df, "NoGuard")  # must not raise


# --------------------------------------------------------------------------
# Type sweep — the v0.1 coercion matrix shapes
# --------------------------------------------------------------------------


def test_type_sweep_lands_with_correct_arrow_types(tmp_path: Path) -> None:
    sink = _sink(tmp_path)
    df = pd.DataFrame(
        {
            "big": pd.Series([PG001_VALUE], dtype="int64"),
            "amount": pd.Series([1234.5678], dtype="float64"),
            "flag": pd.Series([True], dtype="bool"),
            "ts": pd.Series(pd.to_datetime(["2026-05-27T12:34:56Z"], utc=True)),
            "uid": pd.Series([str(uuid.UUID(int=42))], dtype="object"),
            "note": pd.Series(["em—dash, smart “quotes”, café, 🚀"], dtype="object"),
        }
    )

    table = feather.read_table(sink.write(df, "Sweep"))
    schema = table.schema

    assert schema.field("big").type == pa.int64()
    assert schema.field("amount").type == pa.float64()
    assert schema.field("flag").type == pa.bool_()
    assert pa.types.is_timestamp(schema.field("ts").type)
    assert schema.field("ts").type.tz is not None
    uid_type = schema.field("uid").type
    assert pa.types.is_string(uid_type) or pa.types.is_large_string(uid_type)

    # Unicode survives byte-for-byte: Arrow strings are UTF-8 natively, so the
    # ASCII sanitization the ramdb codec forces is not needed here.
    assert table.column("note")[0].as_py() == "em—dash, smart “quotes”, café, 🚀"
    assert table.column("uid")[0].as_py() == str(uuid.UUID(int=42))


def test_nulls_and_nan_survive(tmp_path: Path) -> None:
    sink = _sink(tmp_path)
    df = pd.DataFrame(
        {
            "i": pd.Series([1, None, 3], dtype="Int64"),
            "f": pd.Series([1.5, float("nan"), 3.5], dtype="float64"),
            "s": pd.Series(["a", None, "c"], dtype="object"),
            "t": pd.Series(pd.to_datetime(["2026-01-01", None, "2026-01-03"], utc=True)),
        }
    )

    table = feather.read_table(sink.write(df, "Nulls"))

    assert table.column("i").to_pylist() == [1, None, 3]
    assert table.column("s").to_pylist() == ["a", None, "c"]
    assert table.column("t").to_pylist()[1] is None

    # Float NaN lands as Arrow NULL, not as NaN — a deliberate divergence from
    # the ramdb path (SPEC §6 says "preserve"). pandas has already conflated
    # SQL NULL and 'NaN'::float8 into one float64 NaN before any sink sees the
    # frame; resolving that to null keeps aggregates correct for the common
    # case, where writing NaN would set null_count=0 and poison every sum().
    # See the module docstring in sinks/arrow_ipc.py.
    f = table.column("f")
    assert f.null_count == 1
    assert f.to_pylist() == [1.5, None, 3.5]


def test_empty_dataframe_writes_readable_file(tmp_path: Path) -> None:
    sink = _sink(tmp_path)
    df = pd.DataFrame({"a": pd.Series([], dtype="int64")})
    table = feather.read_table(sink.write(df, "Empty"))
    assert table.num_rows == 0
    assert table.schema.field("a").type == pa.int64()


# --------------------------------------------------------------------------
# Dictionary encoding — explicit config only
# --------------------------------------------------------------------------


def test_dictionary_columns_land_as_dictionary_on_disk(tmp_path: Path) -> None:
    sink = _sink(tmp_path, dictionary_columns={"Dict": ["status"]})
    df = pd.DataFrame(
        {
            "status": ["active", "pending", "active", "closed"] * 250,
            "region": ["West"] * 1000,
        }
    )

    table = feather.read_table(sink.write(df, "Dict"))

    status_type = table.schema.field("status").type
    assert pa.types.is_dictionary(status_type)
    assert status_type.index_type == pa.int32()
    assert pa.types.is_string(status_type.value_type)
    # The unconfigured column stays plain — no auto-detection anywhere.
    # Plain text may land as `string` or `large_string` depending on the pandas
    # dtype the driver produced (pandas 3.0's native `str` dtype maps to
    # large_string). Both are correct and meshroad reads both; the proven
    # perf_1m.arrow artifact carries LargeUtf8 text columns for this reason.
    region_type = table.schema.field("region").type
    assert pa.types.is_string(region_type) or pa.types.is_large_string(region_type)
    assert not pa.types.is_dictionary(region_type)
    assert table.column("status").combine_chunks().dictionary_decode().to_pylist()[:4] == [
        "active",
        "pending",
        "active",
        "closed",
    ]


def test_dictionary_columns_rejects_unknown_column(tmp_path: Path) -> None:
    sink = _sink(tmp_path, dictionary_columns={"D": ["nope"]})
    with pytest.raises(SinkError, match="not in the pulled columns"):
        sink.write(pd.DataFrame({"a": [1]}), "D")


def test_dictionary_columns_rejects_non_string_column(tmp_path: Path) -> None:
    sink = _sink(tmp_path, dictionary_columns={"D": ["a"]})
    with pytest.raises(SinkError, match="only string columns"):
        sink.write(pd.DataFrame({"a": [1, 2]}), "D")


# --------------------------------------------------------------------------
# Configuration refusals
# --------------------------------------------------------------------------


def test_compression_option_is_refused_not_ignored(tmp_path: Path) -> None:
    """A knob whose only reachable effect is to break the consumer's zero-copy
    path must not be silently accepted."""
    sink = ArrowIpcSink()
    with pytest.raises(SinkError, match="does not support 'compression'"):
        sink.open({"output_dir": str(tmp_path), "compression": "zstd"})


def test_output_dir_is_required(tmp_path: Path) -> None:
    with pytest.raises(SinkError, match="requires 'output_dir'"):
        ArrowIpcSink().open({})


def test_missing_output_dir_raises_rather_than_creating(tmp_path: Path) -> None:
    sink = ArrowIpcSink()
    sink.open({"output_dir": str(tmp_path / "absent"), "group": "G"})
    with pytest.raises(FileNotFoundError):
        sink.write(pd.DataFrame({"a": [1]}), "T")


def test_sink_declares_no_incremental_support() -> None:
    assert ArrowIpcSink.sink_name() == "arrow_ipc"
    assert ArrowIpcSink().supports_incremental() is False


# --------------------------------------------------------------------------
# Atomicity
# --------------------------------------------------------------------------


def test_write_leaves_no_tempfiles(tmp_path: Path) -> None:
    sink = _sink(tmp_path)
    sink.write(pd.DataFrame({"a": [1, 2, 3]}), "T")
    leftovers = [p.name for p in sink.target_dir.iterdir() if ".arrow.tmp." in p.name]
    assert leftovers == []


def test_failed_write_leaves_no_tempfile_and_no_partial_output(tmp_path: Path) -> None:
    sink = _sink(tmp_path, dictionary_columns={"T": ["missing"]})
    with pytest.raises(SinkError):
        sink.write(pd.DataFrame({"a": [1]}), "T")
    assert not sink.target_path("T").exists()
    assert [p.name for p in sink.target_dir.iterdir() if ".arrow.tmp." in p.name] == []


def test_reader_holding_old_file_sees_consistent_data_across_swap(tmp_path: Path) -> None:
    """The POSIX rename contract, asserted rather than assumed.

    A reader that opened the previous generation keeps reading the previous
    generation's bytes through the swap — the inode it holds is unlinked from
    the name, not mutated.
    """
    sink = _sink(tmp_path)
    first = sink.write(pd.DataFrame({"v": pd.Series([1, 2, 3], dtype="int64")}), "Swap")

    # Hold the old generation open, as a consumer mid-scan would.
    held = os.open(first, os.O_RDONLY)
    try:
        old_inode = os.fstat(held).st_ino

        sink.write(pd.DataFrame({"v": pd.Series([9, 9, 9, 9], dtype="int64")}), "Swap")

        # Same path, new inode -> the swap really was a rename, not a rewrite.
        assert os.stat(sink.target_path("Swap")).st_ino != old_inode
        # The held descriptor still describes the old, complete file.
        assert os.fstat(held).st_size > 0
        with open(held, "rb", closefd=False) as fh:
            fh.seek(0)
            assert fh.read(6)[:6] == b"ARROW1"
    finally:
        os.close(held)

    assert feather.read_table(sink.target_path("Swap")).column("v").to_pylist() == [9, 9, 9, 9]


def test_cleanup_orphan_tempfiles_removes_only_tempfiles(tmp_path: Path) -> None:
    sink = _sink(tmp_path)
    real = sink.write(pd.DataFrame({"a": [1]}), "Keep")
    orphan = sink.target_dir / ".Keep.arrow.tmp.deadbeef"
    orphan.write_bytes(b"partial")

    assert sink.cleanup_orphan_tempfiles() == 1
    assert not orphan.exists()
    assert real.exists()


def test_written_file_is_uncompressed_and_mmap_readable(tmp_path: Path) -> None:
    """Uncompressed is the whole consumer contract — assert the bytes, not the call."""
    sink = _sink(tmp_path)
    path = sink.write(pd.DataFrame({"v": pd.Series(range(100_000), dtype="int64")}), "Big")

    # An uncompressed int64 column of 100k rows must occupy ~800 KB on disk.
    # A compressed body would be dramatically smaller for this ramp data.
    assert path.stat().st_size > 700_000

    with pa.memory_map(str(path), "rb") as source:
        table = feather.read_table(source)
        assert table.column("v").to_pylist()[:3] == [0, 1, 2]


# --------------------------------------------------------------------------
# timestamp_unit — opt-in normalization, lossless or loud
# --------------------------------------------------------------------------


def test_timestamp_unit_us_matches_the_meshroad_reference_artifact(tmp_path: Path) -> None:
    """DateTime64(6) data lands as timestamp[us], not timestamp[ns].

    pandas has no microsecond-native path here: it carries datetime64[ns], so
    `from_pandas` produces timestamp[ns] while the proven perf_1m.arrow
    artifact meshroad serves carries timestamp[us]. Microsecond source data
    only became nanoseconds by passing through pandas, so casting it back is
    exact — asserted on the value, not just the type.
    """
    sink = _sink(tmp_path, timestamp_unit="us")
    df = pd.DataFrame(
        {"event_time": pd.to_datetime(["2026-01-01 00:00:00.123456", "2026-06-30 23:59:59.999999"])}
    )

    table = feather.read_table(sink.write(df, "Ts"))

    assert table.schema.field("event_time").type == pa.timestamp("us")
    assert [str(v) for v in table.column("event_time").to_pylist()] == [
        "2026-01-01 00:00:00.123456",
        "2026-06-30 23:59:59.999999",
    ]


def test_timestamp_unit_default_leaves_resolution_untouched(tmp_path: Path) -> None:
    """No `timestamp_unit` means no cast — the knob is opt-in.

    A blanket ns->us default would silently truncate any source that genuinely
    has nanosecond resolution, so the absence of config must change nothing.
    """
    sink = _sink(tmp_path)
    df = pd.DataFrame({"event_time": pd.to_datetime(["2026-01-01 00:00:00.123456789"])})

    table = feather.read_table(sink.write(df, "TsDefault"))

    assert table.schema.field("event_time").type == pa.timestamp("ns")


def test_timestamp_unit_refuses_to_truncate_precision(tmp_path: Path) -> None:
    """A lossy cast raises instead of quietly rounding.

    Sub-microsecond detail dropped here would still compare equal on every
    aggregate the benchmark checks — precisely the loss that survives a green
    test run — so the cast is SAFE and the failure is loud.
    """
    sink = _sink(tmp_path, timestamp_unit="us")
    df = pd.DataFrame({"event_time": pd.to_datetime(["2026-01-01 00:00:00.123456789"])})

    with pytest.raises(SinkError, match="would lose precision"):
        sink.write(df, "TsLossy")


def test_timestamp_unit_rejects_a_bogus_unit(tmp_path: Path) -> None:
    with pytest.raises(SinkError, match="must be one of"):
        _sink(tmp_path, timestamp_unit="fortnights")
