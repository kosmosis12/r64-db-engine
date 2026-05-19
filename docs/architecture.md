# Architecture

> **Status:** Draft. Full Producer/Driver/Consumer model documentation migrating from README.

## Components

- **Driver ABC** (`core/`) — abstract base class defining the contract every database driver implements
- **Postgres Driver** (`drivers/postgres/`) — v0.1 reference implementation
- **Producer** — pulls rows from source, applies type coercion, hands off to consumer
- **Consumer** — serializes rows to `.ramdb` binary format, atomic temp-then-rename into loading directory

## Data flow

Source DB → Driver (read + coerce) → Producer (batch) → Consumer (.ramdb writer) → Row64 Server loading dir

## Adding drivers

See [adding-a-driver.md](adding-a-driver.md).
