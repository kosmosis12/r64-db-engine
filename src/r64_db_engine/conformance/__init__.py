"""Source-agnostic conformance contract for r64-db-engine drivers.

Gate A (fidelity). This package abstracts the per-value fidelity guarantees
that were proven for the Postgres reference driver into a contract every
future sibling driver (ClickHouse, BigQuery, Snowflake, Redshift,
Databricks) signs by supplying a `SourceSpec` + fixture pack.

Layout:
  - `coercers`  — canonical, source-agnostic scalar coercers (the registry a
                  generated driver wires its type map onto).
  - `spec`      — the source-capability spec format (type_map, widths,
                  watermark, fixture_pack, pushdown stub) + fixture dataclasses.
  - `contract`  — the five Gate A assertion classes + a runner that produces a
                  per-class go/no-go report.
  - `generator` — emits a driver skeleton + Gate A fixture instantiation from a
                  spec, and can regenerate an importable driver for the
                  self-regeneration proof.

Gate B (throughput/perf) is intentionally NOT here — separate session.
"""

from __future__ import annotations
