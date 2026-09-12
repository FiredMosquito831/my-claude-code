"""Assemble the pricing ladder's rungs for one routed (provider, model).

``application.cost`` owns the ladder and owns no prices; this owns the wiring
between it and the two live catalogues. The split is not decoration: the
application layer may not import providers, and every price in this product has
to come from a fetched, cached, revalidated source with provenance rather than
from a table in the package.

The rungs, in order, are exactly the rungs the capability ladder walks -- which
is the whole design brief. models.dev resolves a price per *field* with its own
``ResolutionTier``, so one lookup produces two cards: the authoritative and
reference tiers (this provider's own bucket, then the curated OpenRouter
catalogue) become the ``models_dev`` rung, and the approximate cross-provider
tiers become the ``cross_provider`` rung underneath LiteLLM. A field is never
promoted across that line, so a bucket answer and a vote can never be mixed
into one price.
"""

from collections.abc import Mapping

from my_claude_code.application.cost import (
    MODE_COMPUTED_ONLY,
    SOURCE_CROSS_PROVIDER,
    SOURCE_MODELS_DEV,
    SOURCE_UNPRICED,
    RateCard,
    TokenUsage,
    resolve_cost,
    retroactive_source,
)
from my_claude_code.core.model_ids import ResolutionTier
from my_claude_code.core.request_log import CostBackfillPricer
from my_claude_code.providers.runtime.litellm_prices import litellm_rate_card
from my_claude_code.providers.runtime.models_dev import (
    model_prices_tiered,
    read_models_dev_cache,
)

#: models.dev field name -> :class:`RateCard` field name. The names already
#: agree; the map exists so a rename upstream fails here rather than silently
#: dropping a rate.
_MODELS_DEV_FIELDS: tuple[tuple[str, str], ...] = (
    ("input_price", "input_price"),
    ("output_price", "output_price"),
    ("cache_read_price", "cache_read_price"),
    ("cache_write_price", "cache_write_price"),
    ("reasoning_price", "reasoning_price"),
)

#: models.dev publishes USD per **million** tokens; everything past a fetcher
#: in this product is USD per single token. This is the division that keeps the
#: two sources -- whose units are exact opposites -- from differing by 1e6.
_PER_MILLION = 1_000_000.0


def _cards_from_models_dev(
    resolved: Mapping[str, tuple[float | None, ResolutionTier | None]],
) -> tuple[RateCard | None, RateCard | None]:
    """Split one tiered price lookup into its authoritative and voted cards."""
    exact: dict[str, float | None] = {}
    approximate: dict[str, float | None] = {}
    exact_tier: ResolutionTier | None = None
    approximate_tier: ResolutionTier | None = None
    for source_field, card_field in _MODELS_DEV_FIELDS:
        value, tier = resolved.get(source_field, (None, None))
        if value is None or tier is None:
            exact[card_field] = None
            approximate[card_field] = None
            continue
        rate = value / _PER_MILLION
        if tier.is_approximate:
            approximate[card_field] = rate
            exact[card_field] = None
            if approximate_tier is None or tier > approximate_tier:
                approximate_tier = tier
        else:
            exact[card_field] = rate
            # A field resolved on this provider's own bucket is also the best
            # answer the vote rung could give, so the lower card inherits it
            # rather than falling back to a stranger's price for it.
            approximate[card_field] = rate
            if exact_tier is None or tier > exact_tier:
                exact_tier = tier
            if approximate_tier is None or tier > approximate_tier:
                approximate_tier = tier

    exact_card = RateCard(
        source=SOURCE_MODELS_DEV,
        tier_label=None if exact_tier is None else exact_tier.name.lower(),
        **exact,
    )
    approximate_card = RateCard(
        source=SOURCE_CROSS_PROVIDER,
        tier_label=(
            None if approximate_tier is None else approximate_tier.name.lower()
        ),
        **approximate,
    )
    return (
        None if exact_card.is_empty else exact_card,
        None if approximate_card.is_empty else approximate_card,
    )


def rate_cards(
    provider_id: str | None,
    model_id: str | None,
    *,
    litellm_enabled: bool,
) -> tuple[RateCard, ...]:
    """Return the computed rungs for one route, tightest first.

    Empty when the route has no provider or no model -- a locally answered
    request cost nothing to run and is priced by nobody, which the log records
    as NULL rather than as zero.
    """
    if not provider_id or not model_id:
        return ()
    cards: list[RateCard] = []
    try:
        resolved = model_prices_tiered(provider_id, model_id)
    except Exception:
        resolved = {}
    exact, approximate = _cards_from_models_dev(resolved)
    if exact is not None:
        cards.append(exact)
    if litellm_enabled:
        try:
            litellm = litellm_rate_card(provider_id, model_id)
        except Exception:
            litellm = None
        if litellm is not None:
            cards.append(litellm)
    if approximate is not None:
        cards.append(approximate)
    return tuple(cards)


def backfill_pricer(*, litellm_enabled: bool) -> CostBackfillPricer | None:
    """A pricer for the one-time historical backfill, or None if it cannot run.

    ``None`` when the models.dev catalogue is not on disk yet. That case has to
    be a refusal rather than a run: the backfill records "nobody publishes a
    rate for this" permanently, and a cold cache would record it about every
    row in the log. A later start, with the catalogue fetched, registers a real
    pricer and the backfill happens then.

    ``MODE_COMPUTED_ONLY`` because there is nothing else it could be. The
    host's own figure for a request from August was never stored, so every
    answer here is computed -- which is exactly why each one comes back under a
    retroactive label rather than under the live rung's name.

    The rate cards are memoised per ``(provider, model)``: a 276,000-row
    backfill names a few thousand distinct routes, and the lookup is the whole
    cost of the walk.
    """
    catalogue = read_models_dev_cache()
    if catalogue is None or not catalogue.index:
        return None
    cards: dict[tuple[str, str], tuple[RateCard, ...]] = {}

    def price(
        provider: str | None,
        model: str | None,
        tokens_in: int | None,
        tokens_out: int | None,
        cache_read_tokens: int | None,
        cache_write_tokens: int | None,
    ) -> tuple[float | None, str | None]:
        if not provider or not model:
            # A locally answered request cost nobody anything and is priced by
            # nobody. Marking it unpriced is the truth and stops it being
            # asked about again.
            return (None, SOURCE_UNPRICED)
        key = (provider, model)
        rungs = cards.get(key)
        if rungs is None:
            rungs = rate_cards(provider, model, litellm_enabled=litellm_enabled)
            cards[key] = rungs
        result = resolve_cost(
            reported_usd=None,
            usage=TokenUsage(
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
                # Never stored on a request row, so never priced here. A
                # source that publishes a separate reasoning rate simply
                # prices those tokens as output, which is what every host
                # bills anyway.
                reasoning_tokens=None,
            ),
            cards=rungs,
            mode=MODE_COMPUTED_ONLY,
        )
        if result.cost_usd is None or result.cost_source is None:
            return (None, SOURCE_UNPRICED)
        retroactive = retroactive_source(result.cost_source)
        if retroactive is None:
            # A rung with no retroactive spelling. Refusing to label it is the
            # contract; storing it under the live rung's name would be the
            # rewrite of history the label exists to prevent.
            return (None, SOURCE_UNPRICED)
        return (result.cost_usd, retroactive)

    return price


__all__ = ["backfill_pricer", "rate_cards"]
