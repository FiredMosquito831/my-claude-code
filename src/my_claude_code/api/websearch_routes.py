"""Standalone REST web search and web fetch HTTP endpoints."""

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from my_claude_code.config.settings import Settings, get_settings
from my_claude_code.core.openai_common import openai_error_payload
from my_claude_code.core.websearch.rest_models import (
    WebFetchRestRequest,
    WebFetchRestResponse,
    WebSearchRestRequest,
    WebSearchRestResponse,
)

from .dependencies import require_proxy_auth
from .request_ids import attach_request_id_headers, get_request_id
from .wire_surfaces import WEBFETCH_ENDPOINT, WEBSEARCH_ENDPOINT

websearch_router = APIRouter()


def _probe_response(allow: str) -> Response:
    return Response(status_code=204, headers={"Allow": allow})


@websearch_router.head(WEBSEARCH_ENDPOINT)
@websearch_router.options(WEBSEARCH_ENDPOINT)
async def websearch_probe() -> Response:
    """Allow proxy capability probes on ``/v1/websearch``."""
    return _probe_response("POST, HEAD, OPTIONS")


@websearch_router.head(WEBFETCH_ENDPOINT)
@websearch_router.options(WEBFETCH_ENDPOINT)
async def webfetch_probe() -> Response:
    """Allow proxy capability probes on ``/v1/webfetch``."""
    return _probe_response("POST, HEAD, OPTIONS")


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


@websearch_router.post(WEBSEARCH_ENDPOINT, response_model=WebSearchRestResponse)
async def websearch_endpoint(
    request: Request,
    request_data: WebSearchRestRequest,
    _auth: None = Depends(require_proxy_auth),
    settings: Settings = Depends(get_settings),
) -> WebSearchRestResponse | JSONResponse:
    """Execute a web search using MCC's configured dashboard providers."""

    from .web_tools import rest_service

    request_id = get_request_id(request)
    try:
        return await rest_service.execute_websearch(
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


@websearch_router.post(WEBFETCH_ENDPOINT, response_model=WebFetchRestResponse)
async def webfetch_endpoint(
    request: Request,
    request_data: WebFetchRestRequest,
    _auth: None = Depends(require_proxy_auth),
    settings: Settings = Depends(get_settings),
) -> WebFetchRestResponse | JSONResponse:
    """Fetch and extract a single web page via MCC's SSRF-pinned fetch stack."""

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
