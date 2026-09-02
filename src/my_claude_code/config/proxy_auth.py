"""Shared proxy-auth policy for FCC client launchers and for server startup."""

from .admin.manifest import SECTIONS

PROXY_NO_AUTH_SENTINEL = "fcc-no-auth"

#: Every spelling of "this machine only" that ``HOST`` can carry. A server bound
#: to one of these is reachable by processes on the same box and nothing else,
#: which is the one situation in which running with no proxy token is a choice
#: rather than an accident.
LOOPBACK_HOSTS = frozenset(
    {
        "127.0.0.1",
        "localhost",
        "::1",
        "[::1]",
        "0:0:0:0:0:0:0:1",
    }
)

#: The dashboard page whose card edits ``ANTHROPIC_AUTH_TOKEN`` and ``HOST``.
#: Both fields declare the ``runtime`` manifest section, and ``admin.js``
#: renders that section on the Providers page.
#: ``tests/contracts/test_proxy_auth_hint_labels.py`` pins both halves so a
#: renamed page or card fails a check rather than a user's search.
RUNTIME_PAGE_LABEL = "Providers"
RUNTIME_SECTION_ID = "runtime"

_SECTION_LABELS = {section.section_id: section.label for section in SECTIONS}


def proxy_auth_token(auth_token: str) -> str:
    """Return the configured proxy token or the no-auth client marker."""

    return auth_token.strip() or PROXY_NO_AUTH_SENTINEL


def host_is_loopback(host: str) -> bool:
    """Whether a bound host reaches only this machine."""
    return host.strip().casefold() in LOOPBACK_HOSTS


def open_proxy_without_auth_error(*, host: str, auth_token: str) -> str | None:
    """Refuse to serve an unauthenticated proxy to the network.

    ``HOST`` defaults to ``0.0.0.0`` and the proxy token is optional, and the
    combination of those two defaults is a proxy that answers anybody on the
    LAN and spends the operator's provider credits doing it. ``require_proxy_auth``
    returns early on an empty token, so nothing downstream would ever ask a
    question about it -- the check has to happen before the socket is bound or
    it does not happen at all.

    A loopback bind with no token stays allowed: that is a deliberate,
    single-machine setup and refusing it would break every developer who never
    wanted a token in the first place.

    Returns the message to print, or ``None`` when the configuration is safe.
    """
    if auth_token.strip():
        return None
    if host_is_loopback(host):
        return None
    return (
        f"Refusing to start: HOST is {host!r}, which accepts connections from "
        "other machines, and ANTHROPIC_AUTH_TOKEN is empty, so this proxy would "
        "serve anyone who can reach it. Set ANTHROPIC_AUTH_TOKEN to a secret, or "
        "set HOST=127.0.0.1 to serve this machine only "
        f"(change either on the dashboard under {RUNTIME_PAGE_LABEL} -> "
        f"{_SECTION_LABELS[RUNTIME_SECTION_ID]})."
    )
