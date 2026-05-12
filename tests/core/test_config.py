"""Config + cadence + env substitution. SPEC §4."""

from __future__ import annotations

from pathlib import Path

import pytest

from r64_db_engine.core.config import (
    Config,
    load_config,
    parse_cadence,
    substitute_env,
)


def test_parse_cadence_basic():
    assert parse_cadence("5s") == 5
    assert parse_cadence("30s") == 30
    assert parse_cadence("5m") == 300
    assert parse_cadence("2h") == 7200


def test_parse_cadence_below_minimum_rejected():
    with pytest.raises(ValueError, match="below minimum"):
        parse_cadence("1s")


def test_parse_cadence_bad_syntax():
    with pytest.raises(ValueError):
        parse_cadence("forever")
    with pytest.raises(ValueError):
        parse_cadence("30")


def test_substitute_env_replaces_vars():
    out = substitute_env(
        "host: ${PG_HOST}\nuser: ${PG_USER}",
        env={"PG_HOST": "db.example.com", "PG_USER": "ro"},
    )
    assert "db.example.com" in out
    assert "ro" in out


def test_substitute_env_missing_raises():
    with pytest.raises(ValueError, match="missing required environment variable"):
        substitute_env("host: ${PG_HOST}", env={})


def test_load_config_minimal(tmp_path: Path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        """
dialect: postgres
postgres:
  host: localhost
  database: analytics
row64:
  loading_dir: /tmp/loading
  group: PG
tables:
  - source: public.orders
    target: Orders
"""
    )
    c = load_config(cfg, env={})
    assert c.dialect == "postgres"
    assert c.postgres.database == "analytics"
    assert len(c.tables) == 1
    assert c.tables[0].target == "Orders"


def test_load_config_duplicate_targets_rejected(tmp_path: Path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        """
dialect: postgres
postgres:
  database: a
row64:
  loading_dir: /tmp/x
tables:
  - source: a.t
    target: T
  - source: b.t
    target: T
"""
    )
    with pytest.raises(Exception, match="duplicate target"):
        load_config(cfg, env={})


def test_incremental_requires_key(tmp_path: Path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        """
dialect: postgres
postgres:
  database: a
row64:
  loading_dir: /tmp/x
tables:
  - source: a.t
    target: T
    mode: incremental
"""
    )
    with pytest.raises(Exception, match="incremental_key"):
        load_config(cfg, env={})


def test_resolve_table_applies_defaults():
    c = Config.model_validate(
        {
            "dialect": "postgres",
            "postgres": {"database": "a"},
            "row64": {"loading_dir": "/tmp"},
            "defaults": {"cadence": "5m", "mode": "full_refresh", "ascii_sanitize": False},
            "tables": [{"source": "a.t", "target": "T"}],
        }
    )
    resolved = c.resolve_table(c.tables[0])
    assert resolved["cadence_seconds"] == 300
    assert resolved["mode"] == "full_refresh"
    assert resolved["ascii_sanitize"] is False


def test_per_table_overrides_defaults():
    c = Config.model_validate(
        {
            "dialect": "postgres",
            "postgres": {"database": "a"},
            "row64": {"loading_dir": "/tmp"},
            "defaults": {"cadence": "5m", "ascii_sanitize": True},
            "tables": [
                {
                    "source": "a.t",
                    "target": "T",
                    "cadence": "30s",
                    "ascii_sanitize": False,
                }
            ],
        }
    )
    r = c.resolve_table(c.tables[0])
    assert r["cadence_seconds"] == 30
    assert r["ascii_sanitize"] is False
