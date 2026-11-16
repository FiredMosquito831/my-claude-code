"""Standalone REST web search, dialect shims, and web fetch HTTP endpoints."""

import json
import secrets
import time
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from my_claude_code.config.settings import Settings, get_settings
from my_claude_code.core.openai_common import openai_error_payload
from my_claude_code.core.websearch.dialects import (
    BraveSearchQuery,
    DeepSeekFetchRequest,
    DeepSeekSearchRequest,
    ExaContentsRequest,
    ExaSearchRequest,
    SearxngSearchQuery,
    TavilyExtractRequest,
    TavilySearchRequest,
    to_brave_search_payload,
    to_deepseek_fetch_payload,
    to_deepseek_search_payload,
    to_exa_contents_payload,
    to_exa_search_payload,
    to_searxng_search_payload,
    to_tavily_extract_payload,
    to_tavily_search_payload,
    translate_websearch_response,
)
from my_claude_code.core.websearch.rest_models import (
    WebFetchRestRequest,
    WebFetchRestResponse,
    WebSearchRestRequest,
    WebSearchRestResponse,
)

from .dependencies import get_services, require_proxy_auth
from .ports import ApiServices
from .rejected_client_log import describe_rejected_client, rejected_client_tracker
from .request_ids import attach_request_id_headers, get_request_id
from .wire_surfaces import (
    BRAVE_SEARCH_COMPAT_ENDPOINT,
    SEARXNG_SEARCH_COMPAT_ENDPOINT,
    WEBFETCH_ENDPOINT,
    WEBSEARCH_ENDPOINT,
)

websearch_router = APIRouter()


def _probe_response(allow: str) -> Response:
    return Response(status_code=204, headers={"Allow": allow})


def _has_standard_proxy_credential(request: Request) -> bool:
    return (
        bool(request.headers.get("authorization"))
        or request.headers.get("x-api-key") is not None
        or request.headers.get("x-goog-api-key") is not None
        or (
            "query_string" in request.scope
            and request.query_params.get("key") is not None
        )
    )


def _reject_unauthenticated(request: Request, detail: str) -> HTTPException:
    rejected_client_tracker().note_rejection(describe_rejected_client(request))
    return HTTPException(status_code=401, detail=detail)


def require_brave_proxy_auth(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> None:
    """Authenticate Brave dialect callers using standard proxy headers or ``X-Subscription-Token``."""

    configured = settings.anthropic_auth_token.strip()
    if not configured:
        return
    if _has_standard_proxy_credential(request):
        require_proxy_auth(request, settings)
        return
    token = request.headers.get("x-subscription-token")
    if token is None:
        raise _reject_unauthenticated(request, "Missing proxy authentication token")
    stripped = token.strip()
    if not stripped or not secrets.compare_digest(
        stripped.encode("utf-8"),
        configured.encode("utf-8"),
    ):
        raise _reject_unauthenticated(request, "Invalid proxy authentication token")


async def require_tavily_proxy_auth(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> None:
    """Authenticate Tavily dialect callers using standard proxy headers or JSON body ``api_key``."""

    configured = settings.anthropic_auth_token.strip()
    if not configured:
        return
    if _has_standard_proxy_credential(request):
        require_proxy_auth(request, settings)
        return
    body_token: str | None = None
    try:
        raw_body = await request.body()
    except Exception:
        raw_body = b""
    if raw_body:
        try:
            parsed = json.loads(raw_body)
            if isinstance(parsed, Mapping) and "api_key" in parsed:
                candidate = parsed.get("api_key")
                if isinstance(candidate, str):
                    body_token = candidate.strip()
                elif candidate is not None:
                    body_token = ""
        except ValueError, UnicodeDecodeError:
            body_token = None
    if body_token is None:
        raise _reject_unauthenticated(request, "Missing proxy authentication token")
    if not body_token or not secrets.compare_digest(
        body_token.encode("utf-8"),
        configured.encode("utf-8"),
    ):
        raise _reject_unauthenticated(request, "Invalid proxy authentication token")


@websearch_router.head(WEBSEARCH_ENDPOINT)
@websearch_router.options(WEBSEARCH_ENDPOINT)
@websearch_router.head(WEBFETCH_ENDPOINT)
@websearch_router.options(WEBFETCH_ENDPOINT)
@websearch_router.head(WEBSEARCH_ENDPOINT + "/tavily")
@websearch_router.options(WEBSEARCH_ENDPOINT + "/tavily")
@websearch_router.head(WEBSEARCH_ENDPOINT + "/tavily/search")
@websearch_router.options(WEBSEARCH_ENDPOINT + "/tavily/search")
@websearch_router.head(WEBSEARCH_ENDPOINT + "/tavily/extract")
@websearch_router.options(WEBSEARCH_ENDPOINT + "/tavily/extract")
@websearch_router.head(WEBSEARCH_ENDPOINT + "/exa")
@websearch_router.options(WEBSEARCH_ENDPOINT + "/exa")
@websearch_router.head(WEBSEARCH_ENDPOINT + "/exa/search")
@websearch_router.options(WEBSEARCH_ENDPOINT + "/exa/search")
@websearch_router.head(WEBSEARCH_ENDPOINT + "/exa/contents")
@websearch_router.options(WEBSEARCH_ENDPOINT + "/exa/contents")
@websearch_router.head(WEBSEARCH_ENDPOINT + "/deepseek")
@websearch_router.options(WEBSEARCH_ENDPOINT + "/deepseek")
@websearch_router.head(WEBSEARCH_ENDPOINT + "/deepseek/tools/search")
@websearch_router.options(WEBSEARCH_ENDPOINT + "/deepseek/tools/search")
@websearch_router.head(WEBSEARCH_ENDPOINT + "/deepseek/v1/tools/search")
@websearch_router.options(WEBSEARCH_ENDPOINT + "/deepseek/v1/tools/search")
@websearch_router.head(WEBSEARCH_ENDPOINT + "/deepseek/tools/fetch")
@websearch_router.options(WEBSEARCH_ENDPOINT + "/deepseek/tools/fetch")
@websearch_router.head(WEBSEARCH_ENDPOINT + "/deepseek/v1/tools/fetch")
@websearch_router.options(WEBSEARCH_ENDPOINT + "/deepseek/v1/tools/fetch")
async def websearch_post_probe() -> Response:
    """Allow proxy capability probes on POST REST websearch/webfetch routes."""
    return _probe_response("POST, HEAD, OPTIONS")


@websearch_router.head(WEBSEARCH_ENDPOINT + "/brave")
@websearch_router.options(WEBSEARCH_ENDPOINT + "/brave")
@websearch_router.head(BRAVE_SEARCH_COMPAT_ENDPOINT)
@websearch_router.options(BRAVE_SEARCH_COMPAT_ENDPOINT)
@websearch_router.head(WEBSEARCH_ENDPOINT + "/searxng")
@websearch_router.options(WEBSEARCH_ENDPOINT + "/searxng")
@websearch_router.head(SEARXNG_SEARCH_COMPAT_ENDPOINT)
@websearch_router.options(SEARXNG_SEARCH_COMPAT_ENDPOINT)
async def websearch_get_probe() -> Response:
    """Allow proxy capability probes on GET dialect websearch routes."""
    return _probe_response("GET, HEAD, OPTIONS")


def _rest_error_response(
    request: Request,
    *,
    request_id: str,
    status_code: int,
    message: str,
    error_type: str,
    param: str | None = None,
    code: str | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    payload = openai_error_payload(
        message=message,
        error_type=error_type,
        param=param,
    )
    if code is not None:
        payload["error"]["code"] = code
    response = JSONResponse(status_code=status_code, content=payload, headers=headers)
    attach_request_id_headers(response, request_id=request_id, path=request.url.path)
    return response


def _validation_error_response(
    request: Request,
    exc: ValidationError,
    *,
    request_id: str,
) -> JSONResponse:
    errors = exc.errors()
    parts: list[str] = []
    first_param: str | None = None
    for item in errors[:3]:
        loc = ".".join(str(seg) for seg in item.get("loc", ()) if seg is not None)
        if first_param is None and loc:
            first_param = loc
        msg = str(item.get("msg", "Invalid input"))
        parts.append(f"{loc}: {msg}" if loc else msg)
    if len(errors) > 3:
        parts.append(f"and {len(errors) - 3} more")
    message = "; ".join(parts) if parts else "Invalid request parameters"
    return _rest_error_response(
        request,
        request_id=request_id,
        status_code=400,
        message=message,
        error_type="invalid_request_error",
        param=first_param,
        code="invalid_request",
    )


async def _run_rest_search(
    request: Request,
    request_data: WebSearchRestRequest,
    settings: Settings,
    services: ApiServices,
) -> WebSearchRestResponse | JSONResponse:
    from .web_tools import rest_service

    request_id = get_request_id(request)
    try:
        return await rest_service.execute_websearch(
            request,
            request_data,
            settings,
            request_id=request_id,
            services=services,
        )
    except rest_service.WebSearchRestError as exc:
        return _rest_error_response(
            request,
            request_id=request_id,
            status_code=exc.status_code,
            message=exc.message,
            error_type=exc.error_type,
            param=exc.param,
            code=exc.code,
            headers=exc.headers,
        )


async def _run_rest_fetch(
    request: Request,
    request_data: WebFetchRestRequest,
    settings: Settings,
) -> WebFetchRestResponse | JSONResponse:
    from .web_tools import rest_service

    request_id = get_request_id(request)
    try:
        return await rest_service.execute_webfetch(
            request,
            request_data,
            settings,
            request_id=request_id,
        )
    except rest_service.WebSearchRestError as exc:
        return _rest_error_response(
            request,
            request_id=request_id,
            status_code=exc.status_code,
            message=exc.message,
            error_type=exc.error_type,
            param=exc.param,
            code=exc.code,
            headers=exc.headers,
        )


@websearch_router.post(WEBSEARCH_ENDPOINT, response_model=None)
async def websearch_endpoint(
    request: Request,
    request_data: WebSearchRestRequest,
    _auth: None = Depends(require_proxy_auth),
    settings: Settings = Depends(get_settings),
    services: ApiServices = Depends(get_services),
) -> WebSearchRestResponse | dict[str, Any] | JSONResponse:
    """Execute a web search using MCC's configured dashboard providers."""

    outcome = await _run_rest_search(request, request_data, settings, services)
    if isinstance(outcome, JSONResponse):
        return outcome
    if request_data.format in ("mcc", "digest"):
        return outcome
    return translate_websearch_response(
        outcome,
        request_data.format,
        need_content=bool(request_data.include_content),
    )


@websearch_router.post(WEBFETCH_ENDPOINT, response_model=WebFetchRestResponse)
async def webfetch_endpoint(
    request: Request,
    request_data: WebFetchRestRequest,
    _auth: None = Depends(require_proxy_auth),
    settings: Settings = Depends(get_settings),
) -> WebFetchRestResponse | JSONResponse:
    """Fetch and extract a single web page via MCC's SSRF-pinned fetch stack."""

    return await _run_rest_fetch(request, request_data, settings)


@websearch_router.post(WEBSEARCH_ENDPOINT + "/tavily")
@websearch_router.post(WEBSEARCH_ENDPOINT + "/tavily/search")
async def tavily_search_endpoint(
    request: Request,
    request_data: TavilySearchRequest,
    _auth: None = Depends(require_tavily_proxy_auth),
    settings: Settings = Depends(get_settings),
    services: ApiServices = Depends(get_services),
) -> JSONResponse:
    """Drop-in Tavily ``POST /search`` dialect endpoint backed by MCC."""

    rest_request = request_data.to_rest_search_request()
    outcome = await _run_rest_search(request, rest_request, settings, services)
    if isinstance(outcome, JSONResponse):
        return outcome
    return JSONResponse(
        content=to_tavily_search_payload(
            outcome,
            include_images=request_data.include_images,
        )
    )


@websearch_router.post(WEBSEARCH_ENDPOINT + "/tavily/extract")
async def tavily_extract_endpoint(
    request: Request,
    request_data: TavilyExtractRequest,
    _auth: None = Depends(require_tavily_proxy_auth),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    """Drop-in Tavily ``POST /extract`` batch extraction endpoint."""

    from .web_tools import rest_service

    request_id = get_request_id(request)
    started = time.perf_counter()
    try:
        outcomes = await rest_service.execute_batch_webfetch(
            request,
            urls=request_data.normalized_urls(),
            settings=settings,
            request_id=request_id,
            extractor=request_data.effective_extractor(),
        )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        return JSONResponse(
            content=to_tavily_extract_payload(
                outcomes,
                request_id=request_id,
                elapsed_ms=elapsed_ms,
            )
        )
    except rest_service.WebSearchRestError as exc:
        return _rest_error_response(
            request,
            request_id=request_id,
            status_code=exc.status_code,
            message=exc.message,
            error_type=exc.error_type,
            param=exc.param,
            code=exc.code,
            headers=exc.headers,
        )


@websearch_router.post(WEBSEARCH_ENDPOINT + "/exa")
@websearch_router.post(WEBSEARCH_ENDPOINT + "/exa/search")
async def exa_search_endpoint(
    request: Request,
    request_data: ExaSearchRequest,
    _auth: None = Depends(require_proxy_auth),
    settings: Settings = Depends(get_settings),
    services: ApiServices = Depends(get_services),
) -> JSONResponse:
    """Drop-in Exa ``POST /search`` dialect endpoint backed by MCC."""

    rest_request = request_data.to_rest_search_request()
    outcome = await _run_rest_search(request, rest_request, settings, services)
    if isinstance(outcome, JSONResponse):
        return outcome
    return JSONResponse(
        content=to_exa_search_payload(
            outcome,
            resolved_search_type=request_data.type,
        )
    )


@websearch_router.post(WEBSEARCH_ENDPOINT + "/exa/contents")
async def exa_contents_endpoint(
    request: Request,
    request_data: ExaContentsRequest,
    _auth: None = Depends(require_proxy_auth),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    """Drop-in Exa ``POST /contents`` batch extraction endpoint."""

    from .web_tools import rest_service

    request_id = get_request_id(request)
    try:
        outcomes = await rest_service.execute_batch_webfetch(
            request,
            urls=request_data.normalized_urls(),
            settings=settings,
            request_id=request_id,
            max_chars=request_data.effective_max_chars(),
        )
        return JSONResponse(
            content=to_exa_contents_payload(
                outcomes,
                request_id=request_id,
            )
        )
    except rest_service.WebSearchRestError as exc:
        return _rest_error_response(
            request,
            request_id=request_id,
            status_code=exc.status_code,
            message=exc.message,
            error_type=exc.error_type,
            param=exc.param,
            code=exc.code,
            headers=exc.headers,
        )


@websearch_router.get(WEBSEARCH_ENDPOINT + "/brave")
@websearch_router.get(BRAVE_SEARCH_COMPAT_ENDPOINT)
async def brave_search_endpoint(
    request: Request,
    q: str | None = Query(default=None),
    count: int | None = Query(default=None),
    freshness: str | None = Query(default=None),
    country: str | None = Query(default=None),
    search_lang: str | None = Query(default=None),
    extra_snippets: bool | None = Query(default=None),
    provider: str | None = Query(default=None),
    _auth: None = Depends(require_brave_proxy_auth),
    settings: Settings = Depends(get_settings),
    services: ApiServices = Depends(get_services),
) -> JSONResponse:
    """Drop-in Brave ``GET /res/v1/web/search`` dialect endpoint backed by MCC."""

    request_id = get_request_id(request)
    try:
        query_model = BraveSearchQuery.model_validate(
            {
                "q": q,
                "count": count,
                "freshness": freshness,
                "country": country,
                "search_lang": search_lang,
                "extra_snippets": extra_snippets,
                "provider": provider,
            }
        )
    except ValidationError as exc:
        return _validation_error_response(request, exc, request_id=request_id)

    outcome = await _run_rest_search(
        request,
        query_model.to_rest_search_request(),
        settings,
        services,
    )
    if isinstance(outcome, JSONResponse):
        return outcome
    return JSONResponse(content=to_brave_search_payload(outcome))


@websearch_router.post(WEBSEARCH_ENDPOINT + "/deepseek")
@websearch_router.post(WEBSEARCH_ENDPOINT + "/deepseek/tools/search")
@websearch_router.post(WEBSEARCH_ENDPOINT + "/deepseek/v1/tools/search")
async def deepseek_search_endpoint(
    request: Request,
    request_data: DeepSeekSearchRequest,
    _auth: None = Depends(require_proxy_auth),
    settings: Settings = Depends(get_settings),
    services: ApiServices = Depends(get_services),
) -> JSONResponse:
    """Drop-in DeepSeek ``POST /v1/tools/search`` dialect endpoint backed by MCC."""

    outcome = await _run_rest_search(
        request,
        request_data.to_rest_search_request(),
        settings,
        services,
    )
    if isinstance(outcome, JSONResponse):
        return outcome
    return JSONResponse(
        content=to_deepseek_search_payload(
            outcome,
            need_content=request_data.need_content,
        )
    )


@websearch_router.post(WEBSEARCH_ENDPOINT + "/deepseek/tools/fetch")
@websearch_router.post(WEBSEARCH_ENDPOINT + "/deepseek/v1/tools/fetch")
async def deepseek_fetch_endpoint(
    request: Request,
    request_data: DeepSeekFetchRequest,
    _auth: None = Depends(require_proxy_auth),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    """Drop-in DeepSeek ``POST /v1/tools/fetch`` dialect endpoint backed by MCC."""

    outcome = await _run_rest_fetch(
        request,
        request_data.to_rest_fetch_request(),
        settings,
    )
    if isinstance(outcome, JSONResponse):
        return outcome
    return JSONResponse(content=to_deepseek_fetch_payload(outcome))


@websearch_router.get(WEBSEARCH_ENDPOINT + "/searxng")
@websearch_router.get(SEARXNG_SEARCH_COMPAT_ENDPOINT)
async def searxng_search_endpoint(
    request: Request,
    q: str | None = Query(default=None),
    format: str | None = Query(default="json"),
    time_range: str | None = Query(default=None),
    language: str | None = Query(default=None),
    categories: str | None = Query(default=None),
    engines: str | None = Query(default=None),
    pageno: int = Query(default=1),
    max_results: int | None = Query(default=None),
    provider: str | None = Query(default=None),
    _auth: None = Depends(require_proxy_auth),
    settings: Settings = Depends(get_settings),
    services: ApiServices = Depends(get_services),
) -> JSONResponse:
    """Drop-in SearXNG ``GET /search?format=json`` dialect endpoint (100% original MCC)."""

    request_id = get_request_id(request)
    try:
        query_model = SearxngSearchQuery.model_validate(
            {
                "q": q,
                "format": format,
                "time_range": time_range,
                "language": language,
                "categories": categories,
                "engines": engines,
                "pageno": pageno,
                "max_results": max_results,
                "provider": provider,
            }
        )
    except ValidationError as exc:
        return _validation_error_response(request, exc, request_id=request_id)

    outcome = await _run_rest_search(
        request,
        query_model.to_rest_search_request(),
        settings,
        services,
    )
    if isinstance(outcome, JSONResponse):
        return outcome
    return JSONResponse(
        content=to_searxng_search_payload(
            outcome,
            category=query_model.categories,
        )
    )
