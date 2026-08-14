#!/usr/bin/env bash
# Build the meshbench ClickHouse benchmark dataset. Idempotent.
#
# DETERMINISM IS A PROPERTY OF THIS SCRIPT, NOT A HAPPY ACCIDENT: every column
# is derived from cityHash64(number, k) with a distinct k per column, and there
# is ZERO rand()/now()/generateUUIDv4(). The tables therefore regenerate
# byte-identically on any machine, any ClickHouse build, in any order. That is
# what lets bench/GROUND-TRUTH-clickhouse.json be committed as a fixed
# expectation rather than re-measured each run.
#
# Hash-index assignment (do not renumber - it would change every value):
#   k=1 account_id   k=2 user_id     k=3 region      k=4 city
#   k=5 category     k=6 segment     k=7 product_name k=8 status
#   k=9 amount       k=10 quantity   k=11 price      k=12 score-null-mask
#   k=13 score-value k=14 event_time-seconds         k=15 event_time-micros
#
# Types are chosen to survive the driver's coercion table: all integers are
# Int64 because UInt64 maps to "unsupported" and raises (it cannot round-trip
# into a signed ramdb int without an overflow risk). `number` from numbers()
# is UInt64 and is implicitly narrowed by the Int64 column declaration.
set -euo pipefail

CONTAINER="${CONTAINER:-meshroad-ch}"
ch() { docker exec -i "$CONTAINER" clickhouse-client "$@"; }

# The generator body. Identical for both tables except the numbers() bound,
# so 1M is a strict row-prefix of 10M.
generator_sql() {
    local table="$1" rows="$2"
    cat <<SQL
INSERT INTO meshbench.${table}
SELECT
    number,
    2000000000 + cityHash64(number, 1) % 1600000000,
    cityHash64(number, 2) % 500000,
    ['North','South','East','West','Central','Northeast','Southwest','Pacific'][(cityHash64(number, 3) % 8) + 1],
    concat('city_', toString(cityHash64(number, 4) % 5000)),
    ['electronics','apparel','grocery','home','sports','toys','auto','office','beauty','garden'][(cityHash64(number, 5) % 10) + 1],
    ['consumer','smb','enterprise','government'][(cityHash64(number, 6) % 4) + 1],
    concat('product_', toString(cityHash64(number, 7) % 50000)),
    ['active','inactive','pending','suspended','closed','trial'][(cityHash64(number, 8) % 6) + 1],
    round(-log(((cityHash64(number, 9) % 9999999) + 1) / 10000000.) * 120.0, 2),
    (cityHash64(number, 10) % 500) + 1,
    round(((cityHash64(number, 11) % 99000) + 1000) / 100.0, 2),
    if(cityHash64(number, 12) % 50 = 0, NULL, round((cityHash64(number, 13) % 1000000) / 10000.0, 4)),
    toDateTime64('2026-01-01 00:00:00', 6) + toIntervalSecond(cityHash64(number, 14) % 15552000) + toIntervalMicrosecond(cityHash64(number, 15) % 1000000)
FROM numbers(${rows});
SQL
}

echo "[dataset] schema"
ch --multiquery <<'SQL'
CREATE DATABASE IF NOT EXISTS meshbench;

CREATE TABLE IF NOT EXISTS meshbench.perf_1m
(
    row_id Int64, account_id Int64, user_id Int64,
    region String, city String, category String, segment String, product_name String,
    status LowCardinality(String),
    amount Float64, quantity Int64, price Float64, score Nullable(Float64),
    event_time DateTime64(6)
) ENGINE = MergeTree ORDER BY row_id;

CREATE TABLE IF NOT EXISTS meshbench.perf_10m AS meshbench.perf_1m;
SQL

# Idempotence: MergeTree does not dedupe, so a blind re-INSERT would double the
# table. Only (re)populate when the row count is not already exactly right.
for pair in "perf_1m:1000000" "perf_10m:10000000"; do
    table="${pair%%:*}"; want="${pair##*:}"
    have="$(ch --query "SELECT count() FROM meshbench.${table}")"
    if [[ "$have" == "$want" ]]; then
        echo "[dataset] ${table}: ${have} rows already correct, skipping"
        continue
    fi
    echo "[dataset] ${table}: have ${have}, want ${want} -> truncate + regenerate"
    ch --query "TRUNCATE TABLE meshbench.${table}"
    generator_sql "$table" "$want" | ch --multiquery
    echo "[dataset] ${table}: now $(ch --query "SELECT count() FROM meshbench.${table}") rows"
done

echo "[dataset] done"
