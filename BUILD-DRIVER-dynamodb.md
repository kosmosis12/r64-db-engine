# BUILD-DRIVER: DynamoDB — r64-db-engine sibling driver

You are building the **DynamoDB driver** for `r64-db-engine` (repo root = source of truth; read the tree before writing anything). This is the third sibling against the Driver ABC after Postgres (v0.1 reference, audit-hardened) and ClickHouse. The commercial driver: Procon Analytics' telematics stack is DynamoDB + Kinesis; this connector materializes their DynamoDB tables as `.ramdb` files for Row64 Server. A flaw here propagates to every future NoSQL sibling — build to reference-grade discipline, not demo-grade.

## Ground rules (non-negotiable)

1. **Read first, write second.** Before any code: read `SPEC.md`, `src/r64_db_engine/core/driver.py` (the ABC), `src/r64_db_engine/drivers/postgres/` and the ClickHouse driver (check whether it lives on `main` or `feat/clickhouse-driver` — mirror whichever is the most hardened committed state), the conformance package (`Gate A` — SourceSpec/FixturePack model + scaffold generator), and `references/coercion.md`.
2. **The firewall is absolute.** Zero changes to `core/` to make this driver work. `core/` must not import from, or name, `dynamodb` anywhere. Use the existing registry/factory indirection (`drivers/__init__.py`, `factory.py` if present). Run the firewall audit before every commit:
   ```bash
   grep -rn "from r64_db_engine.drivers" src/r64_db_engine/core/ tests/core/ && echo "LEAK" || echo "HOLDS"
   grep -rn "import.*drivers" src/r64_db_engine/core/ && echo "LEAK" || echo "HOLDS"
   grep -rn "dynamodb" src/r64_db_engine/core/ && echo "LEAK" || echo "HOLDS"
   ```
3. **One-way only.** Source → ramdb. No writes to DynamoDB ever. No reads from Row64 Server. Output contract is unchanged: atomic tempfile write (`.{target}.ramdb.tmp.{uuid}`, same directory, `os.rename`) into `{loading_dir}/{group}/`.
4. **Never mock `row64tools` in integration tests.** The Postgres audit proved mocked serialization hides real corruption. Integration tests round-trip through real `save_from_df`/`load_to_df`.
5. **Guard, don't corrupt.** The known upstream codec defects apply here with *higher* frequency than Postgres: `row64tools` silently truncates int64 > 2^31 to int32, and decimals round-trip through float64. DynamoDB's `N` type is an **arbitrary-precision decimal string** — overflow and precision-loss are the common case, not the edge case. Reuse the existing `Row64CodecOverflowError` / `NumericPrecisionLossError` guard paths from the Postgres driver. Raise; never write wrong data.
6. **Conventional commits, atomic history, halt at gates.** Commit each phase separately. HALT and report at each gate below rather than plowing through an unresolved semantic question.

## Dependency

Add `boto3` as a driver-scoped dependency (optional extra `[dynamodb]` if the project uses extras; otherwise core deps — match how ClickHouse's client dependency was handled). Use boto3's standard credential chain — never accept raw keys as the only path.

## Architectural deltas vs. the SQL siblings (design decisions, pre-made)

DynamoDB is schemaless and has no SQL. These are the resolved design calls — implement them, don't relitigate them:

### D1 — `discover()` = key schema + bounded sample inference
- `ListTables` (paginated) + `DescribeTable` per configured table → partition key, sort key, item count, billing mode, table status.
- DynamoDB only declares types for **key attributes**. For non-key attributes, run a bounded sample scan (config: `schema_sample_items`, default **1000**, `Limit`-paginated) and infer the schema as the **union of attribute names** with per-attribute type resolution.
- Type-conflict policy across sampled items: if an attribute appears with multiple DynamoDB types, widen to string (JSON-encode non-scalar values) and log a `schema_type_conflict` structured warning naming the attribute and observed types. Deterministic column ordering: key attributes first (partition, then sort), then remaining attributes sorted lexicographically — schema output must be stable across runs.
- Attributes absent from an item are nulls, filled per core NaN rules downstream.
- `validate_table` = table exists, status ACTIVE, configured `incremental_key` (if any) present in the sampled schema.

### D2 — `pull()` = paginated Scan, optional parallel segments
- Full refresh: `Scan` with `LastEvaluatedKey` pagination until exhausted. Config `scan_segments` (default 1, max 32) enables parallel scan via `Segment`/`TotalSegments` with the existing bounded worker pool — do not add a new concurrency primitive.
- Consistency: config `consistent_read` (default `false`; document that it doubles RCU cost).
- Throttling: configure boto3 retries `{"max_attempts": 10, "mode": "adaptive"}`; on `ProvisionedThroughputExceededException` after retries, fail the pull cleanly (state untouched, no partial ramdb) — the atomic-write contract already guarantees this; add a test proving it.
- Page size: config `scan_page_limit` (optional `Limit` per request) for RCU pacing on provisioned tables.

### D3 — Incremental mode = attribute watermark, with honest semantics
- Incremental uses a configured `incremental_key` attribute (must be `N` or `S`-ISO8601-sortable). Two sub-modes:
  - `filter_scan` (default): `Scan` + `FilterExpression: #k > :watermark`. **Document loudly in README and config comments: this reduces transferred rows, NOT consumed RCU — DynamoDB bills the full scan.** It is correctness-incremental, not cost-incremental.
  - `gsi_query`: if config provides `incremental_gsi` (a GSI whose sort key is the incremental attribute) and `incremental_gsi_partition_value`, use `Query` with equality on the GSI partition key plus a strict watermark condition on its sort key. **Erratum:** the original contract omitted DynamoDB's mandatory partition-key equality input. This mode supports only a constant/single-partition time-series GSI; enumerating multi-valued partitions is out of scope. Validate the GSI shape and required value in `validate_table`; never fall back silently to Scan.
- Watermark comparison: strictly `>`, and apply the Postgres PG-003 lesson — prove with a test that a row exactly equal to the stored watermark is neither dropped nor duplicated across two consecutive pulls (numeric `N` comparisons are numeric; `S` comparisons are lexicographic — test both).
- DynamoDB Streams / Kinesis CDC is **explicitly out of scope** for this driver (that is `row64stream` territory). Note it in the driver README section and move on.

### D4 — Coercion table (build `drivers/dynamodb/coercion.py` FIRST, exhaustively tested)

DynamoDB → pandas → ramdb:

| DynamoDB type | Python (boto3) | Target dtype | Rule |
|---|---|---|---|
| `S` | str | object/str | ASCII sanitize per core default |
| `N` | `decimal.Decimal` | int64 **or** float64 | If integral AND within int64: int64; then codec guard raises if > 2^31 boundary per existing overflow guard. If fractional: float64 with exact round-trip check — raise `NumericPrecisionLossError` on loss (consistent with PG-002 policy, honoring any config override the Postgres driver exposes) |
| `BOOL` | bool | bool → per core bool handling (match Postgres bool mapping) |
| `NULL` | None | NaN → core NaN fill rules |
| `B` | bytes | str | hex-encode, exactly matching the Postgres `bytea` mapping. **Erratum:** the original base64 instruction conflicted with its own cross-driver mirroring rule; the tree's established ramdb representation controls. |
| `M` (map) | dict | str | `json.dumps` with sorted keys, Decimal-safe encoder — **JSON, never Python repr** (PG-005 lesson) |
| `L` (list) | list | str | same JSON policy |
| `SS`/`NS`/`BS` (sets) | set | str | JSON **array with deterministic sort order** (sets are unordered; unsorted output breaks round-trip tests). NS members go through the same N numeric policy inside the JSON |

Every row of this table gets a unit test before any driver method is implemented, plus a completeness guard mirroring the Postgres/ClickHouse pattern (test fails if a DynamoDB type exists with no mapping decision).

### D5 — Config block (extend YAML per existing per-driver pattern; core names zero dialects)

```yaml
dialect: dynamodb

dynamodb:
  region: ${AWS_REGION}
  endpoint_url: null            # set to http://localhost:8000 for DynamoDB Local
  # credentials resolve via boto3 chain: env → shared credentials file → IAM role.
  # Optional explicit profile:
  profile: null
  consistent_read: false
  scan_segments: 1
  scan_page_limit: null
  schema_sample_items: 1000

tables:
  - name: vehicle_events
    target: vehicle_events
    mode: incremental
    incremental_key: event_ts        # N (epoch) or ISO8601 S
    incremental_mode: filter_scan    # or gsi_query
    incremental_gsi: null
    incremental_gsi_partition_value: null # required for gsi_query
```

Auth fallback chain analog: explicit `profile` → env (`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`/`AWS_SESSION_TOKEN`) → shared config/credentials files → instance/IAM role. Fail fast with a clear message on `NoCredentialsError` / `UnrecognizedClientException` / `AccessDeniedException` (PG-008 lesson: auth failures must not look like empty tables).

## Local test infrastructure (no AWS account required)

- `scripts/dev_dynamodb.sh`: `docker run -d --name ddb-test -p 8000:8000 amazon/dynamodb-local -jar DynamoDBLocal.jar -sharedDb` + readiness wait. Mirror `dev_postgres.sh` ergonomics (and its known source-poisoning fix — emit env exports, don't kill the shell).
- `scripts/seed_dynamodb.py`: create 3+ tables covering every type in D4 — including: N values straddling 2^31 and 2^63, high-precision decimals, maps/lists nested 2 deep, all three set types, sparse attributes (attribute present in ~50% of items), non-ASCII strings (em-dash), a table with a numeric sort key and one with an S ISO8601 incremental attribute, and a GSI on the incremental attribute for `gsi_query` tests. 50K items on the primary table for throughput parity with the Postgres benchmark.
- Integration tests use testcontainers (or the docker script) against DynamoDB Local with **real row64tools round-trips**. Mark with the existing `--integration` convention.
- Add a `make demo-dynamodb` (or extend `make demo`) matching repo conventions.

## Phase gates (halt and report at each)

- **Gate 0 — Baseline.** `git status` clean; identify the correct base branch (hardened `main` including the Gate A/conformance + factory work; confirm whether ClickHouse is merged — if it is, base includes it). Cut `feat/dynamodb-driver` from that base. Run full existing suite; record the green baseline. HALT if the base is dirty or the suite is red.
- **Gate 1 — Spec + coercion.** Write `references/coercion-dynamodb.md` (or the repo's current per-dialect location) + Gate A SourceSpec for dynamodb via the scaffold generator. Implement `coercion.py` + exhaustive unit tests + completeness guard. If the SourceSpec format cannot express something (schemaless inference, set types), HALT and report the spec strain with a proposed minimal extension — do not fork the format silently (this is the ClickHouse `wrapper_types` lesson).
- **Gate 2 — Driver methods.** `connect` (client construction, auth fail-fast, endpoint_url override), `discover`, `validate_table`, `pull` (full_refresh, both incremental sub-modes, parallel segments). Register in `drivers/__init__.py`. Firewall audit must print HOLDS on all three greps. Empty-stub test: a bare `DynamoDBDriver(Driver)` must have required zero core changes.
- **Gate 3 — Integration.** Seed DynamoDB Local, full round-trip through real row64tools, verify byte-correct via `load_to_df`. Prove: watermark boundary (equal-watermark, both N and S), SIGTERM-mid-write leaves no tmp file, state.db delete + recovery does not duplicate, throttling failure leaves state untouched, sparse-attribute NaN fill, JSON (not repr) for M/L/sets, codec overflow raises on the >2^31 rows.
- **Gate 4 — Docs + handoff.** README `dialect: dynamodb` section with the RCU-honesty paragraph (filter_scan vs gsi_query), config reference, IAM policy snippet (below), and DynamoDB Local quickstart. `ruff` + `mypy` clean. Full suite green including prior drivers. Report a final table: acceptance criteria vs. evidence (test name or command output) — no criterion marked done without a runnable proof.

## IAM policy to document (read-only, table-scoped)

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["dynamodb:ListTables", "dynamodb:DescribeTable",
               "dynamodb:Scan", "dynamodb:Query", "dynamodb:DescribeTimeToLive"],
    "Resource": ["arn:aws:dynamodb:*:*:table/<TableName>",
                 "arn:aws:dynamodb:*:*:table/<TableName>/index/*"]
  }]
}
```
(`ListTables` requires `Resource: "*"` in a separate statement if account-wide listing is wanted; note that in the README.)

## Explicitly out of scope (rebuff scope creep)

- DynamoDB Streams / Kinesis consumption (row64stream's job)
- `ExportTableToPointInTime` → S3 bulk path (viable future enhancement for very large tables; note as a README "future" line, do not build)
- Write-back of any kind; PartiQL query layer; multi-account assume-role orchestration

## Success contract

`feat/dynamodb-driver` branch: all four gates passed with evidence, firewall HOLDS, prior-driver suites untouched and green, conventional-commit atomic history, no uncommitted work left in the tree or stash. The driver has **not** been through the adversarial audit cycle — say so plainly in the final report; reference-grade is declared only after that separate pass.
