"""Exit-scoped failures, and what a chain says when its exits run out (7.81.0).

The rules of "Keep trying exits until one answers", the per-chain switch a
Proxying-page card ticks (``ProxyChainPlan.until_served``). Kept here, beside
the frozen ``proxy_rotating.py`` rather than in it, so that file's own diff is
only the control flow that reads them -- and so the media copy of the loop
(``providers/media/proxy_pool.py``) reads the very same rules.

What a ticked chain treats as **the exit's fault**, from declared data only --
no provider or model name is read anywhere:

* *reachability* -- the proxy refused, timed out connecting, answered ``407``
  or spoke broken SOCKS: :func:`~.proxy_rotating.proxy_reachability_failure`,
  unchanged, and every chain has always moved on it;
* *a country refusal* -- a ``403`` whose host words match the region pattern
  the Responses surface recovery already reads
  (:func:`~my_claude_code.providers.recovery.surface.is_region_refusal`);
* *a dropped connection before the first byte* -- :func:`proxy_transport_failure`;
* *the trigger chips the operator ticked* (``rate_limit`` and the rest), with
  exactly today's meaning.

Bounded by what already bounds a chain: the card's "Switches per request"
under ``PROXY_MAX_SWITCHES_PER_REQUEST`` for the refusals, and
``PROXY_MAX_LIVE_FAILURES`` for every failed exit -- the user's decision of
2026-10-06 19:49 ("respect the set var for max retries before going to next in
chain that we have existing"). When a ticked chain runs out -- its switch limit
reached, its live-failure bound spent with Direct not allowed, or no exit left
to select -- on what an exit causes (:func:`exits_ran_out`), the request ends
with :class:`ExitsExhausted`: a classified ``UNAVAILABLE`` the credential pool
rotates on without charging the key, the route's health does not count, and
the executor answers by moving to the next model of the fallback chain. The
model is not benched for exits that refused; the refused exits are
(``core/proxy_exit_memory``).
"""

from datetime import UTC, datetime

import httpx

from my_claude_code.config.credential_names import (
    credential_fingerprint,
    oauth_credential_id,
)
from my_claude_code.config.credentials import mask_key_label
from my_claude_code.core.failures import (
    ExecutionFailure,
    FailureKind,
    failure_kind,
    find_execution_failure,
)
from my_claude_code.core.proxy_exit_memory import BLOCKED, SPENT
from my_claude_code.providers.base import ProviderConfig

#: Transport failures that, through a proxy and before the first chunk, are the
#: exit dropping the request: the same types the leaf's retry ladder already
#: retries as pre-response transport faults
#: (``failure_policy.retryable_upstream_transport_error``), read along the
#: cause chain the way the reachability reader is. Connect-class failures are
#: reachability and are answered before this is asked.
_TRANSPORT_TYPES: tuple[type[BaseException], ...] = (
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
    httpx.TimeoutException,
    httpx.NetworkError,
)

#: The OpenAI SDK's two transport errors, matched by name so this module does
#: not import the SDK (~2 s cold; the startup import-cost contract).
_SDK_TRANSPORT_NAMES = frozenset({"APIConnectionError", "APITimeoutError"})
_SDK_MODULE = "openai"

_CAUSE_DEPTH = 8


def _chain(error: BaseException) -> list[BaseException]:
    seen: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and len(seen) < _CAUSE_DEPTH:
        seen.append(current)
        current = current.__cause__ or current.__context__
    return seen


def _is_sdk_transport(error: BaseException) -> bool:
    for cls in type(error).__mro__:
        module = cls.__module__ or ""
        if cls.__name__ in _SDK_TRANSPORT_NAMES and (
            module == _SDK_MODULE or module.startswith(f"{_SDK_MODULE}.")
        ):
            return True
    return False


def proxy_transport_failure(
    error: BaseException, *, proxied: bool, before_first_chunk: bool = True
) -> str | None:
    """Name the way the exit dropped a request, or ``None`` if it did not.

    Only for a proxied rung (a dropped connection on this machine's own
    address is the provider's), and only before the first chunk -- after it a
    request may not move address anyway. ``httpx.PoolTimeout`` is never the
    exit's: it is this computer waiting for a connection from its own pool.
    """

    if not proxied or not before_first_chunk:
        return None
    chain = _chain(error)
    if any(isinstance(link, httpx.PoolTimeout) for link in chain):
        return None
    for link in chain:
        if isinstance(link, _TRANSPORT_TYPES) or _is_sdk_transport(link):
            return type(link).__name__
    return None


class ExitsExhausted(ExecutionFailure):
    """A ticked chain ran out of exits for this request.

    ``UNAVAILABLE``, not retryable: the credential pool rotates on it without
    charging the key (only 401/403, 429 and the credits phrase do), the route's
    health does not count it, and the executor moves to the next model.
    ``safe_message``: the sentence is MCC's own, safe to quote.
    """

    safe_message = True


#: The status an exhausted chain answers: what the 7.19.0 "every proxy
#: refused" path already raises -- the provider could not be reached through
#: any exit this request may use.
EXHAUSTED_STATUS = 502


def exits_exhausted(message: str) -> ExitsExhausted:
    return ExitsExhausted(
        kind=FailureKind.UNAVAILABLE,
        status_code=EXHAUSTED_STATUS,
        message=message,
        retryable=False,
    )


def exits_ran_out(error: BaseException | None, advance: str, *, region: bool) -> bool:
    """Whether a ticked chain that stopped on ``error`` ran out of *exits*.

    True for what an exit causes -- a rate limit (a 429 or a free-usage limit),
    a country refusal, an exit that could not be reached or dropped the request
    -- and when nothing was tried at all. Re-raised verbatim, those reach the
    key pool as a charge on the key or a bench on the model for exits this
    request never tried; ended as :class:`ExitsExhausted` they move the request
    to its next model and charge nothing.

    False for every other failure a chip moved the chain on (``quota``,
    ``timeout``, ``upstream``, a model rejection ...): those say something
    about the key or the model, so they reach the pool exactly as they always
    did -- a key out of credits is still charged as one.
    """

    if error is None or region or advance == "reachability":
        return True
    return failure_kind(error) is FailureKind.RATE_LIMIT


def exit_outcome_word(error: BaseException, advance: str, *, region: bool) -> str:
    """A few words for what one exit answered, for the exhaustion sentence."""

    if region:
        return "country refusal"
    if advance == "reachability":
        return "unreachable or dropped"
    failure = find_execution_failure(error)
    kind = failure_kind(error)
    name = kind.value.replace("_", " ") if kind is not None else type(error).__name__
    if failure is not None and failure.status_code:
        return f"{failure.status_code} {name}"
    return name


def exhaustion_sentence(
    name: str,
    *,
    outcomes: list[str],
    skipped: int,
    switch_limit: int | None,
    soonest: float | None,
    refused: int = 0,
) -> str:
    """What a request whose ticked chain ran out is told, and the log shows."""

    census: dict[str, int] = {}
    for word in outcomes:
        census[word] = census.get(word, 0) + 1
    counted = ", ".join(
        f"{count} \N{MULTIPLICATION SIGN} {word}"
        for word, count in sorted(census.items(), key=lambda item: -item[1])
    )
    if outcomes:
        head = (
            f"Every exit this request could use in {name}'s proxy chain refused "
            f"or failed it: tried {len(outcomes)}"
            + (f" ({counted})" if counted else "")
        )
    else:
        head = f"No exit in {name}'s proxy chain can carry a request right now"
    parts = [head]
    if skipped:
        parts.append(
            f"{skipped} skipped from memory (spent, blocked for their country, "
            "or unreachable)"
        )
    if refused:
        parts.append(f"{refused} refused for intercepting TLS")
    sentence = ", ".join(parts)
    if switch_limit is not None:
        sentence += (
            f"; the chain's switch limit ({switch_limit} per request) was reached"
        )
    sentence += "."
    if soonest is not None and soonest > 0:
        sentence += f" The soonest remembered exit is usable again in {soonest:.0f} s."
    sentence += (
        " The request moves on to the next model of its fallback chain; the key "
        f"is not charged. Proxying page -> {name}."
    )
    return sentence


def _utc_clock(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%H:%M:%S UTC")


def dial_memory_text(
    state: str,
    *,
    seconds: float = 0.0,
    until_wall: float | None = None,
    stated_wait: float | None = None,
) -> str:
    """What the request detail says MCC now remembers about one dialled exit.

    ``state`` is a memory state (``spent`` / ``blocked``) or ``unreachable`` /
    ``dropped`` for an exit put on the reachability ledger.
    """

    when = "" if until_wall is None else f" until {_utc_clock(until_wall)}"
    if state == SPENT:
        why = (
            f"stated {stated_wait:g} s"
            if stated_wait is not None
            else f"no wait stated: {seconds:.0f} s"
        )
        return f"remembered spent{when} ({why})"
    if state == BLOCKED:
        return f"remembered blocked for its country{when} ({seconds:.0f} s)"
    if state == "dropped":
        return "dropped before the first byte: skipped until a check passes"
    return "unreachable: skipped until a check passes"


def credential_identity(config: ProviderConfig) -> tuple[str, str]:
    """``(identity, label)`` of the one credential a chain's legs carry.

    The identity keys the exit memory -- a fingerprint, never the secret -- and
    the label is what the Proxying page may show: the masked key, or the OAuth
    account id an account pool already shows.
    """

    if config.oauth_account_id:
        return oauth_credential_id(config.oauth_account_id), config.oauth_account_id
    if config.api_key:
        return credential_fingerprint(config.api_key), mask_key_label(config.api_key)
    return "", ""


__all__ = [
    "EXHAUSTED_STATUS",
    "ExitsExhausted",
    "credential_identity",
    "dial_memory_text",
    "exhaustion_sentence",
    "exit_outcome_word",
    "exits_exhausted",
    "exits_ran_out",
    "proxy_transport_failure",
]
