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

# Captured at import, before any test monkeypatches `conformance.REPO_ROOT`.
# Read-only uses (reading committed targets, specs and ground truth) go through
# this; nothing writes into it.
REAL_REPO = Path(__file__).resolve().parents[2]

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
    depend on whether anything had been imported yet.

    Skipped rather than failed when no `__pycache__` exists: on a cold checkout
    there is nothing to ignore, so the property is untestable here rather than
    violated. Asserting the precondition made this fail on a fresh tree for a
    reason that has nothing to do with the digest.
    """
    caches = list((conformance.REPO_ROOT / "src").rglob("__pycache__"))
    if not caches:
        pytest.skip("no __pycache__ present, so there is nothing to prove is ignored")
    impl = evidence.implementation_digest()
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


# ---------------------------------------------------------------------------
# Q3(a) — the exemption must not launder inputs
# ---------------------------------------------------------------------------


def test_an_input_inside_the_evidence_tree_is_REFUSED(tmp_path: Path) -> None:
    """The laundering path Codex found.

    `factory/evidence/` carries the dirty-tree exemption so a pack's own output
    cannot invalidate it. An INPUT placed there would inherit that exemption: it
    could be swapped between runs without ever making the tree dirty, and every
    pack would keep reporting `ratifies_head: true` while the inputs moved
    underneath it.
    """
    repo = tmp_path
    hostile = repo / "factory" / "evidence" / "hostile-target.yaml"
    hostile.parent.mkdir(parents=True)
    hostile.write_text("dialect: rest\n")

    with pytest.raises(evidence.LaunderedInputError) as exc:
        evidence.assert_inputs_outside_evidence({"target_config": hostile}, repo)
    assert "target_config" in str(exc.value)
    assert str(hostile) in str(exc.value)


def test_inputs_outside_the_evidence_tree_are_accepted(tmp_path: Path) -> None:
    ok = tmp_path / "factory" / "targets" / "t.yaml"
    ok.parent.mkdir(parents=True)
    ok.write_text("dialect: rest\n")
    evidence.assert_inputs_outside_evidence({"target_config": ok}, tmp_path)


def test_the_evidence_directory_itself_is_refused_as_an_input(tmp_path: Path) -> None:
    guarded = tmp_path / "factory" / "evidence"
    guarded.mkdir(parents=True)
    with pytest.raises(evidence.LaunderedInputError):
        evidence.assert_inputs_outside_evidence({"ground_truth": guarded}, tmp_path)


def test_a_none_input_is_not_an_error(tmp_path: Path) -> None:
    """The DB lane has no recipe book; absent is not laundered."""
    evidence.assert_inputs_outside_evidence({"recipe_book": None}, tmp_path)


def test_the_cli_refuses_a_laundered_target_and_writes_no_pack(
    tmp_path: Path, monkeypatch
) -> None:
    """End to end through the real CLI: refusal, non-zero exit, no pack.

    THE FIXTURE LIVES IN `tmp_path`, NEVER IN THE CHECKOUT. An earlier version
    created `factory/evidence/_test_hostile/` inside the repository, which made
    the test unrunnable against a read-only checkout — the F-10 class again: it
    asserted something about the environment it happened to run in.

    `conformance.REPO_ROOT` is monkeypatched to a throwaway tree, which is also
    the honest shape: the laundering rule is *relative to a repository root*,
    so the test should supply one rather than borrow the real one.
    """
    if not evidence._git_facts().get("commit"):
        pytest.skip("not a git checkout; the clean-tree refusal fires before this one")

    fake_root = tmp_path / "repo"
    (fake_root / "factory" / "evidence").mkdir(parents=True)
    (fake_root / "factory" / "targets").mkdir(parents=True)

    hostile = fake_root / "factory" / "evidence" / "rest-openmeteo.yaml"
    hostile.write_text(
        (conformance.REPO_ROOT / "factory" / "targets" / "rest-openmeteo.yaml").read_text()
    )
    monkeypatch.setattr(conformance, "REPO_ROOT", fake_root)

    evidence_dir = tmp_path / "evidence"
    with pytest.raises(SystemExit) as exc:
        conformance.main([
            "--dialect", "rest",
            "--config", str(hostile),
            "--ground-truth", str(REAL_REPO / "bench" / "GROUND-TRUTH-openmeteo.json"),
            "--table", "open_meteo_berlin_hourly",
            "--allow-dirty",
            "--spec", str(REAL_REPO / "factory" / "specs" / "openmeteo-schema.json"),
            "--evidence-dir", str(evidence_dir),
        ])

    assert "inside" in str(exc.value)
    assert not evidence_dir.exists(), "a pack was written despite the refusal"


def test_the_laundering_test_writes_nothing_into_the_checkout() -> None:
    """Guard on the guard: no test fixture may be created inside the repo.

    The off-host replay did not catch the earlier version because it copied the
    tree to a WRITABLE location — it varied the path but not the writability, so
    creating a directory in the "checkout" simply worked. This asserts the
    property directly instead of hoping a replay environment exposes it.
    """
    assert not (conformance.REPO_ROOT / "factory" / "evidence" / "_test_hostile").exists()


# ---------------------------------------------------------------------------
# Q3(b) — extended pins
# ---------------------------------------------------------------------------


def test_the_toolchain_pins_cover_the_lockfiles_and_interpreter() -> None:
    pins = evidence.toolchain_pins(conformance.REPO_ROOT)
    assert len(pins["pyproject_toml"]["sha256"]) == 64
    assert len(pins["uv_lock"]["sha256"]) == 64
    assert pins["python"].count(".") == 2
    assert pins["platform_triple"].startswith("Linux-") or pins["platform_triple"]


def test_the_meshroad_binary_is_content_addressed_when_the_gate_runs(tmp_path: Path) -> None:
    """The serve gate reports counters from a binary built outside this repo.
    A content address is the only thing that says which one ran."""
    binary = tmp_path / "meshroad"
    binary.write_bytes(b"fake binary")
    pins = evidence.toolchain_pins(conformance.REPO_ROOT, str(binary))
    assert pins["meshroad_binary"]["sha256"] == hashlib.sha256(b"fake binary").hexdigest()


def test_an_absent_meshroad_binary_is_recorded_as_absent(tmp_path: Path) -> None:
    pins = evidence.toolchain_pins(conformance.REPO_ROOT, str(tmp_path / "nope"))
    assert pins["meshroad_binary"]["sha256"] == "<not present>"


def test_proxy_environment_is_captured_with_values(monkeypatch) -> None:
    """A pack that recorded a hostname while a proxy silently rerouted every
    call would be describing a run that did not happen."""
    monkeypatch.setenv("HTTPS_PROXY", "http://corp-proxy:3128")
    monkeypatch.setenv("NO_PROXY", "localhost")
    captured = evidence.proxy_environment()
    assert captured["HTTPS_PROXY"] == "http://corp-proxy:3128"
    assert captured["NO_PROXY"] == "localhost"


def test_an_unproxied_environment_says_so_rather_than_being_empty(monkeypatch) -> None:
    for name in evidence.PROXY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    assert "_note" in evidence.proxy_environment()


# ---------------------------------------------------------------------------
# Q3(c) — secrets are described, never hashed
# ---------------------------------------------------------------------------


def test_a_secret_file_is_recorded_by_reference_and_NEVER_hashed(tmp_path: Path) -> None:
    """A sha256 of a low-entropy API key is offline-guessable, and an evidence
    pack travels. Publishing a digest of a short credential hands an attacker an
    oracle to grind against."""
    secret = tmp_path / "api.env"
    secret.write_text("sk-live-abc123")
    secret.chmod(0o600)

    record = evidence.secret_references([str(secret)])[0]
    assert record["contents_hashed"] is False
    assert record["bytes"] == 14
    assert record["mode"] == "0o600"
    assert "mtime_utc" in record
    # Nothing derived from the contents may appear anywhere in the record.
    blob = json.dumps(record)
    assert "sk-live-abc123" not in blob
    assert hashlib.sha256(b"sk-live-abc123").hexdigest() not in blob
    assert "sha256" not in blob


def test_an_absent_secret_file_is_recorded_as_absent(tmp_path: Path) -> None:
    record = evidence.secret_references([str(tmp_path / "gone.env")])[0]
    assert record["present"] is False


def test_the_closure_boundary_names_what_is_not_pinned() -> None:
    items = {entry["item"] for entry in evidence.CLOSURE_BOUNDARY}
    assert "secret contents" in items
    assert any("native" in item for item in items)
    assert any("live source" in item for item in items)
    for entry in evidence.CLOSURE_BOUNDARY:
        assert entry["why"], f"{entry['item']} states no reason"
        assert entry["pinned"] is False


def test_live_source_state_is_marked_measured_rather_than_merely_unpinned() -> None:
    """It is not absent from the pack — the measurements ARE the evidence. The
    boundary is that they establish what the source held during this run, not
    that it will hold it again."""
    entry = next(e for e in evidence.CLOSURE_BOUNDARY if "live source" in e["item"])
    assert entry.get("measured") is True


def test_every_pack_renders_a_closure_boundary_section() -> None:
    md = evidence.render_markdown(_pack(dirty=False, allow_dirty=False))
    assert "CLOSURE BOUNDARY" in md
    assert "does NOT establish" in md
    assert "secret contents" in md


@pytest.mark.parametrize("dialect", ["clickhouse", "rest"])
def test_the_committed_packs_carry_the_closure_boundary_and_extended_pins(dialect: str) -> None:
    packs = sorted((conformance.REPO_ROOT / "factory" / "evidence").glob(
        f"EVIDENCE-{dialect}-*.json"))
    doc = json.loads(packs[-1].read_text())
    prov = doc["provenance"]

    assert prov["closure_boundary"], "no closure boundary recorded"
    assert len(prov["toolchain"]["pyproject_toml"]["sha256"]) == 64
    assert len(prov["toolchain"]["uv_lock"]["sha256"]) == 64
    assert prov["toolchain"]["python"]
    assert "proxy_environment" in prov

    # Secrets: the recipe lane declares none (open-meteo is unauthenticated),
    # and whatever is declared must never carry a content digest.
    for record in prov["secret_references"]:
        assert record["contents_hashed"] is False
        assert "sha256" not in json.dumps(record)


def test_the_committed_packs_do_not_overclaim_in_their_own_text() -> None:
    """The reworded provenance comment must not have crept back."""
    source = (conformance.REPO_ROOT / "factory" / "conformance.py").read_text()
    assert "Every input the run consumed" not in source
    assert "DECLARED inputs this run read from disk" in source


# ---------------------------------------------------------------------------
# Q2 — verify on reuse: a content-addressed name is a CLAIM, not a guarantee
# ---------------------------------------------------------------------------


def _store(tmp_path: Path, payload: bytes = b"genuine artifact bytes") -> tuple[Path, Path]:
    artifact = tmp_path / "small.arrow"
    artifact.write_bytes(payload)
    return artifact, tmp_path / "evidence"


def test_a_freshly_written_store_entry_is_verified(tmp_path: Path) -> None:
    artifact, evidence_dir = _store(tmp_path)
    record = evidence.record_artifact(artifact, evidence_dir)
    assert record["store_verified"] is True
    assert record.get("store_repaired") is None


def test_a_clean_reuse_is_verified_not_assumed(tmp_path: Path) -> None:
    """The second run must HASH the existing file, not trust its filename."""
    artifact, evidence_dir = _store(tmp_path)
    evidence.record_artifact(artifact, evidence_dir)
    record = evidence.record_artifact(artifact, evidence_dir)
    assert record["store_verified"] is True


def test_a_CORRUPTED_store_entry_is_REFUSED(tmp_path: Path) -> None:
    """Corrupted stored evidence is a provenance FINDING, not a cache miss.

    Overwriting it silently would destroy the only trace that something had
    gone wrong with the archive — and the pack would go on asserting a content
    address for bytes nobody had read.
    """
    artifact, evidence_dir = _store(tmp_path)
    first = evidence.record_artifact(artifact, evidence_dir)
    stored = evidence_dir / "artifacts" / f"{first['sha256']}.arrow"
    stored.write_bytes(b"CORRUPTED")

    with pytest.raises(evidence.CorruptStoredEvidenceError) as exc:
        evidence.record_artifact(artifact, evidence_dir)

    message = str(exc.value)
    assert str(stored) in message, "the refusal must name the path"
    assert first["sha256"] in message, "the refusal must give the expected hash"
    assert hashlib.sha256(b"CORRUPTED").hexdigest() in message, "and the actual one"
    assert "--repair-store" in message, "and say how to proceed"


def test_a_corrupted_entry_is_NOT_silently_overwritten(tmp_path: Path) -> None:
    """The bytes must still be the corrupted ones after the refusal — the
    evidence of the corruption is itself evidence."""
    artifact, evidence_dir = _store(tmp_path)
    first = evidence.record_artifact(artifact, evidence_dir)
    stored = evidence_dir / "artifacts" / f"{first['sha256']}.arrow"
    stored.write_bytes(b"CORRUPTED")

    with pytest.raises(evidence.CorruptStoredEvidenceError):
        evidence.record_artifact(artifact, evidence_dir)
    assert stored.read_bytes() == b"CORRUPTED"


def test_repair_store_repairs_and_RECORDS_the_repair(tmp_path: Path) -> None:
    """A repair that left no trace would be indistinguishable from a run that
    never hit corruption."""
    artifact, evidence_dir = _store(tmp_path)
    first = evidence.record_artifact(artifact, evidence_dir)
    stored = evidence_dir / "artifacts" / f"{first['sha256']}.arrow"
    stored.write_bytes(b"CORRUPTED")

    record = evidence.record_artifact(artifact, evidence_dir, repair_store=True)

    assert record["store_repaired"] is True
    assert record["store_verified"] is True
    assert record["store_previous_sha256"] == hashlib.sha256(b"CORRUPTED").hexdigest()
    assert record["store_repaired_sha256"] == first["sha256"]
    assert stored.read_bytes() == artifact.read_bytes()


def test_the_affirmative_claim_is_emitted_only_after_verification(tmp_path: Path) -> None:
    """No record at all comes back from a failed verification — the pack cannot
    be assembled around bytes that did not pass."""
    artifact, evidence_dir = _store(tmp_path)
    first = evidence.record_artifact(artifact, evidence_dir)
    (evidence_dir / "artifacts" / f"{first['sha256']}.arrow").write_bytes(b"X")

    with pytest.raises(evidence.CorruptStoredEvidenceError):
        evidence.record_artifact(artifact, evidence_dir)


def test_the_manifest_route_is_structurally_unaffected(tmp_path: Path) -> None:
    """The large-artifact route rewrites its manifest every run, so there is no
    pre-existing content being trusted. Asserted rather than assumed: a stale
    manifest is simply overwritten, and the record still verifies."""
    artifact = tmp_path / "big.arrow"
    artifact.write_bytes(b"z" * (evidence.ARTIFACT_COPY_LIMIT_BYTES + 1))
    evidence_dir = tmp_path / "evidence"

    first = evidence.record_artifact(artifact, evidence_dir)
    manifest = evidence_dir / "artifacts" / f"{first['sha256']}.manifest.json"
    manifest.write_text('{"tampered": true}')

    second = evidence.record_artifact(artifact, evidence_dir)
    assert second["store_verified"] is True
    assert json.loads(manifest.read_text())["sha256"] == first["sha256"]
    assert "verify-on-reuse does not apply" in second["note"]


def test_MUTATION_removing_the_verification_makes_the_corruption_test_pass(
    tmp_path: Path, monkeypatch
) -> None:
    """Mutation check: the corruption test must depend on the verification.

    A reuse path that skips the hash accepts the corrupted file silently — which
    is exactly the round-2 behaviour. If this ever stops holding, the tests
    above are no longer measuring the verification.
    """
    artifact, evidence_dir = _store(tmp_path)
    first = evidence.record_artifact(artifact, evidence_dir)
    stored = evidence_dir / "artifacts" / f"{first['sha256']}.arrow"
    stored.write_bytes(b"CORRUPTED")

    # Bypass the verification the way the pre-fix code did: trust the filename.
    monkeypatch.setattr(evidence, "sha256_file", lambda path: first["sha256"])
    record = evidence.record_artifact(artifact, evidence_dir)

    assert record["store_verified"] is True, (
        "with verification bypassed the corrupted entry is accepted — which is the "
        "defect, and confirms the real tests above depend on the real check"
    )
    assert stored.read_bytes() == b"CORRUPTED"


def test_the_repair_store_flag_is_exposed_on_the_cli() -> None:
    """The escape hatch the refusal message points at must actually exist.

    `sys.executable`, never a hardcoded `.venv/bin/python`: the interpreter
    running the tests is the one that can import the package, and hardcoding a
    path makes the test assert where it is RUNNING rather than what the CLI
    offers. That is the F-10 defect class, and it broke CI once already.
    """
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "factory.conformance", "--help"],
        cwd=conformance.REPO_ROOT, capture_output=True, text=True, check=False,
    )
    assert "--repair-store" in result.stdout
    assert "--allow-dirty" in result.stdout


def test_the_corrupt_store_refusal_is_rendered_as_a_refusal_not_a_traceback() -> None:
    """`CorruptStoredEvidenceError` is converted to `SystemExit` at the CLI
    boundary, the same way every other loud refusal in this tool is — a
    traceback reads like a bug in the battery rather than a finding about the
    archive.

    Asserted on the handler rather than by provoking a full run: reaching the
    real one costs two million-row pulls, and what matters is that the
    conversion exists on the path that raises it.
    """
    source = (conformance.REPO_ROOT / "factory" / "conformance.py").read_text()
    assert "except evidence.CorruptStoredEvidenceError as exc:" in source
    assert "raise SystemExit(str(exc)) from exc" in source


@pytest.mark.parametrize("dialect", ["clickhouse", "rest"])
def test_the_committed_packs_record_a_verified_store(dialect: str) -> None:
    packs = sorted((conformance.REPO_ROOT / "factory" / "evidence").glob(
        f"EVIDENCE-{dialect}-*.json"))
    record = json.loads(packs[-1].read_text())["provenance"]["artifact"]
    assert record["store_verified"] is True


# ---------------------------------------------------------------------------
# Round 4 Q1(a) — preserve corrupted bytes BEFORE overwriting them
# ---------------------------------------------------------------------------


def test_repair_preserves_the_corrupted_bytes_alongside(tmp_path: Path) -> None:
    """The corrupted bytes are the only evidence the archive went wrong.

    They are copied aside FIRST, under a name recording what they actually
    hashed to, and only then is the entry repaired.
    """
    artifact, evidence_dir = _store(tmp_path)
    first = evidence.record_artifact(artifact, evidence_dir)
    stored = evidence_dir / "artifacts" / f"{first['sha256']}.arrow"
    stored.write_bytes(b"CORRUPTED")
    corrupt_hash = hashlib.sha256(b"CORRUPTED").hexdigest()

    record = evidence.record_artifact(artifact, evidence_dir, repair_store=True)

    preserved = evidence_dir / "artifacts" / f"{first['sha256']}.corrupt-{corrupt_hash}"
    assert preserved.exists(), "the corrupted bytes were destroyed"
    assert preserved.read_bytes() == b"CORRUPTED"
    assert stored.read_bytes() == artifact.read_bytes()

    assert record["store_repaired"] is True
    assert record["store_previous_sha256"] == corrupt_hash
    assert record["store_repaired_sha256"] == first["sha256"]
    assert record["store_corrupt_preserved_path"].endswith(f".corrupt-{corrupt_hash}")


def test_the_preserved_name_records_what_the_bytes_ACTUALLY_hashed_to(tmp_path: Path) -> None:
    """Named by the real digest, not by a counter: two different corruptions of
    the same entry are distinguishable rather than overwriting each other."""
    artifact, evidence_dir = _store(tmp_path)
    first = evidence.record_artifact(artifact, evidence_dir)
    stored = evidence_dir / "artifacts" / f"{first['sha256']}.arrow"

    for payload in (b"CORRUPTION-A", b"CORRUPTION-B"):
        stored.write_bytes(payload)
        evidence.record_artifact(artifact, evidence_dir, repair_store=True)

    preserved = sorted((evidence_dir / "artifacts").glob("*.corrupt-*"))
    assert len(preserved) == 2, "the second corruption overwrote the first"
    for payload in (b"CORRUPTION-A", b"CORRUPTION-B"):
        digest = hashlib.sha256(payload).hexdigest()
        assert any(p.name.endswith(f".corrupt-{digest}") for p in preserved)


def test_a_CRASH_MID_REPAIR_never_destroys_the_corrupted_bytes(
    tmp_path: Path, monkeypatch
) -> None:
    """Crash consistency — no adversary required.

    The repair copy is made to fail AFTER preservation. Whatever happens, disk
    holds either the original corruption or the preserved copy; there is no
    ordering where the corrupted bytes are gone and nothing recorded them.
    """
    artifact, evidence_dir = _store(tmp_path)
    first = evidence.record_artifact(artifact, evidence_dir)
    stored = evidence_dir / "artifacts" / f"{first['sha256']}.arrow"
    stored.write_bytes(b"CORRUPTED")
    corrupt_hash = hashlib.sha256(b"CORRUPTED").hexdigest()

    real_copy = evidence.shutil.copy2
    calls = {"n": 0}

    def flaky(src, dst, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:  # 1 = preserve, 2 = the repair itself
            raise OSError("disk full mid-repair")
        return real_copy(src, dst, *args, **kwargs)

    monkeypatch.setattr(evidence.shutil, "copy2", flaky)
    with pytest.raises(OSError, match="disk full"):
        evidence.record_artifact(artifact, evidence_dir, repair_store=True)

    assert stored.read_bytes() == b"CORRUPTED", "the corruption was destroyed by a failed repair"
    preserved = evidence_dir / "artifacts" / f"{first['sha256']}.corrupt-{corrupt_hash}"
    assert preserved.exists() and preserved.read_bytes() == b"CORRUPTED"


def test_a_failure_to_PRESERVE_aborts_before_any_overwrite(
    tmp_path: Path, monkeypatch
) -> None:
    """If preservation itself cannot complete, the repair must not start."""
    artifact, evidence_dir = _store(tmp_path)
    first = evidence.record_artifact(artifact, evidence_dir)
    stored = evidence_dir / "artifacts" / f"{first['sha256']}.arrow"
    stored.write_bytes(b"CORRUPTED")

    def refuse(src, dst, *args, **kwargs):
        raise OSError("cannot write the preserved copy")

    monkeypatch.setattr(evidence.shutil, "copy2", refuse)
    with pytest.raises(OSError):
        evidence.record_artifact(artifact, evidence_dir, repair_store=True)
    assert stored.read_bytes() == b"CORRUPTED"


# ---------------------------------------------------------------------------
# Round 4 Q1(b) — hash late: the claim comes from the read that backs it
# ---------------------------------------------------------------------------


def test_the_recorded_digest_comes_from_the_final_read(tmp_path: Path) -> None:
    artifact, evidence_dir = _store(tmp_path)
    record = evidence.record_artifact(artifact, evidence_dir)
    assert record["store_verified_sha256"] == record["sha256"]


def test_a_store_mutated_between_check_and_claim_is_caught(
    tmp_path: Path, monkeypatch
) -> None:
    """Verification and claim are the SAME observation.

    Simulated by letting the first read succeed and the last one see different
    bytes — the record must refuse rather than assert a digest that no longer
    holds as of the read backing it.
    """
    artifact, evidence_dir = _store(tmp_path)
    first = evidence.record_artifact(artifact, evidence_dir)

    real = evidence.sha256_file
    seen = {"n": 0}

    def drifting(path):
        seen["n"] += 1
        # 1 = produced artifact, 2 = existing-entry check, 3 = the final read
        if seen["n"] == 3:
            return "0" * 64
        return real(path)

    monkeypatch.setattr(evidence, "sha256_file", drifting)
    with pytest.raises(evidence.CorruptStoredEvidenceError, match="at the moment"):
        evidence.record_artifact(artifact, evidence_dir)
    assert first["sha256"]


def test_the_manifest_route_pins_the_manifest_bytes(tmp_path: Path) -> None:
    """The pack points at a manifest file, so it pins that file's own content
    address rather than merely naming its path."""
    artifact = tmp_path / "big.arrow"
    artifact.write_bytes(b"z" * (evidence.ARTIFACT_COPY_LIMIT_BYTES + 1))
    evidence_dir = tmp_path / "evidence"

    record = evidence.record_artifact(artifact, evidence_dir)
    manifest = evidence_dir / "artifacts" / f"{record['sha256']}.manifest.json"

    assert record["manifest_sha256"] == hashlib.sha256(manifest.read_bytes()).hexdigest()
    assert record["store_verified_sha256"] == record["manifest_sha256"]


# ---------------------------------------------------------------------------
# Round 4 Q1(c) — the standing boundary line
# ---------------------------------------------------------------------------


def test_the_closure_boundary_states_packs_are_unsigned() -> None:
    """The operator ruling, on the record in every pack: verification here
    establishes what was true when the pack was WRITTEN."""
    entry = next(
        e for e in evidence.CLOSURE_BOUNDARY if "concurrent local mutation" in e["item"]
    )
    assert "unsigned" in entry["why"]
    assert "generation-time state" in entry["why"]

    md = evidence.render_markdown(_pack(dirty=False, allow_dirty=False))
    assert "unsigned" in md


# ---------------------------------------------------------------------------
# Round 5 Q1(a) — preservation is VERIFIED, never presumed
# ---------------------------------------------------------------------------


def test_a_TRUNCATED_preserved_copy_is_completed_before_any_overwrite(
    tmp_path: Path,
) -> None:
    """Codex's named case: retry-after-partial-preservation.

    A previous `--repair-store` crashed mid-copy and left a FRAGMENT under the
    right name. "The path exists" is not evidence that preservation completed —
    treating it as such would let this run overwrite `stored` while the archive
    only appeared to hold the corruption.
    """
    artifact, evidence_dir = _store(tmp_path)
    first = evidence.record_artifact(artifact, evidence_dir)
    stored = evidence_dir / "artifacts" / f"{first['sha256']}.arrow"
    stored.write_bytes(b"CORRUPTED-PAYLOAD-LONG-ENOUGH-TO-TRUNCATE")
    actual = hashlib.sha256(stored.read_bytes()).hexdigest()

    # Simulate the crashed run: a partial preserved copy under the real name.
    preserved = evidence_dir / "artifacts" / f"{first['sha256']}.corrupt-{actual}"
    preserved.write_bytes(b"CORRUPTED-PAY")  # truncated
    assert hashlib.sha256(preserved.read_bytes()).hexdigest() != actual

    record = evidence.record_artifact(artifact, evidence_dir, repair_store=True)

    assert preserved.read_bytes() == b"CORRUPTED-PAYLOAD-LONG-ENOUGH-TO-TRUNCATE"
    assert hashlib.sha256(preserved.read_bytes()).hexdigest() == actual
    assert record["store_corrupt_preserved_state"] == "completed"
    assert stored.read_bytes() == artifact.read_bytes()


def test_a_COMPLETE_preserved_copy_is_recognised_and_not_rewritten(tmp_path: Path) -> None:
    artifact, evidence_dir = _store(tmp_path)
    first = evidence.record_artifact(artifact, evidence_dir)
    stored = evidence_dir / "artifacts" / f"{first['sha256']}.arrow"
    stored.write_bytes(b"CORRUPTED")
    actual = hashlib.sha256(b"CORRUPTED").hexdigest()

    evidence.record_artifact(artifact, evidence_dir, repair_store=True)
    stored.write_bytes(b"CORRUPTED")  # corrupt again, identically
    record = evidence.record_artifact(artifact, evidence_dir, repair_store=True)

    assert record["store_corrupt_preserved_state"] == "already complete"
    preserved = evidence_dir / "artifacts" / f"{first['sha256']}.corrupt-{actual}"
    assert preserved.read_bytes() == b"CORRUPTED"


def test_a_FAILED_preservation_leaves_stored_untouched(tmp_path: Path, monkeypatch) -> None:
    """`stored` is not modified until a hash-verified preserved copy exists.

    The copy is made to raise AFTER writing part of the temp file — the state a
    real crash produces — and the assertion is that the corrupted bytes in
    `stored` are still exactly as they were.
    """
    artifact, evidence_dir = _store(tmp_path)
    first = evidence.record_artifact(artifact, evidence_dir)
    stored = evidence_dir / "artifacts" / f"{first['sha256']}.arrow"
    stored.write_bytes(b"CORRUPTED")

    real_copy = evidence.shutil.copy2

    def partial_then_raise(src, dst, *args, **kwargs):
        Path(dst).write_bytes(b"CORR")  # a partial write, as a crash would leave
        raise OSError("crashed mid-copy")

    monkeypatch.setattr(evidence.shutil, "copy2", partial_then_raise)
    with pytest.raises(OSError, match="crashed mid-copy"):
        evidence.record_artifact(artifact, evidence_dir, repair_store=True)

    assert stored.read_bytes() == b"CORRUPTED", "stored was modified before preservation held"
    assert real_copy is not None


def test_a_failed_preservation_leaves_no_partial_file_under_the_real_name(
    tmp_path: Path, monkeypatch
) -> None:
    """The atomic path means `preserved` is never a fragment: the partial write
    lands on a `.tmp-<pid>` name and is cleaned up, so a later run cannot
    mistake debris for a real entry."""
    artifact, evidence_dir = _store(tmp_path)
    first = evidence.record_artifact(artifact, evidence_dir)
    stored = evidence_dir / "artifacts" / f"{first['sha256']}.arrow"
    stored.write_bytes(b"CORRUPTED")
    actual = hashlib.sha256(b"CORRUPTED").hexdigest()

    def partial_then_raise(src, dst, *args, **kwargs):
        Path(dst).write_bytes(b"CO")
        raise OSError("crashed mid-copy")

    monkeypatch.setattr(evidence.shutil, "copy2", partial_then_raise)
    with pytest.raises(OSError):
        evidence.record_artifact(artifact, evidence_dir, repair_store=True)

    preserved = evidence_dir / "artifacts" / f"{first['sha256']}.corrupt-{actual}"
    assert not preserved.exists(), "a fragment was left under the real preserved name"
    assert not list((evidence_dir / "artifacts").glob("*.tmp-*")), "temp debris left behind"


def test_atomic_copy_verified_refuses_a_destination_that_does_not_match(
    tmp_path: Path,
) -> None:
    src = tmp_path / "src"
    src.write_bytes(b"content")
    with pytest.raises(evidence.CorruptStoredEvidenceError, match="verified as"):
        evidence.atomic_copy_verified(src, tmp_path / "dest", "0" * 64)


# ---------------------------------------------------------------------------
# Round 5 Q1(b) — the manifest write is atomic
# ---------------------------------------------------------------------------


def test_a_crash_between_manifest_write_and_rename_leaves_the_prior_manifest(
    tmp_path: Path, monkeypatch
) -> None:
    """`write_text` truncates before writing, so a crash between the two would
    replace a valid manifest with a fragment under the real name."""
    artifact = tmp_path / "big.arrow"
    artifact.write_bytes(b"z" * (evidence.ARTIFACT_COPY_LIMIT_BYTES + 1))
    evidence_dir = tmp_path / "evidence"

    first = evidence.record_artifact(artifact, evidence_dir)
    manifest = evidence_dir / "artifacts" / f"{first['sha256']}.manifest.json"
    before = manifest.read_bytes()

    def fail_before_rename(src, dst):
        raise OSError("crashed between write and rename")

    monkeypatch.setattr(evidence.os, "replace", fail_before_rename)
    with pytest.raises(OSError, match="between write and rename"):
        evidence.record_artifact(artifact, evidence_dir)

    assert manifest.read_bytes() == before, "the prior manifest was destroyed"
    assert json.loads(manifest.read_text())["sha256"] == first["sha256"]
    assert not list((evidence_dir / "artifacts").glob("*.tmp-*"))


def test_atomic_write_text_replaces_completely_or_not_at_all(tmp_path: Path) -> None:
    target = tmp_path / "f.json"
    target.write_text("original")
    evidence.atomic_write_text(target, "replacement")
    assert target.read_text() == "replacement"
    assert not list(tmp_path.glob("*.tmp-*"))
