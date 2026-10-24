"""Base web search provider: config contract and KeyPool-owned rotation loop.

Concrete adapters implement :meth:`BaseWebSearchProvider._search_with_key`;
``search`` owns acquire -> try -> report semantics and domain-flag enforcement.
``search_once`` is one try on one named key, for a caller that owns key
selection itself (the web search chain).
"""

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import ClassVar

import httpx

from my_claude_code.core.websearch.models import WebSearchResponse

from .errors import (
    WebSearchConfigError,
    WebSearchError,
    WebSearchInvalidRequestError,
    WebSearchRateLimitError,
    WebSearchUpstreamError,
)
from .rotation import KeyPool, mask_key_label


@dataclass(frozen=True, slots=True)
class WebSearchProviderConfig:
    """Resolved immutable configuration for one web search provider instance."""

    api_keys: tuple[str, ...]  # may be empty for keyless providers (ddgs, searxng)
    credential_rotation: str  # single|round_robin|least_used|failover
    base_url: str | None  # override (searxng self-host, testing)
    proxy: str | None
    http_timeout: float  # seconds, default 20
    # Dotenv-only advanced options (env var name -> raw value), catalog-driven.
    options: Mapping[str, str] = field(default_factory=dict)


class BaseWebSearchProvider(ABC):
    """Base class for web search providers. Extend this to add your own."""

    PROVIDER_ID: ClassVar[str]
    SUPPORTS_DOMAINS: ClassVar[bool] = False

    def __init__(self, config: WebSearchProviderConfig) -> None:
        self._config = config
        # Keyless providers rotate over a single anonymous key slot (index 0).
        self._pool = KeyPool(
            config.api_keys or ("",), policy=config.credential_rotation
        )
        self._client: httpx.AsyncClient | None = None

    @property
    def provider_id(self) -> str:
        return type(self).PROVIDER_ID

    @property
    def config(self) -> WebSearchProviderConfig:
        return self._config

    @property
    def key_pool(self) -> KeyPool:
        return self._pool

    def key_label(self, key_index: int) -> str:
        """Masked ``first4…last4`` label for the key that served a request."""

        return mask_key_label(self._pool.key_at(key_index))

    async def search(
        self,
        query: str,
        *,
        max_results: int = 10,
        allowed_domains: tuple[str, ...] = (),
        blocked_domains: tuple[str, ...] = (),
    ) -> WebSearchResponse:
        """Run the search with key rotation; each key is tried at most once."""

        if not self.SUPPORTS_DOMAINS:
            allowed_domains = ()
            blocked_domains = ()
        excluded: set[int] = set()
        last_error: WebSearchError | None = None
        while True:
            acquired = self._pool.acquire(exclude=frozenset(excluded))
            if acquired is None:
                break
            key_index, key = acquired
            excluded.add(key_index)
            try:
                response = await self._search_with_key(
                    query,
                    key,
                    key_index,
                    max_results=max_results,
                    allowed_domains=allowed_domains,
                    blocked_domains=blocked_domains,
                )
            except WebSearchRateLimitError as error:
                error.key_index = key_index
                self._pool.report_rate_limit(
                    key_index,
                    message=error.message,
                    retry_after_seconds=error.retry_after_seconds,
                )
                last_error = error
            except WebSearchInvalidRequestError, WebSearchConfigError:
                # Caller/config faults: rotating keys cannot help.
                raise
            except WebSearchError as error:
                error.key_index = key_index
                self._pool.report_failure(
                    key_index, kind=error.kind, message=error.message
                )
                last_error = error
            except Exception as error:
                self._pool.report_failure(
                    key_index, kind="upstream", message=type(error).__name__
                )
                raise
            else:
                self._pool.report_success(key_index)
                return response
        if last_error is not None:
            raise last_error
        raise WebSearchUpstreamError(
            self.provider_id, "no API keys available to serve the search"
        )

    async def search_once(
        self,
        query: str,
        key: str,
        key_index: int,
        *,
        max_results: int = 10,
        allowed_domains: tuple[str, ...] = (),
        blocked_domains: tuple[str, ...] = (),
    ) -> WebSearchResponse:
        """One search with one named key: no pool, no rotation, no health.

        For a caller that owns key selection itself -- the web search chain
        (``api/web_tools/search_chain.py``, 7.112.0) drives the keys with the
        model path's own rotation and retry objects. The domain-flag rule is
        the same one :meth:`search` applies; :meth:`search` and its
        :class:`KeyPool` are untouched and still serve every search made
        without a chain.
        """

        if not self.SUPPORTS_DOMAINS:
            allowed_domains = ()
            blocked_domains = ()
        return await self._search_with_key(
            query,
            key,
            key_index,
            max_results=max_results,
            allowed_domains=allowed_domains,
            blocked_domains=blocked_domains,
        )

    @abstractmethod
    async def _search_with_key(
        self,
        query: str,
        key: str,
        key_index: int,
        *,
        max_results: int,
        allowed_domains: tuple[str, ...],
        blocked_domains: tuple[str, ...],
    ) -> WebSearchResponse:
        """Perform one search attempt with a specific key."""

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise WebSearchConfigError(
                self.provider_id, "HTTP client is not initialized"
            )
        return self._client

    async def close(self) -> None:
        """Release the HTTP client (if any)."""

        client = self._client
        self._client = None
        if client is not None:
            await client.aclose()
