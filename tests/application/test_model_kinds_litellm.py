"""LiteLLM's map as two kind rungs, only while LiteLLM pricing is on (7.85.0).

Spec §4.3 rows 3b and 5b, the slot LiteLLM holds for prices: its modality pair
(fine) where nothing above the cross-provider vote stated one -- the
provider's list, OpenRouter's live list, a models.dev bucket or models.dev's
OpenRouter copy all outrank it -- and above the vote; its ``mode`` and endpoint
words (coarse) after the provider's own type and endpoint words, before the
media rail. ``litellm=None`` is the ladder before 7.85.0, exactly.
"""

import pytest

from my_claude_code.application.litellm_model_map import (
    LiteLLMModel,
    LiteLLMStatement,
)
from my_claude_code.application.model_kinds import (
    KIND_SOURCE_LITELLM,
    KIND_SOURCE_LITELLM_WORDS,
    KIND_SOURCE_MEDIA_RAIL,
    KIND_SOURCE_MODELS_DEV,
    KIND_SOURCE_OPENROUTER_LIVE,
    KIND_SOURCE_PROVIDER_LISTING,
    KIND_SOURCE_PROVIDER_WORDS,
    chat_listing_filter,
    resolve_model_kind,
)
from my_claude_code.application.model_metadata import DeclaredModalities
from my_claude_code.application.openrouter_live import LIVE_MATCH_EXACT, LiveModel
from my_claude_code.config.settings import Settings
from my_claude_code.core.model_ids import ResolutionTier

TEXT = DeclaredModalities(inputs=("text",), outputs=("text",))
HEARS = DeclaredModalities(inputs=("audio", "text"), outputs=("text",))
IMAGE_ONLY = DeclaredModalities(inputs=("text",), outputs=("image",))
CODE = DeclaredModalities(inputs=("text",), outputs=("code", "text"))
REF = "acme/vendor/model"


def _lite(
    pair: DeclaredModalities | None = None, words: tuple[str, ...] | None = None
) -> LiteLLMModel:
    return LiteLLMModel(
        modalities=None
        if pair is None
        else LiteLLMStatement(pair, "acme/vendor/model", "prefixed key"),
        words=None
        if words is None
        else LiteLLMStatement(words, "acme/vendor/model", "prefixed key"),
    )


def _modalities(answer, tier):
    return lambda _provider, _model: (answer, tier)


def _words(words, tier=ResolutionTier.PROVIDER_EXACT):
    return lambda _provider, _model: (words, tier if words is not None else None)


def _resolve(
    declared,
    tier,
    lite,
    *,
    words=None,
    live=None,
    placements=None,
):
    return resolve_model_kind(
        REF,
        modalities=_modalities(declared, tier),
        placements=placements or {},
        kind_words=_words(words),
        live=None if live is None else (lambda _p, _m: live),
        litellm=None if lite is None else (lambda _p, _m: lite),
    )


@pytest.mark.parametrize(
    ("declared", "tier"),
    [
        (TEXT, ResolutionTier.PROVIDER_EXACT),
        (TEXT, ResolutionTier.PROVIDER_TAG_STRIPPED),
        (TEXT, ResolutionTier.MODELS_DEV_BUCKET_EXACT),
        (TEXT, ResolutionTier.OPENROUTER_EXACT),
        (TEXT, ResolutionTier.OPENROUTER_TAG_STRIPPED),
    ],
)
def test_the_fine_rung_never_outranks_anything_above_the_vote(declared, tier) -> None:
    kind = _resolve(declared, tier, _lite(IMAGE_ONLY))
    assert kind.kinds == frozenset({"chat"})
    assert kind.source in {KIND_SOURCE_PROVIDER_LISTING, KIND_SOURCE_MODELS_DEV}


def test_the_fine_rung_answers_above_the_vote() -> None:
    kind = _resolve(TEXT, ResolutionTier.CROSS_PROVIDER_EXACT, _lite(HEARS))
    assert kind.kinds == frozenset({"chat", "asr"})
    assert kind.source == KIND_SOURCE_LITELLM
    assert kind.match == "LiteLLM, prefixed key (acme/vendor/model)"


def test_the_fine_rung_fills_a_gap() -> None:
    kind = _resolve(None, None, _lite(IMAGE_ONLY), words=("chat",))
    assert kind.kinds == frozenset({"image"})
    assert kind.source == KIND_SOURCE_LITELLM


def test_openrouter_live_answers_before_it() -> None:
    live = LiveModel(slugs=("vendor/model",), match=LIVE_MATCH_EXACT, modalities=TEXT)
    kind = _resolve(None, None, _lite(IMAGE_ONLY), live=live)
    assert kind.source == KIND_SOURCE_OPENROUTER_LIVE
    assert kind.kinds == frozenset({"chat"})


def test_a_word_no_kind_rule_reads_states_no_kind() -> None:
    """LiteLLM writes ``code`` as an output on 8 entries."""

    kind = _resolve(None, None, _lite(CODE))
    assert kind.kinds is None


def test_the_coarse_rung_comes_after_the_provider_s_own_words() -> None:
    kind = _resolve(None, None, _lite(words=("audio_transcription",)), words=("chat",))
    assert kind.source == KIND_SOURCE_PROVIDER_WORDS
    assert kind.kinds == frozenset({"chat"})
    gap = _resolve(None, None, _lite(words=("audio_transcription",)))
    assert gap.source == KIND_SOURCE_LITELLM_WORDS
    assert gap.kinds == frozenset({"asr"})
    assert gap.match == "LiteLLM, prefixed key (acme/vendor/model)"


def test_the_coarse_rung_comes_before_the_media_rail() -> None:
    placed = {REF: frozenset({"image"})}
    kind = _resolve(
        None, None, _lite(words=("chat", "/v1/chat/completions")), placements=placed
    )
    assert kind.source == KIND_SOURCE_LITELLM_WORDS
    assert kind.kinds == frozenset({"chat"})
    silent = _resolve(
        None, None, _lite(words=("completion", "/v1/batch")), placements=placed
    )
    assert silent.source == KIND_SOURCE_MEDIA_RAIL  # words no rule reads state nothing


def test_the_coarse_rung_never_answers_where_a_pair_was_stated() -> None:
    kind = _resolve(
        HEARS, ResolutionTier.CROSS_PROVIDER_EXACT, _lite(words=("image_generation",))
    )
    assert kind.source == KIND_SOURCE_MODELS_DEV
    assert kind.kinds == frozenset({"chat", "asr"})


@pytest.mark.parametrize(
    ("declared", "tier", "words"),
    [
        (None, None, None),
        (None, None, ("chat",)),
        (TEXT, ResolutionTier.PROVIDER_EXACT, None),
        (HEARS, ResolutionTier.CROSS_PROVIDER_BARE_UNTAGGED, None),
        (IMAGE_ONLY, ResolutionTier.MODELS_DEV_BUCKET_EXACT, ("chat",)),
    ],
)
def test_without_the_map_the_ladder_is_the_one_before_it(declared, tier, words) -> None:
    before = resolve_model_kind(
        REF,
        modalities=_modalities(declared, tier),
        placements={},
        kind_words=_words(words),
    )
    assert _resolve(declared, tier, None, words=words) == before
    # A map that says nothing about this model is no rung either.
    assert _resolve(declared, tier, None, words=words) == _resolve(
        declared, tier, LiteLLMModel(), words=words
    )


def test_the_listing_filter_passes_the_map_through() -> None:
    settings = Settings.model_validate({})
    listable = chat_listing_filter(
        settings,
        _modalities(None, None),
        kind_words=_words(None),
        litellm=lambda _p, _m: _lite(IMAGE_ONLY),
    )
    assert listable(REF) is False
    unchanged = chat_listing_filter(
        settings, _modalities(None, None), kind_words=_words(None)
    )
    assert unchanged(REF) is True
