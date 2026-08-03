"""Sink registry. Resolves config `sink.type:` to a Sink class.

Deliberately shaped identically to `drivers/__init__.py`: a name -> class map
plus a `resolve()` that raises with the available names listed. `core/` imports
this module lazily, from inside the function that wires the daemon, and never
imports a concrete sink — so adding a sink requires zero `core/` edits.
"""

from __future__ import annotations

from r64_db_engine.core.sink import Sink
from r64_db_engine.sinks.arrow_ipc import ArrowIpcSink
from r64_db_engine.sinks.ramdb import RamdbSink

SINKS: dict[str, type[Sink]] = {
    RamdbSink.sink_name(): RamdbSink,
    ArrowIpcSink.sink_name(): ArrowIpcSink,
}


def default_sink_name() -> str:
    """The sink used when a config carries no `sink:` block.

    Lives here, not in `core/config.py`, so that core never contains a sink
    name — the same reason `dialect` resolution lives in `drivers/`.
    """
    return RamdbSink.sink_name()


def resolve(name: str) -> type[Sink]:
    try:
        return SINKS[name]
    except KeyError as exc:
        available = ", ".join(sorted(SINKS)) or "(none)"
        raise ValueError(f"unknown sink '{name}' (available: {available})") from exc


__all__ = ["SINKS", "default_sink_name", "resolve"]
