"""Shared credential-value parsing helpers."""

from urllib.parse import urlsplit


def parse_credential_keys(credential: str | None) -> tuple[str, ...]:
    """Split a comma-separated credential value into individual keys."""
    if not credential:
        return ()
    return tuple(key for key in (part.strip() for part in credential.split(",")) if key)


def mask_key_label(key: str) -> str:
    """Mask a key for logs/analytics: ``first4…last4`` (shorter keys stay tail-only).

    Analytics and admin responses identify a credential by this label, never by
    its value, so the raw key never reaches a database, log line, or HTTP body.
    """
    if len(key) > 8:
        return f"{key[:4]}…{key[-4:]}"
    if len(key) > 4:
        return f"…{key[-4:]}"
    return "…" if key else ""


def mask_proxy_label(url: str) -> str:
    """Name a proxy by ``host:port``, with any ``user:pass`` removed.

    The sibling of :func:`mask_key_label`, and it exists for a sharper reason:
    a proxy URL may carry credentials in its userinfo, so the *only* form of it
    that may reach an HTTP response, a log line or the request log is this one.
    A proxy password in the request log would be a worse leak than the thing a
    proxy chain is trying to avoid.

    The scheme is deliberately dropped: the label identifies an address, and a
    catalogue that shows ``socks5h://`` twice per row buys nothing. The page
    renders the scheme from its own field. An unparsable string yields ``""``
    rather than itself, so a mask can never fail open.
    """

    candidate = (url or "").strip()
    if not candidate:
        return ""
    try:
        parsed = urlsplit(candidate)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return ""
    if not host:
        return ""
    return f"{host}:{port}" if port else host
