# Gate A — pg Self-Regeneration Proof (Task 5)

## What was proven

The hand-built Postgres driver and a Postgres driver **regenerated from
`POSTGRES_SPEC`** are run through the *same* abstracted Gate A suite and diffed.
The generated driver's `pandas_dtype_for` is derived **only** from the
declarative `type_map` + normalization rules, and its `coerce_value` is wired
through the canonical coercer registry via the declared `coercer_map`. If the
spec failed to fully capture the hand-built fidelity surface, the regenerated
mapping would drift and Gate A would fail.

## How to reproduce

```bash
pip install -e ".[dev]"
pytest tests/conformance -v          # hand-built + regenerated, no Docker needed
```

or, ad hoc:

```python
from r64_db_engine.drivers.postgres.spec import POSTGRES_SPEC
from r64_db_engine.conformance.generator import regenerate
from r64_db_engine.conformance.contract import run_gate_a
# regenerate(POSTGRES_SPEC, out_dir, "r64_db_engine.drivers.postgres.spec:POSTGRES_SPEC")
```

## Result — go/no-go per assertion class

```
HAND-BUILT pg                       REGENERATED pg
  TYPE_MAP  PASS  18 case(s)          TYPE_MAP  PASS  18 case(s)
  WIDTH     PASS  18 case(s)          WIDTH     PASS  18 case(s)
  NULL      PASS  18 case(s)          NULL      PASS  18 case(s)
  TZ        PASS  18 case(s)          TZ        PASS  18 case(s)
  RAMDB     PASS  18 case(s)          RAMDB     PASS  18 case(s)
```

Finer-grained diffs (stronger than the class report):

- **Per-fixture behavioral diff** (dtype + coerce-outcome on all 18 fixtures):
  `no leaks — regenerated == hand-built`.
- **Full type_map diff** (every one of the ~80 declared native types, not just
  the fixtures): `no leaks — identical mapping`.

## Go/no-go per assertion class

| Class | Hand-built | Regenerated | Diverged? |
|---|---|---|---|
| TYPE_MAP (native type → row64 dtype) | PASS | PASS | no |
| WIDTH (int32 codec lane, both stages + numeric precision) | PASS | PASS | no |
| NULL (None passthrough + sentinel fills) | PASS | PASS | no |
| TZ (tz-aware → UTC-naive, date/time/timestamp) | PASS | PASS | no |
| RAMDB (write → load_to_df → value-equal) | PASS | PASS | no |

## Leak found?

**None.** Regenerated-pg is behaviorally identical to hand-built-pg on every
fidelity assertion, every fixture, and every declared type. The abstraction did
not leak.

## Founding template verification

The int32 codec-width case is exercised at **both** lanes from the fixture pack,
with no live database:

- `bigint_over_int32` — value `3_548_933_426` coerces fine, then
  `RamdbWriter.write` raises `Row64CodecOverflowError` (the **write** lane).
- `interval_over_int32` — `timedelta(seconds=3548, microseconds=933426)` →
  `3_548_933_426` µs, rejected by the coercer with `Row64CodecOverflowError`
  (the **coerce** lane).
- `numeric_precision_loss` — a 35-significant-digit `Decimal` rejected with
  `NumericPrecisionLossError`.

This is the integration-only `test_int64_source_values_above_signed_int32`
assertion pulled down to the fixture level, exactly as Task 1 anticipated.

## Verdict

**Fan-out is CLEARED to proceed.** The fidelity contract is source-agnostic,
the spec format fully captures pg's fidelity surface, the generator reproduces
that surface, and the regenerated driver passes Gate A with zero divergence.
Sibling drivers (ClickHouse, BigQuery, Snowflake, Redshift, Databricks) can be
scaffolded from their own specs and held to the same contract.

> Scope note: this is Gate A (fidelity) only. Gate B (throughput/perf) remains a
> separate session, gated on this one — which has now succeeded.

## Post-bootstrap: coercion fork collapsed

The bootstrap left one soft spot: the canonical registry (`conformance/coercers.py`)
*mirrored* pg's hand-built value coercers rather than *being* them, so regenerated-pg
and hand-built-pg agreed as two faithful implementations. That fork is now
collapsed — `drivers/postgres/coercion.py` dispatches `coerce_value` **through**
the canonical registry and retains no private value logic (only the pg type ->
coercer-key map, `PG_COERCER_MAP`). Hand-built and regenerated pg are now one
implementation instantiated twice. The original pg coercion tests pass unchanged
against pg-through-registry, and the regeneration proof above still shows zero
divergence.
