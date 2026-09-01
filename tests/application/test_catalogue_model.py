"""The neutral catalogue record carries the ladder's answers, unknowns included."""

from my_claude_code.api.model_catalog import build_models_list_response
from my_claude_code.application.catalogue_model import (
    build_catalogue_models,
    derive_supports_tool_calls,
)
from my_claude_code.application.model_metadata import (
    ModelReasoningCapability,
    ProviderModelInfo,
)
from my_claude_code.application.ports import RequestRuntimeLease, RequestRuntimePort
from my_claude_code.config.settings import Settings
from my_claude_code.core.reasoning import ReasoningEffort


class FakeRuntime(RequestRuntimePort):
    """A runtime whose ladder answers are stated per (provider, model)."""

    def __init__(
        self,
        *,
        settings: Settings,
        cached_infos: tuple[ProviderModelInfo, ...] = (),
        context_lengths: dict[str, int] | None = None,
        output_limits: dict[str, int] | None = None,
        vision: dict[str, bool] | None = None,
        thinking: dict[str, bool] | None = None,
        reasoning: dict[str, ModelReasoningCapability] | None = None,
    ) -> None:
        self._settings = settings
        self._cached_infos = cached_infos
        self._context_lengths = context_lengths or {}
        self._output_limits = output_limits or {}
        self._vision = vision or {}
        self._thinking = thinking or {}
        self._reasoning = reasoning or {}

    async def acquire(self) -> RequestRuntimeLease:
        raise AssertionError("Catalogue building must not acquire a provider lease.")

    def current_settings(self) -> Settings:
        return self._settings

    def cached_model_supports_thinking(
        self, provider_id: str, model_id: str
    ) -> bool | None:
        return self._thinking.get(f"{provider_id}/{model_id}")

    def cached_model_supports_vision(
        self, provider_id: str, model_id: str
    ) -> bool | None:
        return self._vision.get(f"{provider_id}/{model_id}")

    def model_reasoning_capability(
        self, provider_id: str, model_id: str
    ) -> ModelReasoningCapability | None:
        return self._reasoning.get(f"{provider_id}/{model_id}")

    def model_output_limit(self, provider_id: str, model_id: str) -> int | None:
        return self._output_limits.get(f"{provider_id}/{model_id}")

    def model_context_length(self, provider_id: str, model_id: str) -> int | None:
        return self._context_lengths.get(f"{provider_id}/{model_id}")

    def cached_prefixed_model_infos(self) -> tuple[ProviderModelInfo, ...]:
        return self._cached_infos


def _settings(**update: object) -> Settings:
    return Settings().model_copy(update={"model": "nvidia_nim/configured", **update})


def test_catalogue_model_carries_ladder_context_length_and_max_output() -> None:
    runtime = FakeRuntime(
        settings=_settings(),
        cached_infos=(ProviderModelInfo("open_router/big"),),
        context_lengths={"open_router/big": 262144},
        output_limits={"open_router/big": 32768},
    )

    models = {
        model.gateway_id: model
        for model in build_catalogue_models(runtime.current_settings(), runtime)
    }

    entry = models["anthropic/open_router/big"]
    assert entry.context_length == 262144
    assert entry.max_output_tokens == 32768
    assert entry.provider_id == "open_router"
    assert entry.provider_model_id == "big"


def test_unknown_capability_stays_none_and_is_not_zero() -> None:
    runtime = FakeRuntime(
        settings=_settings(),
        cached_infos=(ProviderModelInfo("open_router/quiet"),),
    )

    entry = next(
        model
        for model in build_catalogue_models(runtime.current_settings(), runtime)
        if model.gateway_id == "anthropic/open_router/quiet"
    )

    assert entry.context_length is None
    assert entry.max_output_tokens is None
    assert entry.supports_vision is None
    assert entry.supports_tool_calls is None
    assert entry.reasoning is None
    assert entry.input_price is None


def test_supports_tool_calls_is_none_when_supported_parameters_unpublished() -> None:
    assert derive_supports_tool_calls(None) is None
    assert derive_supports_tool_calls(frozenset()) is False
    assert derive_supports_tool_calls(frozenset({"temperature"})) is False
    assert derive_supports_tool_calls(frozenset({"tools"})) is True
    assert derive_supports_tool_calls(frozenset({"tool_choice"})) is True


def test_prices_and_parameters_come_from_the_cached_provider_row() -> None:
    info = ProviderModelInfo(
        "open_router/priced",
        input_price=0.000003,
        output_price=0.000015,
        supported_parameters=frozenset({"tools", "temperature"}),
        default_parameters=(("top_p", 0.95),),
    )
    runtime = FakeRuntime(settings=_settings(), cached_infos=(info,))

    entry = next(
        model
        for model in build_catalogue_models(runtime.current_settings(), runtime)
        if model.gateway_id == "anthropic/open_router/priced"
    )

    assert entry.input_price == 0.000003
    assert entry.output_price == 0.000015
    assert entry.supports_tool_calls is True
    assert entry.default_parameters == (("top_p", 0.95),)


def test_no_thinking_variant_advertises_no_reasoning_capability() -> None:
    capability = ModelReasoningCapability(
        can_reason=True,
        supports_effort_control=True,
        supported_efforts=frozenset({ReasoningEffort.LOW, ReasoningEffort.HIGH}),
    )
    runtime = FakeRuntime(
        settings=_settings(),
        cached_infos=(ProviderModelInfo("open_router/thinker"),),
        reasoning={"open_router/thinker": capability},
    )

    models = {
        model.gateway_id: model
        for model in build_catalogue_models(runtime.current_settings(), runtime)
    }

    assert models["anthropic/open_router/thinker"].reasoning == capability
    no_thinking = models["claude-3-freecc-no-thinking/open_router/thinker"]
    assert no_thinking.reasoning is None
    assert no_thinking.force_no_thinking is True


def test_a_model_known_not_to_think_gets_only_the_no_thinking_variant() -> None:
    runtime = FakeRuntime(
        settings=_settings(model="open_router/plain"),
        cached_infos=(ProviderModelInfo("open_router/plain"),),
        thinking={"open_router/plain": False},
    )

    ids = [
        model.gateway_id
        for model in build_catalogue_models(runtime.current_settings(), runtime)
    ]

    assert ids == ["claude-3-freecc-no-thinking/open_router/plain"]


def test_catalogue_models_and_v1_models_agree_on_visible_refs() -> None:
    settings = _settings(model_visibility_deny="open_router/hidden*")
    runtime = FakeRuntime(
        settings=settings,
        cached_infos=(
            ProviderModelInfo("open_router/shown"),
            ProviderModelInfo("open_router/hidden-one"),
        ),
    )

    catalogue_ids = {
        model.gateway_id for model in build_catalogue_models(settings, runtime)
    }
    listing = build_models_list_response(settings, runtime)
    # The eight Claude protocol aliases are names, not routable refs, and are
    # deliberately exempt from visibility; everything else must match exactly.
    listing_ids = {entry.id for entry in listing.data if "/" in entry.id}

    assert catalogue_ids == listing_ids
    assert not any("hidden" in model_id for model_id in catalogue_ids)
