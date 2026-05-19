# Adding a Driver

> **Status:** Draft. Migrating from README.

## Contract

Every driver implements the `Driver` ABC in `core/`. The contract is intentionally minimal: connect, discover schema, pull rows (with optional watermark), close.

## Reference implementation

`drivers/postgres/driver.py` is the v0.1 reference. Mirror its structure when adding:

- ClickHouse
- Redshift
- BigQuery
- Snowflake
- Databricks
