# EVIDENCE — clickhouse / perf_1m

**VERDICT: PASS** — 9 passed, 0 failed, 0 skipped. Generated 2026-08-17T02:41:33Z.

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
| artifact.path | `/tmp/r64-factory/clickhouse-meshbench/arrow_out/perf_1m.arrow` |
| artifact.sha256_pull1 | `db2912dfbd6a42337704e121f6872484f2e51eca7439cfe223d4bb72cc73ea4a` |
| artifact.sha256_pull2 | `db2912dfbd6a42337704e121f6872484f2e51eca7439cfe223d4bb72cc73ea4a` |
| artifact.bytes | `149806522` |
| artifact.rows | `1000000` |
| artifact.blocks | `16` |

## Summary

| # | check | verdict | detail |
|---|---|---|---|
| 1 | `registry_admission` | PASS | dialect 'clickhouse' against registry ['clickhouse', 'postgres'] |
| 2 | `schema_exactness` | PASS | 14 columns, string-width tolerance ON (B-3) |
| 3 | `aggregate_parity` | PASS | 12 aggregates vs ground truth |
| 4 | `rf002_null_discriminator` | PASS | 1 declared discriminator(s) on perf_1m: score |
| 5 | `b2_boundary` | PASS | boundary columns ['event_time'] vs live source (source session timezone: UTC) |
| 6 | `pg011_refusal` | PASS | refused with: sink 'arrow_ipc' cannot serve incremental mode (its output format is not appendable in place), but these tables request it: perf_1m. Use mode: full_refresh. |
| 7 | `block_structure` | PASS | 16 blocks expected for 1000000 rows at 65536/block |
| 8 | `checksum` | PASS | two consecutive same-lane pulls are byte-identical (sha256 db2912dfbd6a4233…) |
| 9 | `zero_copy_serve_gate` | PASS | counter deltas from the server's own get_flight_info app_metadata |

## 1. `registry_admission` — PASS

dialect 'clickhouse' against registry ['clickhouse', 'postgres']

| comparison | actual | expected | ok | note |
|---|---|---|:--:|---|
| resolved driver dialect_name() | `clickhouse` | `clickhouse` | ok |  |
| dialect present in registry listing | `["clickhouse", "postgres"]` | `contains 'clickhouse'` | ok |  |
| drivers.resolve: refuses unregistered dialect | `raised` | `raised` | ok |  |
| drivers.resolve: refusal lists registered dialects | `["clickhouse", "postgres"]` | `["clickhouse", "postgres"]` | ok | message: unknown dialect 'definitely-not-a-registered-dialect' (available: clickhouse, postgres) |
| Config validation: refuses unregistered dialect | `raised` | `raised` | ok |  |
| Config validation: refusal lists registered dialects | `["clickhouse", "postgres"]` | `["clickhouse", "postgres"]` | ok | message: 1 validation error for Config   Value error, unknown dialect 'definitely-not-a-registered-dialect' (registered: clickhouse, postgres) [type=value_error, input_value={'dialect': 'definitely-n...: 0, 'metrics_port': 0}}, input_type=dict]     For further information visit https://errors.pyd... |

## 2. `schema_exactness` — PASS

14 columns, string-width tolerance ON (B-3)

| comparison | actual | expected | ok | note |
|---|---|---|:--:|---|
| column count | `14` | `14` | ok |  |
| column names and order | `["row_id", "account_id", "user_id", "region", "city", "category", "segment", "product_name", "status", "amount", "quantity", "price", "score", "event_time"]` | `["row_id", "account_id", "user_id", "region", "city", "category", "segment", "product_name", "status", "amount", "quantity", "price", "score", "event_time"]` | ok |  |
| type[row_id] | `int64` | `int64` | ok |  |
| type[account_id] | `int64` | `int64` | ok |  |
| type[user_id] | `int64` | `int64` | ok |  |
| type[region] | `large_string` | `large_string` | ok |  |
| type[city] | `large_string` | `large_string` | ok |  |
| type[category] | `large_string` | `large_string` | ok |  |
| type[segment] | `large_string` | `large_string` | ok |  |
| type[product_name] | `large_string` | `large_string` | ok |  |
| type[status] | `dictionary<values=string, indices=int32, ordered=0>` | `dictionary<values=string, indices=int32, ordered=0>` | ok |  |
| type[amount] | `double` | `double` | ok |  |
| type[quantity] | `int64` | `int64` | ok |  |
| type[price] | `double` | `double` | ok |  |
| type[score] | `double` | `double` | ok |  |
| type[event_time] | `timestamp[us]` | `timestamp[us]` | ok |  |

## 3. `aggregate_parity` — PASS

12 aggregates vs ground truth

| comparison | actual | expected | ok | note |
|---|---|---|:--:|---|
| count | `1000000` | `1000000` | ok |  |
| uniq_status | `6` | `6` | ok |  |
| uniq_region | `8` | `8` | ok |  |
| uniq_product_name | `50000` | `50000` | ok |  |
| count_status_active | `166627` | `166627` | ok |  |
| count_region_west | `124935` | `124935` | ok |  |
| sum_quantity | `250480021` | `250480021` | ok |  |
| max_account_id | `3599999873` | `3599999873` | ok |  |
| scaled_amount_sum_exact_int | `11994337292` | `11994337292` | ok |  |
| scaled_amount_sum | `11994337292` | `11994337292` | ok | corroborating only — does not gate (float-order sensitivity) |
| count_score_null | `20039` | `20039` | ok |  |
| pct_score_null | `2.0039` | `2.0039` | ok |  |

## 4. `rf002_null_discriminator` — PASS

1 declared discriminator(s) on perf_1m: score

| comparison | actual | expected | ok | note |
|---|---|---|:--:|---|
| score: spec vs ground-truth null count | `20039` | `20039` | ok | two independent records of the same number; disagreement means one of the two files is stale |
| score: artifact null_count | `20039` | `20039` | ok |  |
| score: count(col) vs count(*) | `979961 vs 1000000` | `must differ` | ok | if equal, nulls were filled in transit |
| score: NaN smuggled as a value | `0` | `0` | ok | a literal NaN sets null_count=0 and poisons every downstream sum() |

## 5. `b2_boundary` — PASS

boundary columns ['event_time'] vs live source (source session timezone: UTC)

| comparison | actual | expected | ok | note |
|---|---|---|:--:|---|
| event_time: min | `2026-01-01 00:00:15.184566` | `2026-01-01 00:00:15.184566` | ok |  |
| event_time: max | `2026-06-29 23:59:30.942340` | `2026-06-29 23:59:30.942340` | ok |  |

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

| comparison | actual | expected | ok | note |
|---|---|---|:--:|---|
| incremental on non-appendable sink | `raised` | `raised` | ok |  |
| refusal names the cause | `sink 'arrow_ipc' cannot serve incremental mode (its output format is not appendable in place), but these tables request it: perf_1m. Use mode: full_refresh.` | `cannot serve incremental mode` | ok |  |

## 7. `block_structure` — PASS

16 blocks expected for 1000000 rows at 65536/block

| comparison | actual | expected | ok | note |
|---|---|---|:--:|---|
| block count | `16` | `16` | ok |  |
| rows across blocks | `1000000` | `1000000` | ok |  |
| block layout | `[65536, 65536, 65536, 65536, 65536, 65536, 65536, 65536, 65536, 65536, 65536, 65536, 65536, 65536, 65536, 16960]` | `[65536, 65536, 65536, 65536, 65536, 65536, 65536, 65536, 65536, 65536, 65536, 65536, 65536, 65536, 65536, 16960]` | ok | 65536-row blocks, final block carries the remainder |

## 8. `checksum` — PASS

two consecutive same-lane pulls are byte-identical (sha256 db2912dfbd6a4233…)

| comparison | actual | expected | ok | note |
|---|---|---|:--:|---|
| sha256 (pull 1 vs pull 2) | `db2912dfbd6a42337704e121f6872484f2e51eca7439cfe223d4bb72cc73ea4a` | `db2912dfbd6a42337704e121f6872484f2e51eca7439cfe223d4bb72cc73ea4a` | ok |  |

<details><summary>observations</summary>

```json
{
  "lane_scope": "byte-identity asserted WITHIN this lane only; cross-lane comparison uses data + schema-minus-metadata + block structure, with string-width tolerance"
}
```

</details>

## 9. `zero_copy_serve_gate` — PASS

counter deltas from the server's own get_flight_info app_metadata

| comparison | actual | expected | ok | note |
|---|---|---|:--:|---|
| cold: copied_columns | `0` | `0` | ok |  |
| warm: copied_columns | `0` | `0` | ok |  |
| cold: zero_copy_columns == columns_decoded | `32 vs 32` | `equal` | ok |  |
| cold: columns actually decoded | `32` | `> 0` | ok | a cold pass that decodes nothing did not exercise the reader |
| warm: miss_rate % | `0.0` | `0.0` | ok |  |
| warm: columns_decoded | `0` | `0` | ok | stronger than miss_rate: a warm pass must decode nothing at all |

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
  "pid": 188451,
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
    "row64tools": "<no __version__>",
    "jsonschema": "<not installed>",
    "httpx": "<not installed>"
  },
  "git": {
    "commit": "7820d1827e77b7a6d6f0200676698099b74fd06e",
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
