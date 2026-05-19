# r64-db-engine — Database Ingestion for Row64

[badges]

Driver-agnostic database ingestion engine. Pulls from external sources (Postgres in v0.1; ClickHouse, Redshift, BigQuery, Snowflake, Databricks as future siblings against the same Driver ABC) and lands typed .ramdb files for Row64 Server to consume.

**v0.1 status:** Postgres driver, end-to-end ingest verified against real Row64 Server install.

## Quick start

```bash
git clone ...
cd r64-db-engine
make dev
make demo   # ephemeral postgres + 50K rows → .ramdb in /tmp/r64-demo
```

## What you get

- One YAML config per pipeline, every field documented in [docs/config-reference.md](docs/config-reference.md)
- Full-refresh + incremental (watermark) modes per table
- Daemon mode with `/health` endpoint, SIGTERM graceful shutdown, atomic temp-then-rename writes
- Type coercion for every Postgres type tested (see [docs/architecture.md](docs/architecture.md))

## Where to next

| If you want to... | Read |
|---|---|
| Run against your own Postgres | [docs/quickstart.md](docs/quickstart.md) |
| Deploy to real Row64 Server | [docs/operator/row64-server-deployment.md](docs/operator/row64-server-deployment.md) |
| Understand the architecture | [docs/architecture.md](docs/architecture.md) |
| Add a new driver | [docs/adding-a-driver.md](docs/adding-a-driver.md) |
| Hit a snag | [docs/operator/troubleshooting.md](docs/operator/troubleshooting.md) |

## License

MIT
