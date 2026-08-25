<!-- GENERATED FILE — DO NOT EDIT.
     Emitted by factory/generate_descriptor_artifacts.py from the driver's
     descriptor(). Edit the descriptor in
     src/r64_db_engine/drivers/<dialect>/descriptor.py and regenerate:
         python -m factory.generate_descriptor_artifacts
     Hand edits here are overwritten and are how per-source prose went
     stale in the first place. -->

# ClickHouse

**Dialect key:** `clickhouse` — the identity this connector is selected by, in config and in the registry.

**Conformance:** drifted — repair brief open.

> This dialect has passed conformance before, but a repair brief is open against it, so the green shown is stale. The last green is reported rather than hidden — the useful question during a drift is what changed since it.

Last green run `2026-08-17T15:54:00Z` against `perf_1m`, tally `{"FAIL": 0, "PASS": 9, "SKIPPED": 1}`, ratifying commit `83391564824f`.

Open repair brief: `REPAIR-BRIEF-clickhouse-20260823.md`.

## What it is

Column-store source, discovered through system.tables and system.columns and pulled over clickhouse-connect. This is the driver the benchmark lane runs against: meshbench.perf_1m is the million-row table the checksum and zero-copy serve gates are proven on, which makes ClickHouse the connector whose type verdicts are the most heavily exercised. Full-refresh only — there is no watermark mode here, and a config asking for one is refused at validation rather than quietly downgraded to a full pull.

## Connecting

**Auth mode:** `password`

**Config profile:** `clickhouse`

**Install extra:** none — dependencies are in the base set.

**Required environment variables.** Names only — this page is generated and committed, so no value from your environment appears here or in any other generated artifact.

- `CLICKHOUSE_HOST`
- `CLICKHOUSE_PORT`
- `CLICKHOUSE_DATABASE`
- `CLICKHOUSE_USER`
- `CLICKHOUSE_PASSWORD`

## Capabilities

| capability | supported | what it means |
|---|---|---|
| `supports_arrow` | no | hands back Arrow natively, without a pandas round-trip |
| `supports_streaming` | no | produces the table in chunks, re-blocked to the 65536-row Arrow IPC layout |
| `supports_incremental` | no | watermark mode; without it a config requesting one is refused, never silently downgraded |
| `supports_catalog` | no | a catalog layer above schema |
| `stable_scan_order` | no | row order repeats across pulls without an ORDER BY — an observation, not a guarantee |
| `tz_sensitive` | no | session timezone can shift returned timestamps; aggregate parity is blind to a uniform shift, which is why min/max boundaries are asserted |

## Type representability

What happens to a source type on the way into the ramdb. `refused` is a feature: the writer fails loudly rather than landing a value that is quietly wrong.

| source type | lands as | verdict | note |
|---|---|---|---|
| `Int64` | `int64` | **native** | — |
| `Int32` | `int64` | **native** | — |
| `Float64` | `float64` | **native** | — |
| `String` | `string` | **native** | — |
| `DateTime` | `datetime64[ns]` | **native** | — |
| `Decimal` | `float64` | **coerced** | Lands as float64, losing exactness beyond a double's significant digits. The same trade the Postgres numeric mapping documents. |
| `Nullable(T)` | `T` | **coerced** | The Nullable wrapper is unwrapped and nullability is carried as a mask rather than as part of the type. RF-002 exists because of the discriminator this leaves behind: null and NaN are distinguishable at the source and must stay distinguishable in the artifact, so the battery asserts the null count explicitly instead of inferring it. |
| `UInt64` | `int64` | **refused** | A UInt64 above the signed int64 maximum has no lossless landing place, and the int32 codec ceiling (RF-001) applies below that anyway. Refused at the writer rather than wrapped to a negative number. |
| `Int64 (above signed int32)` | `int64` | **refused** | RF-001, and this is the driver where it bites hardest: 90.74% of meshbench rows exceed the signed int32 range, so the row64tools 1.0.x codec's silent narrowing would have been the normal path rather than an edge case. The writer refuses the write. |
| `Array(T)` | `string` | **string** | Rendered as text. Element access and length are gone downstream. |
| `Map(K,V)` | `string` | **string** | Serialized to text; no Arrow map type is produced, so key lookups downstream are string operations. |

## Failure modes

Operator messages are value-free by construction: they name the configured side only and never echo bytes from the source's own error text.

| reason code | what to do |
|---|---|
| `auth_failed` | ClickHouse rejected the credentials. Permanent, so the daemon fails fast instead of retrying. Check the user and password in the configured env-file. One local trap presents identically: when this instance runs in Docker and the bridge-allow config is missing from users.d, a correct password is still reported as incorrect — see the committed bridge-allow XML and the recovery procedure in the bench notes before assuming the credential is wrong. |
| `source_disconnected` | The ClickHouse endpoint was unreachable or dropped the connection. Transient, so it is retried and the table is marked disconnected. Note that an unreachable source does not always fail fast — a client retrying against a dead endpoint can hang to the run cap, which the sweep reports as a RED run rather than a skip. |
| `table_missing` | The configured source table is not present in this database. Permanent. Verify the database and table names in the config. |

## Notes

- stable_scan_order is False, and deliberately so. A MergeTree scan order depends on part layout, which background merges rewrite without warning, so repeatability across two pulls is not something to lean on. The lane checksum is order-sensitive, so a target here should pin an explicit ORDER BY rather than trust the scan.
- supports_incremental is False: no watermark mode is implemented. PG-011 requires that a config requesting one be refused loudly, which the battery asserts.
- clickhouse-connect is a base dependency, not an extra, for the reason core/config.py::_registered_dialects documents.
