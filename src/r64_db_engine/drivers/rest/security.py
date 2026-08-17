"""Destination-pinning invariants for the `rest` dialect.

The recipe lane executes calls described by a config file. That makes the config
a control surface, and a control surface that can name a destination is one that
can be pointed somewhere it should not go. These functions are the fence.

Four invariants, each enforced here and each with a failing fixture in
`tests/drivers/rest/test_security.py` proving the malicious shape is REFUSED —
not merely that the benign one is accepted. A security check with no failing
fixture is decoration.

1. **HTTPS only.** No plaintext, ever, and no downgrade.
2. **Hostname fixed per recipe.** The URL host must equal the recipe's recorded
   allowlist host or be a PROPER SUBDOMAIN of it.
3. **No private address space.** The host's RESOLVED addresses must all be
   publicly routable.
4. **No redirects.** Enforced at the client (see `engine.py`), because a 302 is
   a destination change that would otherwise route around 1-3.

# What this does NOT close, stated plainly

Between `assert_public_host()` resolving a name and httpx resolving it again to
open the socket, DNS can change. A DNS-rebinding attacker who controls the
authoritative server for an allowlisted hostname can therefore still win that
race. Closing it properly means pinning the validated IP and carrying the
hostname for TLS SNI and certificate validation, which is a real change to the
transport layer rather than a tightening of this module.

That window is narrow and requires an attacker who already controls DNS for a
host somebody deliberately wrote into a recipe. It is recorded here rather than
papered over, because the failure mode of a security fence is a reader who
believes it covers more than it does.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit


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
    return sorted({info[4][0] for info in infos})


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


__all__ = [
    "RecipeSecurityError",
    "assert_destination",
    "assert_host_allowed",
    "assert_https",
    "assert_public_address",
    "assert_public_host",
    "host_of",
    "resolve_addresses",
]
