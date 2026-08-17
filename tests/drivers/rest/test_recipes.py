"""Recipe-book parsing: what it accepts, and — mostly — what it refuses.

The book is a control surface described by a file, so the loader is the place
where a malformed or malicious book has to die. Refusals here happen at LOAD,
which in the daemon means at startup rather than at the first cadence tick.

The closed-vocabulary tests matter more than they look. Every one of them is
Law 1 in miniature: the moment a book can name something nobody implemented,
the engine has to decide what to do about it at runtime, and runtime
interpretation is exactly what this lane exists to avoid.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from r64_db_engine.drivers.rest.recipes import (
    RecipeBookError,
    load_book,
    parse_book,
)
from r64_db_engine.drivers.rest.security import RecipeSecurityError

BOOK_PATH = Path(__file__).resolve().parents[3] / "factory" / "recipes" / "open-meteo.yaml"

MINIMAL = {
    "dataset": "demo",
    "recipes": [
        {
            "name": "one",
            "method": "GET",
            "url": "https://api.example.com/v1/things",
            "params_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
            "response_schema": {"type": "object"},
            "extract": "items",
        }
    ],
    "output": {"columns": [{"name": "id", "from": "id", "type": "int64"}]},
}


def book(**overrides):
    doc = copy.deepcopy(MINIMAL)
    doc.update(overrides)
    return doc


def mutate(fn):
    doc = copy.deepcopy(MINIMAL)
    fn(doc)
    return doc


# ---------------------------------------------------------------------------
# The shipped book
# ---------------------------------------------------------------------------


def test_the_shipped_open_meteo_book_parses() -> None:
    parsed = load_book(BOOK_PATH)
    assert parsed.dataset == "open_meteo_berlin_hourly"
    assert set(parsed.recipes) == {"geocode", "archive"}
    assert [s.recipe for s in parsed.threading] == ["geocode", "archive"]
    assert [c.name for c in parsed.output] == ["time", "temperature_2m"]


def test_the_shipped_book_threads_across_two_different_hosts() -> None:
    """The binding crosses a host boundary, and each recipe's allowlist is its
    own — so neither recipe can reach the other's host."""
    parsed = load_book(BOOK_PATH)
    assert parsed.recipes["geocode"].allowed_host == "geocoding-api.open-meteo.com"
    assert parsed.recipes["archive"].allowed_host == "archive-api.open-meteo.com"
    assert parsed.threading[-1].bind == {
        "latitude": "geocode.results[0].latitude",
        "longitude": "geocode.results[0].longitude",
    }


def test_the_shipped_book_needs_no_credentials() -> None:
    parsed = load_book(BOOK_PATH)
    assert all(r.auth.type == "none" for r in parsed.recipes.values())
    assert all(r.auth.env_file is None for r in parsed.recipes.values())


def test_the_shipped_book_pins_a_fixed_window_not_a_relative_one() -> None:
    """A relative window would make the artifact a function of the wall clock
    and the checksum check could never pass."""
    parsed = load_book(BOOK_PATH)
    static = parsed.recipes["archive"].static_params
    assert static["start_date"] == "2026-01-01"
    assert static["end_date"] == "2026-03-31"
    assert static["timezone"] == "UTC"


# ---------------------------------------------------------------------------
# Destination pinning, enforced at load
# ---------------------------------------------------------------------------


def test_plaintext_url_is_refused_at_load() -> None:
    with pytest.raises(RecipeSecurityError):
        parse_book(mutate(lambda d: d["recipes"][0].__setitem__("url", "http://api.example.com/v1")))


def test_a_templated_url_is_refused() -> None:
    """A substitutable URL is a destination an input can steer. The URL is
    pinned at creation; inputs fill declared parameters only."""
    with pytest.raises(RecipeSecurityError, match="template placeholder"):
        parse_book(
            mutate(lambda d: d["recipes"][0].__setitem__("url", "https://api.example.com/{path}"))
        )


# ---------------------------------------------------------------------------
# Closed vocabularies
# ---------------------------------------------------------------------------


def test_unknown_top_level_key_is_refused() -> None:
    with pytest.raises(RecipeBookError, match="unknown key"):
        parse_book(book(retries=3))


def test_typo_in_pagination_is_refused_rather_than_ignored() -> None:
    """`paginaton:` silently ignored would run the recipe unpaginated and
    return a first page that looks like a complete pull."""
    with pytest.raises(RecipeBookError, match="unknown key"):
        parse_book(mutate(lambda d: d["recipes"][0].__setitem__("paginaton", {"type": "cursor"})))


@pytest.mark.parametrize("value", ["offset", "keyset", "auto", ""])
def test_unknown_pagination_type_is_refused(value: str) -> None:
    with pytest.raises(RecipeBookError, match="pagination.type"):
        parse_book(mutate(lambda d: d["recipes"][0].__setitem__("pagination", {"type": value})))


def test_unknown_extract_shape_is_refused() -> None:
    with pytest.raises(RecipeBookError, match="extract.shape"):
        parse_book(
            mutate(lambda d: d["recipes"][0].__setitem__("extract", {"path": "x", "shape": "tree"}))
        )


def test_unknown_output_type_is_refused() -> None:
    with pytest.raises(RecipeBookError, match="output.columns"):
        parse_book(
            mutate(lambda d: d["output"]["columns"][0].__setitem__("type", "int32"))
        )


def test_unsupported_http_method_is_refused() -> None:
    with pytest.raises(RecipeBookError, match="method"):
        parse_book(mutate(lambda d: d["recipes"][0].__setitem__("method", "DELETE")))


# ---------------------------------------------------------------------------
# Threading
# ---------------------------------------------------------------------------


def test_threading_may_not_supply_an_undeclared_input() -> None:
    """params_schema is the closed list of what a runtime value may touch. It
    is also the mechanism that keeps inputs away from host and path."""
    doc = book(threading=[{"recipe": "one", "params": {"not_declared": 1}}])
    with pytest.raises(RecipeBookError, match="does not declare"):
        parse_book(doc)


def test_threading_may_not_bind_from_a_recipe_that_has_not_run() -> None:
    doc = copy.deepcopy(MINIMAL)
    doc["recipes"].append({**MINIMAL["recipes"][0], "name": "two"})
    doc["threading"] = [{"recipe": "one", "bind": {"q": "two.value"}}, {"recipe": "two"}]
    with pytest.raises(RecipeBookError, match="has not run yet"):
        parse_book(doc)


def test_threading_may_not_name_an_undefined_recipe() -> None:
    with pytest.raises(RecipeBookError, match="not defined"):
        parse_book(book(threading=[{"recipe": "nope"}]))


def test_duplicate_recipe_names_are_refused() -> None:
    doc = copy.deepcopy(MINIMAL)
    doc["recipes"].append(copy.deepcopy(MINIMAL["recipes"][0]))
    with pytest.raises(RecipeBookError, match="duplicate recipe name"):
        parse_book(doc)


def test_threading_defaults_to_the_last_recipe() -> None:
    parsed = parse_book(book())
    assert [s.recipe for s in parsed.threading] == ["one"]


# ---------------------------------------------------------------------------
# Auth / credential law
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("missing", ["env_file", "key_name"])
def test_auth_requires_both_a_path_and_a_key_name(missing: str) -> None:
    auth = {"type": "header", "env_file": "/etc/x.env", "key_name": "X-Key"}
    auth.pop(missing)
    with pytest.raises(RecipeBookError, match=missing):
        parse_book(mutate(lambda d: d["recipes"][0].__setitem__("auth", auth)))


def test_unknown_auth_type_is_refused() -> None:
    with pytest.raises(RecipeBookError, match="auth.type"):
        parse_book(mutate(lambda d: d["recipes"][0].__setitem__("auth", {"type": "oauth2"})))


def test_a_secret_VALUE_cannot_be_named_in_a_book() -> None:
    """Credential law, enforced structurally: the auth block accepts a PATH and
    a key name, and nothing else. There is no key a secret could be typed into,
    so a book cannot carry one even by mistake."""
    with pytest.raises(RecipeBookError, match="unknown key"):
        parse_book(
            mutate(lambda d: d["recipes"][0].__setitem__(
                "auth", {"type": "header", "key_name": "X-Key", "value": "sk-live-abc123"}
            ))
        )


# ---------------------------------------------------------------------------
# Bounds and required keys
# ---------------------------------------------------------------------------


def test_response_schema_is_required() -> None:
    """Without it there is no per-pull validator, and drift becomes silent."""
    with pytest.raises(RecipeBookError, match="response_schema"):
        parse_book(mutate(lambda d: d["recipes"][0].pop("response_schema")))


def test_extract_is_required() -> None:
    with pytest.raises(RecipeBookError, match="extract"):
        parse_book(mutate(lambda d: d["recipes"][0].pop("extract")))


def test_output_columns_must_not_be_empty() -> None:
    with pytest.raises(RecipeBookError, match="at least one column"):
        parse_book(book(output={"columns": []}))


def test_a_non_utc_timestamp_column_is_refused() -> None:
    """B-2 applied to the recipe lane: a per-column zone would reintroduce the
    uniform shift that aggregate parity cannot see."""
    with pytest.raises(RecipeBookError, match="must be UTC"):
        parse_book(
            book(output={"columns": [
                {"name": "t", "from": "t", "type": "timestamp[us]", "tz": "America/Los_Angeles"}
            ]})
        )


def test_max_pages_must_be_positive() -> None:
    with pytest.raises(RecipeBookError, match="max_pages"):
        parse_book(
            mutate(lambda d: d["recipes"][0].__setitem__(
                "pagination", {"type": "page", "page_param": "p", "max_pages": 0}
            ))
        )


def test_cursor_pagination_requires_its_paths() -> None:
    with pytest.raises(RecipeBookError, match="cursor_path"):
        parse_book(mutate(lambda d: d["recipes"][0].__setitem__("pagination", {"type": "cursor"})))


def test_a_missing_book_is_refused_with_its_path() -> None:
    with pytest.raises(RecipeBookError, match="not found"):
        load_book("/nonexistent/book.yaml")
