"""Connection profiles — named deployment shapes over an existing driver.

A profile is NOT a driver. It is a validation-and-normalization pass over a
driver's connection config, naming a deployment whose constraints are real but
invisible to the driver: a managed Postgres whose connection pooler silently
breaks server-side prepared statements, for instance.

The contract is deliberately narrow. A profile receives the driver config dict
and returns a driver config dict. It may:

  * refuse a combination outright, by raising `ProfileError`
  * normalize a value that would otherwise fail mysteriously later

It may NOT open connections, issue queries, or reach into the driver. Anything
that needs those belongs in the driver.

`core/` names ZERO profiles, for the same reason it names zero sinks (see
`core/sink.py` and the PG-010 note in `core/config.py`): profile selection is a
free-form string resolved against `profiles/` at config time, so adding a
profile requires no edit to this file or to `core/config.py`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class ProfileError(ValueError):
    """A profile refused the configuration.

    Deliberately a refusal, never a downgrade. The failure mode this exists to
    prevent is the silent one: a config that connects fine and then fails
    partway through a pull, or worse, succeeds while quietly losing a guarantee
    the operator believed they had.
    """


class ConnectionProfile(ABC):
    """Validates and normalizes a driver connection config."""

    @classmethod
    @abstractmethod
    def profile_name(cls) -> str:
        """The name used to select this profile in config."""

    @classmethod
    @abstractmethod
    def dialect(cls) -> str:
        """The driver dialect this profile applies to."""

    @classmethod
    @abstractmethod
    def apply(cls, config: dict[str, Any]) -> dict[str, Any]:
        """Return a validated, normalized copy of `config`.

        Raises `ProfileError` if the configuration cannot be made safe.
        """
