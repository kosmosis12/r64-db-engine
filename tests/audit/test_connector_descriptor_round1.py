"""Codex round-1 adversarial reproducers for feat/connector-descriptor.

These tests are strict xfails so the audit branch stays runnable while preserving
the exact contracts the fix pass must make green.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from factory import generate_descriptor_artifacts as gen
from r64_db_engine.core.descriptor import ErrorMap
from r64_db_engine.drivers.postgres.descriptor import POSTGRES


@pytest.mark.xfail(
    strict=True,
    reason="BLOCK(d): an unverified author-written JSON pack currently creates green",
)
def test_forged_last_green_pack_cannot_create_a_passing_state(tmp_path) -> None:
    """Green must require oracle evidence, not merely a writable PASS-shaped JSON file."""
    last_green = tmp_path / "last-green"
    last_green.mkdir()
    (last_green / "EVIDENCE-forged.json").write_text(json.dumps({"verdict": "PASS"}))

    state = gen.conformance_state("forged", last_green, briefs={})

    assert state["state"] != gen.PASSING


@pytest.mark.xfail(
    strict=True,
    reason="BLOCK(c): emit-boundary validation only scans required_env_keys syntax",
)
def test_environment_value_cannot_reach_any_generated_projection(monkeypatch, tmp_path) -> None:
    """An uppercase env value is syntactically indistinguishable from a key today."""
    secret = "ULTRASECRET2026"
    monkeypatch.setenv("AUDIT_DATABASE_PASSWORD", secret)
    poisoned = replace(POSTGRES, required_env_keys=(secret,))
    monkeypatch.setattr(gen, "descriptors", lambda: {"postgres": poisoned})

    artifacts = gen.generate(tmp_path)

    assert all(secret not in body for body in artifacts.values())


@pytest.mark.xfail(
    strict=True,
    reason="BLOCK(c): arbitrary descriptor prose is emitted without a value scan",
)
def test_provider_secret_cannot_reach_artifacts_through_operator_message(
    monkeypatch, tmp_path
) -> None:
    """Interpolation checks do not reject a secret already present in authored prose."""
    secret = "provider-secret-hunter2"
    poisoned_error = ErrorMap(
        pattern=r"authentication failed: .*",
        reason_code="auth.rejected",
        operator_message=secret,
    )
    poisoned = replace(POSTGRES, custom_errors=(poisoned_error,))
    monkeypatch.setattr(gen, "descriptors", lambda: {"postgres": poisoned})

    artifacts = gen.generate(tmp_path)

    assert all(secret not in body for body in artifacts.values())


@pytest.mark.xfail(
    strict=True,
    reason="NOTE(P1): roster and index trust incidental descriptor mapping order",
)
def test_generator_is_deterministic_when_registry_mapping_order_changes(monkeypatch) -> None:
    """Law 1 must hold even when registry construction order is shuffled."""
    baseline = gen.generate()
    real_descriptors = gen.descriptors()
    monkeypatch.setattr(
        gen,
        "descriptors",
        lambda: dict(reversed(list(real_descriptors.items()))),
    )

    shuffled = gen.generate()

    assert shuffled == baseline


@pytest.mark.xfail(
    strict=True,
    reason="NOTE(P2): extras validation checks existence, not dependency ownership",
)
def test_descriptor_cannot_claim_an_unrelated_install_extra(monkeypatch, tmp_path) -> None:
    """A base-dependency Postgres driver must not advertise the metrics extra."""
    mislabeled = replace(POSTGRES, extras_package="metrics")
    monkeypatch.setattr(gen, "descriptors", lambda: {"postgres": mislabeled})

    artifacts = gen.generate(tmp_path)
    postgres_doc = artifacts[tmp_path / "docs/connectors/postgres.md"]

    assert "r64-db-engine[metrics]" not in postgres_doc
