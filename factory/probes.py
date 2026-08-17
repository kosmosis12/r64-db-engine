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

import urllib.request
from typing import Any, Protocol


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


PROBES: dict[str, type] = {
    "clickhouse": ClickHouseHttpProbe,
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

