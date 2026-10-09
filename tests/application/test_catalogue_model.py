"""The neutral catalogue record carries the ladder's answers, unknowns included."""

from collections.abc import Callable

from my_claude_code.api.model_catalog import build_models_list_response
from my_claude_code.application.catalogue_model import (
    build_catalogue_models,
    derive_supports_tool_calls,
)
from my_claude_code.application.litellm_model_map import LiteLLMCatalogue
from my_claude_code.application.model_metadata import (
    DeclaredModalities,
    ModelReasoningCapability,
    ProviderModelDeclaration,
    ProviderModelInfo,
)
from my_claude_code.application.openrouter_live import LiveCatalogue
from my_claude_code.application.ports import RequestRuntimeLease, RequestRuntimePort
from my_claude_code.config.settings import Settings
from my_claude_code.core.gateway_model_ids import gateway_model_id
from my_claude_code.core.model_ids import ResolutionTier
from my_claude_code.core.reasoning import ReasoningEffort
from my_claude_code.core.tier_refs import TIER_ORDER, ModelTier, is_tier_ref, tier_ref


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
        tool_calls: dict[str, bool] | None = None,
        prices: dict[str, dict[str, float]] | None = None,
        modalities: dict[str, DeclaredModalities] | None = None,
        kind_words: dict[str, tuple[str, ...]] | None = None,
        live: LiveCatalogue | None = None,
        tiers: dict[str, ResolutionTier] | None = None,
    ) -> None:
        self._settings = settings
        self._cached_infos = cached_infos
        self._context_lengths = context_lengths or {}
        self._output_limits = output_limits or {}
        self._vision = vision or {}
        self._thinking = thinking or {}
        self._reasoning = reasoning or {}
        self._tool_calls = tool_calls or {}
        self._prices = prices or {}
        self._modalities = modalities or {}
        self._kind_words = kind_words or {}
        self._live = live
        # The rung each ``(field, ref)`` answer came from, where a test needs
        # one (7.84.0: OpenRouter's live list is placed by rung). Keyed
        # ``"<field>:<provider>/<model>"``; absent means no rung (``None``).
        self._tiers = tiers or {}

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

    def model_context_length_tiered(
        self, provider_id: str, model_id: str
    ) -> tuple[int | None, ResolutionTier | None]:
        return self._context_lengths.get(f"{provider_id}/{model_id}"), None

    def model_vision_tiered(
        self, provider_id: str, model_id: str
    ) -> tuple[bool | None, ResolutionTier | None]:
        ref = f"{provider_id}/{model_id}"
        return self._vision.get(ref), self._tiers.get(f"vision:{ref}")

    def model_tool_call_tiered(
        self, provider_id: str, model_id: str
    ) -> tuple[bool | None, ResolutionTier | None]:
        ref = f"{provider_id}/{model_id}"
        return self._tool_calls.get(ref), self._tiers.get(f"tools:{ref}")

    def model_can_reason_tiered(
        self, provider_id: str, model_id: str
    ) -> tuple[bool | None, ResolutionTier | None]:
        ref = f"{provider_id}/{model_id}"
        capability = self._reasoning.get(ref)
        if capability is None or capability.can_reason is None:
            return None, None
        return capability.can_reason, self._tiers.get(f"reason:{ref}")

    def litellm_model_catalogue(self) -> LiteLLMCatalogue | None:
        return None

    def openrouter_live_catalogue(self) -> LiveCatalogue | None:
        return self._live

    def model_prices_tiered(
        self, provider_id: str, model_id: str
    ) -> dict[str, tuple[float | None, ResolutionTier | None]]:
        rates = self._prices.get(f"{provider_id}/{model_id}", {})
        return {
            name: (rates.get(name), None)
            for name in (
                "input_price",
                "output_price",
                "cache_read_price",
                "cache_write_price",
            )
        }

    def model_modalities_lookup(
        self,
    ) -> Callable[[str, str], tuple[DeclaredModalities | None, ResolutionTier | None]]:
        def lookup(
            provider_id: str, model_id: str
        ) -> tuple[DeclaredModalities | None, ResolutionTier | None]:
            declared = self._modalities.get(f"{provider_id}/{model_id}")
            if declared is None:
                return None, None
            return declared, ResolutionTier.MODELS_DEV_BUCKET_EXACT

        return lookup

    def model_kind_words_lookup(
        self,
    ) -> Callable[[str, str], tuple[tuple[str, ...] | None, ResolutionTier | None]]:
        def lookup(
            provider_id: str, model_id: str
        ) -> tuple[tuple[str, ...] | None, ResolutionTier | None]:
            words = self._kind_words.get(f"{provider_id}/{model_id}")
            if words is None:
                return None, None
            return words, ResolutionTier.PROVIDER_EXACT

        return lookup

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


def test_the_rows_own_tool_word_answers_before_models_dev() -> None:
    """7.83.0: no parameter list, but the row said ``function-calling``."""

    stated = ProviderModelInfo(
        "novita/stated", declared=ProviderModelDeclaration(tool_calls=True)
    )
    silent = ProviderModelInfo("novita/silent")
    runtime = FakeRuntime(
        settings=_settings(),
        cached_infos=(stated, silent),
        tool_calls={"novita/stated": False, "novita/silent": False},
    )

    entries = {
        model.gateway_id: model
        for model in build_catalogue_models(runtime.current_settings(), runtime)
    }

    assert entries["anthropic/novita/stated"].supports_tool_calls is True
    # Where the row said nothing, models.dev still answers, as before.
    assert entries["anthropic/novita/silent"].supports_tool_calls is False


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
        # The tier aliases are names for routes and are asserted in
        # ``test_catalogue_tier_aliases``. Since 6.56.0 they are emitted even
        # when the model a tier points at publishes no thinking variant, which
        # is exactly this model -- so they would otherwise crowd out the one
        # fact under test here.
        if not model.provider_model_ref.startswith("mcc/")
    ]

    assert ids == ["claude-3-freecc-no-thinking/open_router/plain"]


def test_a_tier_whose_target_only_has_a_no_thinking_variant_still_gets_an_alias() -> (
    None
):
    """The other half of the missing-``mcc/vision`` bug.

    The alias lookup skipped every ``force_no_thinking`` record, so a tier
    pointing at a model whose provider publishes no thinking variant found
    nothing and was dropped.
    """

    runtime = FakeRuntime(
        settings=_settings(model="open_router/plain"),
        cached_infos=(ProviderModelInfo("open_router/plain"),),
        thinking={"open_router/plain": False},
    )

    refs = {
        model.provider_model_ref
        for model in build_catalogue_models(runtime.current_settings(), runtime)
    }

    assert "mcc/vision" in refs


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
        model.gateway_id
        for model in build_catalogue_models(settings, runtime)
        if not is_tier_ref(model.provider_model_ref)
    }
    listing = build_models_list_response(settings, runtime)
    # The eight Claude protocol aliases are names, not routable refs, and are
    # deliberately exempt from visibility; so are the five coding-agent tier
    # aliases, for the same reason and asserted separately below. Everything
    # else must match exactly.
    listing_ids = {
        entry.id
        for entry in listing.data
        if "/" in entry.id and not is_tier_ref(entry.id)
    }

    assert catalogue_ids == listing_ids
    assert not any("hidden" in model_id for model_id in catalogue_ids)


def test_both_listings_carry_the_five_tier_aliases_exempt_from_visibility() -> None:
    """A tier alias is a protocol name for one of MCC's own routes.

    Filtering one would not hide a model: it would remove the id a coding
    agent's config file already names and break that agent's next session --
    exactly the reasoning the eight Claude aliases already carry.
    """

    settings = _settings(model="open_router/shown", model_visibility_deny="*")
    runtime = FakeRuntime(
        settings=settings, cached_infos=(ProviderModelInfo("open_router/shown"),)
    )

    listing_ids = {
        entry.id for entry in build_models_list_response(settings, runtime).data
    }

    for tier in TIER_ORDER:
        assert tier_ref(tier) in listing_ids
        assert gateway_model_id(tier_ref(tier)) in listing_ids


def test_the_catalogue_is_filtered_by_model_visibility() -> None:
    """The same two glob lists ``/v1/models`` obeys, and nothing asserted it.

    Live proof it already worked: 1,115 discovered rows, 142 visible on the
    Models page, 142 entries in every generated document. Pinned here so a
    future enumeration change cannot quietly publish a hidden model to a CLI.
    """

    runtime = FakeRuntime(
        settings=_settings(
            model="open_router/kept", model_visibility_deny="open_router/hidden*"
        ),
        cached_infos=(
            ProviderModelInfo("open_router/kept"),
            ProviderModelInfo("open_router/hidden-one"),
        ),
    )

    refs = {
        model.provider_model_ref
        for model in build_catalogue_models(runtime.current_settings(), runtime)
        if not is_tier_ref(model.provider_model_ref)
    }

    assert refs == {"open_router/kept"}


def test_the_configured_primary_route_is_marked_on_its_record() -> None:
    """Three CLIs must pin one model to open a session at all.

    Taking the first entry is an enumeration artefact: on a real install it
    picked a free tier the provider had withdrawn, so every Cline, Crush and
    Goose session opened on a model that answered 404.
    """

    runtime = FakeRuntime(
        settings=_settings(model="open_router/primary"),
        cached_infos=(
            ProviderModelInfo("open_router/other"),
            ProviderModelInfo("open_router/primary"),
        ),
    )

    models = build_catalogue_models(runtime.current_settings(), runtime)
    primary = {model.provider_model_ref for model in models if model.is_primary_route}

    # Since the tier aliases exist, the mark moved onto ``mcc/best`` -- which
    # *is* the route ``MODEL`` names, one level of indirection later. A session
    # opened on it follows the route when the operator moves it, where a session
    # opened on ``open_router/primary`` froze today's answer into the agent's
    # own config file. The raw record must not keep the mark as well, or
    # ``select_starting_index`` would return whichever came first.
    assert primary == {tier_ref(ModelTier.BEST)}
    best = next(model for model in models if model.is_primary_route)
    raw = next(
        model
        for model in models
        if model.provider_model_ref == "open_router/primary"
        and not model.force_no_thinking
    )
    assert best.display_name == "Best (open_router/primary)"
    assert not raw.is_primary_route


def _media_runtime(**update: object) -> FakeRuntime:
    """A catalogue holding one model of every stated kind, and one of none."""

    return FakeRuntime(
        settings=_settings(**update),
        cached_infos=(
            ProviderModelInfo("open_router/chat"),
            ProviderModelInfo("open_router/draws"),
            ProviderModelInfo("open_router/chat-and-draws"),
            ProviderModelInfo("open_router/speaks"),
            ProviderModelInfo("open_router/hears"),
            ProviderModelInfo("open_router/films"),
            ProviderModelInfo("open_router/never-described"),
            ProviderModelInfo("custom_x/placed-image"),
        ),
        modalities={
            "open_router/chat": DeclaredModalities(("text",), ("text",)),
            "open_router/draws": DeclaredModalities(("text",), ("image",)),
            "open_router/chat-and-draws": DeclaredModalities(
                ("text", "image"), ("image", "text")
            ),
            "open_router/speaks": DeclaredModalities(("text",), ("audio",)),
            "open_router/hears": DeclaredModalities(("audio",), ("text",)),
            "open_router/films": DeclaredModalities(("text",), ("video",)),
        },
    )


def _listed_refs(settings: Settings, runtime: FakeRuntime) -> tuple[set[str], set[str]]:
    catalogue = {
        model.provider_model_ref
        for model in build_catalogue_models(settings, runtime)
        if not is_tier_ref(model.provider_model_ref)
    }
    listing = {
        entry.display_name.removesuffix(" (no thinking)")
        for entry in build_models_list_response(settings, runtime).data
        if "/" in entry.id and not is_tier_ref(entry.id)
    }
    return catalogue, listing


def test_media_models_leave_both_chat_listings_and_saved_refs_stay() -> None:
    """7.78.2: a stated non-chat kind is not offered as a chat model.

    ``/v1/models`` and the harness catalogues drop the same refs, an unknown
    kind stays, a model that also chats stays, and a media model the operator
    saved on a chat rail stays listed -- it is the route they chose.
    """

    runtime = _media_runtime(
        model="open_router/chat",
        model_haiku="open_router/films",
        model_image="custom_x/placed-image",
    )
    settings = runtime.current_settings()

    catalogue, listing = _listed_refs(settings, runtime)

    assert catalogue == listing
    assert catalogue == {
        "open_router/chat",
        "open_router/chat-and-draws",
        "open_router/never-described",
        # Saved on MODEL_HAIKU: listed whatever its kind.
        "open_router/films",
    }


def test_the_providers_own_words_fill_only_where_both_modality_rungs_are_silent() -> (
    None
):
    """7.80.0: the coarse rung -- model type and endpoint words -- in both lists.

    An endpoint list naming only an image endpoint takes a model out of both
    chat lists; a word the table does not know (Anthropic's ``model``) states
    nothing, so that model stays; and words never outrank a modality pair.
    """

    runtime = FakeRuntime(
        settings=_settings(model="open_router/chat"),
        cached_infos=(
            ProviderModelInfo("open_router/chat"),
            ProviderModelInfo("custom_x/draws-by-endpoint"),
            ProviderModelInfo("custom_x/typed-model"),
            ProviderModelInfo("custom_x/pair-wins"),
        ),
        modalities={
            "open_router/chat": DeclaredModalities(("text",), ("text",)),
            "custom_x/pair-wins": DeclaredModalities(("text",), ("text",)),
        },
        kind_words={
            "custom_x/draws-by-endpoint": ("/v1/images/generations",),
            "custom_x/typed-model": ("model",),
            "custom_x/pair-wins": ("image-generation",),
        },
    )
    settings = runtime.current_settings()

    catalogue, listing = _listed_refs(settings, runtime)

    assert catalogue == listing
    assert catalogue == {
        "open_router/chat",
        "custom_x/typed-model",
        "custom_x/pair-wins",
    }


def test_the_kind_filter_never_runs_ahead_of_visibility_or_reorders() -> None:
    """Hide-only: the surviving entries keep their order and their twins."""

    runtime = _media_runtime(model="open_router/chat")
    settings = runtime.current_settings()

    ids = [entry.id for entry in build_models_list_response(settings, runtime).data]
    discovered = [model_id for model_id in ids if "open_router/" in model_id]

    assert discovered == [
        "anthropic/open_router/chat",
        "claude-3-freecc-no-thinking/open_router/chat",
        "anthropic/open_router/chat-and-draws",
        "claude-3-freecc-no-thinking/open_router/chat-and-draws",
        "anthropic/open_router/never-described",
        "claude-3-freecc-no-thinking/open_router/never-described",
    ]


def test_a_tier_alias_still_points_at_a_media_model_saved_on_its_tier() -> None:
    """The alias is a route, so a saved cross-kind primary keeps its alias."""

    runtime = _media_runtime(model="open_router/draws")
    settings = runtime.current_settings()

    aliases = {
        model.provider_model_ref: model.display_name
        for model in build_catalogue_models(settings, runtime)
        if is_tier_ref(model.provider_model_ref)
    }

    assert aliases[tier_ref(ModelTier.BEST)] == "Best (open_router/draws)"
