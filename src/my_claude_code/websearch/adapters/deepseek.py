"""DeepSeek standalone Web Search adapter (POST api.deepseek.com/v1/tools/search).

Authenticates via Bearer token using dedicated ``DEEPSEEK_SEARCH_API_KEY`` by
default (optional ``DEEPSEEK_API_KEY`` fallback only when
``DEEPSEEK_SEARCH_USE_INFERENCE_KEY=true`` in :mod:`..registry`).

Never auto-selected (``auto_select=False`` in ``websearch_catalog``): each
successful call bills ``$0.00075`` (``$0.75`` per 1,000 calls; no free tier).
"""

from typing import Any, ClassVar

from my_claude_code.config.websearch_catalog import DEEPSEEK_SEARCH_DEFAULT_BASE
from my_claude_code.core.websearch.models import (
    WebSearchRequestOptions,
    WebSearchResponse,
    WebSearchResultItem,
)

from ..base import BaseWebSearchProvider, WebSearchProviderConfig
from ..options import option_enabled
from ._common import opt_float, opt_str, text_str
from .http import build_async_client, request_json

_DEEPSEEK_SEARCH_COST_USD = 0.00075
_MAX_RESULTS_CAP = 50


class DeepSeekWebSearchProvider(BaseWebSearchProvider):
    """Adapter for DeepSeek's standalone ``POST /v1/tools/search`` endpoint."""

    PROVIDER_ID: ClassVar[str] = "deepseek"
    SUPPORTS_DOMAINS: ClassVar[bool] = False

    def __init__(self, config: WebSearchProviderConfig) -> None:
        super().__init__(config)
        self._base_url = (config.base_url or DEEPSEEK_SEARCH_DEFAULT_BASE).rstrip("/")
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
        if request_options is not None and request_options.include_content is not None:
            need_content = bool(request_options.include_content)
        else:
            need_content = option_enabled(options.get("DEEPSEEK_SEARCH_NEED_CONTENT"))
        payload: dict[str, Any] = {
            "query": query,
            "count": min(max_results, _MAX_RESULTS_CAP),
            "need_content": need_content,
        }
        # DeepSeek localization is an IPv4/IPv6 string, never a 2-letter country code.
        raw_localization = opt_str(options.get("DEEPSEEK_SEARCH_LOCALIZATION"))
        localization = raw_localization.strip() if raw_localization is not None else ""
        if localization:
            payload["localization"] = localization
        data = await request_json(
            self._require_client(),
            self.provider_id,
            "POST",
            f"{self._base_url}/v1/tools/search",
            headers={"Authorization": f"Bearer {key}"},
            json_body=payload,
        )
        rows = data.get("results", []) if isinstance(data, dict) else []
        items: list[WebSearchResultItem] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            url = text_str(row.get("url"))
            if not url:
                continue
            snippet = opt_str(row.get("summary")) or text_str(row.get("snippet"))
            content = opt_str(row.get("content")) if need_content else None
            items.append(
                WebSearchResultItem(
                    title=text_str(row.get("title")),
                    url=url,
                    snippet=snippet,
                    content=content,
                    published=opt_str(row.get("date")),
                    score=opt_float(row.get("rerank_score")),
                    favicon=opt_str(row.get("site_icon")),
                    site_name=opt_str(row.get("site_name")),
                )
            )
        return WebSearchResponse(
            provider=self.provider_id,
            query=query,
            results=tuple(items[:max_results]),
            key_index=key_index,
            cost_usd=_DEEPSEEK_SEARCH_COST_USD,
        )
