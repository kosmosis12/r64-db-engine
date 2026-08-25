<!-- GENERATED FILE — DO NOT EDIT.
     Emitted by factory/generate_descriptor_artifacts.py from the driver's
     descriptor(). Edit the descriptor in
     src/r64_db_engine/drivers/<dialect>/descriptor.py and regenerate:
         python -m factory.generate_descriptor_artifacts
     Hand edits here are overwritten and are how per-source prose went
     stale in the first place. -->

# PostgreSQL

**Dialect key:** `postgres` — the identity this connector is selected by, in config and in the registry.

**Conformance:** declared, pending conformance.

> No conformance evidence pack has been committed for this dialect. The driver declares its shape; nothing has yet checked that shape against a real source.

## What it is

The reference driver, and the one every other connector is measured against. Pulls a table or an inline SQL source over psycopg 3, coerces the frame to ramdb-safe dtypes, and writes it atomically. Supports watermarked incremental pulls: a bounded incremental pull is ordered by its cursor and, where a tie breaker is configured, by a second unique column, so rows sharing a timestamp at the watermark boundary are neither replayed nor dropped. Connection shape beyond the plain case is handled by a connection profile rather than by this driver — see the Supabase profile, which refuses transaction-mode pooling outright because server-side prepared statements do not survive it.

## Connecting

**Auth mode:** `password`

**Config profile:** `postgres`

**Install extra:** none — dependencies are in the base set.

**Required environment variables.** Names only — this page is generated and committed, so no value from your environment appears here or in any other generated artifact.

- `PGHOST`
- `PGPORT`
- `PGDATABASE`
- `PGUSER`
- `PGPASSWORD`

## Capabilities

| capability | supported | what it means |
|---|---|---|
| `supports_arrow` | no | hands back Arrow natively, without a pandas round-trip |
| `supports_streaming` | no | produces the table in chunks, re-blocked to the 65536-row Arrow IPC layout |
| `supports_incremental` | yes | watermark mode; without it a config requesting one is refused, never silently downgraded |
| `supports_catalog` | no | a catalog layer above schema |
| `stable_scan_order` | yes | row order repeats across pulls without an ORDER BY — an observation, not a guarantee |
| `tz_sensitive` | no | session timezone can shift returned timestamps; aggregate parity is blind to a uniform shift, which is why min/max boundaries are asserted |

## Type representability

What happens to a source type on the way into the ramdb. `refused` is a feature: the writer fails loudly rather than landing a value that is quietly wrong.

| source type | lands as | verdict | note |
|---|---|---|---|
| `bigint` | `int64` | **native** | — |
| `integer` | `int64` | **native** | — |
| `boolean` | `bool` | **native** | — |
| `text` | `string` | **native** | — |
| `double precision` | `float64` | **native** | — |
| `date` | `datetime64[ns]` | **native** | — |
| `numeric` | `float64` | **coerced** | Decimal lands as float64. Exact for the everyday scales; a numeric(38,15) carrying more significant digits than a double holds does not survive the Decimal -> float64 -> Decimal round trip. Money columns wide enough to matter should be pulled as text and reconstructed downstream. |
| `timestamptz` | `datetime64[ns]` | **coerced** | Normalized to UTC and the offset dropped, because the ramdb has no tz-aware type. The instant is preserved; the original offset is not. |
| `jsonb` | `string` | **string** | Serialized to its JSON text. Readable, not queryable — Arrow has no variant type here, so any downstream filter on a key inside the document is a string operation, not a structured one. |
| `integer[]` | `string` | **string** | Arrays land as their text rendering. The elements are legible but the column is no longer a list; length and element access are gone. |
| `bytea` | `string` | **string** | Rendered as text rather than carried as binary. Non-UTF8 payloads should be encoded at the source before the pull rather than relied on here. |
| `inet` | `string` | **string** | No Arrow equivalent; lands as the printed address. |
| `time` | `string` | **string** | A time-of-day with no date has no Arrow timestamp to land in; carried as text. |
| `interval` | `int64` | **refused** | Carried as microseconds, and therefore subject to the int32 ceiling below: an interval whose microsecond count exceeds signed int32 is refused at the coercer rather than truncated. |
| `bigint (above signed int32)` | `int64` | **refused** | RF-001. The row64tools 1.0.x codec narrows int64 to signed int32 on store, silently, so a bigint above 2147483647 came back as a different number. The writer now refuses the write instead. This is not a rare edge: 90.74% of meshbench rows exceed the int32 range, so the silent-truncation path would have been the normal path. A refusal an operator sees beats a wrong number they find in a dashboard six weeks later. |

## Failure modes

Operator messages are value-free by construction: they name the configured side only and never echo bytes from the source's own error text.

| reason code | what to do |
|---|---|
| `auth_failed` | Postgres rejected the credentials. This is permanent, not transient, so the daemon fails fast at startup rather than retrying against a source that will keep saying no. Check the user and password in the configured env-file, and that the role exists and may log in. |
| `source_disconnected` | The connection to Postgres dropped mid-pull. Treated as transient and retried, and the table is marked disconnected rather than failed. If it persists, check network reachability and any pooler or proxy in the path. |
| `table_missing` | The configured source table or view is not visible to this role. Permanent: retrying will not make it appear. Verify the schema-qualified name in the config and the role's grants. |

## Notes

- stable_scan_order is an OBSERVATION, not a guarantee Postgres makes (D-5). A sequential scan of an unmodified heap has been repeatable across pulls here, which is why the lane checksum is meaningful; it is not promised by the engine and would not survive a concurrent VACUUM or a plan flipping to a parallel scan. Incremental pulls do not rely on it — they impose an explicit ORDER BY.
- tz_sensitive is False because timestamptz values are normalized to UTC during coercion rather than rendered through a session timezone.
- psycopg is in the base dependency set, not behind an extra, because validating any config consults the driver registry (see core/config.py::_registered_dialects).
