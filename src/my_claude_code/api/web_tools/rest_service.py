"""Execution and dashboard-parity orchestration for ``/v1/websearch`` and ``/v1/webfetch``."""

import ipaddress
import math
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import aiohttp
import httpx
from fastapi import Request
from loguru import logger

from my_claude_code.config.settings import Settings
from my_claude_code.core.diagnostics import safe_exception_message
from my_claude_code.core.websearch.rest_models import (
    WebFetchRestRequest,
    WebFetchRestResponse,
    WebSearchRestRequest,
    WebSearchRestResponse,
    WebSearchRestResult,
    WebSearchRestUsage,
)
from my_claude_code.websearch.errors import (
    WebSearchAuthError,
    WebSearchConfigError,
    WebSearchError,
    WebSearchInvalidRequestError,
    WebSearchQuotaError,
    WebSearchRateLimitError,
)
from my_claude_code.websearch.registry import SearchOutcome, emit_search_outcome

from ..websearch_capture import record_rest_web_tool_call
from . import outbound, streaming
from .constants import _MAX_FETCH_CHARS, _MAX_SEARCH_RESULTS
from .egress import (
    WebFetchEgressPolicy,
    WebFetchEgressViolation,
    web_fetch_allowed_scheme_set,
)


class WebSearchRestError(Exception):
    """Structured HTTP error translated into an OpenAI-shaped JSON envelope."""

    def __init__(
        self,
        *,
        status_code: int,
        message: str,
        error_type: str,
        code: str | None = None,
        param: str | None = None,
        headers: dict[str, str] | None = None,
        error_kind: str = "internal",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.error_type = error_type
        self.code = code
        self.param = param
        self.headers = headers
        self.error_kind = error_kind


def _is_loopback_host(host: str | None) -> bool:
    if host is None:
        return False
    normalized = host.strip().strip("[]").lower()
    if normalized == "localhost" or normalized.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _safe_url_for_log(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "[invalid-url]"
    host = parsed.hostname or ""
    if not parsed.scheme or not host:
        return "[configured]"
    port = f":{parsed.port}" if parsed.port is not None else ""
    return urlunsplit((parsed.scheme, f"{host}{port}", parsed.path, "", ""))


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 3)


def _map_websearch_exception(exc: Exception) -> WebSearchRestError:
    if isinstance(exc, WebSearchRestError):
        return exc
    if isinstance(exc, WebSearchInvalidRequestError):
        return WebSearchRestError(
            status_code=400,
            message=safe_exception_message(exc),
            error_type="invalid_request_error",
            code="invalid_request",
            error_kind="invalid_request",
        )
    if isinstance(exc, WebFetchEgressViolation):
        return WebSearchRestError(
            status_code=400,
            message=safe_exception_message(exc),
            error_type="invalid_request_error",
            param="url",
            code="egress_blocked",
            error_kind="invalid_request",
        )
    if isinstance(exc, WebSearchRateLimitError | WebSearchQuotaError):
        retry_after = getattr(exc, "retry_after_seconds", None)
        headers: dict[str, str] | None = None
        if retry_after is not None and retry_after > 0:
            headers = {"Retry-After": str(math.ceil(retry_after))}
        return WebSearchRestError(
            status_code=429,
            message=safe_exception_message(exc),
            error_type="rate_limit_error",
            code="rate_limit_exceeded",
            headers=headers,
            error_kind="rate_limit",
        )
    if isinstance(exc, WebSearchConfigError | WebSearchAuthError):
        code = (
            "search_disabled"
            if isinstance(exc, WebSearchConfigError) and exc.provider == "disabled"
            else "service_unavailable"
        )
        return WebSearchRestError(
            status_code=503,
            message=safe_exception_message(exc),
            error_type="service_unavailable_error",
            code=code,
            error_kind="auth" if isinstance(exc, WebSearchAuthError) else "config",
        )
    if isinstance(exc, WebSearchError):
        return WebSearchRestError(
            status_code=502,
            message=safe_exception_message(exc),
            error_type="upstream_error",
            code="upstream_search_error",
            error_kind=exc.kind,
        )
    if isinstance(
        exc,
        aiohttp.ClientError | httpx.HTTPError | TimeoutError,
    ):
        return WebSearchRestError(
            status_code=502,
            message=safe_exception_message(exc),
            error_type="upstream_error",
            code="upstream_fetch_error",
            error_kind="upstream",
        )
    if isinstance(exc, ValueError):
        return WebSearchRestError(
            status_code=400,
            message=safe_exception_message(exc),
            error_type="invalid_request_error",
            code="invalid_request",
            error_kind="invalid_request",
        )
    return WebSearchRestError(
        status_code=502,
        message=safe_exception_message(exc),
        error_type="upstream_error",
        code="upstream_error",
        error_kind="internal",
    )


def _resolve_webfetch_egress(
    settings: Settings,
    request: Request,
    *,
    requested_allow_private: bool | None,
) -> tuple[WebFetchEgressPolicy, bool]:
    """Build egress policy honoring P1 tightening-only and non-loopback auth rules."""

    allowed_schemes = web_fetch_allowed_scheme_set(settings.web_fetch_allowed_schemes)
    configured_allow_private = bool(settings.web_fetch_allow_private_networks)
    if requested_allow_private is True and not configured_allow_private:
        raise WebSearchRestError(
            status_code=400,
            message=(
                "allow_private_networks=true cannot override "
                "WEB_FETCH_ALLOW_PRIVATE_NETWORKS=false"
            ),
            error_type="invalid_request_error",
            param="allow_private_networks",
            code="private_networks_disabled",
            error_kind="invalid_request",
        )
    effective_allow_private = (
        configured_allow_private
        if requested_allow_private is None
        else bool(requested_allow_private)
    )
    proxy_auth_configured = bool(settings.anthropic_auth_token.strip())
    client_host = request.client.host if request.client is not None else None
    client_is_loopback = _is_loopback_host(client_host)
    if (
        not _is_loopback_host(settings.host)
        and not proxy_auth_configured
        and configured_allow_private
    ):
        logger.warning(
            "WEB_FETCH_ALLOW_PRIVATE_NETWORKS=true while HOST={} has no "
            "PROXY_API_KEY (ANTHROPIC_AUTH_TOKEN); non-loopback private fetch "
            "attempts are blocked",
            settings.host,
        )
    forced_public_only = False
    if effective_allow_private and not client_is_loopback and not proxy_auth_configured:
        logger.warning(
            "Non-loopback unauthenticated /v1/webfetch tightened to public-only "
            "(client_host={!r})",
            client_host,
        )
        effective_allow_private = False
        forced_public_only = True
    return (
        WebFetchEgressPolicy(
            allow_private_network_targets=effective_allow_private,
            allowed_schemes=allowed_schemes,
        ),
        forced_public_only,
    )


async def execute_websearch(
    request: Request,
    request_data: WebSearchRestRequest,
    settings: Settings,
    *,
    request_id: str,
) -> WebSearchRestResponse:
    """Execute ``POST /v1/websearch`` with full dashboard parity and local capture."""

    started = time.perf_counter()
    capture_status = "error"
    capture_error_kind: str | None = None
    capture_error_message: str | None = None
    capture_output: str | None = None
    capture_params: dict[str, Any] = {
        "requested_provider": request_data.provider,
        "format": request_data.format,
    }

    try:
        if not settings.enable_web_server_tools:
            raise WebSearchRestError(
                status_code=503,
                message="Web server tools are disabled by ENABLE_WEB_SERVER_TOOLS=false",
                error_type="service_unavailable_error",
                code="web_tools_disabled",
                error_kind="config",
            )

        effective_max_results = (
            request_data.max_results
            if request_data.max_results is not None
            else _MAX_SEARCH_RESULTS
        )
        request_options = request_data.to_request_options()
        response, items, meta = await outbound._run_web_search_response(
            request_data.query,
            settings,
            max_results=effective_max_results,
            allowed_domains=tuple(request_data.allowed_domains),
            blocked_domains=tuple(request_data.blocked_domains),
            provider_override=request_data.provider,
            request_options=request_options,
            capture_meta=True,
        )

        if response is not None:
            result_models = [
                WebSearchRestResult(
                    title=entry.title,
                    url=entry.url,
                    snippet=entry.snippet,
                    content=entry.content,
                    published=entry.published,
                    score=entry.score,
                    author=entry.author,
                    favicon=entry.favicon,
                    site_name=entry.site_name,
                    highlights=(
                        list(entry.highlights) if entry.highlights is not None else None
                    ),
                )
                for entry in response.results[:effective_max_results]
            ]
        else:
            result_models = [
                WebSearchRestResult(
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    snippet=item.get("snippet", ""),
                    content=item.get("content") or None,
                    published=item.get("published") or None,
                )
                for item in items[:effective_max_results]
            ]

        if request_data.fetch_missing_content and result_models:
            try:
                egress, _forced = _resolve_webfetch_egress(
                    settings, request, requested_allow_private=None
                )
            except WebSearchRestError:
                egress = WebFetchEgressPolicy(
                    allow_private_network_targets=False,
                    allowed_schemes=web_fetch_allowed_scheme_set(
                        settings.web_fetch_allowed_schemes
                    ),
                )
            proxy_url = outbound.websearch_proxy_url()
            fetched_attempts = 0
            for idx, hit in enumerate(result_models):
                if fetched_attempts >= request_data.fetch_top_n:
                    break
                if hit.content:
                    continue
                fetched_attempts += 1
                try:
                    payload, _used, _trunc = await outbound._run_web_fetch_detail(
                        hit.url,
                        egress,
                        proxy_url=proxy_url,
                    )
                except Exception as exc:
                    logger.debug(
                        "fetch_missing_content skipped url={!r} error={}",
                        _safe_url_for_log(hit.url),
                        safe_exception_message(exc),
                    )
                    continue
                fetched_text = payload.get("data", "")
                if fetched_text:
                    result_models[idx] = hit.model_copy(
                        update={"content": fetched_text}
                    )
                    if idx < len(items):
                        items[idx] = {**items[idx], "content": fetched_text}

        digest_text: str | None = None
        if request_data.format == "digest" or request_data.include_digest:
            digest_text = streaming.format_search_digest(
                request_data.query, items, settings
            )

        answer = (
            response.answer
            if response is not None
            else ((items[0].get("answer") or None) if items else None)
        )
        rest_response = WebSearchRestResponse(
            id=f"ws_{uuid4().hex}",
            query=request_data.query,
            provider=meta.terminal_provider,
            fallback_used=meta.fallback_used,
            providers_tried=list(meta.providers_tried),
            supports_domain_filters=meta.supports_domain_filters,
            answer=answer,
            synthesis=None,
            results=result_models,
            digest=digest_text,
            provider_response_time_ms=(
                response.provider_response_time_ms if response is not None else None
            ),
            request_id=response.request_id if response is not None else None,
            usage=WebSearchRestUsage(
                search_requests=1,
                search_cost_usd=response.cost_usd if response is not None else None,
                usage_credits=response.usage_credits if response is not None else None,
                model_tokens_in=None,
                model_tokens_out=None,
                model_cost_usd=None,
            ),
        )
        capture_status = "success"
        capture_output = digest_text or "\n".join(hit.url for hit in result_models)
        capture_params = {
            "search_provider": meta.terminal_provider,
            "providers_tried": list(meta.providers_tried),
            "fallback_used": meta.fallback_used,
            "result_count": len(result_models),
            "format": request_data.format,
        }
        return rest_response
    except Exception as exc:
        mapped = _map_websearch_exception(exc)
        capture_error_kind = mapped.error_kind
        capture_error_message = mapped.message
        raise mapped from exc
    finally:
        record_rest_web_tool_call(
            settings,
            request_id=request_id,
            endpoint="/v1/websearch",
            protocol="rest_websearch",
            optimization="web_search",
            headers=request.headers,
            duration_ms=_elapsed_ms(started),
            status="success" if capture_status == "success" else "error",
            raw_input=request_data.query,
            raw_output=capture_output,
            params=capture_params,
            error_kind=capture_error_kind,
            error_message=capture_error_message,
        )


async def execute_webfetch(
    request: Request,
    request_data: WebFetchRestRequest,
    settings: Settings,
    *,
    request_id: str,
) -> WebFetchRestResponse:
    """Execute ``POST /v1/webfetch`` with P1 SSRF rules, proxy support, and dual telemetry."""

    ts_epoch = time.time()
    started = time.perf_counter()
    capture_status = "error"
    capture_error_kind: str | None = None
    capture_error_message: str | None = None
    capture_output: str | None = None
    capture_params: dict[str, Any] = {
        "extractor": request_data.extractor or settings.web_fetch_extractor,
    }
    egress: WebFetchEgressPolicy | None = None
    forced_public_only = False
    proxy_url: str | None = None
    payload: dict[str, str] | None = None
    extractor_used = ""
    truncated = False

    try:
        if not settings.enable_web_server_tools:
            raise WebSearchRestError(
                status_code=503,
                message="Web server tools are disabled by ENABLE_WEB_SERVER_TOOLS=false",
                error_type="service_unavailable_error",
                code="web_tools_disabled",
                error_kind="config",
            )

        egress, forced_public_only = _resolve_webfetch_egress(
            settings,
            request,
            requested_allow_private=request_data.allow_private_networks,
        )
        proxy_url = outbound.websearch_proxy_url()
        payload, extractor_used, truncated = await outbound._run_web_fetch_detail(
            request_data.url,
            egress,
            extractor=request_data.extractor,
            max_chars=request_data.max_chars,
            proxy_url=proxy_url,
        )
        rest_response = WebFetchRestResponse(
            id=f"wf_{uuid4().hex}",
            url=payload["url"],
            title=payload["title"],
            media_type=payload["media_type"],
            extractor_used=extractor_used,
            truncated=truncated,
            content=payload["data"],
        )
        capture_status = "success"
        capture_output = payload["data"]
        capture_params = {
            "extractor_used": extractor_used,
            "truncated": truncated,
            "chars": len(payload["data"]),
        }
        return rest_response
    except Exception as exc:
        if isinstance(exc, WebFetchEgressViolation) and forced_public_only:
            mapped = WebSearchRestError(
                status_code=400,
                message=(
                    "Private-network web_fetch over a non-loopback connection requires "
                    "PROXY_API_KEY (ANTHROPIC_AUTH_TOKEN) or "
                    "WEB_FETCH_ALLOW_PRIVATE_NETWORKS=false"
                ),
                error_type="invalid_request_error",
                param="url",
                code="egress_blocked",
                error_kind="invalid_request",
            )
        else:
            mapped = _map_websearch_exception(exc)
        capture_error_kind = mapped.error_kind
        capture_error_message = mapped.message
        raise mapped from exc
    finally:
        duration_ms = _elapsed_ms(started)
        record_rest_web_tool_call(
            settings,
            request_id=request_id,
            endpoint="/v1/webfetch",
            protocol="rest_webfetch",
            optimization="web_fetch",
            headers=request.headers,
            duration_ms=duration_ms,
            status="success" if capture_status == "success" else "error",
            raw_input=request_data.url,
            raw_output=capture_output,
            params=capture_params,
            error_kind=capture_error_kind,
            error_message=capture_error_message,
        )
        safe_input_url = _safe_url_for_log(request_data.url) or "[invalid-url]"
        effective_max_chars = (
            request_data.max_chars
            if request_data.max_chars is not None
            else _MAX_FETCH_CHARS
        )
        emit_search_outcome(
            SearchOutcome(
                ts_epoch=ts_epoch,
                ts_iso=datetime.fromtimestamp(ts_epoch, tz=UTC).isoformat(),
                provider="web_fetch",
                key_index=0,
                key_label="",
                query=safe_input_url[:256],
                results_count=1 if capture_status == "success" else 0,
                duration_ms=duration_ms,
                status=capture_status,
                error_kind=capture_error_kind,
                error_message=(
                    capture_error_message[:500] if capture_error_message else None
                ),
                cost_usd=None,
                route_id=None,
                attempt_number=1,
                input_payload={
                    "url": safe_input_url,
                    "extractor": (
                        request_data.extractor or settings.web_fetch_extractor
                    ),
                    "max_chars": effective_max_chars,
                },
                output_payload=(
                    {
                        "url": _safe_url_for_log(payload["url"]) if payload else None,
                        "title": payload["title"][:256] if payload else "",
                        "media_type": payload["media_type"]
                        if payload
                        else "text/plain",
                        "extractor_used": extractor_used,
                        "truncated": truncated,
                        "chars": len(payload["data"]) if payload else 0,
                    }
                    if capture_status == "success"
                    else {
                        "error": {
                            "kind": capture_error_kind or "internal",
                            "message": (capture_error_message or "")[:500],
                        }
                    }
                ),
                provider_config={
                    "provider_id": "web_fetch",
                    "allow_private_networks": (
                        egress.allow_private_network_targets
                        if egress is not None
                        else bool(settings.web_fetch_allow_private_networks)
                    ),
                    "proxy": _safe_url_for_log(proxy_url) if proxy_url else None,
                },
            )
        )
