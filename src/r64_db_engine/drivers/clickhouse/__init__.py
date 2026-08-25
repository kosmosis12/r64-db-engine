"""ClickHouse driver package.

Deliberately re-exports nothing. Importing `...clickhouse.descriptor` imports
this package first, and a re-export here would drag `driver.py` in behind it —
undoing, one convenience import at a time, the lazy enumeration the registry
exists to provide. Import `ClickHouseDriver` from `.driver`, or go through
`drivers.resolve()`.
"""
