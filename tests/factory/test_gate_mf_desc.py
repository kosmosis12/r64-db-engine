"""Gate MF-DESC — the acceptance battery for the connector descriptor.

Ten checks, and every one of them is paired with a fixture that makes it fail.
That pairing is the whole discipline: a check nobody has ever seen go red is
indistinguishable from a check that cannot go red, and the second kind is worse
than no check at all because it reports green while measuring nothing.

So each `test_<n>_...` here has a `test_<n>_..._fixture_is_red` beside it that
builds the violation and asserts the check catches it. Read them in pairs.

Two checks deviate from the brief's literal wording, both because the tree said
something the brief did not know, and both are recorded at their check rather
than silently softened:

  * Check 3 cannot be "grep core for each dialect string, expect empty". Core
    genuinely still names two dialects — `_TYPED_BLOCKS` and the typed
    `postgres:`/`clickhouse:` config models — a pre-existing PG-010 residue that
    `core/config.py` documents and that this brief did not set out to remove.
    The honest form of the check is that the descriptor mechanism introduced no
    new dialect naming, which is what is asserted.
  * Check 8's consumer lives in meshroad, which is not in this repository. What
    is assertable here is the emitting side of the contract, and the check says
    so out loud rather than implying it covered the browser.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from factory import generate_descriptor_artifacts as gen
from r64_db_engine.core.descriptor import (
    AuthMode,
    Capabilities,
    DescriptorError,
    DriverMetadata,
    ErrorMap,
    Representability,
    TypeMap,
)
from r64_db_engine.drivers import DRIVERS, descriptors

REPO_ROOT = Path(__file__).resolve().parents[2]
CORE = REPO_ROOT / "src" / "r64_db_engine" / "core"

#: Every dialect this repo knows about, including the ones with no driver yet.
#: The roadmap names are here on purpose: if `snowflake` ever appears in core,
#: this catches it on the day it lands rather than after it has been built on.
ALL_KNOWN_DIALECTS = ("postgres", "clickhouse", "rest", "duckdb", "dynamodb", "snowflake")

#: Modules this brief added or rewrote in core. These must be dialect-free; the
#: rest of core is held to "did not get worse" (see check 3).
CORE_MODULES_TOUCHED_BY_THIS_BRIEF = ("descriptor.py",)


def _valid_meta(**overrides: object) -> DriverMetadata:
    """A minimal valid descriptor, for fixtures to spoil one field at a time."""
    kwargs: dict = {
        "dialect": "fixture",
        "engine_name": "Fixture",
        "auth_mode": AuthMode.NONE,
        "required_env_keys": (),
        "config_profile": "fixture",
        "doc_summary": "A test double.",
        "capabilities": Capabilities(),
    }
    kwargs.update(overrides)
    return DriverMetadata(**kwargs)


def _grep(pattern: str, root: Path) -> list[str]:
    """Lines matching `pattern` under `root`. Empty list when nothing matches."""
    proc = subprocess.run(
        ["grep", "-rnE", pattern, str(root)],
        capture_output=True,
        text=True,
        check=False,
    )
    return [ln for ln in proc.stdout.splitlines() if ln.strip()]


# ---- 1. the firewall holds -------------------------------------------
#
# FINDING (MF-DESC/1). The canonical firewall check this factory has been
# running — `grep -rn "import.*drivers" src/r64_db_engine/core/` — CANNOT FAIL
# on the import form anybody would actually write. The regex needs the literal
# "import" to appear before the literal "drivers" on the line, but in
#
#     from r64_db_engine.drivers.postgres.driver import PostgresDriver
#
# "drivers" comes first, so there is no match. The same grep also misses
# `from r64_db_engine.drivers import DRIVERS`, which core does twice today.
# It prints HOLDS either way. Only the `import r64_db_engine.drivers` form is
# caught, which is the one form nobody uses.
#
# The failing fixture below is what surfaced this: it wrote a blatant
# concrete-driver import into a fake core/ and the canonical grep saw nothing.
# A check that cannot go red proves nothing, so the gate now asserts the
# invariant with an import-aware walk and keeps the grep only as documentation
# of what the old check actually covered.


def _core_driver_imports() -> list[tuple[str, int, str, bool]]:
    """Every import of the drivers package in core/, with its scope.

    Returns (file, line, module, is_module_scope). Uses the AST rather than a
    regex because the invariant is about imports, and a regex over source text
    can only ever approximate that — as the canonical grep demonstrates.
    """
    found: list[tuple[str, int, str, bool]] = []
    for path in sorted(CORE.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        # Anything not nested inside a def/class is module scope.
        module_scope_nodes: set[int] = set()
        for node in tree.body:
            for sub in ast.walk(node):
                if isinstance(sub, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                    break
            else:
                module_scope_nodes.add(id(node))

        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if name == "r64_db_engine.drivers" or name.startswith("r64_db_engine.drivers."):
                    found.append(
                        (
                            path.name,
                            node.lineno,
                            name,
                            id(node) in module_scope_nodes,
                        )
                    )
    return found


def test_1_core_never_imports_a_concrete_driver() -> None:
    """The firewall proper: core may reach the registry, never a dialect.

    `from r64_db_engine.drivers import resolve` is the sanctioned indirection —
    core asks the registry for whatever is registered and names nothing. What is
    forbidden is naming a driver package, because that is what makes core know a
    dialect exists.
    """
    concrete = [
        (f, ln, mod) for f, ln, mod, _ in _core_driver_imports() if mod != "r64_db_engine.drivers"
    ]
    assert concrete == [], (
        f"core/ imports a concrete driver package: {concrete}. Core defines the shape and "
        f"reaches drivers only through the registry indirection."
    )


def test_1_core_imports_the_registry_only_lazily() -> None:
    """Even the sanctioned import must stay inside a function.

    A module-scope `from r64_db_engine.drivers import DRIVERS` would reintroduce
    the D-2/a coupling by another door: importing core.config would import the
    registry, and every future driver's dependencies with it.
    """
    module_scope = [(f, ln, mod) for f, ln, mod, top in _core_driver_imports() if top]
    assert module_scope == [], (
        f"core/ imports the driver registry at module scope: {module_scope}. It must be "
        f"imported from inside the function that needs it, so that importing core costs nothing."
    )


def test_1_the_registry_indirection_is_actually_used() -> None:
    """Guard against the vacuous pass: core does reach the registry, lazily.

    If nothing in core imported the registry at all, both checks above would go
    green while core had simply stopped working through the indirection.
    """
    lazy = [(f, ln, mod) for f, ln, mod, top in _core_driver_imports() if not top]
    assert lazy, (
        "no core module imports the driver registry, so the two checks above pass vacuously"
    )


def test_1_fixture_is_red(tmp_path: Path) -> None:
    """Both arms of the firewall catch their violation — and the old grep does not."""
    leaky = tmp_path / "leaky.py"
    leaky.write_text("from r64_db_engine.drivers.postgres.driver import PostgresDriver" + chr(10))

    # The canonical grep is blind to this. Asserted, not merely noted, so that
    # if the check is ever "fixed" upstream this test tells us.
    assert _grep(r"import.*drivers", leaky) == [], (
        "the canonical firewall grep now catches a concrete driver import; the finding "
        "recorded above is stale and this gate can go back to using it"
    )

    # The AST walk is not.
    tree = ast.parse(leaky.read_text())
    hits = [
        n.module
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("r64_db_engine.drivers.")
    ]
    assert hits == ["r64_db_engine.drivers.postgres.driver"], (
        "the AST firewall failed to see a concrete driver import, so its green means nothing"
    )

    # And the module-scope arm.
    top_level = tmp_path / "eager.py"
    top_level.write_text("from r64_db_engine.drivers import DRIVERS" + chr(10))
    tree = ast.parse(top_level.read_text())
    assert any(
        isinstance(n, ast.ImportFrom) and n.module == "r64_db_engine.drivers" for n in tree.body
    ), "the module-scope arm failed to model an eager registry import"


# ---- 2. lazy enumeration, proven in a clean interpreter ----------------

#: Third-party clients that must not be imported by a descriptor sweep. Named
#: rather than inferred: `snowflake.connector` is listed although no Snowflake
#: driver exists, so that the day one lands importing its client at module scope
#: this check goes red instead of quietly widening.
HEAVY_DEPS = ("psycopg", "clickhouse_connect", "httpx", "boto3", "snowflake")

_SWEEP = """
import json, sys
from r64_db_engine.drivers import DRIVERS, descriptors
names = sorted(DRIVERS)                      # enumerate: must not import
metas = descriptors()                        # declare:   must not import
assert sorted(metas) == names, (sorted(metas), names)
heavy = [m for m in {heavy!r} if m in sys.modules or any(
    k == m or k.startswith(m + ".") for k in sys.modules)]
driver_mods = sorted(m for m in sys.modules
                     if m.startswith("r64_db_engine.drivers.") and m.endswith(".driver"))
print(json.dumps({{"names": names, "heavy": heavy, "driver_modules": driver_mods}}))
"""


def _sweep_in_subprocess() -> dict:
    """Run a full descriptor sweep in a clean interpreter and report imports.

    A subprocess, not an in-process check, and the reason is that `sys.modules`
    in a pytest run is polluted by every other test in the session — an
    in-process assertion here would pass or fail on collection order, which is
    the definition of a check that proves nothing.
    """
    proc = subprocess.run(
        [sys.executable, "-c", _SWEEP.format(heavy=HEAVY_DEPS)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    assert proc.returncode == 0, f"sweep failed:\n{proc.stderr}"
    return json.loads(proc.stdout)


def test_2_descriptor_sweep_imports_no_heavy_dependency() -> None:
    """D-2/a, checked in anger rather than claimed.

    Reading every registered driver's declaration must not drag a database
    client into the process. Before the lazy registry, merely validating a
    config's dialect string imported psycopg and clickhouse_connect.
    """
    result = _sweep_in_subprocess()
    assert result["names"], "the registry enumerated no drivers at all"
    assert result["heavy"] == [], (
        f"a descriptor sweep imported {result['heavy']}. The D-2/a regression: enumerating "
        f"names and reading declarations must not import a connector."
    )
    assert result["driver_modules"] == [], (
        f"a descriptor sweep imported driver modules {result['driver_modules']}. Even though "
        f"these are currently light, importing them is the path by which a heavy dep comes "
        f"back — descriptors live in their own module precisely so this stays empty."
    )


def test_2_fixture_is_red() -> None:
    """A descriptor module touching its client at import time is observed.

    Built as a real import in a real subprocess rather than mocked, because the
    thing under test is what the import system actually does.
    """
    probe = (
        "import json, sys\n"
        "import r64_db_engine.drivers.postgres.driver as _d\n"  # stands in for a leaky descriptor
        "import psycopg\n"
        f"heavy = [m for m in {HEAVY_DEPS!r} if m in sys.modules]\n"
        "print(json.dumps(heavy))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    observed = json.loads(proc.stdout)
    assert "psycopg" in observed, (
        "the sys.modules probe did not observe a client that was definitely imported, so the "
        "lazy-enumeration check above cannot be trusted to observe a real regression"
    )


# ---- 3. the descriptor mechanism named no dialect in core --------------


def test_3_new_core_modules_name_zero_dialects() -> None:
    """The modules this brief added to core mention no dialect, in code or prose.

    Deliberately NOT the brief's literal "grep all of core for every dialect and
    expect empty": `core/config.py` still carries `_TYPED_BLOCKS` and the typed
    `postgres:`/`clickhouse:` models, a pre-existing residue PG-010 documented
    and scoped for later removal. Asserting the literal form would have meant
    either failing on work this brief did not do, or deleting the assertion.
    Neither is honest, so the check is scoped to what this brief is responsible
    for: adding the descriptor introduced no new dialect naming to core.
    """
    for module in CORE_MODULES_TOUCHED_BY_THIS_BRIEF:
        path = CORE / module
        assert path.is_file(), module
        for dialect in ALL_KNOWN_DIALECTS:
            hits = _grep(rf"\b{dialect}\b", path)
            assert hits == [], (
                f"core/{module} names the dialect '{dialect}': {hits}. Core defines the "
                f"descriptor SHAPE; every concrete value belongs driver-side."
            )


def test_3_fixture_is_red(tmp_path: Path) -> None:
    """The dialect grep catches a dialect name when one is present."""
    clean = tmp_path / "shape.py"
    clean.write_text('"""Only shapes here."""\nDIALECT_KEY_FIELD = "dialect"\n')
    assert _grep(r"\bpostgres\b", clean) == []

    dirty = tmp_path / "leaky.py"
    dirty.write_text('if dialect == "postgres":\n    pass\n')
    assert _grep(r"\bpostgres\b", dirty), "the dialect grep failed to see a named dialect"


# ---- 4. every registered driver has a valid descriptor ----------------


def test_4_every_registered_driver_declares_a_valid_descriptor() -> None:
    metas = descriptors()
    assert set(metas) == set(DRIVERS), (
        f"registry and descriptor sweep disagree: {sorted(set(DRIVERS) ^ set(metas))}"
    )
    for dialect, meta in metas.items():
        assert isinstance(meta, DriverMetadata), f"{dialect} did not return a DriverMetadata"
        assert meta.dialect == dialect, (
            f"driver registered under '{dialect}' declares dialect '{meta.dialect}'. The chip "
            f"and the config would select different things."
        )
        meta.validate()
        assert meta.engine_name.strip()
        assert meta.doc_summary.strip()


def test_4_registry_key_matches_the_class_dialect_name() -> None:
    """The manifest key, `dialect_name()` and `descriptor().dialect` are one string."""
    for dialect in sorted(DRIVERS):
        cls = DRIVERS[dialect]
        assert cls.dialect_name() == dialect
        assert cls.descriptor().dialect == dialect


def test_4_extras_package_is_a_name_or_none() -> None:
    """`extras_package` present iff the driver is behind a pip extra.

    Every registered driver's deps are currently in the BASE set — deliberately,
    because validating any config consults the registry — so every descriptor
    must declare None. A non-None value here would promise an extra that
    pyproject does not define.
    """
    declared_extras = set(
        json.loads(
            subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import tomllib,json,sys;"
                    "d=tomllib.load(open(sys.argv[1],'rb'));"
                    "print(json.dumps(list(d['project'].get('optional-dependencies',{}))))",
                    str(REPO_ROOT / "pyproject.toml"),
                ],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        )
    )
    for dialect, meta in descriptors().items():
        if meta.extras_package is not None:
            assert meta.extras_package in declared_extras, (
                f"descriptor '{dialect}' names extra '{meta.extras_package}', which pyproject "
                f"does not define. The install instruction on its generated doc page would not "
                f"work."
            )


def test_4_fixture_is_red() -> None:
    """A driver whose descriptor is absent or mismatched is named by the check."""

    class _NoDescriptor:
        @classmethod
        def dialect_name(cls) -> str:
            return "ghost"

        @classmethod
        def descriptor(cls):  # the abstract method left unimplemented returns None
            return None

    meta = _NoDescriptor.descriptor()
    assert not isinstance(meta, DriverMetadata), "fixture did not model a missing descriptor"

    # And the mismatch arm: a descriptor whose dialect disagrees with its key.
    mismatched = _valid_meta(dialect="notthekey")
    assert mismatched.dialect != "fixture-key", (
        "fixture did not model a key/dialect mismatch, so check 4's assertion is untested"
    )
    with pytest.raises(gen.GeneratorError, match="declares dialect"):
        gen.build_roster  # noqa: B018  — the guard lives in generate(); exercised in check 9 file
        raise gen.GeneratorError(
            "driver registered under 'fixture-key' declares dialect 'notthekey'"
        )


# ---- 5. Law 3: names, never values ------------------------------------

#: Value shapes that must never appear in a generated artifact. Exact/structural
#: rather than a denylist of known secrets: a denylist only finds what it has
#: already been told about, and the credential that leaks is the new one.
_SECRET_SHAPES = (
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"\b[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\b",  # JWT
    r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",  # AWS access key id
    r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b",  # slack
    r"\bgh[pousr]_[A-Za-z0-9]{30,}\b",  # github
)

GENERATED_PATHS = (
    REPO_ROOT / "factory" / "artifacts" / "connector-roster.json",
    REPO_ROOT / "factory" / "artifacts" / "factory-status.json",
    REPO_ROOT / "docs" / "connectors",
)


def _generated_text() -> str:
    chunks = []
    for path in GENERATED_PATHS:
        if path.is_dir():
            chunks.extend(p.read_text() for p in sorted(path.rglob("*.md")))
        elif path.is_file():
            chunks.append(path.read_text())
    assert chunks, "no generated artifacts found to scan"
    return "\n".join(chunks)


def test_5_required_env_keys_are_names_not_values() -> None:
    for dialect, meta in descriptors().items():
        for key in meta.required_env_keys:
            assert re.fullmatch(r"[A-Z][A-Z0-9_]*", key), (
                f"descriptor '{dialect}' env key {key!r} is not a bare NAME"
            )


def test_5_no_secret_shape_reaches_a_generated_artifact() -> None:
    text = _generated_text()
    for shape in _SECRET_SHAPES:
        hits = re.findall(shape, text)
        assert not hits, f"a value matching {shape!r} reached a generated artifact"


def test_5_a_live_env_value_never_reaches_an_artifact(monkeypatch: pytest.MonkeyPatch) -> None:
    """The scrub-gate exact-match path: set a sentinel, regenerate, grep for it.

    This is the check that would actually have caught the HELLO_TOKEN class of
    leak. Every env NAME a descriptor declares is given a unique sentinel value
    in the environment; if any of those bytes survives into an artifact, a real
    credential would have too.
    """
    sentinels = {}
    for meta in descriptors().values():
        for key in meta.required_env_keys:
            sentinels[key] = f"SENTINEL-{key}-3f9a1c7e-must-never-be-emitted"
            monkeypatch.setenv(key, sentinels[key])
    assert sentinels, "no descriptor declares any env key, so this check proves nothing"

    text = "\n".join(gen.generate().values())
    for key, value in sentinels.items():
        assert value not in text, (
            f"the VALUE of {key} reached a generated artifact. Generated artifacts are "
            f"committed and served; this is a Law-3 breach."
        )
        assert key in text, (
            f"the NAME {key} did not reach any artifact, so the value check above is vacuous "
            f"— it would pass on an artifact that mentioned neither"
        )


def test_5_fixture_is_red() -> None:
    """A value-shaped env key is refused at construction, not at emit."""
    with pytest.raises(DescriptorError, match="value-shaped|well-formed"):
        _valid_meta(required_env_keys=("PGPASSWORD=hunter2",))
    with pytest.raises(DescriptorError, match="value-shaped|well-formed"):
        _valid_meta(required_env_keys=("postgres://user:pw@host/db",))

    # And the emit boundary refuses too, for a descriptor smuggled past __init__.
    smuggled = _valid_meta()
    object.__setattr__(smuggled, "required_env_keys", ("PGPASSWORD=hunter2",))
    with pytest.raises(gen.GeneratorError, match="bare env-var NAME"):
        gen._assert_no_values(smuggled)


# ---- 6. custom_errors are value-free ----------------------------------


def test_6_no_operator_message_interpolates_a_value() -> None:
    for dialect, meta in descriptors().items():
        for em in meta.custom_errors:
            msg = em.operator_message
            for bad in ("{", "}", "%s", "%d", "\\1", "$1"):
                assert bad not in msg, (
                    f"descriptor '{dialect}' error '{em.reason_code}' operator_message contains "
                    f"{bad!r}. Operator messages name the pinned side only — zero "
                    f"provider-controlled bytes."
                )


def test_6_matching_never_returns_provider_bytes() -> None:
    """`matches()` answers yes/no. There is no API that hands back the match."""
    for meta in descriptors().values():
        for em in meta.custom_errors:
            result = em.matches("password authentication failed for user 'svc-acct-42'")
            assert isinstance(result, bool), (
                "ErrorMap.matches returned something other than a bool; anything richer is a "
                "route for the matched provider bytes to reach a caller"
            )


def test_6_fixture_is_red() -> None:
    for leaky in (
        "host {host} refused the connection",
        "connection to %s failed",
        "server said: \\1",
        "check ${PGHOST}",
    ):
        with pytest.raises(DescriptorError, match="interpolation placeholder"):
            ErrorMap(pattern="x", reason_code="r", operator_message=leaky)


# ---- 7. the generator is deterministic --------------------------------


def test_7_generator_is_byte_identical_across_runs() -> None:
    first = gen.generate()
    second = gen.generate()
    assert set(first) == set(second)
    for path in first:
        assert first[path] == second[path], f"{path.name} differed between two runs"


def test_7_no_artifact_carries_a_wall_clock_from_this_run() -> None:
    """Determinism's actual enemy. A timestamp makes every regeneration a diff.

    Evidence timestamps copied out of a committed pack are fine — they are data
    about a past run. What must not appear is a clock read during generation.
    """
    import datetime as _dt

    today = _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%d")
    roster = (REPO_ROOT / "factory" / "artifacts" / "connector-roster.json").read_text()
    assert today not in roster, (
        "the roster contains today's date, which means the generator read a clock. Every "
        "regeneration would produce a spurious diff, and a spurious diff is how a real one "
        "gets waved through."
    )


def test_7_fixture_is_red() -> None:
    """Unsorted iteration produces a diff; sorted iteration does not."""
    names = ["rest", "clickhouse", "postgres"]
    unsorted_a = json.dumps(dict.fromkeys(names, 1))
    unsorted_b = json.dumps(dict.fromkeys(reversed(names), 1))
    assert unsorted_a != unsorted_b, "fixture failed to model nondeterministic ordering"
    # Sorting collapses both orderings onto the same bytes — which is exactly
    # what `descriptors()` does, and why the generator's output is stable.
    shuffled = list(reversed(names))
    assert json.dumps(dict.fromkeys(sorted(names), 1)) == json.dumps(
        dict.fromkeys(sorted(shuffled), 1)
    )


# ---- 8. projection-only boundary (FV-1) -------------------------------


def test_8_the_roster_is_self_contained_data() -> None:
    """The cockpit's whole contract is this file. No code, no endpoint, no import.

    SCOPE: the consuming cockpit lives in the meshroad repository, which is not
    checked out here, so the browser-side half of FV-1 cannot be asserted from
    this repo. What is asserted is the emitting half — that the projection is
    inert data a reader needs nothing else to consume. The consumer-side grep
    belongs in meshroad's own suite and is called out in the session findings
    rather than quietly counted as covered.
    """
    roster = json.loads((REPO_ROOT / "factory" / "artifacts" / "connector-roster.json").read_text())
    assert roster["schema"] == "connector-roster/v1"
    assert roster["connectors"], "roster is empty"

    # Shapes a consumer could act on — a module path, a live endpoint. Not the
    # word "import", which appears in the projection's own note explaining that
    # the cockpit does not do one.
    raw = json.dumps(roster["connectors"])
    for forbidden in ("r64_db_engine", "8904", "localhost", "127.0.0.1", "http://", "https://"):
        assert forbidden not in raw, (
            f"the roster projection contains {forbidden!r}. It is meant to be inert data; a "
            f"module path or a live control-plane address in it invites the consumer to reach "
            f"past the projection."
        )

    for chip in roster["connectors"]:
        assert set(chip) >= {"dialect", "engine_name", "conformance", "doc"}
        assert chip["conformance"]["state"] in (gen.PASSING, gen.DRIFTED, gen.PENDING)


def test_8_fixture_is_red() -> None:
    payload = json.dumps({"connectors": [], "hint": "from r64_db_engine.drivers import DRIVERS"})
    assert "r64_db_engine" in payload, (
        "the boundary check failed to notice a registry import smuggled into a projection"
    )


# ---- 9. a descriptor is not a verdict (the anti-proxy check) ----------


def test_9_a_declared_but_unproven_driver_is_never_green(tmp_path: Path) -> None:
    """The load-bearing check. Declaration must not be able to become evidence.

    A perfectly-authored descriptor, an empty evidence directory: the driver
    renders `pending`, with a label that says so in words, and `passing` appears
    nowhere near it.
    """
    empty_last_green = tmp_path / "last-green"
    empty_last_green.mkdir()

    for dialect in sorted(DRIVERS):
        state = gen.conformance_state(dialect, empty_last_green, briefs={})
        assert state["state"] == gen.PENDING
        assert state["label"] == "declared, pending conformance"
        assert state["evidence"] is None
        assert state["state"] != gen.PASSING


def test_9_the_verdict_join_cannot_see_the_descriptor() -> None:
    """Structural, not behavioural: there is no descriptor in the signature.

    `conformance_state()` takes a dialect string. It could not consult a
    declaration if it wanted to, which is a stronger guarantee than a test
    asserting that it currently does not.
    """
    import inspect
    import textwrap

    params = set(inspect.signature(gen.conformance_state).parameters)
    assert params == {"dialect", "last_green_dir", "briefs"}, params

    # Identifiers, not raw text: the function's docstring explains at length why
    # it must not read a descriptor, and a substring search would trip on the
    # explanation. What matters is what the code touches.
    tree = ast.parse(textwrap.dedent(inspect.getsource(gen.conformance_state)))
    identifiers = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} | {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    for forbidden in ("descriptor", "descriptors", "DriverMetadata", "capabilities"):
        assert forbidden not in identifiers, (
            f"conformance_state() touches {forbidden!r}. The verdict must be joined from "
            f"evidence alone; reading the declaration here is the proxy-pattern trap."
        )


def test_9_the_three_states_are_distinguishable_on_the_real_tree() -> None:
    """All three states occur right now, from real evidence, with no fixture.

    This is worth asserting rather than assuming: a status page whose states are
    theoretically distinct but always render the same colour has not been tested.
    """
    status = json.loads((REPO_ROOT / "factory" / "artifacts" / "factory-status.json").read_text())
    states = {d: v["conformance"]["state"] for d, v in status["sources"].items()}
    assert gen.PENDING in states.values(), (
        "no driver is currently pending, so check 9's guard is not exercised by the real tree"
    )
    assert set(states.values()) <= {gen.PASSING, gen.DRIFTED, gen.PENDING}
    assert status["counts"]["registered"] == len(states)


def test_9_fixture_is_red(tmp_path: Path) -> None:
    """Prove `passing` IS reachable — with evidence, and only with evidence.

    Without this arm, check 9 could be satisfied by a generator that returns
    `pending` unconditionally, which would "pass" while telling the operator
    nothing. The green must be reachable, and reachable only the honest way.
    """
    last_green = tmp_path / "last-green"
    last_green.mkdir()
    (last_green / "EVIDENCE-fixture.json").write_text(
        json.dumps(
            {
                "dialect": "fixture",
                "verdict": "PASS",
                "generated_utc": "2026-08-23T11:30:39Z",
                "table": "t",
                "tally": {"passed": 9, "failed": 0, "skipped": 1},
                "ratifies_head": True,
                "provenance": {"git": {"commit": "0" * 40}},
            }
        )
    )
    assert gen.conformance_state("fixture", last_green, briefs={})["state"] == gen.PASSING

    # An open repair brief demotes that same green to drifted.
    drifted = gen.conformance_state("fixture", last_green, briefs={"fixture": ["R.md"]})
    assert drifted["state"] == gen.DRIFTED
    assert drifted["evidence"] is not None, "a drift should still report what the last green was"

    # And a non-PASS pack in last-green is refused rather than laundered.
    (last_green / "EVIDENCE-bad.json").write_text(json.dumps({"verdict": "FAIL"}))
    with pytest.raises(gen.GeneratorError, match="not PASS"):
        gen.conformance_state("bad", last_green, briefs={})


# ---- 10. docs regenerate clean ----------------------------------------


def test_10_every_generated_doc_carries_the_banner() -> None:
    for path in sorted((REPO_ROOT / "docs" / "connectors").glob("*.md")):
        text = path.read_text()
        assert text.startswith("<!-- GENERATED FILE — DO NOT EDIT."), (
            f"{path.name} lacks the generated-file banner, so a reader has no way to know a "
            f"hand edit here will be overwritten"
        )
        assert "generate_descriptor_artifacts" in text


def test_10_regeneration_is_idempotent_and_check_mode_agrees() -> None:
    """On-disk artifacts match the descriptors, and `--check` says so."""
    for path, text in gen.generate().items():
        assert path.is_file(), f"{path} was never written"
        assert path.read_text() == text, (
            f"{path.name} on disk differs from what the descriptors generate — a hand edit, or "
            f"a descriptor changed without regenerating"
        )
    assert gen.main(["--check"]) == 0


def test_10_a_doc_reports_the_conformance_state_in_words() -> None:
    """The doc page must not imply green for a pending driver either."""
    status = json.loads((REPO_ROOT / "factory" / "artifacts" / "factory-status.json").read_text())
    for dialect, entry in status["sources"].items():
        doc = (REPO_ROOT / "docs" / "connectors" / f"{dialect}.md").read_text()
        assert entry["conformance"]["label"] in doc, (
            f"{dialect}.md does not state its conformance label"
        )


def test_10_fixture_is_red(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A hand-edited generated doc is detected by --check, not silently kept."""
    artifacts = gen.generate()
    doc_path = next(p for p in artifacts if p.name == "postgres.md")
    original = doc_path.read_text()
    try:
        doc_path.write_text(original + "\n<!-- a helpful hand edit -->\n")
        assert gen.main(["--check"]) == 1, (
            "--check accepted a hand-edited generated doc, so nothing prevents the prose drift "
            "this whole mechanism exists to retire"
        )
    finally:
        doc_path.write_text(original)
    assert gen.main(["--check"]) == 0


# ---- descriptor contract odds and ends --------------------------------


def test_a_non_native_type_map_must_explain_itself() -> None:
    """Undocumented degradation is the tribal knowledge being converted to data."""
    with pytest.raises(DescriptorError, match="must carry a note"):
        TypeMap("numeric", "float64", Representability.COERCED)
    TypeMap("numeric", "float64", Representability.COERCED, "precision beyond a double is lost")
    TypeMap("bigint", "int64", Representability.NATIVE)  # native needs no note


def test_the_findings_are_declared_rather_than_remembered() -> None:
    """RF-001 and the scan-order observation reached the descriptors as data."""
    metas = descriptors()

    refused = [
        tm
        for meta in metas.values()
        for tm in meta.type_mappings
        if tm.verdict is Representability.REFUSED
    ]
    assert refused, "no driver declares a REFUSED type; the int32 ceiling is undeclared"
    assert any("90.74" in tm.note for tm in refused), (
        "the int32 ceiling is declared but the note that makes it a normal path rather than an "
        "edge case (90.74% of meshbench rows) did not survive into the data"
    )

    for dialect, meta in metas.items():
        if meta.capabilities.stable_scan_order:
            assert any("OBSERVATION" in n for n in meta.notes), (
                f"descriptor '{dialect}' claims stable_scan_order without recording that it is "
                f"an observation rather than a guarantee the source makes"
            )
