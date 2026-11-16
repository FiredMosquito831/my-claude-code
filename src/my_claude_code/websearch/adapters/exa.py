"""Exa adapter (POST api.exa.ai/search with x-api-key auth).

Snippets are requested via the ``contents.highlights`` opt-in; the provider's
``costDollars.total`` is surfaced as ``cost_usd``. Advanced dotenv options:
``EXA_SEARCH_TYPE`` (deep* costs more), ``EXA_CONTENTS`` content modes,
``EXA_CATEGORY`` verticals (company/people skip date+exclude filters),
date/geo filters, and ``EXA_MAX_AGE_HOURS`` crawl freshness.
"""

from typing import Any, ClassVar

from my_claude_code.config.websearch_catalog import EXA_DEFAULT_BASE
from my_claude_code.core.websearch.models import (
    WebSearchRequestOptions,
    WebSearchResponse,
    WebSearchResultItem,
)

from ..base import BaseWebSearchProvider, WebSearchProviderConfig
from ..options import option_int
from ._common import (
    DEPTH_TO_EXA,
    opt_float,
    opt_float_tuple,
    opt_str,
    opt_str_tuple,
    text_str,
    time_range_start_iso,
)
from .http import build_async_client, request_json

_SNIPPET_CHARS = 1000
# company/people categories reject date filters and excludeDomains upstream.
_CATEGORIES_WITHOUT_DATE_OR_EXCLUDE = frozenset({"company", "people"})

_CONTENTS_MODES: dict[str, dict[str, bool]] = {
    "highlights": {"highlights": True},
    "text": {"text": True},
    "highlights+text": {"highlights": True, "text": True},
    "highlights+summary": {"highlights": True, "summary": True},
    "full": {"highlights": True, "text": True, "summary": True},
}


class ExaWebSearchProvider(BaseWebSearchProvider):
    PROVIDER_ID: ClassVar[str] = "exa"
    SUPPORTS_DOMAINS: ClassVar[bool] = True

    def __init__(self, config: WebSearchProviderConfig) -> None:
        super().__init__(config)
        self._base_url = (config.base_url or EXA_DEFAULT_BASE).rstrip("/")
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
        category = (
            "news"
            if request_options is not None and request_options.topic == "news"
            else options.get("EXA_CATEGORY", "")
        )
        category_restricted = category in _CATEGORIES_WITHOUT_DATE_OR_EXCLUDE
        contents_payload = (
            self._contents_payload()
            if request_options is None
            else self._contents_payload_options(request_options)
        )
        payload: dict[str, Any] = {
            "query": query,
            "numResults": max_results,
            "contents": contents_payload,
        }
        search_type = (
            DEPTH_TO_EXA.get(request_options.search_depth, "")
            if request_options is not None and request_options.search_depth is not None
            else options.get("EXA_SEARCH_TYPE", "")
        )
        if search_type:
            payload["type"] = search_type
        if category:
            payload["category"] = category
        if not category_restricted:
            start = (
                (
                    request_options.start_date
                    if request_options is not None
                    and request_options.start_date is not None
                    else None
                )
                or (
                    time_range_start_iso(request_options.time_range)
                    if request_options is not None
                    and request_options.time_range is not None
                    else None
                )
                or options.get("EXA_START_PUBLISHED_DATE", "")
            )
            if start:
                payload["startPublishedDate"] = start
            end = (
                request_options.end_date
                if request_options is not None and request_options.end_date is not None
                else ""
            ) or options.get("EXA_END_PUBLISHED_DATE", "")
            if end:
                payload["endPublishedDate"] = end
        location = (
            request_options.country.upper()
            if request_options is not None and request_options.country is not None
            else ""
        ) or options.get("EXA_USER_LOCATION", "")
        if location:
            payload["userLocation"] = location
        if allowed_domains:
            payload["includeDomains"] = list(allowed_domains)
        if blocked_domains and not category_restricted:
            payload["excludeDomains"] = list(blocked_domains)
        data = await request_json(
            self._require_client(),
            self.provider_id,
            "POST",
            f"{self._base_url}/search",
            headers={"x-api-key": key},
            json_body=payload,
        )
        rows = data.get("results", []) if isinstance(data, dict) else []
        items = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            url = text_str(row.get("url"))
            if not url:
                continue
            highlights = row.get("highlights")
            snippet = (
                " … ".join(part for part in highlights if isinstance(part, str))
                if isinstance(highlights, list)
                else ""
            )
            text = text_str(row.get("text"))
            summary = text_str(row.get("summary"))
            items.append(
                WebSearchResultItem(
                    title=text_str(row.get("title")),
                    url=url,
                    snippet=(snippet or summary or text)[:_SNIPPET_CHARS],
                    content=(text or summary) or None,
                    published=text_str(row.get("publishedDate")) or None,
                    score=opt_float(row.get("score")),
                    author=opt_str(row.get("author")),
                    favicon=opt_str(row.get("favicon")),
                    highlights=opt_str_tuple(highlights),
                    highlight_scores=opt_float_tuple(row.get("highlightScores")),
                )
            )
        cost = (
            data.get("costDollars", {}).get("total") if isinstance(data, dict) else None
        )
        return WebSearchResponse(
            provider=self.provider_id,
            query=query,
            results=tuple(items[:max_results]),
            key_index=key_index,
            cost_usd=float(cost) if isinstance(cost, int | float) else None,
            request_id=opt_str(data.get("requestId"))
            if isinstance(data, dict)
            else None,
            provider_response_time_ms=opt_float(data.get("searchTime"))
            if isinstance(data, dict)
            else None,
        )

    def _contents_payload(self) -> dict[str, Any]:
        return self._contents_payload_options(None)

    def _contents_payload_options(
        self, request_options: WebSearchRequestOptions | None = None
    ) -> dict[str, Any]:
        options = self._config.options
        mode = options.get("EXA_CONTENTS", "")
        contents: dict[str, Any] = dict(_CONTENTS_MODES.get(mode, {"highlights": True}))
        if request_options is not None:
            if (
                request_options.include_content is True
                or request_options.raw_content_format is not None
            ):
                contents["text"] = True
            elif request_options.include_content is False:
                contents.pop("text", None)
        if (max_age := option_int(options.get("EXA_MAX_AGE_HOURS"))) is not None:
            contents["maxAgeHours"] = max_age
        return contents
