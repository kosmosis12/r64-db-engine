"""The recipe engine: the only code in the pull path.

Given a `RecipeBook` (data) this executes it. Nothing here infers anything, and
nothing adapts: the book says which call to make, which parameters exist, how
pages are followed, what the response must look like, and where the records
are. If reality disagrees with the book, that is a REPAIR SIGNAL, not an
invitation to try something else (Law 1).

The rule that follows from it, and the one most worth stating out loud:

> Retry the REQUEST, never the MEANING.

A transport failure may be retried — the same call, unchanged. A response that
fails `response_schema` validation is NEVER retried differently, coerced,
partially salvaged, or fuzzily parsed. It emits a structured repair event,
fires ntfy, and fails. A connector that silently adapted to a provider change
would turn that change into corrupted data that looks completely healthy.
"""

from __future__ import annotations

import json
import logging
import stat
from pathlib import Path
from typing import Any

from r64_db_engine.drivers.rest.drift import DriftEvent, emit_drift
from r64_db_engine.drivers.rest.recipes import Recipe, RecipeBook
from r64_db_engine.drivers.rest.security import (
    RecipeSecurityError,
    assert_destination,
    assert_public_address,
    confine_next_url,
)

log = logging.getLogger(__name__)


class RecipeExecutionError(RuntimeError):
    """A pull failed. Never downgraded to a partial result."""


class ResponseValidationError(RecipeExecutionError):
    """The response did not match `response_schema` — the drift signal."""


class EngineInvariantError(RecipeExecutionError):
    """A Recipe reached the engine in a state the loader would have refused.

    Unreachable through any supported runtime path — `recipes.py` validates
    every book before a Recipe exists. It raises rather than degrading because
    the alternative for each of these branches is a SILENT TRUNCATION: a pull
    that stops after page one and reports success.
    """


def read_secret(env_file: str) -> str:
    """Read a secret from a 0600 file, by path, at call time.

    The value is returned to be placed directly into a request header or query
    parameter and is never logged, never stored on the recipe, and never
    reaches an evidence pack. The caller is responsible for not widening that.

    Permissions are CHECKED rather than assumed: a world-readable secrets file
    is refused, because the credential law is about where secrets live and a
    0644 file does not satisfy it. Refusing is safe here — it happens before
    any call is made.
    """
    path = Path(env_file).expanduser()
    if not path.exists():
        raise RecipeExecutionError(f"auth env_file does not exist: {path}")
    mode = path.stat().st_mode
    if mode & (stat.S_IRGRP | stat.S_IROTH | stat.S_IWGRP | stat.S_IWOTH):
        raise RecipeExecutionError(
            f"auth env_file {path} is group- or world-accessible (mode {oct(mode & 0o777)}). "
            f"Secrets live in 0600 files. Run: chmod 600 {path}"
        )
    value = path.read_text().strip()
    if not value:
        raise RecipeExecutionError(f"auth env_file {path} is empty")
    return value


def dotted_get(doc: Any, path: str) -> Any:
    """Resolve a dotted path with optional [index] segments.

    Deliberately tiny: `results[0].latitude`, `meta.next_cursor`. Not a JSONPath
    implementation and not an expression evaluator — a book must not be able to
    compute, only to locate. Returns None when the path is absent, so callers
    decide whether absence is terminal (a cursor) or a defect (an extract).
    """
    current = doc
    for raw_segment in path.split("."):
        segment, _, rest = raw_segment.partition("[")
        if segment:
            if not isinstance(current, dict) or segment not in current:
                return None
            current = current[segment]
        while rest:
            index_text, _, rest = rest.partition("]")
            rest = rest.lstrip("[")
            try:
                index = int(index_text)
            except ValueError:
                return None
            if not isinstance(current, list) or not -len(current) <= index < len(current):
                return None
            current = current[index]
    return current


def validate_response(recipe: Recipe, payload: Any, book: RecipeBook, page: int) -> None:
    """Per-pull `response_schema` validation. Failure is a repair signal."""
    import jsonschema

    try:
        jsonschema.validate(payload, recipe.response_schema)
    except jsonschema.ValidationError as exc:
        event = DriftEvent(
            source=book.dataset,
            recipe=recipe.name,
            url=recipe.url,
            page=page,
            reason="response_schema validation failed",
            detail=exc.message,
            json_path=list(exc.absolute_path),
            schema_path=list(exc.absolute_schema_path),
        )
        emit_drift(event)
        raise ResponseValidationError(
            f"recipe {recipe.name!r} response failed response_schema at "
            f"{'.'.join(str(p) for p in exc.absolute_path) or '<root>'}: {exc.message}. "
            f"A repair event was written and ntfy fired. This is NOT retried with a "
            f"different interpretation — the provider changed, so the book must be "
            f"re-researched and re-admitted through the battery."
        ) from exc


def _extract(recipe: Recipe, payload: Any) -> list[dict[str, Any]]:
    """Pull the record list out of a response, in either declared shape."""
    node = dotted_get(payload, recipe.extract.path)
    if node is None:
        raise ResponseValidationError(
            f"recipe {recipe.name!r}: extract path {recipe.extract.path!r} is absent from the "
            f"response. The schema admitted it, so the schema is now weaker than the book "
            f"assumes — tighten response_schema as part of the repair."
        )

    if recipe.extract.shape == "records":
        if not isinstance(node, list):
            raise ResponseValidationError(
                f"recipe {recipe.name!r}: extract shape 'records' expects a list at "
                f"{recipe.extract.path!r}, got {type(node).__name__}"
            )
        return [row for row in node if isinstance(row, dict)]

    # Columnar: an object of equal-length parallel arrays, which is what
    # open-meteo returns. Lengths are checked rather than zipped, because
    # `zip` would silently truncate to the shortest column and produce a
    # short, plausible-looking table.
    if not isinstance(node, dict):
        raise ResponseValidationError(
            f"recipe {recipe.name!r}: extract shape 'columnar' expects an object at "
            f"{recipe.extract.path!r}, got {type(node).__name__}"
        )
    arrays = {k: v for k, v in node.items() if isinstance(v, list)}
    if not arrays:
        raise ResponseValidationError(
            f"recipe {recipe.name!r}: no array columns found at {recipe.extract.path!r}"
        )
    lengths = {k: len(v) for k, v in arrays.items()}
    if len(set(lengths.values())) != 1:
        raise ResponseValidationError(
            f"recipe {recipe.name!r}: columnar arrays have differing lengths {lengths}. "
            f"Zipping them would silently truncate to the shortest."
        )
    count = next(iter(lengths.values()))
    return [{k: arrays[k][i] for k in arrays} for i in range(count)]


def _next_page_params(
    recipe: Recipe, payload: Any, response: Any, page: int
) -> dict[str, Any] | None:
    """Parameters for the next page, or None when the sequence is complete."""
    pagination = recipe.pagination
    if pagination.type == "none":
        return None
    # `cursor_param` / `page_param` are Optional on the dataclass but REQUIRED
    # for their own pagination type — `recipes.py` refuses a book that omits
    # them at load, so a None here means a Recipe was constructed bypassing the
    # loader. That is an INVARIANT VIOLATION, and it raises rather than
    # returning None: silently ending pagination would truncate the pull to its
    # first page and report success, which is the worst available outcome.
    # Unreachable through any supported runtime path; the invariant lives in
    # `tests/drivers/rest/test_engine.py` instead.
    if pagination.type == "cursor":
        if pagination.cursor_param is None:
            raise EngineInvariantError(
                f"recipe {recipe.name!r}: pagination.type is 'cursor' but cursor_param is None. "
                f"The loader refuses such a book, so this Recipe was built bypassing it."
            )
        cursor = dotted_get(payload, pagination.cursor_path or "")
        if not cursor:
            return None
        return {pagination.cursor_param: cursor}
    if pagination.type == "page":
        if pagination.page_param is None:
            raise EngineInvariantError(
                f"recipe {recipe.name!r}: pagination.type is 'page' but page_param is None. "
                f"The loader refuses such a book, so this Recipe was built bypassing it."
            )
        params: dict[str, Any] = {pagination.page_param: page + 1}
        if pagination.size_param and pagination.page_size:
            params[pagination.size_param] = pagination.page_size
        return params
    if pagination.type == "link-header":
        return {"__link__": _parse_link_header(response.headers.get("link", ""), pagination.rel)}
    return None


def _parse_link_header(value: str, rel: str) -> str | None:
    """RFC 8288 Link header, only enough of it to find one rel."""
    for part in value.split(","):
        segments = part.split(";")
        if len(segments) < 2:
            continue
        url = segments[0].strip().strip("<>")
        for attribute in segments[1:]:
            key, _, raw = attribute.strip().partition("=")
            if key.strip().lower() == "rel" and raw.strip().strip('"') == rel:
                return url
    return None


def _assert_connected_peer_was_vetted(
    response: Any, vetted: list[str], recipe: Recipe, url: str
) -> None:
    """The address we CONNECTED to must be the address we VALIDATED.

    This closes the DNS-rebinding gap that `assert_public_host` alone leaves
    open: validation resolves a name, then httpx resolves it again to open the
    socket, and an attacker controlling DNS for an allowlisted host can return
    a public address to the first lookup and a private one to the second.

    The check reads the real peer off the live connection
    (`network_stream` -> `server_addr`) and requires it to be BOTH publicly
    routable AND a member of the set validated moments earlier. Fail-closed: an
    address that cannot be determined is refused, not waved through, because
    "I could not tell" and "it was fine" must not produce the same outcome.

    It runs BEFORE `response.read()`, so no body is fetched from an unvetted
    peer and no bytes reach the caller.

    # Residual window, stated rather than implied

    The REQUEST has already been written to that socket by the time a response
    head exists — so on a rebound connection, the request line, headers and any
    API key have reached the attacker before this fires. What is prevented is
    the RESPONSE being trusted, parsed, and turned into pulled data.

    Closing the remaining window means connecting to the vetted IP directly
    while carrying the hostname for TLS SNI and certificate validation. httpx
    has no first-class hook for that; it requires a custom transport wrapping
    httpcore's connection pool, which is disproportionate here and version-
    fragile. The peer-assertion form is what is implemented, and this paragraph
    is why.
    """
    stream = response.extensions.get("network_stream")
    server_addr = stream.get_extra_info("server_addr") if stream is not None else None
    peer = server_addr[0] if isinstance(server_addr, tuple) and server_addr else None

    if not peer:
        raise RecipeSecurityError(
            f"recipe {recipe.name!r}: could not determine the connected peer address for "
            f"{url}, so the validated-address check cannot be made. Refusing fail-closed — "
            f"an unverifiable connection is treated as an unvetted one."
        )

    # Re-checked independently of the vetted set: if the set were ever computed
    # wrongly, this still refuses a private peer.
    assert_public_address(str(peer))

    if str(peer) not in vetted:
        raise RecipeSecurityError(
            f"recipe {recipe.name!r}: connected to {peer}, which is NOT among the addresses "
            f"validated for this request ({sorted(vetted)}). The name resolved differently "
            f"between validation and connection — that is DNS rebinding, and the response "
            f"is refused unread."
        )


def _request(
    client: Any,
    recipe: Recipe,
    url: str,
    params: dict[str, Any],
    book: RecipeBook,
) -> tuple[Any, Any]:
    """One HTTP call, with every destination invariant re-asserted first."""
    # Re-asserted per call rather than trusted from load: the URL being
    # requested is what matters, and a paginated link-header URL in particular
    # arrives from the PROVIDER rather than from the book.
    addresses = assert_destination(url, recipe.allowed_host)
    log.debug("rest_request recipe=%s url=%s resolved=%s", recipe.name, url, addresses)

    headers: dict[str, str] = {}
    query = dict(params)
    if recipe.auth.type != "none":
        secret = read_secret(recipe.auth.env_file or "")
        if recipe.auth.type == "header":
            headers[recipe.auth.key_name or ""] = secret
        else:
            query[recipe.auth.key_name or ""] = secret
        del secret

    # Sent STREAMING so the peer address can be checked before any body is read.
    # `client.get()` reads the body eagerly, which would put the rebinding check
    # after the very thing it is supposed to gate.
    request = client.build_request(
        recipe.method,
        url,
        params=query if recipe.method == "GET" else None,
        json=query if recipe.method != "GET" else None,
        headers=headers,
    )
    response = client.send(request, stream=True)
    try:
        _assert_connected_peer_was_vetted(response, addresses, recipe, url)

        if response.status_code >= 400:
            raise RecipeExecutionError(
                f"recipe {recipe.name!r} returned HTTP {response.status_code} for {url}"
            )
        response.read()
        body = response.content
    finally:
        response.close()
    if len(body) > book.limits.max_response_bytes:
        raise RecipeExecutionError(
            f"recipe {recipe.name!r} response is {len(body)} bytes, over the "
            f"{book.limits.max_response_bytes}-byte cap. An unbounded read is a memory "
            f"bug waiting for a bad day; raise limits.max_response_bytes deliberately "
            f"if the source genuinely returns this much."
        )
    try:
        return json.loads(body), response
    except json.JSONDecodeError as exc:
        raise ResponseValidationError(
            f"recipe {recipe.name!r} returned a body that is not JSON: {exc}"
        ) from exc


def run_recipe(
    client: Any,
    recipe: Recipe,
    params: dict[str, Any],
    book: RecipeBook,
) -> tuple[list[dict[str, Any]], Any]:
    """Execute one recipe, following pagination. Returns (records, first payload).

    The first payload is returned alongside because threading binds against the
    RESPONSE DOCUMENT (`results[0].latitude`), not against extracted records.
    """
    undeclared = set(params) - recipe.declared_params
    if undeclared:
        raise RecipeSecurityError(
            f"recipe {recipe.name!r} was given undeclared input(s) {sorted(undeclared)}. "
            f"Declared: {sorted(recipe.declared_params)}."
        )

    records: list[dict[str, Any]] = []
    first_payload: Any = None
    url = recipe.url
    page_params = {**recipe.static_params, **params}

    for page in range(1, recipe.pagination.max_pages + 1):
        payload, response = _request(client, recipe, url, page_params, book)
        validate_response(recipe, payload, book, page)
        if first_payload is None:
            first_payload = payload
        records.extend(_extract(recipe, payload))

        nxt = _next_page_params(recipe, payload, response, page)
        if not nxt:
            break
        if "__link__" in nxt:
            link = nxt["__link__"]
            if not link:
                break
            # DEFAULT-DENY. The next URL came from the PROVIDER, so it is
            # confined to the recipe's pinned endpoint: same scheme, host and
            # port exactly (no subdomain latitude on this path), the pinned
            # path or a declared `allowed_next_paths` entry, and ONLY the query
            # string adopted. The URL is rebuilt from vetted parts rather than
            # approved in place.
            url = confine_next_url(link, recipe.url, recipe.pagination.allowed_next_paths)
        else:
            page_params = {**page_params, **nxt}
    else:
        raise RecipeExecutionError(
            f"recipe {recipe.name!r} hit the {recipe.pagination.max_pages}-page cap without "
            f"the provider signalling an end. Refusing to return a silently truncated "
            f"result: raise pagination.max_pages deliberately, or narrow the query."
        )

    return records, first_payload


def run_book(book: RecipeBook, client: Any = None) -> list[dict[str, Any]]:
    """Execute the whole book in threading order; return the terminal records.

    Redirects are DISABLED. A 3xx is a destination change decided by the remote
    end, which would route straight around the host allowlist and the
    private-address fence.
    """
    import httpx

    owns_client = client is None
    if owns_client:
        client = httpx.Client(
            timeout=book.limits.timeout_s,
            follow_redirects=False,
            headers={"user-agent": "r64-db-engine/rest-recipe-engine"},
        )
    try:
        payloads: dict[str, Any] = {}
        records: list[dict[str, Any]] = []
        for step in book.threading:
            recipe = book.recipes[step.recipe]
            params = dict(step.params)
            for target, expression in step.bind.items():
                producer, _, rest = expression.partition(".")
                value = dotted_get(payloads.get(producer), rest) if rest else payloads.get(producer)
                if value is None:
                    raise RecipeExecutionError(
                        f"threading step {step.recipe!r}: binding {target}={expression!r} "
                        f"resolved to nothing. The upstream response no longer has that "
                        f"shape — re-research rather than defaulting."
                    )
                params[target] = value
            records, payload = run_recipe(client, recipe, params, book)
            payloads[recipe.name] = payload
        return records
    finally:
        if owns_client:
            client.close()


def records_to_frame(records: list[dict[str, Any]], book: RecipeBook) -> Any:
    """Map extracted records onto the declared output columns.

    Only declared columns survive, in declared order — the artifact schema is a
    property of the BOOK, not of whatever the provider happened to return, so a
    provider that adds a field does not silently widen the artifact.
    """
    import pandas as pd

    data: dict[str, Any] = {}
    for column in book.output:
        values = [row.get(column.source) for row in records]
        if column.type == "int64":
            data[column.name] = pd.array(values, dtype="Int64")
        elif column.type == "double":
            data[column.name] = pd.array(values, dtype="float64")
        elif column.type == "bool":
            data[column.name] = pd.array(values, dtype="boolean")
        elif column.type == "timestamp[us]":
            # UTC without exception. `utc=True` interprets a naive string AS
            # UTC rather than as local time, which is the whole B-2 lesson
            # applied to an API: a naive parse under a local session zone
            # shifts every value uniformly and passes every aggregate check.
            parsed = pd.to_datetime(pd.Series(values, dtype="object"), utc=True, format="ISO8601")
            data[column.name] = parsed.dt.tz_localize(None).astype("datetime64[us]")
        else:
            data[column.name] = pd.array([None if v is None else str(v) for v in values],
                                         dtype="string")
    return pd.DataFrame(data, columns=[c.name for c in book.output])


__all__ = [
    "EngineInvariantError",
    "RecipeExecutionError",
    "ResponseValidationError",
    "dotted_get",
    "read_secret",
    "records_to_frame",
    "run_book",
    "run_recipe",
    "validate_response",
]
