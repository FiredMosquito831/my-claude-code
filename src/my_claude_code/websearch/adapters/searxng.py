"""SearXNG adapter (keyless, self-hosted; GET {base}/search?format=json)."""

from typing import Any, ClassVar

from my_claude_code.core.websearch.models import (
    WebSearchRequestOptions,
    WebSearchResponse,
    WebSearchResultItem,
)

from ..base import BaseWebSearchProvider, WebSearchProviderConfig
from ..errors import WebSearchAuthError, WebSearchConfigError
from ._common import TIME_RANGE_TO_SEARXNG, opt_float, text_str
from .http import build_async_client, request_json


class SearxngWebSearchProvider(BaseWebSearchProvider):
    PROVIDER_ID: ClassVar[str] = "searxng"
    SUPPORTS_DOMAINS: ClassVar[bool] = False

    def __init__(self, config: WebSearchProviderConfig) -> None:
        super().__init__(config)
        if not config.base_url or not config.base_url.strip():
            raise WebSearchConfigError(
                self.provider_id,
                "SEARXNG_BASE_URL is required for the searxng provider "
                "(point it at a self-hosted instance with format=json enabled)",
            )
        self._base_url = config.base_url.rstrip("/")
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
        options = self._config.options
        params: dict[str, Any] = {"q": query, "format": "json"}
        if engines := options.get("SEARXNG_ENGINES", ""):
            params["engines"] = engines
        categories = (
            request_options.topic
            if request_options is not None
            and request_options.topic in {"general", "news"}
            else ""
        ) or options.get("SEARXNG_CATEGORIES", "")
        if categories:
            params["categories"] = categories
        time_range = (
            TIME_RANGE_TO_SEARXNG.get(request_options.time_range, "")
            if request_options is not None and request_options.time_range is not None
            else ""
        ) or options.get("SEARXNG_TIME_RANGE", "")
        if time_range:
            params["time_range"] = time_range
        language = (
            request_options.language
            if request_options is not None and request_options.language is not None
            else ""
        ) or options.get("SEARXNG_LANGUAGE", "")
        if language:
            params["language"] = language
        if safesearch := options.get("SEARXNG_SAFESEARCH", ""):
            params["safesearch"] = safesearch
        try:
            data = await request_json(
                self._require_client(),
                self.provider_id,
                "GET",
                f"{self._base_url}/search",
                params=params,
            )
        except WebSearchAuthError as error:
            # Most public instances disable format=json and answer 403.
            raise WebSearchAuthError(
                self.provider_id,
                "SearXNG instance rejected the request; enable format=json in "
                f"its settings.yml ({error.message})",
                status_code=error.status_code,
            ) from error
        rows = data.get("results", []) if isinstance(data, dict) else []
        items = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            url = text_str(row.get("url"))
            if not url:
                continue
            items.append(
                WebSearchResultItem(
                    title=text_str(row.get("title")),
                    url=url,
                    snippet=text_str(row.get("content")),
                    content=None,
                    published=text_str(row.get("publishedDate")) or None,
                    score=opt_float(row.get("score")),
                )
            )
        return WebSearchResponse(
            provider=self.provider_id,
            query=query,
            results=tuple(items[:max_results]),
            key_index=key_index,
            cost_usd=None,
        )
