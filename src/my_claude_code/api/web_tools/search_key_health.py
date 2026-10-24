"""The key health the Web Search page shows, from whichever pool serves (7.113.0).

A search provider's keys live in one of two pools, and which one serves is a
property of today's route:

* no ``WEB_SEARCH_CHAIN``, or a chain that does not list the provider: the
  provider's own ``KeyPool`` (``websearch/rotation.py``, the 7.112.1 rules),
  read through ``search_providers.cached_key_pool_snapshot`` exactly as before;
* a chain that lists the provider: its ``SearchKeyBooks``
  (``search_books.py``, 7.112.0) -- the model path's own
  ``CredentialRotationState``.

The two report in different shapes and spell their states differently: the
pool's snapshot says ``locked_out``, the model metrics say ``LOCKED_OUT``.
The page reads ONE shape and ONE spelling -- the pool snapshot's, which is
what it has always read -- so the books are projected into it here.
:data:`STATE_SPELLING` is the whole mapping; the default pool needs none.

Beside that shape, :func:`key_facts` says *why* a key is benched, in one
vocabulary for both pools, so the page can use the model rules' words
("benched 60 s: rate limited", "out of credits", "locked out, tier 2 of 3").
"""

from collections.abc import Mapping
from typing import Any

from my_claude_code.config.model_refs import parse_model_ref_chain
from my_claude_code.config.settings import Settings
from my_claude_code.providers.credential_rotation import QUOTA_BENCH_REASON

from .search_books import SearchKeyBooks

#: The model metrics' state names (``PoolHealthState.name``) -> the one
#: spelling the page reads (``PoolHealthState.value``, the pool snapshot's).
#: Every state has an entry; a name missing here fails loudly rather than
#: rendering as a state the page does not know.
STATE_SPELLING: Mapping[str, str] = {
    "HEALTHY": "healthy",
    "COOLDOWN": "cooldown",
    "CIRCUIT_OPEN": "circuit_open",
    "LOCKED_OUT": "locked_out",
}

#: ``key_facts`` reasons: None while healthy, else one of these.
REASON_RATE_LIMITED = "rate_limited"
REASON_OUT_OF_CREDITS = "out_of_credits"
REASON_REJECTED = "rejected"
REASON_FAILURES = "failures"

POOL_DEFAULT = "default"
POOL_CHAIN = "chain"


def chain_entries(settings: Settings) -> tuple[str, ...]:
    """The entries of ``WEB_SEARCH_CHAIN``; empty when no chain is set."""

    chain = settings.web_search_chain
    return parse_model_ref_chain(chain) if chain else ()


def serving_pool(settings: Settings, provider_id: str) -> str:
    """Which pool serves this provider on today's route."""

    return POOL_CHAIN if provider_id in chain_entries(settings) else POOL_DEFAULT


def books_match(books: SearchKeyBooks, keys: tuple[str, ...]) -> bool:
    """Whether the books were built for exactly these keys, in this order.

    Health is index-keyed: books built for another key list describe other
    keys, and the next chain search rebuilds them.
    """

    return books.keys == (keys or ("",))


def books_health(
    books: SearchKeyBooks,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The books in the pool snapshot's shape, and their :func:`key_facts`.

    The snapshot's keys are filled from the model metrics; the two the
    metrics do not keep (``successes``, ``consecutive_failures``) are
    ``None`` -- not measured, never a zero.
    """

    tiers = len(books.policy.lockout_tiers)
    keys: list[dict[str, Any]] = []
    facts: list[dict[str, Any]] = []
    for index, entry in enumerate(books.state.get_metrics()):
        state = STATE_SPELLING[str(entry["state"])]
        if state == "healthy":
            remaining = 0.0
        elif state == "locked_out":
            remaining = float(entry["lockout_remaining"])
        else:
            remaining = float(entry["cooldown_remaining"])
        remaining = round(max(0.0, remaining), 3)
        reason_text = entry.get("cooldown_reason")
        keys.append(
            {
                "index": index,
                "key_label": books.label(index),
                "state": state,
                "state_remaining_seconds": remaining,
                "requests": entry["request_count"],
                "successes": None,
                "failures": entry["failure_count"],
                "consecutive_failures": None,
                "rate_limits": entry["rate_limits"],
                "lockouts": entry["auth_failures"],
                "last_error": reason_text,
            }
        )
        facts.append(
            {
                "index": index,
                "state": state,
                "remaining_s": remaining,
                "reason": _books_reason(state, reason_text),
                "lockout_tier": min(int(entry["auth_failures"]), tiers),
                "lockout_tiers": tiers,
            }
        )
    return {"policy": books.state.policy, "keys": keys}, facts


def _books_reason(state: str, cooldown_reason: object) -> str | None:
    """Why one books key is benched.

    The model rules bench a key for exactly three things: a rejected key
    (locked out), a 429 or an exact out-of-credits phrase (a cooldown, the
    latter named by the pool). The breaker is not reached by them; it is
    named anyway so no state is left without words.
    """

    if state == "healthy":
        return None
    if state == "locked_out":
        return REASON_REJECTED
    if state == "cooldown":
        return (
            REASON_OUT_OF_CREDITS
            if cooldown_reason == QUOTA_BENCH_REASON
            else REASON_RATE_LIMITED
        )
    return REASON_FAILURES
