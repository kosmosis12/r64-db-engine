"""`python -m factory.conformance` — the oracle. Exit code is the verdict.

    .venv/bin/python -m factory.conformance \\
      --dialect clickhouse \\
      --config factory/targets/clickhouse-meshbench.yaml \\
      --ground-truth bench/GROUND-TRUTH-clickhouse.json \\
      --table perf_1m --evidence-dir factory/evidence

# What this module is and is not

It GATHERS. Every verdict is reached by a pure judge in `factory/battery.py`,
which sees only the facts collected here. The split is what makes the oracle
provably able to fail: a judge with no I/O can be handed a corrupted
`Observation` in a unit test, with no container and no network, and shown to
return FAIL. `tests/factory/test_battery.py` does exactly that, once per check.

# Three files, three jobs

- `--config` is a NORMAL ENGINE CONFIG. It is what an operator would run, and
  it validates through `core.config.Config` untouched.
- `--ground-truth` holds expectations captured AT THE SOURCE. A pipeline
  checked against its own output proves only that it is self-consistent.
- the spec (`factory/specs/<dataset>-schema.json`) holds what the battery needs
  and the engine config cannot carry: the schema, the RF-002 discriminators,
  the B-2 boundary columns, the aggregate map, the serve-gate workload.

The spec path defaults by convention — `factory/specs/<dataset>-schema.json`,
where `<dataset>` is the target's filename stem with its `<dialect>-` prefix
removed, so `targets/clickhouse-meshbench.yaml` resolves to
`specs/meshbench-schema.json`. `--spec` overrides it. A missing default is
refused loudly, naming both the convention and the flag.

# Two pulls, always

The checksum check needs two consecutive same-lane pulls, so every run takes
two. Facts for the data checks come from pull 1; pull 2 is compared against it.
The serve gate, when requested, runs against the file as it stands after pull 2
— which is the same file when the checksum check passes, and when it does not,
the pack says so and the gate result is still about a real artifact.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from factory import battery, evidence, probes, serve_gate
from factory.battery import CheckResult, Observation

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_yaml(path: Path) -> dict[str, Any]:
    import yaml

    doc = yaml.safe_load(path.read_text())
    if not isinstance(doc, dict):
        raise SystemExit(f"{path}: expected a YAML mapping, got {type(doc).__name__}")
    return doc


def default_spec_path(config_path: Path, dialect: str) -> Path:
    stem = config_path.stem
    prefix = f"{dialect}-"
    dataset = stem[len(prefix):] if stem.startswith(prefix) else stem
    return REPO_ROOT / "factory" / "specs" / f"{dataset}-schema.json"


def resolve_spec(config_path: Path, dialect: str, explicit: Path | None) -> dict[str, Any]:
    path = explicit or default_spec_path(config_path, dialect)
    if not path.exists():
        raise SystemExit(
            f"conformance spec not found: {path}\n"
            f"By convention the spec for target '{config_path.name}' is "
            f"factory/specs/<dataset>-schema.json, where <dataset> is the target stem with "
            f"its '{dialect}-' prefix removed. Pass --spec to point somewhere else.\n"
            f"The spec is not optional: it carries the schema, the RF-002 discriminators and "
            f"the B-2 boundary columns, and the battery cannot gate on what it is not told."
        )
    return json.loads(path.read_text())


# ---------------------------------------------------------------------------
# Artifact facts
# ---------------------------------------------------------------------------


def _canon_timestamp(value: Any) -> str:
    """Render a timestamp bound the way the source renders it, so both compare.

    ClickHouse's `toString(min(event_time))` on a DateTime64(6) yields
    `2026-01-01 00:00:15.184566`. A pyarrow scalar comes back as a datetime.
    Both are normalized here, in the GATHERING layer, so the judge compares two
    already-canonical strings and stays free of source-specific formatting.
    """
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S.%f")
    return str(value)


def _decoded(column: Any) -> Any:
    """A dictionary column's decoded values; anything else unchanged."""
    import pyarrow as pa

    if pa.types.is_dictionary(column.type):
        return column.combine_chunks().dictionary_decode()
    return column


def compute_aggregates(table: Any, specs: list[dict[str, Any]]) -> dict[str, Any]:
    """Evaluate the spec's declared aggregate ops against the artifact.

    A small, closed, declarative vocabulary. No expression evaluation and no
    model anywhere near it: the spec is DATA, this function is the only code,
    and an unrecognized op is refused rather than guessed at.
    """
    import numpy as np
    import pyarrow.compute as pc

    out: dict[str, Any] = {}
    for spec in specs:
        op = spec["op"]
        key = spec["ground_truth_key"]
        column = spec.get("column")

        # All three loop variables are bound as defaults, not captured. A
        # closure over the loop variable would report whichever spec entry
        # happened to be last when it fired — naming the wrong entry in an
        # error whose entire job is to say which entry is wrong.
        def need_column(gt_key: str = key, op_name: str = op, name: Any = column) -> Any:
            """The column this op operates on, or a loud refusal.

            Every op except `row_count` needs one. Resolving it through a
            guard rather than an Optional keeps a spec that omits `column`
            from failing later as an opaque `NoneType has no attribute
            null_count` — which says nothing about which spec entry is wrong.
            """
            if not name:
                raise SystemExit(
                    f"spec entry for {gt_key!r} uses op {op_name!r}, which requires a "
                    f"'column', but none is declared."
                )
            return table.column(name)

        if op == "row_count":
            out[key] = table.num_rows
        elif op == "nunique":
            out[key] = int(pc.count_distinct(_decoded(need_column())).as_py())
        elif op == "count_equals":
            out[key] = int(pc.sum(pc.equal(_decoded(need_column()), spec["value"])).as_py() or 0)
        elif op == "sum_int":
            out[key] = int(pc.sum(need_column()).as_py())
        elif op == "max_int":
            out[key] = int(pc.max(need_column()).as_py())
        elif op == "null_count":
            out[key] = int(need_column().null_count)
        elif op == "null_pct":
            out[key] = 100.0 * need_column().null_count / table.num_rows if table.num_rows else 0.0
        elif op == "scaled_sum_exact_int":
            # THE AUTHORITY. Round each value to an integer number of minor
            # units first, then sum in int64. Integer addition is associative,
            # so this is order-independent and reproducible across any scan
            # order the source happens to use.
            values = np.asarray(need_column().to_numpy(zero_copy_only=False), dtype="float64")
            out[key] = int(np.rint(values[~np.isnan(values)] * spec["scale"]).astype("int64").sum())
        elif op == "scaled_sum_float":
            # Corroborating only. Sums in float64 and scales at the end, which
            # is the order-SENSITIVE form the ground-truth file records
            # alongside the exact-int one.
            values = np.asarray(need_column().to_numpy(zero_copy_only=False), dtype="float64")
            out[key] = int(round(float(np.nansum(values)) * spec["scale"]))
        else:
            raise SystemExit(
                f"spec declares unknown aggregate op {op!r} for {key!r}. "
                f"The op vocabulary is closed by design — add the op to "
                f"factory/conformance.compute_aggregates rather than making the spec "
                f"expressive enough to say anything."
            )
    return out


def read_artifact(path: Path, spec: dict[str, Any]) -> dict[str, Any]:
    """Everything the judges need from one artifact file."""
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.ipc as ipc

    reader = ipc.open_file(pa.memory_map(str(path)))
    table = reader.read_all()

    null_counts = {name: int(table.column(name).null_count) for name in table.column_names}

    # NaN smuggled in as a VALUE is a different failure from a null and must be
    # counted separately: a literal NaN sets null_count = 0 while poisoning
    # every downstream sum(). `pc.is_nan` yields null for null entries and
    # `pc.sum` skips nulls, so this counts real NaN values only.
    nan_counts: dict[str, int] = {}
    for name in table.column_names:
        col = table.column(name)
        if pa.types.is_floating(col.type):
            nan_counts[name] = int(pc.sum(pc.is_nan(col)).as_py() or 0)
        else:
            nan_counts[name] = 0

    bounds: dict[str, tuple[Any, Any]] = {}
    for name in spec.get("boundary_columns", []):
        if name in table.column_names:
            col = table.column(name)
            bounds[name] = (
                _canon_timestamp(pc.min(col).as_py()),
                _canon_timestamp(pc.max(col).as_py()),
            )

    return {
        "path": path,
        "row_count": table.num_rows,
        "schema": {f.name: str(f.type) for f in table.schema},
        "block_rows": [reader.get_batch(i).num_rows for i in range(reader.num_record_batches)],
        "null_counts": null_counts,
        "nan_value_counts": nan_counts,
        "aggregates": compute_aggregates(table, spec.get("aggregates", [])),
        "column_bounds": bounds,
        "sha256": _sha256(path),
        "bytes": path.stat().st_size,
    }


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Pulling
# ---------------------------------------------------------------------------


def recipe_security_outcomes(
    doc: dict[str, Any], dialect: str
) -> list[tuple[str, bool, str]] | None:
    """Apply destination-pinning mutations to the ACTUAL shipped recipe book.

    Returns `(mutation, refused, message)` per attempt, or None when the target
    is not a recipe-lane dialect.

    Mutating the shipped book rather than a fixture is the point. A fence that
    is correct in `security.py` but never wired into the loader would pass
    every unit test and admit every malicious book; this runs the mutations
    through the real load path, against the real hosts this target ships.
    """
    if dialect != "rest":
        return None
    book_path = (doc.get("rest") or {}).get("recipe_book")
    if not book_path:
        return None

    import yaml

    from r64_db_engine.drivers.rest.recipes import parse_book
    from r64_db_engine.drivers.rest.security import (
        assert_host_allowed,
        assert_public_host,
        confine_next_url,
        host_of,
    )

    path = Path(book_path)
    if not path.is_absolute():
        path = REPO_ROOT / path
    raw = yaml.safe_load(path.read_text())

    def attempt(label: str, fn: Any) -> tuple[str, bool, str]:
        """Refused == passed. Any exception is a refusal; returning is not."""
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - the refusal is the pass condition
            return (label, True, f"{type(exc).__name__}: {exc}")
        return (label, False, "accepted without error")

    def mutated(fn: Any) -> dict[str, Any]:
        book = json.loads(json.dumps(raw))
        fn(book)
        return book

    outcomes: list[tuple[str, bool, str]] = []

    # 1. https -> http. A downgrade must be refused at LOAD, not at call time.
    outcomes.append(
        attempt(
            "https->http downgrade in recipe[0].url",
            lambda: parse_book(
                mutated(lambda b: b["recipes"][0].__setitem__(
                    "url", b["recipes"][0]["url"].replace("https://", "http://", 1)
                ))
            ),
        )
    )

    # 2. The literal evil-<host> case, against every host this book actually
    #    pins. `endswith("checkr.com")` is TRUE for "evil-checkr.com", so a
    #    suffix-matching implementation passes here and a boundary-matching one
    #    does not.
    for recipe in raw["recipes"]:
        allowed = host_of(recipe["url"])
        lookalike = f"evil-{allowed}"
        outcomes.append(
            attempt(
                f"lookalike host {lookalike} against pinned {allowed}",
                lambda a=allowed, lk=lookalike: assert_host_allowed(f"https://{lk}/v1/x", a),
            )
        )

    # 3. A URL template placeholder — a destination an input could steer.
    outcomes.append(
        attempt(
            "templated url (host/path substitution)",
            lambda: parse_book(
                mutated(lambda b: b["recipes"][0].__setitem__(
                    "url", b["recipes"][0]["url"] + "/{path}"
                ))
            ),
        )
    )

    # 4. A threading input the target recipe does not declare.
    outcomes.append(
        attempt(
            "undeclared threading input",
            lambda: parse_book(
                mutated(lambda b: b["threading"][-1].setdefault("params", {}).__setitem__(
                    "not_a_declared_param", 1
                ))
            ),
        )
    )

    # 5. Private address space — the SSRF fence, checked on RESOLUTION rather
    #    than on spelling.
    outcomes.append(
        attempt("loopback destination (SSRF)", lambda: assert_public_host("https://localhost/"))
    )

    # 6. Pagination steering, against the SHIPPED book's own pinned URLs. These
    #    are statically checkable — `confine_next_url` is a pure function of the
    #    crafted Link value and the recipe's pinned URL — so they belong at
    #    battery level rather than only in unit tests.
    for recipe in raw["recipes"]:
        pinned = recipe["url"]
        host = host_of(pinned)
        for label, crafted in (
            ("cross-path next-URL", f"https://{host}/v1/../admin?page=2"),
            ("undeclared path next-URL", f"https://{host}/definitely-not-the-pinned-path?p=2"),
            ("subdomain next-URL", f"https://attacker.{host}/x?p=2"),
        ):
            outcomes.append(
                attempt(
                    f"{label} against pinned {pinned}",
                    lambda c=crafted, u=pinned: confine_next_url(c, u, []),
                )
            )

    # 7. `allowed_next_paths` OMITTED vs EXPLICITLY EMPTY must behave
    #    identically — both refuse. A default-deny rule that only denies when
    #    someone remembered to write `[]` is not default-deny.
    #
    #    The distinction is made at the BOOK level, where it actually lives: two
    #    variants are parsed through the real loader and the confinement is run
    #    with whatever each produced. Passing `[]` twice at the call site would
    #    have tested nothing about how an omitted key is loaded.
    first_url = raw["recipes"][0]["url"]
    elsewhere = f"https://{host_of(first_url)}/somewhere-else"
    for label, book_mutation in (
        ("OMITTED", lambda b: b["recipes"][0].__setitem__(
            "pagination", {"type": "link-header"})),
        ("EXPLICITLY EMPTY", lambda b: b["recipes"][0].__setitem__(
            "pagination", {"type": "link-header", "allowed_next_paths": []})),
    ):
        parsed = parse_book(mutated(book_mutation))
        declared = parsed.recipes[raw["recipes"][0]["name"]].pagination.allowed_next_paths
        outcomes.append(
            attempt(
                f"cross-path next-URL with allowed_next_paths {label}",
                lambda d=declared: confine_next_url(elsewhere, first_url, d),
            )
        )

    # No "the shipped book still loads" control is recorded here. A loader that
    # refused EVERYTHING would score a perfect result on the mutations above,
    # so that control matters — but it has already been supplied, and more
    # strongly, by the run itself: this check executes after two successful
    # pulls of the real book, so if it did not load, every preceding check
    # would already have failed. Recording it again as a pseudo-mutation would
    # render as "REFUSED / REFUSED" in the pack and read as its own opposite.
    return outcomes


def build_config(doc: dict[str, Any]) -> Any:
    from r64_db_engine.core.config import Config

    return Config.model_validate(doc)


def run_pull(doc: dict[str, Any], target: str) -> Path:
    from r64_db_engine.core.daemon import build_daemon

    daemon = build_daemon(build_config(doc))
    asyncio.run(daemon.run(once=True))
    return daemon.writer.target_path(target)


def apply_work_dir(doc: dict[str, Any], work_dir: Path | None) -> dict[str, Any]:
    """Point the sink, state and loading dirs at a run-scoped directory.

    Without `--work-dir` the target's own paths are used as written; the
    directories are created either way, because `ArrowIpcSink.ensure_ready`
    refuses a missing `output_dir` rather than creating it (a typo'd path
    should fail, not quietly materialize).
    """
    doc = json.loads(json.dumps(doc))  # deep copy; the doc is plain JSON-able data
    if work_dir is not None:
        doc.setdefault("sink", {})["output_dir"] = str(work_dir / "arrow_out")
        doc.setdefault("row64", {})["loading_dir"] = str(work_dir / "loading")
        doc.setdefault("runtime", {})["state_dir"] = str(work_dir / "state")
    for path in (
        doc.get("sink", {}).get("output_dir"),
        doc.get("row64", {}).get("loading_dir"),
        doc.get("runtime", {}).get("state_dir"),
    ):
        if path:
            Path(path).mkdir(parents=True, exist_ok=True)
    return doc


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def run(args: argparse.Namespace, argv: list[str] | None = None) -> int:
    # BEFORE any work: a pack must ratify a commit whose content actually ran.
    # Checked first so a dirty tree costs a second, not two million-row pulls.
    try:
        git_facts = evidence.assert_clean_tree(allow_dirty=args.allow_dirty)
    except evidence.DirtyTreeError as exc:
        # A refusal, not a crash. Rendered like every other loud refusal in
        # this tool — message plus non-zero exit — rather than as a traceback
        # that reads like a bug in the battery.
        raise SystemExit(str(exc)) from exc

    config_path = Path(args.config).resolve()
    doc = load_yaml(config_path)
    declared = doc.get("dialect")
    if args.dialect and declared != args.dialect:
        raise SystemExit(
            f"--dialect {args.dialect!r} does not match the target's own dialect "
            f"{declared!r} in {config_path}. Refusing to run a battery whose "
            f"label disagrees with what it is about to pull."
        )
    if not isinstance(declared, str) or not declared:
        raise SystemExit(f"{config_path}: `dialect` is missing or not a string")
    # Narrowed to `str` here, once, so everything downstream — the registry
    # check, the probe lookup, the evidence pack — takes the concrete type its
    # signature declares instead of an Optional threaded through twelve calls.
    dialect: str = declared

    spec_path = Path(args.spec).resolve() if args.spec else default_spec_path(config_path, dialect)
    spec = resolve_spec(config_path, dialect, Path(args.spec).resolve() if args.spec else None)
    # A recipe-lane target names its book by path; a database target has none.
    # Recorded either way so the pack states which inputs existed rather than
    # leaving a reader to infer it from an absent key.
    raw_book = (doc.get(dialect) or {}).get("recipe_book") if isinstance(doc.get(dialect), dict) else None
    recipe_book_path: Path | None = None
    if raw_book:
        recipe_book_path = Path(raw_book)
        if not recipe_book_path.is_absolute():
            recipe_book_path = REPO_ROOT / recipe_book_path
    ground_truth_doc = json.loads(Path(args.ground_truth).read_text())
    if args.table not in ground_truth_doc.get("tables", {}):
        raise SystemExit(
            f"--table {args.table!r} is not in {args.ground_truth} "
            f"(has: {sorted(ground_truth_doc.get('tables', {}))})"
        )
    ground_truth = ground_truth_doc["tables"][args.table]

    # The six pinned inputs, resolved once. Enforced BEFORE any pull: an input
    # living inside the evidence tree would inherit the dirty-file exemption and
    # could be swapped between runs while every pack still reported
    # ratifies_head: true.
    pinned_inputs: dict[str, Path | None] = {
        "target_config": config_path,
        "recipe_book": recipe_book_path,
        "schema_spec": spec_path,
        "ground_truth": Path(args.ground_truth).resolve(),
    }
    try:
        evidence.assert_inputs_outside_evidence(pinned_inputs, REPO_ROOT)
    except evidence.LaunderedInputError as exc:
        raise SystemExit(str(exc)) from exc

    # Secret files the book declares, recorded by reference only.
    declared_env_files: list[str] = []
    if recipe_book_path and recipe_book_path.exists():
        import yaml as _yaml

        book_doc = _yaml.safe_load(recipe_book_path.read_text()) or {}
        for recipe in book_doc.get("recipes", []):
            env_file = (recipe.get("auth") or {}).get("env_file")
            if env_file:
                declared_env_files.append(env_file)

    work_dir = Path(args.work_dir).resolve() if args.work_dir else None
    doc = apply_work_dir(doc, work_dir)

    table_entries = [t for t in doc.get("tables", []) if t.get("target") == args.table]
    if not table_entries:
        raise SystemExit(
            f"--table {args.table!r} matches no table target in {config_path} "
            f"(targets: {[t.get('target') for t in doc.get('tables', [])]})"
        )
    source = table_entries[0]["source"]

    checks: list[CheckResult] = []

    # --- 1. Registry admission -------------------------------------------
    from r64_db_engine.drivers import DRIVERS
    from r64_db_engine.drivers import resolve as resolve_driver

    def validate_with_dialect(name: str) -> Any:
        mutated = json.loads(json.dumps(doc))
        mutated["dialect"] = name
        return build_config(mutated)

    checks.append(
        battery.check_registry_admission(
            dialect=dialect,
            registered=sorted(DRIVERS),
            resolve_fn=resolve_driver,
            validate_config_fn=validate_with_dialect,
        )
    )

    # --- pulls ------------------------------------------------------------
    print(f"[conformance] pull 1/2: {source} -> arrow_ipc", file=sys.stderr)
    artifact_path = run_pull(doc, args.table)
    first = read_artifact(artifact_path, spec)

    print(f"[conformance] pull 2/2: {source} (checksum determinism)", file=sys.stderr)
    run_pull(doc, args.table)
    second = read_artifact(artifact_path, spec)

    obs = Observation(
        dialect=dialect,
        table=args.table,
        source=source,
        row_count=first["row_count"],
        schema=first["schema"],
        block_rows=first["block_rows"],
        null_counts=first["null_counts"],
        nan_value_counts=first["nan_value_counts"],
        aggregates=first["aggregates"],
        column_bounds=first["column_bounds"],
        artifact_sha256=first["sha256"],
        artifact_bytes=first["bytes"],
        second_sha256=second["sha256"],
        second_row_count=second["row_count"],
        second_schema=second["schema"],
        second_block_rows=second["block_rows"],
        second_aggregates=second["aggregates"],
    )

    # --- 2. Schema exactness ----------------------------------------------
    checks.append(
        battery.check_schema_exactness(
            obs.schema,
            spec.get("columns", []),
            string_width_tolerant=bool(spec.get("string_width_tolerant", True)),
        )
    )

    # --- 3. Aggregate parity ----------------------------------------------
    checks.append(
        battery.check_aggregate_parity(obs.aggregates, ground_truth, spec.get("aggregates", []))
    )

    # --- 4. RF-002 ---------------------------------------------------------
    checks.append(
        battery.check_rf002_discriminator(
            row_count=obs.row_count,
            null_counts=obs.null_counts,
            nan_value_counts=obs.nan_value_counts,
            ground_truth=ground_truth,
            spec=spec,
            table=args.table,
        )
    )

    # --- 5. B-2 boundary ---------------------------------------------------
    probe_endpoint = ""
    try:
        probe = probes.resolve(dialect)(doc.get(dialect, {}))
        probe_endpoint = probe.describe()
        obs.source_timezone = probe.session_timezone()
        for column in spec.get("boundary_columns", []):
            obs.source_bounds[column] = probe.bounds(source, column)
        obs.source_queries = probe.queries()
        b2 = battery.check_b2_boundary(
            obs.column_bounds,
            obs.source_bounds,
            spec.get("boundary_columns", []),
            source_timezone=obs.source_timezone,
            queries=obs.source_queries,
        )
    except probes.ProbeError as exc:
        b2 = CheckResult(
            name="b2_boundary",
            status=battery.FAIL,
            detail=(
                f"the live source could not be probed, so the boundary assertion could not be "
                f"made: {exc}. This is a FAIL and not a SKIP — an unprobed boundary is exactly "
                f"the gap B-2 exists to close."
            ),
        )
    checks.append(b2)

    # --- 6. PG-011 refusal -------------------------------------------------
    def build_incremental() -> Any:
        from r64_db_engine.core.daemon import build_daemon

        mutated = json.loads(json.dumps(doc))
        for entry in mutated["tables"]:
            if entry.get("target") == args.table:
                entry["mode"] = "incremental"
                entry.setdefault("incremental_key", "row_id")
                entry.setdefault("incremental_type", "int")
        return build_daemon(build_config(mutated))

    checks.append(battery.check_pg011_refusal(build_incremental))

    # --- 7. Block structure ------------------------------------------------
    checks.append(battery.check_block_structure(obs.block_rows, obs.row_count))

    # --- 8. Checksum -------------------------------------------------------
    checks.append(
        battery.check_checksum(
            obs.artifact_sha256,
            obs.second_sha256,
            first_rows=obs.row_count,
            second_rows=obs.second_row_count,
            first_schema=obs.schema,
            second_schema=obs.second_schema,
            first_blocks=obs.block_rows,
            second_blocks=obs.second_block_rows,
            first_aggregates=obs.aggregates,
            second_aggregates=obs.second_aggregates,
        )
    )

    # --- 9. Recipe-lane security invariants --------------------------------
    checks.append(battery.check_recipe_security(recipe_security_outcomes(doc, dialect)))

    # --- 10. Zero-copy serve gate (optional) -------------------------------
    if args.serve_gate:
        sql_template = spec.get("serve_gate_sql")
        if not sql_template:
            checks.append(
                CheckResult(
                    name="zero_copy_serve_gate",
                    status=battery.FAIL,
                    detail="--serve-gate was requested but the spec declares no serve_gate_sql",
                )
            )
        else:
            sql = sql_template.format(table=args.table)
            gate_dir = artifact_path.parent
            try:
                measured = serve_gate.measure(
                    artifact_path,
                    args.table,
                    sql,
                    addr=args.serve_addr,
                    binary=args.meshroad_binary,
                    pidfile=gate_dir / "factory-serve.pid",
                    log_path=gate_dir / "factory-serve.log",
                )
                result = battery.check_serve_gate(measured["cold"], measured["warm"])
                result.observations.update(
                    {"sql": sql, "addr": measured["addr"], "pid": measured["pid"],
                     "baseline": measured["baseline"], "rows": measured["cold_rows"]}
                )
                checks.append(result)
            except Exception as exc:  # noqa: BLE001
                # Deliberately broad. The evidence pack is the artifact (Law 2),
                # so a gate that blows up must still produce a pack that SAYS it
                # blew up — aborting the run here would destroy the record of
                # the eight checks that already ran. Narrowing this to
                # ServeGateError once cost exactly that: a Flight transport
                # error escaped and took the whole pack with it.
                checks.append(
                    CheckResult(
                        name="zero_copy_serve_gate",
                        status=battery.FAIL,
                        detail=f"serve gate could not be run: {type(exc).__name__}: {exc}",
                    )
                )
    else:
        checks.append(battery.check_serve_gate(None, None))

    # --- evidence pack -----------------------------------------------------
    # Content-address the artifact FIRST. A corrupted store entry must refuse
    # before the pack exists, so the affirmative content-address claim is never
    # assembled around bytes that failed verification.
    try:
        artifact_record = evidence.record_artifact(
            artifact_path, Path(args.evidence_dir).resolve(), repair_store=args.repair_store
        )
    except evidence.CorruptStoredEvidenceError as exc:
        raise SystemExit(str(exc)) from exc

    pack = evidence.build_pack(
        dialect=dialect,
        table=args.table,
        source=source,
        checks=checks,
        artifact={
            "path": str(artifact_path),
            "sha256_pull1": obs.artifact_sha256,
            "sha256_pull2": obs.second_sha256,
            "bytes": obs.artifact_bytes,
            "rows": obs.row_count,
            "blocks": len(obs.block_rows),
        },
        invocation={
            "config": str(config_path),
            "ground_truth": str(Path(args.ground_truth).resolve()),
            "spec": str(spec_path),
            "serve_gate": bool(args.serve_gate),
            "source_endpoint": probe_endpoint,
            "source_timezone": obs.source_timezone,
            "note": (
                "data checks read pull 1; the serve gate, when run, reads the file as it "
                "stands after pull 2 (identical when the checksum check passes)"
            ),
        },
        container=ground_truth_doc.get("source", {}).get("container"),
        provenance={
            "git": git_facts,
            "allow_dirty": bool(args.allow_dirty),
            # The exact command, so the run is reproducible from the pack alone
            # rather than from whatever the reader assumes was typed.
            "command": " ".join(
                [".venv/bin/python", "-m", "factory.conformance", *(argv or [])]
            ),
            # The DECLARED inputs this run read from disk, pinned by content.
            # Not "every input the run consumed" — that was overclaimed: the
            # live source, the secret contents and the runtime beyond the
            # lockfiles are outside what a pack can pin, and the CLOSURE
            # BOUNDARY section says so explicitly rather than leaving a reader
            # to infer it from an absence.
            "inputs": evidence.digest_inputs(pinned_inputs),
            "implementation": evidence.implementation_digest(),
            "toolchain": evidence.toolchain_pins(
                REPO_ROOT, args.meshroad_binary if args.serve_gate else None
            ),
            "proxy_environment": evidence.proxy_environment(),
            # Paths and metadata only — never a digest of the contents. See
            # `evidence.secret_references` and the CLOSURE BOUNDARY section.
            "secret_references": evidence.secret_references(declared_env_files),
            "artifact": artifact_record,
            "closure_boundary": evidence.CLOSURE_BOUNDARY,
        },
    )

    json_path, md_path = evidence.write_pack(pack, Path(args.evidence_dir).resolve(), date=args.date)

    print("", file=sys.stderr)
    for check in checks:
        print(f"  {check.status:8s} {check.name}", file=sys.stderr)
    tally = pack.tally
    print("", file=sys.stderr)
    print(
        f"[conformance] VERDICT {pack.verdict_line} — {tally['PASS']} passed, "
        f"{tally['FAIL']} failed, {tally['SKIPPED']} skipped",
        file=sys.stderr,
    )
    if pack.allow_dirty:
        print(
            "[conformance] ALLOW-DIRTY: this pack ratifies NO commit — the tree did not "
            "match HEAD. Do not use it to admit a driver.",
            file=sys.stderr,
        )
    print(f"[conformance] evidence: {json_path}", file=sys.stderr)
    print(f"[conformance] evidence: {md_path}", file=sys.stderr)

    return 0 if pack.verdict == battery.PASS else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m factory.conformance",
        description="MESHFORGE conformance battery. Exit code 0 only if every non-skipped check passed.",
    )
    p.add_argument("--dialect", required=True)
    p.add_argument("--config", required=True, help="engine config document for the target")
    p.add_argument("--ground-truth", required=True, help="source-captured expectations JSON")
    p.add_argument("--table", required=True, help="table target; also the ground-truth table key")
    p.add_argument("--evidence-dir", default=str(REPO_ROOT / "factory" / "evidence"))
    p.add_argument("--spec", default=None, help="override the conventional spec path")
    p.add_argument("--work-dir", default=None, help="run-scoped dir for artifacts/state")
    p.add_argument("--date", default=None, help="YYYYMMDD stamp for the evidence filenames")
    p.add_argument("--serve-gate", action="store_true", help="run the zero-copy serve gate")
    p.add_argument(
        "--allow-dirty",
        action="store_true",
        help=(
            "emit a pack from a dirty tree. The pack is stamped ALLOW-DIRTY in its verdict "
            "line and header and explicitly ratifies NO commit — for local iteration only."
        ),
    )
    p.add_argument(
        "--repair-store",
        action="store_true",
        help=(
            "re-copy a corrupted content-addressed store entry from the freshly-hashed "
            "produced artifact, recording the repair and both hashes in the pack. Without "
            "this, corrupted stored evidence is a hard refusal."
        ),
    )
    p.add_argument("--serve-addr", default=serve_gate.DEFAULT_ADDR)
    p.add_argument("--meshroad-binary", default=serve_gate.DEFAULT_BINARY)
    return p


def main(argv: list[str] | None = None) -> int:
    effective = list(argv) if argv is not None else sys.argv[1:]
    return run(build_parser().parse_args(effective), effective)


if __name__ == "__main__":
    raise SystemExit(main())
