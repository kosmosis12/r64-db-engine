# Configuration Reference

> **Status:** Draft. Full YAML schema documentation migrating from README. See `examples/production.yaml` for the canonical annotated template.

## Schema

See [`examples/production.yaml`](../examples/production.yaml) for an exhaustive annotated example covering every supported field.

## Examples

| File | Purpose |
|---|---|
| `examples/minimal.yaml` | Smallest viable config — dev container |
| `examples/production.yaml` | Env-driven template, every feature exercised |
| `examples/incremental.yaml` | Watermark-keyed pulls |
| `examples/cachyos-demo.yaml` | Real Row64 Server install (cachyos-kos) |
| `examples/cachyos-demo-utf8.yaml` | Non-ASCII regression test |

## DynamoDB incremental modes

`filter_scan` applies `incremental_key > watermark` as a Scan filter. It reduces
rows transferred to the engine but does **not** reduce consumed read capacity:
DynamoDB charges for every item evaluated by the scan.

`gsi_query` is the cost-incremental option for a single-partition time-series
GSI. Configure `incremental_gsi`, its sort-key attribute as `incremental_key`,
and the scalar `incremental_gsi_partition_value`. The driver resolves the GSI
partition-key name and its `S`/`N` type from `DescribeTable`, then queries:

```text
#gsi_pk = :partition_value AND #incremental_key > :watermark
```

The partition value is mandatory; validation fails rather than falling back to
a Scan. GSIs partitioned by many fleet, dealer, or tenant values require
partition enumeration and are out of scope. Because GSIs are eventually
consistent, the watermark advances only to the maximum incremental value
actually returned, never wall-clock time; lagging writes can then appear on a
later pull.
