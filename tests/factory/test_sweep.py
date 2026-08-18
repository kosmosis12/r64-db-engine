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


# A systemd unit must hardcode ABSOLUTE paths on its deployment host, so the
# unit legitimately names /home/kos/... while a CI runner checks the repo out at
# /home/runner/work/.... Asserting the unit's paths equal THIS checkout's
# location therefore tests where the tests happen to be running, not whether the
# unit is correct — which is exactly how these three failed on the first PR.
#
# The machine-independent property, and the one that actually catches typos, is
# INTERNAL CONSISTENCY: whatever root the unit declares, its ExecStart must use
# that same root's venv and that same root's sweep. That is asserted everywhere.
# The stronger "and that root is this checkout" claim is kept, but as its own
# on-host test with an explicit skip reason.
DEPLOYMENT_ROOT = Path(_unit(SERVICE)["Service"]["WorkingDirectory"])
DEPLOYED_PYTHON = DEPLOYMENT_ROOT / ".venv" / "bin" / "python"
ON_DEPLOYMENT_HOST = DEPLOYMENT_ROOT == conformance.REPO_ROOT


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="systemd-analyze not present")
@pytest.mark.skipif(
    not DEPLOYED_PYTHON.exists(),
    reason=(
        f"systemd-analyze verify resolves ExecStart and fails if the binary is absent; "
        f"{DEPLOYED_PYTHON} does not exist here, so this can only run on the deployment host"
    ),
)
@pytest.mark.parametrize("unit", ["r64-factory-conformance.service", "r64-factory-conformance.timer"])
def test_units_pass_systemd_analyze_verify(unit: str) -> None:
    result = subprocess.run(
        ["systemd-analyze", "verify", f"./{unit}"],
        cwd=SYSTEMD_DIR, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    # verify is quiet on success; any output is a complaint worth failing on.
    assert result.stderr.strip() == "", result.stderr


def test_the_service_runs_as_kos_with_no_user_site() -> None:
    service = _unit(SERVICE)["Service"]
    assert service["User"] == "kos"
    # An absolute path that looks like this repo — not necessarily THIS checkout.
    assert DEPLOYMENT_ROOT.is_absolute()
    assert DEPLOYMENT_ROOT.name == "r64-db-engine"
    # Standing discipline: a stray package in ~/.local must never shadow the
    # pinned one — pyarrow above all, since it owns the IPC block layout.
    assert "PYTHONNOUSERSITE=1" in service["Environment"]


def test_the_service_execstart_is_internally_consistent_with_its_working_directory() -> None:
    """ExecStart must use the venv and the sweep of the root it declares.

    This is the check with teeth: a unit whose WorkingDirectory and ExecStart
    drifted apart would install cleanly and then fail at 04:00 on a Sunday.
    """
    exec_start = _unit(SERVICE)["Service"]["ExecStart"]
    assert str(DEPLOYED_PYTHON) in exec_start
    assert str(DEPLOYMENT_ROOT / "factory" / "bin" / "factory-conformance-sweep") in exec_start
    assert "--serve-gate" in exec_start


@pytest.mark.skipif(
    not ON_DEPLOYMENT_HOST,
    reason=f"unit targets {DEPLOYMENT_ROOT}, this checkout is {conformance.REPO_ROOT}",
)
def test_on_the_deployment_host_the_unit_points_at_this_very_checkout() -> None:
    """The stronger claim, kept but fenced to where it can be true.

    On Kos's machine the installed unit must drive THIS repo, not a stale copy
    at some other path. Off-host it is unknowable, so it skips with a reason
    rather than asserting something about the CI runner's filesystem.
    """
    assert DEPLOYMENT_ROOT == conformance.REPO_ROOT
    assert str(SWEEP) in _unit(SERVICE)["Service"]["ExecStart"]
    assert SWEEP.exists()


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


# ---------------------------------------------------------------------------
# Auto-commit: the evidence cadence
# ---------------------------------------------------------------------------
#
# These are fast and unconditional. The rule they protect — a failing sweep
# never files its pack into history — is the one thing about auto-commit that
# must hold without a live source, so it is tested without one.


def _sweep_module():
    """Import the extensionless sweep script as a module.

    The sweep has no `.py` suffix because it is an executable an operator runs
    by name, so a normal import will not find it.
    """
    import importlib.machinery
    import importlib.util

    spec = importlib.util.spec_from_loader(
        "factory_conformance_sweep",
        importlib.machinery.SourceFileLoader("factory_conformance_sweep", str(SWEEP)),
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _green(target: str = "clickhouse-meshbench", table: str = "perf_1m") -> dict:
    return {"target": target, "table": table, "dialect": "clickhouse", "verdict": "PASS"}


def _repo(tmp_path: Path) -> Path:
    """A throwaway git repo with an evidence subtree, committed once."""
    repo = tmp_path / "repo"
    (repo / "factory" / "evidence").mkdir(parents=True)
    (repo / "README.md").write_text("seed\n")
    for argv in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "test@example.invalid"],
        ["config", "user.name", "Test"],
        ["add", "-A"],
        ["commit", "-qm", "seed"],
    ):
        subprocess.run(["git", *argv], cwd=repo, check=True, capture_output=True)
    return repo


def test_a_failing_run_is_never_auto_committed() -> None:
    sweep = _sweep_module()
    ok, reason = sweep.should_auto_commit([_green(), {**_green(), "verdict": "FAIL"}], [])
    assert ok is False
    # The refusal names what failed, so the skip is never implicit.
    assert "not PASS" in reason and "FAIL" in reason


def test_unresolved_drift_blocks_auto_commit_even_when_every_battery_is_green() -> None:
    sweep = _sweep_module()
    ok, reason = sweep.should_auto_commit([_green()], [{"recipe": "r", "reason": "schema"}])
    assert ok is False
    assert "drift" in reason


def test_a_fully_green_sweep_is_allowed_to_commit() -> None:
    sweep = _sweep_module()
    ok, reason = sweep.should_auto_commit([_green(), _green("rest-openmeteo", "hourly")], [])
    assert ok is True
    assert reason == "2/2 PASS"


def test_an_empty_result_set_is_not_a_green_sweep() -> None:
    sweep = _sweep_module()
    ok, _ = sweep.should_auto_commit([], [])
    assert ok is False


def test_the_commit_subject_matches_the_agreed_pattern() -> None:
    sweep = _sweep_module()
    message = sweep.commit_message("20260817", [_green(), _green("rest-openmeteo", "hourly")], None)
    assert message.splitlines()[0] == "evidence: sweep 20260817, 2/2 PASS"
    # The body carries the findings, so `git log` answers what was green.
    assert "clickhouse-meshbench/perf_1m (clickhouse): PASS" in message


def test_auto_commit_commits_the_evidence_subtree(tmp_path: Path) -> None:
    sweep = _sweep_module()
    repo = _repo(tmp_path)
    evidence = repo / "factory" / "evidence"
    (evidence / "EVIDENCE-clickhouse-20260817.json").write_text('{"verdict": "PASS"}')

    sha = sweep.auto_commit_green(evidence, "evidence: sweep 20260817, 1/1 PASS", repo_root=repo)

    assert sha is not None
    log = subprocess.run(
        ["git", "log", "-1", "--format=%H%n%s", "--name-only"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout
    assert sha in log
    assert "evidence: sweep 20260817, 1/1 PASS" in log
    assert "factory/evidence/EVIDENCE-clickhouse-20260817.json" in log


def test_auto_commit_leaves_unrelated_working_tree_changes_alone(tmp_path: Path) -> None:
    """The fence that matters most: this runs unattended, at 04:00, on a Sunday.

    A `git commit -a` on a timer would absorb whatever Kos happened to be
    editing. Both a dirty tracked file and an unrelated STAGED file must survive
    the sweep's commit untouched.
    """
    sweep = _sweep_module()
    repo = _repo(tmp_path)
    evidence = repo / "factory" / "evidence"
    (evidence / "EVIDENCE-rest-20260817.json").write_text('{"verdict": "PASS"}')

    (repo / "README.md").write_text("a half-finished edit\n")
    (repo / "staged.py").write_text("import os\n")
    subprocess.run(["git", "add", "staged.py"], cwd=repo, check=True, capture_output=True)

    sweep.auto_commit_green(evidence, "evidence: sweep 20260817, 1/1 PASS", repo_root=repo)

    files = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.split()
    assert files == ["factory/evidence/EVIDENCE-rest-20260817.json"]

    # The unrelated edit is still dirty and the unrelated staged file still staged.
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True,
    ).stdout
    assert " M README.md" in status
    assert "A  staged.py" in status


def test_auto_commit_is_a_no_op_when_the_evidence_is_unchanged(tmp_path: Path) -> None:
    sweep = _sweep_module()
    repo = _repo(tmp_path)
    evidence = repo / "factory" / "evidence"
    (evidence / "EVIDENCE-rest-20260817.json").write_text('{"verdict": "PASS"}')
    sweep.auto_commit_green(evidence, "evidence: sweep 20260817, 1/1 PASS", repo_root=repo)

    before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout
    # Nothing changed since; a second sweep must not create an empty commit.
    assert sweep.auto_commit_green(evidence, "again", repo_root=repo) is None
    after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout
    assert before == after


def test_auto_commit_refuses_an_evidence_dir_outside_the_checkout(tmp_path: Path) -> None:
    """Refused by name, not silently skipped — it is a misconfiguration."""
    sweep = _sweep_module()
    repo = _repo(tmp_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    with pytest.raises(sweep.AutoCommitRefused) as exc:
        sweep.auto_commit_green(outside, "evidence: sweep 20260817, 1/1 PASS", repo_root=repo)
    assert "inside the checkout" in str(exc.value)


def test_auto_commit_refuses_an_oversized_payload(tmp_path: Path) -> None:
    """A meshbench-sized artifact must not land in history on a timer."""
    sweep = _sweep_module()
    repo = _repo(tmp_path)
    evidence = repo / "factory" / "evidence"
    (evidence / "huge.arrow").write_bytes(b"\0" * (sweep._COMMIT_MAX_BYTES + 1))

    with pytest.raises(sweep.AutoCommitRefused) as exc:
        sweep.auto_commit_green(evidence, "evidence: sweep 20260817, 1/1 PASS", repo_root=repo)
    assert "refusing to auto-commit" in str(exc.value)
    # And it refused BEFORE staging anything.
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout
    assert status.startswith("??")


def test_the_service_passes_auto_commit_because_the_timer_is_the_cadence() -> None:
    assert "--auto-commit" in _unit(SERVICE)["Service"]["ExecStart"]
