"""Capture Anthropic's unified rate-limit response headers, verbatim.

The dashboard is allowed to say "you hit your 5-hour window, it resets at X"
only when a real Anthropic response said so. Nothing here computes a window
from a wall clock, infers one from a 429, or carries a value forward past the
next response: every field is a string Anthropic sent, or absent.

The names are read out of Claude Code 2.1.258 at offsets 99354660 and
101783084. They are an allow-list for the same reason
:mod:`my_claude_code.core.request_headers` uses one -- an unknown response
header is not stored, so forgetting to update this set costs diagnostics
rather than leaking whatever a proxy decided to add.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field

# Offsets 99354660 / 101783084 of the 2.1.258 binary, verbatim.
UNIFIED_RATE_LIMIT_HEADERS: frozenset[str] = frozenset(
    {
        "anthropic-usage-limit",
        "anthropic-ratelimit-unified-status",
        "anthropic-ratelimit-unified-reset",
        "anthropic-ratelimit-unified-5h-utilization",
        "anthropic-ratelimit-unified-5h-reset",
        "anthropic-ratelimit-unified-5h-surpassed-threshold",
        "anthropic-ratelimit-unified-7d-utilization",
        "anthropic-ratelimit-unified-7d-reset",
        "anthropic-ratelimit-unified-7d-surpassed-threshold",
        "anthropic-ratelimit-unified-overage-status",
        "anthropic-ratelimit-unified-overage-reset",
        "anthropic-ratelimit-unified-overage-in-use",
        "anthropic-ratelimit-unified-overage-disabled-reason",
        "anthropic-ratelimit-unified-slow-budget-reset",
        "anthropic-ratelimit-unified-representative-claim",
    }
)

# Statuses Claude Code itself recognises (same offsets). Kept so the dashboard
# reuses Anthropic's own vocabulary instead of inventing a second one.
UNIFIED_STATUS_VALUES: frozenset[str] = frozenset(
    {
        "overage-active",
        "overage-warning",
        "overage-exhausted",
        "approaching-weekly-limit",
        "extra-usage-required",
        "fast-mode-limit",
        "fast-mode-short-limit",
        "member-zero-credit-limit",
        "opus-limit",
        "opus-warning",
        "org-spend-cap-hit",
        "org-zero-credit-limit",
        "out-of-credits",
        "seat-tier-zero-credit-limit",
        "session-limit-reached",
        "sonnet-limit",
        "sonnet-warning",
        "weekly-limit-reached",
    }
)

MAX_VALUE_CHARS = 128


@dataclass(frozen=True, slots=True)
class RateLimitSnapshot:
    """One response's unified rate-limit headers, plus when they arrived."""

    observed_at: float
    status_code: int
    values: dict[str, str] = field(default_factory=dict)


def capture_rate_limit_headers(
    headers: Mapping[str, str] | None,
) -> dict[str, str]:
    """Return the allow-listed unified rate-limit headers from one response."""
    if not headers:
        return {}
    captured: dict[str, str] = {}
    for raw_name, raw_value in headers.items():
        name = str(raw_name).strip().lower()
        if name in UNIFIED_RATE_LIMIT_HEADERS and isinstance(raw_value, str):
            value = raw_value.strip()
            if value:
                captured[name] = value[:MAX_VALUE_CHARS]
    return captured


class RateLimitObserver:
    """Hold the most recent snapshot, and nothing older.

    Deliberately last-write-wins with no history: the card answers "where is
    this account right now?", and a stale window is worse than no window.
    """

    __slots__ = ("_by_account", "_latest")

    def __init__(self) -> None:
        self._latest: RateLimitSnapshot | None = None
        # One snapshot per account since 7.30.0. A window belongs to the
        # subscription that observed it: reporting one account's 5-hour
        # utilisation on another account's row is not a rounding error, it is
        # a wrong answer to the only question the card is asked.
        self._by_account: dict[str, RateLimitSnapshot] = {}

    @property
    def latest(self) -> RateLimitSnapshot | None:
        """The most recent snapshot from any account.

        Kept unkeyed so the pre-7.30.0 card payload -- which has exactly one
        windows block -- keeps meaning what it meant. Per-account rows read
        :meth:`latest_for`.
        """
        return self._latest

    def latest_for(self, account_id: str) -> RateLimitSnapshot | None:
        """One account's most recent snapshot, or ``None``."""
        if not account_id:
            return self._latest
        return self._by_account.get(account_id)

    def observe(
        self,
        headers: Mapping[str, str] | None,
        *,
        status_code: int,
        now: float,
        account_id: str = "",
    ) -> None:
        captured = capture_rate_limit_headers(headers)
        if not captured:
            return
        snapshot = RateLimitSnapshot(
            observed_at=now, status_code=status_code, values=captured
        )
        self._latest = snapshot
        if account_id:
            self._by_account[account_id] = snapshot


# Process-wide, because the card is read by the admin API while the provider
# instance that saw the header lives inside a provider generation the admin
# routes cannot reach. One credential, one account, one window.
OBSERVER = RateLimitObserver()


__all__ = [
    "MAX_VALUE_CHARS",
    "OBSERVER",
    "UNIFIED_RATE_LIMIT_HEADERS",
    "UNIFIED_STATUS_VALUES",
    "RateLimitObserver",
    "RateLimitSnapshot",
    "capture_rate_limit_headers",
]
