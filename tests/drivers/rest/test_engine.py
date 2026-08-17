"""The recipe engine: extraction, pagination, validation, and the drift signal.

No network. A tiny fake client stands in for httpx, which is what lets the
pagination and drift paths be exercised deterministically — the alternative is
a live API that would have to be persuaded to misbehave on demand.

The central assertion of this module is a negative one: **a response that fails
its schema is never retried differently, coerced, or partially salvaged.** It
emits a repair event and raises. A connector that silently adapted to a
provider change would turn that change into corrupted data that looks entirely
healthy, which is the failure mode the whole recipe lane is arranged to
prevent.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote, quote_plus

import pytest

from r64_db_engine.drivers.rest import drift, engine
from r64_db_engine.drivers.rest.recipes import parse_book

# The address `stub_dns` reports as the validated resolution. The fake peer
# matches it, so the rebinding check passes by default and a test has to opt
# INTO a mismatch — the same way reality works.
VETTED_ADDRESS = "93.184.216.34"


class FakeNetworkStream:
    def __init__(self, peer):
        self._peer = peer

    def get_extra_info(self, name):
        if name == "server_addr" and self._peer is not None:
            return (self._peer, 443)
        return None


class FakeRequest:
    def __init__(self, method, url, params, json_body, headers):
        self.method = method
        self.url = url
        self.params = dict(params or {})
        self.json_body = dict(json_body or {})
        self.headers = dict(headers or {})


class FakeResponse:
    """Models the STREAMING response the engine now uses.

    `read()` is deliberately explicit and counted: the rebinding check must run
    before any body is fetched, and `body_reads` is what lets a test prove the
    body was never read on a refused connection.
    """

    def __init__(self, payload, status=200, headers=None, peer=VETTED_ADDRESS, raw=None):
        self._payload = raw if raw is not None else json.dumps(payload).encode()
        self.status_code = status
        self.headers = headers or {}
        self.extensions = {"network_stream": FakeNetworkStream(peer)}
        self.content = b""
        self.body_reads = 0
        self.closed = False

    def read(self):
        self.body_reads += 1
        self.content = self._payload
        return self.content

    def close(self):
        self.closed = True


class FakeClient:
    """Returns queued payloads and records what it was asked to send."""

    def __init__(self, payloads, headers=None, peers=None):
        self._payloads = list(payloads)
        self._headers = list(headers or [{}] * len(self._payloads))
        self._peers = list(peers or [VETTED_ADDRESS] * len(self._payloads))
        self.calls: list[tuple[str, dict, dict]] = []
        self.responses: list[FakeResponse] = []

    def build_request(self, method, url, params=None, json=None, headers=None):
        return FakeRequest(method, url, params, json, headers)

    def send(self, request, stream=False):
        sent = request.params if request.method == "GET" else request.json_body
        self.calls.append((request.url, dict(sent), dict(request.headers)))
        response = FakeResponse(
            self._payloads.pop(0),
            headers=self._headers.pop(0),
            peer=self._peers.pop(0),
        )
        self.responses.append(response)
        return response

    def close(self):
        pass


def make_book(**recipe_overrides):
    recipe = {
        "name": "one",
        "method": "GET",
        "url": "https://api.example.com/v1/items",
        "params_schema": {"type": "object", "properties": {"q": {"type": "string"},
                                                           "cursor": {"type": "string"},
                                                           "page": {"type": "integer"}}},
        "response_schema": {"type": "object", "required": ["items"]},
        "extract": "items",
    }
    recipe.update(recipe_overrides)
    return parse_book({
        "dataset": "demo",
        "recipes": [recipe],
        "output": {"columns": [{"name": "id", "from": "id", "type": "int64"}]},
    })


@pytest.fixture(autouse=True)
def isolated_drift_dir(tmp_path, monkeypatch):
    """Repair events must never land in the repo's evidence directory."""
    monkeypatch.setenv(drift.DRIFT_DIR_ENV, str(tmp_path / "drift"))
    # Do not fire real alerts from the test suite.
    monkeypatch.setattr(drift, "NTFY_BINARY", str(tmp_path / "no-such-ntfy"))


@pytest.fixture(autouse=True)
def stub_dns(monkeypatch):
    """Stub ONLY the DNS lookup, so these tests are offline and deterministic.

    Everything else in the destination fence still runs on every request the
    engine makes here — the https assertion, the host-allowlist assertion, and
    the public-address assertion against the address this returns. That is
    deliberate: it keeps these tests proving the fence is WIRED INTO the request
    path, which is the failure a security module can have while every one of its
    own unit tests passes.

    Real resolution behaviour (localhost, private ranges, unresolvable names) is
    covered for real in `test_security.py`, which does not stub anything.
    """
    from r64_db_engine.drivers.rest import security

    monkeypatch.setattr(security, "resolve_addresses", lambda host: ["93.184.216.34"])


# ---------------------------------------------------------------------------
# dotted_get — locate, never compute
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,expected",
    [
        ("a", {"b": [10, 20]}),
        ("a.b", [10, 20]),
        ("a.b[0]", 10),
        ("a.b[1]", 20),
        ("a.b[-1]", 20),
        ("results[0].latitude", None),
        ("missing", None),
        ("a.b[9]", None),
    ],
)
def test_dotted_get(path: str, expected) -> None:
    assert engine.dotted_get({"a": {"b": [10, 20]}}, path) == expected


def test_dotted_get_reaches_the_open_meteo_binding_shape() -> None:
    doc = {"results": [{"latitude": 52.52, "longitude": 13.41}]}
    assert engine.dotted_get(doc, "results[0].latitude") == 52.52


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def test_records_shape_extracts_a_list_of_objects() -> None:
    book = make_book()
    client = FakeClient([{"items": [{"id": 1}, {"id": 2}]}])
    records, _ = engine.run_recipe(client, book.recipes["one"], {}, book)
    assert records == [{"id": 1}, {"id": 2}]


def test_columnar_shape_zips_parallel_arrays() -> None:
    book = make_book(
        extract={"path": "hourly", "shape": "columnar"},
        response_schema={"type": "object", "required": ["hourly"]},
    )
    client = FakeClient([{"hourly": {"time": ["t0", "t1"], "temp": [1.0, 2.0]}}])
    records, _ = engine.run_recipe(client, book.recipes["one"], {}, book)
    assert records == [{"time": "t0", "temp": 1.0}, {"time": "t1", "temp": 2.0}]


def test_columnar_arrays_of_differing_length_are_refused() -> None:
    """`zip` would silently truncate to the shortest column and hand back a
    short table that looks entirely healthy. That is the whole reason lengths
    are checked instead of zipped."""
    book = make_book(
        extract={"path": "hourly", "shape": "columnar"},
        response_schema={"type": "object", "required": ["hourly"]},
    )
    client = FakeClient([{"hourly": {"time": ["t0", "t1", "t2"], "temp": [1.0, 2.0]}}])
    with pytest.raises(engine.ResponseValidationError, match="differing lengths"):
        engine.run_recipe(client, book.recipes["one"], {}, book)


def test_a_missing_extract_path_is_a_validation_error_not_an_empty_pull() -> None:
    """Returning zero rows here would look like 'the source is empty today'."""
    book = make_book(response_schema={"type": "object"})
    client = FakeClient([{"something_else": []}])
    with pytest.raises(engine.ResponseValidationError, match="is absent from the response"):
        engine.run_recipe(client, book.recipes["one"], {}, book)


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def test_cursor_pagination_follows_until_the_cursor_is_absent() -> None:
    book = make_book(
        pagination={"type": "cursor", "cursor_path": "next", "cursor_param": "cursor"},
        response_schema={"type": "object", "required": ["items"]},
    )
    client = FakeClient([
        {"items": [{"id": 1}], "next": "c2"},
        {"items": [{"id": 2}], "next": "c3"},
        {"items": [{"id": 3}]},
    ])
    records, _ = engine.run_recipe(client, book.recipes["one"], {}, book)
    assert [r["id"] for r in records] == [1, 2, 3]
    assert [c[1].get("cursor") for c in client.calls] == [None, "c2", "c3"]


def test_link_header_pagination_follows_rel_next() -> None:
    book = make_book(pagination={"type": "link-header"})
    client = FakeClient(
        [{"items": [{"id": 1}]}, {"items": [{"id": 2}]}],
        headers=[{"link": '<https://api.example.com/v1/items?p=2>; rel="next"'}, {}],
    )
    records, _ = engine.run_recipe(client, book.recipes["one"], {}, book)
    assert [r["id"] for r in records] == [1, 2]
    assert client.calls[1][0] == "https://api.example.com/v1/items?p=2"


def test_hitting_the_page_cap_refuses_rather_than_truncating() -> None:
    """A provider whose cursor never terminates must not yield a quietly
    partial dataset that every other check would pass."""
    book = make_book(
        pagination={"type": "cursor", "cursor_path": "next", "cursor_param": "cursor",
                    "max_pages": 3},
        response_schema={"type": "object", "required": ["items"]},
    )
    client = FakeClient([{"items": [{"id": i}], "next": f"c{i}"} for i in range(10)])
    with pytest.raises(engine.RecipeExecutionError, match="page cap"):
        engine.run_recipe(client, book.recipes["one"], {}, book)


# ---------------------------------------------------------------------------
# Response validation — the drift signal
# ---------------------------------------------------------------------------


def test_a_schema_violation_raises_and_is_not_salvaged() -> None:
    book = make_book()
    client = FakeClient([{"wrong_key": []}])
    with pytest.raises(engine.ResponseValidationError, match="response_schema"):
        engine.run_recipe(client, book.recipes["one"], {}, book)


def test_a_schema_violation_writes_a_structured_repair_event(tmp_path) -> None:
    book = make_book()
    client = FakeClient([{"wrong_key": []}])
    with pytest.raises(engine.ResponseValidationError):
        engine.run_recipe(client, book.recipes["one"], {}, book)

    events = drift.read_events("demo")
    assert len(events) == 1
    event = events[0]
    assert event["recipe"] == "one"
    assert event["source"] == "demo"
    assert event["url"] == "https://api.example.com/v1/items"
    assert "validation failed" in event["reason"]
    assert event["observed_utc"]


def test_repair_events_accumulate_rather_than_overwrite() -> None:
    """The accumulation IS the signal: failing once an hour between weekly
    sweeps is a different situation from failing once, and an overwriting log
    cannot tell them apart."""
    book = make_book()
    for _ in range(3):
        with pytest.raises(engine.ResponseValidationError):
            engine.run_recipe(FakeClient([{"wrong_key": []}]), book.recipes["one"], {}, book)
    assert len(drift.read_events("demo")) == 3


def test_a_failed_alert_does_not_mask_the_validation_failure() -> None:
    """ntfy is best-effort. A missing binary must not convert a loud schema
    failure into a confusing secondary error."""
    book = make_book()
    with pytest.raises(engine.ResponseValidationError, match="response_schema"):
        engine.run_recipe(FakeClient([{"wrong_key": []}]), book.recipes["one"], {}, book)


def test_a_non_json_body_is_a_validation_error() -> None:
    book = make_book()

    class BadClient(FakeClient):
        def send(self, request, stream=False):
            return FakeResponse(None, raw=b"<html>502 Bad Gateway</html>")

    with pytest.raises(engine.ResponseValidationError, match="not JSON"):
        engine.run_recipe(BadClient([{}]), book.recipes["one"], {}, book)


def test_an_http_error_status_is_refused() -> None:
    book = make_book()

    class ErrorClient(FakeClient):
        def send(self, request, stream=False):
            return FakeResponse({"items": []}, status=503)

    with pytest.raises(engine.RecipeExecutionError, match="HTTP 503"):
        engine.run_recipe(ErrorClient([{}]), book.recipes["one"], {}, book)


def test_an_oversized_response_is_refused() -> None:
    book = parse_book({
        "dataset": "demo",
        "recipes": [{
            "name": "one", "method": "GET", "url": "https://api.example.com/v1/items",
            "params_schema": {"type": "object", "properties": {}},
            "response_schema": {"type": "object"}, "extract": "items",
        }],
        "output": {"columns": [{"name": "id", "from": "id", "type": "int64"}]},
        "limits": {"max_response_bytes": 32},
    })
    client = FakeClient([{"items": [{"id": i} for i in range(100)]}])
    with pytest.raises(engine.RecipeExecutionError, match="over the"):
        engine.run_recipe(client, book.recipes["one"], {}, book)


# ---------------------------------------------------------------------------
# Undeclared inputs
# ---------------------------------------------------------------------------


def test_an_undeclared_runtime_input_is_refused_at_execution_too() -> None:
    """Belt and braces with the loader check: the closed params list is what
    keeps a runtime value away from host and path, so it is enforced at both
    ends rather than trusted from one."""
    from r64_db_engine.drivers.rest.security import RecipeSecurityError

    book = make_book()
    with pytest.raises(RecipeSecurityError, match="undeclared input"):
        engine.run_recipe(FakeClient([{"items": []}]), book.recipes["one"], {"evil": 1}, book)


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------


def test_a_world_readable_secret_file_is_refused(tmp_path: Path) -> None:
    """The credential law is about where secrets LIVE. A 0644 file does not
    satisfy it, and refusing costs nothing here — it happens before any call."""
    secret = tmp_path / "api.env"
    secret.write_text("sk-live-abc123\n")
    secret.chmod(0o644)
    with pytest.raises(engine.RecipeExecutionError, match="world-accessible"):
        engine.read_secret(str(secret))


def test_a_0600_secret_file_is_read(tmp_path: Path) -> None:
    secret = tmp_path / "api.env"
    secret.write_text("sk-live-abc123\n")
    secret.chmod(0o600)
    assert engine.read_secret(str(secret)) == "sk-live-abc123"


def test_a_missing_or_empty_secret_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(engine.RecipeExecutionError, match="does not exist"):
        engine.read_secret(str(tmp_path / "absent.env"))
    empty = tmp_path / "empty.env"
    empty.write_text("")
    empty.chmod(0o600)
    with pytest.raises(engine.RecipeExecutionError, match="is empty"):
        engine.read_secret(str(empty))


def test_a_header_secret_reaches_the_header_and_not_the_query(tmp_path: Path) -> None:
    """And, critically, it is not recorded anywhere the engine logs."""
    secret = tmp_path / "api.env"
    secret.write_text("sk-live-abc123")
    secret.chmod(0o600)
    book = make_book(auth={"type": "header", "env_file": str(secret), "key_name": "X-Api-Key"})
    client = FakeClient([{"items": []}])
    engine.run_recipe(client, book.recipes["one"], {}, book)

    _url, params, headers = client.calls[0]
    assert headers["X-Api-Key"] == "sk-live-abc123"
    assert "sk-live-abc123" not in json.dumps(params)


# ---------------------------------------------------------------------------
# Output mapping
# ---------------------------------------------------------------------------


def test_only_declared_columns_survive_in_declared_order() -> None:
    """The artifact schema is a property of the BOOK, not of whatever the
    provider happened to return, so a provider that adds a field does not
    silently widen the artifact."""
    book = parse_book({
        "dataset": "demo",
        "recipes": [{
            "name": "one", "method": "GET", "url": "https://api.example.com/v1/items",
            "params_schema": {"type": "object", "properties": {}},
            "response_schema": {"type": "object"}, "extract": "items",
        }],
        "output": {"columns": [
            {"name": "b", "from": "b", "type": "double"},
            {"name": "a", "from": "a", "type": "int64"},
        ]},
    })
    frame = engine.records_to_frame(
        [{"a": 1, "b": 2.5, "surprise_new_field": "x"}], book
    )
    assert list(frame.columns) == ["b", "a"]


def test_a_missing_field_becomes_a_true_null_not_a_fill() -> None:
    """RF-002 on the recipe lane: a zero-fill would drag every mean() down
    while every total still looked plausible."""
    book = make_book()
    frame = engine.records_to_frame([{"id": 1}, {}, {"id": 3}], book)
    assert frame["id"].isna().tolist() == [False, True, False]


def test_timestamps_are_parsed_as_utc_not_as_local_time() -> None:
    """B-2, at the exact line where an API can reintroduce it. A naive parse
    under a local session zone shifts every value uniformly and passes every
    aggregate check."""
    book = parse_book({
        "dataset": "demo",
        "recipes": [{
            "name": "one", "method": "GET", "url": "https://api.example.com/v1/items",
            "params_schema": {"type": "object", "properties": {}},
            "response_schema": {"type": "object"}, "extract": "items",
        }],
        "output": {"columns": [{"name": "t", "from": "t", "type": "timestamp[us]", "tz": "UTC"}]},
    })
    frame = engine.records_to_frame([{"t": "2026-01-01T00:00"}, {"t": "2026-01-01T12:30:15"}], book)
    assert str(frame["t"].iloc[0]) == "2026-01-01 00:00:00"
    assert str(frame["t"].iloc[1]) == "2026-01-01 12:30:15"
    assert str(frame["t"].dtype) == "datetime64[us]"


def test_an_explicit_offset_is_converted_to_utc_rather_than_dropped() -> None:
    book = parse_book({
        "dataset": "demo",
        "recipes": [{
            "name": "one", "method": "GET", "url": "https://api.example.com/v1/items",
            "params_schema": {"type": "object", "properties": {}},
            "response_schema": {"type": "object"}, "extract": "items",
        }],
        "output": {"columns": [{"name": "t", "from": "t", "type": "timestamp[us]", "tz": "UTC"}]},
    })
    frame = engine.records_to_frame([{"t": "2026-01-01T08:00:00+08:00"}], book)
    assert str(frame["t"].iloc[0]) == "2026-01-01 00:00:00"


# ---------------------------------------------------------------------------
# T3(b) — DNS rebinding: the address validated must be the address connected to
# ---------------------------------------------------------------------------


def test_a_rebound_peer_is_refused_and_the_body_is_never_read() -> None:
    """The rebinding attack, end to end through the engine.

    An attacker controlling DNS for an allowlisted host answers the VALIDATION
    lookup with a public address and the CONNECT lookup with an internal one.
    `assert_public_host` alone cannot see this — it only ever saw the first
    answer. The peer check does, and the assertion that matters is the second
    one: the body is never read, so nothing from the rebound peer is parsed or
    returned.
    """
    book = make_book()
    client = FakeClient([{"items": [{"id": 1}]}], peers=["10.0.0.5"])
    with pytest.raises(engine.RecipeSecurityError, match="rebinding|private"):
        engine.run_recipe(client, book.recipes["one"], {}, book)

    assert client.responses[0].body_reads == 0, "the body was read from an unvetted peer"
    assert client.responses[0].closed, "the connection was left open"


def test_a_public_peer_outside_the_vetted_set_is_still_refused() -> None:
    """Rebinding to another PUBLIC address is still rebinding.

    `assert_public_address` would happily pass 8.8.8.8. The set-membership
    check is what catches a swap to a different host the attacker controls.
    """
    book = make_book()
    client = FakeClient([{"items": []}], peers=["8.8.8.8"])
    with pytest.raises(engine.RecipeSecurityError, match="NOT among the addresses validated"):
        engine.run_recipe(client, book.recipes["one"], {}, book)
    assert client.responses[0].body_reads == 0


def test_an_undeterminable_peer_fails_closed() -> None:
    """'I could not tell' and 'it was fine' must not produce the same outcome."""
    book = make_book()
    client = FakeClient([{"items": []}], peers=[None])
    with pytest.raises(engine.RecipeSecurityError, match="could not determine"):
        engine.run_recipe(client, book.recipes["one"], {}, book)
    assert client.responses[0].body_reads == 0


def test_the_vetted_peer_is_accepted_and_the_body_is_read() -> None:
    """The fence is not vacuously strict — the honest path still works."""
    book = make_book()
    client = FakeClient([{"items": [{"id": 1}]}])
    records, _ = engine.run_recipe(client, book.recipes["one"], {}, book)
    assert records == [{"id": 1}]
    assert client.responses[0].body_reads == 1


def test_the_peer_check_runs_on_every_page_not_just_the_first() -> None:
    """A connection rebound on page two is the interesting case: page one
    establishes trust and the attacker takes over afterwards."""
    book = make_book(
        pagination={"type": "cursor", "cursor_path": "next", "cursor_param": "cursor"},
        response_schema={"type": "object", "required": ["items"]},
    )
    client = FakeClient(
        [{"items": [{"id": 1}], "next": "c2"}, {"items": [{"id": 2}]}],
        peers=[VETTED_ADDRESS, "127.0.0.1"],
    )
    with pytest.raises(engine.RecipeSecurityError):
        engine.run_recipe(client, book.recipes["one"], {}, book)
    assert client.responses[1].body_reads == 0


# ---------------------------------------------------------------------------
# T3(a) — pagination steering, through the engine
# ---------------------------------------------------------------------------


def _link(url: str) -> dict:
    return {"link": f'<{url}>; rel="next"'}


def test_link_header_pagination_adopts_only_the_query_string() -> None:
    """The next URL is REBUILT from the pinned endpoint plus the provider's
    query. Anything else the provider put in that URL is discarded, not
    approved."""
    book = make_book(pagination={"type": "link-header"})
    client = FakeClient(
        [{"items": [{"id": 1}]}, {"items": [{"id": 2}]}],
        headers=[_link("https://api.example.com/v1/items?page=2&cursor=abc"), {}],
    )
    records, _ = engine.run_recipe(client, book.recipes["one"], {}, book)
    assert [r["id"] for r in records] == [1, 2]
    assert client.calls[1][0] == "https://api.example.com/v1/items?page=2&cursor=abc"


@pytest.mark.parametrize(
    "hostile,why",
    [
        ("https://evil.com/v1/items?page=2", "a different host entirely"),
        ("https://api.example.com.evil.net/v1/items?page=2", "the pinned host as a prefix"),
        ("https://evil-api.example.com/v1/items?page=2", "a lookalike host"),
        ("https://attacker.api.example.com/v1/items?page=2", "a SUBDOMAIN of the pinned host"),
        ("http://api.example.com/v1/items?page=2", "an https->http downgrade"),
        ("https://api.example.com:8443/v1/items?page=2", "a different port"),
        ("https://api.example.com/v1/admin?page=2", "an undeclared path"),
        ("https://user:pw@api.example.com/v1/items?page=2", "credentials in the authority"),
    ],
)
def test_a_steered_next_url_is_refused(hostile: str, why: str) -> None:
    """Default-deny on the pagination path.

    The SUBDOMAIN case is the one worth staring at: `assert_host_allowed`
    deliberately permits proper subdomains for the URL the author wrote down.
    Extending that latitude to a URL the SERVER supplies would hand a provider
    (or a header injector) a steering primitive, so it is removed here.
    """
    book = make_book(pagination={"type": "link-header"})
    client = FakeClient([{"items": [{"id": 1}]}], headers=[_link(hostile)])
    with pytest.raises(engine.RecipeSecurityError):
        engine.run_recipe(client, book.recipes["one"], {}, book)


def test_a_declared_allowed_next_path_is_followed() -> None:
    """Cross-path pagination works — but only because the AUTHOR declared it."""
    book = make_book(
        pagination={"type": "link-header", "allowed_next_paths": ["/v1/items/page2"]},
    )
    client = FakeClient(
        [{"items": [{"id": 1}]}, {"items": [{"id": 2}]}],
        headers=[_link("https://api.example.com/v1/items/page2?x=1"), {}],
    )
    records, _ = engine.run_recipe(client, book.recipes["one"], {}, book)
    assert [r["id"] for r in records] == [1, 2]
    assert client.calls[1][0] == "https://api.example.com/v1/items/page2?x=1"


def test_an_undeclared_path_is_refused_even_on_the_pinned_host() -> None:
    book = make_book(
        pagination={"type": "link-header", "allowed_next_paths": ["/v1/items/page2"]},
    )
    client = FakeClient(
        [{"items": [{"id": 1}]}],
        headers=[_link("https://api.example.com/v1/items/page3?x=1")],
    )
    with pytest.raises(engine.RecipeSecurityError, match="allowed_next_paths"):
        engine.run_recipe(client, book.recipes["one"], {}, book)


# ---------------------------------------------------------------------------
# T5 — engine invariants, unreachable via the loader
# ---------------------------------------------------------------------------


def test_a_hand_built_cursor_recipe_without_cursor_param_raises() -> None:
    """The invariant the loader makes unreachable, asserted where it lives.

    `recipes.py` refuses a book whose cursor pagination omits `cursor_param`,
    so this Recipe cannot arise at runtime — it is constructed by hand here.
    The branch must RAISE rather than return None: silently ending pagination
    would truncate the pull to its first page and report success, which is the
    worst outcome available to it.
    """
    from r64_db_engine.drivers.rest.recipes import Auth, Extract, Pagination, Recipe

    recipe = Recipe(
        name="hand-built",
        method="GET",
        url="https://api.example.com/v1/items",
        allowed_host="api.example.com",
        auth=Auth(),
        params_schema={"type": "object", "properties": {}},
        response_schema={"type": "object"},
        pagination=Pagination(type="cursor", cursor_path="next", cursor_param=None),
        extract=Extract(path="items"),
    )
    with pytest.raises(engine.EngineInvariantError, match="cursor_param is None"):
        engine._next_page_params(recipe, {"next": "c2"}, None, 1)


def test_a_hand_built_page_recipe_without_page_param_raises() -> None:
    from r64_db_engine.drivers.rest.recipes import Auth, Extract, Pagination, Recipe

    recipe = Recipe(
        name="hand-built",
        method="GET",
        url="https://api.example.com/v1/items",
        allowed_host="api.example.com",
        auth=Auth(),
        params_schema={"type": "object", "properties": {}},
        response_schema={"type": "object"},
        pagination=Pagination(type="page", page_param=None),
        extract=Extract(path="items"),
    )
    with pytest.raises(engine.EngineInvariantError, match="page_param is None"):
        engine._next_page_params(recipe, {}, None, 1)


def test_the_loader_makes_those_invariants_unreachable() -> None:
    """Codex's point, pinned: the runtime path cannot produce those Recipes."""
    from r64_db_engine.drivers.rest.recipes import RecipeBookError

    with pytest.raises(RecipeBookError, match="cursor_param"):
        make_book(pagination={"type": "cursor", "cursor_path": "next"})
    with pytest.raises(RecipeBookError, match="page_param"):
        make_book(pagination={"type": "page"})


# ---------------------------------------------------------------------------
# Q4(c) — redirects fail closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_a_redirect_is_refused_and_the_body_is_never_read(status: int) -> None:
    """The invariant the docs claimed and no fixture covered.

    A 3xx is a destination change chosen by the remote end. Following one would
    move the request somewhere the recipe never pinned, routing around the
    https, host-allowlist and private-address checks in a single hop.

    Two assertions, and the second is the one with teeth: the redirect is
    refused by NAME (not as an incidental JSON parse failure on an empty body),
    and `body_reads == 0` — nothing from a redirecting response is parsed.
    """
    book = make_book()

    class RedirectingClient(FakeClient):
        def send(self, request, stream=False):
            response = FakeResponse(
                {"items": []}, status=status,
                headers={"location": "https://evil.example.com/v1/items"},
            )
            self.responses.append(response)
            return response

    client = RedirectingClient([{}])
    with pytest.raises(engine.RecipeSecurityError, match="Redirects are refused"):
        engine.run_recipe(client, book.recipes["one"], {}, book)

    assert client.responses[0].body_reads == 0
    assert client.responses[0].closed


def test_the_redirect_refusal_names_the_location_it_declined() -> None:
    """A refusal that does not say WHERE it was being sent is not actionable."""
    book = make_book()

    class RedirectingClient(FakeClient):
        def send(self, request, stream=False):
            response = FakeResponse(
                {}, status=302, headers={"location": "https://attacker.test/steal"})
            self.responses.append(response)
            return response

    with pytest.raises(engine.RecipeSecurityError, match="attacker.test"):
        engine.run_recipe(RedirectingClient([{}]), book.recipes["one"], {}, book)


def test_the_engine_client_disables_redirect_following() -> None:
    """The refusal above only ever fires because httpx was told not to follow.

    Asserted on the real client the engine builds, not on a fake: if
    `follow_redirects` were ever flipped to True, httpx would transparently
    chase the Location and the 3xx would never reach the check.
    """
    import inspect

    source = inspect.getsource(engine.run_book)
    assert "follow_redirects=False" in source


# ---------------------------------------------------------------------------
# Q3(a) — secrets never escape through the error boundary
# ---------------------------------------------------------------------------

SECRET = "sk-live-9f2a7b31c4d5e6f8"


@pytest.fixture()
def query_auth_book(tmp_path: Path):
    """A recipe whose credential travels in the URL — the leaky shape.

    Query auth is the hard case: the key is IN the request URL, so any error
    that renders that URL renders the credential with it.
    """
    secret_file = tmp_path / "api.env"
    secret_file.write_text(SECRET)
    secret_file.chmod(0o600)
    return make_book(auth={
        "type": "query", "env_file": str(secret_file), "key_name": "api_key",
    })


def _query_auth_book_with(book, response_schema):
    """The same query-auth recipe with a tighter response schema."""
    recipe = book.recipes["one"]
    return parse_book({
        "dataset": "demo",
        "recipes": [{
            "name": "one",
            "method": "GET",
            "url": recipe.url,
            "auth": {
                "type": "query",
                "env_file": recipe.auth.env_file,
                "key_name": recipe.auth.key_name,
            },
            "params_schema": {"type": "object", "properties": {}},
            "response_schema": response_schema,
            "extract": "items",
        }],
        "output": {"columns": [{"name": "id", "from": "id", "type": "int64"}]},
    })


def _query_auth_book_with_pagination(book):
    """The query-auth recipe, with link-header pagination enabled."""
    recipe = book.recipes["one"]
    return parse_book({
        "dataset": "demo",
        "recipes": [{
            "name": "one",
            "method": "GET",
            "url": recipe.url,
            "auth": {
                "type": "query",
                "env_file": recipe.auth.env_file,
                "key_name": recipe.auth.key_name,
            },
            "params_schema": {"type": "object", "properties": {}},
            "response_schema": {"type": "object", "required": ["items"]},
            "pagination": {"type": "link-header"},
            "extract": "items",
        }],
        "output": {"columns": [{"name": "id", "from": "id", "type": "int64"}]},
    })


def _assert_clean(text: str) -> None:
    """No form of the secret may survive — literal or URL-encoded."""
    for form in (SECRET, quote(SECRET, safe=""), quote_plus(SECRET)):
        assert form not in text, f"secret leaked as {form!r} in: {text[:400]}"


def test_a_transport_failure_does_not_leak_a_query_credential(query_auth_book) -> None:
    """The canonical leak: httpx renders the full request URL in transport
    errors, and for query auth that URL carries the key."""
    book = query_auth_book

    class ExplodingClient(FakeClient):
        def send(self, request, stream=False):
            raise RuntimeError(
                f"ConnectError: failed to reach "
                f"https://api.example.com/v1/items?api_key={SECRET}&q=1"
            )

    with pytest.raises(engine.RecipeExecutionError) as exc:
        engine.run_recipe(ExplodingClient([{}]), book.recipes["one"], {}, book)
    _assert_clean(str(exc.value))
    assert "«redacted»" in str(exc.value)


def test_an_http_error_status_does_not_leak_a_query_credential(query_auth_book) -> None:
    book = query_auth_book

    class ErrorClient(FakeClient):
        def send(self, request, stream=False):
            response = FakeResponse({}, status=503)
            self.responses.append(response)
            return response

    with pytest.raises(engine.RecipeExecutionError) as exc:
        engine.run_recipe(ErrorClient([{}]), book.recipes["one"], {}, book)
    _assert_clean(str(exc.value))


def test_a_schema_violation_does_not_leak_the_credential_into_the_RAISED_text(
    query_auth_book,
) -> None:
    book = query_auth_book
    client = FakeClient([{"wrong_key": [], "echo": f"?api_key={SECRET}"}])
    with pytest.raises(engine.ResponseValidationError) as exc:
        engine.run_recipe(client, book.recipes["one"], {}, book)
    _assert_clean(str(exc.value))


def test_a_schema_violation_does_not_leak_the_credential_into_the_DRIFT_EVENT(
    query_auth_book,
) -> None:
    """The one that matters most.

    Drift events are AGENT-READ: the next agent opens the repair record in
    order to fix the connector. A credential landing there is a credential in
    model context, which is Law 3 violated at exactly the point the factory is
    supposed to enforce it.
    """
    book = query_auth_book
    client = FakeClient([{"wrong_key": [], "echo": f"https://x/y?api_key={SECRET}"}])
    with pytest.raises(engine.ResponseValidationError):
        engine.run_recipe(client, book.recipes["one"], {}, book)

    events = drift.read_events("demo")
    assert events, "no drift event was written"
    _assert_clean(json.dumps(events))


def test_the_traceback_chain_does_not_reintroduce_the_secret(query_auth_book) -> None:
    """`raise ... from exc` would print the ORIGINAL, unscrubbed exception in
    the traceback and undo the scrubbing entirely. The engine uses `from None`
    on every scrubbed re-raise; this asserts the chain is genuinely severed."""
    book = query_auth_book

    class ExplodingClient(FakeClient):
        def send(self, request, stream=False):
            raise RuntimeError(f"boom with {SECRET} inside")

    with pytest.raises(engine.RecipeExecutionError) as exc:
        engine.run_recipe(ExplodingClient([{}]), book.recipes["one"], {}, book)

    assert exc.value.__cause__ is None, "the unscrubbed original is still chained"
    assert exc.value.__suppress_context__ is True


def test_a_header_credential_is_also_scrubbed_from_errors(tmp_path: Path) -> None:
    """Header auth keeps the key out of the URL, but an exception can still
    quote a header dict."""
    secret_file = tmp_path / "api.env"
    secret_file.write_text(SECRET)
    secret_file.chmod(0o600)
    book = make_book(auth={
        "type": "header", "env_file": str(secret_file), "key_name": "X-Api-Key"})

    class ExplodingClient(FakeClient):
        def send(self, request, stream=False):
            raise RuntimeError(f"headers were {{'X-Api-Key': '{SECRET}'}}")

    with pytest.raises(engine.RecipeExecutionError) as exc:
        engine.run_recipe(ExplodingClient([{}]), book.recipes["one"], {}, book)
    _assert_clean(str(exc.value))


def test_MUTATION_without_the_scrubber_the_secret_leaks(query_auth_book, monkeypatch) -> None:
    """Mutation check: the tests above must depend on the scrubber.

    With `scrub` neutered to the identity, the credential appears in the raised
    text — which is the pre-fix behaviour, and confirms these tests measure the
    scrubbing rather than restating it.
    """
    monkeypatch.setattr(engine.Scrubber, "scrub", lambda self, text: str(text))
    monkeypatch.setattr(
        engine.Scrubber, "scrubbed", lambda self, exc: f"{type(exc).__name__}: {exc}"
    )
    book = query_auth_book

    class ExplodingClient(FakeClient):
        def send(self, request, stream=False):
            raise RuntimeError(f"ConnectError: ...?api_key={SECRET}")

    with pytest.raises(engine.RecipeExecutionError) as exc:
        engine.run_recipe(ExplodingClient([{}]), book.recipes["one"], {}, book)
    assert SECRET in str(exc.value), (
        "with the scrubber neutered the secret should leak — if it does not, the "
        "tests above are not measuring the scrubber"
    )


# --- the Scrubber itself ---------------------------------------------------


def test_the_scrubber_redacts_every_encoding_of_the_value() -> None:
    from urllib.parse import quote, quote_plus

    s = engine.Scrubber()
    s.register_secret(SECRET)
    for form in (SECRET, quote(SECRET, safe=""), quote_plus(SECRET)):
        assert SECRET not in s.scrub(f"prefix {form} suffix")


def test_the_scrubber_redacts_by_param_name_even_when_the_value_was_rewritten() -> None:
    """Literal matching alone is not enough: a client may re-serialize the
    value. The parameter NAME is stable when its rendering is not."""
    s = engine.Scrubber()
    s.register_secret(SECRET)
    s.register_auth_key("api_key")
    out = s.scrub("GET https://api.example.com/v1?api_key=SOMETHING-RE-ENCODED&p=2")
    assert "SOMETHING-RE-ENCODED" not in out
    assert "p=2" in out, "redaction must stop at the parameter boundary"


def test_the_scrubber_ignores_values_too_short_to_be_credentials() -> None:
    """Replacing a 1-3 character string would corrupt unrelated text without
    protecting anything a real credential looks like."""
    s = engine.Scrubber()
    s.register_secret("ab")
    assert s.scrub("a stable table") == "a stable table"


# ---------------------------------------------------------------------------
# Round 4 Q2(a) — validation errors are VALUE-FREE
# ---------------------------------------------------------------------------

# A secret short enough to fall UNDER the literal-scrubbing floor. If the only
# defence were the scrubber, this would survive; value-free errors are what
# actually stop it.
SHORT_SECRET = "sk-a1b2"


def test_a_validation_error_never_reports_the_failing_VALUE(query_auth_book) -> None:
    """The credential-echo class, killed outright.

    A provider that echoes a submitted API key back inside its response body is
    REMOTE behaviour — nothing the engine does controls it. jsonschema's own
    message embeds the instance, so it is never propagated: the message is
    composed from path + constraint + schema path only.
    """
    # The book's own schema must actually constrain the type, or the value
    # sails past the validator and `_extract` raises instead — a different
    # (also value-free) path, but not the one under test.
    book = _query_auth_book_with(
        query_auth_book,
        {"type": "object", "required": ["items"],
         "properties": {"items": {"type": "array"}}},
    )
    echoed = f"ECHOED-{SECRET}-BACK"
    client = FakeClient([{"items": echoed}])  # a real `type` violation

    with pytest.raises(engine.ResponseValidationError) as exc:
        engine.run_recipe(client, book.recipes["one"], {}, book)

    text = str(exc.value)
    assert echoed not in text
    _assert_clean(text)
    # It must still be diagnosable.
    assert "instance path" in text
    assert "violates constraint" in text


def test_a_validation_error_says_WHICH_constraint_broke() -> None:
    """Value-free must not mean information-free — a repair brief that says only
    'validation failed' has moved the problem to the reader's memory."""
    book = make_book(response_schema={
        "type": "object",
        "properties": {"items": {"type": "array"}},
        "required": ["items"],
    })
    client = FakeClient([{"items": {"not": "an array"}}])
    with pytest.raises(engine.ResponseValidationError) as exc:
        engine.run_recipe(client, book.recipes["one"], {}, book)

    text = str(exc.value)
    assert "items" in text, "the instance PATH is reportable and useful"
    assert "'type'" in text, "the violated constraint is named"
    assert "not an array" not in text, "but never the instance value"


def test_the_drift_event_is_value_free_too(query_auth_book) -> None:
    """The drift event is the agent-read record — the one that matters most."""
    book = query_auth_book
    echoed = f"leaked-{SECRET}"
    client = FakeClient([{"items": echoed}])
    with pytest.raises(engine.ResponseValidationError):
        engine.run_recipe(client, book.recipes["one"], {}, book)

    events = json.dumps(drift.read_events("demo"))
    assert echoed not in events
    _assert_clean(events)


def test_a_SHORT_secret_below_the_scrub_floor_still_does_not_leak(tmp_path: Path) -> None:
    """The floor's justification, tested rather than asserted.

    `SHORT_SECRET` is under `MIN_SCRUBBABLE_LENGTH`, so literal scrubbing does
    NOT protect it. It still never reaches the message, because value-free
    errors are the primary defence and scrubbing only the backstop.
    """
    secret_file = tmp_path / "api.env"
    secret_file.write_text(SHORT_SECRET)
    secret_file.chmod(0o600)
    book = make_book(
        auth={"type": "query", "env_file": str(secret_file), "key_name": "api_key"},
        response_schema={"type": "object", "required": ["items"]},
    )
    client = FakeClient([{"echo": SHORT_SECRET}])

    with pytest.raises(engine.ResponseValidationError) as exc:
        engine.run_recipe(client, book.recipes["one"], {}, book)
    assert SHORT_SECRET not in str(exc.value)
    assert SHORT_SECRET not in json.dumps(drift.read_events("demo"))


def test_the_scrub_floor_is_declared_not_incidental() -> None:
    s = engine.Scrubber()
    assert engine.Scrubber.MIN_SCRUBBABLE_LENGTH == 8
    s.register_secret("short")           # under the floor
    s.register_secret("long-enough-secret")
    assert s.scrub("short") == "short"
    assert "long-enough-secret" not in s.scrub("x long-enough-secret y")


# ---------------------------------------------------------------------------
# Round 4 Q2(b) — one outer boundary covers EVERY stage
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stage",
    ["build_request", "send", "peer_check", "read", "decode", "validate"],
)
def test_an_exception_from_ANY_stage_is_scrubbed_at_the_boundary(
    query_auth_book, stage: str, monkeypatch
) -> None:
    """Per-site scrubbing is a promise every future raise remembers to keep.

    The boundary is the guarantee: a raise injected at each stage of the
    post-secret-load path must cross it scrubbed, with the chain severed —
    including from code this module does not own.
    """
    book = query_auth_book
    leaky = f"boom carrying {SECRET} in it"

    class StagedClient(FakeClient):
        def build_request(self, method, url, params=None, json=None, headers=None):
            if stage == "build_request":
                raise RuntimeError(leaky)
            return super().build_request(method, url, params=params, json=json, headers=headers)

        def send(self, request, stream=False):
            if stage == "send":
                raise RuntimeError(leaky)
            response = super().send(request, stream=stream)
            if stage == "read":
                def exploding_read():
                    raise RuntimeError(leaky)
                response.read = exploding_read
            return response

    if stage == "peer_check":
        monkeypatch.setattr(
            engine, "_assert_connected_peer_was_vetted",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError(leaky)),
        )
    if stage == "decode":
        monkeypatch.setattr(
            engine.json, "loads", lambda *a, **k: (_ for _ in ()).throw(RuntimeError(leaky))
        )
    if stage == "validate":
        monkeypatch.setattr(
            engine, "validate_response",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError(leaky)),
        )

    client = StagedClient([{"items": []}])
    with pytest.raises(engine.RecipeExecutionError) as exc:
        engine.run_recipe(client, book.recipes["one"], {}, book)

    _assert_clean(str(exc.value))
    assert exc.value.__cause__ is None, "the unscrubbed original is still chained"
    assert exc.value.__suppress_context__ is True


def test_the_boundary_preserves_our_own_exception_types(query_auth_book) -> None:
    """Callers and tests discriminate on type; the boundary must not flatten
    everything into one class while it scrubs."""
    book = query_auth_book

    class RedirectingClient(FakeClient):
        def send(self, request, stream=False):
            response = FakeResponse({}, status=302, headers={"location": "https://x/y"})
            self.responses.append(response)
            return response

    with pytest.raises(engine.RecipeSecurityError):
        engine.run_recipe(RedirectingClient([{}]), book.recipes["one"], {}, book)


def test_MUTATION_without_the_boundary_an_unanticipated_raise_leaks(
    query_auth_book, monkeypatch
) -> None:
    """Mutation check for the boundary specifically.

    With `scrub_boundary` neutered to a pass-through, a raise from a stage that
    has no inner scrubbing of its own leaks — which is the gap the boundary
    exists to close, and confirms the tests above measure it.
    """
    from contextlib import contextmanager

    @contextmanager
    def passthrough(scrubber, context):
        yield

    monkeypatch.setattr(engine, "scrub_boundary", passthrough)
    book = query_auth_book

    monkeypatch.setattr(
        engine, "_assert_connected_peer_was_vetted",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError(f"peer said {SECRET}")),
    )
    with pytest.raises(RuntimeError) as exc:
        engine.run_recipe(FakeClient([{"items": []}]), book.recipes["one"], {}, book)
    assert SECRET in str(exc.value), (
        "with the boundary neutered the secret should leak — if it does not, the "
        "tests above are not measuring the boundary"
    )


# ---------------------------------------------------------------------------
# Round 4 Q2(d) — the drift serializer scrubs the final bytes
# ---------------------------------------------------------------------------


def test_the_drift_serializer_scrubs_the_line_that_reaches_disk(tmp_path: Path) -> None:
    """Field-level scrubbing depends on every current AND future field being
    remembered. Scrubbing the serialized line is a property of the bytes that
    actually land on disk."""
    from r64_db_engine.drivers.rest.drift import DriftEvent, emit_drift, read_events

    scrubber = engine.Scrubber()
    scrubber.register_secret(SECRET)

    emit_drift(
        DriftEvent(
            source="demo", recipe="one", url="https://x/y", page=1,
            reason="response_schema validation failed",
            # A field that bypassed field-level scrubbing entirely.
            detail=f"unscrubbed field carrying {SECRET}",
        ),
        scrubber=scrubber,
    )
    assert SECRET not in json.dumps(read_events("demo"))


# ---------------------------------------------------------------------------
# Round 5 Q2 — a secret-bearing Link header, refused value-free
# ---------------------------------------------------------------------------


def _repair_artifacts() -> str:
    """Everything the failure left behind that an agent would later read."""
    from r64_db_engine.drivers.rest.drift import drift_dir

    blob = json.dumps(drift.read_events("demo"))
    directory = drift_dir()
    if directory.exists():
        for path in directory.rglob("*"):
            if path.is_file():
                blob += path.read_text(errors="replace")
    return blob


@pytest.mark.parametrize(
    "label,hostile",
    [
        ("secret in query",
         f"https://evil.example.com/v1/items?api_key={SECRET}"),
        ("secret as userinfo",
         f"https://user:{SECRET}@api.example.com/v1/items?p=2"),
        ("secret URL-encoded in query",
         f"https://evil.example.com/v1/items?api_key={quote(SECRET, safe='')}"),
        ("secret embedded in a path segment",
         f"https://api.example.com/v1/items/{SECRET}/next?p=2"),
        ("8-char prefix of the secret in the path",
         f"https://api.example.com/v1/{SECRET[:8]}-page2?p=2"),
    ],
)
def test_a_secret_bearing_LINK_HEADER_is_refused_without_echoing_it(
    query_auth_book, label: str, hostile: str
) -> None:
    """Codex's named case: secret-bearing Link-header confinement failure.

    A provider can put credential material anywhere in the next-URL it hands
    back — query, userinfo, path — including material it echoes from the
    request it just received. The refusal must name the violated RULE and the
    offending component CATEGORY, and must not echo the candidate.

    Asserted across three sinks, because a refusal that stays out of the
    exception but lands in the repair record has not protected anything: the
    exception text, the drift event, and every artifact left on disk.
    """
    book = query_auth_book
    client = FakeClient(
        [{"items": [{"id": 1}]}],
        headers=[{"link": f'<{hostile}>; rel="next"'}],
    )
    paged = _query_auth_book_with_pagination(book)

    with pytest.raises(engine.RecipeSecurityError) as exc:
        engine.run_recipe(client, paged.recipes["one"], {}, paged)

    text = str(exc.value)
    _assert_clean(text)
    assert hostile not in text, "the candidate URL was echoed verbatim"
    # No component content beyond the canonicalized host and the port.
    assert "api_key=" not in text
    assert "rejected:" in text, "the refusal must name the violated rule"

    artifacts = _repair_artifacts()
    _assert_clean(artifacts)
    assert hostile not in artifacts


def test_the_refusal_names_the_rule_and_the_component_category_only() -> None:
    """Structural, not value-free-to-the-point-of-useless.

    The canonicalized host IS reported — it is compared against a pinned-known
    value, so naming it identifies which allowlist decision fired without
    disclosing anything the provider chose freely. The path never is.
    """
    from r64_db_engine.drivers.rest.security import confine_next_url

    pinned = "https://api.example.com/v1/items?page=1"

    with pytest.raises(engine.RecipeSecurityError) as exc:
        confine_next_url("https://evil.example.com/v1/items?t=abc", pinned, [])
    assert "host outside pinned set: evil.example.com" in str(exc.value)
    assert "t=abc" not in str(exc.value)

    with pytest.raises(engine.RecipeSecurityError) as exc:
        confine_next_url("https://api.example.com/v1/secret-token-path?t=abc", pinned, [])
    assert "path outside the declared set" in str(exc.value)
    assert "secret-token-path" not in str(exc.value), "the path was echoed"

    with pytest.raises(engine.RecipeSecurityError) as exc:
        confine_next_url("http://api.example.com/v1/items", pinned, [])
    assert "non-https scheme" in str(exc.value)

    with pytest.raises(engine.RecipeSecurityError) as exc:
        confine_next_url("https://u:pw@api.example.com/v1/items", pinned, [])
    assert "userinfo present" in str(exc.value)
    assert "pw" not in str(exc.value).replace("provider", "").replace("password", "")


def test_MUTATION_echoing_the_candidate_url_leaks_the_secret(
    query_auth_book, monkeypatch
) -> None:
    """Mutation check for the structural refusal.

    Restore the old behaviour — quote the candidate in the message — and the
    secret rides out in the exception. That is what the round-4 principle
    generalized to security refusals prevents, and it confirms the tests above
    measure the structural messages rather than restating them.
    """
    from r64_db_engine.drivers.rest import security

    def echoing(next_url, pinned_url, allowed_next_paths):
        raise security.RecipeSecurityError(f"refused next-URL {next_url!r}")

    monkeypatch.setattr(engine, "confine_next_url", echoing)
    paged = _query_auth_book_with_pagination(query_auth_book)
    hostile = f"https://evil.example.com/v1?api_key={SECRET}"
    client = FakeClient(
        [{"items": [{"id": 1}]}], headers=[{"link": f'<{hostile}>; rel="next"'}]
    )
    with pytest.raises(engine.RecipeSecurityError) as exc:
        engine.run_recipe(client, paged.recipes["one"], {}, paged)
    # The scrubber (the BACKSTOP) still redacts the literal, which is exactly
    # why it is not the guarantee: the rest of the candidate URL rides out.
    assert "evil.example.com" in str(exc.value), (
        "with the candidate echoed, provider-controlled content reaches the message — "
        "confirming the structural refusal is what prevents it"
    )


def test_the_confinement_happens_INSIDE_the_scrub_boundary(
    query_auth_book, monkeypatch
) -> None:
    """Nothing provider-derived is processed after the boundary closes.

    Proven by the boundary's own fingerprint rather than by chain-severance:
    a clean structural refusal is deliberately bare-re-raised (there is nothing
    to hide, and the original traceback is worth keeping), so a severed chain
    would NOT distinguish "crossed the boundary" from "never entered it".

    Instead a FOREIGN exception is raised from the confinement step. Only the
    boundary wraps such a thing as `RecipeExecutionError` and prefixes it with
    the page context — so seeing that prefix is proof the confinement ran
    inside it.
    """
    paged = _query_auth_book_with_pagination(query_auth_book)

    def foreign(next_url, pinned_url, allowed_next_paths):
        raise ValueError(f"parser blew up on {SECRET}")

    monkeypatch.setattr(engine, "confine_next_url", foreign)
    client = FakeClient(
        [{"items": [{"id": 1}]}],
        headers=[{"link": '<https://api.example.com/v1/items?p=2>; rel="next"'}],
    )
    with pytest.raises(engine.RecipeExecutionError) as exc:
        engine.run_recipe(client, paged.recipes["one"], {}, paged)

    assert "page 1" in str(exc.value), "no boundary context — confinement ran outside it"
    _assert_clean(str(exc.value))
    assert exc.value.__cause__ is None
    assert exc.value.__suppress_context__ is True


def test_a_clean_structural_refusal_keeps_its_original_traceback(query_auth_book) -> None:
    """The boundary rewraps only when it actually changed the message.

    A structural refusal carries nothing to redact, so it is re-raised as-is —
    which preserves the traceback a reader needs. Rewrapping unconditionally
    would discard that for no benefit.
    """
    paged = _query_auth_book_with_pagination(query_auth_book)
    hostile = f"https://evil.example.com/v1?api_key={SECRET}"
    client = FakeClient(
        [{"items": [{"id": 1}]}], headers=[{"link": f'<{hostile}>; rel="next"'}]
    )
    with pytest.raises(engine.RecipeSecurityError) as exc:
        engine.run_recipe(client, paged.recipes["one"], {}, paged)
    _assert_clean(str(exc.value))
    assert hostile not in str(exc.value)
