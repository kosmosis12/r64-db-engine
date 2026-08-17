"""The zero-copy serve gate: spin an ephemeral meshroad serve, read its counters.

# Process discipline (non-negotiable)

The serve started here is EPHEMERAL and is torn down by **PID, explicitly**,
from a `finally` block that runs on every exit path including failure. Its PID
is written to a pidfile first, so an interrupted run leaves a record of what to
clean up rather than an orphan nobody can name.

There is no pattern-kill anywhere in this module, and there must never be. A
`pkill meshroad` would take down the plane of record on :8802 along with the
ephemeral serve — a production process killed as a side effect of a test is
exactly the class of accident this discipline exists to prevent. The default
port here is 8903 for the same reason: it is not 8802, and it is not adjacent
to it.

# Counters, not timings

Everything judged comes from the server's own instrumentation, read out of
`get_flight_info` app_metadata. The harness measures nothing itself. This is
also why the gate carries no timing claim: bench doctrine requires a
root-quiesced machine, and none of this is that.

The counters are CUMULATIVE over the server's lifetime, so this module returns
DELTAS around each pass. A raw warm snapshot includes the cold pass's misses and
would report a 50% miss rate on a perfectly warm cache — the single most likely
way to misread this instrument.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

DEFAULT_ADDR = "127.0.0.1:8903"
DEFAULT_BINARY = "/usr/local/bin/meshroad"

# Counters read out of app_metadata. Anything absent from a snapshot is treated
# as 0 so a server build that adds or drops a counter degrades to a reported
# mismatch rather than a KeyError mid-gate.
COUNTER_KEYS = (
    "cache_hits",
    "cache_misses",
    "columns_decoded",
    "blocks_assembled",
    "zero_copy_columns",
    "copied_columns",
    "bytes_read",
    "bytes_served_cached",
)


class ServeGateError(RuntimeError):
    """The serve gate could not be run. Never downgraded to a silent pass."""


@contextmanager
def ephemeral_serve(
    artifact: Path,
    table: str,
    ready_sql: str,
    *,
    addr: str = DEFAULT_ADDR,
    binary: str = DEFAULT_BINARY,
    pidfile: Path | None = None,
    log_path: Path | None = None,
    startup_timeout_s: float = 30.0,
) -> Iterator[int]:
    """Run `meshroad serve` for the duration of the block. Yields its PID.

    Torn down by PID in the `finally`, SIGTERM first and SIGKILL only if the
    process is still alive after a grace period.
    """
    if not Path(binary).exists():
        raise ServeGateError(f"meshroad binary not found at {binary}")
    if not artifact.exists():
        raise ServeGateError(f"artifact not found: {artifact}")

    log_handle = open(log_path, "wb") if log_path else subprocess.DEVNULL  # noqa: SIM115
    proc = subprocess.Popen(
        [binary, "serve", "--file", str(artifact), "--table", table, "--addr", addr],
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    if pidfile is not None:
        pidfile.write_text(f"{proc.pid}\n")

    try:
        _await_ready(addr, proc, startup_timeout_s, log_path, ready_sql)
        yield proc.pid
    finally:
        _terminate(proc)
        if log_handle is not subprocess.DEVNULL:
            log_handle.close()
        if pidfile is not None:
            pidfile.unlink(missing_ok=True)


def _terminate(proc: subprocess.Popen) -> None:
    """SIGTERM the exact PID, then SIGKILL that same PID if it survives.

    `os.kill(proc.pid, ...)` and never a name- or pattern-based kill: the only
    process this function may ever stop is the one it started.
    """
    if proc.poll() is not None:
        return
    try:
        os.kill(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.kill(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        proc.wait(timeout=10)


def _await_ready(
    addr: str, proc: subprocess.Popen, timeout_s: float, log_path: Path | None, sql: str
) -> None:
    """Poll until the server answers the call the gate will actually make.

    Readiness is probed with `get_flight_info` on the real workload descriptor,
    not with an admin RPC, because two earlier probes each failed for their own
    reason and both failures were misleading:

    - `list_flights()` alone returns a LAZY generator and never touches the
      socket, so it reported "ready" instantly against a server that was not
      listening; the real failure then surfaced one call later as a bewildering
      connection-refused in the middle of the measurement.
    - `list(list_flights())` does connect, but this meshroad build answers it
      `UNIMPLEMENTED` — a fully-serving server looked permanently unready.

    Probing with the call under test has neither failure mode: it connects, and
    a build that can answer it is by definition ready for the gate. It does not
    perturb the counters (verified: a fresh server reports all-zero counters
    after one `get_flight_info`), and the baseline snapshot is taken after this
    returns regardless, so the delta model would absorb it even if it did.
    """
    import pyarrow.flight as fl

    deadline = time.monotonic() + timeout_s
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            tail = ""
            if log_path and log_path.exists():
                tail = log_path.read_text(errors="replace")[-2000:]
            raise ServeGateError(
                f"meshroad serve exited during startup with code {proc.returncode}. Log:\n{tail}"
            )
        try:
            client = fl.FlightClient(f"grpc://{addr}")
            client.get_flight_info(fl.FlightDescriptor.for_command(sql.encode()))
            return
        except Exception as exc:  # noqa: BLE001 - not up yet is the common case
            last_error = exc
            time.sleep(0.25)
    raise ServeGateError(f"meshroad serve did not become ready on {addr} in {timeout_s}s: {last_error}")


def _snapshot(client: Any, sql: str) -> dict[str, int]:
    """Current cumulative counters. Does not itself perturb them.

    Verified rather than assumed: on a freshly started server a `get_flight_info`
    returns all-zero counters, so planning a query costs no decode.
    """
    import pyarrow.flight as fl

    info = client.get_flight_info(fl.FlightDescriptor.for_command(sql.encode()))
    if not info.app_metadata:
        raise ServeGateError(
            "get_flight_info returned no app_metadata; this meshroad build does not expose "
            "the counters the gate reads. Refusing to report a pass without them."
        )
    raw = json.loads(info.app_metadata)
    return {k: int(raw.get(k, 0)) for k in COUNTER_KEYS}


def _run_workload(client: Any, sql: str) -> int:
    import pyarrow.flight as fl

    info = client.get_flight_info(fl.FlightDescriptor.for_command(sql.encode()))
    rows = 0
    for endpoint in info.endpoints:
        rows += client.do_get(endpoint.ticket).read_all().num_rows
    return rows


def measure(
    artifact: Path,
    table: str,
    sql: str,
    *,
    addr: str = DEFAULT_ADDR,
    binary: str = DEFAULT_BINARY,
    pidfile: Path | None = None,
    log_path: Path | None = None,
) -> dict[str, Any]:
    """Cold and warm counter DELTAS for one workload against one artifact.

    Three snapshots — baseline, post-cold, post-warm — because the counters are
    cumulative. `cold = post_cold - baseline`, `warm = post_warm - post_cold`.
    """
    import pyarrow.flight as fl

    with ephemeral_serve(
        artifact, table, sql, addr=addr, binary=binary, pidfile=pidfile, log_path=log_path
    ) as pid:
        client = fl.FlightClient(f"grpc://{addr}")
        baseline = _snapshot(client, sql)
        cold_rows = _run_workload(client, sql)
        after_cold = _snapshot(client, sql)
        warm_rows = _run_workload(client, sql)
        after_warm = _snapshot(client, sql)

    cold = {k: after_cold[k] - baseline[k] for k in COUNTER_KEYS}
    warm = {k: after_warm[k] - after_cold[k] for k in COUNTER_KEYS}
    return {
        "sql": sql,
        "addr": addr,
        "pid": pid,
        "baseline": baseline,
        "cold": cold,
        "warm": warm,
        "cold_rows": cold_rows,
        "warm_rows": warm_rows,
    }


__all__ = [
    "COUNTER_KEYS",
    "DEFAULT_ADDR",
    "DEFAULT_BINARY",
    "ServeGateError",
    "ephemeral_serve",
    "measure",
]
