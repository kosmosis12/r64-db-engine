"""Destination-pinning invariants, proven by the shapes they must REFUSE.

Every test here that matters asserts a refusal. A security fence tested only in
the positive direction — "the benign URL is accepted" — passes identically when
the fence has been deleted, which makes it worse than no test at all because it
reads like coverage.

No network: `assert_https` and `assert_host_allowed` are pure string work, and
`assert_public_address` takes an address rather than resolving one. Only the
two `assert_public_host` tests resolve, and they resolve names that answer
locally.
"""

from __future__ import annotations

import pytest

from r64_db_engine.drivers.rest.security import (
    RecipeSecurityError,
    assert_host_allowed,
    assert_https,
    assert_public_address,
    assert_public_host,
    confine_next_url,
    host_of,
)

# ---------------------------------------------------------------------------
# HTTPS only
# ---------------------------------------------------------------------------


def test_https_is_accepted() -> None:
    assert_https("https://api.example.com/v1/things")


@pytest.mark.parametrize(
    "url",
    [
        "http://api.example.com/v1/things",
        "HTTP://api.example.com/v1/things",
        "ftp://api.example.com/things",
        "file:///etc/passwd",
        "gopher://api.example.com/",
        "//api.example.com/v1/things",
    ],
)
def test_non_https_schemes_are_refused(url: str) -> None:
    """A recipe carries an API key. There is no configuration under which
    sending it in the clear is the intended behaviour, so this refuses rather
    than warns."""
    with pytest.raises(RecipeSecurityError):
        assert_https(url)


# ---------------------------------------------------------------------------
# Hostname pinning — the evil-checkr.com case, literally
# ---------------------------------------------------------------------------


def test_exact_host_is_allowed() -> None:
    assert_host_allowed("https://checkr.com/v1/x", "checkr.com")


def test_proper_subdomain_is_allowed() -> None:
    assert_host_allowed("https://api.checkr.com/v1/x", "checkr.com")


def test_deep_subdomain_is_allowed() -> None:
    assert_host_allowed("https://eu.api.checkr.com/v1/x", "checkr.com")


def test_evil_checkr_com_is_refused() -> None:
    """THE case, spelled out because it is the one a plausible implementation
    gets wrong.

        "evil-checkr.com".endswith("checkr.com")   -> True

    An attacker only has to register the lookalike. Suffix matching hands them
    the allowlist; matching on the dot-delimited label boundary does not.
    """
    with pytest.raises(RecipeSecurityError, match="not the allowed host 'checkr.com'") as exc:
        assert_host_allowed("https://evil-checkr.com/v1/x", "checkr.com")
    # The ALLOWED host is named; the candidate never is.
    assert "evil-checkr.com" not in str(exc.value)


@pytest.mark.parametrize(
    "host",
    [
        "evil-checkr.com",      # suffix match, no label boundary
        "xcheckr.com",          # same, one character
        "notcheckr.com",
        "checkr.com.evil.net",  # allowlist as a PREFIX — a different registrable domain
        "checkr.co",            # near miss
        "evil.com",
    ],
)
def test_lookalike_hosts_are_refused(host: str) -> None:
    with pytest.raises(RecipeSecurityError):
        assert_host_allowed(f"https://{host}/v1/x", "checkr.com")


def test_host_comparison_is_case_and_trailing_dot_insensitive() -> None:
    """`API.Checkr.COM.` is the same host. Refusing it would be a correctness
    bug; accepting `evil-checkr.com` because of the same normalization would be
    a security one — so both directions are pinned."""
    assert_host_allowed("https://API.Checkr.COM./v1/x", "checkr.com")
    with pytest.raises(RecipeSecurityError):
        assert_host_allowed("https://EVIL-Checkr.COM./v1/x", "checkr.com")


def test_url_without_a_hostname_is_refused() -> None:
    with pytest.raises(RecipeSecurityError):
        host_of("https:///v1/x")


# ---------------------------------------------------------------------------
# Private address space — the SSRF fence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address,label",
    [
        ("127.0.0.1", "loopback"),
        ("::1", "loopback"),
        ("10.0.0.5", "private"),
        ("172.17.0.1", "private"),      # the docker bridge gateway
        ("192.168.1.10", "private"),
        ("169.254.169.254", "link-local"),  # the cloud metadata endpoint
        ("fe80::1", "link-local"),
        ("0.0.0.0", "unspecified"),
        ("224.0.0.1", "multicast"),
        ("fc00::1", "private"),
    ],
)
def test_non_public_addresses_are_refused(address: str, label: str) -> None:
    """169.254.169.254 is the one to notice: reaching cloud instance metadata
    through a config-described call is the canonical SSRF payoff."""
    with pytest.raises(RecipeSecurityError) as exc:
        assert_public_address(address)
    assert label in str(exc.value)


@pytest.mark.parametrize("address", ["1.1.1.1", "93.184.216.34", "2606:4700:4700::1111"])
def test_public_addresses_are_accepted(address: str) -> None:
    assert_public_address(address)


def test_unparseable_address_is_refused_rather_than_ignored() -> None:
    with pytest.raises(RecipeSecurityError):
        assert_public_address("not-an-address")


def test_localhost_is_refused_by_where_it_resolves_not_how_it_is_spelled() -> None:
    """The fence is on resolution, which is the only check an attacker cannot
    spell their way around."""
    with pytest.raises(RecipeSecurityError, match="loopback"):
        assert_public_host("https://localhost/v1/x")


def test_a_name_that_resolves_to_loopback_is_refused() -> None:
    """Same fence, via a name that is not spelled 'localhost' at all."""
    with pytest.raises(RecipeSecurityError, match="loopback"):
        assert_public_host("https://localhost.localdomain/v1/x")


def test_unresolvable_host_is_refused_rather_than_skipped() -> None:
    with pytest.raises(RecipeSecurityError, match="could not resolve"):
        assert_public_host("https://this-host-does-not-exist.invalid/v1/x")


# ---------------------------------------------------------------------------
# Pagination confinement — the provider-supplied next URL
# ---------------------------------------------------------------------------

PINNED = "https://api.example.com/v1/items?page=1"


def test_the_next_url_keeps_the_pinned_endpoint_and_takes_only_the_query() -> None:
    """Rebuilt from vetted parts, not approved in place.

    A validated string passed through unchanged can still carry something
    nobody checked; a string reassembled from the pinned scheme, netloc and
    path plus the provider's query can only contain what was vetted.
    """
    out = confine_next_url("https://api.example.com/v1/items?page=2&x=y", PINNED, [])
    assert out == "https://api.example.com/v1/items?page=2&x=y"


def test_a_fragment_is_dropped_from_the_next_url() -> None:
    out = confine_next_url("https://api.example.com/v1/items?page=2#frag", PINNED, [])
    assert "#" not in out


@pytest.mark.parametrize(
    "hostile",
    [
        "https://evil.com/v1/items?page=2",
        "https://api.example.com.evil.net/v1/items?page=2",
        "https://evil-api.example.com/v1/items?page=2",
        "http://api.example.com/v1/items?page=2",
        "https://api.example.com:8443/v1/items?page=2",
        "https://user:pw@api.example.com/v1/items?page=2",
    ],
)
def test_a_steered_next_url_is_refused(hostile: str) -> None:
    with pytest.raises(RecipeSecurityError):
        confine_next_url(hostile, PINNED, [])


def test_a_subdomain_next_url_is_refused_even_though_the_host_rule_allows_it() -> None:
    """The deliberate asymmetry, pinned in a test so it cannot drift back.

    `assert_host_allowed` permits `sub.api.example.com` for an AUTHORED URL —
    that latitude is a convenience for the person writing the recipe. On the
    pagination path the URL comes from the SERVER, where the same latitude is a
    steering primitive, so it is removed entirely.
    """
    steered = "https://attacker.api.example.com/v1/items?page=2"
    # The authored-URL rule would allow it...
    assert_host_allowed(steered, "api.example.com")
    # ...and the pagination rule does not.
    with pytest.raises(RecipeSecurityError, match="host outside pinned set") as exc:
        confine_next_url(steered, PINNED, [])
    text = str(exc.value)
    # TERMINAL FORM: the PINNED host is named, the candidate never is. Being
    # compared against a pinned value justifies the comparison, not printing
    # the thing compared — a host label can carry a secret prefix, and a whole
    # credential can be a subdomain.
    assert "pinned: api.example.com" in text
    assert "attacker" not in text
    assert steered not in text


def test_a_different_path_is_refused_by_default() -> None:
    with pytest.raises(RecipeSecurityError, match="allowed_next_paths"):
        confine_next_url("https://api.example.com/v1/admin?page=2", PINNED, [])


def test_a_declared_path_is_permitted() -> None:
    out = confine_next_url(
        "https://api.example.com/v1/items/page2?x=1", PINNED, ["/v1/items/page2"]
    )
    assert out == "https://api.example.com/v1/items/page2?x=1"


def test_a_declared_path_does_not_widen_the_host_rule() -> None:
    """Declaring a path must not accidentally admit another host on that path."""
    with pytest.raises(RecipeSecurityError, match="host outside pinned set"):
        confine_next_url(
            "https://evil.com/v1/items/page2?x=1", PINNED, ["/v1/items/page2"]
        )
