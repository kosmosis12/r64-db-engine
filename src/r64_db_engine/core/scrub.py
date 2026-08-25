"""Credential scrubbing — one implementation, every boundary that needs it.

This is the round-4 mechanism (`49c01ed`) lifted out of the recipe engine so a
second boundary can use it instead of forking it. Nothing about removing
credential material from text is specific to HTTP, and a parallel scanner
authored beside this one would be a second thing to keep correct — which is the
same argument that put ONE outer boundary around the request path rather than a
guard at every raise site.

Two boundaries use it today:

  * `drivers/rest/engine.py` — the post-secret-load request path. A recipe's
    secret is read at call time from a 0600 file and placed in a header or a
    query parameter; if the call fails it can come straight back out inside an
    exception message or an httpx repr. Drift events and repair briefs are
    AGENT-READ, so that is a Law-3 violation, not a logging nit.
  * `factory/generate_descriptor_artifacts.py` — the post-descriptor-load emit
    path. Those artifacts are committed to git and served to a browser, so the
    cost of being wrong is a credential in public.

**What this is and is not.** Scrubbing is DEFENCE IN DEPTH, never the
guarantee. The guarantee is that the surfaces upstream carry no instance values
in the first place: value-free errors on the engine side, name-shaped
`required_env_keys` and authored prose on the descriptor side. Round 6 settled
that distinction and it is not re-litigated here — a boundary that filters
afterwards is what you keep when the thing it filters should never have arrived.
"""

from __future__ import annotations

import re
from urllib.parse import quote, quote_plus

__all__ = ["Scrubber"]


class Scrubber:
    """Removes registered credential material from anything about to be surfaced.

    Two mechanisms, because one is not enough:

    1. **Literal replacement** of the secret and its URL-encoded forms. Catches
       the common case where the value is echoed verbatim.
    2. **Query-parameter redaction by NAME**. Catches the forms literal matching
       misses — percent-encoding, `+` for space, or a client that re-serialized
       the value — because the parameter name is stable even when its rendering
       is not.

    Registered secrets are held only for the duration of the call.
    """

    __slots__ = ("_secrets", "_auth_keys")

    REDACTED = "«redacted»"

    def __init__(self) -> None:
        self._secrets: set[str] = set()
        self._auth_keys: set[str] = set()

    #: Secrets shorter than this are NOT registered for literal scrubbing.
    #: Replacing a short string across arbitrary error text corrupts unrelated
    #: content and produces confusing, wrong diagnostics — a redaction that
    #: eats the word "table" is worse than useless. The floor is DECLARED
    #: rather than tuned silently, and it is why value-free errors are the
    #: primary defence and scrubbing only the backstop: a credential short
    #: enough to fall under this floor is still never placed into a message,
    #: because the message never carries instance values in the first place.
    MIN_SCRUBBABLE_LENGTH = 8

    def register_secret(self, value: str) -> None:
        if value and len(value) >= self.MIN_SCRUBBABLE_LENGTH:
            self._secrets.add(value)

    def register_auth_key(self, key: str) -> None:
        if key:
            self._auth_keys.add(key)

    def scrub(self, text: str) -> str:
        out = str(text)
        for secret in self._secrets:
            for form in (secret, quote(secret, safe=""), quote_plus(secret)):
                if form:
                    out = out.replace(form, self.REDACTED)
        for key in self._auth_keys:
            out = re.sub(
                rf"([?&]{re.escape(key)}=)[^&\s\"'\)\]]*",
                rf"\1{self.REDACTED}",
                out,
            )
        return out

    def scrubbed(self, exc: BaseException) -> str:
        return self.scrub(f"{type(exc).__name__}: {exc}")
