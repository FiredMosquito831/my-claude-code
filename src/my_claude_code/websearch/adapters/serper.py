"""Serper adapter (POST google.serper.dev/search with X-API-KEY auth).

Result URLs live in the ``link`` field (not ``url``). Advanced dotenv options:
``SERPER_GL``/``SERPER_HL``/``SERPER_TBS`` request params and
``SERPER_RICH_BLOCKS`` (default on): answerBox -> response.answer with
knowledgeGraph and peopleAlsoAsk appended as context.
"""

from typing import Any, ClassVar

from my_claude_code.config.websearch_catalog import SERPER_DEFAULT_BASE
from my_claude_code.core.websearch.models import (
    WebSearchRequestOptions,
    WebSearchResponse,
    WebSearchResultItem,
)

from ..base import BaseWebSearchProvider, WebSearchProviderConfig
from ..options import option_enabled
from ._common import TIME_RANGE_TO_TBS, dict_list, opt_float, text_str
from .http import build_async_client, request_json


class SerperWebSearchProvider(BaseWebSearchProvider):
    PROVIDER_ID: ClassVar[str] = "serper"
    SUPPORTS_DOMAINS: ClassVar[bool] = False

    def __init__(self, config: WebSearchProviderConfig) -> None:
        super().__init__(config)
        self._base_url = (config.base_url or SERPER_DEFAULT_BASE).rstrip("/")
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
        payload: dict[str, Any] = {"q": query, "num": max_results}
        gl = (
            request_options.country.lower()
            if request_options is not None and request_options.country is not None
            else ""
        ) or options.get("SERPER_GL", "")
        if gl:
            payload["gl"] = gl
        hl = (
            request_options.language.lower()
            if request_options is not None and request_options.language is not None
            else ""
        ) or options.get("SERPER_HL", "")
        if hl:
            payload["hl"] = hl
        tbs = (
            TIME_RANGE_TO_TBS.get(request_options.time_range, "")
            if request_options is not None and request_options.time_range is not None
            else ""
        ) or options.get("SERPER_TBS", "")
        if tbs:
            payload["tbs"] = tbs
        data = await request_json(
            self._require_client(),
            self.provider_id,
            "POST",
            f"{self._base_url}/search",
            headers={"X-API-KEY": key},
            json_body=payload,
        )
        rows = dict_list(data, "organic")
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
                )
            )
        rich_enabled = (
            request_options.include_answer
            if request_options is not None
            and request_options.include_answer is not None
            else option_enabled(options.get("SERPER_RICH_BLOCKS"), default=True)
        )
        answer = (
            _rich_blocks_answer(data) if isinstance(data, dict) and rich_enabled else ""
        )
        return WebSearchResponse(
            provider=self.provider_id,
            query=query,
            results=tuple(items[:max_results]),
            key_index=key_index,
            cost_usd=None,
            answer=answer or None,
            usage_credits=opt_float(data.get("credits"))
            if isinstance(data, dict)
            else None,
        )


def _rich_blocks_answer(data: dict[str, Any]) -> str:
    """answerBox lead, then knowledgeGraph + peopleAlsoAsk context."""

    parts: list[str] = []
    answer_box = data.get("answerBox")
    if isinstance(answer_box, dict):
        text = text_str(answer_box.get("answer")) or text_str(answer_box.get("snippet"))
        if text:
            parts.append(text)
    knowledge_graph = data.get("knowledgeGraph")
    if isinstance(knowledge_graph, dict):
        title = text_str(knowledge_graph.get("title"))
        description = text_str(knowledge_graph.get("description"))
        if title and description:
            parts.append(f"{title}: {description}")
        elif title or description:
            parts.append(title or description)
    for row in dict_list(data, "peopleAlsoAsk"):
        question = text_str(row.get("question"))
        snippet = text_str(row.get("snippet"))
        if question and snippet:
            parts.append(f"Q: {question}\nA: {snippet}")
        elif question:
            parts.append(f"Q: {question}")
    return "\n\n".join(parts)
