"""Provider model-list metadata cache."""

from collections.abc import Iterable
from dataclasses import replace

from my_claude_code.application.model_metadata import (
    ModelReasoningCapability,
    ProviderModelInfo,
)
from my_claude_code.config.provider_registry import get_provider_registry
from my_claude_code.core.model_ids import ResolutionTier, strip_model_id_tag
from my_claude_code.providers.model_listing import model_infos_from_ids


class ProviderModelCache:
    """Store provider model metadata for instant model-list responses."""

    def __init__(
        self,
        available_provider_ids: Iterable[str] | None = None,
    ) -> None:
        if available_provider_ids is None:
            available_provider_ids = get_provider_registry().supported_ids()
        self._available_provider_ids = frozenset(available_provider_ids)
        self._model_infos_by_provider: dict[str, dict[str, ProviderModelInfo]] = {}

    def cache_model_ids(self, provider_id: str, model_ids: Iterable[str]) -> None:
        """Store raw provider model ids with unknown capability metadata."""
        self.cache_model_infos(provider_id, model_infos_from_ids(model_ids))

    def cache_model_infos(
        self, provider_id: str, model_infos: Iterable[ProviderModelInfo]
    ) -> None:
        """Store provider model metadata by raw provider model id."""
        if provider_id not in self._available_provider_ids:
            return
        clean_infos = {
            info.model_id: info for info in model_infos if info.model_id.strip()
        }
        self._model_infos_by_provider[provider_id] = clean_infos

    def set_available_providers(self, provider_ids: Iterable[str]) -> None:
        """Replace the provider scope and discard entries outside it."""
        self._available_provider_ids = frozenset(provider_ids)
        self._model_infos_by_provider = {
            provider_id: infos
            for provider_id, infos in self._model_infos_by_provider.items()
            if provider_id in self._available_provider_ids
        }

    def add_provider(self, provider_id: str) -> None:
        """Make one dynamically authenticated provider cacheable."""

        self._available_provider_ids = self._available_provider_ids | {provider_id}

    def remove_provider(self, provider_id: str) -> None:
        """Evict one provider and stop accepting its discovered metadata."""

        self._available_provider_ids = self._available_provider_ids - {provider_id}
        self._model_infos_by_provider.pop(provider_id, None)

    def cached_model_ids(self) -> dict[str, frozenset[str]]:
        """Return cached raw provider model ids by provider."""
        return {
            provider_id: frozenset(infos)
            for provider_id, infos in self._model_infos_by_provider.items()
        }

    def has_provider(self, provider_id: str) -> bool:
        """Return whether this provider has any cached model-list result."""
        return provider_id in self._model_infos_by_provider

    def cached_model_info_tiered(
        self, provider_id: str, model_id: str
    ) -> tuple[ProviderModelInfo, ResolutionTier] | None:
        """Find one model in its own provider's catalogue, and say how.

        Tier 1 is the id exactly as routed. Tier 2 is the same id with its
        pricing/routing tag stripped -- ``minimax/minimax-m3-free`` against a
        catalogue that lists ``minimax/minimax-m3``, or ``tencent/hy3-paid``
        against ``tencent/hy3``. Tier 2 is the rung that did not exist before:
        a tagged model whose own host publishes limits under the untagged name
        used to skip straight past it to a stranger's catalogue, and a
        stranger's catalogue must never be consulted while the model's own
        host still has an untried answer.

        The tag is stripped from the *query*, never from the catalogue, so an
        exact entry always wins and a provider listing both ``x`` and
        ``x:free`` keeps them distinct.
        """
        infos = self._model_infos_by_provider.get(provider_id, {})
        info = infos.get(model_id)
        if info is not None:
            return info, ResolutionTier.PROVIDER_EXACT
        stripped = strip_model_id_tag(model_id)
        if stripped is None:
            return None
        for candidate, entry in infos.items():
            if candidate.strip().lower() == stripped:
                return entry, ResolutionTier.PROVIDER_TAG_STRIPPED
        return None

    def _cached_model_info(
        self, provider_id: str, model_id: str
    ) -> ProviderModelInfo | None:
        found = self.cached_model_info_tiered(provider_id, model_id)
        return None if found is None else found[0]

    def cached_model_supports_thinking(
        self, provider_id: str, model_id: str
    ) -> bool | None:
        """Return cached thinking support when a provider exposes it."""
        info = self._cached_model_info(provider_id, model_id)
        if info is None:
            return None
        return info.supports_thinking

    def cached_model_supports_vision(
        self, provider_id: str, model_id: str
    ) -> bool | None:
        """Return cached image-input support when a provider exposes it."""
        info = self._cached_model_info(provider_id, model_id)
        if info is None:
            return None
        return info.supports_vision

    def cached_model_max_output_tokens(
        self, provider_id: str, model_id: str
    ) -> int | None:
        """Return the provider's own declared output ceiling for this model.

        ``None`` when the provider does not publish one, which is unknown, not
        unlimited and not zero.
        """
        info = self._cached_model_info(provider_id, model_id)
        if info is None:
            return None
        return info.max_output_tokens

    def cached_model_context_length(
        self, provider_id: str, model_id: str
    ) -> int | None:
        """Return the provider's own declared context window for this model.

        Prompt plus completion, unlike
        :meth:`cached_model_max_output_tokens`. ``None`` when the provider does
        not publish one.
        """
        info = self._cached_model_info(provider_id, model_id)
        if info is None:
            return None
        return info.context_length

    def cached_model_reasoning_capability(
        self, provider_id: str, model_id: str
    ) -> ModelReasoningCapability | None:
        """Return everything the provider itself said about this model's reasoning.

        The whole capability, not the single thinking boolean: a gateway that
        publishes a ``reasoning`` block states its effort vocabulary, whether
        thinking is mandatory and whether a token budget is accepted, and all
        of it must reach the merge or models.dev silently decides questions the
        routing target already answered.

        ``None`` when the model is not cached, or when the provider said
        nothing at all about its reasoning.
        """
        info = self._cached_model_info(provider_id, model_id)
        if info is None:
            return None
        capability = info.reasoning_capability
        if capability is None:
            if info.supports_thinking is None:
                return None
            return ModelReasoningCapability(can_reason=info.supports_thinking)
        if capability.can_reason is None and info.supports_thinking is not None:
            return replace(capability, can_reason=info.supports_thinking)
        return capability

    def cached_prefixed_model_refs(self) -> tuple[str, ...]:
        """Return cached provider models in user-selectable ``provider/model`` form."""
        return tuple(info.model_id for info in self.cached_prefixed_model_infos())

    def cached_prefixed_model_infos(self) -> tuple[ProviderModelInfo, ...]:
        """Return cached provider models with user-selectable prefixed ids."""
        infos: list[ProviderModelInfo] = []
        supported_ids = get_provider_registry().supported_ids()
        ordered_ids = [
            provider_id
            for provider_id in supported_ids
            if provider_id in self._available_provider_ids
        ]
        ordered_ids.extend(
            provider_id
            for provider_id in self._model_infos_by_provider
            if provider_id not in supported_ids
        )
        for provider_id in ordered_ids:
            provider_infos = self._model_infos_by_provider.get(provider_id, {})
            infos.extend(
                replace(info, model_id=f"{provider_id}/{info.model_id}")
                for info in sorted(
                    provider_infos.values(), key=lambda item: item.model_id
                )
            )
        return tuple(infos)

    def clear(self) -> None:
        """Clear all cached model metadata."""
        self._model_infos_by_provider.clear()
