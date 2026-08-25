"""The `rest` dialect — the recipe lane's driver.

See `driver.py` for the config shape and `recipes.py` for the book schema.

Deliberately re-exports nothing, for the reason the ClickHouse package
docstring gives: a convenience re-export here would import `driver.py` whenever
anything touches `...rest.descriptor`, and the descriptor sweep is supposed to
be free. Import `RestDriver` from `.driver`, or go through `drivers.resolve()`.
"""
