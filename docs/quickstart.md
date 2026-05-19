# Quickstart

10-minute path from clone to first `.ramdb` produced.

> **Status:** Draft. Full content migrating from README. See [`make demo`](../Makefile) for current quickstart path.

## Prerequisites

- Docker (for ephemeral Postgres)
- Python 3.11+
- `make`

## Steps

```bash
git clone <repo-url>
cd r64-db-engine
make dev      # creates .venv, installs deps
make demo     # ephemeral postgres + 50K rows → .ramdb in /tmp/r64-demo
```

Expected output: `[demo] success — produced files: Customers.ramdb` (~5MB).
