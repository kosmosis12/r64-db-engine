"""Live-source probes: ask the SOURCE what it holds, without using the driver.

# Why this exists at all

The B-2 boundary check compares the artifact's min/max against the source's
own min/max. Getting the source's answer through the driver under test would
let a single defect satisfy both sides of the comparison — a fixture that used
the driver to verify the driver can hide a fault in both directions at once.

So these probes talk to the source over its most primitive interface, with no
engine code and, where possible, no client library either. The ClickHouse probe
is raw HTTP through `urllib`; `clickhouse_connect` — the library the driver
uses — is deliberately not imported.

# Registry shape

`PROBES` + `resolve()` is the same shape as `drivers/__init__.py` and
`sinks/__init__.py`, for the same reason: adding a probe for a new dialect is a
registration, not an edit to a dispatcher. An unregistered dialect is refused
loudly with the registered names listed, so a driver campaign that forgets its
probe gets a clear instruction rather than a skipped check.

# Credentials

A probe receives the dialect's config block and may use a password from it to
authenticate. It must never place one in a returned query string, an
`endpoint` description, or anything else that reaches the evidence pack.
`describe()` returns a credential-free endpoint string for exactly that reason.
"""

from __future__ import annotations

import json
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit


class ProbeError(RuntimeError):
    """The source could not be probed. Never silently downgraded to a skip."""


def _from_clause(source: str) -> str:
    """Render a config `source` as a FROM clause, handling inline SQL.

    A target may pin its scan order by giving `source` as a full `SELECT ...`
    statement rather than a table name — which the clickhouse driver supports
    and the meshbench target uses, because a bare table scan is not
    byte-reproducible. The probe must wrap such a source as a subquery or it
    would emit `FROM SELECT * FROM ...`.

    Detected the same way the driver detects it (a leading `select ` or an
    embedded newline), deliberately: if the probe and the driver disagreed
    about what counts as inline SQL, they would be reading two different
    things and the boundary comparison would be meaningless.
    """
    stripped = source.strip()
    if stripped.lower().startswith("select ") or "\n" in stripped:
        return f"({stripped}) AS sub"
    return stripped


class SourceProbe(Protocol):
    """Minimal read-only interface the battery needs from a live source."""

    def describe(self) -> str:
        """Credential-free endpoint description, safe for the evidence pack."""

    def bounds(self, source: str, column: str) -> tuple[str, str]:
        """(min, max) of `column` in `source`, as source-rendered strings."""

    def session_timezone(self) -> str:
        """The session timezone the source answers in ('' if not applicable)."""

    def queries(self) -> list[str]:
        """Every statement issued so far, for the evidence pack."""


class ClickHouseHttpProbe:
    """ClickHouse over its HTTP interface. No client library involved.

    Statements go by POST. ClickHouse treats an HTTP GET as implicitly readonly
    (`Code: 164 READONLY`), and POST serves SELECT equally well, so there is one
    path rather than two.

    Timestamps are rendered with `toString()` so the comparison is made on the
    source's own rendering rather than on a value that has already been through
    a client's type mapping — the client is one of the things under test.
    """

    def __init__(self, block: dict[str, Any]) -> None:
        self._host = str(block.get("host", "127.0.0.1"))
        self._port = int(block.get("port", 8123))
        self._database = str(block.get("database", "default"))
        self._user = str(block.get("user", "default"))
        # Read but never echoed: it is used to build a request header and is
        # excluded from describe() and from every recorded query.
        self._password = block.get("password") or ""
        self._secure = bool(block.get("secure", False))
        self._issued: list[str] = []

    def describe(self) -> str:
        scheme = "https" if self._secure else "http"
        return f"{scheme}://{self._host}:{self._port}/ (database={self._database}, user={self._user})"

    def _post(self, sql: str) -> str:
        self._issued.append(sql)
        scheme = "https" if self._secure else "http"
        url = f"{scheme}://{self._host}:{self._port}/?database={self._database}"
        req = urllib.request.Request(url, data=sql.encode(), method="POST")
        req.add_header("X-ClickHouse-User", self._user)
        if self._password:
            req.add_header("X-ClickHouse-Key", self._password)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310 - fixed scheme
                return resp.read().decode().strip()
        except Exception as exc:  # noqa: BLE001 - re-raised with the query, never the password
            raise ProbeError(f"ClickHouse probe failed for {sql!r}: {exc}") from exc

    def bounds(self, source: str, column: str) -> tuple[str, str]:
        sql = f"SELECT toString(min({column})), toString(max({column})) FROM {_from_clause(source)}"
        raw = self._post(sql)
        parts = raw.split("\t")
        if len(parts) != 2:
            raise ProbeError(f"expected two tab-separated bounds from {sql!r}, got {raw!r}")
        return parts[0], parts[1]

    def session_timezone(self) -> str:
        return self._post("SELECT timezone()")

    def queries(self) -> list[str]:
        return list(self._issued)


class RestRecipeProbe:
    """Probe an API described by a recipe book, WITHOUT the recipe engine.

    Independence is the whole point of a probe, and it is sharper here than on
    a database. The thing under test is the engine: its threading, its columnar
    extraction, its UTC parsing. So this probe reimplements the minimum needed
    to fetch the same data by a different route — raw `urllib`, `json`, and
    hand-rolled binding — importing neither `httpx`, nor `jsonschema`, nor a
    single function from `drivers/rest/engine.py`.

    If both sides shared the parsing, a timezone bug would shift the artifact
    and the "source truth" by the same eight hours and B-2 would pass while the
    artifact was wrong. That is precisely the failure this check exists to
    catch, so the duplication is deliberate and must not be refactored away.
    """

    def __init__(self, block: dict[str, Any]) -> None:
        book_path = block.get("recipe_book")
        if not book_path:
            raise ProbeError("rest probe requires `recipe_book` in the `rest:` config block")
        # Narrowed to `str` at the boundary, once, rather than re-narrowed (or
        # not) at each use site downstream.
        self._book_path: str = str(book_path)
        self._issued: list[str] = []
        self._book: dict[str, Any] | None = None
        self._terminal: tuple[Any, dict[str, Any]] | None = None

    def _load(self) -> dict[str, Any]:
        import yaml

        if self._book is None:
            path = Path(self._book_path).expanduser()
            if not path.is_absolute():
                path = Path(__file__).resolve().parents[1] / path
            self._book = yaml.safe_load(path.read_text())
        return self._book

    def describe(self) -> str:
        book = self._load()
        hosts = [urlsplit(r["url"]).hostname for r in book.get("recipes", [])]
        return f"recipe book {Path(self._book_path).name} over {', '.join(h or '?' for h in hosts)}"

    def _get(self, url: str, params: dict[str, Any]) -> Any:
        query = urlencode({k: v for k, v in params.items() if v is not None})
        full = f"{url}?{query}" if query else url
        if urlsplit(full).scheme != "https":
            raise ProbeError(f"probe refuses a non-https URL: {full!r}")
        self._issued.append(f"GET {full}")
        try:
            with urllib.request.urlopen(full, timeout=60) as resp:  # noqa: S310 - https asserted
                return json.loads(resp.read().decode())
        except Exception as exc:  # noqa: BLE001
            raise ProbeError(f"rest probe failed for {full}: {exc}") from exc

    def _run_thread(self) -> tuple[Any, dict[str, Any]]:
        """Re-run the book's thread by hand. Returns (terminal payload, recipe).

        Memoized: the boundary check and the timezone read both need the same
        terminal payload, and a live API should be called once per run rather
        than once per question.
        """
        if self._terminal is not None:
            return self._terminal

        book = self._load()
        recipes = {r["name"]: r for r in book["recipes"]}
        payloads: dict[str, Any] = {}
        payload: Any = None
        recipe: dict[str, Any] = {}

        for step in book["threading"]:
            recipe = recipes[step["recipe"]]
            params = dict(recipe.get("static_params") or {})
            params.update(step.get("params") or {})
            for target, expression in (step.get("bind") or {}).items():
                producer, _, rest = expression.partition(".")
                params[target] = _dotted(payloads[producer], rest)
            payload = self._get(recipe["url"], params)
            payloads[recipe["name"]] = payload

        self._terminal = (payload, recipe)
        return self._terminal

    def bounds(self, source: str, column: str) -> tuple[str, str]:
        """min/max of a mapped output column, computed from the raw response."""
        book = self._load()
        mapping = {c["name"]: c["from"] for c in book["output"]["columns"]}
        field = mapping.get(column, column)

        payload, recipe = self._run_thread()
        extract = recipe.get("extract")
        extract_path = extract["path"] if isinstance(extract, dict) else extract
        if not isinstance(extract_path, str):
            raise ProbeError(
                f"recipe {recipe.get('name')!r} has no usable `extract` path; the probe cannot "
                f"locate the records to take bounds from"
            )
        node = _dotted(payload, extract_path)

        if isinstance(node, dict):  # columnar
            values = [v for v in node.get(field, []) if v is not None]
        else:  # records
            values = [row.get(field) for row in node if row.get(field) is not None]
        if not values:
            raise ProbeError(f"rest probe found no values for column {column!r}")
        return _canon(min(values)), _canon(max(values))

    def session_timezone(self) -> str:
        """What the PROVIDER says it answered in — the B-2 fact for an API.

        A database reports a session timezone; an API reports one in its
        payload. Recording it is what makes a matching pair of bounds mean
        something: bounds that agree under a provider which quietly switched to
        local time would agree and both be wrong.
        """
        payload, _ = self._run_thread()
        if isinstance(payload, dict):
            for key in ("timezone", "timezone_abbreviation"):
                if isinstance(payload.get(key), str):
                    return payload[key]
        return ""

    def queries(self) -> list[str]:
        return list(self._issued)


def _dotted(doc: Any, path: str) -> Any:
    current = doc
    for raw in path.split("."):
        segment, _, rest = raw.partition("[")
        if segment:
            current = current[segment]
        while rest:
            index, _, rest = rest.partition("]")
            rest = rest.lstrip("[")
            current = current[int(index)]
    return current


def _canon(value: Any) -> str:
    """Render a bound the way the battery canonicalizes artifact bounds.

    open-meteo emits `2026-01-01T00:00` (no seconds). The artifact side renders
    `%Y-%m-%d %H:%M:%S.%f`, so both are normalized to that here rather than
    compared as the strings each side happens to produce.
    """
    text = str(value)
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f",
                "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d %H:%M:%S.%f")
        except ValueError:
            continue
    return text


PROBES: dict[str, type] = {
    "clickhouse": ClickHouseHttpProbe,
    "rest": RestRecipeProbe,
}


def resolve(dialect: str) -> type:
    try:
        return PROBES[dialect]
    except KeyError as exc:
        available = ", ".join(sorted(PROBES)) or "(none)"
        raise ProbeError(
            f"no live-source probe registered for dialect '{dialect}' (registered: {available}). "
            f"The B-2 boundary check compares the artifact against the LIVE source, so a "
            f"dialect without a probe cannot be admitted — write one rather than skipping it."
        ) from exc


__all__ = ["PROBES", "ClickHouseHttpProbe", "ProbeError", "SourceProbe", "resolve"]

