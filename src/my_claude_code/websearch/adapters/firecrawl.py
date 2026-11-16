"""Firecrawl adapter (POST api.firecrawl.dev/v2/search with Bearer auth).

Advanced dotenv options: ``FIRECRAWL_SOURCES`` (web/news/images arrays),
``FIRECRAWL_SCRAPE_FORMAT`` per-result scrape (summary -> snippet upgrade,
markdown -> item.content; multiplies credits), ``FIRECRAWL_TBS`` date filter
and ``FIRECRAWL_LOCATION`` geo.
"""

from typing import Any, ClassVar

from my_claude_code.config.websearch_catalog import FIRECRAWL_DEFAULT_BASE
from my_claude_code.core.websearch.models import (
    WebSearchRequestOptions,
    WebSearchResponse,
    WebSearchResultItem,
)

from ..base import BaseWebSearchProvider, WebSearchProviderConfig
from ._common import TIME_RANGE_TO_TBS, text_str
from .http import build_async_client, request_json

_KNOWN_SOURCES = ("web", "news", "images")


class FirecrawlWebSearchProvider(BaseWebSearchProvider):
    PROVIDER_ID: ClassVar[str] = "firecrawl"
    SUPPORTS_DOMAINS: ClassVar[bool] = True

    def __init__(self, config: WebSearchProviderConfig) -> None:
        super().__init__(config)
        self._base_url = (config.base_url or FIRECRAWL_DEFAULT_BASE).rstrip("/")
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
        sources = self._sources()
        payload: dict[str, Any] = {
            "query": query,
            # ``limit`` is per source type upstream, so asking for
            # ``max_results`` from each of N sources fetches (and bills)
            # N times what the caller can use.
            "limit": max(1, -(-max_results // max(1, len(sources)))),
            "sources": sources,
        }
        country = (
            request_options.country.upper()
            if request_options is not None and request_options.country is not None
            else ""
        ) or options.get("FIRECRAWL_COUNTRY", "")
        if country:
            payload["country"] = country
        if categories := options.get("FIRECRAWL_CATEGORIES", ""):
            payload["categories"] = [
                entry.strip() for entry in categories.split(",") if entry.strip()
            ]
        scrape_format = options.get("FIRECRAWL_SCRAPE_FORMAT", "")
        if request_options is not None:
            if request_options.raw_content_format is not None:
                scrape_format = (
                    "markdown"
                    if request_options.raw_content_format == "markdown"
                    else scrape_format
                )
            elif request_options.include_content is True and not scrape_format:
                scrape_format = "markdown"
            elif request_options.include_content is False:
                scrape_format = ""
        if scrape_format:
            payload["scrapeOptions"] = {"formats": [{"type": scrape_format}]}
        tbs = (
            TIME_RANGE_TO_TBS.get(request_options.time_range, "")
            if request_options is not None and request_options.time_range is not None
            else ""
        ) or options.get("FIRECRAWL_TBS", "")
        if tbs:
            payload["tbs"] = tbs
        if location := options.get("FIRECRAWL_LOCATION", ""):
            payload["location"] = location
        # includeDomains/excludeDomains are mutually exclusive upstream.
        if allowed_domains:
            payload["includeDomains"] = list(allowed_domains)
        elif blocked_domains:
            payload["excludeDomains"] = list(blocked_domains)
        data = await request_json(
            self._require_client(),
            self.provider_id,
            "POST",
            f"{self._base_url}/v2/search",
            headers={"Authorization": f"Bearer {key}"},
            json_body=payload,
        )
        section = data.get("data", {}) if isinstance(data, dict) else {}
        items = []
        for source in sources:
            if source not in _KNOWN_SOURCES:
                continue
            rows = section.get(source, []) if isinstance(section, dict) else []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                item = self._map_row(source, row)
                if item is not None:
                    items.append(item)
        return WebSearchResponse(
            provider=self.provider_id,
            query=query,
            results=tuple(items[:max_results]),
            key_index=key_index,
            cost_usd=None,
        )

    def _sources(self) -> list[str]:
        raw = self._config.options.get("FIRECRAWL_SOURCES", "")
        sources = [part.strip() for part in raw.split(",") if part.strip()]
        return sources or ["web"]

    @staticmethod
    def _map_row(source: str, row: dict[str, Any]) -> WebSearchResultItem | None:
        if source == "images":
            url = text_str(row.get("imageUrl")) or text_str(row.get("url"))
            if not url:
                return None
            return WebSearchResultItem(
                title=text_str(row.get("title")),
                url=url,
                snippet="",
                content=None,
                published=None,
            )
        url = text_str(row.get("url"))
        if not url:
            return None
        if source == "news":
            return WebSearchResultItem(
                title=text_str(row.get("title")),
                url=url,
                snippet=text_str(row.get("snippet"))
                or text_str(row.get("description")),
                content=text_str(row.get("markdown")) or None,
                published=text_str(row.get("date")) or None,
            )
        return WebSearchResultItem(
            title=text_str(row.get("title")),
            url=url,
            snippet=text_str(row.get("summary")) or text_str(row.get("description")),
            content=text_str(row.get("markdown")) or None,
            published=None,
        )
