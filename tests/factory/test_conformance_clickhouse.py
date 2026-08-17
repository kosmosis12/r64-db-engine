"""The battery, end to end, against the live meshbench ClickHouse container.

Nothing is stubbed. This is the same run Gate F1 is ratified on, executed
through the CLI's own entry point so that the argument handling, the spec
resolution, the two pulls, the live probe and the evidence writer are all
exercised as an operator would exercise them.

Run with:
    .venv/bin/pytest tests/factory/test_conformance_clickhouse.py --integration -s

Requires the `meshroad-ch` container UP with `meshbench.perf_1m` at 1,000,000
rows (`bench/make-dataset.sh`). The serve-gate variant additionally requires
the `meshroad` binary and a free 127.0.0.1:8903.

Note on ports: the gate binds **8903**, never 8802. :8802 is the plane of
record. Teardown here is by PID from `factory/serve_gate.py`, never by pattern.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from factory import conformance, serve_gate

pytestmark = pytest.mark.integration

TARGET = conformance.REPO_ROOT / "factory" / "targets" / "clickhouse-meshbench.yaml"
GROUND_TRUTH = conformance.REPO_ROOT / "bench" / "GROUND-TRUTH-clickhouse.json"


def _argv(tmp_path: Path, *extra: str) -> list[str]:
    return [
        "--dialect", "clickhouse",
        "--config", str(TARGET),
        "--ground-truth", str(GROUND_TRUTH),
        "--table", "perf_1m",
        "--evidence-dir", str(tmp_path / "evidence"),
        "--work-dir", str(tmp_path / "work"),
        "--date", "19700101",
        *extra,
    ]


@pytest.fixture(scope="module")
def green_run(tmp_path_factory) -> tuple[int, dict]:
    """One full battery run without the serve gate, shared across assertions."""
    tmp_path = tmp_path_factory.mktemp("conformance")
    code = conformance.main(_argv(tmp_path))
    pack = json.loads((tmp_path / "evidence" / "EVIDENCE-clickhouse-19700101.json").read_text())
    return code, pack


def test_battery_is_green_end_to_end(green_run) -> None:
    code, pack = green_run
    failures = [c["name"] for c in pack["checks"] if c["status"] == "FAIL"]
    assert failures == [], f"failed checks: {failures}"
    assert pack["verdict"] == "PASS"
    assert code == 0


def test_every_check_ran(green_run) -> None:
    """The battery must not quietly shrink. Ten named checks, in order."""
    _, pack = green_run
    assert [c["name"] for c in pack["checks"]] == [
        "registry_admission",
        "schema_exactness",
        "aggregate_parity",
        "rf002_null_discriminator",
        "b2_boundary",
        "pg011_refusal",
        "block_structure",
        "checksum",
        "recipe_security_invariants",
        "zero_copy_serve_gate",
    ]


def test_only_the_serve_gate_and_recipe_security_are_skipped_without_the_flag(green_run) -> None:
    """`recipe_security_invariants` is skipped here with a reason — clickhouse
    is not a recipe-lane dialect, so there is no book to mutate. The battery is
    kept uniform across dialects rather than shorter for some, so a missing
    check reads as a shrunken battery instead of as a different lane."""
    _, pack = green_run
    skipped = {c["name"] for c in pack["checks"] if c["status"] == "SKIPPED"}
    assert skipped == {"zero_copy_serve_gate", "recipe_security_invariants"}
    for check in pack["checks"]:
        if check["status"] == "SKIPPED":
            assert check["detail"], f"{check['name']} skipped without a reason"


def test_the_run_actually_moved_a_million_rows(green_run) -> None:
    """Guards against a battery that passes every check on an empty artifact."""
    _, pack = green_run
    assert pack["artifact"]["rows"] == 1_000_000
    assert pack["artifact"]["blocks"] == 16


def test_two_pulls_were_byte_identical(green_run) -> None:
    _, pack = green_run
    assert pack["artifact"]["sha256_pull1"] == pack["artifact"]["sha256_pull2"]


def test_rf002_discriminated_on_real_nulls(green_run) -> None:
    """Not a vacuous pass: `score` really does carry 20,039 nulls at 1M."""
    _, pack = green_run
    rf = next(c for c in pack["checks"] if c["name"] == "rf002_null_discriminator")
    counts = [c for c in rf["comparisons"] if c["label"] == "score: artifact null_count"]
    assert counts and counts[0]["actual"] == 20039


def test_b2_recorded_the_source_session_timezone(green_run) -> None:
    """A matching pair of bounds means something different when the source
    session is UTC than when it is not, so the pack records which it was."""
    _, pack = green_run
    b2 = next(c for c in pack["checks"] if c["name"] == "b2_boundary")
    assert b2["observations"]["source_timezone"] == "UTC"
    assert b2["queries"], "the live source must have been queried"


def test_the_pack_records_the_environment_that_could_change_the_artifact(green_run) -> None:
    _, pack = green_run
    env = pack["environment"]
    assert env["packages"]["pyarrow"] == "25.0.0"
    assert env["container"]["name"] == "meshroad-ch"


def test_the_pack_contains_no_credentials(green_run) -> None:
    """Credential law, asserted on the artifact that actually leaves the machine."""
    _, pack = green_run
    text = json.dumps(pack).lower()
    assert "password" not in text
    assert "x-clickhouse-key" not in text


def test_the_human_pack_leads_with_a_verdict(tmp_path_factory) -> None:
    tmp_path = tmp_path_factory.mktemp("conformance_md")
    conformance.main(_argv(tmp_path))
    md = (tmp_path / "evidence" / "EVIDENCE-clickhouse-19700101.md").read_text()
    assert "VERDICT: PASS" in md.splitlines()[2]


# ---------------------------------------------------------------------------
# Serve gate
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not Path(serve_gate.DEFAULT_BINARY).exists(),
    reason=f"meshroad binary not present at {serve_gate.DEFAULT_BINARY}",
)
def test_serve_gate_is_green_and_leaves_no_process_behind(tmp_path: Path) -> None:
    code = conformance.main(_argv(tmp_path, "--serve-gate"))
    pack = json.loads((tmp_path / "evidence" / "EVIDENCE-clickhouse-19700101.json").read_text())
    gate = next(c for c in pack["checks"] if c["name"] == "zero_copy_serve_gate")

    assert gate["status"] == "PASS", gate["detail"]
    assert code == 0

    obs = gate["observations"]
    assert obs["cold_delta"]["copied_columns"] == 0
    assert obs["warm_delta"]["copied_columns"] == 0
    assert obs["warm_delta"]["columns_decoded"] == 0
    assert obs["cold_delta"]["columns_decoded"] > 0
    # The counters must be deltas, not raw cumulative snapshots: a cumulative
    # warm reading would carry the cold pass's misses and never reach zero.
    assert obs["warm_delta"]["cache_misses"] == 0

    # The pidfile is removed on the way out, and the ephemeral serve is gone.
    assert not (tmp_path / "work" / "arrow_out" / "factory-serve.pid").exists()
    assert not _port_in_use(8903), "the ephemeral serve outlived the gate"


def _port_in_use(port: int) -> bool:
    import socket

    with socket.socket() as sock:
        return sock.connect_ex(("127.0.0.1", port)) == 0


def test_serve_gate_tears_down_by_pid_even_when_the_workload_raises(tmp_path: Path) -> None:
    """The `finally` is the point: a gate that failed mid-measurement must not
    leave an orphan holding the port. Teardown is by the PID we started and
    never by pattern — a pattern kill here would reach :8802."""
    artifact = tmp_path / "tiny.arrow"
    _write_tiny_artifact(artifact)

    with pytest.raises(RuntimeError, match="deliberate"), serve_gate.ephemeral_serve(
        artifact,
        "tiny",
        "SELECT * FROM tiny",
        addr="127.0.0.1:8904",
        pidfile=tmp_path / "serve.pid",
        log_path=tmp_path / "serve.log",
    ):
        raise RuntimeError("deliberate failure inside the gate")

    assert not (tmp_path / "serve.pid").exists()
    assert not _port_in_use(8904)


def _write_tiny_artifact(path: Path) -> None:
    pa = pytest.importorskip("pyarrow")
    import pyarrow.ipc as ipc

    table = pa.table({"a": pa.array([1, 2, 3], type=pa.int64())})
    with ipc.new_file(str(path), table.schema) as writer:
        writer.write_table(table)


def test_a_poisoned_ground_truth_makes_the_battery_fail(tmp_path: Path) -> None:
    """The oracle proven able to fail on the REAL pipeline, not just in unit
    fixtures. A mutated COPY of the ground truth — never the committed file —
    must turn a green run red.

    Without this, "conformance passed" would be indistinguishable from
    "conformance cannot fail here".
    """
    import hashlib

    digest_before = hashlib.sha256(GROUND_TRUTH.read_bytes()).hexdigest()

    poisoned = tmp_path / "GROUND-TRUTH-poisoned.json"
    doc = json.loads(GROUND_TRUTH.read_text())
    doc["tables"]["perf_1m"]["sum_quantity"] += 1
    poisoned.write_text(json.dumps(doc))

    argv = _argv(tmp_path)
    argv[argv.index("--ground-truth") + 1] = str(poisoned)
    code = conformance.main(argv)

    pack = json.loads((tmp_path / "evidence" / "EVIDENCE-clickhouse-19700101.json").read_text())
    parity = next(c for c in pack["checks"] if c["name"] == "aggregate_parity")
    assert parity["status"] == "FAIL"
    assert pack["verdict"] == "FAIL"
    assert code == 1

    # Exactly one aggregate moved, and it is the one that was poisoned. A run
    # that failed for some unrelated reason would prove nothing about the
    # oracle's sensitivity.
    mismatched = [c["label"] for c in parity["comparisons"] if not c["ok"]]
    assert mismatched == ["sum_quantity"]

    # The committed ground truth is read-only to this battery. The poisoned
    # copy lives in tmp; the real file must be byte-unchanged by the run.
    assert hashlib.sha256(GROUND_TRUTH.read_bytes()).hexdigest() == digest_before
