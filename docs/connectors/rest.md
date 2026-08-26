<!-- GENERATED FILE — DO NOT EDIT.
     Emitted by factory/generate_descriptor_artifacts.py from the driver's
     descriptor(). Edit the descriptor in
     src/r64_db_engine/drivers/<dialect>/descriptor.py and regenerate:
         python -m factory.generate_descriptor_artifacts
     Hand edits here are overwritten and are how per-source prose went
     stale in the first place. -->

# REST (recipe lane)

**Dialect key:** `rest` — the identity this connector is selected by, in config and in the registry.

**Conformance:** conformance-passing.

> Checksum-backed verdict from the conformance battery against a live source.

Last green run `2026-08-23T11:30:39Z` against `open_meteo_berlin_hourly`, tally `{"FAIL": 0, "PASS": 9, "SKIPPED": 1}`, ratifying commit `e6f6e585131e`.

## What it is

The long-tail lane: one generic driver that executes a compiled recipe book instead of speaking a wire protocol. A recipe is a single call with its method and URL pinned at authoring time, its auth supplied as a path to a 0600 env-file, and its request and response schemas declared; a book is an ordered set of recipes plus the threading that feeds one call's output into the next. The book is compiled once and executed by hand-written code — no model sits in the pull path, which is what makes the lane auditable. Security invariants are enforced in code and proven by test rather than documented as intent: HTTPS only, hostname fixed per recipe with real subdomain matching so a lookalike domain is not a suffix match, private and loopback and link-local address space refused, redirects not followed because a followed redirect is an SSRF bypass around the pinning, and response size and time capped. Every pull validates the response against the declared schema; a validation failure emits a structured repair event and exits non-zero, and never retries with a reinterpretation.

## Connecting

**Auth mode:** `none`

**Config profile:** `rest`

**Install extra:** none — dependencies are in the base set.

**Required environment variables:** none. This source needs no credential.

## Capabilities

| capability | supported | what it means |
|---|---|---|
| `supports_arrow` | no | hands back Arrow natively, without a pandas round-trip |
| `supports_streaming` | no | produces the table in chunks, re-blocked to the 65536-row Arrow IPC layout |
| `supports_incremental` | no | watermark mode; without it a config requesting one is refused, never silently downgraded |
| `supports_catalog` | no | a catalog layer above schema |
| `stable_scan_order` | yes | row order repeats across pulls without an ORDER BY — an observation, not a guarantee |
| `tz_sensitive` | no | session timezone can shift returned timestamps; aggregate parity is blind to a uniform shift, which is why min/max boundaries are asserted |

## Type representability

What happens to a source type on the way into the ramdb. `refused` is a feature: the writer fails loudly rather than landing a value that is quietly wrong.

| source type | lands as | verdict | note |
|---|---|---|---|
| `JSON number (integral)` | `int64` | **native** | — |
| `JSON number (fractional)` | `float64` | **native** | — |
| `JSON string` | `string` | **native** | — |
| `JSON boolean` | `bool` | **native** | — |
| `ISO-8601 timestamp string` | `datetime64[ns]` | **coerced** | Parsed from text into a timestamp by the extraction step. The source's timezone is whatever the recipe declared it to request, so the book pins it explicitly rather than accepting a provider default that could change. |
| `JSON null` | `null mask` | **coerced** | A JSON null becomes a real null rather than a sentinel. RF-002: null and NaN must stay distinguishable in the artifact, so a dataset declares its expected null count and the battery asserts it rather than inferring it from whatever landed. |
| `JSON object` | `string` | **string** | A nested object that no JSONPath extract flattens lands as its serialized text. The fix is normally a better extract in the recipe, not a downstream string parse. |
| `JSON array` | `string` | **string** | Same as objects: an array not unrolled into rows by the recipe lands as text. |
| `JSON number (above signed int32)` | `int64` | **refused** | RF-001 applies to this lane exactly as it does to the databases: the row64tools 1.0.x codec narrows int64 to signed int32 on store, so a large identifier from an API is refused at the writer rather than silently becoming a different number. |

## Failure modes

Operator messages are value-free by construction: they name the configured side only and never echo bytes from the source's own error text.

| reason code | what to do |
|---|---|
| `auth_failed` | The API rejected the request's credentials. Permanent — retrying with the same key will keep failing. Check the key in the recipe's env-file, that the file is still mode 0600, and that the key has not been rotated or scoped away from this endpoint. |
| `rate_limited` | The API rate-limited the pull. Transient. If it recurs at the configured cadence, the cadence is wrong for this provider's quota and belongs in the recipe's pagination and pacing spec rather than in a retry loop. |
| `response_schema_drift` | The response no longer matches the schema the recipe declared. This is the drift signal the lane exists to catch, and it is deliberately not recoverable in place: the pull exits non-zero and emits a repair event rather than guessing at a new interpretation of the payload. The recipe book is re-researched and re-admitted through the battery. |
| `destination_pin_violation` | A request tried to leave the host, scheme, or address space the recipe pinned at authoring time. Refused before the request was made. This is an invariant, not a setting: if the provider genuinely moved, the recipe is re-authored and reviewed, never widened at runtime. |

## Notes

- auth_mode is NONE because the admitted book — open-meteo — is a zero-credential public API. The lane itself supports keyed APIs; auth is declared per recipe as a path to a 0600 env-file, and the KEY NAME reaches config while the value never leaves the file (Law 3).
- stable_scan_order is True as an OBSERVATION about this lane's shape rather than a promise any provider makes: a recipe book's output order is the order the engine walks its recipes and rows, which is deterministic for a fixed book and a fixed response. A provider that reorders its own payload between calls would break it, which is what the pull-to-pull checksum comparison is there to detect.
- supports_incremental is False. Some APIs expose a cursor that would support a watermark, but nothing in the compiled-book path implements one yet, and declaring a capability with no fixture exercising it is exactly the untested claim the merge bar blocks on.
