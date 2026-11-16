"""Parallel Search adapter (POST api.parallel.ai/v1/search).

Snippets are the provider's token-compressed ``excerpts`` joined per result.
v1 nests source/excerpt tuning under ``advanced_settings`` and takes
``mode``/``max_chars_total`` at the top level; the older ``/v1beta/search``
used a different ``mode`` vocabulary (``fast``/``one-shot``/``agentic``) that
did not match the values this catalog exposes.
"""

from typing import Any, ClassVar

from my_claude_code.config.websearch_catalog import PARALLEL_DEFAULT_BASE
from my_claude_code.core.websearch.models import (
    WebSearchRequestOptions,
    WebSearchResponse,
    WebSearchResultItem,
)

from ..base import BaseWebSearchProvider, WebSearchProviderConfig
from ..options import option_int
from ._common import (
    DEPTH_TO_PARALLEL,
    opt_str,
    opt_str_tuple,
    text_str,
)
from .http import build_async_client, request_json

_SNIPPET_CHARS = 1000


class ParallelWebSearchProvider(BaseWebSearchProvider):
    PROVIDER_ID: ClassVar[str] = "parallel"
    SUPPORTS_DOMAINS: ClassVar[bool] = True

    def __init__(self, config: WebSearchProviderConfig) -> None:
        super().__init__(config)
        self._base_url = (config.base_url or PARALLEL_DEFAULT_BASE).rstrip("/")
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
        payload: dict[str, Any] = {
            "objective": query,
            "search_queries": [query],
        }
        mode = (
            DEPTH_TO_PARALLEL.get(request_options.search_depth, "")
            if request_options is not None and request_options.search_depth is not None
            else ""
        ) or options.get("PARALLEL_MODE", "")
        if mode:
            payload["mode"] = mode
        if (total_chars := option_int(options.get("PARALLEL_TOTAL_CHARS"))) is not None:
            payload["max_chars_total"] = total_chars
        advanced: dict[str, Any] = {"max_results": max_results}
        if (
            excerpt_chars := option_int(options.get("PARALLEL_EXCERPT_CHARS"))
        ) is not None:
            advanced["excerpt_settings"] = {"max_chars_per_result": excerpt_chars}
        source_policy: dict[str, Any] = {}
        if allowed_domains:
            source_policy["include_domains"] = list(allowed_domains)
        if blocked_domains:
            source_policy["exclude_domains"] = list(blocked_domains)
        if source_policy:
            advanced["source_policy"] = source_policy
        location = (
            request_options.country.upper()
            if request_options is not None and request_options.country is not None
            else ""
        ) or options.get("PARALLEL_LOCATION", "")
        if location:
            advanced["location"] = location
        payload["advanced_settings"] = advanced
        data = await request_json(
            self._require_client(),
            self.provider_id,
            "POST",
            f"{self._base_url}/v1/search",
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
            excerpts = row.get("excerpts")
            content = (
                "\n\n".join(part for part in excerpts if isinstance(part, str))
                if isinstance(excerpts, list)
                else ""
            )
            items.append(
                WebSearchResultItem(
                    title=text_str(row.get("title")),
                    url=url,
                    snippet=content[:_SNIPPET_CHARS],
                    content=content or None,
                    published=text_str(row.get("publish_date")) or None,
                    highlights=opt_str_tuple(excerpts),
                )
            )
        request_id = (
            (opt_str(data.get("search_id")) or opt_str(data.get("session_id")))
            if isinstance(data, dict)
            else None
        )
        return WebSearchResponse(
            provider=self.provider_id,
            query=query,
            results=tuple(items[:max_results]),
            key_index=key_index,
            cost_usd=None,
            request_id=request_id,
        )
