"""Context7 adapter: library documentation as search results (7.113.0).

The HTTP API here was read from Context7's own two clients, never from
memory: ``@upstash/context7-mcp`` 4.4.4 (``dist/lib/api.js``,
``dist/lib/constants.js``, ``dist/lib/encryption.js``) and
``@upstash/context7-sdk`` 0.6.0 (``dist/client.js``, ``dist/client.d.ts``),
both packed from the npm registry on 2026-10-24.

One search is two GETs, each with ``Authorization: Bearer <key>`` (Context7
keys start with ``ctx7sk``):

1. ``GET {base}/v2/libs/search?query=<q>&libraryName=<q>`` resolves the query
   to a library: ``{"results": [{"id", "title", "description", ...}]}``,
   best match first. No match is an empty list, and the search returns no
   results without a second call.
2. ``GET {base}/v2/context?query=<q>&libraryId=<id>&type=json`` returns that
   library's documentation for the query: ``{"codeSnippets": [{"codeTitle",
   "codeDescription", "codeList": [{"language", "code"}], "codeId", ...}],
   "infoSnippets": [{"content", "breadcrumb", "pageId", ...}]}``.

Each snippet becomes one result, code snippets first (the SDK's own order):
``title`` = the library's title + the snippet's section, ``url`` = the source
Context7 returns (``codeId`` / ``pageId``), ``snippet`` = the excerpt -- the
description, then the code in fenced blocks, as the SDK formats it -- cut at
``_SNIPPET_CHARS`` with a visible marker; ``content`` = the same excerpt cut
at the larger ``_CONTENT_CHARS``, so the digest's page-text cap applies.

Failures go through the shared HTTP mapping (``http.py``) unchanged: 401/403
rejected key, 402 credits, 429 rate limit with the provider's ``Retry-After``,
400/404/405/422 refused query (Context7 answers 404 for an unknown library),
anything else upstream. A body that is not JSON, or JSON in another shape
than the two above, is an unreadable answer (``KIND_DETAIL_BAD_JSON``), the
same as any adapter's.
"""

from typing import Any, ClassVar

from my_claude_code.config.websearch_catalog import CONTEXT7_DEFAULT_BASE
from my_claude_code.core.websearch.models import (
    WebSearchResponse,
    WebSearchResultItem,
)

from ..base import BaseWebSearchProvider, WebSearchProviderConfig
from ..errors import KIND_DETAIL_BAD_JSON, WebSearchError, WebSearchUpstreamError
from .http import build_async_client, extract_error_message, request_json

#: The excerpt a result carries as its snippet, and the fuller form the
#: digest reads when it renders page text. Both are cut with the marker below
#: so a reader can tell a cut excerpt from a short one.
_SNIPPET_CHARS = 1000
_CONTENT_CHARS = 8000
TRUNCATION_MARKER = " …[excerpt truncated]"
#: The section name Context7's SDK gives a documentation snippet with no
#: breadcrumb (``formatInfoSnippet``).
_DEFAULT_SECTION = "Documentation"


class Context7WebSearchProvider(BaseWebSearchProvider):
    PROVIDER_ID: ClassVar[str] = "context7"
    SUPPORTS_DOMAINS: ClassVar[bool] = False

    def __init__(self, config: WebSearchProviderConfig) -> None:
        super().__init__(config)
        self._base_url = (config.base_url or CONTEXT7_DEFAULT_BASE).rstrip("/")
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
        headers = {"Authorization": f"Bearer {key}"}
        found = await self._get(
            "/v2/libs/search", headers, {"query": query, "libraryName": query}
        )
        library = self._first_library(found)
        items: list[WebSearchResultItem] = []
        if library is not None:
            library_id, library_title = library
            docs = await self._get(
                "/v2/context",
                headers,
                {"query": query, "libraryId": library_id, "type": "json"},
            )
            items = self._results(docs, library_title or library_id)
        return WebSearchResponse(
            provider=self.provider_id,
            query=query,
            results=tuple(items[:max_results]),
            key_index=key_index,
            cost_usd=None,
        )

    async def _get(
        self, path: str, headers: dict[str, str], params: dict[str, str]
    ) -> Any:
        """One GET through the shared JSON helper and its status mapping.

        Context7's error body is ``{"error": <code>, "message": <sentence>}``
        and both its clients show the sentence; the shared helper reads
        ``error`` first, so the sentence is put back. The error's kind and
        status are the helper's, untouched.
        """

        try:
            return await request_json(
                self._require_client(),
                self.provider_id,
                "GET",
                f"{self._base_url}{path}",
                headers=headers,
                params=params,
            )
        except WebSearchError as error:
            body = error.upstream_body
            stated = body.get("message") if isinstance(body, dict) else None
            if isinstance(stated, str) and stated.strip():
                error.message = extract_error_message(stated)
            raise

    def _first_library(self, data: Any) -> tuple[str, str] | None:
        """The best-matching library's ``(id, title)``; None when none matched."""

        rows = data.get("results") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise self._unreadable("the library search answer has no results list")
        if not rows:
            return None
        first = rows[0]
        library_id = first.get("id") if isinstance(first, dict) else None
        if not isinstance(library_id, str) or not library_id.strip():
            raise self._unreadable("the best library match carries no id")
        return library_id, _text(first.get("title"))

    def _results(self, data: Any, library: str) -> list[WebSearchResultItem]:
        if not isinstance(data, dict):
            raise self._unreadable("the documentation answer is not an object")
        if "codeSnippets" not in data and "infoSnippets" not in data:
            raise self._unreadable("the documentation answer has no snippets")
        code = data.get("codeSnippets", [])
        info = data.get("infoSnippets", [])
        if not isinstance(code, list) or not isinstance(info, list):
            raise self._unreadable("the documentation answer's snippets are not lists")
        items: list[WebSearchResultItem] = []
        for row in code:
            if not isinstance(row, dict):
                continue
            section = _text(row.get("codeTitle")) or _text(row.get("pageTitle"))
            items.extend(
                _item(library, section, _text(row.get("codeId")), _code_excerpt(row))
            )
        for row in info:
            if not isinstance(row, dict):
                continue
            section = _text(row.get("breadcrumb")) or _DEFAULT_SECTION
            items.extend(
                _item(
                    library,
                    section,
                    _text(row.get("pageId")),
                    _text(row.get("content")),
                )
            )
        return items

    def _unreadable(self, what: str) -> WebSearchUpstreamError:
        return WebSearchUpstreamError(
            self.provider_id,
            f"response body is not in Context7's documented shape: {what}",
            kind_detail=KIND_DETAIL_BAD_JSON,
        )


def _item(
    library: str, section: str, source: str, excerpt: str
) -> list[WebSearchResultItem]:
    """Zero or one result: a snippet with no source cannot be cited."""

    if not source:
        return []
    title = f"{library} — {section}" if section else library
    return [
        WebSearchResultItem(
            title=title,
            url=source,
            snippet=bounded_excerpt(excerpt, _SNIPPET_CHARS),
            content=bounded_excerpt(excerpt, _CONTENT_CHARS) or None,
            published=None,
        )
    ]


def _code_excerpt(row: dict[str, Any]) -> str:
    """The description, then each code block fenced (the SDK's own format)."""

    blocks = []
    listed = row.get("codeList")
    for entry in listed if isinstance(listed, list) else []:
        if not isinstance(entry, dict):
            continue
        code = _text(entry.get("code"))
        if code:
            blocks.append(f"```{_text(entry.get('language'))}\n{code}\n```")
    fenced = "\n\n".join(blocks)
    description = _text(row.get("codeDescription"))
    if description and fenced:
        return f"{description}\n\n{fenced}"
    return description or fenced


def bounded_excerpt(text: str, limit: int) -> str:
    """``text`` cut to ``limit`` characters, the marker included, when longer."""

    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(TRUNCATION_MARKER))] + TRUNCATION_MARKER


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""
