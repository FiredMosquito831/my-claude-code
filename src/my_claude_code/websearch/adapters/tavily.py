"""Tavily adapter (POST api.tavily.com/search with Bearer auth).

``search_depth`` defaults to ``basic`` so ``auto_parameters`` cannot silently
upgrade cost; HTTP 432 (plan usage limit) maps to a quota error. Advanced
dotenv options: depth/topic/time_range, ``TAVILY_INCLUDE_ANSWER`` (LLM answer
-> response.answer) and ``TAVILY_INCLUDE_RAW_CONTENT`` (-> item.content).
``auto_parameters`` is never enabled.
"""

from typing import Any, ClassVar

from my_claude_code.config.websearch_catalog import TAVILY_DEFAULT_BASE
from my_claude_code.core.websearch.models import (
    WebSearchRequestOptions,
    WebSearchResponse,
    WebSearchResultItem,
)

from ..base import BaseWebSearchProvider, WebSearchProviderConfig
from ..errors import WebSearchError, WebSearchQuotaError
from ..options import option_int
from ._common import (
    DEPTH_TO_TAVILY,
    opt_float,
    opt_images,
    opt_seconds_to_ms,
    opt_str,
    text_str,
)
from .http import build_async_client, request_json

_EXTRA_STATUS_ERRORS: dict[int, type[WebSearchError]] = {432: WebSearchQuotaError}
# Tavily documents max_results as 0-20; sending more is rejected upstream.
_MAX_RESULTS_CAP = 20


class TavilyWebSearchProvider(BaseWebSearchProvider):
    PROVIDER_ID: ClassVar[str] = "tavily"
    SUPPORTS_DOMAINS: ClassVar[bool] = True

    def __init__(self, config: WebSearchProviderConfig) -> None:
        super().__init__(config)
        self._base_url = (config.base_url or TAVILY_DEFAULT_BASE).rstrip("/")
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
        search_depth = (
            DEPTH_TO_TAVILY.get(request_options.search_depth, "")
            if request_options is not None and request_options.search_depth is not None
            else ""
        ) or (options.get("TAVILY_SEARCH_DEPTH", "") or "basic")
        payload: dict[str, Any] = {
            "query": query,
            "max_results": min(max_results, _MAX_RESULTS_CAP),
            "search_depth": search_depth,
        }
        topic = (
            request_options.topic
            if request_options is not None and request_options.topic is not None
            else ""
        ) or options.get("TAVILY_TOPIC", "")
        if topic:
            payload["topic"] = topic
        time_range = (
            request_options.time_range
            if request_options is not None and request_options.time_range is not None
            else ""
        ) or options.get("TAVILY_TIME_RANGE", "")
        if time_range:
            payload["time_range"] = time_range
        if request_options is not None and request_options.include_answer is not None:
            payload["include_answer"] = request_options.include_answer
        elif include_answer := options.get("TAVILY_INCLUDE_ANSWER", ""):
            payload["include_answer"] = include_answer
        if request_options is not None and (
            request_options.raw_content_format is not None
            or request_options.include_content is not None
        ):
            if request_options.raw_content_format is not None:
                payload["include_raw_content"] = request_options.raw_content_format
            elif request_options.include_content is True:
                payload["include_raw_content"] = "markdown"
            else:
                payload["include_raw_content"] = False
        elif raw_content := options.get("TAVILY_INCLUDE_RAW_CONTENT", ""):
            payload["include_raw_content"] = raw_content
        if (chunks := option_int(options.get("TAVILY_CHUNKS_PER_SOURCE"))) is not None:
            payload["chunks_per_source"] = chunks
        country = (
            request_options.country
            if request_options is not None and request_options.country is not None
            else ""
        ) or options.get("TAVILY_COUNTRY", "")
        if country:
            payload["country"] = country
        start_date = (
            request_options.start_date[:10]
            if request_options is not None and request_options.start_date is not None
            else ""
        ) or options.get("TAVILY_START_DATE", "")
        if start_date:
            payload["start_date"] = start_date
        end_date = (
            request_options.end_date[:10]
            if request_options is not None and request_options.end_date is not None
            else ""
        ) or options.get("TAVILY_END_DATE", "")
        if end_date:
            payload["end_date"] = end_date
        if allowed_domains:
            payload["include_domains"] = list(allowed_domains)
        if blocked_domains:
            payload["exclude_domains"] = list(blocked_domains)
        data = await request_json(
            self._require_client(),
            self.provider_id,
            "POST",
            f"{self._base_url}/search",
            headers={"Authorization": f"Bearer {key}"},
            json_body=payload,
            extra_status_errors=_EXTRA_STATUS_ERRORS,
        )
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
                    content=text_str(row.get("raw_content")) or None,
                    published=None,
                    score=opt_float(row.get("score")),
                    favicon=opt_str(row.get("favicon")),
                )
            )
        answer = text_str(data.get("answer")) if isinstance(data, dict) else ""
        usage = data.get("usage") if isinstance(data, dict) else None
        return WebSearchResponse(
            provider=self.provider_id,
            query=query,
            results=tuple(items[:max_results]),
            key_index=key_index,
            cost_usd=None,
            answer=answer or None,
            request_id=opt_str(data.get("request_id"))
            if isinstance(data, dict)
            else None,
            provider_response_time_ms=opt_seconds_to_ms(data.get("response_time"))
            if isinstance(data, dict)
            else None,
            usage_credits=opt_float(usage.get("credits"))
            if isinstance(usage, dict)
            else None,
            images=opt_images(data.get("images")) if isinstance(data, dict) else None,
        )
