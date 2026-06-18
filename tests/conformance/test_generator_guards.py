"""Negative tests for the generator/spec fail-fast guards (CodeRabbit Gate A review).

Each guard protects the scaffold generator we are about to clone across five
sibling drivers. An untested guard is the blind-gate pathology one level in —
so these prove each guard's *raise* branch actually fires, not just that the
happy path passes through it.

One test per guard:
  1. generator.regenerate — `spec.dialect` must be a Python identifier.
  2. generator.regenerate — `fixtures_ref` must be "module:attr".
  3. generated coercion — a coercer key absent from coercers.REGISTRY raises.
  4. FixtureCase — `raises` + `roundtrip=True` is a contradiction.
"""

from __future__ import annotations

import dataclasses
import importlib
import sys
from pathlib import Path

import pytest

from r64_db_engine.conformance.coercers import Row64CodecOverflowError
from r64_db_engine.conformance.generator import regenerate
from r64_db_engine.conformance.spec import FixtureCase
from r64_db_engine.drivers.postgres.spec import POSTGRES_SPEC

_FIXTURES_REF = "r64_db_engine.drivers.postgres.spec:POSTGRES_SPEC"


def test_bad_dialect_rejected(tmp_path: Path) -> None:
    """Guard 1: a dialect that can't be a package/class name is refused."""
    spec = dataclasses.replace(POSTGRES_SPEC, dialect="not a valid-identifier")
    with pytest.raises(ValueError, match="valid Python identifier"):
        regenerate(spec, tmp_path, _FIXTURES_REF)


def test_malformed_fixtures_ref_rejected(tmp_path: Path) -> None:
    """Guard 2: a fixtures_ref without the module:attr colon is refused."""
    with pytest.raises(ValueError, match="module:attr"):
        regenerate(POSTGRES_SPEC, tmp_path, "missing_colon_separator")


def test_missing_registry_key_raises_in_generated_coercion(tmp_path: Path) -> None:
    """Guard 3: a coercer_map key absent from coercers.REGISTRY raises at use,
    rather than KeyError-ing opaquely."""
    spec = dataclasses.replace(
        POSTGRES_SPEC,
        dialect="pgbadkey",
        coercer_map={**dict(POSTGRES_SPEC.coercer_map), "text": "nonexistent_key"},
    )
    regenerate(spec, tmp_path, _FIXTURES_REF)
    sys.path.insert(0, str(tmp_path))
    for name in list(sys.modules):
        if name.startswith("pgbadkey_driver"):
            del sys.modules[name]
    try:
        coercion = importlib.import_module("pgbadkey_driver.coercion")
        with pytest.raises(ValueError, match="not in coercers.REGISTRY"):
            coercion.coerce_value("hello", "text")
    finally:
        sys.path.remove(str(tmp_path))
        for name in list(sys.modules):
            if name.startswith("pgbadkey_driver"):
                del sys.modules[name]


def test_fixture_raises_with_roundtrip_true_rejected() -> None:
    """Guard 4: an overflow case (raises set) cannot also claim roundtrip=True —
    rejected values never reach a round-trip frame."""
    with pytest.raises(ValueError, match="cannot have roundtrip=True"):
        FixtureCase(
            "bad_overflow",
            "bigint",
            2**40,
            "int64",
            roundtrip=True,
            raises=Row64CodecOverflowError,
            raises_stage="write",
        )
