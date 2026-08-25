"""Shared credential-value parsing helpers."""


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
