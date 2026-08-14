# Configuration Reference

> **Status:** Draft. Full YAML schema documentation migrating from README. See `examples/production.yaml` for the canonical annotated template.

## Schema

See [`examples/production.yaml`](../examples/production.yaml) for an exhaustive annotated example covering every supported field.

## Selecting a source dialect

`dialect:` names the driver, and the top-level block **named after the dialect**
carries that driver's connection options:

```yaml
dialect: postgres
postgres:
  host: db.internal
  database: analytics
```

`dialect` is a free-form string validated against the driver registry
(`src/r64_db_engine/drivers/__init__.py`) — it is not a closed enumeration in
the config schema. Naming a dialect with no registered driver is refused at
**config time**, not later at connection time, with the registered names
listed:

```
unknown dialect 'postgres9' (registered: clickhouse, postgres)
```

The permitted top-level keys are exactly the **declared config fields** plus a
block named after a **registered dialect**. Both halves are enforced, so a
misspelled `telemtry:` and an unregistered `dynamodb:` are equally errors
rather than silently-ignored sections.

Core validates the `postgres:` and `clickhouse:` blocks against typed models
(`sslmode` values, ports, timeouts). Any other dialect's block is passed to
`Driver.connect()` opaquely and validated by the driver itself, so
driver-specific keys need no core support:

```yaml
dialect: dynamodb
dynamodb:
  region: us-east-1
  scan_segments: 4          # refused by the driver outside 1..32
  consistent_read: true
```

> The DynamoDB driver itself is not on `main` yet — it merges at Gate C, and
> until then this config is **refused**, because `dynamodb` is not a registered
> dialect. That is the intended answer: the config layer no longer stands in
> the way (which is what blocked the merge), but it will not pretend a driver
> exists before it does. Registering the driver is the only step needed to make
> this config valid — no edit to `core/config.py` accompanies it.

> Before this, `dialect` was `Literal["postgres", "clickhouse"]` (PG-010), so a
> driver that was complete and tested still could not be named in a config
> file. Its integration tests had to declare `dialect: postgres` with a dummy
> `database:` to get past validation.

**One block only.** Unknown top-level keys are still refused, so a misspelled
`telemtry:` is an error rather than a silently ignored section. The single
exception is the block matching the selected dialect.

### `profile:` is a different thing

Top-level `profile:` selects a *connection profile* — a named deployment shape
(e.g. `supabase`) validated against the profile registry, which may normalize or
refuse the dialect block. A `profile` key **inside** a dialect block is just
another driver option: `dynamodb.profile` is an AWS profile name and never
reaches the profile registry.

## Examples

| File | Purpose |
|---|---|
| `examples/minimal.yaml` | Smallest viable config — dev container |
| `examples/production.yaml` | Env-driven template, every feature exercised |
| `examples/incremental.yaml` | Watermark-keyed pulls |
| `examples/cachyos-demo.yaml` | Real Row64 Server install (cachyos-kos) |
| `examples/cachyos-demo-utf8.yaml` | Non-ASCII regression test |
