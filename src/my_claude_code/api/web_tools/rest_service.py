"""Execution and dashboard-parity orchestration for ``/v1/websearch`` and ``/v1/webfetch``."""

import asyncio
import ipaddress
import math
import re
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import aiohttp
import httpx
from fastapi import Request
from loguru import logger

from my_claude_code.application.errors import ApplicationError
from my_claude_code.application.execution import (
    ProviderExecutor,
    route_execution_policy,
    route_health_registry,
)
from my_claude_code.application.ports import RequestRuntimeLease
from my_claude_code.application.routing import ModelRouter
from my_claude_code.config.model_overrides import current_model_overrides
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic import (
    Message,
    MessagesRequest,
    aggregate_anthropic_sse_to_message,
    anthropic_status_for_error_type,
    get_token_count,
)
from my_claude_code.core.client_fingerprint import harness_from_headers
from my_claude_code.core.diagnostics import safe_exception_message
from my_claude_code.core.failures import FailureKind, find_execution_failure
from my_claude_code.core.openai_common import openai_error_type_for_failure
from my_claude_code.core.websearch.dialects import BatchFetchOutcome
from my_claude_code.core.websearch.rest_models import (
    WebFetchRestRequest,
    WebFetchRestResponse,
    WebSearchRestRequest,
    WebSearchRestResponse,
    WebSearchRestResult,
    WebSearchRestSynthesis,
    WebSearchRestUsage,
    WebSearchSynthesisCitation,
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

from ..dependencies import resolve_provider
from ..ports import ApiServices
from ..request_capture import build_capture
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


_SYNTHESIS_SYSTEM_PROMPT = (
    "You are a grounded research synthesizer. Answer the user's query accurately "
    "and concisely using ONLY the numbered web search sources provided below. "
    "Cite supporting claims inline using bracketed source numbers such as [1] "
    "or [2]. If the provided sources do not contain enough information to answer "
    "part or all of the query, state that clearly. Never fabricate URLs, facts, "
    "dates, or statistics outside the sources."
)
_SYNTHESIS_SOURCE_TEXT_CAP = 4000
_BATCH_FETCH_CONCURRENCY = 5

_SSE_ERROR_TYPE_TO_OPENAI: dict[str, str] = {
    "invalid_request_error": "invalid_request_error",
    "authentication_error": "authentication_error",
    "billing_error": "billing_error",
    "permission_error": "permission_error",
    "not_found_error": "not_found_error",
    "request_too_large": "request_too_large",
    "rate_limit_error": "rate_limit_error",
    "timeout_error": "timeout_error",
    "overloaded_error": "overloaded_error",
    "api_error": "api_error",
}


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _build_synthesis_user_prompt(
    query: str,
    results: list[WebSearchRestResult],
) -> str:
    if not results:
        return f"Query: {query}\n\nSources:\n(No search results returned.)"
    sections: list[str] = [f"Query: {query}", "", "Sources:"]
    for idx, hit in enumerate(results, start=1):
        body = (hit.content or hit.snippet or "").strip()
        if len(body) > _SYNTHESIS_SOURCE_TEXT_CAP:
            body = body[:_SYNTHESIS_SOURCE_TEXT_CAP] + "..."
        lines = [f"[{idx}] {hit.title or hit.url}", f"URL: {hit.url}"]
        if hit.published:
            lines.append(f"Published: {hit.published}")
        if body:
            lines.append(f"Excerpt: {body}")
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def _extract_grounded_citations(
    answer_text: str,
    results: list[WebSearchRestResult],
) -> list[WebSearchSynthesisCitation]:
    """Extract deduplicated inline ``[N]`` citations pointing to actual results."""

    citations: list[WebSearchSynthesisCitation] = []
    seen: set[int] = set()
    for match in re.finditer(r"\[(\d+)\]", answer_text):
        idx = int(match.group(1))
        if idx < 1 or idx > len(results) or idx in seen:
            continue
        seen.add(idx)
        hit = results[idx - 1]
        citations.append(
            WebSearchSynthesisCitation(
                index=idx,
                url=hit.url,
                title=hit.title,
            )
        )
    return citations


def _build_model_router(
    services: ApiServices, lease: RequestRuntimeLease
) -> ModelRouter:
    return ModelRouter(
        lease.settings,
        vision_lookup=services.requests.cached_model_supports_vision,
        reasoning_capability_lookup=services.requests.model_reasoning_capability,
        reasoning_dialect_lookup=services.requests.model_reasoning_dialect,
        output_limit_lookup=services.requests.model_output_limit,
        context_length_lookup=services.requests.model_context_length,
        model_preferences=current_model_overrides,
    )


def _throttle_lookup(lease: RequestRuntimeLease, provider_id: str) -> float | None:
    try:
        provider = resolve_provider(provider_id, lease=lease)
    except Exception:
        return None
    try:
        return provider.throttle_remaining()
    except Exception:
        return None


def _map_synthesis_exception(exc: BaseException) -> WebSearchRestError:
    if isinstance(exc, WebSearchRestError):
        return exc
    failure = find_execution_failure(exc)
    if failure is not None:
        headers: dict[str, str] | None = None
        retry_after = getattr(failure, "retry_after", None)
        if (
            failure.kind == FailureKind.RATE_LIMIT
            and isinstance(retry_after, int | float)
            and retry_after > 0
        ):
            headers = {"Retry-After": str(math.ceil(retry_after))}
        return WebSearchRestError(
            status_code=failure.status_code,
            message=safe_exception_message(failure),
            error_type=openai_error_type_for_failure(failure),
            code=failure.kind.value,
            headers=headers,
            error_kind=failure.kind.value,
        )
    if isinstance(exc, ApplicationError):
        return WebSearchRestError(
            status_code=exc.status_code,
            message=safe_exception_message(exc),
            error_type=openai_error_type_for_failure(exc.kind),
            code=exc.kind.value,
            error_kind=exc.kind.value,
        )
    return WebSearchRestError(
        status_code=502,
        message=safe_exception_message(exc),
        error_type="upstream_error",
        code="synthesis_error",
        error_kind="upstream",
    )


def _map_synthesis_stream_error(error: Mapping[str, Any]) -> WebSearchRestError:
    raw_type = error.get("type")
    error_type = (
        raw_type.strip()
        if isinstance(raw_type, str) and raw_type.strip()
        else "api_error"
    )
    raw_message = error.get("message")
    message = (
        raw_message.strip()
        if isinstance(raw_message, str) and raw_message.strip()
        else "Synthesis model request failed."
    )
    status_code = anthropic_status_for_error_type(error_type)
    openai_type = _SSE_ERROR_TYPE_TO_OPENAI.get(error_type, "api_error")
    return WebSearchRestError(
        status_code=status_code,
        message=message,
        error_type=openai_type,
        code=openai_type,
        error_kind="upstream",
    )


async def _run_grounded_synthesis(
    request: Request,
    request_data: WebSearchRestRequest,
    results: list[WebSearchRestResult],
    *,
    services: ApiServices,
    request_id: str,
) -> tuple[WebSearchRestSynthesis, int | None, int | None, float | None]:
    lease: RequestRuntimeLease | None = None
    try:
        lease = await services.requests.acquire()
    except Exception as exc:
        raise _map_synthesis_exception(exc) from exc

    capture = None
    try:
        target_model = request_data.model or lease.settings.model
        user_prompt = _build_synthesis_user_prompt(request_data.query, results)
        synth_request = MessagesRequest(
            model=target_model,
            max_tokens=request_data.synthesis_max_tokens or 1024,
            messages=[Message(role="user", content=user_prompt)],
            system=_SYNTHESIS_SYSTEM_PROMPT,
            stream=False,
        )
        capture = build_capture(
            lease.settings,
            synth_request,
            request_id=request_id,
            endpoint=request.url.path,
            protocol="rest_websearch",
            headers=request.headers,
            stream=False,
        )
        harness = harness_from_headers(request.headers).harness
        router = _build_model_router(services, lease)
        plan = router.resolve_messages_plan(synth_request, harness=harness)
        capture.set_plan(plan)
        capture.set_routing(plan.primary)
        executor = ProviderExecutor(
            lambda pid: resolve_provider(pid, lease=lease),
            policy=route_execution_policy(lease.settings),
            health=route_health_registry(lease.settings),
            token_counter=get_token_count,
            generation_id=lease.generation_id,
            log_raw_payloads=lease.settings.log_raw_api_payloads,
            retry_first=lease.settings.fallback_retry_first,
            provider_lookup=lambda pid: _throttle_lookup(lease, pid),
        )
        stream = executor.stream(
            plan,
            wire_api="messages",
            raw_log_label="FULL_PAYLOAD",
            raw_log_payload=plan.primary.request.model_dump(),
            request_id=request_id,
            on_attempt=capture.set_routing,
            on_attempt_result=capture.record_attempt_result,
        )
        message, stream_error = await aggregate_anthropic_sse_to_message(
            capture.wrap(stream)
        )
        if stream_error is not None:
            raise _map_synthesis_stream_error(stream_error)

        text_parts: list[str] = []
        raw_content = message.get("content")
        if isinstance(raw_content, list):
            for block in raw_content:
                if isinstance(block, Mapping) and block.get("type") == "text":
                    text = block.get("text")
                    if isinstance(text, str):
                        text_parts.append(text)
        answer_text = "\n".join(part for part in text_parts if part).strip()
        citations = _extract_grounded_citations(answer_text, results)

        raw_model = str(message.get("model") or "").strip()
        synth_model = (
            raw_model
            if raw_model and raw_model != "unknown"
            else (capture.recorded_model or target_model)
        )
        raw_usage = message.get("usage")
        usage_dict = raw_usage if isinstance(raw_usage, Mapping) else {}
        cap_in, cap_out = capture.recorded_usage
        tokens_in = (
            cap_in
            if cap_in is not None
            else _optional_int(usage_dict.get("input_tokens"))
        )
        tokens_out = (
            cap_out
            if cap_out is not None
            else _optional_int(usage_dict.get("output_tokens"))
        )
        return (
            WebSearchRestSynthesis(
                model=synth_model,
                answer=answer_text,
                citations=citations,
            ),
            tokens_in,
            tokens_out,
            capture.recorded_cost_usd,
        )
    except WebSearchRestError:
        raise
    except Exception as exc:
        if capture is not None:
            failure = find_execution_failure(exc)
            capture.finish_error(failure if failure is not None else exc)
        raise _map_synthesis_exception(exc) from exc
    finally:
        if lease is not None:
            await lease.release()


async def execute_websearch(
    request: Request,
    request_data: WebSearchRestRequest,
    settings: Settings,
    *,
    request_id: str,
    services: ApiServices | None = None,
) -> WebSearchRestResponse:
    """Execute ``POST /v1/websearch`` with full dashboard parity and optional synthesis."""

    started = time.perf_counter()
    capture_status = "error"
    capture_error_kind: str | None = None
    capture_error_message: str | None = None
    capture_output: str | None = None
    capture_params: dict[str, Any] = {
        "requested_provider": request_data.provider,
        "format": request_data.format,
    }
    search_phase_finished = False

    try:
        if not settings.enable_web_server_tools:
            raise WebSearchRestError(
                status_code=503,
                message="Web server tools are disabled by ENABLE_WEB_SERVER_TOOLS=false",
                error_type="service_unavailable_error",
                code="web_tools_disabled",
                error_kind="config",
            )
        if request_data.synthesize and services is None:
            raise WebSearchRestError(
                status_code=503,
                message="Runtime services unavailable for synthesis",
                error_type="service_unavailable_error",
                code="service_unavailable",
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
                    highlight_scores=(
                        list(entry.highlight_scores)
                        if entry.highlight_scores is not None
                        else None
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
        search_phase_finished = True

        synthesis: WebSearchRestSynthesis | None = None
        model_tokens_in: int | None = None
        model_tokens_out: int | None = None
        model_cost_usd: float | None = None
        if request_data.synthesize:
            assert services is not None
            (
                synthesis,
                model_tokens_in,
                model_tokens_out,
                model_cost_usd,
            ) = await _run_grounded_synthesis(
                request,
                request_data,
                result_models,
                services=services,
                request_id=request_id,
            )

        rest_response = WebSearchRestResponse(
            id=f"ws_{uuid4().hex}",
            query=request_data.query,
            provider=meta.terminal_provider,
            fallback_used=meta.fallback_used,
            providers_tried=list(meta.providers_tried),
            supports_domain_filters=meta.supports_domain_filters,
            answer=answer,
            synthesis=synthesis,
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
                model_tokens_in=model_tokens_in,
                model_tokens_out=model_tokens_out,
                model_cost_usd=model_cost_usd,
            ),
            images=(
                list(response.images)
                if response is not None and response.images is not None
                else None
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
        # When synthesize=true reaches the LLM step, RequestCapture owns the one
        # is_local=0 row in requests.db; never emit a second local:web_search row.
        if not (request_data.synthesize and search_phase_finished):
            record_rest_web_tool_call(
                settings,
                request_id=request_id,
                endpoint=request.url.path,
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
            endpoint=request.url.path,
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
        _emit_webfetch_outcome(
            ts_epoch=ts_epoch,
            duration_ms=duration_ms,
            input_url=request_data.url,
            extractor=request_data.extractor or settings.web_fetch_extractor,
            max_chars=(
                request_data.max_chars
                if request_data.max_chars is not None
                else _MAX_FETCH_CHARS
            ),
            status=capture_status,
            error_kind=capture_error_kind,
            error_message=capture_error_message,
            payload=payload,
            extractor_used=extractor_used,
            truncated=truncated,
            egress=egress,
            settings=settings,
            proxy_url=proxy_url,
        )


def _emit_webfetch_outcome(
    *,
    ts_epoch: float,
    duration_ms: float,
    input_url: str,
    extractor: str,
    max_chars: int,
    status: str,
    error_kind: str | None,
    error_message: str | None,
    payload: Mapping[str, str] | None,
    extractor_used: str,
    truncated: bool,
    egress: WebFetchEgressPolicy | None,
    settings: Settings,
    proxy_url: str | None,
) -> None:
    safe_input_url = _safe_url_for_log(input_url) or "[invalid-url]"
    emit_search_outcome(
        SearchOutcome(
            ts_epoch=ts_epoch,
            ts_iso=datetime.fromtimestamp(ts_epoch, tz=UTC).isoformat(),
            provider="web_fetch",
            key_index=0,
            key_label="",
            query=safe_input_url[:256],
            results_count=1 if status == "success" else 0,
            duration_ms=duration_ms,
            status=status,
            error_kind=error_kind,
            error_message=(error_message[:500] if error_message else None),
            cost_usd=None,
            route_id=None,
            attempt_number=1,
            input_payload={
                "url": safe_input_url,
                "extractor": extractor,
                "max_chars": max_chars,
            },
            output_payload=(
                {
                    "url": _safe_url_for_log(payload["url"]) if payload else None,
                    "title": payload["title"][:256] if payload else "",
                    "media_type": (payload["media_type"] if payload else "text/plain"),
                    "extractor_used": extractor_used,
                    "truncated": truncated,
                    "chars": len(payload["data"]) if payload else 0,
                }
                if status == "success"
                else {
                    "error": {
                        "kind": error_kind or "internal",
                        "message": (error_message or "")[:500],
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


async def execute_batch_webfetch(
    request: Request,
    *,
    urls: list[str],
    settings: Settings,
    request_id: str,
    extractor: str | None = None,
    max_chars: int | None = None,
) -> list[BatchFetchOutcome]:
    """Fetch multiple URLs concurrently with P1 SSRF guard and per-URL failure tolerance."""

    started = time.perf_counter()
    if not settings.enable_web_server_tools:
        err = WebSearchRestError(
            status_code=503,
            message="Web server tools are disabled by ENABLE_WEB_SERVER_TOOLS=false",
            error_type="service_unavailable_error",
            code="web_tools_disabled",
            error_kind="config",
        )
        record_rest_web_tool_call(
            settings,
            request_id=request_id,
            endpoint=request.url.path,
            protocol="rest_webfetch",
            optimization="web_fetch",
            headers=request.headers,
            duration_ms=_elapsed_ms(started),
            status="error",
            raw_input="\n".join(urls),
            raw_output=None,
            params={"urls_total": len(urls)},
            error_kind=err.error_kind,
            error_message=err.message,
        )
        raise err

    egress, forced_public_only = _resolve_webfetch_egress(
        settings, request, requested_allow_private=None
    )
    proxy_url = outbound.websearch_proxy_url()
    effective_extractor = extractor or settings.web_fetch_extractor
    effective_max_chars = max_chars if max_chars is not None else _MAX_FETCH_CHARS
    semaphore = asyncio.Semaphore(_BATCH_FETCH_CONCURRENCY)

    async def _fetch_one(target_url: str) -> BatchFetchOutcome:
        url_ts_epoch = time.time()
        url_started = time.perf_counter()
        payload: dict[str, str] | None = None
        used = ""
        trunc = False
        err_kind: str | None = None
        err_msg: str | None = None
        outcome_status = "error"
        try:
            async with semaphore:
                payload, used, trunc = await outbound._run_web_fetch_detail(
                    target_url,
                    egress,
                    extractor=extractor,
                    max_chars=max_chars,
                    proxy_url=proxy_url,
                )
            outcome_status = "success"
            return BatchFetchOutcome(
                url=target_url,
                final_url=payload["url"],
                title=payload["title"],
                content=payload["data"],
            )
        except Exception as exc:
            if isinstance(exc, WebFetchEgressViolation) and forced_public_only:
                mapped = WebSearchRestError(
                    status_code=400,
                    message=(
                        "Private-network web_fetch over a non-loopback connection "
                        "requires PROXY_API_KEY (ANTHROPIC_AUTH_TOKEN) or "
                        "WEB_FETCH_ALLOW_PRIVATE_NETWORKS=false"
                    ),
                    error_type="invalid_request_error",
                    param="url",
                    code="egress_blocked",
                    error_kind="invalid_request",
                )
            else:
                mapped = _map_websearch_exception(exc)
            err_kind = mapped.error_kind
            err_msg = mapped.message
            return BatchFetchOutcome(
                url=target_url,
                error=mapped.message,
                status_code=mapped.status_code,
            )
        finally:
            _emit_webfetch_outcome(
                ts_epoch=url_ts_epoch,
                duration_ms=_elapsed_ms(url_started),
                input_url=target_url,
                extractor=effective_extractor,
                max_chars=effective_max_chars,
                status=outcome_status,
                error_kind=err_kind,
                error_message=err_msg,
                payload=payload,
                extractor_used=used,
                truncated=trunc,
                egress=egress,
                settings=settings,
                proxy_url=proxy_url,
            )

    outcomes = list(await asyncio.gather(*(_fetch_one(u) for u in urls)))
    succeeded = sum(1 for o in outcomes if o.error is None)
    failed = len(outcomes) - succeeded
    first_error = next((o.error for o in outcomes if o.error is not None), None)
    record_rest_web_tool_call(
        settings,
        request_id=request_id,
        endpoint=request.url.path,
        protocol="rest_webfetch",
        optimization="web_fetch",
        headers=request.headers,
        duration_ms=_elapsed_ms(started),
        status="success" if succeeded > 0 else "error",
        raw_input="\n".join(urls),
        raw_output="\n".join(o.final_url or o.url for o in outcomes if o.error is None)
        or None,
        params={
            "urls_total": len(urls),
            "succeeded": succeeded,
            "failed": failed,
        },
        error_kind="invalid_request" if succeeded == 0 and failed > 0 else None,
        error_message=first_error if succeeded == 0 else None,
    )
    return outcomes
