# EVIDENCE — rest / open_meteo_berlin_hourly

**VERDICT: PASS** — 9 passed, 0 failed, 1 skipped. Generated 2026-08-17T12:09:15Z.

> Ratifies `246911cc1dba` from a clean tree: the code that ran is the code at that commit, and every input below is pinned by sha256.

> This pack is the review artifact (Law 2). Every comparison below records BOTH sides, passing ones included, so that a reviewer can ratify the driver from this file without reading the diff.

## Run

| field | value |
|---|---|
| dialect | `rest` |
| table | `open_meteo_berlin_hourly` |
| source | `open_meteo_berlin_hourly` |
| config | `/home/kos/builds/r64-db-engine/factory/targets/rest-openmeteo.yaml` |
| ground_truth | `/home/kos/builds/r64-db-engine/bench/GROUND-TRUTH-openmeteo.json` |
| spec | `/home/kos/builds/r64-db-engine/factory/specs/openmeteo-schema.json` |
| serve_gate | `True` |
| source_endpoint | `recipe book open-meteo.yaml over geocoding-api.open-meteo.com, archive-api.open-meteo.com` |
| source_timezone | `GMT` |
| note | `data checks read pull 1; the serve gate, when run, reads the file as it stands after pull 2 (identical when the checksum check passes)` |
| artifact.path | `/tmp/r64-factory-sweep/rest-openmeteo/arrow_out/open_meteo_berlin_hourly.arrow` |
| artifact.sha256_pull1 | `a16b8d2ed10a81a11ac9e7ddfb39e96d4afe0d6a22b8a1a3a4bcb61bf29870ca` |
| artifact.sha256_pull2 | `a16b8d2ed10a81a11ac9e7ddfb39e96d4afe0d6a22b8a1a3a4bcb61bf29870ca` |
| artifact.bytes | `36090` |
| artifact.rows | `2160` |
| artifact.blocks | `1` |

## Summary

| # | check | verdict | reason | detail |
|---|---|---|---|---|
| 1 | `registry_admission` | PASS |  | dialect 'rest' against registry ['clickhouse', 'postgres', 'rest'] |
| 2 | `schema_exactness` | PASS |  | 2 columns, string-width tolerance ON (B-3) |
| 3 | `aggregate_parity` | PASS |  | 5 aggregates vs ground truth |
| 4 | `rf002_null_discriminator` | SKIPPED |  | no nullable column to discriminate on: the open-meteo archive window 2026-01-01..2026-03-31 for Berlin returns 2160 hourly readings with zero nulls in temperature_2m (verified at the source), so no column can discriminate count(col) from count(*) |
| 5 | `b2_boundary` | PASS |  | boundary columns ['time'] vs live source (source session timezone: GMT) |
| 6 | `pg011_refusal` | PASS |  | refused with: sink 'arrow_ipc' cannot serve incremental mode (its output format is not appendable in place), but these tables request it: open_meteo_berlin_hourly. Use mode: full_refresh. |
| 7 | `block_structure` | PASS |  | 1 blocks expected for 2160 rows at 65536/block |
| 8 | `checksum` | PASS |  | two consecutive same-lane pulls are byte-identical (sha256 a16b8d2ed10a81a1…) |
| 9 | `recipe_security_invariants` | PASS |  | 14 destination-pinning mutations, all of which must be refused |
| 10 | `zero_copy_serve_gate` | PASS |  | counter deltas from the server's own get_flight_info app_metadata |

## 1. `registry_admission` — PASS

dialect 'rest' against registry ['clickhouse', 'postgres', 'rest']

| comparison | actual | expected | ok | code | note |
|---|---|---|:--:|---|---|
| resolved driver dialect_name() | `rest` | `rest` | ok |  |  |
| dialect present in registry listing | `["clickhouse", "postgres", "rest"]` | `contains 'rest'` | ok |  |  |
| drivers.resolve: refuses unregistered dialect | `raised` | `raised` | ok |  |  |
| drivers.resolve: refusal lists registered dialects | `["clickhouse", "postgres", "rest"]` | `["clickhouse", "postgres", "rest"]` | ok |  | message: unknown dialect 'definitely-not-a-registered-dialect' (available: clickhouse, postgres, rest) |
| Config validation: refuses unregistered dialect | `raised` | `raised` | ok |  |  |
| Config validation: refusal lists registered dialects | `["clickhouse", "postgres", "rest"]` | `["clickhouse", "postgres", "rest"]` | ok |  | message: 1 validation error for Config   Value error, unknown dialect 'definitely-not-a-registered-dialect' (registered: clickhouse, postgres, rest) [type=value_error, input_value={'dialect': 'definitely-n...: 0, 'metrics_port': 0}}, input_type=dict]     For further information visit https://erro... |

## 2. `schema_exactness` — PASS

2 columns, string-width tolerance ON (B-3)

| comparison | actual | expected | ok | code | note |
|---|---|---|:--:|---|---|
| column count | `2` | `2` | ok |  |  |
| column names and order | `["time", "temperature_2m"]` | `["time", "temperature_2m"]` | ok |  |  |
| type[time] | `timestamp[us]` | `timestamp[us]` | ok |  |  |
| type[temperature_2m] | `double` | `double` | ok |  |  |

## 3. `aggregate_parity` — PASS

5 aggregates vs ground truth

| comparison | actual | expected | ok | code | note |
|---|---|---|:--:|---|---|
| count | `2160` | `2160` | ok |  |  |
| count_temp_null | `0` | `0` | ok |  |  |
| pct_temp_null | `0.0` | `0.0` | ok |  |  |
| scaled_temp_sum_exact_int | `41712` | `41712` | ok |  |  |
| scaled_temp_sum | `41712` | `41712` | ok |  | corroborating only — does not gate (float-order sensitivity) |

## 4. `rf002_null_discriminator` — SKIPPED

no nullable column to discriminate on: the open-meteo archive window 2026-01-01..2026-03-31 for Berlin returns 2160 hourly readings with zero nulls in temperature_2m (verified at the source), so no column can discriminate count(col) from count(*)

## 5. `b2_boundary` — PASS

boundary columns ['time'] vs live source (source session timezone: GMT)

| comparison | actual | expected | ok | code | note |
|---|---|---|:--:|---|---|
| time: min | `2026-01-01 00:00:00.000000` | `2026-01-01 00:00:00.000000` | ok |  |  |
| time: max | `2026-03-31 23:00:00.000000` | `2026-03-31 23:00:00.000000` | ok |  |  |

Source queries issued:

```sql
GET https://geocoding-api.open-meteo.com/v1/search?name=Berlin&count=1&format=json
GET https://archive-api.open-meteo.com/v1/archive?start_date=2026-01-01&end_date=2026-03-31&hourly=temperature_2m&timezone=UTC&latitude=52.52437&longitude=13.41053
```

<details><summary>observations</summary>

```json
{
  "source_timezone": "GMT"
}
```

</details>

## 6. `pg011_refusal` — PASS

refused with: sink 'arrow_ipc' cannot serve incremental mode (its output format is not appendable in place), but these tables request it: open_meteo_berlin_hourly. Use mode: full_refresh.

| comparison | actual | expected | ok | code | note |
|---|---|---|:--:|---|---|
| incremental on non-appendable sink | `raised` | `raised` | ok |  |  |
| refusal names the cause | `sink 'arrow_ipc' cannot serve incremental mode (its output format is not appendable in place), but these tables request it: open_meteo_berlin_hourly. Use mode: full_refresh.` | `cannot serve incremental mode` | ok |  |  |

## 7. `block_structure` — PASS

1 blocks expected for 2160 rows at 65536/block

| comparison | actual | expected | ok | code | note |
|---|---|---|:--:|---|---|
| block count | `1` | `1` | ok |  |  |
| rows across blocks | `2160` | `2160` | ok |  |  |
| block layout | `[2160]` | `[2160]` | ok |  | 65536-row blocks, final block carries the remainder |

## 8. `checksum` — PASS

two consecutive same-lane pulls are byte-identical (sha256 a16b8d2ed10a81a1…)

| comparison | actual | expected | ok | code | note |
|---|---|---|:--:|---|---|
| sha256 (pull 1 vs pull 2) | `a16b8d2ed10a81a11ac9e7ddfb39e96d4afe0d6a22b8a1a3a4bcb61bf29870ca` | `a16b8d2ed10a81a11ac9e7ddfb39e96d4afe0d6a22b8a1a3a4bcb61bf29870ca` | ok |  |  |

<details><summary>observations</summary>

```json
{
  "lane_scope": "byte-identity asserted WITHIN this lane only; cross-lane comparison uses data + schema-minus-metadata + block structure, with string-width tolerance"
}
```

</details>

## 9. `recipe_security_invariants` — PASS

14 destination-pinning mutations, all of which must be refused

| comparison | actual | expected | ok | code | note |
|---|---|---|:--:|---|---|
| https->http downgrade in recipe[0].url | `REFUSED` | `REFUSED` | ok |  | RecipeSecurityError: recipe URL must use https, got http:// in 'http://geocoding-api.open-meteo.com/v1/search'. Plaintext is refused outright rather than warned about: a recipe carries an API key, and there is no configuration under which sending it in the clear is the intended behaviour. |
| lookalike host evil-geocoding-api.open-meteo.com against pinned geocoding-api.open-meteo.com | `REFUSED` | `REFUSED` | ok |  | RecipeSecurityError: recipe host 'evil-geocoding-api.open-meteo.com' is not 'geocoding-api.open-meteo.com' nor a subdomain of it. The URL is pinned at recipe creation and runtime inputs may populate declared body/query parameters only — never the host or the path. |
| lookalike host evil-archive-api.open-meteo.com against pinned archive-api.open-meteo.com | `REFUSED` | `REFUSED` | ok |  | RecipeSecurityError: recipe host 'evil-archive-api.open-meteo.com' is not 'archive-api.open-meteo.com' nor a subdomain of it. The URL is pinned at recipe creation and runtime inputs may populate declared body/query parameters only — never the host or the path. |
| templated url (host/path substitution) | `REFUSED` | `REFUSED` | ok |  | RecipeSecurityError: recipes[geocode].url contains a template placeholder: 'https://geocoding-api.open-meteo.com/v1/search/{path}'. The URL is PINNED at recipe creation — runtime inputs may populate declared body/query parameters only, never the host or the path. A substitutable URL is a destination |
| undeclared threading input | `REFUSED` | `REFUSED` | ok |  | RecipeBookError: threading[1] supplies input(s) ['not_a_declared_param'] that recipe 'archive' does not declare in params_schema.properties (declared: ['end_date', 'hourly', 'latitude', 'longitude', 'start_date', 'timezone']). Runtime inputs may only populate DECLARED parameters. |
| loopback destination (SSRF) | `REFUSED` | `REFUSED` | ok |  | RecipeSecurityError: recipe host resolves to 127.0.0.1 (loopback address space), which is refused. Reaching an internal service through a config-described call is the SSRF shape this fence exists to prevent. |
| cross-path next-URL against pinned https://geocoding-api.open-meteo.com/v1/search | `REFUSED` | `REFUSED` | ok |  | RecipeSecurityError: pagination next-URL path '/v1/../admin' is neither the recipe's pinned path '/v1/search' nor one of its declared allowed_next_paths []. Cross-path pagination must be declared at authoring time; it is never inferred from what a provider sends. |
| undeclared path next-URL against pinned https://geocoding-api.open-meteo.com/v1/search | `REFUSED` | `REFUSED` | ok |  | RecipeSecurityError: pagination next-URL path '/definitely-not-the-pinned-path' is neither the recipe's pinned path '/v1/search' nor one of its declared allowed_next_paths []. Cross-path pagination must be declared at authoring time; it is never inferred from what a provider sends. |
| subdomain next-URL against pinned https://geocoding-api.open-meteo.com/v1/search | `REFUSED` | `REFUSED` | ok |  | RecipeSecurityError: pagination next-URL host 'attacker.geocoding-api.open-meteo.com' does not EXACTLY match the recipe's pinned host 'geocoding-api.open-meteo.com'. Subdomain latitude is deliberately not available on the pagination path: this URL came from the provider, not from the recipe author. |
| cross-path next-URL against pinned https://archive-api.open-meteo.com/v1/archive | `REFUSED` | `REFUSED` | ok |  | RecipeSecurityError: pagination next-URL path '/v1/../admin' is neither the recipe's pinned path '/v1/archive' nor one of its declared allowed_next_paths []. Cross-path pagination must be declared at authoring time; it is never inferred from what a provider sends. |
| undeclared path next-URL against pinned https://archive-api.open-meteo.com/v1/archive | `REFUSED` | `REFUSED` | ok |  | RecipeSecurityError: pagination next-URL path '/definitely-not-the-pinned-path' is neither the recipe's pinned path '/v1/archive' nor one of its declared allowed_next_paths []. Cross-path pagination must be declared at authoring time; it is never inferred from what a provider sends. |
| subdomain next-URL against pinned https://archive-api.open-meteo.com/v1/archive | `REFUSED` | `REFUSED` | ok |  | RecipeSecurityError: pagination next-URL host 'attacker.archive-api.open-meteo.com' does not EXACTLY match the recipe's pinned host 'archive-api.open-meteo.com'. Subdomain latitude is deliberately not available on the pagination path: this URL came from the provider, not from the recipe author. |
| cross-path next-URL with allowed_next_paths OMITTED | `REFUSED` | `REFUSED` | ok |  | RecipeSecurityError: pagination next-URL path '/somewhere-else' is neither the recipe's pinned path '/v1/search' nor one of its declared allowed_next_paths []. Cross-path pagination must be declared at authoring time; it is never inferred from what a provider sends. |
| cross-path next-URL with allowed_next_paths EXPLICITLY EMPTY | `REFUSED` | `REFUSED` | ok |  | RecipeSecurityError: pagination next-URL path '/somewhere-else' is neither the recipe's pinned path '/v1/search' nor one of its declared allowed_next_paths []. Cross-path pagination must be declared at authoring time; it is never inferred from what a provider sends. |

## 10. `zero_copy_serve_gate` — PASS

counter deltas from the server's own get_flight_info app_metadata

| comparison | actual | expected | ok | code | note |
|---|---|---|:--:|---|---|
| cold: copied_columns | `0` | `0` | ok |  |  |
| warm: copied_columns | `0` | `0` | ok |  |  |
| cold: zero_copy_columns == columns_decoded | `1 vs 1` | `equal` | ok |  |  |
| cold: columns actually decoded | `1` | `> 0` | ok |  | a cold pass that decodes nothing did not exercise the reader |
| warm: miss_rate % | `0.0` | `0.0` | ok |  |  |
| warm: columns_decoded | `0` | `0` | ok |  | stronger than miss_rate: a warm pass must decode nothing at all |

<details><summary>observations</summary>

```json
{
  "cold_delta": {
    "cache_hits": 0,
    "cache_misses": 1,
    "columns_decoded": 1,
    "blocks_assembled": 1,
    "zero_copy_columns": 1,
    "copied_columns": 0,
    "bytes_read": 17280,
    "bytes_served_cached": 0
  },
  "warm_delta": {
    "cache_hits": 1,
    "cache_misses": 0,
    "columns_decoded": 0,
    "blocks_assembled": 1,
    "zero_copy_columns": 0,
    "copied_columns": 0,
    "bytes_read": 0,
    "bytes_served_cached": 17280
  },
  "sql": "SELECT count(*), min(time), max(time) FROM open_meteo_berlin_hourly",
  "addr": "127.0.0.1:8903",
  "pid": 1015086,
  "baseline": {
    "cache_hits": 0,
    "cache_misses": 0,
    "columns_decoded": 0,
    "blocks_assembled": 0,
    "zero_copy_columns": 0,
    "copied_columns": 0,
    "bytes_read": 0,
    "bytes_served_cached": 0
  },
  "rows": 1
}
```

</details>

## CLOSURE BOUNDARY — what this pack does NOT establish

| item | pinned | why |
|---|---|---|
| secret contents | **no** | a sha256 of a low-entropy API key is offline-guessable, and an evidence pack travels. Only path, size, mtime and mode are recorded — enough to say the same secret file was in place, nothing about the secret. |
| native and runtime dependencies beyond the lockfiles | **no** | pyproject.toml and uv.lock fix the declared and resolved Python sets, and the meshroad binary is content-addressed. Shared libraries, the OS package set and the container's own contents are NOT pinned; the container image digest is recorded, which identifies the image but does not reconstruct it. |
| live source state | measured, not pinned | a live database or API cannot be pinned by a pack — it is not ours and it moves. What the pack carries is MEASUREMENT of it at run time: row counts, aggregates, min/max bounds, session timezone, and the artifact's content address. Those values are already in this pack; they establish what the sou... |
| the machine's wall clock and scheduling | **no** | no timing claim is made by any check in this battery, so clock and load are deliberately outside the boundary rather than silently assumed. |

## Provenance — what this pack ratifies

```json
{
  "allow_dirty": false,
  "artifact": {
    "bytes": 36090,
    "path": "factory/evidence/artifacts/a16b8d2ed10a81a11ac9e7ddfb39e96d4afe0d6a22b8a1a3a4bcb61bf29870ca.arrow",
    "produced_at": "/tmp/r64-factory-sweep/rest-openmeteo/arrow_out/open_meteo_berlin_hourly.arrow",
    "sha256": "a16b8d2ed10a81a11ac9e7ddfb39e96d4afe0d6a22b8a1a3a4bcb61bf29870ca",
    "storage": "copied",
    "suffix": ".arrow"
  },
  "closure_boundary": [
    {
      "item": "secret contents",
      "pinned": false,
      "why": "a sha256 of a low-entropy API key is offline-guessable, and an evidence pack travels. Only path, size, mtime and mode are recorded \u2014 enough to say the same secret file was in place, nothing about the secret."
    },
    {
      "item": "native and runtime dependencies beyond the lockfiles",
      "pinned": false,
      "why": "pyproject.toml and uv.lock fix the declared and resolved Python sets, and the meshroad binary is content-addressed. Shared libraries, the OS package set and the container's own contents are NOT pinned; the container image digest is recorded, which identifies the image but does not reconstruct it."
    },
    {
      "item": "live source state",
      "measured": true,
      "pinned": false,
      "why": "a live database or API cannot be pinned by a pack \u2014 it is not ours and it moves. What the pack carries is MEASUREMENT of it at run time: row counts, aggregates, min/max bounds, session timezone, and the artifact's content address. Those values are already in this pack; they establish what the source held during this run, not that it will hold it again."
    },
    {
      "item": "the machine's wall clock and scheduling",
      "pinned": false,
      "why": "no timing claim is made by any check in this battery, so clock and load are deliberately outside the boundary rather than silently assumed."
    }
  ],
  "command": ".venv/bin/python -m factory.conformance --dialect rest --config /home/kos/builds/r64-db-engine/factory/targets/rest-openmeteo.yaml --ground-truth /home/kos/builds/r64-db-engine/bench/GROUND-TRUTH-openmeteo.json --table open_meteo_berlin_hourly --evidence-dir /home/kos/builds/r64-db-engine/factory/evidence --work-dir /tmp/r64-factory-sweep/rest-openmeteo --serve-gate",
  "git": {
    "branch": "feat/meshforge-factory",
    "commit": "246911cc1dbaf80a5f597f6c8022d27f811d84cf",
    "dirty": false,
    "dirty_exemption": "factory/evidence/"
  },
  "implementation": {
    "distribution_version": "0.1.0",
    "source_files": 46,
    "source_sha256": "a7d75c3929f4e441720461b3f9585b554e47266fa4c4bdb1a609f0019a8f338b"
  },
  "inputs": {
    "ground_truth": {
      "path": "/home/kos/builds/r64-db-engine/bench/GROUND-TRUTH-openmeteo.json",
      "sha256": "334538dd7817392f35d5fb9393df3fce27ca89cc2c54b5cbc3a416eeed891d48"
    },
    "recipe_book": {
      "path": "/home/kos/builds/r64-db-engine/factory/recipes/open-meteo.yaml",
      "sha256": "47bf2d4f186ac7fb742d36a9d5942d4725f01a7bc1fa0d7cc2fb3368ce25126b"
    },
    "schema_spec": {
      "path": "/home/kos/builds/r64-db-engine/factory/specs/openmeteo-schema.json",
      "sha256": "9da38ec6a85ea35dbc5294089e3015e2a0cbd3ca202f8373b548fb29b1e82376"
    },
    "target_config": {
      "path": "/home/kos/builds/r64-db-engine/factory/targets/rest-openmeteo.yaml",
      "sha256": "880b232281840e8619ff51e551cb8456e84c92d169602801cced2bbfa7693a3f"
    }
  },
  "proxy_environment": {
    "_note": "no proxy-related environment variables were set"
  },
  "secret_references": [],
  "toolchain": {
    "meshroad_binary": {
      "bytes": 106288656,
      "path": "/usr/local/bin/meshroad",
      "sha256": "05e056d48f4ca8551cc3f11c97abeb2fc670cb6d951c5394cbd9d9e16d1e236d"
    },
    "platform_triple": "Linux-x86_64-glibc",
    "pyproject_toml": {
      "path": "pyproject.toml",
      "sha256": "7c02da1c2dc19e3fd47c7747c86dff4832ab4aafc250796730c3fd6aff4f6da0"
    },
    "python": "3.13.12",
    "python_implementation": "CPython",
    "uv_lock": {
      "path": "uv.lock",
      "sha256": "c41b2599c5b0a596e33728ae27a4e506a97f99bb31bfef7fb708e552466e6e08"
    }
  }
}
```

## Environment

```json
{
  "python": "3.13.12",
  "platform": "Linux-7.1.5-1-cachyos-x86_64-with-glibc2.44",
  "packages": {
    "pyarrow": "25.0.0",
    "pandas": "3.0.5",
    "pydantic": "2.13.4",
    "clickhouse_connect": "1.6.0",
    "row64tools": "1.0.11",
    "jsonschema": "4.26.0",
    "httpx": "0.28.1"
  },
  "git": {
    "commit": "246911cc1dbaf80a5f597f6c8022d27f811d84cf",
    "branch": "feat/meshforge-factory",
    "dirty": false,
    "dirty_exemption": "factory/evidence/"
  }
}
```
