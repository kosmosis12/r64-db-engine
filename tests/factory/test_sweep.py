"""The maintenance loop end to end: the sweep, the repair brief, the units.

The systemd tests are fast and unconditional — they parse and verify the unit
files that Kos will install with sudo, so a typo in an ExecStart is caught here
rather than at 04:00 on a Sunday by a timer that silently never ran.

The sweep tests are integration-marked: a full sweep pulls a million rows twice
against the live container, plus the recipe lane against the live API.
"""

from __future__ import annotations

import configparser
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from factory import conformance

SYSTEMD_DIR = conformance.REPO_ROOT / "factory" / "systemd"
SERVICE = SYSTEMD_DIR / "r64-factory-conformance.service"
TIMER = SYSTEMD_DIR / "r64-factory-conformance.timer"
SWEEP = conformance.REPO_ROOT / "factory" / "bin" / "factory-conformance-sweep"


def _unit(path: Path) -> configparser.ConfigParser:
    # interpolation=None because systemd specifiers are %-prefixed (`%n`, `%i`,
    # `%H`) and configparser's default interpolation treats `%` as its own
    # syntax, so `OnFailure=ntfy-fail@%n.service` raises on read.
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    # systemd keys are case-sensitive; configparser lowercases by default.
    parser.optionxform = str  # type: ignore[assignment]
    parser.read_string(path.read_text())
    return parser


# ---------------------------------------------------------------------------
# systemd units
# ---------------------------------------------------------------------------


def test_both_units_exist() -> None:
    assert SERVICE.exists() and TIMER.exists()


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="systemd-analyze not present")
@pytest.mark.parametrize("unit", ["r64-factory-conformance.service", "r64-factory-conformance.timer"])
def test_units_pass_systemd_analyze_verify(unit: str) -> None:
    result = subprocess.run(
        ["systemd-analyze", "verify", f"./{unit}"],
        cwd=SYSTEMD_DIR, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    # verify is quiet on success; any output is a complaint worth failing on.
    assert result.stderr.strip() == "", result.stderr


def test_the_service_runs_as_kos_from_the_repo_with_no_user_site() -> None:
    service = _unit(SERVICE)["Service"]
    assert service["User"] == "kos"
    assert service["WorkingDirectory"] == str(conformance.REPO_ROOT)
    # Standing discipline: a stray package in ~/.local must never shadow the
    # pinned one — pyarrow above all, since it owns the IPC block layout.
    assert "PYTHONNOUSERSITE=1" in service["Environment"]


def test_the_service_execstart_points_at_the_venv_and_the_real_sweep() -> None:
    exec_start = _unit(SERVICE)["Service"]["ExecStart"]
    assert str(conformance.REPO_ROOT / ".venv" / "bin" / "python") in exec_start
    assert str(SWEEP) in exec_start
    assert "--serve-gate" in exec_start


def test_the_service_alerts_on_failure_via_the_fleet_convention() -> None:
    assert _unit(SERVICE)["Unit"]["OnFailure"] == "ntfy-fail@%n.service"


def test_the_service_does_not_retry_a_conformance_failure() -> None:
    """A conformance failure is a REAL SIGNAL about a source that changed.
    Retrying would either mask a transient or hammer a third-party API; the
    repair brief and the ntfy alert are the response."""
    assert _unit(SERVICE)["Service"]["Restart"] == "no"


def test_the_service_is_bounded_in_time() -> None:
    """A hung sweep is worse than a failed one, because nothing alerts."""
    assert _unit(SERVICE)["Service"]["TimeoutStartSec"] == "45min"


def test_the_timer_is_weekly_persistent_and_jittered() -> None:
    timer = _unit(TIMER)["Timer"]
    assert timer["OnCalendar"].startswith("Sun")
    assert timer["Persistent"] == "true"
    assert timer["RandomizedDelaySec"] == "300"


def test_the_timer_documents_the_bench_window_conflict() -> None:
    """A sweep pulls a million rows and spins a Flight server. A bench series
    measuring an idle machine would report that as a lane effect, so quiescing
    this timer belongs on the root-quiesce checklist — and the unit has to SAY
    so, because the person reading it at 2am is not reading the close-out."""
    text = TIMER.read_text()
    assert "MUST NOT FIRE DURING A BENCH WINDOW" in text
    assert "systemctl stop  r64-factory-conformance.timer" in text
    assert "root-quiesce" in text


def test_the_timer_points_at_the_service() -> None:
    assert _unit(TIMER)["Timer"]["Unit"] == "r64-factory-conformance.service"


# ---------------------------------------------------------------------------
# The sweep, end to end
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_the_sweep_is_green_across_every_target(tmp_path: Path) -> None:
    """Gate F4's first half: both targets, one invocation, exit 0."""
    result = subprocess.run(
        [str(SWEEP),
         "--evidence-dir", str(tmp_path / "evidence"),
         "--brief-dir", str(tmp_path / "briefs"),
         "--work-dir", str(tmp_path / "work"),
         "--date", "19700101"],
        cwd=conformance.REPO_ROOT, capture_output=True, text=True, check=False, timeout=2400,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
    assert "clickhouse-meshbench" in result.stdout
    assert "rest-openmeteo" in result.stdout
    assert "[sweep] PASS" in result.stdout

    # A green sweep records the baseline the next failure will diff against.
    green = tmp_path / "evidence" / "last-green"
    assert (green / "EVIDENCE-clickhouse.json").exists()
    assert (green / "EVIDENCE-rest.json").exists()
    # No repair briefs on a green run.
    assert not list((tmp_path / "briefs").glob("REPAIR-BRIEF-*.md")) if (tmp_path / "briefs").exists() else True


@pytest.mark.integration
def test_a_poisoned_target_produces_a_repair_brief_with_the_correct_diff(tmp_path: Path) -> None:
    """Gate F4's second half.

    The ground truth is poisoned in a TMP COPY — never the committed file. That
    is not squeamishness: editing the real ground truth to explore a failure is
    the exact move that converts an oracle into a mirror.
    """
    import hashlib

    real_gt = conformance.REPO_ROOT / "bench" / "GROUND-TRUTH-clickhouse.json"
    digest_before = hashlib.sha256(real_gt.read_bytes()).hexdigest()

    # First, a green run to establish the last-green baseline.
    evidence = tmp_path / "evidence"
    common = ["--target", "clickhouse-meshbench",
              "--evidence-dir", str(evidence),
              "--work-dir", str(tmp_path / "work"),
              "--date", "19700101"]
    green = subprocess.run(
        [str(SWEEP), *common, "--brief-dir", str(tmp_path / "briefs")],
        cwd=conformance.REPO_ROOT, capture_output=True, text=True, check=False, timeout=1800,
    )
    assert green.returncode == 0, green.stdout[-3000:]

    # Now poison a COPY and sweep against it.
    gt_dir = tmp_path / "poisoned-bench"
    gt_dir.mkdir()
    doc = json.loads(real_gt.read_text())
    doc["tables"]["perf_1m"]["sum_quantity"] += 7
    (gt_dir / "GROUND-TRUTH-clickhouse.json").write_text(json.dumps(doc, indent=2))

    briefs = tmp_path / "briefs"
    poisoned = subprocess.run(
        [str(SWEEP), *common, "--ground-truth-dir", str(gt_dir), "--brief-dir", str(briefs)],
        cwd=conformance.REPO_ROOT, capture_output=True, text=True, check=False, timeout=1800,
    )
    assert poisoned.returncode == 1, poisoned.stdout[-3000:]
    assert "[sweep] FAIL" in poisoned.stdout
    assert "REPAIR BRIEF WRITTEN" in poisoned.stdout

    brief_path = briefs / "REPAIR-BRIEF-clickhouse-19700101.md"
    assert brief_path.exists()
    brief = brief_path.read_text()

    # The brief must name the failing check and show BOTH sides from BOTH runs,
    # so a reader can see that the EXPECTATION moved while the pipeline held —
    # which is the correct diagnosis for a poisoned ground truth.
    assert "`aggregate_parity`" in brief
    assert "Failing comparisons in `aggregate_parity`" in brief
    assert "sum_quantity" in brief
    assert str(doc["tables"]["perf_1m"]["sum_quantity"]) in brief
    assert "Read the two *expected* columns first" in brief
    assert "NEVER edit ground truth to match a failing pipeline" in brief

    # The committed ground truth is byte-unchanged.
    assert hashlib.sha256(real_gt.read_bytes()).hexdigest() == digest_before


@pytest.mark.integration
def test_an_unreachable_source_is_a_red_sweep_not_a_skip(tmp_path: Path) -> None:
    """Environmental prerequisites are FAILURES. Silently skipping a target
    whose source is down would turn the timer into decoration: the one week the
    sweep quietly checks nothing is the week it was needed."""
    target_dir = tmp_path / "targets"
    target_dir.mkdir()
    doc = conformance.load_yaml(
        conformance.REPO_ROOT / "factory" / "targets" / "clickhouse-meshbench.yaml"
    )
    doc["clickhouse"]["port"] = 1  # nothing listens here
    import yaml

    # Keep the target STEM so the spec still resolves by convention — the point
    # of this test is an unreachable SOURCE, not a misconfigured target, and a
    # renamed stem would fail for the wrong reason.
    (target_dir / "clickhouse-meshbench.yaml").write_text(yaml.safe_dump(doc))
    (tmp_path / "bench").mkdir()
    shutil.copy2(
        conformance.REPO_ROOT / "bench" / "GROUND-TRUTH-clickhouse.json",
        tmp_path / "bench" / "GROUND-TRUTH-clickhouse.json",
    )

    result = subprocess.run(
        [str(SWEEP),
         "--targets-dir", str(target_dir),
         "--ground-truth-dir", str(tmp_path / "bench"),
         "--evidence-dir", str(tmp_path / "evidence"),
         "--brief-dir", str(tmp_path / "briefs"),
         "--work-dir", str(tmp_path / "work"),
         # A dead endpoint does NOT fail fast — the clickhouse client retried
         # against a closed port for eleven minutes in an earlier run of this
         # very test, which is what surfaced the missing time bound. 60s is
         # enough to prove the bound fires without making the suite wait.
         "--per-target-timeout", "60",
         "--date", "19700101"],
        cwd=conformance.REPO_ROOT, capture_output=True, text=True, check=False, timeout=300,
    )
    assert result.returncode == 1, result.stdout[-3000:]
    assert "[sweep] FAIL" in result.stdout
    # And it must be reported as an error, with a brief, rather than swallowed.
    assert "ERROR" in result.stdout
    assert (tmp_path / "briefs" / "REPAIR-BRIEF-clickhouse-19700101.md").exists()
