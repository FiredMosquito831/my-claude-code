"""Agent catalogues read OpenRouter's live list at their own seam (7.84.0).

The catalogue record takes the live list's numbers only where every rung above
said nothing, and its intrinsic flags above models.dev's tiers 5-10 (which only
a provider with no models.dev bucket reaches) and in gaps. The record's output
limit is the catalogue's own field: routing's clamp reads the runtime's
``model_output_limit``, which never sees the live list (user decision Q5).
"""

from typing import Any

from my_claude_code.application.catalogue_model import build_catalogue_models
from my_claude_code.application.model_metadata import (
    DeclaredModalities,
    ModelReasoningCapability,
    ProviderModelInfo,
)
from my_claude_code.application.openrouter_live import (
    LIVE_MATCH_EXACT,
    LiveCatalogue,
    LiveModel,
)
from my_claude_code.config.settings import Settings
from my_claude_code.core.model_ids import ResolutionTier
from tests.application.test_catalogue_model import FakeRuntime

REF = "nous_portal/acme/model"
GATEWAY_ID = "anthropic/nous_portal/acme/model"


def _settings() -> Settings:
    return Settings().model_copy(update={"model": "nvidia_nim/configured"})


def _answer(**fields: Any) -> LiveModel:
    base: dict[str, Any] = {
        "slugs": ("acme/model",),
        "match": LIVE_MATCH_EXACT,
        "modalities": DeclaredModalities(inputs=("text",), outputs=("text",)),
        "supports_vision": True,
        "can_reason": True,
        "supports_tool_calls": True,
        "context_length": 131072,
        "max_output_tokens": 65536,
        "input_price": 0.5,
        "output_price": 1.5,
        "cache_read_price": 0.05,
        "cache_write_price": 0.6,
    }
    base.update(fields)
    return LiveModel(**base)


def _live(answer: LiveModel | None) -> LiveCatalogue:
    return LiveCatalogue(
        mark="1:1", fetched_at=None, rows=1, lookup=lambda _p, _m: answer
    )


def _entry(runtime: FakeRuntime):
    return next(
        model
        for model in build_catalogue_models(runtime.current_settings(), runtime)
        if model.gateway_id == GATEWAY_ID
    )


def test_numbers_fill_gaps_and_never_reach_routing() -> None:
    runtime = FakeRuntime(
        settings=_settings(),
        cached_infos=(ProviderModelInfo(REF),),
        context_lengths={REF: 200000},
        prices={REF: {"input_price": 9.0}},
        live=_live(_answer()),
    )
    entry = _entry(runtime)
    assert entry.context_length == 200000  # stated above: kept
    assert entry.input_price == 9.0  # stated above: kept
    assert entry.max_output_tokens == 65536  # a gap: filled
    assert entry.output_price == 1.5
    assert entry.cache_read_price == 0.05
    assert entry.cache_write_price == 0.6
    # The routing lookup is the runtime's, and it is untouched.
    assert runtime.model_output_limit("nous_portal", "acme/model") is None


def test_intrinsic_flags_go_above_tier_five_and_below_a_bucket() -> None:
    copy_tier = ResolutionTier.OPENROUTER_EXACT
    bucket_tier = ResolutionTier.MODELS_DEV_BUCKET_EXACT
    runtime = FakeRuntime(
        settings=_settings(),
        cached_infos=(ProviderModelInfo(REF),),
        vision={REF: False},
        tool_calls={REF: False},
        reasoning={REF: ModelReasoningCapability(can_reason=False)},
        tiers={f"vision:{REF}": copy_tier, f"tools:{REF}": copy_tier},
        live=_live(_answer()),
    )
    entry = _entry(runtime)
    assert entry.supports_vision is True
    assert entry.supports_tool_calls is True
    # can_reason came with no rung in this fake: a provider's own, kept.
    assert entry.reasoning == ModelReasoningCapability(can_reason=False)

    bucketed = FakeRuntime(
        settings=_settings(),
        cached_infos=(ProviderModelInfo(REF),),
        vision={REF: False},
        tool_calls={REF: False},
        reasoning={REF: ModelReasoningCapability(can_reason=False)},
        tiers={
            f"vision:{REF}": bucket_tier,
            f"tools:{REF}": bucket_tier,
            f"reason:{REF}": bucket_tier,
        },
        live=_live(_answer()),
    )
    entry = _entry(bucketed)
    assert entry.supports_vision is False
    assert entry.supports_tool_calls is False
    assert entry.reasoning == ModelReasoningCapability(can_reason=False)


def test_a_reasoning_gap_is_filled_and_the_rest_of_the_record_kept() -> None:
    runtime = FakeRuntime(
        settings=_settings(),
        cached_infos=(ProviderModelInfo(REF),),
        reasoning={REF: ModelReasoningCapability(supports_effort_control=True)},
        live=_live(_answer()),
    )
    entry = _entry(runtime)
    assert entry.reasoning == ModelReasoningCapability(
        can_reason=True, supports_effort_control=True
    )


def test_the_provider_s_own_parameter_list_is_never_replaced() -> None:
    record = ProviderModelInfo(
        REF, supported_parameters=frozenset({"max_tokens"}), supports_vision=False
    )
    runtime = FakeRuntime(
        settings=_settings(), cached_infos=(record,), live=_live(_answer())
    )
    entry = _entry(runtime)
    assert entry.supports_tool_calls is False
    assert entry.supports_vision is False


def test_openrouter_s_own_models_and_no_match_change_nothing() -> None:
    plain = FakeRuntime(settings=_settings(), cached_infos=(ProviderModelInfo(REF),))
    before = _entry(plain)
    for answer in (None, _answer(own_list=True)):
        runtime = FakeRuntime(
            settings=_settings(),
            cached_infos=(ProviderModelInfo(REF),),
            live=_live(answer),
        )
        assert _entry(runtime) == before
