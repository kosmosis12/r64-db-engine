"""Supabase connection profile over the Postgres driver.

Supabase IS Postgres, so the reference-grade `postgres` driver already speaks
it. What Supabase adds is deployment shape, and one shape is actively unsafe
for this driver:

  Local dev stack      direct Postgres, 127.0.0.1:54322. Nothing special.
  Hosted direct        db.<ref>.supabase.co:5432. IPv6 by default; IPv4 needs
                       the pooler or the paid add-on. Full Postgres semantics.
  Hosted pooled        <ref>.pooler.supabase.com
                         :5432 session mode      -- supported, with a caveat
                         :6543 transaction mode  -- REFUSED

Transaction-mode pooling multiplexes many clients onto few server connections
and hands a connection back after every transaction. Server-side prepared
statements are per-session state, so a `PREPARE` issued on one transaction is
simply not there for the next -- psycopg's automatic prepared statements
(`prepare_threshold=5` by default) start failing partway through a pull with
"prepared statement ... does not exist". That is exactly the mid-pull mystery
failure this profile exists to prevent, so transaction mode is refused by name
rather than worked around.

Session mode holds a server connection for the client's whole session, so
prepared statements survive -- but the pooler is still in the path, and a
session can be recycled underneath a long-lived client. This profile therefore
forces `prepare_threshold=None` (never prepare) whenever a pooler host is
detected, trading a little planning time for a guarantee.
"""

from __future__ import annotations

import ipaddress
import logging
from typing import Any

from r64_db_engine.core.profile import ConnectionProfile, ProfileError

log = logging.getLogger(__name__)

#: Supabase's transaction-mode pooler port. Never safe for this driver.
TRANSACTION_POOLER_PORT = 6543
#: Supabase's session-mode pooler port (and the direct-connection port).
SESSION_POOLER_PORT = 5432
#: Substring identifying a Supabase connection pooler host.
POOLER_HOST_MARKER = "pooler.supabase.com"
#: sslmode values that do not guarantee an encrypted connection.
_UNENCRYPTED_SSLMODES = frozenset({"allow", "prefer"})


def _is_loopback(host: str) -> bool:
    """True for a host that cannot leave the machine.

    Only a genuine loopback address or `localhost` counts. Anything else --
    including a private LAN address -- is treated as remote, because the
    question this answers is "can this connection be observed on the wire",
    not "is this host nearby".
    """
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class SupabaseProfile(ConnectionProfile):
    """Named profile for Supabase Postgres. Config-layer only."""

    @classmethod
    def profile_name(cls) -> str:
        return "supabase"

    @classmethod
    def dialect(cls) -> str:
        return "postgres"

    @classmethod
    def apply(cls, config: dict[str, Any]) -> dict[str, Any]:
        out = dict(config)
        host = str(out.get("host") or "")
        port = int(out.get("port") or SESSION_POOLER_PORT)
        pooled = POOLER_HOST_MARKER in host.lower()

        # --- refuse transaction-mode pooling, loudly -----------------------
        # Checked on the port alone, not on the port AND the hostname: 6543 is
        # the transaction-mode port whatever the host is called, and a custom
        # DNS name in front of the pooler must not smuggle it past this gate.
        if port == TRANSACTION_POOLER_PORT:
            raise ProfileError(
                f"supabase profile: refusing transaction-mode pooler port {port}. "
                "Transaction-mode pooling returns the server connection after every "
                "transaction, so server-side prepared statements do not survive and a "
                "pull fails partway through with 'prepared statement does not exist'. "
                f"Use session mode (port {SESSION_POOLER_PORT}) or a direct connection. "
                "This is refused rather than silently degraded."
            )

        # --- pooler in the path: never prepare -----------------------------
        if pooled:
            requested = out.get("prepare_threshold", "unset")
            out["prepare_threshold"] = None
            log.info(
                "supabase_profile pooled_host=%s prepare_threshold=None (was %r)",
                host,
                requested,
            )

        # --- transport ------------------------------------------------------
        if not _is_loopback(host):
            sslmode = str(out.get("sslmode") or "prefer")
            if sslmode == "disable":
                raise ProfileError(
                    f"supabase profile: refusing sslmode=disable for non-loopback host "
                    f"{host!r}. A hosted Supabase connection carries credentials and "
                    "row data over the public internet; plaintext is not a supported "
                    "configuration. Use sslmode=require or stronger."
                )
            if sslmode in _UNENCRYPTED_SSLMODES:
                # `prefer` silently falls back to plaintext if the server declines
                # TLS, which reads as encrypted but is not guaranteed to be.
                out["sslmode"] = "require"
                log.info(
                    "supabase_profile host=%s sslmode=%s -> require", host, sslmode
                )

        return out
