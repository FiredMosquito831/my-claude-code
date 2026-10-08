"""The media node a chain becomes when it may not go direct and cannot go anywhere.

The media copy of :mod:`my_claude_code.providers.runtime.masked_refusal`, built
by the media registry's ``_single`` from the same
:class:`~my_claude_code.providers.base.MaskedRefusalPlan` (7.78.8): a chain
switched on with Direct fallback off and nothing in it to route through. The
leaf it replaces would have had no proxy at all.

It owns no client. It still answers :meth:`supports` from the provider's
declared surfaces -- a provider that never offered an endpoint is skipped
uncharged, as always -- and everything it does support is refused at
``preflight`` with the 503 that names the setting, so the media executor moves
to the next model exactly as it does for any provider that cannot serve. An
accepted job polled while the chain is refused finds no leaf to be read
through (:meth:`leaf_for` is ``None``), so it is not dialled either.
"""

from collections.abc import AsyncIterator

from my_claude_code.application.media.request import (
    MediaAttempt,
    MediaChunk,
    MediaRequest,
)
from my_claude_code.config.credentials import mask_key_label
from my_claude_code.config.media_surfaces import MediaSurface
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.runtime.masked_refusal import masked_refusal_failure

from .leaf import MediaLeaf, surfaces_serve


class _RefusedMedia:
    """An async iterator whose first step raises the refusal."""

    def __init__(self, message: str) -> None:
        self._message = message

    def __aiter__(self) -> _RefusedMedia:
        return self

    async def __anext__(self) -> MediaChunk:
        raise masked_refusal_failure(self._message)

    async def aclose(self) -> None:
        return None


class MediaRefusalNode:
    """Answers every media call with "not sent: Direct fallback is off"."""

    def __init__(
        self,
        *,
        provider_id: str,
        config: ProviderConfig,
        surfaces: tuple[MediaSurface, ...],
        message: str,
    ) -> None:
        self._provider_id = provider_id
        self._config = config
        self._surfaces = surfaces
        self._message = message

    @property
    def provider_id(self) -> str:
        return self._provider_id

    @property
    def refusal(self) -> str:
        return self._message

    @property
    def credential_label(self) -> str | None:
        key = self._config.api_key
        return mask_key_label(key) if key else None

    def throttle_remaining(self, model: str | None = None) -> float:
        return 0.0

    def supports(self, request: MediaRequest) -> bool:
        return surfaces_serve(self._surfaces, request)

    def preflight(self, attempt: MediaAttempt) -> None:
        raise masked_refusal_failure(self._message)

    def execute(
        self, attempt: MediaAttempt, *, request_id: str | None = None
    ) -> AsyncIterator[MediaChunk]:
        return _RefusedMedia(self._message)

    def leaf_for(self, key_index: int, proxy_label: str | None) -> MediaLeaf | None:
        return None

    async def cleanup(self) -> None:
        return None


__all__ = ["MediaRefusalNode"]
