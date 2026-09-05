"""Capture the ChatGPT/Codex response headers Codex itself parses, verbatim.

Same contract as :mod:`my_claude_code.providers.anthropic_oauth.rate_limit_headers`,
and sourced the same way: the names below are the ones Codex CLI 0.151.0 reads
off its own Responses stream (``codex-api/src/sse/responses.rs``), read out of
the shipped binary rather than guessed from documentation.

Before this existed MCC discarded every upstream response header on this
provider -- ``grep`` for ``.headers`` in the provider and the streaming module
returned nothing at all -- so the live quota windows and the credits balance
OpenAI sends on every response were thrown away, and the dashboard could only
learn about a limit by being refused by one.

Nothing here computes a window, infers one from a 429, or carries a value
forward past the next response: every field is a string OpenAI sent, or
absent. An unknown header is not stored, so forgetting to update the allow-list
costs diagnostics rather than storing whatever a proxy decided to add.
"""

import time
from collections.abc import Mapping
from dataclasses import dataclass, field

#: Read out of Codex CLI 0.151.0's SSE response handler, lower-cased because
#: HTTP header names are case-insensitive and ``httpx`` hands them back in the
#: casing the server chose.
CODEX_RESPONSE_HEADERS: frozenset[str] = frozenset(
    {
        # Catalogue/stream identity.
        "x-models-etag",
        "openai-model",
        "x-reasoning-included",
        "x-request-id",
        "x-codex-turn-state",
        "x-codex-promo-message",
        # Credits.
        "x-codex-credits-has-credits",
        "x-codex-credits-unlimited",
        "x-codex-credits-balance",
        # The two rolling usage windows.
        "x-codex-primary-used-percent",
        "x-codex-primary-window-minutes",
        "x-codex-primary-reset-at",
        "x-codex-secondary-used-percent",
        "x-codex-secondary-window-minutes",
        "x-codex-secondary-reset-at",
        # Why a limit fired, in OpenAI's own vocabulary.
        "x-codex-limit-name",
        "x-codex-rate-limit-reached-type",
    }
)

MAX_VALUE_CHARS = 128


@dataclass(frozen=True, slots=True)
class CodexResponseSnapshot:
    """One response's allow-listed headers, plus when they arrived."""

    observed_at: float
    status_code: int
    values: dict[str, str] = field(default_factory=dict)


def capture_codex_response_headers(
    headers: Mapping[str, str] | None,
) -> dict[str, str]:
    """Return the allow-listed Codex headers from one upstream response."""
    if not headers:
        return {}
    captured: dict[str, str] = {}
    for raw_name, raw_value in headers.items():
        name = str(raw_name).strip().lower()
        if name in CODEX_RESPONSE_HEADERS and isinstance(raw_value, str):
            value = raw_value.strip()
            if value:
                captured[name] = value[:MAX_VALUE_CHARS]
    return captured


class CodexResponseObserver:
    """Hold the most recent snapshot, and nothing older.

    Last-write-wins with no history, for the same reason the Anthropic twin is:
    the card answers "where is this account right now?", and a stale window is
    worse than no window.
    """

    __slots__ = ("_latest",)

    def __init__(self) -> None:
        self._latest: CodexResponseSnapshot | None = None

    @property
    def latest(self) -> CodexResponseSnapshot | None:
        return self._latest

    def observe(
        self,
        headers: Mapping[str, str] | None,
        *,
        status_code: int,
        now: float | None = None,
    ) -> None:
        captured = capture_codex_response_headers(headers)
        if not captured:
            return
        self._latest = CodexResponseSnapshot(
            observed_at=time.time() if now is None else now,
            status_code=status_code,
            values=captured,
        )


# Process-wide, because the card is read by the admin API while the provider
# instance that saw the header lives inside a provider generation the admin
# routes cannot reach. One credential, one account, one window.
OBSERVER = CodexResponseObserver()


__all__ = [
    "CODEX_RESPONSE_HEADERS",
    "MAX_VALUE_CHARS",
    "OBSERVER",
    "CodexResponseObserver",
    "CodexResponseSnapshot",
    "capture_codex_response_headers",
]
