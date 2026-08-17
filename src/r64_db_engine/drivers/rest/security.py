"""Destination-pinning invariants for the `rest` dialect.

The recipe lane executes calls described by a config file. That makes the config
a control surface, and a control surface that can name a destination is one that
can be pointed somewhere it should not go. These functions are the fence.

Each invariant has a failing fixture in `tests/drivers/rest/test_security.py`
proving the malicious shape is REFUSED — not merely that the benign one is
accepted. A security check with no failing fixture is decoration.

# What is actually enforced

1. **HTTPS only.** No plaintext, ever, and no downgrade.
2. **Destination fixed at authoring.** The URL is pinned when the recipe is
   written; it admits no placeholder, and runtime inputs may populate declared
   query/body parameters only. For that AUTHORED url the host rule permits the
   pinned host or a proper subdomain of it (`assert_host_allowed`).
3. **Every request re-validated** — https, host rule, and public-address —
   at call time, not once at load.
4. **No private address space.** Every RESOLVED address must be publicly
   routable, checked on resolution rather than on spelling.
5. **Pagination confined to the pinned endpoint, query-only by default.** A
   provider-supplied next-URL goes through `confine_next_url`, which requires
   scheme, port and canonicalized host to match the pinned URL *exactly* —
   subdomain latitude is deliberately NOT available here, because this URL comes
   from the server rather than the author — and adopts only the query string,
   rebuilding the URL from vetted parts so the candidate never reaches the
   client. Cross-path pagination requires an explicit `allowed_next_paths`
   declaration in the recipe book; absent it, refused.
8. **Refusals about provider-controlled URLs are STRUCTURAL.** They name the
   violated rule and the offending component CATEGORY, never the content — the
   candidate URL, its query, its path and its userinfo are not reported and not
   recorded. The canonicalized host is the one exception, because it is compared
   against pinned-known values. This is the same principle as value-free
   validation errors, generalized: literal scrubbing is the backstop, never the
   guarantee.
6. **No redirects.** A 302 is a destination change chosen by the remote end.
7. **Rebinding closed at response time.** `engine._assert_connected_peer_was_vetted`
   reads the real peer off the live connection and requires it to be public AND
   in the set validated moments earlier, fail-closed, BEFORE any body is read.

# The residual window, stated plainly

The rebinding check runs once a response head exists, which means **the request
has already been written to the socket**. On a rebound connection the request
line, headers and any API key have therefore reached the attacker before the
check fires. What is prevented is the RESPONSE being trusted, parsed, or turned
into pulled data — not the request leaking.

Closing that last gap means connecting to the vetted IP directly while carrying
the hostname for TLS SNI and certificate validation. httpx exposes no
first-class hook for it; it requires a custom transport wrapping httpcore's
connection pool, which is disproportionate here and fragile across versions.
The peer-assertion form is what is implemented, and this paragraph is why.

Both are recorded rather than papered over, because the failure mode of a
security fence is a reader who believes it covers more than it does.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit


class RecipeSecurityError(ValueError):
    """A recipe or a runtime input violated a destination-pinning invariant.

    A ValueError subclass so config validation reports it like any other bad
    config, and never something a caller might be tempted to catch and retry.
    """


def assert_https(url: str) -> None:
    """Refuse anything that is not https.

    Checked on the recipe as authored AND on the URL actually about to be
    requested, so a mutation applied after load cannot downgrade the scheme.
    """
    scheme = urlsplit(url).scheme.lower()
    if scheme != "https":
        raise RecipeSecurityError(
            f"recipe URL must use https, got {scheme or '<none>'}:// in {url!r}. "
            f"Plaintext is refused outright rather than warned about: a recipe "
            f"carries an API key, and there is no configuration under which "
            f"sending it in the clear is the intended behaviour."
        )


def host_of(url: str) -> str:
    host = urlsplit(url).hostname
    if not host:
        raise RecipeSecurityError(f"recipe URL has no hostname: {url!r}")
    return host.lower().rstrip(".")


def assert_host_allowed(url: str, allowed_host: str) -> None:
    """The URL's host must BE the allowlist host, or a proper subdomain of it.

    The subtle case, and the reason this is not a `str.endswith` call:

        allowed = "checkr.com"
        "api.checkr.com"   -> ALLOWED   (proper subdomain)
        "checkr.com"       -> ALLOWED   (exact)
        "evil-checkr.com"  -> REFUSED   <-- endswith(".checkr.com") is False,
                                            but endswith("checkr.com") is TRUE

    An attacker who can register `evil-checkr.com` defeats suffix matching
    outright, which is why the comparison is on a dot-delimited label boundary.
    `xcheckr.com`, `checkr.com.evil.net` and `notcheckr.com` all fail for the
    same reason.
    """
    host = host_of(url)
    allowed = allowed_host.lower().rstrip(".")
    if host == allowed:
        return
    if host.endswith("." + allowed):
        return
    raise RecipeSecurityError(
        f"recipe host {host!r} is not {allowed!r} nor a subdomain of it. "
        f"The URL is pinned at recipe creation and runtime inputs may populate "
        f"declared body/query parameters only — never the host or the path."
    )


def resolve_addresses(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise RecipeSecurityError(f"could not resolve recipe host {host!r}: {exc}") from exc
    # `sockaddr[0]` is typed `str | int` because the tuple shape differs across
    # address families; for AF_INET/AF_INET6 it is always the address string.
    # Coerced explicitly rather than left implicit, so `assert_public_address`
    # receives what its signature promises.
    return sorted({str(info[4][0]) for info in infos})


def assert_public_address(address: str) -> None:
    """Refuse any address that is not publicly routable.

    This is the SSRF fence. `https://metadata.internal/` and
    `https://localhost/` are refused not because of how they are spelled but
    because of where they RESOLVE, which is the only check an attacker cannot
    spell their way around.

    Every non-global category is refused explicitly rather than relying on
    `is_global` alone, so the reason in the error names the actual category.
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError as exc:
        raise RecipeSecurityError(f"unparseable resolved address {address!r}: {exc}") from exc

    # Order is significant: these categories OVERLAP in Python's ipaddress —
    # 127.0.0.1, 169.254.169.254 and 0.0.0.0 all report `is_private` as well as
    # their own more specific category. Most-specific first, so the refusal
    # names the actual reason ("link-local", which points at cloud metadata)
    # rather than the vaguest true one ("private"). Every branch refuses, so
    # the ordering affects only the message — but the message is what a reader
    # acts on.
    categories = (
        (ip.is_loopback, "loopback"),
        (ip.is_link_local, "link-local"),
        (ip.is_unspecified, "unspecified"),
        (ip.is_multicast, "multicast"),
        (ip.is_reserved, "reserved"),
        (ip.is_private, "private"),
    )
    for matched, label in categories:
        if matched:
            raise RecipeSecurityError(
                f"recipe host resolves to {address} ({label} address space), which is "
                f"refused. Reaching an internal service through a config-described "
                f"call is the SSRF shape this fence exists to prevent."
            )
    if not ip.is_global:
        raise RecipeSecurityError(
            f"recipe host resolves to {address}, which is not publicly routable."
        )


def assert_public_host(url: str) -> list[str]:
    """Resolve the URL's host and refuse unless EVERY address is public.

    Every address, not the first: a hostname with one public and one loopback
    answer would otherwise pass while the client might connect to either.
    Returns the addresses so they can be recorded in the evidence pack.
    """
    host = host_of(url)
    addresses = resolve_addresses(host)
    if not addresses:
        raise RecipeSecurityError(f"recipe host {host!r} resolved to no addresses")
    for address in addresses:
        assert_public_address(address)
    return addresses


def assert_destination(url: str, allowed_host: str, *, resolve: bool = True) -> list[str]:
    """All destination invariants, in the order a reader should think about them."""
    assert_https(url)
    assert_host_allowed(url, allowed_host)
    return assert_public_host(url) if resolve else []


def confine_next_url(next_url: str, pinned_url: str, allowed_next_paths: list[str]) -> str:
    """Confine a PROVIDER-SUPPLIED next-page URL to the recipe's pinned endpoint.

    # Why this is stricter than `assert_host_allowed`

    A `Link: <...>; rel="next"` header is a destination chosen by the remote
    end. Running it through the same rule as the authored URL would let a
    provider — or anyone who can inject that header — move the request to any
    SUBDOMAIN of the pinned host, because that rule deliberately admits proper
    subdomains. For a URL the author wrote down, subdomain latitude is a
    convenience. For a URL the *server* just handed us, it is a steering
    primitive, so it is removed here entirely.

    The rule is DEFAULT-DENY and query-only:

    - scheme, host **and port** must be byte-equal to the pinned URL's. Not
      "a subdomain of", not "resolves to the same address" — equal.
    - the path must equal the pinned path, or appear verbatim in
      `allowed_next_paths`, which the recipe author declares at authoring time.
      A provider that genuinely paginates across paths therefore requires an
      explicit declaration; absent it, the next URL is REFUSED, not followed.
    - **only the query string is adopted.** The returned URL is rebuilt from
      the pinned scheme/netloc plus the permitted path plus the next URL's
      query. Nothing else survives — not userinfo, not a fragment, not
      parameters.

    Rebuilding rather than approving-in-place is the point: a validated string
    that is then passed through unchanged can still carry something nobody
    checked, whereas a string reassembled from vetted parts can only contain
    vetted parts.
    """
    # STRUCTURAL REFUSALS — the candidate URL is never echoed.
    #
    # This URL is PROVIDER-CONTROLLED, so every component of it is untrusted
    # content: the query can carry a submitted API key, the path can carry a
    # token, and userinfo is credential material by definition. An error that
    # quoted the candidate would put that straight into an exception message, a
    # drift event, and a repair brief the next agent reads.
    #
    # So a refusal names the VIOLATED RULE and the offending COMPONENT
    # CATEGORY, never the component's content. The canonicalized host is the
    # one exception, and it is safe for a specific reason: it is compared
    # against pinned-known values, so reporting it tells a reader which
    # allowlist decision fired without revealing anything the provider chose
    # freely. Port is numeric. Nothing else is reportable.
    #
    # This is round 4's value-free principle generalized from validation errors
    # to security refusals: every engine-raised error that can carry
    # provider-controlled content is structural. Literal scrubbing is the
    # backstop, never the guarantee.
    pinned = urlsplit(pinned_url)
    try:
        candidate = urlsplit(next_url)
    except ValueError:
        raise RecipeSecurityError(
            "pagination next-URL rejected: malformed URL (not parseable)"
        ) from None

    if (candidate.scheme or "").lower() != "https":
        raise RecipeSecurityError(
            "pagination next-URL rejected: non-https scheme. The URL is not reported: it is "
            "provider-controlled and may carry credential material."
        )

    if candidate.username or candidate.password:
        raise RecipeSecurityError(
            "pagination next-URL rejected: userinfo present in authority. The value is not "
            "reported — userinfo is credential material by definition."
        )

    pinned_host = (pinned.hostname or "").lower().rstrip(".")
    candidate_host = (candidate.hostname or "").lower().rstrip(".")
    if not candidate_host:
        raise RecipeSecurityError("pagination next-URL rejected: no host in authority")
    if candidate_host != pinned_host:
        # The canonicalized host is reportable: it is checked against a
        # pinned-known value, so naming it identifies the decision without
        # disclosing anything freely chosen.
        raise RecipeSecurityError(
            f"pagination next-URL rejected: host outside pinned set: {candidate_host}. "
            f"Subdomain latitude is deliberately unavailable here — this URL came from the "
            f"provider, not the recipe author."
        )
    if candidate.port != pinned.port:
        raise RecipeSecurityError(
            f"pagination next-URL rejected: port outside pinned set: {candidate.port}"
        )

    if candidate.path != pinned.path and candidate.path not in allowed_next_paths:
        # The path itself is NOT reported: a provider can put a token in it.
        raise RecipeSecurityError(
            f"pagination next-URL rejected: path outside the declared set "
            f"({1 + len(allowed_next_paths)} permitted). Cross-path pagination must be "
            f"declared at authoring time via allowed_next_paths; it is never inferred from "
            f"what a provider sends. The candidate path is not reported — it is "
            f"provider-controlled."
        )

    return urlunsplit((pinned.scheme, pinned.netloc, candidate.path, candidate.query, ""))


__all__ = [
    "RecipeSecurityError",
    "assert_destination",
    "confine_next_url",
    "assert_host_allowed",
    "assert_https",
    "assert_public_address",
    "assert_public_host",
    "host_of",
    "resolve_addresses",
]
