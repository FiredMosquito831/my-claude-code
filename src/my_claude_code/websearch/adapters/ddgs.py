"""DuckDuckGo adapter via the keyless ``ddgs`` metasearch package.

The package is synchronous, so calls run in a worker thread with a fresh
``DDGS()`` instance per call (per upstream guidance).
"""

import asyncio
from typing import Any, ClassVar

from ddgs import DDGS
from ddgs.exceptions import DDGSException, RatelimitException, TimeoutException

from my_claude_code.core.websearch.models import (
    WebSearchResponse,
    WebSearchResultItem,
)

from ..base import BaseWebSearchProvider
from ..errors import (
    KIND_DETAIL_TIMEOUT,
    WebSearchRateLimitError,
    WebSearchUpstreamError,
)


class DdgsWebSearchProvider(BaseWebSearchProvider):
    """Keyless metasearch over public engines (DuckDuckGo/Bing/Brave/...)."""

    PROVIDER_ID: ClassVar[str] = "ddgs"
    SUPPORTS_DOMAINS: ClassVar[bool] = False

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
        try:
            rows = await asyncio.to_thread(self._run_text_search, query, max_results)
        except RatelimitException as exc:
            raise WebSearchRateLimitError(
                self.provider_id, f"ddgs engine rate limit: {exc}"
            ) from exc
        except TimeoutException as exc:
            raise WebSearchUpstreamError(
                self.provider_id,
                f"ddgs search timed out: {exc}",
                kind_detail=KIND_DETAIL_TIMEOUT,
            ) from exc
        except DDGSException as exc:
            raise WebSearchUpstreamError(
                self.provider_id, f"ddgs search failed: {exc}"
            ) from exc
        items = tuple(
            WebSearchResultItem(
                title=_text(row.get("title")),
                url=_text(row.get("href")),
                snippet=_text(row.get("body")),
                content=None,
                published=None,
            )
            for row in rows
            if isinstance(row, dict) and _text(row.get("href"))
        )
        return WebSearchResponse(
            provider=self.provider_id,
            query=query,
            results=items,
            key_index=key_index,
            cost_usd=None,
        )

    def _run_text_search(self, query: str, max_results: int) -> list[dict[str, Any]]:
        options = self._config.options
        kwargs: dict[str, Any] = {"max_results": max_results}
        if backend := options.get("DDGS_BACKEND", ""):
            kwargs["backend"] = backend
        if region := options.get("DDGS_REGION", ""):
            kwargs["region"] = region
        if timelimit := options.get("DDGS_TIMELIMIT", ""):
            kwargs["timelimit"] = timelimit
        if safesearch := options.get("DDGS_SAFESEARCH", ""):
            kwargs["safesearch"] = safesearch
        return DDGS(
            proxy=self._config.proxy, timeout=int(self._config.http_timeout)
        ).text(query, **kwargs)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""
