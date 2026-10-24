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

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from my_claude_code.application.cost import (
    MODE_AUTO,
    MODE_COMPUTED_ONLY,
    MODE_REPORTED_ONLY,
    SOURCE_CROSS_PROVIDER,
    SOURCE_LITELLM,
    SOURCE_MODELS_DEV,
    SOURCE_UNPRICED,
    SOURCE_UNPRICED_1H_WRITE,
    OneHourWriteRate,
    RateCard,
    TokenUsage,
    resolve_cost,
    retroactive_source,
)
from my_claude_code.application.media_cost import (
    MediaRateCard,
    MediaUsage,
    resolve_media_cost,
)
from my_claude_code.core.model_ids import ResolutionTier
from my_claude_code.core.request_log import CostBackfillPricer
from my_claude_code.providers.runtime.litellm_prices import (
    litellm_media_card,
    litellm_rate_card,
)
from my_claude_code.providers.runtime.models_dev import (
    model_audio_prices_tiered,
    model_prices_tiered,
    read_models_dev_cache,
)
from my_claude_code.providers.runtime.openrouter_catalogue import (
    listed_write_1h_per_million,
    openrouter_live_write_1h_price,
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

    # ``cache_write_1h`` named, as None: models.dev states no 1-hour write
    # rate (7.111.0), and naming it keeps the five rates below from ever
    # being read as one.
    exact_card = RateCard(
        source=SOURCE_MODELS_DEV,
        tier_label=None if exact_tier is None else exact_tier.name.lower(),
        cache_write_1h=None,
        **exact,
    )
    approximate_card = RateCard(
        source=SOURCE_CROSS_PROVIDER,
        tier_label=(
            None if approximate_tier is None else approximate_tier.name.lower()
        ),
        cache_write_1h=None,
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


# -- 7.111.0: 1-hour prompt-cache writes, priced by the lifetime sent ---------
#
# Anthropic bills a write that keeps the cache for an hour at a different rate
# from one that keeps it for five minutes, and says per request how many of
# each it wrote (``usage.cache_creation``). models.dev states one write rate --
# the 5-minute one -- so until 7.111.0 every 1-hour write was priced at it.
# Three sources state the 1-hour rate: LiteLLM (its own rung, when the operator
# turned LiteLLM pricing on), the provider's own list (OpenRouter-dialect
# lists: Nous Portal's, OpenRouter's) and OpenRouter's live list (the 7.84.0
# rung). Nothing below is read for a request that wrote no 1-hour entries:
# :func:`price_request` hands those exactly today's :func:`rate_cards`.

#: The ``OneHourWriteRate.source`` words besides ``litellm``. Stored in the
#: row's ``params.cost_write_1h`` and read by the dashboard.
WRITE_1H_SOURCE_PROVIDER_LIST = "provider_list"
WRITE_1H_SOURCE_OPENROUTER_LIVE = "openrouter_live"

#: ``(provider, model) -> the provider's own list row`` for that model, or
#: ``None``. Registered by the composition root (the rows live in the provider
#: runtime, which a request capture cannot reach), exactly as the backfill
#: pricer is handed to the request log.
type PublishedRowLookup = Callable[[str, str], Mapping[str, Any] | None]

_published_row_lock = threading.Lock()
_published_row_lookup: PublishedRowLookup | None = None


def set_published_row_lookup(lookup: PublishedRowLookup | None) -> None:
    """Register (or clear) where the provider's own list rows are read from."""

    global _published_row_lookup
    with _published_row_lock:
        _published_row_lookup = lookup


def _per_token(per_million: float) -> float:
    return per_million / _PER_MILLION


def provider_list_write_1h_rate(
    provider_id: str, model_id: str
) -> OneHourWriteRate | None:
    """The provider's own list's 1-hour write rate for this model, or ``None``."""

    with _published_row_lock:
        lookup = _published_row_lookup
    if lookup is None:
        return None
    try:
        row = lookup(provider_id, model_id)
    except Exception:
        return None
    if not isinstance(row, Mapping):
        return None
    per_million = listed_write_1h_per_million(row)
    if per_million is None:
        return None
    return OneHourWriteRate(
        price=_per_token(per_million),
        source=WRITE_1H_SOURCE_PROVIDER_LIST,
        label=f"{provider_id}'s own model list",
    )


def openrouter_live_write_1h_rate(
    provider_id: str, model_id: str
) -> OneHourWriteRate | None:
    """OpenRouter's live list's 1-hour write rate for this route, or ``None``.

    When the asking provider IS OpenRouter the list is its own, and the rate is
    labelled as the provider's own list's.
    """

    try:
        found = openrouter_live_write_1h_price(provider_id, model_id)
    except Exception:
        return None
    if found is None:
        return None
    slugs = ", ".join(found.slugs)
    if found.own_list:
        return OneHourWriteRate(
            price=_per_token(found.per_million),
            source=WRITE_1H_SOURCE_PROVIDER_LIST,
            label=f"OpenRouter's own model list ({slugs})",
        )
    return OneHourWriteRate(
        price=_per_token(found.per_million),
        source=WRITE_1H_SOURCE_OPENROUTER_LIVE,
        label=f"OpenRouter live, {found.match} ({slugs})",
    )


def write_split_rate_cards(
    provider_id: str | None,
    model_id: str | None,
    *,
    litellm_enabled: bool,
    openrouter_live_enabled: bool,
) -> tuple[RateCard, ...]:
    """The rungs for a request that wrote 1-hour cache entries, in ladder order.

    Today's rungs, each carrying the 1-hour rate its own sources state, with
    nothing moved: models.dev (whose own catalogue states no 1-hour rate, so it
    carries the provider's own list's -- the provider-specific number order
    7.84.0 fixed: the provider first), LiteLLM (its own 1-hour rate only, so a
    LiteLLM price is LiteLLM's throughout), the cross-provider vote (the
    provider's list's, as it inherits the provider's own answers). Then, last
    and only as a gap fill (7.84.0: OpenRouter's numbers fill gaps, never
    override), models.dev and the vote again carrying OpenRouter live's rate.
    A card with no 1-hour rate cannot price such a request (``covers``).
    """

    base = rate_cards(provider_id, model_id, litellm_enabled=litellm_enabled)
    if not provider_id or not model_id:
        return base
    listed = provider_list_write_1h_rate(provider_id, model_id)
    live = (
        openrouter_live_write_1h_rate(provider_id, model_id)
        if openrouter_live_enabled
        else None
    )
    first: list[RateCard] = []
    gap_fill: list[RateCard] = []
    for card in base:
        if card.source == SOURCE_LITELLM:
            first.append(card)
            continue
        if listed is not None:
            first.append(replace(card, cache_write_1h=listed))
            continue
        first.append(card)
        if live is not None:
            gap_fill.append(replace(card, cache_write_1h=live))
    return (*first, *gap_fill)


@dataclass(frozen=True, slots=True)
class RequestPrice:
    """What :func:`price_request` stores: the amount, its source, and the 1-hour rate used."""

    cost_usd: float | None
    cost_source: str | None
    #: The 1-hour write rate the price used; ``None`` unless the request
    #: reported 1-hour writes and a card priced them.
    cache_write_1h: OneHourWriteRate | None = None


def price_request(
    provider_id: str | None,
    model_id: str | None,
    usage: TokenUsage,
    *,
    reported_usd: float | None,
    mode: str = MODE_AUTO,
    litellm_enabled: bool,
    openrouter_live_enabled: bool = False,
) -> RequestPrice:
    """Walk the ladder for one request row (the request capture's one entry point).

    A request that reported no 1-hour writes -- the split absent, or 0 -- is
    priced by exactly the call every row took before 7.111.0. One that did is
    priced from :func:`write_split_rate_cards`; when no card states a 1-hour
    rate but one would have priced everything else, the row is stored as
    ``unpriced_1h_write`` with no amount -- never at the 5-minute rate, never 0.
    A reported cost still wins first, untouched.
    """

    if not usage.one_hour_write_tokens:
        result = resolve_cost(
            reported_usd=reported_usd,
            usage=usage,
            cards=rate_cards(provider_id, model_id, litellm_enabled=litellm_enabled),
            mode=mode,
        )
        return RequestPrice(result.cost_usd, result.cost_source)
    cards = write_split_rate_cards(
        provider_id,
        model_id,
        litellm_enabled=litellm_enabled,
        openrouter_live_enabled=openrouter_live_enabled,
    )
    result = resolve_cost(
        reported_usd=reported_usd, usage=usage, cards=cards, mode=mode
    )
    if result.cost_source is not None or mode == MODE_REPORTED_ONLY:
        return RequestPrice(result.cost_usd, result.cost_source, result.cache_write_1h)
    without_split = resolve_cost(
        reported_usd=None,
        usage=replace(usage, cache_write_1h_tokens=None),
        cards=cards,
        mode=MODE_COMPUTED_ONLY,
    )
    if without_split.is_priced:
        return RequestPrice(None, SOURCE_UNPRICED_1H_WRITE)
    return RequestPrice(None, None)


def write_1h_price_statements(
    provider_id: str,
    model_id: str,
    *,
    litellm_enabled: bool,
    openrouter_live_enabled: bool,
) -> list[dict[str, Any]]:
    """Every 1-hour write rate a source states for one route, in the order pricing reads them.

    For the Models page: the provider's own list, LiteLLM (when on),
    OpenRouter live (when on). USD per million tokens, as the page shows every
    price. Empty when no source states one.
    """

    statements: list[OneHourWriteRate] = []
    listed = provider_list_write_1h_rate(provider_id, model_id)
    if listed is not None:
        statements.append(listed)
    if litellm_enabled:
        try:
            card = litellm_rate_card(provider_id, model_id)
        except Exception:
            card = None
        if card is not None and card.cache_write_1h is not None:
            statements.append(card.cache_write_1h)
    if openrouter_live_enabled:
        live = openrouter_live_write_1h_rate(provider_id, model_id)
        if live is not None and not any(
            known.source == live.source and known.price == live.price
            for known in statements
        ):
            statements.append(live)
    return [
        {
            "usd_per_million": rate.price * _PER_MILLION,
            "source": rate.source,
            "label": rate.label,
        }
        for rate in statements
    ]


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


def _audio_rates(
    resolved: Mapping[str, tuple[float | None, ResolutionTier | None]],
) -> tuple[dict[str, float], dict[str, float]]:
    """Split the two audio-token rates the way the token rates are split.

    A rate resolved on this provider's own bucket (or the curated reference)
    belongs to the ``models_dev`` rung and is inherited by the vote rung below
    it; a voted rate belongs to the vote rung only. Per million -> per token,
    the same division the token rates get.
    """
    exact: dict[str, float] = {}
    approximate: dict[str, float] = {}
    for field in ("input_audio_price", "output_audio_price"):
        value, tier = resolved.get(field, (None, None))
        if value is None or tier is None:
            continue
        rate = value / _PER_MILLION
        approximate[field] = rate
        if not tier.is_approximate:
            exact[field] = rate
    return exact, approximate


def media_rate_cards(
    provider_id: str | None,
    model_id: str | None,
    *,
    litellm_enabled: bool,
) -> tuple[MediaRateCard, ...]:
    """The computed rungs for one media route, in chat's order (7.69.0).

    models.dev's authoritative tiers (token rates, plus its audio-token rates),
    then LiteLLM only when the operator turned it on (its unit rates and token
    rates), then models.dev's cross-provider vote. Empty when the route has no
    provider or no model.
    """
    if not provider_id or not model_id:
        return ()
    try:
        prices = model_prices_tiered(provider_id, model_id)
        audio = model_audio_prices_tiered(provider_id, model_id)
    except Exception:
        prices, audio = {}, {}
    exact, approximate = _cards_from_models_dev(prices)
    audio_exact, audio_approximate = _audio_rates(audio)
    cards: list[MediaRateCard] = []
    if exact is not None:
        cards.append(
            MediaRateCard(
                source=SOURCE_MODELS_DEV,
                tokens=exact,
                input_audio_price=audio_exact.get("input_audio_price"),
                output_audio_price=audio_exact.get("output_audio_price"),
                tier_label=exact.tier_label,
            )
        )
    if litellm_enabled:
        try:
            litellm = litellm_media_card(provider_id, model_id)
        except Exception:
            litellm = None
        if litellm is not None:
            cards.append(litellm)
    if approximate is not None:
        cards.append(
            MediaRateCard(
                source=SOURCE_CROSS_PROVIDER,
                tokens=approximate,
                input_audio_price=audio_approximate.get("input_audio_price"),
                output_audio_price=audio_approximate.get("output_audio_price"),
                tier_label=approximate.tier_label,
            )
        )
    return tuple(cards)


def price_media(
    provider_id: str | None,
    model_id: str | None,
    usage: MediaUsage,
    *,
    reported_usd: float | None,
    mode: str,
    litellm_enabled: bool,
) -> tuple[float | None, str | None]:
    """``(cost_usd, cost_source)`` for one media row; ``(None, 'unpriced')`` if nothing priced it.

    Synchronous: it reads the cached catalogues, so it runs off the loop -- on
    the request log's writer thread (``RequestRecord.pricer``) or through
    ``asyncio.to_thread``.
    """
    result = resolve_media_cost(
        reported_usd=reported_usd,
        usage=usage,
        cards=media_rate_cards(provider_id, model_id, litellm_enabled=litellm_enabled),
        mode=mode,
    )
    return result.cost_usd, result.cost_source


__all__ = [
    "WRITE_1H_SOURCE_OPENROUTER_LIVE",
    "WRITE_1H_SOURCE_PROVIDER_LIST",
    "RequestPrice",
    "backfill_pricer",
    "media_rate_cards",
    "openrouter_live_write_1h_rate",
    "price_media",
    "price_request",
    "provider_list_write_1h_rate",
    "rate_cards",
    "set_published_row_lookup",
    "write_1h_price_statements",
    "write_split_rate_cards",
]
