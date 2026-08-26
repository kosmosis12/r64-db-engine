"""Round-2 adversarial reproducers for feat/connector-descriptor.

Provenance, stated because it bears on how much these are worth. The round-2
audit was begun by Codex and abandoned mid-run when its provider's content
filter suppressed every model turn. Codex's one completed result — the
import-aware AST firewall walk (P3) — is not re-tested here; it passed under
its own observation. P1 and P2 below were specified by a third agent and
executed here. The auditor-of-record is therefore the spec-plus-execution pair,
not an independent second model, and that is weaker than the round-1 lineage.

P1 was a real defect and is fixed; its test is a standing regression test and
goes red the moment the fix is backed out. P2 was confirmed as reproducible but
ruled a DECLARED LIMIT rather than a defect, so its test now asserts the limit
in the positive — see its docstring.
"""

from __future__ import annotations

import hashlib
import json
import traceback
from dataclasses import replace
from pathlib import Path

import pytest

from factory import generate_descriptor_artifacts as gen
from r64_db_engine.drivers.postgres.descriptor import POSTGRES

# Synthetic and clearly fake. Neither is a credential; both are shaped like one
# so that the question "did provider-shaped content survive?" has a literal,
# greppable answer.
FAKE_TOKEN = "FAKE-TOKEN-c9f2a41b7e5d3096"
FAKE_DSN = "postgresql://svc_user:FAKEPASS-8b21d7f4@db.internal:5432/appdb"


def _rendered(exc: BaseException) -> str:
    """The FULL operator-visible rendering, including the __cause__/__context__ chain.

    `chain=True` is the point: a message that is clean on its own still leaks if
    the exception it was raised `from` carries the value into the traceback.
    """
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__, chain=True))


def _poisoned(**fields: object):
    """A descriptor carrying `fields`, bypassing `validate()`.

    `DriverMetadata.validate()` runs in `__post_init__`, so a value-shaped
    `required_env_keys` entry cannot be constructed through `replace()` at all.
    Bypassing it is not cheating the probe — it is the exact scenario
    `_assert_no_values` documents as its reason to exist: "a check at the
    boundary you actually care about is worth more than an appeal to one
    upstream that a refactor could route around." This probe is that refactor.
    """
    meta = replace(POSTGRES)
    for name, value in fields.items():
        object.__setattr__(meta, name, value)
    return meta


class _StripFails(str):
    """Prose that survives rendering and JSON encoding but dies in the scrubber.

    Deliberately raises with a message carrying NO synthetic value. An injected
    exception that quotes the secret would prove only that the probe put it
    there; the question is whether content the DESCRIPTOR carries rides out on
    a failure of the scrubber itself.
    """

    def strip(self, *args: object) -> str:
        raise RuntimeError("scrubber machinery failure")


def test_no_emit_path_exception_surfaces_provider_shaped_content(monkeypatch, tmp_path) -> None:
    """The scrub boundary must cover the emit path's FAILURES, not only its return.

    `generate()` scrubs the finished text of every artifact — on the success
    path. Every `raise` above that line is outside the boundary, and the
    refusal messages interpolate descriptor content verbatim. `main()` then
    prints the exception straight to stderr.

    Two arms, both asserting the same property against the full rendered chain:

      a. an exception raised inside the emit path proper;
      b. an exception raised inside the scrubber's own machinery.
    """
    # -- arm (a): the guard against a credential in a name slot quotes it ----
    monkeypatch.setattr(gen, "descriptors", lambda: {"postgres": _poisoned(
        required_env_keys=(FAKE_DSN,),
    )})
    with pytest.raises(Exception) as caught:
        gen.generate(tmp_path)
    emit_path = _rendered(caught.value)

    # -- arm (b): the scrubber dies; does the descriptor's content ride out? -
    monkeypatch.setattr(gen, "descriptors", lambda: {"postgres": _poisoned(
        notes=(_StripFails(f"contact ops with {FAKE_TOKEN} to rotate"),),
    )})
    with pytest.raises(Exception) as caught:
        gen.generate(tmp_path)
    scrubber_path = _rendered(caught.value)

    for label, rendered in (("emit path", emit_path), ("scrubber", scrubber_path)):
        assert FAKE_DSN not in rendered, f"connection URL survived the {label} exception"
        assert FAKE_TOKEN not in rendered, f"token survived the {label} exception"


def _oracle_shaped_pack(dialect: str, digest: str) -> dict[str, object]:
    """A pack satisfying every condition `_unauthenticated` imposes.

    Nothing here was produced by a battery run. Each field is written by hand
    to agree with each other field, which is all the authentication asks for.
    """
    names = [
        "registry_admission", "schema_exactness", "aggregate_parity",
        "null_discriminators", "tz_boundary", "watermark_refusal",
        "ipc_block_structure", "checksum", "recipe_destination_pinning",
        "zero_copy_serve_gate",
    ]
    return {
        "dialect": dialect,
        "verdict": "PASS",
        "verdict_status": "PASS",
        "generated_utc": "2026-08-26T09:00:00Z",
        "table": "public.orders",
        "ratifies_head": True,
        "provenance": {"git": {"commit": "f" * 40}},
        "tally": {"PASS": 9, "FAIL": 0, "SKIPPED": 1},
        "artifact": {"sha256_pull1": digest, "sha256_pull2": digest},
        "checks": [
            {"name": n, "status": "SKIPPED" if n == "zero_copy_serve_gate" else "PASS"}
            for n in names
        ],
    }


def test_packs_are_attestation_not_authentication(tmp_path) -> None:
    """A self-consistent evidence pack IS accepted. That is the declared limit.

    # What this asserts and why it is asserted in the positive

    `_unauthenticated()` re-derives the verdict from the pack's own `checks` and
    requires the pack's claims to agree. Every input to that agreement is a
    field of the same writable file, so the pack is checked against ITSELF: a
    hand-authored pack for a dialect that never existed is accepted, and a real
    pack tampered post-production is accepted. Round 2 confirmed both.

    This is not a defect. It is the evidence system's declared boundary, stated
    in `factory/evidence.py:687` — the limits table entry for "concurrent local
    mutation of the store or of the pack itself":

        "packs attest generation-time state; they are unsigned and do not
        defend against concurrent local mutation of the store or of the pack
        itself. An attacker with local write access can rewrite the pack more
        easily than the bytes it points at, so verification here establishes
        what was true when the pack was written — not what is true when it is
        read."

    Packs are ATTESTATION. They record what a battery run observed. They are
    not AUTHENTICATION and were never claimed to be by the limits table.

    # The compensating control

    Authentication's trust anchor is OPERATOR MERGE PROVENANCE — a human
    merging from a fresh shell — not a mark inside the repo. Signing in-repo
    would add ceremony without separation: any key the sweep can sign with is
    writable by the same process that writes the packs, so it fails the
    identity dominance test the same way PARTFORGE v0 did. A forged pack still
    has to survive review to reach `main`.

    # What `460ee1a` did and did not deliver

    It genuinely closed round 1's finding: a bare `{"verdict": "PASS"}` no
    longer creates green, and that arm is still guarded (see
    `test_connector_descriptor_round1.py`). What its commit message overclaimed
    is unforgeability — "cannot be made to without running one". A
    self-consistent file can be made without running one, as below. The
    correction is recorded in `CODEX-AUDIT-connector-descriptor-round2.md`;
    the commit message itself is immutable history.

    # If this test goes RED

    That is the DESIRED signal, not a regression: it means somebody added real
    authentication to the evidence path. Do not "fix" this test — verify the
    new mechanism, then update this docstring, the limits table entry at
    `factory/evidence.py:687`, and the round-2 report.
    """
    last_green = tmp_path / "last-green"
    last_green.mkdir()

    # -- a dialect that has never existed, digests over a literal ----------
    forged = _oracle_shaped_pack(
        "forgedsql", hashlib.sha256(b"nothing was ever pulled").hexdigest()
    )
    (last_green / "EVIDENCE-forgedsql.json").write_text(json.dumps(forged))
    state = gen.conformance_state("forgedsql", last_green, briefs={})
    assert state["state"] == gen.PASSING, (
        "a self-consistent hand-authored pack was rejected — if this is real "
        "authentication rather than an accident, see this test's docstring"
    )

    # -- a real pack, tampered, with no mark left to detect it -------------
    real_path = Path("factory/evidence/last-green/EVIDENCE-clickhouse.json")
    tampered = json.loads(real_path.read_text())
    # The byte-identity the `checksum` check proved, re-pointed at bytes that
    # were never pulled. Still two well-formed, equal sha256 digests.
    fake = hashlib.sha256(b"different bytes entirely").hexdigest()
    tampered["artifact"]["sha256_pull1"] = fake
    tampered["artifact"]["sha256_pull2"] = fake
    tampered["table"] = "public.attacker_controlled"
    tampered["provenance"]["git"]["commit"] = "0" * 40
    (last_green / "EVIDENCE-clickhouse.json").write_text(json.dumps(tampered))
    state = gen.conformance_state("clickhouse", last_green, briefs={})
    assert state["state"] == gen.PASSING, (
        "a tampered pack was rejected — see this test's docstring before "
        "treating this as a failure"
    )

    # The round-1 finding remains closed: a pack carrying no oracle output at
    # all is still refused. Attestation is a floor, not the absence of one.
    (last_green / "EVIDENCE-asserted.json").write_text(json.dumps({"verdict": "PASS"}))
    assert gen.conformance_state("asserted", last_green, briefs={})["state"] != gen.PASSING
