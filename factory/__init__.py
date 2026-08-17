"""MESHFORGE factory — the machine-checkable admission path for drivers.

This package is deliberately OUTSIDE `src/r64_db_engine/`. It is tooling that
*judges* the engine, not part of the engine, and keeping it out of the
installed package is what makes the Gate F1 assertion

    git grep -rnE "(^|[^_])[Ff]actory" src/r64_db_engine/core/

meaningful: core cannot import its own oracle, so the oracle cannot be
weakened by the code it is grading. `python -m factory.conformance` resolves
this package from the repo root via CWD.

Law 4 (autonomy is bounded by verification): a driver is admitted only through
`factory.conformance`. If the battery cannot check a property, the factory
cannot ship it — extend the battery first.
"""

from __future__ import annotations

__all__: list[str] = []
