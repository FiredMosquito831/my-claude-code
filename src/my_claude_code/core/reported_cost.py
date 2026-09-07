"""Carry a host's own answer about what a request cost, from wire to log.

MCC never knew what a request cost. Prices resolved -- ``model_prices_tiered``
has returned USD/1M with a resolution tier for every (provider, model) since
6.35.0 -- and nothing in ``src/`` ever multiplied one by a token count. This
module is the first half of closing that: the rung above every price table,
which is the host simply telling us.

One host tells us today. OpenRouter returns ``usage.cost`` in the final SSE
chunk of every streamed response, always, with no request-side flag: the
``usage: {include: true}`` and ``stream_options: {include_usage: true}`` opt-ins
are documented as deprecated and inert. Nothing here names OpenRouter. Any
OpenAI-shaped host that reports a cost under the same key is read the same way,
which is the whole point -- the ladder decides, not a provider branch.

The transport is the collector pattern this codebase already uses twice, for
the same reason both times: the value is produced deep inside a provider and
consumed at the request-log commit, and every layer between them speaks
Anthropic SSE, which has no field for a cost. Smuggling one into the translated
stream would put a proprietary number in front of the client. A mutable object
installed at the API boundary and written by the provider stays visible across
any number of context copies, which a ``ContextVar`` holding an immutable value
would not.

**The BYOK trap, which is the whole reason this carries four fields.** On
bring-your-own-key traffic OpenRouter's ``cost`` is only its own ~5% surcharge,
and the actual inference charge is ``cost_details.upstream_inference_cost``.
Storing ``cost`` alone there would under-report a request by roughly twenty
times. ``usage.is_byok`` decides it, so the decision is recorded rather than
guessed -- and where the flag is absent the reported rung is declined entirely
and the request prices from a table instead, because a figure that might be a
surcharge is worse than an honest estimate.
"""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

#: Keys read off a host's usage block. Named here rather than inline so the
#: parse and its tests cannot drift.
COST_KEY = "cost"
COST_DETAILS_KEY = "cost_details"
UPSTREAM_COST_KEY = "upstream_inference_cost"
IS_BYOK_KEY = "is_byok"
COMPLETION_DETAILS_KEY = "completion_tokens_details"
REASONING_TOKENS_KEY = "reasoning_tokens"


@dataclass(slots=True)
class ReportedCost:
    """What one host said this request cost, and what it takes to read it."""

    #: The charge the host names, in USD. On BYOK traffic this is the
    #: surcharge only, never the whole bill.
    cost_usd: float | None = None
    #: The upstream inference charge a BYOK response reports separately.
    upstream_cost_usd: float | None = None
    #: Whether the host billed this as bring-your-own-key. ``None`` means the
    #: response did not say, which is not the same as ``False``.
    is_byok: bool | None = None
    #: Reasoning tokens the host reported, so a source with a reasoning rate
    #: can price them at it instead of at the output rate.
    reasoning_tokens: int | None = None

    @property
    def total_usd(self) -> float | None:
        """The request's whole cost according to the host, or ``None``.

        ``None`` on a response that reports a cost without saying whether it
        was BYOK: the number is then either the whole bill or one twentieth of
        it, and there is no way to tell which. Declining the rung costs an
        estimate; accepting it costs a wrong invoice.
        """
        if self.cost_usd is None:
            return None
        if self.is_byok is None:
            return None
        if not self.is_byok:
            return self.cost_usd
        if self.upstream_cost_usd is None:
            return None
        return self.cost_usd + self.upstream_cost_usd


_REPORTED_COST: ContextVar[ReportedCost | None] = ContextVar(
    "fcc_reported_cost", default=None
)


def install_reported_cost() -> ReportedCost:
    """Start collecting a host-reported cost for the current request."""
    slot = ReportedCost()
    _REPORTED_COST.set(slot)
    return slot


def reported_cost() -> ReportedCost | None:
    """Return the collector for the request in flight, if one is installed."""
    return _REPORTED_COST.get()


@contextmanager
def paused_reported_cost() -> Iterator[None]:
    """Stop attributing reported costs to the tracked request.

    A describe call is a second request MCC issues on its own behalf, on a
    different model and often a different provider. Left recording, its host's
    ``usage.cost`` would be stored as the *client's* request cost -- the same
    shape of mis-attribution ``paused_wire_trace`` and ``paused_recovery_trace``
    exist to prevent, and with the same fix. The describe hop is still priced;
    it prices from a table, on its own attempt row.
    """
    token = _REPORTED_COST.set(None)
    try:
        yield
    finally:
        _REPORTED_COST.reset(token)


def _lookup(source: Any, key: str) -> Any:
    """Read one key off a Mapping, an SDK model, or its ``model_extra``.

    Hosts that report a cost report it as an undeclared extra field on the
    OpenAI usage model, so ``getattr`` alone finds nothing.
    """
    if source is None:
        return None
    if isinstance(source, Mapping):
        return source.get(key)
    value = getattr(source, key, None)
    if value is None:
        extra = getattr(source, "model_extra", None)
        if isinstance(extra, Mapping):
            return extra.get(key)
    return value


def _as_float(value: Any) -> float | None:
    """Coerce a reported charge to a float, or ``None`` if it is not one."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def record_reported_usage(usage_info: Any) -> None:
    """Fold one host usage block into the collector, if one is installed.

    A no-op outside a tracked request and on a usage block that reports no
    cost, so every provider may call it unconditionally and a host that says
    nothing costs nothing.
    """
    slot = _REPORTED_COST.get()
    if slot is None or usage_info is None:
        return
    cost = _as_float(_lookup(usage_info, COST_KEY))
    if cost is not None:
        slot.cost_usd = cost
    details = _lookup(usage_info, COST_DETAILS_KEY)
    upstream = _as_float(_lookup(details, UPSTREAM_COST_KEY))
    if upstream is not None:
        slot.upstream_cost_usd = upstream
    byok = _lookup(usage_info, IS_BYOK_KEY)
    if isinstance(byok, bool):
        slot.is_byok = byok
    elif cost is not None and details is not None and upstream is None:
        # A host that itemises ``cost_details`` and reports no upstream charge
        # has answered the BYOK question by omission: there is no upstream
        # inference cost to add. Only that shape is read as ``False``; a bare
        # ``cost`` with no itemisation stays unknown.
        slot.is_byok = False
    reasoning = _as_int(
        _lookup(_lookup(usage_info, COMPLETION_DETAILS_KEY), REASONING_TOKENS_KEY)
    )
    if reasoning is not None:
        slot.reasoning_tokens = reasoning


__all__ = [
    "ReportedCost",
    "install_reported_cost",
    "paused_reported_cost",
    "record_reported_usage",
    "reported_cost",
]
