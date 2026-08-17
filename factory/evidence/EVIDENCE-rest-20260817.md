# EVIDENCE — rest / open_meteo_berlin_hourly

**VERDICT: PASS** — 9 passed, 0 failed, 1 skipped. Generated 2026-08-17T08:53:39Z.

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

| # | check | verdict | detail |
|---|---|---|---|
| 1 | `registry_admission` | PASS | dialect 'rest' against registry ['clickhouse', 'postgres', 'rest'] |
| 2 | `schema_exactness` | PASS | 2 columns, string-width tolerance ON (B-3) |
| 3 | `aggregate_parity` | PASS | 5 aggregates vs ground truth |
| 4 | `rf002_null_discriminator` | SKIPPED | no nullable column to discriminate on: the open-meteo archive window 2026-01-01..2026-03-31 for Berlin returns 2160 hourly readings with zero nulls in temperature_2m (verified at the source), so no column can discriminate count(col) from count(*) |
| 5 | `b2_boundary` | PASS | boundary columns ['time'] vs live source (source session timezone: GMT) |
| 6 | `pg011_refusal` | PASS | refused with: sink 'arrow_ipc' cannot serve incremental mode (its output format is not appendable in place), but these tables request it: open_meteo_berlin_hourly. Use mode: full_refresh. |
| 7 | `block_structure` | PASS | 1 blocks expected for 2160 rows at 65536/block |
| 8 | `checksum` | PASS | two consecutive same-lane pulls are byte-identical (sha256 a16b8d2ed10a81a1…) |
| 9 | `recipe_security_invariants` | PASS | 6 destination-pinning mutations, all of which must be refused |
| 10 | `zero_copy_serve_gate` | PASS | counter deltas from the server's own get_flight_info app_metadata |

## 1. `registry_admission` — PASS

dialect 'rest' against registry ['clickhouse', 'postgres', 'rest']

| comparison | actual | expected | ok | note |
|---|---|---|:--:|---|
| resolved driver dialect_name() | `rest` | `rest` | ok |  |
| dialect present in registry listing | `["clickhouse", "postgres", "rest"]` | `contains 'rest'` | ok |  |
| drivers.resolve: refuses unregistered dialect | `raised` | `raised` | ok |  |
| drivers.resolve: refusal lists registered dialects | `["clickhouse", "postgres", "rest"]` | `["clickhouse", "postgres", "rest"]` | ok | message: unknown dialect 'definitely-not-a-registered-dialect' (available: clickhouse, postgres, rest) |
| Config validation: refuses unregistered dialect | `raised` | `raised` | ok |  |
| Config validation: refusal lists registered dialects | `["clickhouse", "postgres", "rest"]` | `["clickhouse", "postgres", "rest"]` | ok | message: 1 validation error for Config   Value error, unknown dialect 'definitely-not-a-registered-dialect' (registered: clickhouse, postgres, rest) [type=value_error, input_value={'dialect': 'definitely-n...: 0, 'metrics_port': 0}}, input_type=dict]     For further information visit https://erro... |

## 2. `schema_exactness` — PASS

2 columns, string-width tolerance ON (B-3)

| comparison | actual | expected | ok | note |
|---|---|---|:--:|---|
| column count | `2` | `2` | ok |  |
| column names and order | `["time", "temperature_2m"]` | `["time", "temperature_2m"]` | ok |  |
| type[time] | `timestamp[us]` | `timestamp[us]` | ok |  |
| type[temperature_2m] | `double` | `double` | ok |  |

## 3. `aggregate_parity` — PASS

5 aggregates vs ground truth

| comparison | actual | expected | ok | note |
|---|---|---|:--:|---|
| count | `2160` | `2160` | ok |  |
| count_temp_null | `0` | `0` | ok |  |
| pct_temp_null | `0.0` | `0.0` | ok |  |
| scaled_temp_sum_exact_int | `41712` | `41712` | ok |  |
| scaled_temp_sum | `41712` | `41712` | ok | corroborating only — does not gate (float-order sensitivity) |

## 4. `rf002_null_discriminator` — SKIPPED

no nullable column to discriminate on: the open-meteo archive window 2026-01-01..2026-03-31 for Berlin returns 2160 hourly readings with zero nulls in temperature_2m (verified at the source), so no column can discriminate count(col) from count(*)

## 5. `b2_boundary` — PASS

boundary columns ['time'] vs live source (source session timezone: GMT)

| comparison | actual | expected | ok | note |
|---|---|---|:--:|---|
| time: min | `2026-01-01 00:00:00.000000` | `2026-01-01 00:00:00.000000` | ok |  |
| time: max | `2026-03-31 23:00:00.000000` | `2026-03-31 23:00:00.000000` | ok |  |

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

| comparison | actual | expected | ok | note |
|---|---|---|:--:|---|
| incremental on non-appendable sink | `raised` | `raised` | ok |  |
| refusal names the cause | `sink 'arrow_ipc' cannot serve incremental mode (its output format is not appendable in place), but these tables request it: open_meteo_berlin_hourly. Use mode: full_refresh.` | `cannot serve incremental mode` | ok |  |

## 7. `block_structure` — PASS

1 blocks expected for 2160 rows at 65536/block

| comparison | actual | expected | ok | note |
|---|---|---|:--:|---|
| block count | `1` | `1` | ok |  |
| rows across blocks | `2160` | `2160` | ok |  |
| block layout | `[2160]` | `[2160]` | ok | 65536-row blocks, final block carries the remainder |

## 8. `checksum` — PASS

two consecutive same-lane pulls are byte-identical (sha256 a16b8d2ed10a81a1…)

| comparison | actual | expected | ok | note |
|---|---|---|:--:|---|
| sha256 (pull 1 vs pull 2) | `a16b8d2ed10a81a11ac9e7ddfb39e96d4afe0d6a22b8a1a3a4bcb61bf29870ca` | `a16b8d2ed10a81a11ac9e7ddfb39e96d4afe0d6a22b8a1a3a4bcb61bf29870ca` | ok |  |

<details><summary>observations</summary>

```json
{
  "lane_scope": "byte-identity asserted WITHIN this lane only; cross-lane comparison uses data + schema-minus-metadata + block structure, with string-width tolerance"
}
```

</details>

## 9. `recipe_security_invariants` — PASS

6 destination-pinning mutations, all of which must be refused

| comparison | actual | expected | ok | note |
|---|---|---|:--:|---|
| https->http downgrade in recipe[0].url | `REFUSED` | `REFUSED` | ok | RecipeSecurityError: recipe URL must use https, got http:// in 'http://geocoding-api.open-meteo.com/v1/search'. Plaintext is refused outright rather than warned about: a recipe carries an API key, and there is no configuration under which sending it in the clear is the intended behaviour. |
| lookalike host evil-geocoding-api.open-meteo.com against pinned geocoding-api.open-meteo.com | `REFUSED` | `REFUSED` | ok | RecipeSecurityError: recipe host 'evil-geocoding-api.open-meteo.com' is not 'geocoding-api.open-meteo.com' nor a subdomain of it. The URL is pinned at recipe creation and runtime inputs may populate declared body/query parameters only — never the host or the path. |
| lookalike host evil-archive-api.open-meteo.com against pinned archive-api.open-meteo.com | `REFUSED` | `REFUSED` | ok | RecipeSecurityError: recipe host 'evil-archive-api.open-meteo.com' is not 'archive-api.open-meteo.com' nor a subdomain of it. The URL is pinned at recipe creation and runtime inputs may populate declared body/query parameters only — never the host or the path. |
| templated url (host/path substitution) | `REFUSED` | `REFUSED` | ok | RecipeSecurityError: recipes[geocode].url contains a template placeholder: 'https://geocoding-api.open-meteo.com/v1/search/{path}'. The URL is PINNED at recipe creation — runtime inputs may populate declared body/query parameters only, never the host or the path. A substitutable URL is a destination |
| undeclared threading input | `REFUSED` | `REFUSED` | ok | RecipeBookError: threading[1] supplies input(s) ['not_a_declared_param'] that recipe 'archive' does not declare in params_schema.properties (declared: ['end_date', 'hourly', 'latitude', 'longitude', 'start_date', 'timezone']). Runtime inputs may only populate DECLARED parameters. |
| loopback destination (SSRF) | `REFUSED` | `REFUSED` | ok | RecipeSecurityError: recipe host resolves to 127.0.0.1 (loopback address space), which is refused. Reaching an internal service through a config-described call is the SSRF shape this fence exists to prevent. |

## 10. `zero_copy_serve_gate` — PASS

counter deltas from the server's own get_flight_info app_metadata

| comparison | actual | expected | ok | note |
|---|---|---|:--:|---|
| cold: copied_columns | `0` | `0` | ok |  |
| warm: copied_columns | `0` | `0` | ok |  |
| cold: zero_copy_columns == columns_decoded | `1 vs 1` | `equal` | ok |  |
| cold: columns actually decoded | `1` | `> 0` | ok | a cold pass that decodes nothing did not exercise the reader |
| warm: miss_rate % | `0.0` | `0.0` | ok |  |
| warm: columns_decoded | `0` | `0` | ok | stronger than miss_rate: a warm pass must decode nothing at all |

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
  "pid": 725716,
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
    "commit": "c4122fd8c6332257fcc039ba93d4152d7b78d9db",
    "branch": "feat/meshforge-factory",
    "dirty": true
  }
}
```
