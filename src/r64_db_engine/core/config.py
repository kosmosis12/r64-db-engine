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
    startup, NOT a `Literal[...]`. That is deliberate and load-bearing: PG-010
    records that core already bakes the first *dialect* into core validation,
    and enumerating sink names here would clone the same leak onto a second
    axis. Adding a sink must require zero edits to this file.

    Sink-specific options are accepted as extra keys and passed through
    opaquely, exactly as `Driver.connect()` receives an opaque config dict.
    """

    model_config = ConfigDict(extra="allow")

    type: str

    def options(self) -> dict[str, Any]:
        """Sink-specific keys only, with the selector removed."""
        return {k: v for k, v in self.model_dump().items() if k != "type"}


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dialect: Literal["postgres", "clickhouse"] = "postgres"
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
    def _check_driver_config(self) -> Config:
        if self.dialect == "postgres" and self.postgres is None:
            raise ValueError("postgres config is required when dialect is 'postgres'")
        if self.dialect == "clickhouse" and self.clickhouse is None:
            raise ValueError("clickhouse config is required when dialect is 'clickhouse'")
        return self

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
        if self.dialect == "postgres":
            if self.postgres is None:
                raise ValueError("postgres config is required")
            return self._apply_profile(self.postgres.model_dump())
        if self.dialect == "clickhouse":
            if self.clickhouse is None:
                raise ValueError("clickhouse config is required")
            return self._apply_profile(self.clickhouse.model_dump())
        raise ValueError(f"unknown dialect: {self.dialect}")

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
