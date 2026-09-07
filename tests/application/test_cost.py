"""The pricing ladder: which source wins, and what "unpriced" has to survive.

The defect this feature closes is that MCC never knew what a request cost.
Prices resolved -- ``model_prices_tiered`` has returned USD/1M with a resolution
tier since 6.35.0 -- and nothing ever multiplied one by a token count.

The tests that matter here are not the arithmetic ones. Three of the five
implementations surveyed for this design destroy the unknown-versus-free
distinction, each at a different layer, so most of this file is about what must
stay ``None``.
"""

import pytest

from my_claude_code.application.cost import (
    MODE_AUTO,
    MODE_COMPUTED_ONLY,
    MODE_REPORTED_ONLY,
    SOURCE_CROSS_PROVIDER,
    SOURCE_LITELLM,
    SOURCE_MODELS_DEV,
    SOURCE_PROVIDER,
    RateCard,
    TokenUsage,
    compute_from_rates,
    resolve_cost,
)

# $3/Mtok in, $15/Mtok out -- Claude Sonnet 4.5's published rates, expressed
# the way everything past a fetcher is: USD per single token.
_SONNET = RateCard(
    source=SOURCE_MODELS_DEV,
    input_price=3e-06,
    output_price=1.5e-05,
    cache_read_price=3e-07,
    cache_write_price=3.75e-06,
    tier_label="models_dev_bucket_exact",
)


def test_a_provider_reported_cost_wins_over_every_computed_rung():
    result = resolve_cost(
        reported_usd=0.42,
        usage=TokenUsage(tokens_in=1_000_000, tokens_out=1_000_000),
        cards=[_SONNET],
    )
    assert result.cost_usd == 0.42
    assert result.cost_source == SOURCE_PROVIDER
    # Not averaged with, corrected by, or compared against the computed $18.
    assert result.cost_usd != pytest.approx(18.0)


def test_reported_only_stores_nothing_when_the_host_reports_nothing():
    result = resolve_cost(
        reported_usd=None,
        usage=TokenUsage(tokens_in=1_000, tokens_out=1_000),
        cards=[_SONNET],
        mode=MODE_REPORTED_ONLY,
    )
    assert result.cost_usd is None
    assert result.cost_source is None


def test_computed_only_ignores_the_host_and_prices_from_the_table():
    """How a provider's own billing gets audited against a published price."""
    result = resolve_cost(
        reported_usd=0.42,
        usage=TokenUsage(tokens_in=1_000_000, tokens_out=0),
        cards=[_SONNET],
        mode=MODE_COMPUTED_ONLY,
    )
    assert result.cost_usd == pytest.approx(3.0)
    assert result.cost_source == SOURCE_MODELS_DEV


def test_a_free_model_prices_at_zero_from_the_source_that_says_zero():
    free = RateCard(source=SOURCE_MODELS_DEV, input_price=0.0, output_price=0.0)
    result = resolve_cost(
        reported_usd=None,
        usage=TokenUsage(tokens_in=5_000, tokens_out=2_000),
        cards=[free],
    )
    assert result.cost_usd == 0.0
    assert result.cost_source == SOURCE_MODELS_DEV
    assert result.is_priced, "zero from a source is a price, not an absence"


def test_an_unpriced_model_stores_null_not_zero():
    result = resolve_cost(
        reported_usd=None,
        usage=TokenUsage(tokens_in=5_000, tokens_out=2_000),
        cards=[],
    )
    assert result.cost_usd is None
    assert result.cost_source is None
    assert not result.is_priced


def test_an_unmeasured_request_is_unpriced_rather_than_free():
    result = resolve_cost(reported_usd=None, usage=TokenUsage(), cards=[_SONNET])
    assert result.cost_usd is None


def test_cache_reads_and_writes_price_at_their_own_rates():
    """Never a base-rate fallback: on a warm prompt that is an order of magnitude."""
    amount = compute_from_rates(
        TokenUsage(cache_read_tokens=1_000_000, cache_write_tokens=1_000_000),
        _SONNET,
    )
    assert amount == pytest.approx(0.3 + 3.75)
    assert amount != pytest.approx(6.0), "not both at the input rate"


def test_one_request_is_never_priced_from_two_rungs():
    """A card that cannot price the whole request is skipped whole."""
    input_only = RateCard(
        source=SOURCE_MODELS_DEV, input_price=3e-06, output_price=1.5e-05
    )
    complete = RateCard(
        source=SOURCE_LITELLM,
        input_price=1e-06,
        output_price=2e-06,
        cache_read_price=1e-07,
    )
    result = resolve_cost(
        reported_usd=None,
        usage=TokenUsage(
            tokens_in=1_000_000, tokens_out=1_000_000, cache_read_tokens=1_000_000
        ),
        cards=[input_only, complete],
    )
    assert result.cost_source == SOURCE_LITELLM
    assert result.cost_usd == pytest.approx(1.0 + 2.0 + 0.1)


def test_a_card_with_no_cache_rate_still_prices_a_request_that_used_no_cache():
    """A counter of zero needs no rate: absence is only fatal where it is used."""
    input_only = RateCard(
        source=SOURCE_MODELS_DEV, input_price=3e-06, output_price=1.5e-05
    )
    result = resolve_cost(
        reported_usd=None,
        usage=TokenUsage(tokens_in=1_000_000, tokens_out=0, cache_read_tokens=0),
        cards=[input_only],
    )
    assert result.cost_usd == pytest.approx(3.0)


def test_reasoning_prices_as_output_when_no_source_names_a_rate():
    """Which is what every host that publishes no reasoning rate actually bills."""
    amount = compute_from_rates(
        TokenUsage(tokens_out=1_000_000, reasoning_tokens=400_000), _SONNET
    )
    assert amount == pytest.approx(15.0)


def test_a_published_reasoning_rate_wins_for_the_reasoning_tokens():
    card = RateCard(
        source=SOURCE_LITELLM,
        input_price=0.0,
        output_price=1.5e-05,
        reasoning_price=3e-05,
    )
    amount = compute_from_rates(
        TokenUsage(tokens_out=1_000_000, reasoning_tokens=400_000), card
    )
    # 400k at the reasoning rate, the remaining 600k at the output rate --
    # reasoning tokens are part of the output count, not an addition to it.
    assert amount == pytest.approx(400_000 * 3e-05 + 600_000 * 1.5e-05)


def test_reasoning_tokens_can_never_exceed_the_reported_output():
    card = RateCard(source=SOURCE_LITELLM, output_price=1e-05, reasoning_price=1e-04)
    amount = compute_from_rates(
        TokenUsage(tokens_out=100, reasoning_tokens=100_000), card
    )
    assert amount == pytest.approx(100 * 1e-04)


def test_the_ladder_order_is_models_dev_then_litellm_then_the_vote():
    cards = [
        RateCard(source=SOURCE_MODELS_DEV, input_price=3e-06),
        RateCard(source=SOURCE_LITELLM, input_price=2e-06),
        RateCard(source=SOURCE_CROSS_PROVIDER, input_price=1e-06),
    ]
    usage = TokenUsage(tokens_in=1_000_000)
    assert (
        resolve_cost(reported_usd=None, usage=usage, cards=cards).cost_source
        == SOURCE_MODELS_DEV
    )
    assert (
        resolve_cost(reported_usd=None, usage=usage, cards=cards[1:]).cost_source
        == SOURCE_LITELLM
    )
    assert (
        resolve_cost(reported_usd=None, usage=usage, cards=cards[2:]).cost_source
        == SOURCE_CROSS_PROVIDER
    )


def test_models_dev_per_million_and_litellm_per_token_agree_for_one_model():
    """The 1,000,000x guard.

    The two sources publish in opposite units: models.dev says ``3`` per
    million and LiteLLM says ``3e-06`` per token for the same model. Both are
    normalised at their own fetcher, so by the time a card exists the two must
    be indistinguishable -- and this is the one test that would catch a
    division moved to a call site.
    """
    models_dev_published_per_million = 3.0
    litellm_published_per_token = 3e-06
    from_models_dev = RateCard(
        source=SOURCE_MODELS_DEV,
        input_price=models_dev_published_per_million / 1_000_000,
    )
    from_litellm = RateCard(
        source=SOURCE_LITELLM, input_price=litellm_published_per_token
    )
    usage = TokenUsage(tokens_in=250_000)
    assert compute_from_rates(usage, from_models_dev) == pytest.approx(
        compute_from_rates(usage, from_litellm)
    )
    assert compute_from_rates(usage, from_models_dev) == pytest.approx(0.75)


def test_the_tier_label_travels_with_the_answer():
    result = resolve_cost(
        reported_usd=None, usage=TokenUsage(tokens_in=1_000), cards=[_SONNET]
    )
    assert result.tier_label == "models_dev_bucket_exact"


def test_a_reported_cost_carries_no_tier_label():
    result = resolve_cost(
        reported_usd=0.1, usage=TokenUsage(tokens_in=1_000), cards=[_SONNET]
    )
    assert result.tier_label is None, "rung 1 shows no est. badge"


def test_an_estimated_reasoning_count_is_flagged_only_where_it_was_priced():
    """A guessed token count feeding a price is a double estimate."""
    with_rate = RateCard(
        source=SOURCE_LITELLM, output_price=1e-05, reasoning_price=2e-05
    )
    usage = TokenUsage(tokens_out=1_000, reasoning_tokens=400, reasoning_estimated=True)
    assert resolve_cost(
        reported_usd=None, usage=usage, cards=[with_rate], mode=MODE_AUTO
    ).reasoning_estimated
    # Without a reasoning rate the estimate never influenced the number.
    without_rate = RateCard(source=SOURCE_MODELS_DEV, output_price=1e-05)
    assert not resolve_cost(
        reported_usd=None, usage=usage, cards=[without_rate]
    ).reasoning_estimated
