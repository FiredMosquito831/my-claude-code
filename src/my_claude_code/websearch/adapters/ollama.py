"""Ollama hosted web search adapter (POST ollama.com/api/web_search)."""

from typing import Any, ClassVar

from my_claude_code.config.websearch_catalog import OLLAMA_SEARCH_DEFAULT_BASE
from my_claude_code.core.websearch.models import (
    WebSearchRequestOptions,
    WebSearchResponse,
    WebSearchResultItem,
)

from ..base import BaseWebSearchProvider, WebSearchProviderConfig
from .http import build_async_client, request_json

_SNIPPET_CHARS = 1000
# Ollama caps max_results at 10 per request.
_MAX_RESULTS_CAP = 10


class OllamaWebSearchProvider(BaseWebSearchProvider):
    PROVIDER_ID: ClassVar[str] = "ollama"
    SUPPORTS_DOMAINS: ClassVar[bool] = False

    def __init__(self, config: WebSearchProviderConfig) -> None:
        super().__init__(config)
        self._base_url = (config.base_url or OLLAMA_SEARCH_DEFAULT_BASE).rstrip("/")
        self._client = build_async_client(
            proxy=config.proxy, http_timeout=config.http_timeout
        )

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
        return await self._search_with_key_options(
            query,
            key,
            key_index,
            max_results=max_results,
            allowed_domains=allowed_domains,
            blocked_domains=blocked_domains,
            request_options=None,
        )

    async def _search_with_key_options(
        self,
        query: str,
        key: str,
        key_index: int,
        *,
        max_results: int,
        allowed_domains: tuple[str, ...],
        blocked_domains: tuple[str, ...],
        request_options: WebSearchRequestOptions | None = None,
    ) -> WebSearchResponse:
        _ = request_options
        data = await request_json(
            self._require_client(),
            self.provider_id,
            "POST",
            f"{self._base_url}/api/web_search",
            headers={"Authorization": f"Bearer {key}"},
            json_body={
                "query": query,
                "max_results": min(max_results, _MAX_RESULTS_CAP),
            },
        )
        rows = data.get("results", []) if isinstance(data, dict) else []
        items = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            url = _text(row.get("url"))
            if not url:
                continue
            content = _text(row.get("content"))
            items.append(
                WebSearchResultItem(
                    title=_text(row.get("title")),
                    url=url,
                    snippet=content[:_SNIPPET_CHARS],
                    content=content or None,
                    published=None,
                )
            )
        return WebSearchResponse(
            provider=self.provider_id,
            query=query,
            results=tuple(items[:max_results]),
            key_index=key_index,
            cost_usd=None,
        )


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""
