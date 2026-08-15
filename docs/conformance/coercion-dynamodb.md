# DynamoDB coercion fidelity

This document fixes the DynamoDB client-value to pandas/ramdb representation
used by the driver and its Gate A conformance fixtures. Values described here
are the Python objects produced by boto3's `TypeDeserializer`; no network or
live table is involved in Gate A.

## Conformance boundary

The DynamoDB SourceSpec uses a synthetic fixed fixture schema containing every
native value type below. DynamoDB's real schema inference is discovery behavior,
not coercion fidelity: bounded sampling, sparse-attribute union, conflict
widening, and key-first deterministic ordering are tested with the driver in
Gates 2 and 3. They are deliberately absent from the shared SourceSpec format.

## Mapping

| Native type | boto3 output | Target dtype | Canonical representation |
| --- | --- | --- | --- |
| `S` | `str` | `string` | String; column-level ASCII sanitization follows the core default. |
| `N` integral | `Decimal` | `int64` | Signed-int64 value; the existing ramdb signed-int32 writer guard still applies. |
| `N` fractional | `Decimal` | `float64` | Only when `Decimal(str(float(value))) == value`; otherwise raise `NumericPrecisionLossError`. |
| `BOOL` | `bool` | `bool` | Boolean, with core null filling. |
| `NULL` | `None` | inferred column dtype | Preserve `None` for core sentinel handling. |
| `B` | `bytes` | `string` | Lowercase hexadecimal, matching Postgres `bytea`. |
| `M` | `dict` | `string` | Compact JSON with sorted keys and Decimal-safe recursive conversion. |
| `L` | `list` | `string` | Compact JSON with Decimal-safe recursive conversion. |
| `SS` | `set[str]` | `string` | Deterministically sorted compact JSON array. |
| `NS` | `set[Decimal]` | `string` | Deterministically sorted compact JSON array; members use the `N` policy. |
| `BS` | `set[bytes]` | `string` | Deterministically sorted JSON array of lowercase hexadecimal strings. |

`N` is value-sensitive. The SourceSpec declares a static `float64` fallback for
schema-only contexts and a canonical `decimal_numeric` dtype resolver for
fixture/row contexts. Integral values outside signed int64 raise
`Row64CodecOverflowError`; fractional values that cannot survive float64 raise
`NumericPrecisionLossError`. Values in the signed-int64 but outside the
row64tools signed-int32 storage lane are rejected by `RamdbWriter` before a
file is written.

## Determinism and safety

- Maps always use JSON key sorting; Python `repr` is never an output format.
- Sets are converted to arrays and sorted by canonical compact JSON encoding.
- Nested maps, lists, sets, binary values, and Decimals use the same recursive
  rules as top-level values.
- Non-finite numeric values and precision-losing decimals are rejected.
- Binary values use hex. This resolves an erratum in the original build
  contract, whose base64 sentence contradicted its instruction to mirror the
  established Postgres `bytea` representation.

## Completeness surface

The supported native-type set is exactly:

```text
S N BOOL NULL B M L SS NS BS
```

The driver test suite compares this set with both the dtype map and coercer map.
Adding or removing a mapping decision without updating the completeness test is
therefore a hard failure.
