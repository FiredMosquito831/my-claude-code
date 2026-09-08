"""Outbound HTTP for web_search / web_fetch (client, body caps, logging)."""

import asyncio
import socket
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urljoin, urlparse
from uuid import uuid4

import aiohttp
import httpx
from aiohttp import ClientSession, ClientTimeout, TCPConnector
from aiohttp.abc import AbstractResolver, ResolveResult
from loguru import logger

from my_claude_code.config.settings import Settings, get_settings
from my_claude_code.core.websearch.models import WebSearchResponse
from my_claude_code.websearch.errors import WebSearchConfigError, WebSearchError
from my_claude_code.websearch.registry import (
    SearchOutcome,
    SearchRouteOutcome,
    WebSearchRoute,
    emit_route_outcome,
    emit_search_outcome,
    resolve_search_route,
    search_with_logging,
)

from . import constants
from .constants import (
    _MAX_FETCH_CHARS,
    _MAX_SEARCH_RESULTS,
    _REDIRECT_RESPONSE_BODY_CAP_BYTES,
    _REQUEST_TIMEOUT_S,
    _WEB_FETCH_REDIRECT_STATUSES,
    _WEB_TOOL_HTTP_HEADERS,
)
from .egress import (
    WebFetchEgressPolicy,
    WebFetchEgressViolation,
    get_validated_stream_addrinfos_for_egress,
)
from .parsers import HTMLTextParser, SearchResultParser
from .search_providers import runtime_provider

_LEGACY_PROVIDER_ID = "legacy"
_ERROR_MESSAGE_LOG_CHARS = 500


@dataclass(slots=True)
class _SearchRouteTrace:
    route_id: str
    ts_epoch: float
    query: str
    started: float
    primary_provider: str = "unresolved"
    terminal_provider: str = "unresolved"
    providers: list[str] = field(default_factory=list)
    attempt_count: int = 0
    status: str = "error"
    results_count: int = 0
    cost_usd: float | None = None
    error_kind: str | None = "internal"
    error_message: str | None = "web search route did not complete"

    def begin_attempt(self, provider: str) -> int:
        if not self.providers:
            self.primary_provider = provider
        self.providers.append(provider)
        self.terminal_provider = provider
        self.attempt_count += 1
        return self.attempt_count

    def succeed(
        self,
        provider: str,
        *,
        results_count: int,
        cost_usd: float | None,
    ) -> None:
        self.terminal_provider = provider
        self.status = "success"
        self.results_count = results_count
        self.cost_usd = cost_usd
        self.error_kind = None
        self.error_message = None

    def fail(self, provider: str, error: BaseException) -> None:
        self.terminal_provider = provider
        self.status = "error"
        self.results_count = 0
        self.cost_usd = None
        self.error_kind = _search_error_kind(error)
        self.error_message = str(error)

    def outcome(self) -> SearchRouteOutcome:
        providers = tuple(self.providers) or (self.primary_provider,)
        return SearchRouteOutcome(
            route_id=self.route_id,
            ts_epoch=self.ts_epoch,
            ts_iso=datetime.fromtimestamp(self.ts_epoch, tz=UTC).isoformat(),
            query=self.query,
            primary_provider=self.primary_provider,
            terminal_provider=self.terminal_provider,
            provider_path=providers,
            attempt_count=self.attempt_count,
            fallback_used=len(providers) > 1,
            duration_ms=_elapsed_ms(self.started),
            status=self.status,
            results_count=self.results_count,
            cost_usd=self.cost_usd,
            error_kind=self.error_kind,
            error_message=self.error_message,
        )


def _safe_public_host_for_logs(url: str) -> str:
    host = urlparse(url).hostname or ""
    return host[:253]


def _log_web_tool_failure(
    tool_name: str,
    error: BaseException,
    *,
    fetch_url: str | None = None,
) -> None:
    exc_type = type(error).__name__
    if isinstance(error, WebFetchEgressViolation):
        host = _safe_public_host_for_logs(fetch_url) if fetch_url else ""
        logger.warning(
            "web_tool_egress_rejected tool={} exc_type={} host={!r}",
            tool_name,
            exc_type,
            host,
        )
        return
    if tool_name == "web_fetch" and fetch_url:
        logger.warning(
            "web_tool_failure tool={} exc_type={} host={!r}",
            tool_name,
            exc_type,
            _safe_public_host_for_logs(fetch_url),
        )
    else:
        logger.warning("web_tool_failure tool={} exc_type={}", tool_name, exc_type)


def _web_tool_client_error_summary(
    tool_name: str,
    error: BaseException,
    *,
    verbose: bool,
) -> str:
    if tool_name == "web_search" and isinstance(error, WebSearchConfigError):
        return f"web_search unavailable: {error.message}"
    if verbose:
        return f"{tool_name} failed: {type(error).__name__}"
    return "Web tool request failed."


async def _iter_response_body_under_cap(
    response: httpx.Response, max_bytes: int
) -> AsyncIterator[bytes]:
    if max_bytes <= 0:
        return
    received = 0
    async for chunk in response.aiter_bytes(chunk_size=65_536):
        if received >= max_bytes:
            break
        remaining = max_bytes - received
        if len(chunk) <= remaining:
            received += len(chunk)
            yield chunk
            if received >= max_bytes:
                break
        else:
            yield chunk[:remaining]
            break


async def _drain_response_body_capped(response: httpx.Response, max_bytes: int) -> None:
    async for _ in _iter_response_body_under_cap(response, max_bytes):
        pass


async def _read_response_body_capped(response: httpx.Response, max_bytes: int) -> bytes:
    return b"".join(
        [piece async for piece in _iter_response_body_under_cap(response, max_bytes)]
    )


_NUMERIC_RESOLVE_FLAGS = socket.AI_NUMERICHOST | socket.AI_NUMERICSERV
_NAME_RESOLVE_FLAGS = socket.NI_NUMERICHOST | socket.NI_NUMERICSERV


def getaddrinfo_rows_to_resolve_results(
    host: str, addrinfos: list[tuple]
) -> list[ResolveResult]:
    """Map :func:`socket.getaddrinfo` rows to aiohttp :class:`ResolveResult` (ThreadedResolver logic)."""
    out: list[ResolveResult] = []
    for family, _type, proto, _canon, sockaddr in addrinfos:
        if family == socket.AF_INET6:
            if len(sockaddr) < 3:
                continue
            if sockaddr[3]:
                resolved_host, port = socket.getnameinfo(sockaddr, _NAME_RESOLVE_FLAGS)
            else:
                resolved_host, port = sockaddr[:2]
        else:
            assert family == socket.AF_INET, family
            resolved_host, port = sockaddr[0], sockaddr[1]
            resolved_host = str(resolved_host)
            port = int(port)
        out.append(
            ResolveResult(
                hostname=host,
                host=resolved_host,
                port=int(port),
                family=family,
                proto=proto,
                flags=_NUMERIC_RESOLVE_FLAGS,
            )
        )
    return out


class _PinnedEgressStaticResolver(AbstractResolver):
    """Return only pre-validated :class:`ResolveResult` for the outbound request."""

    def __init__(self, results: list[ResolveResult]) -> None:
        self._results = results

    async def resolve(
        self, host: str, port: int = 0, family: int = socket.AF_INET
    ) -> list[ResolveResult]:
        return self._results

    async def close(self) -> None:  # pragma: no cover - aiohttp contract
        return


async def _read_aiohttp_body_capped(
    response: aiohttp.ClientResponse, max_bytes: int
) -> bytes:
    received = 0
    parts: list[bytes] = []
    async for chunk in response.content.iter_chunked(65_536):
        if received >= max_bytes:
            break
        remaining = max_bytes - received
        if len(chunk) <= remaining:
            received += len(chunk)
            parts.append(chunk)
        else:
            parts.append(chunk[:remaining])
            break
    return b"".join(parts)


async def _drain_aiohttp_body_capped(
    response: aiohttp.ClientResponse, max_bytes: int
) -> None:
    if max_bytes <= 0:
        return
    received = 0
    async for chunk in response.content.iter_chunked(65_536):
        received += len(chunk)
        if received >= max_bytes:
            break


async def _run_web_search(
    query: str,
    settings: Settings | None = None,
    *,
    allowed_domains: tuple[str, ...] = (),
    blocked_domains: tuple[str, ...] = (),
) -> list[dict[str, str]]:
    """Run web_search using the configured, explicit fallback route.

    ``allowed_domains``/``blocked_domains`` come from the client's tool
    definition. Providers that cannot filter server-side drop them in
    :meth:`BaseWebSearchProvider.search`, which is recorded per attempt as
    ``supports_domain_filters``.
    """

    settings = settings if settings is not None else get_settings()
    trace = _SearchRouteTrace(
        route_id=uuid4().hex,
        ts_epoch=time.time(),
        query=query,
        started=time.perf_counter(),
    )
    try:
        route = resolve_search_route(settings)
        route_context = _route_context_snapshot(settings, route)
        if route.provider_ids:
            trace.primary_provider = route.provider_ids[0]
            trace.terminal_provider = route.provider_ids[0]
        elif route.use_legacy_scrape:
            trace.primary_provider = _LEGACY_PROVIDER_ID
            trace.terminal_provider = _LEGACY_PROVIDER_ID
        else:
            trace.primary_provider = settings.web_search_provider
            trace.terminal_provider = settings.web_search_provider
        if route.disabled:
            error = WebSearchConfigError(
                "disabled",
                "web search is disabled by WEB_SEARCH_PROVIDER=disabled",
            )
            trace.fail(trace.terminal_provider, error)
            raise error
        if route.provider_ids:
            results = await _provider_web_search(
                query,
                settings,
                route,
                trace,
                route_context,
                allowed_domains=allowed_domains,
                blocked_domains=blocked_domains,
            )
            if results is not None:
                return results
        if route.use_legacy_scrape:
            return await _legacy_route_search(query, trace, route_context)
        error = WebSearchConfigError(
            settings.web_search_provider,
            "web search has no configured route",
        )
        trace.fail(trace.terminal_provider, error)
        raise error
    except asyncio.CancelledError as error:
        trace.fail(trace.terminal_provider, error)
        raise
    except Exception as error:
        trace.fail(trace.terminal_provider, error)
        raise
    finally:
        emit_route_outcome(trace.outcome())


async def _provider_web_search(
    query: str,
    settings: Settings,
    route: WebSearchRoute,
    trace: _SearchRouteTrace,
    route_context: dict[str, object],
    *,
    allowed_domains: tuple[str, ...] = (),
    blocked_domains: tuple[str, ...] = (),
) -> list[dict[str, str]] | None:
    """Try the provider route; None means its terminal legacy fallback may run."""

    last_error: WebSearchError | None = None
    for attempt, provider_id in enumerate(route.provider_ids, start=1):
        attempt_number = trace.begin_attempt(provider_id)
        attempt_ts = time.time()
        attempt_started = time.perf_counter()
        try:
            provider = await runtime_provider(settings, provider_id)
        except Exception as error:
            trace.fail(provider_id, error)
            _emit_manual_attempt(
                trace=trace,
                provider=provider_id,
                attempt_number=attempt_number,
                ts_epoch=attempt_ts,
                started=attempt_started,
                status="error",
                error=error,
                route_context=route_context,
            )
            raise
        try:
            response = await search_with_logging(
                provider,
                query,
                max_results=_MAX_SEARCH_RESULTS,
                allowed_domains=allowed_domains,
                blocked_domains=blocked_domains,
                route_id=trace.route_id,
                attempt_number=attempt_number,
                route_context=route_context,
            )
        except WebSearchConfigError as error:
            # A selected provider missing credentials/base URL is an operator
            # error, not an upstream outage. Never hide it behind another search.
            trace.fail(provider_id, error)
            raise
        except WebSearchError as error:
            last_error = error
            trace.fail(provider_id, error)
            logger.warning(
                "web_search provider attempt failed provider={} attempt={}/{} error={}",
                provider_id,
                attempt,
                len(route.provider_ids),
                error,
            )
        except Exception as error:
            trace.fail(provider_id, error)
            raise
        else:
            results = _web_search_response_items(response)
            trace.succeed(
                provider_id,
                results_count=len(results),
                cost_usd=response.cost_usd,
            )
            return results

    if route.use_legacy_scrape:
        return None
    if last_error is not None:
        raise last_error
    raise WebSearchConfigError(
        settings.web_search_provider,
        "web search provider route is empty",
    )


async def _legacy_route_search(
    query: str,
    trace: _SearchRouteTrace,
    route_context: dict[str, object],
) -> list[dict[str, str]]:
    attempt_number = trace.begin_attempt(_LEGACY_PROVIDER_ID)
    ts_epoch = time.time()
    started = time.perf_counter()
    try:
        raw_results = await _legacy_web_search_scrape(query)
    except Exception as error:
        trace.fail(_LEGACY_PROVIDER_ID, error)
        _emit_manual_attempt(
            trace=trace,
            provider=_LEGACY_PROVIDER_ID,
            attempt_number=attempt_number,
            ts_epoch=ts_epoch,
            started=started,
            status="error",
            error=error,
            route_context=route_context,
        )
        raise
    results = [{**item, "provider": _LEGACY_PROVIDER_ID} for item in raw_results]
    _emit_manual_attempt(
        trace=trace,
        provider=_LEGACY_PROVIDER_ID,
        attempt_number=attempt_number,
        ts_epoch=ts_epoch,
        started=started,
        status="success",
        results_count=len(results),
        results=results,
        route_context=route_context,
    )
    trace.succeed(
        _LEGACY_PROVIDER_ID,
        results_count=len(results),
        cost_usd=None,
    )
    return results


def _emit_manual_attempt(
    *,
    trace: _SearchRouteTrace,
    provider: str,
    attempt_number: int,
    ts_epoch: float,
    started: float,
    status: str,
    results_count: int = 0,
    results: list[dict[str, str]] | None = None,
    error: BaseException | None = None,
    route_context: dict[str, object] | None = None,
    allowed_domains: tuple[str, ...] = (),
    blocked_domains: tuple[str, ...] = (),
) -> None:
    input_payload: dict[str, object] = {
        "query": trace.query,
        "max_results": _MAX_SEARCH_RESULTS,
        "allowed_domains": list(allowed_domains),
        "blocked_domains": list(blocked_domains),
    }
    output_payload: dict[str, object]
    if error is None:
        output_payload = {
            "provider": provider,
            "query": trace.query,
            "answer": None,
            "results": results or [],
            "result_count": results_count,
            "key_index": 0,
            "cost_usd": None,
        }
    else:
        output_payload = {
            "error": {
                "kind": _search_error_kind(error),
                "type": type(error).__name__,
                "message": str(error)[:_ERROR_MESSAGE_LOG_CHARS],
            }
        }
    emit_search_outcome(
        SearchOutcome(
            ts_epoch=ts_epoch,
            ts_iso=datetime.fromtimestamp(ts_epoch, tz=UTC).isoformat(),
            provider=provider,
            key_index=0,
            key_label="",
            query=trace.query,
            results_count=results_count,
            duration_ms=_elapsed_ms(started),
            status=status,
            error_kind=_search_error_kind(error) if error is not None else None,
            error_message=(
                str(error)[:_ERROR_MESSAGE_LOG_CHARS] if error is not None else None
            ),
            cost_usd=None,
            route_id=trace.route_id,
            attempt_number=attempt_number,
            input_payload=input_payload,
            output_payload=output_payload,
            provider_config={
                "provider_id": provider,
                "credential_rotation": "none",
                "credential_count": 0,
                "base_url": None,
                "proxy": None,
                "http_timeout_seconds": _REQUEST_TIMEOUT_S,
                "supports_domain_filters": False,
                "options": {},
                "route": route_context or {},
            },
        )
    )


def _route_context_snapshot(
    settings: Settings,
    route: WebSearchRoute,
) -> dict[str, object]:
    provider_path = list(route.provider_ids)
    if route.use_legacy_scrape:
        provider_path.append(_LEGACY_PROVIDER_ID)
    return {
        "selected_provider": settings.web_search_provider,
        "fallback_policy": settings.web_search_fallback_policy,
        "resolved_provider_path": provider_path,
        "legacy_fallback": route.use_legacy_scrape,
        "disabled": route.disabled,
        "max_results": _MAX_SEARCH_RESULTS,
        "digest_chars": settings.websearch_digest_chars,
        "digest_answer": settings.websearch_digest_answer,
    }


def _search_error_kind(error: BaseException) -> str:
    if isinstance(error, WebSearchError):
        return error.kind
    if isinstance(error, httpx.HTTPError):
        return "upstream"
    if isinstance(error, asyncio.CancelledError):
        return "cancelled"
    return "internal"


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 3)


def _web_search_response_items(response: WebSearchResponse) -> list[dict[str, str]]:
    """Pass provider richness through to the streaming digest (title/url only
    was the v4.9.0 shape; snippet/content/published/answer/provider are additive).

    ``content`` is the provider's extracted page text, populated by exa,
    tavily, firecrawl, jina, brave, ollama and parallel when the operator turns
    the corresponding option on. It is deliberately carried separately from
    ``snippet`` so the digest can prefer the fuller text under its own cap.
    """

    return [
        {
            "title": item.title,
            "url": item.url,
            "snippet": item.snippet,
            "content": item.content or "",
            "published": item.published or "",
            "answer": response.answer or "",
            "provider": response.provider,
        }
        for item in response.results[:_MAX_SEARCH_RESULTS]
    ]


async def _legacy_web_search_scrape(query: str) -> list[dict[str, str]]:
    async with (
        httpx.AsyncClient(
            timeout=_REQUEST_TIMEOUT_S,
            follow_redirects=True,
            headers=_WEB_TOOL_HTTP_HEADERS,
        ) as client,
        client.stream(
            "GET",
            "https://lite.duckduckgo.com/lite/",
            params={"q": query},
        ) as response,
    ):
        response.raise_for_status()
        body_bytes = await _read_response_body_capped(
            response, constants._MAX_WEB_FETCH_RESPONSE_BYTES
        )
    text = body_bytes.decode("utf-8", errors="replace")
    parser = SearchResultParser()
    parser.feed(text)
    return parser.results[:_MAX_SEARCH_RESULTS]


async def _run_web_fetch(url: str, egress: WebFetchEgressPolicy) -> dict[str, str]:
    """Fetch URL with manual redirects; each hop is DNS-pinned to validated addresses."""
    current_url = url
    redirect_hops = 0
    timeout = ClientTimeout(total=_REQUEST_TIMEOUT_S)

    while True:
        addr_infos = await asyncio.to_thread(
            get_validated_stream_addrinfos_for_egress, current_url, egress
        )
        host = urlparse(current_url).hostname or ""
        results = getaddrinfo_rows_to_resolve_results(host, addr_infos)
        resolver = _PinnedEgressStaticResolver(results)
        connector = TCPConnector(
            resolver=resolver,
            force_close=True,
        )
        try:
            async with (
                ClientSession(
                    timeout=timeout,
                    headers=_WEB_TOOL_HTTP_HEADERS,
                    connector=connector,
                ) as session,
                session.get(current_url, allow_redirects=False) as response,
            ):
                if response.status in _WEB_FETCH_REDIRECT_STATUSES:
                    await _drain_aiohttp_body_capped(
                        response, _REDIRECT_RESPONSE_BODY_CAP_BYTES
                    )
                    if redirect_hops >= constants._MAX_WEB_FETCH_REDIRECTS:
                        raise WebFetchEgressViolation(
                            "web_fetch exceeded maximum redirects "
                            f"({constants._MAX_WEB_FETCH_REDIRECTS})"
                        )
                    location = response.headers.get("location")
                    if not location or not location.strip():
                        raise WebFetchEgressViolation(
                            "web_fetch redirect response missing Location header"
                        )
                    current_url = urljoin(str(response.url), location.strip())
                    redirect_hops += 1
                    continue
                response.raise_for_status()
                content_type = response.headers.get("content-type", "text/plain")
                final_url = str(response.url)
                encoding = response.get_encoding() or "utf-8"
                body_bytes = await _read_aiohttp_body_capped(
                    response, constants._MAX_WEB_FETCH_RESPONSE_BYTES
                )
        finally:
            await connector.close()

        break

    text = body_bytes.decode(encoding, errors="replace")
    title = final_url
    data = text
    if "html" in content_type.lower():
        parser = HTMLTextParser()
        parser.feed(text)
        title = parser.title or final_url
        data = "\n".join(parser.text_parts)
    return {
        "url": final_url,
        "title": title,
        "media_type": "text/plain",
        "data": data[:_MAX_FETCH_CHARS],
    }
