"""Supabase connection profile. Config-layer refusals and normalizations.

The governing rule is the PG-011 one: refuse loudly, never degrade silently.
Every case below is a configuration that would otherwise connect fine and then
either fail partway through a pull or quietly lose a guarantee.
"""

from __future__ import annotations

import pytest

from r64_db_engine.core.config import Config
from r64_db_engine.core.profile import ProfileError
from r64_db_engine.profiles import resolve
from r64_db_engine.profiles.supabase import (
    SESSION_POOLER_PORT,
    TRANSACTION_POOLER_PORT,
    SupabaseProfile,
)


def _config(**postgres: object) -> Config:
    block = {"database": "postgres", **postgres}
    return Config.model_validate(
        {
            "dialect": "postgres",
            "profile": "supabase",
            "postgres": block,
            "row64": {"loading_dir": "/tmp/r64-profile-test"},
            "tables": [{"source": "public.t", "target": "T"}],
        }
    )


# ---- registry --------------------------------------------------------


def test_profile_resolves_by_name():
    assert resolve("supabase") is SupabaseProfile


def test_unknown_profile_lists_available_names():
    with pytest.raises(ValueError, match="unknown profile 'redshift'"):
        resolve("redshift")


def test_profile_dialect_mismatch_is_refused():
    cfg = Config.model_validate(
        {
            "dialect": "clickhouse",
            "profile": "supabase",
            "clickhouse": {"database": "default"},
            "row64": {"loading_dir": "/tmp/r64-profile-test"},
            "tables": [{"source": "t", "target": "T"}],
        }
    )
    with pytest.raises(ValueError, match="applies to dialect 'postgres'"):
        cfg.driver_config()


# ---- transaction-mode pooling: the headline refusal ------------------


def test_transaction_mode_pooler_port_is_refused():
    """6543 breaks server-side prepared statements. Refuse, do not degrade.

    The failure this prevents is a pull that starts cleanly and dies partway
    through with "prepared statement ... does not exist" — the exact class of
    mysterious mid-pull failure the profile exists to make impossible.
    """
    cfg = _config(host="proj.pooler.supabase.com", port=TRANSACTION_POOLER_PORT)
    with pytest.raises(ProfileError) as raised:
        cfg.driver_config()
    message = str(raised.value)
    assert "transaction-mode" in message
    assert str(TRANSACTION_POOLER_PORT) in message
    assert "prepared statement" in message
    # The error must say what to do instead, not merely that it refused.
    assert str(SESSION_POOLER_PORT) in message


def test_transaction_mode_port_refused_even_behind_a_custom_hostname():
    """The port is the tell. A custom DNS name must not smuggle 6543 past."""
    cfg = _config(host="db.internal.example.com", port=TRANSACTION_POOLER_PORT)
    with pytest.raises(ProfileError, match="transaction-mode"):
        cfg.driver_config()


# ---- session-mode pooling: allowed, but never prepare ----------------


def test_session_mode_pooler_forces_prepare_threshold_none():
    cfg = _config(host="proj.pooler.supabase.com", port=SESSION_POOLER_PORT)
    out = cfg.driver_config()
    assert out["prepare_threshold"] is None


def test_pooler_overrides_an_explicit_prepare_threshold():
    """An operator cannot opt back into preparing behind a pooler."""
    cfg = _config(
        host="proj.pooler.supabase.com", port=SESSION_POOLER_PORT, prepare_threshold=5
    )
    assert cfg.driver_config()["prepare_threshold"] is None


def test_direct_hosted_connection_keeps_prepared_statements():
    """No pooler in the path — preparing is safe and stays on."""
    cfg = _config(host="db.abcdefgh.supabase.co", port=5432)
    assert cfg.driver_config()["prepare_threshold"] == 5


# ---- transport -------------------------------------------------------


def test_non_loopback_upgrades_prefer_to_require():
    """`prefer` falls back to plaintext if the server declines TLS."""
    cfg = _config(host="db.abcdefgh.supabase.co", sslmode="prefer")
    assert cfg.driver_config()["sslmode"] == "require"


def test_non_loopback_refuses_explicit_sslmode_disable():
    cfg = _config(host="db.abcdefgh.supabase.co", sslmode="disable")
    with pytest.raises(ProfileError, match="sslmode=disable"):
        cfg.driver_config()


def test_stronger_sslmode_is_not_weakened():
    cfg = _config(host="db.abcdefgh.supabase.co", sslmode="verify-full")
    assert cfg.driver_config()["sslmode"] == "verify-full"


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1"])
def test_loopback_is_left_alone(host: str):
    """The local dev stack is direct Postgres on loopback. Do not touch it."""
    cfg = _config(host=host, port=54322, sslmode="disable")
    out = cfg.driver_config()
    assert out["sslmode"] == "disable"
    assert out["prepare_threshold"] == 5


def test_private_lan_address_is_still_treated_as_remote():
    """`is it on the wire` is the question, not `is it nearby`."""
    cfg = _config(host="192.168.1.50", sslmode="prefer")
    assert cfg.driver_config()["sslmode"] == "require"


# ---- no profile: nothing changes -------------------------------------


def test_without_a_profile_the_config_is_untouched():
    cfg = Config.model_validate(
        {
            "dialect": "postgres",
            "postgres": {"database": "postgres", "host": "db.example.com"},
            "row64": {"loading_dir": "/tmp/r64-profile-test"},
            "tables": [{"source": "public.t", "target": "T"}],
        }
    )
    out = cfg.driver_config()
    assert out["sslmode"] == "prefer"
    assert out["prepare_threshold"] == 5
