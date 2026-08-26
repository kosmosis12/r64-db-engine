"""The `clickhouse` connector descriptor. See `core.descriptor` for the shape.

Separate from `driver.py` so it stays importable without clickhouse_connect —
the same structural guarantee the Postgres descriptor makes, for the same
lazy-enumeration reason.
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

CLICKHOUSE = DriverMetadata(
    dialect="clickhouse",
    engine_name="ClickHouse",
    auth_mode=AuthMode.PASSWORD,
    required_env_keys=(
        "CLICKHOUSE_HOST",
        "CLICKHOUSE_PORT",
        "CLICKHOUSE_DATABASE",
        "CLICKHOUSE_USER",
        "CLICKHOUSE_PASSWORD",
    ),
    config_profile="clickhouse",
    doc_summary=(
        "Column-store source, discovered through system.tables and system.columns and "
        "pulled over clickhouse-connect. This is the driver the benchmark lane runs "
        "against: meshbench.perf_1m is the million-row table the checksum and "
        "zero-copy serve gates are proven on, which makes ClickHouse the connector "
        "whose type verdicts are the most heavily exercised. Full-refresh only — there "
        "is no watermark mode here, and a config asking for one is refused at "
        "validation rather than quietly downgraded to a full pull."
    ),
    capabilities=Capabilities(
        supports_arrow=False,
        supports_streaming=False,
        supports_incremental=False,
        supports_catalog=False,
        stable_scan_order=False,
        tz_sensitive=False,
    ),
    type_mappings=(
        TypeMap("Int64", "int64", Representability.NATIVE),
        TypeMap("Int32", "int64", Representability.NATIVE),
        TypeMap("Float64", "float64", Representability.NATIVE),
        TypeMap("String", "string", Representability.NATIVE),
        TypeMap("DateTime", "datetime64[ns]", Representability.NATIVE),
        TypeMap(
            "Decimal",
            "float64",
            Representability.COERCED,
            "Lands as float64, losing exactness beyond a double's significant digits. "
            "The same trade the Postgres numeric mapping documents.",
        ),
        TypeMap(
            "Nullable(T)",
            "T",
            Representability.COERCED,
            "The Nullable wrapper is unwrapped and nullability is carried as a mask "
            "rather than as part of the type. RF-002 exists because of the discriminator "
            "this leaves behind: null and NaN are distinguishable at the source and must "
            "stay distinguishable in the artifact, so the battery asserts the null count "
            "explicitly instead of inferring it.",
        ),
        TypeMap(
            "UInt64",
            "int64",
            Representability.REFUSED,
            "A UInt64 above the signed int64 maximum has no lossless landing place, and "
            "the int32 codec ceiling (RF-001) applies below that anyway. Refused at the "
            "writer rather than wrapped to a negative number.",
        ),
        TypeMap(
            "Int64 (above signed int32)",
            "int64",
            Representability.REFUSED,
            "RF-001, and this is the driver where it bites hardest: 90.74% of meshbench "
            "rows exceed the signed int32 range, so the row64tools 1.0.x codec's silent "
            "narrowing would have been the normal path rather than an edge case. The "
            "writer refuses the write.",
        ),
        TypeMap(
            "Array(T)",
            "string",
            Representability.STRING_FALLBACK,
            "Rendered as text. Element access and length are gone downstream.",
        ),
        TypeMap(
            "Map(K,V)",
            "string",
            Representability.STRING_FALLBACK,
            "Serialized to text; no Arrow map type is produced, so key lookups "
            "downstream are string operations.",
        ),
    ),
    custom_errors=(
        ErrorMap(
            pattern=r"AUTHENTICATION_FAILED|Code:\s*516|password is incorrect|"
            r"default:\s*Authentication failed",
            reason_code="auth_failed",
            operator_message=(
                "ClickHouse rejected the credentials. Permanent, so the daemon fails fast "
                "instead of retrying. Check the user and password in the configured env-file. "
                "One local trap presents identically: when this instance runs in Docker and "
                "the bridge-allow config is missing from users.d, a correct password is still "
                "reported as incorrect — see the committed bridge-allow XML and the recovery "
                "procedure in the bench notes before assuming the credential is wrong."
            ),
        ),
        ErrorMap(
            pattern=r"Connection refused|Connection reset|NETWORK_ERROR|SOCKET_TIMEOUT|"
            r"Read timed out",
            reason_code="source_disconnected",
            operator_message=(
                "The ClickHouse endpoint was unreachable or dropped the connection. Transient, "
                "so it is retried and the table is marked disconnected. Note that an "
                "unreachable source does not always fail fast — a client retrying against a "
                "dead endpoint can hang to the run cap, which the sweep reports as a RED run "
                "rather than a skip."
            ),
        ),
        ErrorMap(
            pattern=r"UNKNOWN_TABLE|Code:\s*60\b|Table .* does not exist",
            reason_code="table_missing",
            operator_message=(
                "The configured source table is not present in this database. Permanent. "
                "Verify the database and table names in the config."
            ),
        ),
    ),
    extras_package=None,
    notes=(
        "stable_scan_order is False, and deliberately so. A MergeTree scan order depends "
        "on part layout, which background merges rewrite without warning, so repeatability "
        "across two pulls is not something to lean on. The lane checksum is order-sensitive, "
        "so a target here should pin an explicit ORDER BY rather than trust the scan.",
        "supports_incremental is False: no watermark mode is implemented. PG-011 requires "
        "that a config requesting one be refused loudly, which the battery asserts.",
        "clickhouse-connect is a base dependency, not an extra, for the reason "
        "core/config.py::_registered_dialects documents.",
    ),
)
