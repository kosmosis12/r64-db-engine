<!-- GENERATED FILE — DO NOT EDIT.
     Emitted by factory/generate_descriptor_artifacts.py from the driver's
     descriptor(). Edit the descriptor in
     src/r64_db_engine/drivers/<dialect>/descriptor.py and regenerate:
         python -m factory.generate_descriptor_artifacts
     Hand edits here are overwritten and are how per-source prose went
     stale in the first place. -->

# Connectors

One page per registered driver, generated from that driver's `descriptor()`.
This directory replaces the hand-written per-source prose that used to live in SKILL.md and drift against the code.

| connector | dialect | auth | conformance |
|---|---|---|---|
| [ClickHouse](clickhouse.md) | `clickhouse` | `password` | drifted — repair brief open |
| [PostgreSQL](postgres.md) | `postgres` | `password` | declared, pending conformance |
| [REST (recipe lane)](rest.md) | `rest` | `none` | conformance-passing |

`declared, pending conformance` means exactly what it says: the driver describes its shape, and no evidence pack proves that shape against a real source yet. It is a distinct state from passing and from drifted, and it is never rendered as green.
