"""Compose the ``anthropic-beta`` header MCC sends upstream.

MCC used to send a fixed two-value string and discard whatever the client
asked for. Real Claude Code sends eight to twelve betas, and dropping them is
not neutral: without ``context-1m-2025-08-07`` a 1M-context session is silently
capped at 200k, and without ``interleaved-thinking-2025-05-14`` the model
reasons differently. Both are capability the subscription already paid for.

The rule is floor first, then the client's own list, intersected with a closed
allow-list, order-stable and deduplicated. Passing the client's list through
verbatim is the other failure: a beta the account is not entitled to answers
400, and MCC would then blame the model.
"""

from .constants import ANTHROPIC_OAUTH_BETA_ALLOWLIST, ANTHROPIC_OAUTH_BETA_FLOOR


def split_betas(raw: str | None) -> tuple[str, ...]:
    """Split one ``anthropic-beta`` header value into its names."""
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def merge_betas(client_betas: str | None) -> tuple[str, tuple[str, ...]]:
    """Return ``(header_value, dropped_names)`` for one inbound request.

    ``dropped_names`` is what the client asked for and did not get. It is
    logged rather than sent, because an unrecognised beta is the single most
    likely cause of a 400 that looks like a model problem, and a name nobody
    can see is a name nobody can add to the allow-list.
    """
    ordered: list[str] = list(ANTHROPIC_OAUTH_BETA_FLOOR)
    seen = set(ordered)
    dropped: list[str] = []
    for name in split_betas(client_betas):
        if name in seen:
            continue
        if name not in ANTHROPIC_OAUTH_BETA_ALLOWLIST:
            if name not in dropped:
                dropped.append(name)
            continue
        seen.add(name)
        ordered.append(name)
    return ",".join(ordered), tuple(dropped)


__all__ = ["merge_betas", "split_betas"]
