"""Recipe-book loading and validation. The book is DATA; nothing here executes.

A recipe is one call. A recipe book is an ordered set of recipes plus the
threading that binds one recipe's output to the next one's inputs, compiled
once at authoring time into a declarative document.

Law 1 in structural form: **the book is data, the engine is the only code.** An
agent researches an API once, at build time, and writes down what it learned.
At pull time nothing infers anything. That is what makes a long-tail connector
reproducible and reviewable rather than merely quick to produce.

The consequence, and it is deliberate: this vocabulary is CLOSED. An unknown
`pagination.type`, an unknown `extract.shape`, an unknown output `type`, or an
undeclared parameter is REFUSED, not guessed at. The moment a book can describe
something nobody implemented, runtime interpretation is back with extra steps.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from r64_db_engine.drivers.rest.security import (
    RecipeSecurityError,
    assert_host_allowed,
    assert_https,
    host_of,
)

PAGINATION_TYPES = frozenset({"none", "cursor", "page", "link-header"})
AUTH_TYPES = frozenset({"none", "header", "query"})
EXTRACT_SHAPES = frozenset({"records", "columnar"})
OUTPUT_TYPES = frozenset({"int64", "double", "string", "bool", "timestamp[us]"})
HTTP_METHODS = frozenset({"GET", "POST"})

DEFAULT_MAX_RESPONSE_BYTES = 32 * 1024 * 1024
DEFAULT_TIMEOUT_S = 30.0
DEFAULT_MAX_PAGES = 100


class RecipeBookError(ValueError):
    """The recipe book is malformed. Refused at load, never at first pull."""


@dataclass(frozen=True)
class Auth:
    type: str = "none"
    env_file: str | None = None
    key_name: str | None = None


@dataclass(frozen=True)
class Pagination:
    type: str = "none"
    cursor_path: str | None = None
    cursor_param: str | None = None
    page_param: str | None = None
    size_param: str | None = None
    page_size: int | None = None
    rel: str = "next"
    max_pages: int = DEFAULT_MAX_PAGES
    # Paths a provider-supplied next-URL may move to, declared at AUTHORING
    # time. Empty means "the pinned path only" — the default-deny position.
    allowed_next_paths: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Extract:
    path: str
    shape: str = "records"


@dataclass(frozen=True)
class Recipe:
    name: str
    method: str
    url: str
    allowed_host: str
    auth: Auth
    params_schema: dict[str, Any]
    response_schema: dict[str, Any]
    pagination: Pagination
    extract: Extract
    static_params: dict[str, Any] = field(default_factory=dict)

    @property
    def declared_params(self) -> set[str]:
        return set(self.params_schema.get("properties", {}))


@dataclass(frozen=True)
class OutputColumn:
    name: str
    source: str
    type: str
    tz: str | None = None


@dataclass(frozen=True)
class ThreadStep:
    recipe: str
    bind: dict[str, str] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Limits:
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES
    timeout_s: float = DEFAULT_TIMEOUT_S


@dataclass(frozen=True)
class RecipeBook:
    dataset: str
    recipes: dict[str, Recipe]
    threading: list[ThreadStep]
    output: list[OutputColumn]
    limits: Limits
    path: Path | None = None

    @property
    def terminal_step(self) -> ThreadStep:
        """The step whose records become the artifact. Always the last."""
        return self.threading[-1]


def _require(doc: dict[str, Any], key: str, where: str) -> Any:
    if key not in doc:
        raise RecipeBookError(f"{where}: missing required key '{key}'")
    return doc[key]


def _reject_unknown(doc: dict[str, Any], permitted: set[str], where: str) -> None:
    """Typo protection. An unrecognized key is refused, never ignored.

    Silently ignoring `paginaton:` would run the recipe unpaginated and return
    a first page that looks like a complete pull.
    """
    unknown = set(doc) - permitted
    if unknown:
        raise RecipeBookError(
            f"{where}: unknown key(s) {sorted(unknown)}. Permitted: {sorted(permitted)}"
        )


def _parse_auth(doc: Any, where: str) -> Auth:
    if doc is None:
        return Auth()
    if not isinstance(doc, dict):
        raise RecipeBookError(f"{where}.auth must be a mapping")
    _reject_unknown(doc, {"type", "env_file", "key_name"}, f"{where}.auth")
    auth_type = doc.get("type", "none")
    if auth_type not in AUTH_TYPES:
        raise RecipeBookError(
            f"{where}.auth.type must be one of {sorted(AUTH_TYPES)}, got {auth_type!r}"
        )
    if auth_type != "none":
        for key in ("env_file", "key_name"):
            if not doc.get(key):
                raise RecipeBookError(
                    f"{where}.auth.{key} is required when auth.type is {auth_type!r}. "
                    f"env_file is a PATH to a 0600 file the engine reads at call time; "
                    f"a secret value is never a config key."
                )
    return Auth(type=auth_type, env_file=doc.get("env_file"), key_name=doc.get("key_name"))


def _parse_pagination(doc: Any, where: str) -> Pagination:
    if doc is None:
        return Pagination()
    if not isinstance(doc, dict):
        raise RecipeBookError(f"{where}.pagination must be a mapping")
    _reject_unknown(
        doc,
        {"type", "cursor_path", "cursor_param", "page_param", "size_param",
         "page_size", "rel", "max_pages", "allowed_next_paths"},
        f"{where}.pagination",
    )
    ptype = doc.get("type", "none")
    if ptype not in PAGINATION_TYPES:
        raise RecipeBookError(
            f"{where}.pagination.type must be one of {sorted(PAGINATION_TYPES)}, got {ptype!r}"
        )
    if ptype == "cursor":
        for key in ("cursor_path", "cursor_param"):
            if not doc.get(key):
                raise RecipeBookError(f"{where}.pagination.{key} is required for type 'cursor'")
    if ptype == "page" and not doc.get("page_param"):
        raise RecipeBookError(f"{where}.pagination.page_param is required for type 'page'")
    if doc.get("allowed_next_paths") and ptype != "link-header":
        # Accept-and-ignore would leave the author believing they had widened
        # something. Only the link-header path consumes a provider-supplied URL.
        raise RecipeBookError(
            f"{where}.pagination.allowed_next_paths only applies to type 'link-header' "
            f"(got type {ptype!r}); cursor and page pagination never adopt a provider URL."
        )
    for path in doc.get("allowed_next_paths") or []:
        if not isinstance(path, str) or not path.startswith("/"):
            raise RecipeBookError(
                f"{where}.pagination.allowed_next_paths entries must be absolute paths "
                f"beginning with '/', got {path!r}"
            )

    # max_pages is a HARD bound, not a hint: a provider whose cursor never
    # terminates would otherwise loop until the process dies.
    max_pages = int(doc.get("max_pages", DEFAULT_MAX_PAGES))
    if max_pages < 1:
        raise RecipeBookError(f"{where}.pagination.max_pages must be >= 1, got {max_pages}")

    return Pagination(
        type=ptype,
        cursor_path=doc.get("cursor_path"),
        cursor_param=doc.get("cursor_param"),
        page_param=doc.get("page_param"),
        size_param=doc.get("size_param"),
        page_size=doc.get("page_size"),
        rel=doc.get("rel", "next"),
        max_pages=max_pages,
        allowed_next_paths=list(doc.get("allowed_next_paths") or []),
    )


def _parse_extract(doc: Any, where: str) -> Extract:
    if isinstance(doc, str):
        return Extract(path=doc)
    if not isinstance(doc, dict):
        raise RecipeBookError(f"{where}.extract must be a string path or a mapping")
    _reject_unknown(doc, {"path", "shape"}, f"{where}.extract")
    shape = doc.get("shape", "records")
    if shape not in EXTRACT_SHAPES:
        raise RecipeBookError(
            f"{where}.extract.shape must be one of {sorted(EXTRACT_SHAPES)}, got {shape!r}. "
            f"'records' is a list of objects; 'columnar' is an object of equal-length "
            f"parallel arrays (the shape open-meteo returns)."
        )
    return Extract(path=_require(doc, "path", f"{where}.extract"), shape=shape)


def _parse_recipe(doc: dict[str, Any], index: int) -> Recipe:
    where = f"recipes[{index}]"
    if not isinstance(doc, dict):
        raise RecipeBookError(f"{where} must be a mapping")
    _reject_unknown(
        doc,
        {"name", "method", "url", "auth", "params_schema", "response_schema",
         "pagination", "extract", "static_params"},
        where,
    )
    name = _require(doc, "name", where)
    where = f"recipes[{name}]"

    method = str(_require(doc, "method", where)).upper()
    if method not in HTTP_METHODS:
        raise RecipeBookError(f"{where}.method must be one of {sorted(HTTP_METHODS)}, got {method!r}")

    url = str(_require(doc, "url", where))
    # Validated at LOAD, so a book that names a plaintext or malformed endpoint
    # is refused before anything is executed. The same assertions run again at
    # call time against the URL actually being requested.
    assert_https(url)
    allowed_host = host_of(url)
    if "{" in url or "}" in url:
        # `{where}` already names the recipe, so the URL adds nothing an author
        # cannot look up in their own book. One rule everywhere beats a rule
        # that depends on whether the caller happens to be handling authored or
        # provider content — the second kind is the kind that drifts.
        raise RecipeSecurityError(
            f"{where}.url contains a template placeholder. The URL is PINNED at recipe "
            f"creation — runtime inputs may populate declared body/query parameters only, "
            f"never the host or the path. A substitutable URL is a destination an input "
            f"can steer. The URL is not reported."
        )
    assert_host_allowed(url, allowed_host)

    params_schema = doc.get("params_schema") or {"type": "object", "properties": {}}
    response_schema = _require(doc, "response_schema", where)
    if not isinstance(response_schema, dict):
        raise RecipeBookError(f"{where}.response_schema must be a jsonschema mapping")

    return Recipe(
        name=name,
        method=method,
        url=url,
        allowed_host=allowed_host,
        auth=_parse_auth(doc.get("auth"), where),
        params_schema=params_schema,
        response_schema=response_schema,
        pagination=_parse_pagination(doc.get("pagination"), where),
        extract=_parse_extract(_require(doc, "extract", where), where),
        static_params=dict(doc.get("static_params") or {}),
    )


def _parse_output(doc: Any) -> list[OutputColumn]:
    if not isinstance(doc, dict) or "columns" not in doc:
        raise RecipeBookError("output.columns is required")
    columns: list[OutputColumn] = []
    for i, col in enumerate(doc["columns"]):
        where = f"output.columns[{i}]"
        _reject_unknown(col, {"name", "from", "type", "tz"}, where)
        col_type = _require(col, "type", where)
        if col_type not in OUTPUT_TYPES:
            raise RecipeBookError(
                f"{where}.type must be one of {sorted(OUTPUT_TYPES)}, got {col_type!r}. "
                f"Integers are int64-native: there is no narrowing type to choose."
            )
        if col_type == "timestamp[us]":
            tz = col.get("tz", "UTC")
            if tz != "UTC":
                raise RecipeBookError(
                    f"{where}.tz must be UTC (got {tz!r}). Timestamps are normalized to UTC "
                    f"on this lane without exception — B-2 applies to APIs exactly as it does "
                    f"to databases, and a per-column zone would reintroduce the uniform shift "
                    f"that aggregate parity cannot see."
                )
        columns.append(
            OutputColumn(
                name=_require(col, "name", where),
                source=_require(col, "from", where),
                type=col_type,
                tz=col.get("tz", "UTC") if col_type == "timestamp[us]" else None,
            )
        )
    if not columns:
        raise RecipeBookError("output.columns must declare at least one column")
    return columns


def parse_book(doc: dict[str, Any], path: Path | None = None) -> RecipeBook:
    if not isinstance(doc, dict):
        raise RecipeBookError("recipe book must be a mapping")
    _reject_unknown(doc, {"dataset", "recipes", "threading", "output", "limits"}, "recipe book")

    raw_recipes = _require(doc, "recipes", "recipe book")
    if not isinstance(raw_recipes, list) or not raw_recipes:
        raise RecipeBookError("recipes must be a non-empty list")
    recipes = [_parse_recipe(r, i) for i, r in enumerate(raw_recipes)]
    by_name: dict[str, Recipe] = {}
    for recipe in recipes:
        if recipe.name in by_name:
            raise RecipeBookError(f"duplicate recipe name {recipe.name!r}")
        by_name[recipe.name] = recipe

    raw_threading = doc.get("threading") or [{"recipe": recipes[-1].name}]
    threading: list[ThreadStep] = []
    seen: set[str] = set()
    for i, step in enumerate(raw_threading):
        where = f"threading[{i}]"
        _reject_unknown(step, {"recipe", "bind", "params"}, where)
        name = _require(step, "recipe", where)
        if name not in by_name:
            raise RecipeBookError(
                f"{where} names recipe {name!r}, which is not defined "
                f"(defined: {sorted(by_name)})"
            )
        bind = dict(step.get("bind") or {})
        params = dict(step.get("params") or {})

        # Every bound and literal input must be DECLARED by the target recipe's
        # params_schema. An undeclared input is refused rather than forwarded:
        # the schema is the closed list of what a runtime value may touch, and
        # it is the mechanism that keeps inputs away from host and path.
        declared = by_name[name].declared_params
        undeclared = (set(bind) | set(params)) - declared
        if undeclared:
            raise RecipeBookError(
                f"{where} supplies input(s) {sorted(undeclared)} that recipe {name!r} does "
                f"not declare in params_schema.properties (declared: {sorted(declared)}). "
                f"Runtime inputs may only populate DECLARED parameters."
            )

        # A binding may only read a recipe that has already run.
        for target, expression in bind.items():
            producer = expression.split(".", 1)[0]
            if producer not in seen:
                raise RecipeBookError(
                    f"{where}.bind[{target}] reads {producer!r}, which has not run yet "
                    f"(available: {sorted(seen) or 'nothing'}). Threading is ordered."
                )
        threading.append(ThreadStep(recipe=name, bind=bind, params=params))
        seen.add(name)

    raw_limits = doc.get("limits") or {}
    _reject_unknown(raw_limits, {"max_response_bytes", "timeout_s"}, "limits")
    limits = Limits(
        max_response_bytes=int(raw_limits.get("max_response_bytes", DEFAULT_MAX_RESPONSE_BYTES)),
        timeout_s=float(raw_limits.get("timeout_s", DEFAULT_TIMEOUT_S)),
    )
    if limits.max_response_bytes < 1 or limits.timeout_s <= 0:
        raise RecipeBookError("limits.max_response_bytes and limits.timeout_s must be positive")

    return RecipeBook(
        dataset=_require(doc, "dataset", "recipe book"),
        recipes=by_name,
        threading=threading,
        output=_parse_output(_require(doc, "output", "recipe book")),
        limits=limits,
        path=path,
    )


def load_book(path: str | Path) -> RecipeBook:
    import yaml

    resolved = Path(path).expanduser()
    if not resolved.exists():
        raise RecipeBookError(f"recipe book not found: {resolved}")
    return parse_book(yaml.safe_load(resolved.read_text()), resolved)


__all__ = [
    "AUTH_TYPES",
    "EXTRACT_SHAPES",
    "OUTPUT_TYPES",
    "PAGINATION_TYPES",
    "Auth",
    "Extract",
    "Limits",
    "OutputColumn",
    "Pagination",
    "Recipe",
    "RecipeBook",
    "RecipeBookError",
    "ThreadStep",
    "load_book",
    "parse_book",
]
