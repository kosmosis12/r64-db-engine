"""T4 — an evidence pack must ratify a commit whose content actually ran.

Codex's objection, restated: a pack generated from a dirty tree names a SHA
whose content is not what executed. Someone re-running that commit gets
different code and possibly a different verdict, and nothing in the pack said
so — which makes the pack an assertion about a state that never existed.

The rule is a hard refusal, with one deliberate escape hatch that cannot be
mistaken for the real thing: `--allow-dirty` stamps ALLOW-DIRTY into the
verdict line and the pack header and sets `ratifies_head: false`.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from factory import conformance, evidence
from factory.battery import PASS, CheckResult

# ---------------------------------------------------------------------------
# The refusal
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


@pytest.fixture()
def clean_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "t@example.com", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    (repo / "a.txt").write_text("one\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "initial", cwd=repo)
    return repo


def test_a_clean_tree_is_accepted_and_reports_its_commit(clean_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(clean_repo)
    facts = evidence.assert_clean_tree()
    assert facts["dirty"] is False
    assert len(facts["commit"]) == 40


def test_a_dirty_tree_is_REFUSED(clean_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(clean_repo)
    (clean_repo / "a.txt").write_text("two\n")
    with pytest.raises(evidence.DirtyTreeError) as exc:
        evidence.assert_clean_tree()
    message = str(exc.value)
    assert "DIRTY working tree" in message
    assert "--allow-dirty" in message
    # It must name WHAT is uncommitted; "something changed" is not actionable.
    assert "a.txt" in message


def test_an_untracked_file_also_makes_the_tree_dirty(clean_repo: Path, monkeypatch) -> None:
    """An untracked file is content that ran and is not in the commit."""
    monkeypatch.chdir(clean_repo)
    (clean_repo / "new.py").write_text("x = 1\n")
    with pytest.raises(evidence.DirtyTreeError):
        evidence.assert_clean_tree()


def test_allow_dirty_permits_the_run(clean_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(clean_repo)
    (clean_repo / "a.txt").write_text("two\n")
    facts = evidence.assert_clean_tree(allow_dirty=True)
    assert facts["dirty"] is True


# ---------------------------------------------------------------------------
# The stamp
# ---------------------------------------------------------------------------


def _pack(dirty: bool, allow_dirty: bool):
    return evidence.build_pack(
        dialect="clickhouse", table="t", source="s",
        checks=[CheckResult("ok", PASS)],
        artifact={}, invocation={},
        provenance={
            "git": {"commit": "a" * 40, "branch": "b", "dirty": dirty},
            "allow_dirty": allow_dirty,
        },
    )


def test_a_clean_pack_ratifies_its_head() -> None:
    pack = _pack(dirty=False, allow_dirty=False)
    assert pack.ratifies_head is True
    assert pack.verdict_line == PASS
    assert "ALLOW-DIRTY" not in evidence.render_markdown(pack)


def test_an_allow_dirty_pack_is_stamped_in_the_verdict_line() -> None:
    pack = _pack(dirty=True, allow_dirty=True)
    assert pack.ratifies_head is False
    assert pack.verdict_line == "PASS (ALLOW-DIRTY)"


def test_an_allow_dirty_pack_says_so_in_its_header_not_a_footnote() -> None:
    """A reader skims the top. The warning has to be where they look."""
    md = evidence.render_markdown(_pack(dirty=True, allow_dirty=True))
    head = md.splitlines()[:8]
    assert any("ALLOW-DIRTY" in line for line in head)
    assert any("RATIFIES NO COMMIT" in line for line in head)


def test_the_json_form_carries_the_ratification_flag() -> None:
    doc = _pack(dirty=True, allow_dirty=True).as_dict()
    assert doc["ratifies_head"] is False
    assert doc["verdict"] == "PASS (ALLOW-DIRTY)"
    # The underlying status stays machine-readable and unmodified.
    assert doc["verdict_status"] == PASS


# ---------------------------------------------------------------------------
# Input digests
# ---------------------------------------------------------------------------


def test_inputs_are_pinned_by_content_not_by_path(tmp_path: Path) -> None:
    f = tmp_path / "spec.json"
    f.write_text('{"a": 1}')
    digests = evidence.digest_inputs({"schema_spec": f, "recipe_book": None})
    assert digests["schema_spec"]["sha256"] == hashlib.sha256(f.read_bytes()).hexdigest()
    assert digests["recipe_book"] is None

    # The point of hashing: same path, different content, different digest.
    before = digests["schema_spec"]["sha256"]
    f.write_text('{"a": 2}')
    assert evidence.digest_inputs({"schema_spec": f})["schema_spec"]["sha256"] != before


def test_a_missing_input_is_recorded_as_missing_not_omitted(tmp_path: Path) -> None:
    digests = evidence.digest_inputs({"ground_truth": tmp_path / "absent.json"})
    assert digests["ground_truth"]["sha256"] == "<missing>"


def test_the_implementation_digest_covers_engine_and_battery_sources() -> None:
    """The git SHA identifies a COMMIT, not an install. A pack run against a
    different checkout than the one on PATH would carry a plausible SHA and the
    wrong implementation, so the executed source is hashed directly."""
    impl = evidence.implementation_digest()
    assert len(impl["source_sha256"]) == 64
    assert impl["source_files"] > 20
    assert impl["distribution_version"] not in ("", "<not installed>")


def test_the_implementation_digest_is_deterministic() -> None:
    """Same tree, same digest. A digest that drifted between two calls on
    unchanged source would make every pack incomparable to every other."""
    assert (
        evidence.implementation_digest()["source_sha256"]
        == evidence.implementation_digest()["source_sha256"]
    )


def test_the_implementation_digest_ignores_pycache() -> None:
    """Compiled bytecode is not source. Including it would make the digest
    depend on whether anything had been imported yet."""
    impl = evidence.implementation_digest()
    caches = list((conformance.REPO_ROOT / "src").rglob("__pycache__"))
    assert caches, "no __pycache__ present, so this test proves nothing here"
    assert evidence.implementation_digest()["source_sha256"] == impl["source_sha256"]


# ---------------------------------------------------------------------------
# Artifact recording — no /tmp references
# ---------------------------------------------------------------------------


def test_a_small_artifact_is_copied_into_the_evidence_tree(tmp_path: Path) -> None:
    artifact = tmp_path / "small.arrow"
    artifact.write_bytes(b"x" * 1024)
    evidence_dir = tmp_path / "evidence"
    record = evidence.record_artifact(artifact, evidence_dir)

    assert record["storage"] == "copied"
    stored = evidence_dir / "artifacts" / f"{record['sha256']}.arrow"
    assert stored.exists()
    assert stored.read_bytes() == artifact.read_bytes()


def test_a_large_artifact_is_content_addressed_rather_than_copied(tmp_path: Path) -> None:
    """The meshbench artifact is ~150 MB. Committing it would bloat the repo
    without adding a check the sha256 does not already provide — but a reader
    must never have to GUESS whether bytes are present."""
    artifact = tmp_path / "big.arrow"
    artifact.write_bytes(b"y" * (evidence.ARTIFACT_COPY_LIMIT_BYTES + 1))
    evidence_dir = tmp_path / "evidence"
    record = evidence.record_artifact(artifact, evidence_dir)

    assert record["storage"] == "content-addressed manifest"
    manifest = evidence_dir / "artifacts" / f"{record['sha256']}.manifest.json"
    assert manifest.exists()
    assert json.loads(manifest.read_text())["sha256"] == record["sha256"]
    assert "not committed" in record["note"]


def test_the_artifact_record_pins_content_so_a_swap_is_visible(tmp_path: Path) -> None:
    a = tmp_path / "a.arrow"
    a.write_bytes(b"one")
    first = evidence.record_artifact(a, tmp_path / "evidence")["sha256"]
    a.write_bytes(b"two")
    assert evidence.record_artifact(a, tmp_path / "evidence")["sha256"] != first


# ---------------------------------------------------------------------------
# The committed packs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dialect", ["clickhouse", "rest"])
def test_the_committed_packs_ratify_a_clean_head(dialect: str) -> None:
    """The deliverable. Each committed pack must name a commit, from a clean
    tree, with every input pinned and no volatile path standing in for evidence.
    """
    packs = sorted((conformance.REPO_ROOT / "factory" / "evidence").glob(
        f"EVIDENCE-{dialect}-*.json"))
    assert packs, f"no committed pack for {dialect}"
    doc = json.loads(packs[-1].read_text())

    prov = doc["provenance"]
    assert prov["allow_dirty"] is False
    assert prov["git"]["dirty"] is False
    assert len(prov["git"]["commit"]) == 40
    assert doc["ratifies_head"] is True
    assert "ALLOW-DIRTY" not in doc["verdict"]

    # The exact command, so the run is reproducible from the pack alone.
    assert "factory.conformance" in prov["command"]
    assert f"--dialect {dialect}" in prov["command"].replace("--dialect ", "--dialect ")

    # Six inputs pinned: four files, the implementation, the artifact.
    inputs = prov["inputs"]
    for name in ("target_config", "schema_spec", "ground_truth"):
        assert len(inputs[name]["sha256"]) == 64, f"{name} not pinned"
    assert len(prov["implementation"]["source_sha256"]) == 64
    assert len(prov["artifact"]["sha256"]) == 64

    # The recipe book is pinned for the recipe lane and absent for the DB lane.
    if dialect == "rest":
        assert len(inputs["recipe_book"]["sha256"]) == 64
    else:
        assert inputs["recipe_book"] is None


@pytest.mark.parametrize("dialect", ["clickhouse", "rest"])
def test_the_committed_packs_reference_no_volatile_paths_as_evidence(dialect: str) -> None:
    """`/tmp/...` in an artifact record points at something already deleted by
    the time anyone reads it, which is the opposite of evidence."""
    packs = sorted((conformance.REPO_ROOT / "factory" / "evidence").glob(
        f"EVIDENCE-{dialect}-*.json"))
    record = json.loads(packs[-1].read_text())["provenance"]["artifact"]

    # `produced_at` may legitimately be a work dir — it records where the run
    # wrote. The evidence POINTER must not be.
    assert not record["path"].startswith("/tmp"), record["path"]
    assert record["path"].startswith("factory/evidence/artifacts/")
    assert (conformance.REPO_ROOT / record["path"]).exists()
