"""One closable generation of lazily constructed provider clients."""

import asyncio
from collections.abc import MutableMapping

from my_claude_code.config.settings import Settings
from my_claude_code.providers.base import BaseProvider

from .factory import create_provider


class ProviderRuntime:
    """Own provider instances for one immutable settings snapshot."""

    def __init__(
        self,
        settings: Settings,
        providers: MutableMapping[str, BaseProvider] | None = None,
    ) -> None:
        self.settings = settings
        self._providers = providers if providers is not None else {}

    def is_cached(self, provider_id: str) -> bool:
        """Return whether a provider for this id is already cached."""
        return provider_id in self._providers

    def resolve_provider(self, provider_id: str) -> BaseProvider:
        """Return an existing provider or create it lazily."""
        if provider_id not in self._providers:
            self._providers[provider_id] = create_provider(provider_id, self.settings)
        return self._providers[provider_id]

    def cached_providers(self) -> dict[str, BaseProvider]:
        """A snapshot of every provider this generation holds right now."""
        return dict(self._providers)

    def adopt(self, provider_id: str, provider: BaseProvider) -> bool:
        """Hold a provider another generation already built, unless one is here.

        How a proxy-chain save keeps every *other* provider's object -- its
        client, its credential pool and its benches -- across a generation
        replace (7.55.0). Returns whether the object was taken: an id this
        runtime already holds keeps what it has, so a runtime a factory
        pre-populated is never overridden and nothing it built is orphaned.
        """
        if provider_id in self._providers:
            return False
        self._providers[provider_id] = provider
        return True

    def detach(self, provider_id: str, provider: BaseProvider) -> bool:
        """Stop holding ``provider`` without closing it.

        For an object another live generation still holds: the one that holds
        it last is the one whose :meth:`cleanup` closes it. Only the exact
        object is detached, so a same-id provider this runtime built for
        itself is never let go by mistake.
        """
        if self._providers.get(provider_id) is not provider:
            return False
        del self._providers[provider_id]
        return True

    async def cleanup(self) -> None:
        """Release every provider client constructed by this generation."""
        errors: list[Exception] = []
        for provider_id, provider in list(self._providers.items()):
            try:
                await provider.cleanup()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                errors.append(exc)
            else:
                self._providers.pop(provider_id, None)
        if len(errors) == 1:
            raise errors[0]
        if len(errors) > 1:
            raise ExceptionGroup("One or more provider cleanups failed", errors)
