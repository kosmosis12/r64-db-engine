"""The `rest` connector descriptor. See `core.descriptor` for the shape.

`rest` is the descriptor's most useful stress test, because it is not a
database. Its "connection" is a recipe book, its schema is a response contract,
and it has no URI in the sense Superset means. A descriptor that could only
describe databases would have failed here — which is precisely why the dialect
key, and not a SQLAlchemy URI, is this project's identity for a connector.
"""

from __future__ import annotations

from r64_db_engine.core.descriptor import (
    AuthMode,
    Capabilities,
    DriverMetadata,
    ErrorMap,
    Representability,
    TypeMap,
)

REST = DriverMetadata(
    dialect="rest",
    engine_name="REST (recipe lane)",
    auth_mode=AuthMode.NONE,
    required_env_keys=(),
    config_profile="rest",
    doc_summary=(
        "The long-tail lane: one generic driver that executes a compiled recipe book "
        "instead of speaking a wire protocol. A recipe is a single call with its method "
        "and URL pinned at authoring time, its auth supplied as a path to a 0600 "
        "env-file, and its request and response schemas declared; a book is an ordered "
        "set of recipes plus the threading that feeds one call's output into the next. "
        "The book is compiled once and executed by hand-written code — no model sits in "
        "the pull path, which is what makes the lane auditable. Security invariants are "
        "enforced in code and proven by test rather than documented as intent: HTTPS "
        "only, hostname fixed per recipe with real subdomain matching so a lookalike "
        "domain is not a suffix match, private and loopback and link-local address space "
        "refused, redirects not followed because a followed redirect is an SSRF bypass "
        "around the pinning, and response size and time capped. Every pull validates the "
        "response against the declared schema; a validation failure emits a structured "
        "repair event and exits non-zero, and never retries with a reinterpretation."
    ),
    capabilities=Capabilities(
        supports_arrow=False,
        supports_streaming=False,
        supports_incremental=False,
        supports_catalog=False,
        stable_scan_order=True,
        tz_sensitive=False,
    ),
    type_mappings=(
        TypeMap("JSON number (integral)", "int64", Representability.NATIVE),
        TypeMap("JSON number (fractional)", "float64", Representability.NATIVE),
        TypeMap("JSON string", "string", Representability.NATIVE),
        TypeMap("JSON boolean", "bool", Representability.NATIVE),
        TypeMap(
            "ISO-8601 timestamp string",
            "datetime64[ns]",
            Representability.COERCED,
            "Parsed from text into a timestamp by the extraction step. The source's "
            "timezone is whatever the recipe declared it to request, so the book pins it "
            "explicitly rather than accepting a provider default that could change.",
        ),
        TypeMap(
            "JSON null",
            "null mask",
            Representability.COERCED,
            "A JSON null becomes a real null rather than a sentinel. RF-002: null and "
            "NaN must stay distinguishable in the artifact, so a dataset declares its "
            "expected null count and the battery asserts it rather than inferring it "
            "from whatever landed.",
        ),
        TypeMap(
            "JSON object",
            "string",
            Representability.STRING_FALLBACK,
            "A nested object that no JSONPath extract flattens lands as its serialized "
            "text. The fix is normally a better extract in the recipe, not a downstream "
            "string parse.",
        ),
        TypeMap(
            "JSON array",
            "string",
            Representability.STRING_FALLBACK,
            "Same as objects: an array not unrolled into rows by the recipe lands as text.",
        ),
        TypeMap(
            "JSON number (above signed int32)",
            "int64",
            Representability.REFUSED,
            "RF-001 applies to this lane exactly as it does to the databases: the "
            "row64tools 1.0.x codec narrows int64 to signed int32 on store, so a large "
            "identifier from an API is refused at the writer rather than silently "
            "becoming a different number.",
        ),
    ),
    custom_errors=(
        ErrorMap(
            pattern=r"\b401\b|\b403\b|Unauthorized|Forbidden|invalid[_ ]api[_ ]key",
            reason_code="auth_failed",
            operator_message=(
                "The API rejected the request's credentials. Permanent — retrying with the "
                "same key will keep failing. Check the key in the recipe's env-file, that the "
                "file is still mode 0600, and that the key has not been rotated or scoped away "
                "from this endpoint."
            ),
        ),
        ErrorMap(
            pattern=r"\b429\b|rate limit|too many requests",
            reason_code="rate_limited",
            operator_message=(
                "The API rate-limited the pull. Transient. If it recurs at the configured "
                "cadence, the cadence is wrong for this provider's quota and belongs in the "
                "recipe's pagination and pacing spec rather than in a retry loop."
            ),
        ),
        ErrorMap(
            pattern=r"response failed schema validation|ValidationError|"
            r"does not match declared response_schema",
            reason_code="response_schema_drift",
            operator_message=(
                "The response no longer matches the schema the recipe declared. This is the "
                "drift signal the lane exists to catch, and it is deliberately not "
                "recoverable in place: the pull exits non-zero and emits a repair event "
                "rather than guessing at a new interpretation of the payload. The recipe book "
                "is re-researched and re-admitted through the battery."
            ),
        ),
        ErrorMap(
            pattern=r"destination pinning|host not permitted|scheme must be https|"
            r"refused private address",
            reason_code="destination_pin_violation",
            operator_message=(
                "A request tried to leave the host, scheme, or address space the recipe pinned "
                "at authoring time. Refused before the request was made. This is an invariant, "
                "not a setting: if the provider genuinely moved, the recipe is re-authored and "
                "reviewed, never widened at runtime."
            ),
        ),
    ),
    extras_package=None,
    notes=(
        "auth_mode is NONE because the admitted book — open-meteo — is a zero-credential "
        "public API. The lane itself supports keyed APIs; auth is declared per recipe as a "
        "path to a 0600 env-file, and the KEY NAME reaches config while the value never "
        "leaves the file (Law 3).",
        "stable_scan_order is True as an OBSERVATION about this lane's shape rather than a "
        "promise any provider makes: a recipe book's output order is the order the engine "
        "walks its recipes and rows, which is deterministic for a fixed book and a fixed "
        "response. A provider that reorders its own payload between calls would break it, "
        "which is what the pull-to-pull checksum comparison is there to detect.",
        "supports_incremental is False. Some APIs expose a cursor that would support a "
        "watermark, but nothing in the compiled-book path implements one yet, and declaring "
        "a capability with no fixture exercising it is exactly the untested claim the merge "
        "bar blocks on.",
    ),
)
