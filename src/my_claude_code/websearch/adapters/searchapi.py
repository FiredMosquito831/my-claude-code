"""SearchAPI.io adapter (GET www.searchapi.io/api/v1/search, api_key query param).

Result URLs live in the ``link`` field; error payloads can arrive with HTTP 200,
so the body is checked for a top-level ``error``. Advanced dotenv options:
``SEARCHAPI_ENGINE``/``SEARCHAPI_TIME_PERIOD``/``SEARCHAPI_GL``/``SEARCHAPI_HL``
request params; ``answer_box``/``knowledge_graph`` are captured into
``response.answer`` when present.
"""

from typing import Any, ClassVar

from my_claude_code.config.websearch_catalog import SEARCHAPI_DEFAULT_BASE
from my_claude_code.core.websearch.models import (
    WebSearchRequestOptions,
    WebSearchResponse,
    WebSearchResultItem,
)

from ..base import BaseWebSearchProvider, WebSearchProviderConfig
from ._common import (
    TIME_RANGE_TO_SEARCHAPI,
    classify_json_error,
    dict_list,
    opt_nested_float,
    opt_seconds_to_ms,
    opt_str,
    text_str,
)
from .http import build_async_client, request_json


class SearchApiWebSearchProvider(BaseWebSearchProvider):
    PROVIDER_ID: ClassVar[str] = "searchapi"
    SUPPORTS_DOMAINS: ClassVar[bool] = False

    def __init__(self, config: WebSearchProviderConfig) -> None:
        super().__init__(config)
        self._base_url = (config.base_url or SEARCHAPI_DEFAULT_BASE).rstrip("/")
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
        params: dict[str, Any] = {
            "engine": options.get("SEARCHAPI_ENGINE", "") or "google",
            "q": query,
            "api_key": key,
            "num": max_results,
        }
        time_period = (
            TIME_RANGE_TO_SEARCHAPI.get(request_options.time_range, "")
            if request_options is not None and request_options.time_range is not None
            else ""
        ) or options.get("SEARCHAPI_TIME_PERIOD", "")
        if time_period:
            params["time_period"] = time_period
        gl = (
            request_options.country.lower()
            if request_options is not None and request_options.country is not None
            else ""
        ) or options.get("SEARCHAPI_GL", "")
        if gl:
            params["gl"] = gl
        hl = (
            request_options.language.lower()
            if request_options is not None and request_options.language is not None
            else ""
        ) or options.get("SEARCHAPI_HL", "")
        if hl:
            params["hl"] = hl
        if safe := options.get("SEARCHAPI_SAFE", ""):
            params["safe"] = safe
        data = await request_json(
            self._require_client(),
            self.provider_id,
            "GET",
            f"{self._base_url}/api/v1/search",
            params=params,
        )
        if isinstance(data, dict) and data.get("error"):
            raise classify_json_error(
                self.provider_id,
                data["error"],
                status_code=200,
                upstream_body={"error": data["error"]},
            )
        rows = dict_list(data, "organic_results")
        items = []
        for row in rows:
            url = text_str(row.get("link"))
            if not url:
                continue
            items.append(
                WebSearchResultItem(
                    title=text_str(row.get("title")),
                    url=url,
                    snippet=text_str(row.get("snippet")),
                    content=None,
                    published=text_str(row.get("date")) or None,
                    favicon=opt_str(row.get("favicon")),
                    site_name=opt_str(row.get("source")),
                )
            )
        include_answer = (
            request_options.include_answer
            if request_options is not None
            and request_options.include_answer is not None
            else True
        )
        answer = _answer_lead(data) if isinstance(data, dict) and include_answer else ""
        meta = data.get("search_metadata") if isinstance(data, dict) else None
        return WebSearchResponse(
            provider=self.provider_id,
            query=query,
            results=tuple(items[:max_results]),
            key_index=key_index,
            cost_usd=None,
            answer=answer or None,
            request_id=opt_str(meta.get("id")) if isinstance(meta, dict) else None,
            provider_response_time_ms=opt_seconds_to_ms(
                opt_nested_float(data, "search_metadata", "total_time_taken")
            ),
        )


def _answer_lead(data: dict[str, Any]) -> str:
    """answer_box lead, then knowledge_graph description as context."""

    parts: list[str] = []
    answer_box = data.get("answer_box")
    if isinstance(answer_box, dict):
        text = text_str(answer_box.get("answer")) or text_str(answer_box.get("snippet"))
        if text:
            parts.append(text)
    knowledge_graph = data.get("knowledge_graph")
    if isinstance(knowledge_graph, dict):
        title = text_str(knowledge_graph.get("title"))
        description = text_str(knowledge_graph.get("description"))
        if title and description:
            parts.append(f"{title}: {description}")
        elif title or description:
            parts.append(title or description)
    return "\n\n".join(parts)
