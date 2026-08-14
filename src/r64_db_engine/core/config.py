"""Config models + YAML loader. SPEC §4."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_ENV_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")
_DURATION_PATTERN = re.compile(r"^(\d+)\s*(s|m|h)$")
_MIN_CADENCE_SECONDS = 5


class PostgresConfig(BaseModel):
    host: str = "localhost"
    port: int = 5432
    database: str
    user: str | None = None
    password: str | None = None
    sslmode: Literal[
        "disable", "allow", "prefer", "require", "verify-ca", "verify-full"
    ] = "prefer"
    application_name: str = "r64-db-engine"
    connect_timeout: int = 10
    statement_timeout: int = 300
    # psycopg's automatic-prepared-statement threshold. `None` disables
    # preparing entirely, which is required whenever a connection pooler sits
    # in the path. Default 5 is psycopg's own default, so an untouched config
    # behaves exactly as it did before this knob existed.
    prepare_threshold: int | None = 5


class ClickHouseConfig(BaseModel):
    host: str = "localhost"
    port: int = 8123
    database: str
    user: str | None = None
    password: str | None = None
    secure: bool = False
    connect_timeout: int = 10


class Row64Config(BaseModel):
    loading_dir: str
    group: str = "PostgresSource"


class DefaultsConfig(BaseModel):
    cadence: str = "60s"
    mode: Literal["full_refresh", "incremental"] = "full_refresh"
    max_rows: int | None = None
    ascii_sanitize: bool = True


class TableConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    target: str
    mode: Literal["full_refresh", "incremental"] | None = None
    incremental_key: str | None = None
    incremental_type: Literal["timestamp", "int"] = "timestamp"
    cadence: str | None = None
    max_rows: int | None = None
    ascii_sanitize: bool | None = None

    @model_validator(mode="after")
    def _check_incremental(self) -> TableConfig:
        if self.mode == "incremental" and not self.incremental_key:
            raise ValueError(
                f"table '{self.target}': incremental mode requires incremental_key"
            )
        return self


class TelemetryConfig(BaseModel):
    log_level: Literal["debug", "info", "warning", "error"] = "info"
    log_format: Literal["json", "text"] = "json"
    health_port: int = 8765
    metrics_port: int = 0


class RuntimeConfig(BaseModel):
    worker_pool_size: int = Field(default=4, ge=1, le=64)
    state_dir: str = "~/.r64-db-engine"
    shutdown_grace_seconds: int = Field(default=30, ge=1)


class SinkConfig(BaseModel):
    """Output-sink selection. Core names ZERO sinks — see `core/sink.py`.

    `type` is a free-form string resolved against the sink registry at daemon
    startup, NOT a `Literal[...]`. That is deliberate and load-bearing: it is
    the same shape `dialect` and `profile` use, for the same reason. Adding a
    sink must require zero edits to this file.

    Sink-specific options are accepted as extra keys and passed through
    opaquely, exactly as `Driver.connect()` receives an opaque config dict.
    """

    model_config = ConfigDict(extra="allow")

    type: str

    def options(self) -> dict[str, Any]:
        """Sink-specific keys only, with the selector removed."""
        return {k: v for k, v in self.model_dump().items() if k != "type"}


# Dialects for which core still carries a typed config model. This is NOT the
# set of supported dialects and nothing may treat it as one — a dialect absent
# from here configures fine, as an opaque block validated by its own driver.
# It exists only so that `_source_block` can tell a legacy typed field from an
# arbitrary top-level key, and it shrinks as those models move driver-side.
_TYPED_BLOCKS = frozenset({"postgres", "clickhouse"})


def _registered_dialects() -> frozenset[str]:
    """Dialect names the driver registry currently knows.

    Imported lazily, from inside validation, so `core/` never imports a
    concrete driver at module scope — the same discipline `_apply_profile`
    uses for the profile registry. `core.config` therefore stays importable on
    its own, but *validating* a config does pull in every registered driver's
    third-party dependencies (psycopg, clickhouse_connect, boto3 after Gate C).
    That coupling is the cost of validating dialect names against the registry
    instead of against a constant, and it is deliberate: a constant here would
    be PG-010 again.
    """
    from r64_db_engine.drivers import DRIVERS

    return frozenset(DRIVERS)


class Config(BaseModel):
    """Top-level config.

    `dialect` is a free-form string resolved against the driver registry, NOT a
    `Literal[...]`. That is PG-010: core used to enumerate the dialects it knew,
    so DynamoDB — a driver that is complete and locally proven — could not be
    named in a config file at all, and its own integration tests had to smuggle
    it through as `dialect: postgres` with `database: "unused-config-vessel"`.
    A driver that cannot be configured is not really shipped.

    The dialect's config block is whatever top-level key matches the dialect
    name. Core keeps typed models for `postgres:` and `clickhouse:` because they
    already existed and carry real validation (`sslmode`, ports, timeouts), but
    a dialect core has never heard of gets its block passed through opaquely,
    exactly as `SinkConfig` passes sink options and as `Driver.connect()`
    already expects. The driver validates its own keys — the DynamoDB driver,
    for instance, refuses `scan_segments` outside 1..32 itself.

    `extra="allow"` is therefore load-bearing rather than lax: unknown keys are
    still refused by `_reject_unknown_top_level_keys` below, which keeps the
    typo protection `extra="forbid"` gave. The permitted set is exactly
    `{declared fields} u {registered dialects}` — checked against the driver
    registry, not against any list kept in this file.

    Note for downstream consumers: "r64-db-engine's Config forbids extra
    top-level keys" remains TRUE IN EFFECT, but it is now enforced by that
    validator rather than by pydantic's `extra="forbid"`. Foreign top-level
    state (e.g. GUI or cockpit bookkeeping) is still refused, because it is not
    a registered dialect name.
    """

    model_config = ConfigDict(extra="allow")

    dialect: str = "postgres"
    postgres: PostgresConfig | None = None
    clickhouse: ClickHouseConfig | None = None
    # Optional named deployment shape applied over the selected dialect's
    # config. A free-form string resolved against the profile registry at
    # config time, NOT a `Literal[...]` — same reasoning as `SinkConfig.type`
    # below: enumerating profile names here would clone the PG-010 leak onto a
    # third axis. Absent means "no profile", so every existing config is
    # untouched.
    profile: str | None = None
    row64: Row64Config
    # Optional: absent means "the registry's default sink, configured from the
    # legacy `row64:` block", so every pre-sink config keeps working untouched.
    sink: SinkConfig | None = None
    defaults: DefaultsConfig = Field(default_factory=DefaultsConfig)
    tables: list[TableConfig]
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)

    @model_validator(mode="after")
    def _reject_unknown_top_level_keys(self) -> Config:
        """Restore `extra="forbid"`'s typo protection against the registry.

        The permitted top-level keys are exactly:

            {declared config fields} u {registered dialect names}

        Pydantic enforces the first half natively; this enforces the second.
        Without it, `extra="allow"` would silently swallow a misspelled
        `telemtry:` block and run with the default telemetry config.

        The registry — not a constant in this file — is what says which dialect
        names are real, so registering a driver is still the only step a new
        dialect needs. An unregistered dialect is refused HERE, at config time,
        rather than later at driver resolution: PG-011 doctrine is to refuse
        loudly and early, and a config naming a driver that does not exist is
        wrong the moment it is written.
        """
        registered = _registered_dialects()
        listing = ", ".join(sorted(registered)) or "(none)"

        if self.dialect not in registered:
            raise ValueError(
                f"unknown dialect '{self.dialect}' (registered: {listing})"
            )

        unknown = set(self.model_extra or {}) - registered
        if unknown:
            raise ValueError(
                f"unknown top-level config key(s): {', '.join(sorted(unknown))}. "
                f"Permitted keys are the declared config fields plus a block "
                f"named after a registered dialect (registered: {listing})."
            )
        return self

    @model_validator(mode="after")
    def _check_driver_config(self) -> Config:
        if self._source_block() is None:
            raise ValueError(
                f"{self.dialect} config is required when dialect is '{self.dialect}'"
            )
        return self

    def _source_block(self) -> dict[str, Any] | None:
        """The raw config block for the selected dialect, or None if absent.

        Checks the typed fields first so `postgres:`/`clickhouse:` keep their
        validation and defaults, then falls through to the opaque extra block.
        """
        if self.dialect in _TYPED_BLOCKS:
            typed = getattr(self, self.dialect, None)
            return None if typed is None else dict(typed.model_dump())
        block = (self.model_extra or {}).get(self.dialect)
        if block is None:
            return None
        if not isinstance(block, dict):
            raise ValueError(
                f"'{self.dialect}:' must be a mapping of connection options, "
                f"got {type(block).__name__}"
            )
        return dict(block)

    @field_validator("tables")
    @classmethod
    def _unique_targets(cls, v: list[TableConfig]) -> list[TableConfig]:
        seen: set[str] = set()
        for t in v:
            if t.target in seen:
                raise ValueError(f"duplicate target name: {t.target}")
            seen.add(t.target)
        return v

    def resolve_table(self, t: TableConfig) -> dict[str, Any]:
        """Apply defaults to a single table and return a flat dict."""
        cadence = t.cadence or self.defaults.cadence
        mode = t.mode or self.defaults.mode
        ascii_sanitize = (
            t.ascii_sanitize if t.ascii_sanitize is not None else self.defaults.ascii_sanitize
        )
        max_rows = t.max_rows if t.max_rows is not None else self.defaults.max_rows
        return {
            "source": t.source,
            "target": t.target,
            "mode": mode,
            "incremental_key": t.incremental_key,
            "incremental_type": t.incremental_type,
            "cadence": cadence,
            "cadence_seconds": parse_cadence(cadence),
            "max_rows": max_rows,
            "ascii_sanitize": ascii_sanitize,
        }

    def driver_config(self) -> dict[str, Any]:
        """Return the config block for the selected dialect.

        A `profile:`, if set, gets the last word: it validates the block and may
        refuse it outright. Applied here rather than in a field validator so
        that the refusal fires on the same path the daemon actually takes to
        build a connection, and so `core/` never imports a concrete profile.
        """
        block = self._source_block()
        if block is None:
            raise ValueError(f"{self.dialect} config is required")
        return self._apply_profile(block)

    def _apply_profile(self, block: dict[str, Any]) -> dict[str, Any]:
        if self.profile is None:
            return block
        from r64_db_engine.profiles import resolve as resolve_profile

        profile = resolve_profile(self.profile)
        if profile.dialect() != self.dialect:
            raise ValueError(
                f"profile '{self.profile}' applies to dialect "
                f"'{profile.dialect()}', not '{self.dialect}'"
            )
        return profile.apply(block)


def parse_cadence(s: str) -> int:
    """Parse 'Ns', 'Nm', 'Nh' duration (SPEC §4.3)."""
    m = _DURATION_PATTERN.match(s.strip().lower())
    if not m:
        raise ValueError(f"invalid cadence syntax: {s!r}; use Ns / Nm / Nh")
    n = int(m.group(1))
    unit = m.group(2)
    seconds = n * {"s": 1, "m": 60, "h": 3600}[unit]
    if seconds < _MIN_CADENCE_SECONDS:
        raise ValueError(f"cadence {s!r} below minimum of {_MIN_CADENCE_SECONDS}s")
    return seconds


def substitute_env(text: str, env: dict[str, str] | None = None) -> str:
    """Replace ${VAR} references with env values; raise on missing."""
    e = env if env is not None else os.environ
    missing: list[str] = []

    def repl(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in e:
            missing.append(name)
            return ""
        return e[name]

    result = _ENV_PATTERN.sub(repl, text)
    if missing:
        raise ValueError(
            f"missing required environment variable(s): {', '.join(sorted(set(missing)))}"
        )
    return result


def load_config(
    path: str | Path,
    env: dict[str, str] | None = None,
) -> Config:
    raw = Path(path).read_text(encoding="utf-8")
    rendered = substitute_env(raw, env)
    data = yaml.safe_load(rendered)
    return Config.model_validate(data)


__all__ = [
    "Config",
    "PostgresConfig",
    "ClickHouseConfig",
    "Row64Config",
    "SinkConfig",
    "DefaultsConfig",
    "TableConfig",
    "TelemetryConfig",
    "RuntimeConfig",
    "load_config",
    "parse_cadence",
    "substitute_env",
]
