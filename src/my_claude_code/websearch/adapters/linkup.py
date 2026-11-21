"""Linkup adapter (POST api.linkup.so/v1/search with Bearer auth).

Result titles live in the ``name`` field (not ``title``). Advanced dotenv
options: ``LINKUP_DEPTH`` (deep = 10x cost) and ``LINKUP_OUTPUT_TYPE``;
``sourcedAnswer`` returns an LLM ``answer`` plus ``sources[]`` (mapped to
result items, answer -> response.answer).
"""

from typing import Any, ClassVar

from my_claude_code.config.websearch_catalog import LINKUP_DEFAULT_BASE
from my_claude_code.core.websearch.models import (
    WebSearchRequestOptions,
    WebSearchResponse,
    WebSearchResultItem,
)

from ..base import BaseWebSearchProvider, WebSearchProviderConfig
from ._common import (
    DEPTH_TO_LINKUP,
    dict_list,
    opt_str,
    text_str,
    time_range_start_iso,
)
from .http import build_async_client, request_json

_SNIPPET_CHARS = 1000


class LinkupWebSearchProvider(BaseWebSearchProvider):
    PROVIDER_ID: ClassVar[str] = "linkup"
    SUPPORTS_DOMAINS: ClassVar[bool] = True

    def __init__(self, config: WebSearchProviderConfig) -> None:
        super().__init__(config)
        self._base_url = (config.base_url or LINKUP_DEFAULT_BASE).rstrip("/")
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
        if request_options is not None and request_options.include_answer is not None:
            output_type = (
                "sourcedAnswer" if request_options.include_answer else "searchResults"
            )
        else:
            output_type = options.get("LINKUP_OUTPUT_TYPE", "") or "searchResults"
        depth = (
            DEPTH_TO_LINKUP.get(request_options.search_depth, "")
            if request_options is not None and request_options.search_depth is not None
            else ""
        ) or (options.get("LINKUP_DEPTH", "") or "standard")
        payload: dict[str, Any] = {
            "q": query,
            "depth": depth,
            "outputType": output_type,
            # Without this the API returns its own default set and we pay for
            # results only to discard them in the client-side slice below.
            "maxResults": max_results,
        }
        from_date = (
            (
                request_options.start_date[:10]
                if request_options is not None
                and request_options.start_date is not None
                else ""
            )
            or (
                time_range_start_iso(request_options.time_range)
                if request_options is not None
                and request_options.time_range is not None
                else ""
            )
            or options.get("LINKUP_FROM_DATE", "")
        )
        if from_date:
            payload["fromDate"] = from_date
        to_date = (
            request_options.end_date[:10]
            if request_options is not None and request_options.end_date is not None
            else ""
        ) or options.get("LINKUP_TO_DATE", "")
        if to_date:
            payload["toDate"] = to_date
        if allowed_domains:
            payload["includeDomains"] = list(allowed_domains)
        if blocked_domains:
            payload["excludeDomains"] = list(blocked_domains)
        data = await request_json(
            self._require_client(),
            self.provider_id,
            "POST",
            f"{self._base_url}/v1/search",
            headers={"Authorization": f"Bearer {key}"},
            json_body=payload,
        )
        answer = ""
        if isinstance(data, dict) and output_type == "sourcedAnswer":
            answer = text_str(data.get("answer"))
            rows = dict_list(data, "sources")
            snippet_field = "snippet"
        else:
            rows = dict_list(data, "results")
            snippet_field = "content"
        items = []
        for row in rows:
            url = text_str(row.get("url"))
            if not url:
                continue
            text = text_str(row.get(snippet_field))
            items.append(
                WebSearchResultItem(
                    title=text_str(row.get("name")),
                    url=url,
                    snippet=text[:_SNIPPET_CHARS],
                    # searchResults returns the full extracted page text in
                    # ``content``; keep it so the digest can use the fuller
                    # form rather than only the truncated snippet.
                    content=text if snippet_field == "content" and text else None,
                    published=None,
                    favicon=opt_str(row.get("favicon")),
                )
            )
        return WebSearchResponse(
            provider=self.provider_id,
            query=query,
            results=tuple(items[:max_results]),
            key_index=key_index,
            cost_usd=None,
            answer=answer or None,
        )
