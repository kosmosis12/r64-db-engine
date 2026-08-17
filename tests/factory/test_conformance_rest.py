"""The recipe lane, end to end, against the live open-meteo API.

Nothing is stubbed and nothing is authenticated: both endpoints are public, so
this whole file runs with zero credentials by design. That is why open-meteo
was chosen to prove the lane — a session that needs no secret cannot leak one.

This is the run Gate F3 is ratified on.

Run with:
    .venv/bin/pytest tests/factory/test_conformance_rest.py --integration -s

Requires outbound HTTPS to geocoding-api.open-meteo.com and
archive-api.open-meteo.com.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from factory import conformance

pytestmark = pytest.mark.integration

TARGET = conformance.REPO_ROOT / "factory" / "targets" / "rest-openmeteo.yaml"
GROUND_TRUTH = conformance.REPO_ROOT / "bench" / "GROUND-TRUTH-openmeteo.json"
TABLE = "open_meteo_berlin_hourly"


def _argv(tmp_path: Path, *extra: str) -> list[str]:
    return [
        "--dialect", "rest",
        "--config", str(TARGET),
        "--ground-truth", str(GROUND_TRUTH),
        "--table", TABLE,
        "--evidence-dir", str(tmp_path / "evidence"),
        "--work-dir", str(tmp_path / "work"),
        "--date", "19700101",
        *extra,
    ]


@pytest.fixture(scope="module")
def green_run(tmp_path_factory) -> tuple[int, dict]:
    tmp_path = tmp_path_factory.mktemp("rest_conformance")
    code = conformance.main(_argv(tmp_path, "--serve-gate"))
    pack = json.loads((tmp_path / "evidence" / "EVIDENCE-rest-19700101.json").read_text())
    return code, pack


def test_the_reduced_battery_is_green(green_run) -> None:
    code, pack = green_run
    failures = [c["name"] for c in pack["checks"] if c["status"] == "FAIL"]
    assert failures == [], f"failed checks: {failures}"
    assert pack["verdict"] == "PASS"
    assert code == 0


def test_rf002_skips_with_a_stated_reason_rather_than_passing_vacuously(green_run) -> None:
    """The whole point of the declaration rule. This window genuinely has no
    nullable column, and 'there is nothing to check' must read differently from
    'nothing was checked'."""
    _, pack = green_run
    rf = next(c for c in pack["checks"] if c["name"] == "rf002_null_discriminator")
    assert rf["status"] == "SKIPPED"
    assert "zero nulls" in rf["detail"]


def test_the_artifact_carries_the_expected_shape(green_run) -> None:
    _, pack = green_run
    assert pack["artifact"]["rows"] == 2160
    # 2160 < 65536, so one block is the CORRECT layout here, not a collapsed one.
    assert pack["artifact"]["blocks"] == 1


def test_two_pulls_of_a_live_api_are_byte_identical(green_run) -> None:
    """Only possible because the book pins a FIXED historical window against the
    archive endpoint. A forecast, or a relative window, could never satisfy this."""
    _, pack = green_run
    assert pack["artifact"]["sha256_pull1"] == pack["artifact"]["sha256_pull2"]


def test_b2_boundary_ran_against_the_live_api_and_recorded_the_provider_timezone(green_run) -> None:
    _, pack = green_run
    b2 = next(c for c in pack["checks"] if c["name"] == "b2_boundary")
    assert b2["status"] == "PASS"
    assert b2["observations"]["source_timezone"] in {"GMT", "UTC"}
    assert any("archive-api.open-meteo.com" in q for q in b2["queries"])


def test_every_security_mutation_was_refused(green_run) -> None:
    """Gate F3's security half, asserted on the SHIPPED book's real hosts."""
    _, pack = green_run
    check = next(c for c in pack["checks"] if c["name"] == "recipe_security_invariants")
    assert check["status"] == "PASS"

    labels = {c["label"] for c in check["comparisons"]}
    assert any("https->http" in label for label in labels)
    assert any("lookalike host evil-" in label for label in labels)
    assert any("loopback" in label for label in labels)
    assert any("templated url" in label for label in labels)
    assert any("undeclared" in label for label in labels)
    # Every recorded outcome must be a refusal — that is the pass condition.
    assert all(c["actual"] == "REFUSED" for c in check["comparisons"])


def test_the_pack_contains_no_credentials(green_run) -> None:
    _, pack = green_run
    text = json.dumps(pack).lower()
    assert "password" not in text
    assert "api-key" not in text
    assert "authorization" not in text


def test_the_serve_gate_holds_on_a_single_block_artifact(green_run) -> None:
    """copied_columns must be 0 whether the file has one block or sixteen."""
    _, pack = green_run
    gate = next(c for c in pack["checks"] if c["name"] == "zero_copy_serve_gate")
    assert gate["status"] == "PASS"
    assert gate["observations"]["cold_delta"]["copied_columns"] == 0
    assert gate["observations"]["warm_delta"]["copied_columns"] == 0
    assert gate["observations"]["warm_delta"]["columns_decoded"] == 0


def test_a_poisoned_ground_truth_makes_the_rest_battery_fail(tmp_path: Path) -> None:
    """The oracle proven able to fail on the recipe lane too, on the real
    pipeline rather than in a unit fixture. The committed ground truth is never
    touched — the poison goes into a tmp copy."""
    import hashlib

    digest_before = hashlib.sha256(GROUND_TRUTH.read_bytes()).hexdigest()

    poisoned = tmp_path / "poisoned.json"
    doc = json.loads(GROUND_TRUTH.read_text())
    doc["tables"][TABLE]["scaled_temp_sum_exact_int"] += 1
    poisoned.write_text(json.dumps(doc))

    argv = _argv(tmp_path)
    argv[argv.index("--ground-truth") + 1] = str(poisoned)
    code = conformance.main(argv)

    pack = json.loads((tmp_path / "evidence" / "EVIDENCE-rest-19700101.json").read_text())
    parity = next(c for c in pack["checks"] if c["name"] == "aggregate_parity")
    assert parity["status"] == "FAIL"
    assert code == 1
    mismatched = [c["label"] for c in parity["comparisons"] if not c["ok"]]
    assert mismatched == ["scaled_temp_sum_exact_int"]

    assert hashlib.sha256(GROUND_TRUTH.read_bytes()).hexdigest() == digest_before


def test_zero_core_edits_for_the_rest_dialect() -> None:
    """PG-010 repeated on a non-database source class, asserted rather than
    asserted-about. `rest` is one entry in the driver registry and nothing in
    core knows the word exists."""
    import subprocess

    result = subprocess.run(
        ["git", "grep", "-rniE", r"\brest\b", "--", "src/r64_db_engine/core/"],
        cwd=conformance.REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.stdout.strip() == "", f"core mentions the rest dialect:\n{result.stdout}"
