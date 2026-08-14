"""Profile registry. Resolves config `profile:` to a ConnectionProfile class.

Deliberately shaped identically to `drivers/__init__.py` and `sinks/__init__.py`:
a name -> class map plus a `resolve()` that raises with the available names
listed. `core/` imports this module lazily, from inside the function that builds
the driver config, and never imports a concrete profile — so adding a profile
requires zero `core/` edits, and the PG-010 leak is not cloned onto a third axis.
"""

from __future__ import annotations

from r64_db_engine.core.profile import ConnectionProfile
from r64_db_engine.profiles.supabase import SupabaseProfile

PROFILES: dict[str, type[ConnectionProfile]] = {
    SupabaseProfile.profile_name(): SupabaseProfile,
}


def resolve(name: str) -> type[ConnectionProfile]:
    try:
        return PROFILES[name]
    except KeyError as exc:
        available = ", ".join(sorted(PROFILES)) or "(none)"
        raise ValueError(f"unknown profile '{name}' (available: {available})") from exc


__all__ = ["PROFILES", "resolve"]
