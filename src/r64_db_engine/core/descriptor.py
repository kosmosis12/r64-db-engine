"""Connector descriptor — one declarative block per driver, read by everything.

A driver's identity used to be written down in four places that drifted against
each other: the registry in `drivers/__init__.py`, a hardcoded chip list in the
cockpit, the per-source conformance badges, and hand-written prose in SKILL.md.
Four hand-maintained copies of one fact is three opportunities for a lie.

This module defines the single source. Every driver declares a `DriverMetadata`
via `Driver.descriptor()`; the registry, the cockpit roster projection and the
generated connector docs are all *derived* from that declaration. Nobody
hand-writes a connector doc page again.

The shape is lifted from Apache Superset's `db_engine_specs` `metadata` block,
which solved the same consolidation: one spec attribute feeds the registry, the
UI, the capability matrix and all of its doc pages.
(https://superset.apache.org/user-docs/databases/ ,
 https://deepwiki.com/apache/superset/4.2-database-engine-abstraction)

Four of Superset's mechanisms are deliberately NOT adopted. They are recorded
here rather than in a commit message because the next person to read Superset
will want to add them back:

  * **`supports_url()` URI routing.** Superset identifies an engine by parsing a
    SQLAlchemy URI. Our sources include an HTTP recipe lane where a URI is
    simply the wrong key — a book of calls is not a connection string.
    The dialect key plus the `sources/*.yaml` config profile is strictly more
    general, and it is what PG-010 already established. Keep the dialect key.
  * **A capability score ("159/201").** That number is a public-matrix marketing
    artifact. Our conformance oracle produces a binary verdict against real
    query results checksummed against the source — epistemically stronger than
    counting features. The descriptor declares *capabilities*; the verdict stays
    checksum-backed and lives somewhere else. Diluting a proven verdict into a
    feature count is the proxy-pattern trap.
  * **`_time_grain_expressions`.** Live-query pushdown grouping. Our grain lives
    downstream in DataFusion/meshbox after ingestion, not in the connector.
  * **OAuth2 redirect machinery and an SSH tunnel manager.** The key-pair /
    env-file posture covers the warehouses, and the tailnet is the tunnel.
    `AuthMode` *declares* a mode; no flow is built here for any of them. A
    source that genuinely demands an interactive flow is a future brief.

Two invariants this module exists to hold:

**Law 1 — model at authoring time, never at runtime.** A descriptor is static
data. Resolving one must not connect to anything, read an environment value, or
consult the network. It is answerable from the source tree alone.

**The lazy-registry requirement (D-2/a).** Resolving a descriptor must not import
the driver's heavy third-party dependency. `core.config` validates a config by
asking the registry which dialects exist; before this, that pulled psycopg and
clickhouse_connect into the process just to check a string. Descriptors are
declared on light module scope with the connector import deferred to
`connect()`, so a full descriptor sweep imports no database client at all.

**Firewall.** `core/` names ZERO dialects. This module defines only the *shape*;
every concrete value lives driver-side, reached through the registry
indirection. The firewall grep over this package must still print HOLDS after
this module exists — see `tests/core/test_descriptor.py` for the check, which
deliberately greps rather than analyses imports and so is worded around here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

__all__ = [
    "AuthMode",
    "Capabilities",
    "DescriptorError",
    "DriverMetadata",
    "ErrorMap",
    "Representability",
    "TypeMap",
]


class DescriptorError(ValueError):
    """A descriptor declared something the contract forbids.

    A refusal, never a downgrade — the same posture as `ProfileError`. A
    descriptor that is wrong is wrong at authoring time, and the whole point of
    moving four hand-maintained mechanisms into one declaration is that the one
    declaration gets checked.
    """


class AuthMode(Enum):
    """How a source is authenticated. Declaration only — no flow is built here.

    The value of naming the mode is that the generated doc page and the cockpit
    chip can both say what an operator has to arrange before a pull will work,
    without either of them hardcoding it per source.
    """

    NONE = "none"
    """Zero-credential. An open endpoint; nothing to arrange."""

    PASSWORD = "password"
    """User plus secret, supplied through an env-file. The common case."""

    KEYPAIR = "keypair"
    """A private key file (e.g. a `.p8`) plus a passphrase reference."""

    IAM = "iam"
    """Ambient cloud identity — an instance role or a local credential chain."""


class Representability(Enum):
    """What happens to a source type on the way to the ramdb.

    This is Superset's `GenericDataType` inverted. Superset maps a source type
    into a display category so a chart can decide how to render it. We care
    about the opposite direction and a harder question: does the value survive
    the trip, and if not, does it degrade loudly or quietly?
    """

    NATIVE = "native"
    """Source type to arrow type to ramdb, lossless. Nothing to know."""

    COERCED = "coerced"
    """Lands, but transformed. The value is usable; its type is not the one the
    source had. Precision may be gone. The `note` must say what was traded."""

    STRING_FALLBACK = "string"
    """No arrow equivalent, so the value lands as its text rendering. Readable,
    not computable. A downstream aggregate over this column is a mistake."""

    REFUSED = "refused"
    """The writer refuses, loudly, at write time rather than truncating. This is
    the honest end of the range: a refusal an operator sees beats a silent
    narrowing they discover in a dashboard six weeks later."""


@dataclass(frozen=True)
class TypeMap:
    """One source type and the verdict on carrying it into the ramdb."""

    source_type: str
    arrow_type: str
    verdict: Representability
    note: str = ""

    def __post_init__(self) -> None:
        if not self.source_type.strip():
            raise DescriptorError("TypeMap.source_type must be a non-empty type name")
        if not self.arrow_type.strip():
            raise DescriptorError(
                f"TypeMap for '{self.source_type}' must name the arrow type it lands as"
            )
        if not isinstance(self.verdict, Representability):
            raise DescriptorError(
                f"TypeMap for '{self.source_type}' must carry a Representability verdict, "
                f"got {type(self.verdict).__name__}"
            )
        # A non-native verdict without a note is the drift this module exists to
        # end: someone knew why the type degrades, and the knowledge stayed in
        # their head instead of in the doc the generator emits.
        if self.verdict is not Representability.NATIVE and not self.note.strip():
            raise DescriptorError(
                f"TypeMap for '{self.source_type}' is {self.verdict.value} and must carry a "
                f"note saying what was traded — an undocumented degradation is the drift "
                f"this descriptor exists to retire"
            )


#: Anything that could interpolate a runtime value into an operator message.
#: `{...}`/`{}` is str.format and f-string residue, `%s`/`%(name)s` is printf,
#: `$1`/`${x}` is shell-style, and `\1` is a regex backreference to the matched
#: text — the specific way an error map leaks the provider's bytes.
_INTERPOLATION = re.compile(r"\{[^}]*\}|%[-#0-9.+ ]*[sdrfgexo%]|%\([^)]*\)|\$\{?\w+|\\\d")


@dataclass(frozen=True)
class ErrorMap:
    """A raw driver exception mapped to a reason code and a value-free message.

    Superset's `custom_errors` composed with our own value-free-error doctrine.
    Superset's messages interpolate the matched groups back into the text, which
    is exactly what we will not do: a driver exception carries provider-chosen
    bytes — hostnames, row contents, occasionally the credential itself — and an
    operator-facing string that echoes them is an exfiltration path wearing a
    helpful face.

    So the split is: `pattern` matches the raw exception (provider bytes stay on
    that side of the line and are never rendered), `reason_code` names the
    failure in our existing vocabulary, and `operator_message` is authored text
    naming only the pinned side — what the operator configured, what to check.
    Zero provider-controlled bytes reach the message.
    """

    pattern: str
    reason_code: str
    operator_message: str

    def __post_init__(self) -> None:
        if not self.pattern.strip():
            raise DescriptorError("ErrorMap.pattern must be a non-empty regex")
        try:
            re.compile(self.pattern)
        except re.error as exc:
            raise DescriptorError(
                f"ErrorMap.pattern for '{self.reason_code}' is not a valid regex: {exc}"
            ) from exc
        if not self.reason_code.strip():
            raise DescriptorError(f"ErrorMap for pattern '{self.pattern}' must name a reason code")
        if not self.operator_message.strip():
            raise DescriptorError(f"ErrorMap '{self.reason_code}' must carry an operator message")
        leak = _INTERPOLATION.search(self.operator_message)
        if leak is not None:
            raise DescriptorError(
                f"ErrorMap '{self.reason_code}' operator_message contains an interpolation "
                f"placeholder ({leak.group(0)!r}). Operator messages are value-free: they name "
                f"the pinned side only, never a provider-controlled value from the raw exception"
            )

    def matches(self, raw_exception_text: str) -> bool:
        """True when this map claims the given raw driver exception.

        Note what this does NOT do: it never returns the match. The matched text
        is provider-controlled and has no route to an operator-facing surface.
        """
        return re.search(self.pattern, raw_exception_text, re.IGNORECASE) is not None


@dataclass(frozen=True)
class Capabilities:
    """What shape of ingestion this driver supports.

    Superset carries roughly forty capability attributes, most of them about
    live query pushdown — LIMIT syntax, subquery support, time grains. Those are
    not our frame; we pull and land, we do not federate. So this is the Gate-A
    capability pair widened only along axes the *ingestion* path actually
    branches on. Every flag here gates a real branch, and Gate MF-DESC check 5
    requires a fixture exercising that branch: a capability nobody tests is a
    claim, not a capability.
    """

    supports_arrow: bool = False
    """The driver can hand back Arrow natively, without a pandas round-trip."""

    supports_streaming: bool = False
    """The driver can produce a table in chunks, which the sink re-blocks to the
    65536-row Arrow IPC layout the consumer's column cache is keyed on."""

    supports_incremental: bool = False
    """Watermark mode: the driver can pull only rows past a recorded high-water
    mark. A source without it is full-refresh only, and PG-011 requires that be
    refused loudly rather than silently degraded to a full pull."""

    supports_catalog: bool = False
    """The source has a catalog layer above schema (database.schema.table)."""

    stable_scan_order: bool = False
    """Row order is repeatable across identical pulls without an ORDER BY.

    Declared as an *observation*, never as a guarantee the source makes. It
    matters because the lane checksum is order-sensitive: a source that
    reshuffles freely needs an explicit ordering before its checksum means
    anything. Where this is True the note on the descriptor should say it was
    observed, not promised."""

    tz_sensitive: bool = False
    """Session timezone can shift the timestamps the source returns.

    The B-2 transfer doctrine: aggregate parity is blind to a uniform shift, so
    a whole-table sum can match perfectly while every timestamp is off by the
    same number of hours. That is why the battery asserts min/max boundaries
    rather than aggregates alone. A driver declaring this must pin the session
    timezone explicitly instead of inheriting the server's."""


#: An env-var NAME. Uppercase, digits, underscores — never a value. The check is
#: shape-based on purpose: it is not looking for known secrets (a denylist finds
#: only what it already knows), it requires the positive form of a name.
_ENV_KEY_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")

#: Characters that appear in assignments and values but never in a bare name.
_VALUE_SHAPED = ("=", " ", "\t", "\n", ":", "/", "@")


@dataclass(frozen=True)
class DriverMetadata:
    """The single declarative source for one connector's identity.

    Read by: the registry (`dialect`), the cockpit roster projection, the
    generated connector doc page, and the FORGE-VIEW cards. Written by: the
    driver, once.

    Critically NOT read from here: whether the driver is conformance-green. A
    descriptor is a *declaration of shape*; the oracle's verdict is a separate,
    checksum-backed fact joined in downstream. Letting the mere existence of a
    descriptor render as green would be a proxy for the thing we actually
    measure, and a green that quietly rescopes what it measures is worse than
    no green at all.
    """

    dialect: str
    """The registry key. The ONE identity — not a URI (see the module docstring
    on why `supports_url()` was rejected)."""

    engine_name: str
    """Human label for a chip or a doc heading."""

    auth_mode: AuthMode

    required_env_keys: tuple[str, ...]
    """Env-var NAMES the operator must set. Never values, ever — Law 3. This
    tuple reaches generated artifacts that are committed and served, so it is
    validated to be name-shaped rather than trusted to be."""

    config_profile: str
    """Which `sources/*.yaml` profile shape this driver expects."""

    doc_summary: str
    """One paragraph of prose. This is the body of the generated doc page, and
    it is the thing that replaces hand-written per-source SKILL prose."""

    capabilities: Capabilities

    type_mappings: tuple[TypeMap, ...] = ()
    custom_errors: tuple[ErrorMap, ...] = ()

    extras_package: str | None = None
    """The pip extra that must be installed for this driver, or None when its
    dependencies are in the base set. Note that today every registered driver's
    deps ARE base deps, for the reason `core/config.py::_registered_dialects`
    documents: validating any config touches the registry."""

    notes: tuple[str, ...] = field(default_factory=tuple)
    """Free-form declarations that did not fit a capability flag. Rendered into
    the generated doc. Where a capability is an observation rather than a
    guarantee, say so here."""

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Raise `DescriptorError` unless this descriptor satisfies the contract.

        Called from `__post_init__`, so an invalid descriptor cannot be
        constructed at all — the failure lands at import time, in the driver
        that authored it, not in the generator three steps downstream.
        """
        if not self.dialect.strip():
            raise DescriptorError("DriverMetadata.dialect must be a non-empty registry key")
        if self.dialect != self.dialect.strip().lower():
            raise DescriptorError(
                f"dialect '{self.dialect}' must be lowercase and unpadded — it is the registry "
                f"key, and a key that differs from its rendering is a lookup miss waiting to happen"
            )
        if not self.engine_name.strip():
            raise DescriptorError(f"descriptor '{self.dialect}' must carry a human engine_name")
        if not isinstance(self.auth_mode, AuthMode):
            raise DescriptorError(
                f"descriptor '{self.dialect}' auth_mode must be an AuthMode, "
                f"got {type(self.auth_mode).__name__}"
            )
        if not isinstance(self.capabilities, Capabilities):
            raise DescriptorError(
                f"descriptor '{self.dialect}' capabilities must be a Capabilities, "
                f"got {type(self.capabilities).__name__}"
            )
        if not self.config_profile.strip():
            raise DescriptorError(f"descriptor '{self.dialect}' must name a config_profile")
        if not self.doc_summary.strip():
            raise DescriptorError(
                f"descriptor '{self.dialect}' must carry a doc_summary — it is the body of the "
                f"generated doc page, and an empty one reintroduces the prose drift this replaces"
            )

        self._validate_env_key_names()

        if self.extras_package is not None and not self.extras_package.strip():
            raise DescriptorError(
                f"descriptor '{self.dialect}' extras_package must be a package extra name or None, "
                f"never an empty string"
            )

        seen: set[str] = set()
        for tm in self.type_mappings:
            if tm.source_type in seen:
                raise DescriptorError(
                    f"descriptor '{self.dialect}' declares source type '{tm.source_type}' twice — "
                    f"two verdicts for one type is the ambiguity the single source removes"
                )
            seen.add(tm.source_type)

        codes: set[str] = set()
        for em in self.custom_errors:
            if em.reason_code in codes:
                raise DescriptorError(
                    f"descriptor '{self.dialect}' declares reason code '{em.reason_code}' twice"
                )
            codes.add(em.reason_code)

    def _validate_env_key_names(self) -> None:
        """Law 3 at the descriptor boundary: names only, never values.

        `required_env_keys` is copied verbatim into artifacts that get committed
        to git and served to a browser. The credential-never-in-artifact
        guarantee therefore has to be enforced where the value is authored, not
        only where it is rendered — by the time a value reaches the roster JSON
        the leak has already happened.
        """
        if not isinstance(self.required_env_keys, tuple):
            raise DescriptorError(
                f"descriptor '{self.dialect}' required_env_keys must be a tuple, "
                f"got {type(self.required_env_keys).__name__}"
            )
        for key in self.required_env_keys:
            if not isinstance(key, str):
                raise DescriptorError(
                    f"descriptor '{self.dialect}' required_env_keys must contain strings, "
                    f"got {type(key).__name__}"
                )
            if any(ch in key for ch in _VALUE_SHAPED):
                raise DescriptorError(
                    f"descriptor '{self.dialect}' required_env_keys entry {key!r} is value-shaped. "
                    f"These are env-var NAMES only — a value here would be copied verbatim into "
                    f"committed artifacts (Law 3)"
                )
            if not _ENV_KEY_NAME.match(key):
                raise DescriptorError(
                    f"descriptor '{self.dialect}' required_env_keys entry {key!r} is not a "
                    f"well-formed env-var name (expected ^[A-Z][A-Z0-9_]*$)"
                )

    def as_dict(self) -> dict[str, object]:
        """Plain-data projection. The one place descriptors become artifacts.

        Key order is fixed by this function and every collection is emitted in
        the order the driver declared it, so the generator's output is a pure
        function of the source tree (Law 1). Nothing here reads a dict whose
        iteration order is incidental.
        """
        return {
            "dialect": self.dialect,
            "engine_name": self.engine_name,
            "auth_mode": self.auth_mode.value,
            "required_env_keys": list(self.required_env_keys),
            "config_profile": self.config_profile,
            "doc_summary": self.doc_summary,
            "extras_package": self.extras_package,
            "capabilities": {
                "supports_arrow": self.capabilities.supports_arrow,
                "supports_streaming": self.capabilities.supports_streaming,
                "supports_incremental": self.capabilities.supports_incremental,
                "supports_catalog": self.capabilities.supports_catalog,
                "stable_scan_order": self.capabilities.stable_scan_order,
                "tz_sensitive": self.capabilities.tz_sensitive,
            },
            "type_mappings": [
                {
                    "source_type": tm.source_type,
                    "arrow_type": tm.arrow_type,
                    "verdict": tm.verdict.value,
                    "note": tm.note,
                }
                for tm in self.type_mappings
            ],
            "custom_errors": [
                {
                    "reason_code": em.reason_code,
                    "pattern": em.pattern,
                    "operator_message": em.operator_message,
                }
                for em in self.custom_errors
            ],
            "notes": list(self.notes),
        }
