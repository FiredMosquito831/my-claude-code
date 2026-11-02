"""Capture the usage headers ordinary inference responses already carry, verbatim.

The third observer in this codebase, and the same discipline as its two
siblings -- :mod:`my_claude_code.providers.anthropic_oauth.rate_limit_headers`
and :mod:`my_claude_code.providers.chatgpt_oauth.response_headers`:

* a **declared allow-list** of header names. A name that is not on the list is
  not stored, so forgetting to extend a list costs diagnostics rather than
  storing whatever an edge or a proxy decided to add;
* **verbatim or absent, never computed.** Nothing here parses a percentage out
  of a string, infers a window from a wall clock, derives one from a 429, or
  carries a value forward past the next response. Every field is the string
  the provider sent, or nothing at all;
* **last write wins, no history.** The question a usage card asks is "where is
  this account right now?", and a stale window is worse than no window;
* it **never raises.** It runs in the read path of every provider that opts in,
  so a header it cannot read is recorded as absent and a fault inside the
  observer costs that observation and nothing else. A usage figure is never a
  reason to fail a request.

Why it exists at all: D1 in `specs/INVESTIGATION-USAGE-AND-CREDITS.md` -- no
provider other than the two OAuth subscriptions captured a quota response
header, so Nous Portal's entire credits state (`x-nous-credits-*`), which
arrives on every inference response MCC already sends, was thrown away. Those
headers cost no request; reading them is the cheapest reading in the feature.

THE ALLOW-LISTS. Three families, each with the source it came from:

``NOUS_CREDITS_HEADERS``
    Nous Portal sends its credits state on ordinary inference traffic. The
    names are the ones ``hermes-agent``'s ``agent/credits_tracker.py`` reads;
    their own docstring documents the header contract, and its
    ``_header_name`` composes ``x-nous-`` + ``credits-`` (or ``x-nous-tool-``
    for the two tool-pool fields) + the field with underscores as dashes, so
    the list below is that declaration expanded once, here, rather than a
    pattern match. **Third-party evidence: Nous publishes no vendor document
    for it.** A host that does not send them records nothing, which is the
    correct answer to "does this host send them?" too -- it stays state 3 on
    the Usage page.

``GENERIC_RATE_LIMIT_HEADERS``
    The ``x-ratelimit-*`` family: the de-facto convention OpenAI, Groq,
    Mistral, OpenRouter, DeepSeek and their neighbours ship on ordinary
    responses. Kept separate from :data:`my_claude_code.core.rate_limit`'s
    ``RATE_LIMIT_RESET_HEADERS`` on purpose -- that tuple is read by the
    credential ladder to decide how long to bench a key, and it is frozen. The
    overlap (the three ``reset`` names) is harmless: this module stores them
    for *display*, that one parses them for *action*, and neither reads the
    other's copy.

``CODEX_RESPONSE_HEADERS`` lives with its own observer in
:mod:`my_claude_code.providers.chatgpt_oauth.response_headers` and is not
restated here.

WHAT IS NOT CAPTURED, deliberately: ``retry-after``. It is an instruction to
the client about this request, not a figure about the account, and storing it
as if it were a usage reading would be the "unknown is not unsupported"
mistake in the other direction.
"""

import time
from collections.abc import Mapping
from dataclasses import dataclass, field

__all__ = [
    "GENERIC_RATE_LIMIT_HEADERS",
    "MAX_VALUE_CHARS",
    "NOUS_CREDITS_HEADERS",
    "OBSERVER",
    "USAGE_HEADER_ALLOW_LIST",
    "UsageHeaderObserver",
    "UsageSnapshot",
    "capture_usage_headers",
]

#: Nous Portal's credits state, verbatim from ``hermes-agent``'s
#: ``agent/credits_tracker.py`` (its ``_HEADER_FIELDS`` composition plus the
#: names it reads directly). Lower-cased, because HTTP header names are
#: case-insensitive and ``httpx`` hands them back in whatever casing the edge
#: chose.
NOUS_CREDITS_HEADERS: frozenset[str] = frozenset(
    {
        "x-nous-credits-version",
        "x-nous-credits-denominator-kind",
        "x-nous-credits-remaining-micros",
        "x-nous-credits-remaining-usd",
        "x-nous-credits-subscription-micros",
        "x-nous-credits-subscription-usd",
        # The subscription's ceiling. The spec calls this "subscription-cap";
        # this is the name the source actually reads.
        "x-nous-credits-subscription-limit-micros",
        "x-nous-credits-subscription-limit-usd",
        "x-nous-credits-rollover-micros",
        "x-nous-credits-purchased-micros",
        "x-nous-credits-purchased-usd",
        "x-nous-credits-as-of-ms",
        "x-nous-credits-paid-access",
        "x-nous-credits-disabled-reason",
        "x-nous-credits-spend-source",
        "x-nous-credits-window-gauges",
        "x-nous-credits-window-paused-reason",
        "x-nous-credits-keep-going-on-credits",
        "x-nous-tool-pool-micros",
        "x-nous-tool-pool-gated-off",
    }
)

#: The ``x-ratelimit-*`` family as ordinary inference responses carry it.
#: Both the OpenAI spelling (hyphenated) and the bare ``ratelimit-*`` one some
#: gateways emit are listed; a host that sends neither records nothing.
GENERIC_RATE_LIMIT_HEADERS: frozenset[str] = frozenset(
    {
        "x-ratelimit-limit",
        "x-ratelimit-limit-requests",
        "x-ratelimit-limit-tokens",
        "x-ratelimit-remaining",
        "x-ratelimit-remaining-requests",
        "x-ratelimit-remaining-tokens",
        "x-ratelimit-reset",
        "x-ratelimit-reset-requests",
        "x-ratelimit-reset-tokens",
        "ratelimit-limit",
        "ratelimit-remaining",
        "ratelimit-reset",
    }
)

#: Everything one observation may store. Deliberately the union of the two
#: families rather than a per-provider list: which of them a given host sends
#: is the host's answer, and a provider that sends none records nothing.
USAGE_HEADER_ALLOW_LIST: frozenset[str] = NOUS_CREDITS_HEADERS | (
    GENERIC_RATE_LIMIT_HEADERS
)

#: A captured value is bounded for the same reason its two siblings bound
#: theirs: the store is a readout, not a transcript, and an edge that echoes a
#: path into a header must not turn into an unbounded string on a card. 256
#: rather than 128 because ``x-nous-credits-window-gauges`` is a serialised
#: structure a real host has every right to make longer than a percentage.
MAX_VALUE_CHARS = 256


@dataclass(frozen=True, slots=True)
class UsageSnapshot:
    """One response's allow-listed usage headers, plus when they arrived.

    ``observed_at`` is epoch seconds, stamped when the provider's bytes
    arrived -- not when anything was rendered, and not when this was read back.
    It is what the Usage page turns into "read 3 minutes ago" and what decides
    Measured from Stale.
    """

    observed_at: float
    status_code: int
    values: dict[str, str] = field(default_factory=dict)


def capture_usage_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    """Return the allow-listed usage headers from one upstream response.

    Total by construction: a missing mapping, a non-string name, a non-string
    value and an empty value each produce nothing rather than an exception,
    because this runs inside the read path of a live request.
    """

    if not headers:
        return {}
    captured: dict[str, str] = {}
    for raw_name, raw_value in headers.items():
        try:
            name = str(raw_name).strip().lower()
            if name not in USAGE_HEADER_ALLOW_LIST:
                continue
            if not isinstance(raw_value, str):
                continue
            value = raw_value.strip()
            if value:
                captured[name] = value[:MAX_VALUE_CHARS]
        except Exception:  # a header may never fail a request
            continue
    return captured


class UsageHeaderObserver:
    """Hold the most recent snapshot per provider and account, nothing older.

    Two axes, because a reading belongs to both: the *provider* that saw it
    (``nous_portal`` and ``open_router`` send different families and must not
    answer for each other) and, where one applies, the *credential* that
    carried it. The account argument is the same one its two siblings take and
    for the same reason -- reporting one account's remaining credits on
    another account's row is a wrong answer, not a rounding error.

    An observer with no provider id records under ``""``, which is what a bare
    construction in a test uses.
    """

    __slots__ = ("_by_key",)

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str], UsageSnapshot] = {}

    def latest_for(
        self, provider_id: str, account_id: str = ""
    ) -> UsageSnapshot | None:
        """That ``(provider, account)``'s most recent snapshot, or ``None``."""
        return self._by_key.get((provider_id or "", account_id or ""))

    def latest_keys(self) -> tuple[tuple[str, str], ...]:
        """Every ``(provider, account)`` a snapshot is held for.

        Registration order, which since Python 3.7 is insertion order. Read by
        the read-only admin route so a provider with no *current* snapshot can
        still be told apart from one nobody has ever observed.
        """
        return tuple(self._by_key)

    def observe(
        self,
        headers: Mapping[str, str] | None,
        *,
        status_code: int,
        provider_id: str,
        now: float | None = None,
        account_id: str = "",
    ) -> None:
        """Record one response's allow-listed headers, if it carried any.

        A response with none of them is not recorded at all: "this host sent
        nothing" must not overwrite "this host told us 40 minutes ago", because
        the second is a reading and the first is not.
        """
        captured = capture_usage_headers(headers)
        if not captured:
            return
        self._by_key[(provider_id or "", account_id or "")] = UsageSnapshot(
            observed_at=time.time() if now is None else now,
            status_code=status_code,
            values=captured,
        )

    def reset(self) -> None:
        """Forget every snapshot. For tests and for a process restart only."""

        self._by_key.clear()


#: Process-wide, for the reason both siblings give: the admin route that reads
#: this while a provider instance sits inside a provider generation the admin
#: routes cannot reach. One process, one answer per provider and credential.
OBSERVER = UsageHeaderObserver()
