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


def test_load_clickhouse_config(tmp_path: Path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        """
dialect: clickhouse
clickhouse:
  host: ch.example.com
  port: 8443
  database: analytics
  user: svc
  password: secret
  secure: true
row64:
  loading_dir: /tmp/loading
tables:
  - source: analytics.orders
    target: Orders
"""
    )
    c = load_config(cfg, env={})
    assert c.dialect == "clickhouse"
    assert c.clickhouse is not None
    assert c.clickhouse.secure is True
    assert c.driver_config()["host"] == "ch.example.com"


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


# --------------------------------------------------------------------------
# PG-010: a dialect core has never heard of must be configurable
#
# The permitted top-level keys are exactly {declared fields} u {registered
# dialects}. These tests pin both halves, and pin that REGISTRATION — not an
# edit to core/config.py — is what makes a dialect valid.
# --------------------------------------------------------------------------


class _FakeDriver:
    """Stands in for a driver core has never heard of. Never connected."""

    @classmethod
    def dialect_name(cls) -> str:
        return "dynamodb"


@pytest.fixture
def registered(monkeypatch):
    """Register a driver under a name core does not know, and nothing else.

    This is exactly what merging a new driver does: one entry in the registry.
    No edit to `core/config.py` accompanies it — that is the whole PG-010
    claim, so the test earns it by making registration the only change.
    """

    def _register(name: str = "dynamodb"):
        from r64_db_engine import drivers

        monkeypatch.setitem(drivers.DRIVERS, name, _FakeDriver)
        return name

    return _register


def _ddb_config(**overrides) -> dict:
    base = {
        "dialect": "dynamodb",
        "dynamodb": {
            "region": "us-east-1",
            "scan_segments": 4,
            "consistent_read": True,
        },
        "row64": {"loading_dir": "/tmp"},
        "tables": [{"source": "Orders", "target": "Orders"}],
    }
    base.update(overrides)
    return base


def test_registered_dialect_is_accepted_and_its_block_passed_opaquely(registered):
    """The PG-010 reproducer: `dialect: dynamodb` used to be unrepresentable.

    `Config.dialect` was `Literal["postgres", "clickhouse"]`, so a complete,
    locally-proven driver could not be named in a config file — its own
    integration tests had to declare `dialect: postgres` with
    `database: "unused-config-vessel"` to get past validation.
    """
    registered()
    c = Config.model_validate(_ddb_config())

    assert c.dialect == "dynamodb"
    assert c.driver_config() == {
        "region": "us-east-1",
        "scan_segments": 4,
        "consistent_read": True,
    }
    assert "database" not in c.driver_config()  # no vessel needed


def test_registered_dialect_block_is_not_validated_by_core(registered):
    """Driver-specific keys are the driver's business, not core's.

    `scan_segments: 99` is outside the real driver's documented 1..32 range.
    Core must pass it through untouched and let `DynamoDBDriver.connect()`
    refuse it — core validating it would mean core knowing the key exists,
    which is the leak this fix closes.
    """
    registered()
    c = Config.model_validate(
        _ddb_config(dynamodb={"region": "us-east-1", "scan_segments": 99, "made_up": "x"})
    )
    assert c.driver_config()["scan_segments"] == 99
    assert c.driver_config()["made_up"] == "x"


def test_core_needed_no_edit_to_learn_the_new_dialect(registered):
    """The dialect name is not special-cased anywhere in core.

    A second, entirely fictional driver registers and configures identically,
    which it could not if `dynamodb` had been hardcoded somewhere.
    """
    registered("acmedb")
    c = Config.model_validate(
        {
            "dialect": "acmedb",
            "acmedb": {"cluster": "a1"},
            "row64": {"loading_dir": "/tmp"},
            "tables": [{"source": "t", "target": "T"}],
        }
    )
    assert c.driver_config() == {"cluster": "a1"}


# -- condition 1(a): a misspelled declared field is refused -----------------


def test_misspelled_declared_field_is_refused(registered):
    """`extra="allow"` must not have cost us the typo protection.

    A misspelled `telemtry:` would otherwise be swallowed silently and the
    daemon would run with default telemetry.
    """
    registered()
    cfg = _ddb_config()
    cfg["telemtry"] = {"log_level": "debug"}
    with pytest.raises(Exception, match="unknown top-level config key"):
        Config.model_validate(cfg)


def test_misspelled_dialect_block_is_refused(registered):
    """`postgress:` is a typo, not a dialect, even though it looks like one."""
    registered()
    with pytest.raises(Exception, match="unknown top-level config key"):
        Config.model_validate(
            {
                "dialect": "postgres",
                "postgres": {"database": "a"},
                "postgress": {"database": "typo"},
                "row64": {"loading_dir": "/tmp"},
                "tables": [{"source": "a.t", "target": "T"}],
            }
        )


# -- condition 1(b): an UNREGISTERED dialect-shaped block is refused --------


def test_unregistered_dialect_is_refused_listing_registered_dialects():
    """A dialect-shaped block whose driver is not registered must NOT pass.

    Without the registry check this validated fine and only failed later, at
    driver resolution. `dynamodb` is the live case: the driver is written and
    locally proven but unmerged, so until Gate C it is not a configurable
    dialect and saying so at config time is the honest answer.
    """
    with pytest.raises(Exception) as exc:
        Config.model_validate(_ddb_config())

    message = str(exc.value)
    assert "unknown dialect 'dynamodb'" in message
    assert "registered: clickhouse, postgres" in message


def test_unregistered_dialect_block_alongside_a_valid_dialect_is_refused():
    """The block is refused as a key too, not just as a `dialect:` value."""
    with pytest.raises(Exception) as exc:
        Config.model_validate(
            {
                "dialect": "postgres",
                "postgres": {"database": "a"},
                "dynamodb": {"region": "us-east-1"},
                "row64": {"loading_dir": "/tmp"},
                "tables": [{"source": "a.t", "target": "T"}],
            }
        )

    message = str(exc.value)
    assert "unknown top-level config key(s): dynamodb" in message
    assert "registered: clickhouse, postgres" in message


def test_error_lists_the_dialects_registered_right_now(registered):
    """The listing is read from the registry, so it grows when a driver lands."""
    with pytest.raises(Exception, match="unknown dialect") as before:
        Config.model_validate(_ddb_config())
    assert "registered: clickhouse, postgres" in str(before.value)

    registered()  # simulate Gate C merging the DynamoDB driver

    with pytest.raises(Exception, match="unknown dialect") as after:
        Config.model_validate(
            {
                "dialect": "nosuchdb",
                "row64": {"loading_dir": "/tmp"},
                "tables": [{"source": "t", "target": "T"}],
            }
        )
    assert "registered: clickhouse, dynamodb, postgres" in str(after.value)


# -- the rest of the allow-set contract ------------------------------------


def test_registered_dialect_without_its_block_is_refused(registered):
    registered()
    cfg = _ddb_config()
    del cfg["dynamodb"]
    with pytest.raises(Exception, match="dynamodb config is required"):
        Config.model_validate(cfg)


def test_dialect_block_must_be_a_mapping(registered):
    registered()
    with pytest.raises(Exception, match="must be a mapping"):
        Config.model_validate(_ddb_config(dynamodb="us-east-1"))


def test_a_dialect_named_block_does_not_shadow_a_real_field(registered):
    """`dialect: tables` must not hand the driver the table list.

    `_source_block` reads typed fields by name, so a config naming a real field
    as its dialect could otherwise smuggle it through. Registered here so the
    test reaches `_source_block` rather than stopping at the registry check.
    """
    registered("tables")
    with pytest.raises(Exception, match="tables config is required"):
        Config.model_validate(
            {
                "dialect": "tables",
                "row64": {"loading_dir": "/tmp"},
                "tables": [{"source": "a.t", "target": "T"}],
            }
        )


def test_dialect_block_profile_key_does_not_collide_with_the_connection_profile(
    registered,
):
    """`dynamodb.profile` (an AWS profile) is not `profile:` (a ConnectionProfile).

    Two unrelated things share the word. The AWS one lives inside the dialect
    block and must reach the driver untouched; the connection-profile one is
    top-level and is resolved against the profile registry. Worth pinning
    because conflating them would silently send an AWS profile name into
    `profiles.resolve()`.
    """
    registered()
    c = Config.model_validate(
        _ddb_config(dynamodb={"region": "us-east-1", "profile": "prod-ro"})
    )
    assert c.profile is None
    assert c.driver_config()["profile"] == "prod-ro"


def test_foreign_top_level_state_is_still_refused(registered):
    """The premise `meshroad/sources/serves.json` documents still holds.

    That file keeps GUI/cockpit state out of the engine YAML because "Config
    forbids extra top-level keys". Enforcement moved from pydantic's
    `extra="forbid"` to `_reject_unknown_top_level_keys`, so this pins that the
    effect is unchanged.
    """
    registered()
    cfg = _ddb_config()
    cfg["serves"] = {"Orders": {"addr": "127.0.0.1:8802"}}
    with pytest.raises(Exception, match="unknown top-level config key"):
        Config.model_validate(cfg)
