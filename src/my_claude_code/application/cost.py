"""What one request cost, and which source said so.

Pricing follows the same metadata-discovery ladder as every other model fact in
MCC: the host's own answer first, then this provider's catalogue bucket, then a
reference catalogue, then a cross-provider vote, then nothing. Nothing here
branches on a provider id or a model name; the rung that answers decides, and
the rung is carried back so a reader can see how much to trust the number.

**Never a hardcoded price table.** Every rate below arrives from a live-fetched,
cached, conditionally-revalidated source with provenance. This module owns no
prices at all -- it is handed candidate rate cards and picks one.

**NULL is the answer when nothing resolved, and it has to survive.** Three of
the five implementations surveyed for this feature destroy the
unknown-versus-free distinction, each at a different layer: one coerces at
compute time, one at payload time, one at read time -- and the third does it
*despite* storing a correct nullable column. A free model prices at zero only
because a source said zero; an unpriced model prices at nothing at all. So
there is no ``or 0.0`` here, no ``?? 0`` in the serializer, and no
``COALESCE(cost_usd, 0)`` in any query that reads what this produces.

**One request is priced from one rung, whole.** A source that names an input
rate but no cache-read rate has not priced a request that read from cache;
mixing its input rate with another source's cache rate would produce a number
no source ever stated. Such a request falls to the next rung entire.
"""

from collections.abc import Sequence
from dataclasses import dataclass

#: ``cost_source`` values, in ladder order. Stored in the request log and
#: rendered by the dashboard, so they are a wire contract: renaming one
#: rewrites history's labels.
SOURCE_PROVIDER = "provider"
SOURCE_MODELS_DEV = "models_dev"
SOURCE_LITELLM = "litellm"
SOURCE_CROSS_PROVIDER = "cross_provider"

COST_SOURCES: tuple[str, ...] = (
    SOURCE_PROVIDER,
    SOURCE_MODELS_DEV,
    SOURCE_LITELLM,
    SOURCE_CROSS_PROVIDER,
)

#: The same three computed rungs, spelled as what they are when a price is
#: resolved long after the request happened. A retroactive price is not the
#: label the live path writes: ``models_dev`` means "models.dev priced this at
#: the moment it happened", and a request from August priced from September's
#: catalogue has to say so or the column stops being provenance and becomes a
#: guess wearing provenance's clothes.
SOURCE_MODELS_DEV_BACKFILL = "models_dev_backfill"
SOURCE_LITELLM_BACKFILL = "litellm_backfill"
SOURCE_CROSS_PROVIDER_BACKFILL = "cross_provider_backfill"

#: There is deliberately **no** ``provider_backfill``. The ``provider`` rung is
#: a figure the host reported for one specific request and it is gone; a
#: reported cost can never be reconstructed. Everything below is an estimate,
#: full stop, which is why ``cost_breakdown``'s ``reported_usd`` keeps matching
#: only the rows a host really did report.
_RETROACTIVE: dict[str, str] = {
    SOURCE_MODELS_DEV: SOURCE_MODELS_DEV_BACKFILL,
    SOURCE_LITELLM: SOURCE_LITELLM_BACKFILL,
    SOURCE_CROSS_PROVIDER: SOURCE_CROSS_PROVIDER_BACKFILL,
}

BACKFILL_COST_SOURCES: tuple[str, ...] = (
    SOURCE_MODELS_DEV_BACKFILL,
    SOURCE_LITELLM_BACKFILL,
    SOURCE_CROSS_PROVIDER_BACKFILL,
)

#: Stored on a row the backfill *tried* and could not price, with ``cost_usd``
#: still NULL. It is not a price and nothing ever sums it; it exists so the
#: predicate the backfill walks -- "no cost and no source" -- stops matching a
#: row whose answer is already known to be "nobody publishes a rate for this",
#: and 154,000 such rows are not re-priced on every start for the rest of the
#: log's life. NULL survives, because NULL is still the honest cost.
SOURCE_UNPRICED = "unpriced"

#: Every value that may appear in the stored ``cost_source`` column: a closed
#: set, so a reader can enumerate it and a renderer can be held to labelling
#: all of it.
ALL_COST_SOURCES: tuple[str, ...] = (
    *COST_SOURCES,
    *BACKFILL_COST_SOURCES,
    SOURCE_UNPRICED,
)


def retroactive_source(source: str) -> str | None:
    """Spell one live ladder rung as its retroactive equivalent.

    ``None`` for :data:`SOURCE_PROVIDER`, which has no retroactive form, and
    for anything that is not a computed rung -- a caller handed something else
    has resolved a price from a source this module does not know about, and
    inventing a label for it would be the one thing the ``cost_source``
    contract forbids.
    """
    return _RETROACTIVE.get(source)


#: How the ladder may be walked. Mirrors ccusage's three modes, and exists so
#: an operator can audit a host's own billing against a computed estimate
#: without editing code.
MODE_AUTO = "auto"
MODE_REPORTED_ONLY = "reported_only"
MODE_COMPUTED_ONLY = "computed_only"

COST_MODES: tuple[str, ...] = (MODE_AUTO, MODE_REPORTED_ONLY, MODE_COMPUTED_ONLY)


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """The counters one request is priced from.

    ``tokens_in`` excludes cached reads, exactly as the request log stores it:
    Anthropic's ``input_tokens`` is the uncached portion and the two cache
    counters are separate facts, so summing them here is arithmetic, not a
    reinterpretation.
    """

    tokens_in: int | None = None
    tokens_out: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    #: Reasoning tokens, when the host reported them. Part of ``tokens_out``,
    #: not an addition to it -- they are only ever *re-priced* out of it.
    reasoning_tokens: int | None = None
    #: True when ``reasoning_tokens`` is MCC's own estimate rather than the
    #: host's count. An estimated token count feeding a price is a double
    #: estimate and the modal says so.
    reasoning_estimated: bool = False

    @property
    def is_empty(self) -> bool:
        """Whether nothing was measured at all."""
        return not any(
            (
                self.tokens_in,
                self.tokens_out,
                self.cache_read_tokens,
                self.cache_write_tokens,
            )
        )


@dataclass(frozen=True, slots=True)
class RateCard:
    """One source's rates for one model, in **USD per single token**.

    Normalisation happens at the fetcher, never here: models.dev publishes per
    million and LiteLLM publishes per token, and converting at the call site is
    the single most likely place in this feature to ship a 1,000,000x error.
    Everything past a fetcher is per-token.

    ``None`` on a rate means the source did not state it, which is why
    :meth:`covers` exists: a request that read from cache cannot be priced by a
    card with no cache-read rate.
    """

    source: str
    input_price: float | None = None
    output_price: float | None = None
    cache_read_price: float | None = None
    cache_write_price: float | None = None
    #: A rate of its own for reasoning tokens, where the source publishes one
    #: (models.dev ``cost.reasoning``, LiteLLM ``output_cost_per_reasoning_token``).
    #: Absent everywhere else, and absent is not zero: reasoning then prices as
    #: output, which is what every host actually bills.
    reasoning_price: float | None = None
    #: A short human label for where in the ladder this came from -- the
    #: ``ResolutionTier`` name for a tiered lookup, the key shape for a flat
    #: one. Rendered in the modal beside the "est." badge.
    tier_label: str | None = None

    def covers(self, usage: TokenUsage) -> bool:
        """Whether this card states every rate this usage actually needs.

        A counter of zero needs no rate: a request that read nothing from cache
        is fully priced by a card that says nothing about cache reads.
        """
        if usage.tokens_in and self.input_price is None:
            return False
        if usage.tokens_out and self.output_price is None:
            return False
        if usage.cache_read_tokens and self.cache_read_price is None:
            return False
        return not (usage.cache_write_tokens and self.cache_write_price is None)

    @property
    def is_empty(self) -> bool:
        """Whether the source stated no rate at all."""
        return all(
            rate is None
            for rate in (
                self.input_price,
                self.output_price,
                self.cache_read_price,
                self.cache_write_price,
            )
        )


@dataclass(frozen=True, slots=True)
class CostResult:
    """The priced answer, or the honest absence of one."""

    cost_usd: float | None = None
    cost_source: str | None = None
    #: The rung label, for the modal's "est. - models.dev - exact match".
    tier_label: str | None = None
    #: True when the priced reasoning tokens were MCC's estimate.
    reasoning_estimated: bool = False

    @property
    def is_priced(self) -> bool:
        return self.cost_usd is not None


def compute_from_rates(usage: TokenUsage, card: RateCard) -> float | None:
    """Multiply one rate card by one usage, or ``None`` if it cannot.

    Cache reads and cache writes price at their own rates, never at the base
    input rate: a fallback like that invents a number the source never stated,
    and on a warm 268k-token prompt it is wrong by an order of magnitude.

    Reasoning tokens price as output unless the card carries a reasoning rate,
    in which case they are subtracted from the output count and re-priced --
    they are part of ``tokens_out``, not an addition to it.
    """
    if not card.covers(usage):
        return None
    total = 0.0
    if usage.tokens_in and card.input_price is not None:
        total += usage.tokens_in * card.input_price
    if usage.cache_read_tokens and card.cache_read_price is not None:
        total += usage.cache_read_tokens * card.cache_read_price
    if usage.cache_write_tokens and card.cache_write_price is not None:
        total += usage.cache_write_tokens * card.cache_write_price
    output = usage.tokens_out or 0
    reasoning = usage.reasoning_tokens or 0
    if card.reasoning_price is not None and reasoning:
        # Bounded by the reported output, so a bad estimate can never claim
        # more reasoning tokens than the host billed output tokens.
        reasoning = min(reasoning, output)
        total += reasoning * card.reasoning_price
        output -= reasoning
    if output and card.output_price is not None:
        total += output * card.output_price
    return total


def resolve_cost(
    *,
    reported_usd: float | None,
    usage: TokenUsage,
    cards: Sequence[RateCard],
    mode: str = MODE_AUTO,
) -> CostResult:
    """Walk the ladder once and return what it found.

    ``cards`` are the computed rungs in ladder order -- models.dev, LiteLLM,
    the cross-provider vote -- already normalised to USD per token by their
    fetchers. The first card that states every rate this usage needs wins
    outright; a card that does not is skipped whole rather than patched from
    the next one.

    A reported cost wins over every computed rung and is never averaged with,
    corrected by, or compared against one. ``reported_only`` stops after it;
    ``computed_only`` skips it, which is how a host's own billing gets audited.
    """
    if mode != MODE_COMPUTED_ONLY and reported_usd is not None:
        return CostResult(cost_usd=reported_usd, cost_source=SOURCE_PROVIDER)
    if mode == MODE_REPORTED_ONLY:
        return CostResult()
    if usage.is_empty:
        # Nothing was measured, so nothing can be computed. Distinct from a
        # measured request nobody publishes a price for, but stored the same
        # way: NULL, never zero.
        return CostResult()
    for card in cards:
        if card.is_empty:
            continue
        amount = compute_from_rates(usage, card)
        if amount is None:
            continue
        return CostResult(
            cost_usd=amount,
            cost_source=card.source,
            tier_label=card.tier_label,
            reasoning_estimated=(
                usage.reasoning_estimated
                and card.reasoning_price is not None
                and bool(usage.reasoning_tokens)
            ),
        )
    return CostResult()


__all__ = [
    "ALL_COST_SOURCES",
    "BACKFILL_COST_SOURCES",
    "COST_MODES",
    "COST_SOURCES",
    "MODE_AUTO",
    "MODE_COMPUTED_ONLY",
    "MODE_REPORTED_ONLY",
    "SOURCE_CROSS_PROVIDER",
    "SOURCE_CROSS_PROVIDER_BACKFILL",
    "SOURCE_LITELLM",
    "SOURCE_LITELLM_BACKFILL",
    "SOURCE_MODELS_DEV",
    "SOURCE_MODELS_DEV_BACKFILL",
    "SOURCE_PROVIDER",
    "SOURCE_UNPRICED",
    "CostResult",
    "RateCard",
    "TokenUsage",
    "compute_from_rates",
    "resolve_cost",
    "retroactive_source",
]
