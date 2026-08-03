"""Sink abstraction: the registry, the naming law, and the firewall.

The load-bearing test here is `test_stub_sink_requires_zero_core_changes`. The
sink abstraction exists to make "adding an output format touches no core/ code"
executable rather than aspirational — the same discipline SPEC §3.1 imposes on
drivers.
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from r64_db_engine.core.config import Config
from r64_db_engine.core.sink import Sink, SinkError


# --------------------------------------------------------------------------
# A stub sink defined ENTIRELY in this test file. If satisfying it ever
# requires editing core/, the abstraction has failed.
# --------------------------------------------------------------------------
class NullSink(Sink):
    """Writes nothing; records what it was asked to write."""

    def __init__(self) -> None:
        self.opened_with: dict[str, Any] | None = None
        self.written: list[tuple[str, int]] = []
        self.root = Path("/nonexistent")

    @classmethod
    def sink_name(cls) -> str:
        return "null_stub"

    def open(self, config: dict[str, Any]) -> None:
        self.opened_with = dict(config)
        self.root = Path(config.get("root", "/nonexistent"))

    def ensure_ready(self) -> None:
        pass

    def target_path(self, target: str) -> Path:
        return self.root / f"{target}.null"

    def write(self, df: pd.DataFrame, target: str) -> Path:
        self.written.append((target, len(df)))
        return self.target_path(target)

    def cleanup_orphan_tempfiles(self) -> int:
        return 0


def test_stub_sink_requires_zero_core_changes(tmp_path: Path) -> None:
    """The firewall law for the write side.

    `NullSink` is declared in this test module — core has never heard of it —
    and it satisfies the interface the daemon depends on without a single
    `core/` edit.
    """
    sink = NullSink()
    sink.open({"root": str(tmp_path)})
    sink.ensure_ready()

    path = sink.write(pd.DataFrame({"a": [1, 2, 3]}), "T")

    assert sink.written == [("T", 3)]
    assert path == tmp_path / "T.null"
    assert sink.cleanup_orphan_tempfiles() == 0
    # Defaults to the safe answer without the subclass saying anything.
    assert sink.supports_incremental() is False


def test_core_sink_module_names_zero_sinks() -> None:
    """`core/sink.py` must not enumerate concrete sinks.

    Docstrings may *discuss* names (the module explains why the law exists);
    executable code must not contain them.
    """
    import r64_db_engine.core.sink as sink_mod

    source = Path(sink_mod.__file__).read_text()
    code = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("#")
    )
    # Strip docstrings crudely but adequately: everything between triple quotes.
    parts = code.split('"""')
    executable = "".join(parts[::2])

    for name in ("ramdb", "arrow_ipc", "RamdbSink", "ArrowIpcSink"):
        assert name not in executable, f"core/sink.py names a concrete sink: {name}"


def test_core_does_not_import_concrete_sinks() -> None:
    """Architectural firewall, write side: core/ leaks no sinks.* dependency.

    Mirrors `test_core_does_not_import_postgres_driver`. The registry import in
    `build_daemon` is function-local, exactly as the drivers registry import is,
    so no core module exposes a sink-derived attribute.
    """
    import r64_db_engine.core as core_pkg

    for mod_info in pkgutil.walk_packages(core_pkg.__path__, prefix="r64_db_engine.core."):
        mod = importlib.import_module(mod_info.name)
        for attr in dir(mod):
            obj = getattr(mod, attr, None)
            mod_name = getattr(obj, "__module__", "") or ""
            assert not mod_name.startswith("r64_db_engine.sinks"), (
                f"core module {mod_info.name} leaked a sink dependency via {attr}"
            )


def test_core_config_has_no_sink_literal() -> None:
    """`SinkConfig.type` is a free-form str, not a Literal enumerating sinks.

    A `Literal["ramdb", "arrow_ipc"]` here would clone the PG-010 dialect leak
    onto the output axis.
    """
    from r64_db_engine.core.config import SinkConfig

    cfg = SinkConfig.model_validate({"type": "some_sink_that_does_not_exist", "k": "v"})
    assert cfg.type == "some_sink_that_does_not_exist"
    assert cfg.options() == {"k": "v"}


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------


def test_registry_resolves_known_sinks() -> None:
    from r64_db_engine.sinks import SINKS, default_sink_name, resolve

    assert set(SINKS) >= {"ramdb", "arrow_ipc"}
    assert resolve("ramdb").sink_name() == "ramdb"
    assert resolve("arrow_ipc").sink_name() == "arrow_ipc"
    # The default lives in the registry, not in core.
    assert default_sink_name() in SINKS


def test_registry_rejects_unknown_sink_with_available_list() -> None:
    from r64_db_engine.sinks import resolve

    with pytest.raises(ValueError, match="unknown sink 'nope'"):
        resolve("nope")


# --------------------------------------------------------------------------
# Incremental fail-fast (the PG-011 trap, closed)
# --------------------------------------------------------------------------


def _config(tmp_path: Path, mode: str, sink: dict[str, Any] | None) -> Config:
    loading = tmp_path / "loading"
    loading.mkdir(exist_ok=True)
    table: dict[str, Any] = {"source": "public.t", "target": "T", "mode": mode, "cadence": "5s"}
    if mode == "incremental":
        table["incremental_key"] = "id"
        table["incremental_type"] = "int"
    payload: dict[str, Any] = {
        "dialect": "postgres",
        "postgres": {"database": "x"},
        "row64": {"loading_dir": str(loading), "group": "G"},
        "tables": [table],
        "runtime": {"state_dir": str(tmp_path / "state")},
        "telemetry": {"health_port": 0, "metrics_port": 0},
    }
    if sink is not None:
        payload["sink"] = sink
    return Config.model_validate(payload)


def test_incremental_against_nonappendable_sink_fails_fast(tmp_path: Path) -> None:
    """A non-appendable sink must refuse incremental, not silently downgrade.

    Silently writing a partial snapshot would be indistinguishable, to the
    consumer, from a complete one — strictly worse than refusing to start.
    """
    from r64_db_engine.core.daemon import _reject_incremental_on_nonappendable_sink

    cfg = _config(tmp_path, "incremental", {"type": "null_stub"})
    with pytest.raises(SinkError, match="cannot serve incremental mode"):
        _reject_incremental_on_nonappendable_sink(cfg, NullSink())


def test_full_refresh_against_nonappendable_sink_is_allowed(tmp_path: Path) -> None:
    from r64_db_engine.core.daemon import _reject_incremental_on_nonappendable_sink

    cfg = _config(tmp_path, "full_refresh", {"type": "null_stub"})
    _reject_incremental_on_nonappendable_sink(cfg, NullSink())  # must not raise


def test_incremental_against_appendable_sink_still_allowed(tmp_path: Path) -> None:
    """v0.1 ramdb behaviour is preserved exactly — not tightened in passing."""
    from r64_db_engine.core.daemon import _reject_incremental_on_nonappendable_sink
    from r64_db_engine.sinks.ramdb import RamdbSink

    cfg = _config(tmp_path, "incremental", None)
    sink = RamdbSink()
    sink.open({"loading_dir": str(tmp_path / "loading"), "group": "G"})
    _reject_incremental_on_nonappendable_sink(cfg, sink)  # must not raise


# --------------------------------------------------------------------------
# Backward compatibility
# --------------------------------------------------------------------------


def test_config_without_sink_block_still_validates(tmp_path: Path) -> None:
    """Every pre-sink config keeps working, untouched."""
    cfg = _config(tmp_path, "full_refresh", None)
    assert cfg.sink is None


def test_build_daemon_defaults_to_registry_default_sink(tmp_path: Path) -> None:
    from r64_db_engine.core.daemon import build_daemon
    from r64_db_engine.sinks import default_sink_name

    cfg = _config(tmp_path, "full_refresh", None)
    daemon = build_daemon(cfg)
    assert type(daemon.writer).sink_name() == default_sink_name()
    assert daemon.writer.target_path("T").name == "T.ramdb"


def test_build_daemon_honours_explicit_sink_block(tmp_path: Path) -> None:
    from r64_db_engine.core.daemon import build_daemon

    out = tmp_path / "arrow_out"
    out.mkdir()
    cfg = _config(
        tmp_path,
        "full_refresh",
        {"type": "arrow_ipc", "output_dir": str(out), "group": "G"},
    )
    daemon = build_daemon(cfg)
    assert type(daemon.writer).sink_name() == "arrow_ipc"
    assert daemon.writer.target_path("T").name == "T.arrow"
