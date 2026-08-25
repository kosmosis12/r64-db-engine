"""The `postgres` connector descriptor. See `core.descriptor` for the shape.

Kept in its own module, apart from `driver.py`, for one reason worth stating:
this file must stay importable without psycopg. `driver.py` already defers its
client import, but a descriptor that lived beside a hundred lines of connection
handling is one careless edit away from dragging the client back in. A separate
module makes the lazy-enumeration property structural instead of vigilant.

Everything here is authored data. No value is read from the environment, no
connection is opened, nothing is computed at import time (Law 1).
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

#: The reference-grade driver. Every capability below is exercised by the
#: conformance battery against a live Postgres, which is why this descriptor is
#: the one to copy when authoring the next.
POSTGRES = DriverMetadata(
    dialect="postgres",
    engine_name="PostgreSQL",
    auth_mode=AuthMode.PASSWORD,
    required_env_keys=("PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD"),
    config_profile="postgres",
    doc_summary=(
        "The reference driver, and the one every other connector is measured against. "
        "Pulls a table or an inline SQL source over psycopg 3, coerces the frame to "
        "ramdb-safe dtypes, and writes it atomically. Supports watermarked incremental "
        "pulls: a bounded incremental pull is ordered by its cursor and, where a tie "
        "breaker is configured, by a second unique column, so rows sharing a timestamp "
        "at the watermark boundary are neither replayed nor dropped. Connection shape "
        "beyond the plain case is handled by a connection profile rather than by this "
        "driver — see the Supabase profile, which refuses transaction-mode pooling "
        "outright because server-side prepared statements do not survive it."
    ),
    capabilities=Capabilities(
        supports_arrow=False,
        supports_streaming=False,
        supports_incremental=True,
        supports_catalog=False,
        stable_scan_order=True,
        tz_sensitive=False,
    ),
    type_mappings=(
        TypeMap("bigint", "int64", Representability.NATIVE),
        TypeMap("integer", "int64", Representability.NATIVE),
        TypeMap("boolean", "bool", Representability.NATIVE),
        TypeMap("text", "string", Representability.NATIVE),
        TypeMap("double precision", "float64", Representability.NATIVE),
        TypeMap("date", "datetime64[ns]", Representability.NATIVE),
        TypeMap(
            "numeric",
            "float64",
            Representability.COERCED,
            "Decimal lands as float64. Exact for the everyday scales; a numeric(38,15) "
            "carrying more significant digits than a double holds does not survive the "
            "Decimal -> float64 -> Decimal round trip. Money columns wide enough to "
            "matter should be pulled as text and reconstructed downstream.",
        ),
        TypeMap(
            "timestamptz",
            "datetime64[ns]",
            Representability.COERCED,
            "Normalized to UTC and the offset dropped, because the ramdb has no "
            "tz-aware type. The instant is preserved; the original offset is not.",
        ),
        TypeMap(
            "jsonb",
            "string",
            Representability.STRING_FALLBACK,
            "Serialized to its JSON text. Readable, not queryable — Arrow has no "
            "variant type here, so any downstream filter on a key inside the document "
            "is a string operation, not a structured one.",
        ),
        TypeMap(
            "integer[]",
            "string",
            Representability.STRING_FALLBACK,
            "Arrays land as their text rendering. The elements are legible but the "
            "column is no longer a list; length and element access are gone.",
        ),
        TypeMap(
            "bytea",
            "string",
            Representability.STRING_FALLBACK,
            "Rendered as text rather than carried as binary. Non-UTF8 payloads should "
            "be encoded at the source before the pull rather than relied on here.",
        ),
        TypeMap(
            "inet",
            "string",
            Representability.STRING_FALLBACK,
            "No Arrow equivalent; lands as the printed address.",
        ),
        TypeMap(
            "time",
            "string",
            Representability.STRING_FALLBACK,
            "A time-of-day with no date has no Arrow timestamp to land in; carried as text.",
        ),
        TypeMap(
            "interval",
            "int64",
            Representability.REFUSED,
            "Carried as microseconds, and therefore subject to the int32 ceiling below: "
            "an interval whose microsecond count exceeds signed int32 is refused at the "
            "coercer rather than truncated.",
        ),
        TypeMap(
            "bigint (above signed int32)",
            "int64",
            Representability.REFUSED,
            "RF-001. The row64tools 1.0.x codec narrows int64 to signed int32 on store, "
            "silently, so a bigint above 2147483647 came back as a different number. "
            "The writer now refuses the write instead. This is not a rare edge: 90.74% "
            "of meshbench rows exceed the int32 range, so the silent-truncation path "
            "would have been the normal path. A refusal an operator sees beats a wrong "
            "number they find in a dashboard six weeks later.",
        ),
    ),
    custom_errors=(
        ErrorMap(
            pattern=r"\b(28000|28P01)\b|password authentication failed|role .* does not exist",
            reason_code="auth_failed",
            operator_message=(
                "Postgres rejected the credentials. This is permanent, not transient, so the "
                "daemon fails fast at startup rather than retrying against a source that will "
                "keep saying no. Check the user and password in the configured env-file, and "
                "that the role exists and may log in."
            ),
        ),
        ErrorMap(
            pattern=r"\b08[0-9A-Z]{3}\b|server closed the connection|connection is closed",
            reason_code="source_disconnected",
            operator_message=(
                "The connection to Postgres dropped mid-pull. Treated as transient and retried, "
                "and the table is marked disconnected rather than failed. If it persists, check "
                "network reachability and any pooler or proxy in the path."
            ),
        ),
        ErrorMap(
            pattern=r"\b42P01\b|relation .* does not exist",
            reason_code="table_missing",
            operator_message=(
                "The configured source table or view is not visible to this role. Permanent: "
                "retrying will not make it appear. Verify the schema-qualified name in the "
                "config and the role's grants."
            ),
        ),
    ),
    extras_package=None,
    notes=(
        "stable_scan_order is an OBSERVATION, not a guarantee Postgres makes (D-5). A "
        "sequential scan of an unmodified heap has been repeatable across pulls here, "
        "which is why the lane checksum is meaningful; it is not promised by the engine "
        "and would not survive a concurrent VACUUM or a plan flipping to a parallel scan. "
        "Incremental pulls do not rely on it — they impose an explicit ORDER BY.",
        "tz_sensitive is False because timestamptz values are normalized to UTC during "
        "coercion rather than rendered through a session timezone.",
        "psycopg is in the base dependency set, not behind an extra, because validating "
        "any config consults the driver registry (see core/config.py::_registered_dialects).",
    ),
)
