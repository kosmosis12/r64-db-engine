"""Round-2 adversarial reproducers for feat/connector-descriptor.

Provenance, stated because it bears on how much these are worth. The round-2
audit was begun by Codex and abandoned mid-run when its provider's content
filter suppressed every model turn. Codex's one completed result — the
import-aware AST firewall walk (P3) — is not re-tested here; it passed under
its own observation. P1 and P2 below were specified by a third agent and
executed here. The auditor-of-record is therefore the spec-plus-execution pair,
not an independent second model, and that is weaker than the round-1 lineage.

Both are strict xfails: they encode contracts the fix pass must satisfy, and
they go red again the moment their fix is backed out.
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


@pytest.mark.xfail(
    strict=True,
    reason="BLOCK(r2-p1): the emit scrub boundary wraps only generate()'s success "
    "return; every refusal above it interpolates descriptor content verbatim",
)
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


@pytest.mark.xfail(
    strict=True,
    reason="BLOCK(r2-p2): _unauthenticated checks the pack against itself, and every "
    "input to that consistency is under the writer's control",
)
def test_green_cannot_be_authored_by_hand_or_survive_tampering(tmp_path) -> None:
    """Green must originate from oracle evidence, not from a self-consistent file.

    Arm 1 forges a pack no battery ever produced. Arm 2 takes a REAL pack and
    tampers evidence fields post-production. If either renders passing, the
    authentication is a shape check — a richer one than reading
    `pack["verdict"]`, but the same species, because every input to its
    consistency is under the writer's control.
    """
    last_green = tmp_path / "last-green"
    last_green.mkdir()

    # -- arm 1: a dialect that has never existed, digests over a literal ----
    forged = _oracle_shaped_pack(
        "forgedsql", hashlib.sha256(b"nothing was ever pulled").hexdigest()
    )
    (last_green / "EVIDENCE-forgedsql.json").write_text(json.dumps(forged))
    state = gen.conformance_state("forgedsql", last_green, briefs={})
    assert state["state"] != gen.PASSING, "a hand-authored pack created green"

    # -- arm 2: a real pack, tampered, with no mark left to detect it -------
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
    assert state["state"] != gen.PASSING, "a tampered pack still rendered green"
