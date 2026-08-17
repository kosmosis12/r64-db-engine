"""The `rest` dialect — the recipe lane's driver.

See `driver.py` for the config shape and `recipes.py` for the book schema.
"""

from __future__ import annotations

from r64_db_engine.drivers.rest.driver import RestDriver

__all__ = ["RestDriver"]
