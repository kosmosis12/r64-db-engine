# EVIDENCE — clickhouse / perf_1m

**VERDICT: PASS** — 9 passed, 0 failed, 1 skipped. Generated 2026-08-17T11:25:58Z.

> Ratifies `6655b9026f23` from a clean tree: the code that ran is the code at that commit, and every input below is pinned by sha256.

> This pack is the review artifact (Law 2). Every comparison below records BOTH sides, passing ones included, so that a reviewer can ratify the driver from this file without reading the diff.

## Run

| field | value |
|---|---|
| dialect | `clickhouse` |
| table | `perf_1m` |
| source | `SELECT * FROM meshbench.perf_1m ORDER BY row_id` |
| config | `/home/kos/builds/r64-db-engine/factory/targets/clickhouse-meshbench.yaml` |
| ground_truth | `/home/kos/builds/r64-db-engine/bench/GROUND-TRUTH-clickhouse.json` |
| spec | `/home/kos/builds/r64-db-engine/factory/specs/meshbench-schema.json` |
| serve_gate | `True` |
| source_endpoint | `http://127.0.0.1:8123/ (database=meshbench, user=default)` |
| source_timezone | `UTC` |
| note | `data checks read pull 1; the serve gate, when run, reads the file as it stands after pull 2 (identical when the checksum check passes)` |
| artifact.path | `/tmp/r64-factory-sweep/clickhouse-meshbench/arrow_out/perf_1m.arrow` |
| artifact.sha256_pull1 | `db2912dfbd6a42337704e121f6872484f2e51eca7439cfe223d4bb72cc73ea4a` |
| artifact.sha256_pull2 | `db2912dfbd6a42337704e121f6872484f2e51eca7439cfe223d4bb72cc73ea4a` |
| artifact.bytes | `149806522` |
| artifact.rows | `1000000` |
| artifact.blocks | `16` |

## Summary

| # | check | verdict | reason | detail |
|---|---|---|---|---|
| 1 | `registry_admission` | PASS |  | dialect 'clickhouse' against registry ['clickhouse', 'postgres', 'rest'] |
| 2 | `schema_exactness` | PASS |  | 14 columns, string-width tolerance ON (B-3) |
| 3 | `aggregate_parity` | PASS |  | 12 aggregates vs ground truth |
| 4 | `rf002_null_discriminator` | PASS |  | 1 declared discriminator(s) on perf_1m: score |
| 5 | `b2_boundary` | PASS |  | boundary columns ['event_time'] vs live source (source session timezone: UTC) |
| 6 | `pg011_refusal` | PASS |  | refused with: sink 'arrow_ipc' cannot serve incremental mode (its output format is not appendable in place), but these tables request it: perf_1m. Use mode: full_refresh. |
| 7 | `block_structure` | PASS |  | 16 blocks expected for 1000000 rows at 65536/block |
| 8 | `checksum` | PASS |  | two consecutive same-lane pulls are byte-identical (sha256 db2912dfbd6a4233…) |
| 9 | `recipe_security_invariants` | SKIPPED |  | not a recipe-lane dialect; no recipe book to mutate |
| 10 | `zero_copy_serve_gate` | PASS |  | counter deltas from the server's own get_flight_info app_metadata |

## 1. `registry_admission` — PASS

dialect 'clickhouse' against registry ['clickhouse', 'postgres', 'rest']

| comparison | actual | expected | ok | code | note |
|---|---|---|:--:|---|---|
| resolved driver dialect_name() | `clickhouse` | `clickhouse` | ok |  |  |
| dialect present in registry listing | `["clickhouse", "postgres", "rest"]` | `contains 'clickhouse'` | ok |  |  |
| drivers.resolve: refuses unregistered dialect | `raised` | `raised` | ok |  |  |
| drivers.resolve: refusal lists registered dialects | `["clickhouse", "postgres", "rest"]` | `["clickhouse", "postgres", "rest"]` | ok |  | message: unknown dialect 'definitely-not-a-registered-dialect' (available: clickhouse, postgres, rest) |
| Config validation: refuses unregistered dialect | `raised` | `raised` | ok |  |  |
| Config validation: refusal lists registered dialects | `["clickhouse", "postgres", "rest"]` | `["clickhouse", "postgres", "rest"]` | ok |  | message: 1 validation error for Config   Value error, unknown dialect 'definitely-not-a-registered-dialect' (registered: clickhouse, postgres, rest) [type=value_error, input_value={'dialect': 'definitely-n...: 0, 'metrics_port': 0}}, input_type=dict]     For further information visit https://erro... |

## 2. `schema_exactness` — PASS

14 columns, string-width tolerance ON (B-3)

| comparison | actual | expected | ok | code | note |
|---|---|---|:--:|---|---|
| column count | `14` | `14` | ok |  |  |
| column names and order | `["row_id", "account_id", "user_id", "region", "city", "category", "segment", "product_name", "status", "amount", "quantity", "price", "score", "event_time"]` | `["row_id", "account_id", "user_id", "region", "city", "category", "segment", "product_name", "status", "amount", "quantity", "price", "score", "event_time"]` | ok |  |  |
| type[row_id] | `int64` | `int64` | ok |  |  |
| type[account_id] | `int64` | `int64` | ok |  |  |
| type[user_id] | `int64` | `int64` | ok |  |  |
| type[region] | `large_string` | `large_string` | ok |  |  |
| type[city] | `large_string` | `large_string` | ok |  |  |
| type[category] | `large_string` | `large_string` | ok |  |  |
| type[segment] | `large_string` | `large_string` | ok |  |  |
| type[product_name] | `large_string` | `large_string` | ok |  |  |
| type[status] | `dictionary<values=string, indices=int32, ordered=0>` | `dictionary<values=string, indices=int32, ordered=0>` | ok |  |  |
| type[amount] | `double` | `double` | ok |  |  |
| type[quantity] | `int64` | `int64` | ok |  |  |
| type[price] | `double` | `double` | ok |  |  |
| type[score] | `double` | `double` | ok |  |  |
| type[event_time] | `timestamp[us]` | `timestamp[us]` | ok |  |  |

## 3. `aggregate_parity` — PASS

12 aggregates vs ground truth

| comparison | actual | expected | ok | code | note |
|---|---|---|:--:|---|---|
| count | `1000000` | `1000000` | ok |  |  |
| uniq_status | `6` | `6` | ok |  |  |
| uniq_region | `8` | `8` | ok |  |  |
| uniq_product_name | `50000` | `50000` | ok |  |  |
| count_status_active | `166627` | `166627` | ok |  |  |
| count_region_west | `124935` | `124935` | ok |  |  |
| sum_quantity | `250480021` | `250480021` | ok |  |  |
| max_account_id | `3599999873` | `3599999873` | ok |  |  |
| scaled_amount_sum_exact_int | `11994337292` | `11994337292` | ok |  |  |
| scaled_amount_sum | `11994337292` | `11994337292` | ok |  | corroborating only — does not gate (float-order sensitivity) |
| count_score_null | `20039` | `20039` | ok |  |  |
| pct_score_null | `2.0039` | `2.0039` | ok |  |  |

## 4. `rf002_null_discriminator` — PASS

1 declared discriminator(s) on perf_1m: score

| comparison | actual | expected | ok | code | note |
|---|---|---|:--:|---|---|
| score: spec vs ground-truth null count | `20039` | `20039` | ok |  | two independent records of the same number; disagreement means one of the two files is stale |
| score: artifact null_count | `20039` | `20039` | ok |  |  |
| score: count(col) vs count(*) | `979961 vs 1000000` | `must differ` | ok |  | if equal, nulls were filled in transit |
| score: NaN smuggled as a value | `0` | `0` | ok |  | a literal NaN sets null_count=0 and poisons every downstream sum() |

## 5. `b2_boundary` — PASS

boundary columns ['event_time'] vs live source (source session timezone: UTC)

| comparison | actual | expected | ok | code | note |
|---|---|---|:--:|---|---|
| event_time: min | `2026-01-01 00:00:15.184566` | `2026-01-01 00:00:15.184566` | ok |  |  |
| event_time: max | `2026-06-29 23:59:30.942340` | `2026-06-29 23:59:30.942340` | ok |  |  |

Source queries issued:

```sql
SELECT timezone()
SELECT toString(min(event_time)), toString(max(event_time)) FROM (SELECT * FROM meshbench.perf_1m ORDER BY row_id) AS sub
```

<details><summary>observations</summary>

```json
{
  "source_timezone": "UTC"
}
```

</details>

## 6. `pg011_refusal` — PASS

refused with: sink 'arrow_ipc' cannot serve incremental mode (its output format is not appendable in place), but these tables request it: perf_1m. Use mode: full_refresh.

| comparison | actual | expected | ok | code | note |
|---|---|---|:--:|---|---|
| incremental on non-appendable sink | `raised` | `raised` | ok |  |  |
| refusal names the cause | `sink 'arrow_ipc' cannot serve incremental mode (its output format is not appendable in place), but these tables request it: perf_1m. Use mode: full_refresh.` | `cannot serve incremental mode` | ok |  |  |

## 7. `block_structure` — PASS

16 blocks expected for 1000000 rows at 65536/block

| comparison | actual | expected | ok | code | note |
|---|---|---|:--:|---|---|
| block count | `16` | `16` | ok |  |  |
| rows across blocks | `1000000` | `1000000` | ok |  |  |
| block layout | `[65536, 65536, 65536, 65536, 65536, 65536, 65536, 65536, 65536, 65536, 65536, 65536, 65536, 65536, 65536, 16960]` | `[65536, 65536, 65536, 65536, 65536, 65536, 65536, 65536, 65536, 65536, 65536, 65536, 65536, 65536, 65536, 16960]` | ok |  | 65536-row blocks, final block carries the remainder |

## 8. `checksum` — PASS

two consecutive same-lane pulls are byte-identical (sha256 db2912dfbd6a4233…)

| comparison | actual | expected | ok | code | note |
|---|---|---|:--:|---|---|
| sha256 (pull 1 vs pull 2) | `db2912dfbd6a42337704e121f6872484f2e51eca7439cfe223d4bb72cc73ea4a` | `db2912dfbd6a42337704e121f6872484f2e51eca7439cfe223d4bb72cc73ea4a` | ok |  |  |

<details><summary>observations</summary>

```json
{
  "lane_scope": "byte-identity asserted WITHIN this lane only; cross-lane comparison uses data + schema-minus-metadata + block structure, with string-width tolerance"
}
```

</details>

## 9. `recipe_security_invariants` — SKIPPED

not a recipe-lane dialect; no recipe book to mutate

## 10. `zero_copy_serve_gate` — PASS

counter deltas from the server's own get_flight_info app_metadata

| comparison | actual | expected | ok | code | note |
|---|---|---|:--:|---|---|
| cold: copied_columns | `0` | `0` | ok |  |  |
| warm: copied_columns | `0` | `0` | ok |  |  |
| cold: zero_copy_columns == columns_decoded | `32 vs 32` | `equal` | ok |  |  |
| cold: columns actually decoded | `32` | `> 0` | ok |  | a cold pass that decodes nothing did not exercise the reader |
| warm: miss_rate % | `0.0` | `0.0` | ok |  |  |
| warm: columns_decoded | `0` | `0` | ok |  | stronger than miss_rate: a warm pass must decode nothing at all |

<details><summary>observations</summary>

```json
{
  "cold_delta": {
    "cache_hits": 0,
    "cache_misses": 32,
    "columns_decoded": 32,
    "blocks_assembled": 16,
    "zero_copy_columns": 32,
    "copied_columns": 0,
    "bytes_read": 12001040,
    "bytes_served_cached": 0
  },
  "warm_delta": {
    "cache_hits": 32,
    "cache_misses": 0,
    "columns_decoded": 0,
    "blocks_assembled": 16,
    "zero_copy_columns": 0,
    "copied_columns": 0,
    "bytes_read": 0,
    "bytes_served_cached": 12001040
  },
  "sql": "SELECT status, sum(amount), count(*) FROM perf_1m GROUP BY status",
  "addr": "127.0.0.1:8903",
  "pid": 948795,
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
  "rows": 6
}
```

</details>

## Provenance — what this pack ratifies

```json
{
  "allow_dirty": false,
  "artifact": {
    "bytes": 149806522,
    "note": "artifact is 149806522 bytes, over the 8388608-byte copy limit; the sha256 above is the content address and the bytes are not committed",
    "path": "factory/evidence/artifacts/db2912dfbd6a42337704e121f6872484f2e51eca7439cfe223d4bb72cc73ea4a.manifest.json",
    "produced_at": "/tmp/r64-factory-sweep/clickhouse-meshbench/arrow_out/perf_1m.arrow",
    "sha256": "db2912dfbd6a42337704e121f6872484f2e51eca7439cfe223d4bb72cc73ea4a",
    "storage": "content-addressed manifest",
    "suffix": ".arrow"
  },
  "command": ".venv/bin/python -m factory.conformance --dialect clickhouse --config /home/kos/builds/r64-db-engine/factory/targets/clickhouse-meshbench.yaml --ground-truth /home/kos/builds/r64-db-engine/bench/GROUND-TRUTH-clickhouse.json --table perf_1m --evidence-dir /home/kos/builds/r64-db-engine/factory/evidence --work-dir /tmp/r64-factory-sweep/clickhouse-meshbench --serve-gate",
  "git": {
    "branch": "feat/meshforge-factory",
    "commit": "6655b9026f23568889190ce845586e3dd1235a0d",
    "dirty": false
  },
  "implementation": {
    "distribution_version": "0.1.0",
    "source_files": 46,
    "source_sha256": "0443a9a67f4f2520d910c076a9f14f8f4325a801d740f7ac0a16a34b95f332b8"
  },
  "inputs": {
    "ground_truth": {
      "path": "/home/kos/builds/r64-db-engine/bench/GROUND-TRUTH-clickhouse.json",
      "sha256": "6c7f9331a553dc76890ba3dd19bf888aca50a5a2bfb823333c2ef83c40082a4e"
    },
    "recipe_book": null,
    "schema_spec": {
      "path": "/home/kos/builds/r64-db-engine/factory/specs/meshbench-schema.json",
      "sha256": "93c735e0c6647f7f9c17ca6921146bdd3416ee594aa760fae6700b86add76636"
    },
    "target_config": {
      "path": "/home/kos/builds/r64-db-engine/factory/targets/clickhouse-meshbench.yaml",
      "sha256": "7c504d92bcd95f699f46d0fb3e93374ed87def4277065cfba05a00d022d85db3"
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
    "commit": "6655b9026f23568889190ce845586e3dd1235a0d",
    "branch": "feat/meshforge-factory",
    "dirty": true
  },
  "container": {
    "name": "meshroad-ch",
    "image": "clickhouse/clickhouse-server:latest",
    "image_id": "sha256:07afc18d8a9706eb9d85c5c5d2752e5270f91bbc2894caeaecb73e4d0f603bf5",
    "status": "running"
  }
}
```
