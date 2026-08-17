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
