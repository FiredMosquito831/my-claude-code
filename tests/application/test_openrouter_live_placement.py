"""Where OpenRouter's live list sits on the ladder, per field class (7.84.0).

The user's binding decisions (2026-10-08 21:00 and 21:33): intrinsic fields --
what a model accepts and produces (so its kind), image input, reasoning, tool
calls -- go provider, then OpenRouter live, then models.dev for a provider with
no models.dev bucket, and provider, then bucket, then OpenRouter live (gaps
only) for one with a bucket; numbers go provider, then models.dev, then
OpenRouter live last, gaps only. A bucketed provider's models.dev answer is
always tier 3-4, so the rule is stated on tiers.
"""

import pytest

from my_claude_code.application.model_kinds import (
    KIND_SOURCE_MODELS_DEV,
    KIND_SOURCE_OPENROUTER_LIVE,
    KIND_SOURCE_PROVIDER_LISTING,
    ModelKind,
    chat_listing_filter,
    live_kind_alternative,
    resolve_model_kind,
)
from my_claude_code.application.model_metadata import DeclaredModalities
from my_claude_code.application.openrouter_live import (
    LIVE_MATCH_EXACT,
    LiveModel,
    live_fills_gap,
    live_wins_intrinsic,
)
from my_claude_code.config.settings import Settings
from my_claude_code.core.model_ids import ResolutionTier

TEXT = DeclaredModalities(inputs=("text",), outputs=("text",))
HEARS = DeclaredModalities(inputs=("audio", "text"), outputs=("text",))
IMAGE_ONLY = DeclaredModalities(inputs=("text",), outputs=("image",))
DECISIONS = DeclaredModalities(inputs=("text",), outputs=("decisions",))


def _live(modalities: DeclaredModalities | None, *, own: bool = False) -> LiveModel:
    return LiveModel(
        slugs=("vendor/model",),
        match=LIVE_MATCH_EXACT,
        own_list=own,
        modalities=modalities,
    )


@pytest.mark.parametrize(
    ("existing", "tier", "live", "wins"),
    [
        (None, None, True, True),  # a gap, whoever's provider it is
        (False, ResolutionTier.OPENROUTER_EXACT, True, True),  # bucket-less, copy
        (False, ResolutionTier.CROSS_PROVIDER_BARE_UNTAGGED, True, True),  # vote
        (False, ResolutionTier.MODELS_DEV_BUCKET_EXACT, True, False),  # bucket
        (False, ResolutionTier.MODELS_DEV_BUCKET_TAG_STRIPPED, True, False),
        (False, ResolutionTier.PROVIDER_EXACT, True, False),  # the provider's own
        (False, None, True, False),  # a cached record carries no tier: provider
        (True, ResolutionTier.CROSS_PROVIDER_EXACT, None, False),  # live silent
        (None, None, None, False),
    ],
)
def test_an_intrinsic_field_takes_the_live_value_only_above_tier_five(
    existing: object, tier: ResolutionTier | None, live: object, wins: bool
) -> None:
    assert live_wins_intrinsic(existing, tier, live) is wins
    # The page carries tiers as ints; the rule reads both.
    if tier is not None:
        assert live_wins_intrinsic(existing, int(tier), live) is wins


def test_a_number_takes_the_live_value_only_in_a_gap() -> None:
    assert live_fills_gap(None, 128000)
    assert not live_fills_gap(64000, 128000)
    assert not live_fills_gap(0.0, 1.5)
    assert not live_fills_gap(None, None)


def _modalities(answer, tier):
    return lambda _provider, _model: (answer, tier)


def _kind(modalities, live, kind_words=None) -> ModelKind:
    return resolve_model_kind(
        "acme/vendor/model",
        modalities=modalities,
        placements={},
        kind_words=kind_words,
        live=lambda _provider, _model: live,
    )


def test_the_provider_s_own_pair_outranks_the_live_list() -> None:
    kind = _kind(_modalities(TEXT, ResolutionTier.PROVIDER_EXACT), _live(HEARS))
    assert kind.source == KIND_SOURCE_PROVIDER_LISTING
    assert kind.kinds == frozenset({"chat"})


def test_a_bucket_outranks_the_live_list() -> None:
    kind = _kind(
        _modalities(HEARS, ResolutionTier.MODELS_DEV_BUCKET_EXACT), _live(TEXT)
    )
    assert kind.source == KIND_SOURCE_MODELS_DEV
    assert kind.kinds == frozenset({"chat", "asr"})
    alternative = live_kind_alternative(kind, _live(TEXT))
    assert alternative is not None
    assert alternative.kinds == frozenset({"chat"})
    assert alternative.source == KIND_SOURCE_OPENROUTER_LIVE


@pytest.mark.parametrize(
    "tier",
    [ResolutionTier.OPENROUTER_EXACT, ResolutionTier.CROSS_PROVIDER_EXACT],
)
def test_without_a_bucket_the_live_list_goes_before_models_dev(
    tier: ResolutionTier,
) -> None:
    kind = _kind(_modalities(TEXT, tier), _live(TEXT))
    assert kind.source == KIND_SOURCE_OPENROUTER_LIVE
    assert kind.kinds == frozenset({"chat"})
    assert kind.match == "OpenRouter live, exact id"
    assert kind.tier is None
    assert live_kind_alternative(kind, _live(TEXT)) is None


def test_the_live_list_fills_a_kind_nobody_stated_before_the_coarse_words() -> None:
    kind = _kind(
        _modalities(None, None),
        _live(IMAGE_ONLY),
        kind_words=lambda _p, _m: (("chat",), ResolutionTier.PROVIDER_EXACT),
    )
    assert kind.source == KIND_SOURCE_OPENROUTER_LIVE
    assert kind.kinds == frozenset({"image"})


def test_openrouter_s_own_list_never_feeds_its_own_kind() -> None:
    kind = _kind(_modalities(None, None), _live(TEXT, own=True))
    assert kind.kinds is None


def test_a_word_no_kind_rule_reads_states_no_kind() -> None:
    """``text -> decisions`` must not take a model out of the chat lists."""

    kind = _kind(_modalities(None, None), _live(DECISIONS))
    assert kind.kinds is None
    listable = chat_listing_filter(
        Settings.model_validate({}),
        _modalities(None, None),
        live=lambda _p, _m: _live(DECISIONS),
    )
    assert listable("acme/vendor/model")


def test_without_the_rung_the_kind_is_what_it_was() -> None:
    for modalities in (
        _modalities(TEXT, ResolutionTier.CROSS_PROVIDER_EXACT),
        _modalities(None, None),
    ):
        before = resolve_model_kind(
            "acme/vendor/model", modalities=modalities, placements={}
        )
        assert _kind(modalities, None) == before


def test_the_chat_lists_follow_a_live_kind() -> None:
    listable = chat_listing_filter(
        Settings.model_validate({}),
        _modalities(None, None),
        live=lambda _p, _m: _live(IMAGE_ONLY),
    )
    assert not listable("acme/vendor/model")
