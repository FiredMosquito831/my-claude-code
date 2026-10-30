"""DuckDuckGo adapter via the keyless ``ddgs`` metasearch package.

The package is synchronous, so calls run in a worker thread with a fresh
``DDGS()`` instance per call (per upstream guidance).

DuckDuckGo's own engine answers with its redirect link rather than the
destination (RESEARCH-DDG-WEBSEARCH.md D1): a result arrives as
``//duckduckgo.com/l/?uddg=<target>&rut=<token>`` and an ad as
``duckduckgo.com/y.js?...``. The ``ddgs`` package has already done two things
to a row's ``href`` before this adapter sees it: the engine drops the
``https://duckduckgo.com/y.js?`` ad form (the scheme-relative form leaks
through), and ``results._normalize_url`` percent-decodes the whole href once.
So the row is reported as the destination it names -- decoded once, never
twice -- and an ad row is dropped instead of being returned as an organic
result. A row whose ``/l/`` link has no usable ``uddg`` keeps the URL the
engine gave it.

``DDGS_SAFESEARCH`` reaches the package as written, except ``strict`` -- the
spelling MCC's own docs gave, which ``ddgs`` does not know -- read as ``on``
(``config.websearch_catalog.DDGS_SAFESEARCH_ALIASES``, 7.120.2).
"""

import asyncio
from typing import Any, ClassVar
from urllib.parse import unquote, urlsplit

from ddgs import DDGS
from ddgs.exceptions import DDGSException, RatelimitException, TimeoutException

from my_claude_code.config.websearch_catalog import DDGS_SAFESEARCH_ALIASES
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
        items = tuple(item for row in rows if (item := _result_item(row)) is not None)
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
            kwargs["safesearch"] = DDGS_SAFESEARCH_ALIASES.get(
                safesearch.lower(), safesearch
            )
        return DDGS(
            proxy=self._config.proxy, timeout=int(self._config.http_timeout)
        ).text(query, **kwargs)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


#: DuckDuckGo's own host: result links are ``/l/?uddg=``, ad links ``/y.js``.
_DDG_HOST = "duckduckgo.com"
_DDG_RESULT_PATH = "/l/"
_DDG_AD_PATH = "/y.js"


def _duckduckgo_path(href: str) -> str | None:
    """A ``duckduckgo.com`` link's path (scheme-relative or absolute), else ``None``."""

    try:
        parts = urlsplit(href)
    except ValueError:  # e.g. an unclosed IPv6 bracket; leave the row as it is
        return None
    if parts.scheme not in {"", "http", "https"}:
        return None
    if (parts.hostname or "").lower() != _DDG_HOST:
        return None
    return parts.path


def _is_ad(href: str) -> bool:
    """Whether a row is an ad: the engine's ``y.js`` click link, either scheme form."""

    return _duckduckgo_path(href) == _DDG_AD_PATH


def _result_item(row: Any) -> WebSearchResultItem | None:
    """One engine row as MCC's item: ``None`` for an ad or a row with no link."""

    if not isinstance(row, dict):
        return None
    href = _text(row.get("href"))
    if not href or _is_ad(href):
        return None
    destination = _destination(href)
    return WebSearchResultItem(
        title=_text(row.get("title")),
        url=href if destination is None else destination,
        snippet=_text(row.get("body")),
        content=None,
        published=None,
    )


def _destination(href: str) -> str | None:
    """The destination a ``/l/?uddg=`` redirect names, or ``None`` to keep ``href``.

    ``uddg`` is read unsplit to the end of the query (a decoded destination may
    itself contain ``&``) and DuckDuckGo's trailing ``&rut=<token>`` is cut
    off. The value is decoded only when it did not already arrive as an
    absolute URL: ``ddgs`` has percent-decoded the href once already
    (``results._normalize_url``), so decoding again would corrupt a
    destination that legitimately carries a percent-escape of its own.
    """

    if _duckduckgo_path(href) != _DDG_RESULT_PATH:
        return None
    value = _parameter_value(urlsplit(href).query, "uddg")
    if not value:
        return None
    value = _without_rut(value)
    if _is_absolute_http_url(value):
        return value
    decoded = unquote(value)
    return decoded if _is_absolute_http_url(decoded) else None


def _parameter_value(query: str, name: str) -> str | None:
    """A parameter's raw value, to the end of the query (it may contain ``&``)."""

    prefix = f"{name}="
    start = 0
    while start <= len(query):
        if query.startswith(prefix, start):
            return query[start + len(prefix) :]
        boundary = query.find("&", start)
        if boundary < 0:
            return None
        start = boundary + 1
    return None


def _without_rut(value: str) -> str:
    """Cut DuckDuckGo's trailing ``&rut=<token>`` (a plain token, no ``&``) off."""

    marker = "&rut="
    at = value.rfind(marker)
    if at < 0:
        return value
    token = value[at + len(marker) :]
    if not token or any(char in token for char in "&/?# "):
        return value
    return value[:at]


def _is_absolute_http_url(value: str) -> bool:
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return parts.scheme in {"http", "https"} and bool(parts.hostname)
