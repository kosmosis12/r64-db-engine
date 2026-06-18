"""Scaffold generator.

From a `SourceSpec` it emits an importable driver package:

  <out_dir>/<dialect>_driver/
      __init__.py
      coercion.py   # pandas_dtype_for rebuilt purely from spec.type_map +
                    # normalization rules; coerce_value wired through the
                    # canonical coercers via spec.coercer_map.
      driver.py     # skeleton against the existing Driver ABC: connection mgmt,
                    # type-map wiring, query builder, watermark logic, .ramdb
                    # writer call — all TODO-stubbed except the fidelity surface.
      spec.py       # rebuilds a SourceSpec wired to the generated coercion, so
                    # Gate A can run against the regenerated driver.

The generated `coercion.py` is the crux of the self-regeneration proof: its
`pandas_dtype_for` is derived *only* from the declarative spec. If that derived
mapping diverges from the hand-built driver's on any fixture or type-map entry,
the abstraction has leaked and Gate A fails on the regenerated spec.

No throughput/pushdown code is generated (Gate B).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from r64_db_engine.conformance.spec import SourceSpec


def regenerate(spec: SourceSpec, out_dir: str | Path, fixtures_ref: str) -> Path:
    """Emit the driver package for `spec` into `out_dir`.

    `fixtures_ref` is "module:attr" pointing at the source `SourceSpec` whose
    fixture pack the regenerated spec reuses (the canonical edge-case table is
    the author's shared yardstick — the regenerated driver is proven against it,
    not handed a different one).

    Returns the package directory.
    """
    if not spec.dialect.isidentifier():
        raise ValueError(
            f"spec.dialect must be a valid Python identifier (it names the "
            f"generated package and class); got {spec.dialect!r}"
        )
    if ":" not in fixtures_ref:
        raise ValueError(
            f"fixtures_ref must be in 'module:attr' form; got {fixtures_ref!r}"
        )
    for w in spec.wrapper_types:
        # Wrappers are baked into generated code and matched against the
        # lowercased type string, so each must be a lowercase identifier.
        if not (isinstance(w, str) and w.isidentifier() and w == w.lower()):
            raise ValueError(
                f"wrapper_types entries must be lowercase identifiers; got {w!r}"
            )
    pkg_name = f"{spec.dialect}_driver"
    pkg = Path(out_dir) / pkg_name
    pkg.mkdir(parents=True, exist_ok=True)

    (pkg / "__init__.py").write_text(
        f'"""Generated {spec.dialect} driver scaffold. Do not edit by hand."""\n'
    )
    (pkg / "coercion.py").write_text(_coercion_module(spec))
    (pkg / "driver.py").write_text(_driver_module(spec))
    (pkg / "spec.py").write_text(_spec_module(spec, fixtures_ref))
    return pkg


# ---- emitted modules ---------------------------------------------------


def _coercion_module(spec: SourceSpec) -> str:
    return textwrap.dedent(
        f'''\
        """Generated coercion for {spec.dialect}. pandas_dtype_for is derived
        purely from the declarative type_map; coerce_value dispatches through the
        canonical coercer registry via the declared coercer_map."""

        from __future__ import annotations

        import re
        from typing import Any

        import pandas as pd

        from r64_db_engine.conformance import coercers

        TYPE_MAP: dict[str, str] = {spec.type_map!r}
        COERCER_MAP: dict[str, str] = {dict(spec.coercer_map)!r}
        ARRAY_DTYPE = {spec.array_dtype!r}
        UNKNOWN_DTYPE = {spec.unknown_dtype!r}
        ARRAY_COERCER = {spec.array_coercer!r}
        WRAPPER_TYPES: tuple[str, ...] = {tuple(spec.wrapper_types)!r}


        def _normalize(source_type: str) -> str:
            s = source_type.strip().lower()
            # Unwrap transparent wrappers to their inner type, recursively, before
            # stripping params: nullable(int32) -> int32, and composed wrappers
            # lowcardinality(nullable(string)) -> string. No-op when WRAPPER_TYPES
            # is empty (e.g. postgres), so non-wrapper sources are unchanged.
            changed = True
            while changed:
                changed = False
                for w in WRAPPER_TYPES:
                    if s.startswith(w + "(") and s.endswith(")"):
                        s = s[len(w) + 1:-1].strip()
                        changed = True
            s = re.sub(r"\\s*\\([^)]*\\)", "", s)
            s = re.sub(r"\\s+", " ", s).strip()
            return s


        def pandas_dtype_for(source_type: str) -> str:
            norm = _normalize(source_type)
            if norm.endswith("[]"):
                return ARRAY_DTYPE
            return TYPE_MAP.get(norm, UNKNOWN_DTYPE)


        def coerce_value(value: Any, source_type: str) -> Any:
            if value is None:
                return None
            if isinstance(value, float) and pd.isna(value):
                return None
            norm = _normalize(source_type)
            if norm.endswith("[]"):
                fn = coercers.REGISTRY.get(ARRAY_COERCER)
                if fn is None:
                    raise ValueError(
                        f"array coercer {{ARRAY_COERCER!r}} not in coercers.REGISTRY"
                    )
                return fn(value)
            key = COERCER_MAP.get(norm)
            if key is None:
                return str(value)
            fn = coercers.REGISTRY.get(key)
            if fn is None:
                raise ValueError(
                    f"coercer {{key!r}} (for {{source_type!r}}) not in coercers.REGISTRY"
                )
            return fn(value)
        '''
    )


def _driver_module(spec: SourceSpec) -> str:
    cls = "".join(p.capitalize() for p in spec.dialect.split("_")) + "Driver"
    return textwrap.dedent(
        f'''\
        """Generated {spec.dialect} driver skeleton against the Driver ABC.

        Connection/discovery/query/watermark bodies are TODO stubs — the
        generator wires the *fidelity surface* (type map + value coercion) and
        leaves the source-specific I/O for the driver author to fill in.
        """

        from __future__ import annotations

        from typing import Any

        from r64_db_engine.core.driver import (
            Driver,
            PullResult,
            TableMetadata,
            ValidationResult,
        )
        from r64_db_engine.core.coercion import apply_coercion
        from r64_db_engine.core.ramdb_writer import RamdbWriter
        from {spec.dialect}_driver import coercion as _coercion

        # Native cursor types valid as an incremental watermark (from the spec).
        WATERMARK_CURSOR_TYPES = {tuple(spec.watermark.cursor_types)!r}
        WATERMARK_MONOTONIC = {spec.watermark.monotonic!r}


        class {cls}(Driver):
            @classmethod
            def dialect_name(cls) -> str:
                return {spec.dialect!r}

            async def connect(self, config: dict[str, Any]) -> None:
                # TODO: establish a connection pool for {spec.dialect}.
                raise NotImplementedError("connect: fill in {spec.dialect} connection mgmt")

            async def close(self) -> None:
                # TODO: close the pool.
                raise NotImplementedError("close")

            async def discover(self, schema_filter: str | None = None) -> list[TableMetadata]:
                # TODO: list tables; mark columns whose type is in
                # WATERMARK_CURSOR_TYPES as candidate_incremental_keys.
                raise NotImplementedError("discover")

            async def validate_table(self, table_config: dict[str, Any]) -> ValidationResult:
                # TODO: validate source/incremental_key against the live schema.
                raise NotImplementedError("validate_table")

            async def pull(
                self, table_config: dict[str, Any], previous_watermark: str | int | None
            ) -> PullResult:
                # Pipeline (fill in the I/O):
                #   1. build query (full_refresh vs incremental WHERE cursor > wm)
                #   2. fetch rows
                #   3. df = frame(rows); pre-coerce object columns via coerce_value
                #   4. df = apply_coercion(df, {{col: pandas_dtype_for(type)}})
                #   5. RamdbWriter(loading_dir, group).write(df, target)
                #   6. compute new watermark from the max cursor value
                raise NotImplementedError("pull")

            def coerce_value(self, value: Any, source_type: str) -> Any:
                return _coercion.coerce_value(value, source_type)


        # Silence unused-import lint on the wired-but-stubbed helpers.
        _ = (apply_coercion, RamdbWriter, _coercion.pandas_dtype_for)
        '''
    )


def _spec_module(spec: SourceSpec, fixtures_ref: str) -> str:
    module, _, attr = fixtures_ref.partition(":")
    return textwrap.dedent(
        f'''\
        """Generated SourceSpec for {spec.dialect}, wired to the generated
        coercion. Reuses the author's canonical fixture pack as the shared Gate A
        yardstick."""

        from __future__ import annotations

        import importlib

        from r64_db_engine.conformance.spec import (
            PushdownStub,
            SourceSpec,
            WatermarkSpec,
        )
        from {spec.dialect}_driver import coercion as _coercion

        _src = getattr(importlib.import_module({module!r}), {attr!r})

        SPEC = SourceSpec(
            dialect={spec.dialect!r},
            type_map=dict(_coercion.TYPE_MAP),
            widths={dict(spec.widths)!r},
            watermark=WatermarkSpec(
                cursor_types={tuple(spec.watermark.cursor_types)!r},
                monotonic={spec.watermark.monotonic!r},
            ),
            fixture_pack=_src.fixture_pack,
            array_dtype={spec.array_dtype!r},
            unknown_dtype={spec.unknown_dtype!r},
            coercer_map=dict(_coercion.COERCER_MAP),
            array_coercer={spec.array_coercer!r},
            coerce_value=_coercion.coerce_value,
            pandas_dtype_for=_coercion.pandas_dtype_for,
            pushdown=PushdownStub(notes={spec.pushdown.notes!r}),
        )
        '''
    )


__all__ = ["regenerate"]
