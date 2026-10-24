"""What a marketplace provider reads off the wire, on every door (7.109.0).

The provider half of ``config/marketplaces.py``: the three things the shared
code can only ask a declaring profile, never decide for itself.

* **The list rows.** A marketplace list row may carry one ask per seller
  *account* -- tens of thousands of numbers for one model (RES §3, §13.5) --
  and 7.86.0 keeps every row's own words verbatim beside the catalogue. So
  the row's ``asks_*`` arrays are regrouped into price levels before the row
  is read at all: ``{"summarised_from": 2295, "levels": [[0.025, 1052], ...]}``,
  the row's own numbers, losslessly regrouped. Both shapes the research names
  are tolerated: one entry per account (a number) or per level (a
  ``[price, count]`` pair or a ``{price, count}`` object).
* **The refusal.** InferHub answers "nothing under your ceiling" with a 402
  whose structured code is ``no_provider_under_bid`` -- and a 402 is also
  "balance too low" there, so the status alone decides nothing. Only that
  code, read from ``error.code`` or a top-level ``code``, becomes the short
  400; every other 402 keeps the shared meaning (out of credits) untouched.
* **The read-back.** The allow-listed response headers, verbatim, per
  attempt, and ``usage.cost`` as the whole bill.

Nothing here runs for a profile that declares no marketplace: every caller
checks ``profile.marketplace`` first.
"""

import json
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

import httpx

from my_claude_code.application.marketplace import (
    MarketplaceChoice,
    no_offer_message,
)
from my_claude_code.config.marketplaces import MarketplaceDialect
from my_claude_code.core.failures import ExecutionFailure, FailureKind
from my_claude_code.core.wire_capture import record_marketplace_readback
from my_claude_code.providers.model_listing import model_list_items


@dataclass(frozen=True, slots=True)
class MarketplaceAttempt:
    """The model one marketplace attempt is for, and its ceiling (or none)."""

    model_id: str
    choice: MarketplaceChoice | None

    @property
    def headers(self) -> Mapping[str, str]:
        return {} if self.choice is None else self.choice.headers


#: The attempt in flight on a marketplace provider's Messages or Responses
#: door. Those doors build their headers deep inside a sender that is shared
#: by every request of the provider, so the per-attempt ceiling reaches them
#: here rather than through a provider attribute two requests would share.
#: Set only by a provider whose profile declares a marketplace.
_ATTEMPT: ContextVar[MarketplaceAttempt | None] = ContextVar(
    "mcc_marketplace_attempt", default=None
)


@contextmanager
def marketplace_attempt(attempt: MarketplaceAttempt) -> Iterator[None]:
    """Make ``attempt`` the one this request's doors send, for the block."""

    token = _ATTEMPT.set(attempt)
    try:
        yield
    finally:
        # A stream finalised from another task cannot reset its own token;
        # that task's context is discarded with it.
        with suppress(ValueError):
            _ATTEMPT.reset(token)


def current_marketplace_attempt() -> MarketplaceAttempt | None:
    return _ATTEMPT.get()


#: How far up a ``__cause__`` chain the refusal reader looks: a door raises
#: its classified failure ``from`` the transport error, one link away.
_CAUSE_DEPTH = 4
#: The status the short refusal is answered with: the door's own 400.
NO_OFFER_REFUSAL_STATUS = 400


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def summarise_asks(values: Any) -> Any:
    """One ``asks_*`` array as price levels, or the value unchanged.

    Unchanged for anything that is not a list (``None``, an already
    summarised object): there is nothing to regroup.
    """

    if not isinstance(values, list):
        return values
    levels: Counter[float] = Counter()
    for entry in values:
        price = _number(entry)
        count: int | None = 1
        if price is None and isinstance(entry, Sequence) and not isinstance(entry, str):
            if len(entry) == 2:
                price, count = _number(entry[0]), _count(entry[1])
        elif price is None and isinstance(entry, Mapping):
            price, count = _number(entry.get("price")), _count(entry.get("count"))
        if price is None or count is None:
            continue
        levels[price] += count
    return {
        "summarised_from": len(values),
        "levels": [[price, levels[price]] for price in sorted(levels)],
    }


def _row_dict(item: Any) -> dict[str, Any] | None:
    if isinstance(item, Mapping):
        return {str(key): value for key, value in item.items()}
    dump = getattr(item, "model_dump", None)
    if callable(dump):
        row = dump(mode="json", by_alias=True, exclude_unset=True)
        if isinstance(row, Mapping):
            return {str(key): value for key, value in row.items()}
    return None


def summarise_listing_payload(
    payload: Any, dialect: MarketplaceDialect, *, provider_name: str
) -> dict[str, Any]:
    """The model-list payload with every row's asks regrouped into levels."""

    rows: list[Any] = []
    for item in model_list_items(
        payload, provider_name=provider_name, collection_field="data"
    ):
        row = _row_dict(item)
        if row is None:
            rows.append(item)
            continue
        pricing = row.get(dialect.pricing_field)
        if isinstance(pricing, Mapping):
            summarised = dict(pricing)
            for field in dialect.ask_fields:
                if field in summarised:
                    summarised[field] = summarise_asks(summarised[field])
            row[dialect.pricing_field] = summarised
        rows.append(row)
    return {"object": "list", "data": rows}


# ------------------------------------------------------------ the refusal


def _status(error: BaseException) -> int | None:
    status = getattr(error, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def _bodies(error: BaseException) -> list[Mapping[str, Any]]:
    """Every structured body the error carries: the whole response first."""

    found: list[Mapping[str, Any]] = []
    response = getattr(error, "response", None)
    if isinstance(response, httpx.Response):
        try:
            parsed = json.loads(response.text)
        except Exception:
            parsed = None
        if isinstance(parsed, Mapping):
            found.append(parsed)
    body = getattr(error, "body", None)
    if isinstance(body, Mapping):
        found.append(body)
    return found


def _at(body: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = body
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _carrier(
    dialect: MarketplaceDialect, error: BaseException
) -> Mapping[str, Any] | None:
    """The body of a no-offer refusal behind ``error``, or ``None``."""

    current: BaseException | None = error
    for _ in range(_CAUSE_DEPTH):
        if current is None:
            return None
        if _status(current) == dialect.no_offer_status:
            for body in _bodies(current):
                for path in dialect.no_offer_code_paths:
                    if _at(body, path) == dialect.no_offer_code:
                        return body
        current = current.__cause__
    return None


def _minimum(body: Mapping[str, Any], name: str) -> float | None:
    for value in (body.get(name), _at(body, ("error", name))):
        number = _number(value)
        if number is not None:
            return number
    return None


def no_offer_failure(
    dialect: MarketplaceDialect,
    error: BaseException,
    *,
    model_id: str,
    choice: MarketplaceChoice | None,
) -> ExecutionFailure | None:
    """InferHub's "nothing under your ceiling", as the door's short 400.

    ``None`` for every other error -- a 402 without the code included -- so
    the shared policy classifies it exactly as it always did. Never retried:
    MCC does not re-bid (a re-bid is a price rise nobody agreed to).
    """

    body = _carrier(dialect, error)
    if body is None:
        return None
    minimums = (
        _minimum(body, dialect.no_offer_min_fields[0]),
        _minimum(body, dialect.no_offer_min_fields[1]),
    )
    return ExecutionFailure(
        kind=FailureKind.INVALID_REQUEST,
        status_code=NO_OFFER_REFUSAL_STATUS,
        message=no_offer_message(
            dialect, model_id=model_id, choice=choice, minimums=minimums
        ),
        retryable=False,
    )


# ------------------------------------------------------------ the read-back


def marketplace_headers_of(dialect: MarketplaceDialect, headers: Any) -> dict[str, str]:
    """The allow-listed response headers, verbatim, in the order they came."""

    if headers is None:
        return {}
    items = headers.items() if hasattr(headers, "items") else ()
    kept: dict[str, str] = {}
    for name, value in items:
        lowered = str(name).lower()
        if lowered in dialect.response_headers:
            kept[lowered] = str(value)
    return kept


def record_marketplace_headers(dialect: MarketplaceDialect, headers: Any) -> None:
    """Keep this attempt's allow-listed response headers, if it sent any."""

    kept = marketplace_headers_of(dialect, headers)
    if kept:
        record_marketplace_readback(headers=kept)


def record_marketplace_error_headers(
    dialect: MarketplaceDialect, error: BaseException
) -> None:
    """The same, off a refused response, wherever the error carries it."""

    current: BaseException | None = error
    for _ in range(_CAUSE_DEPTH):
        if current is None:
            return
        response = getattr(current, "response", None)
        if isinstance(response, httpx.Response):
            record_marketplace_headers(dialect, response.headers)
            return
        current = current.__cause__


__all__ = [
    "NO_OFFER_REFUSAL_STATUS",
    "MarketplaceAttempt",
    "current_marketplace_attempt",
    "marketplace_attempt",
    "marketplace_headers_of",
    "no_offer_failure",
    "record_marketplace_error_headers",
    "record_marketplace_headers",
    "summarise_asks",
    "summarise_listing_payload",
]
