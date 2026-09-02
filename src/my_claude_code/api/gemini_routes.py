"""FastAPI routes for the inbound Google Gemini surface.

Its own module rather than three more entries in ``routes.py``, because the
path shape is unlike every other route this proxy serves and the reason is
worth stating once where it is easy to find.

Google puts the *method* in the path after a colon and the model before it --
``POST /v1beta/models/gemini-3-pro:streamGenerateContent`` -- and MCC's routable
ids contain slashes, so the model segment is ``anthropic/openrouter/gpt-5``
rather than one path component. Both survive the wire unescaped: the bundled
``@google/genai`` client joins the path as a plain string and hands it to
``new URL()``, which percent-encodes neither ``/`` nor ``:`` in a path
(``constructUrl``/``tModel``, Gemini CLI 0.49.0). So the route matches the whole
tail greedily and splits it here, from the right, in
``core/gemini_api/paths.py``.

Route order in this module is therefore load-bearing: the exact ``GET
/v1beta/models`` collection route is declared before the greedy describe route
that would otherwise swallow it.
"""

from collections.abc import Mapping

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from my_claude_code.application.errors import ApplicationError
from my_claude_code.application.ports import ProviderResolver, RequestRuntimeLease
from my_claude_code.application.routing import ModelRouter
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic import MessagesRequest, get_token_count
from my_claude_code.core.gemini_api import (
    COUNT_TOKENS,
    GENERATE_CONTENT,
    STREAM_GENERATE_CONTENT,
    GeminiApiAdapter,
    GeminiGenerateContentRequest,
    gemini_count_tokens_payload,
    gemini_error_payload,
    parse_model_method_path,
    strip_models_prefix,
)
from my_claude_code.core.trace import trace_event

from .dependencies import (
    get_services,
    get_settings,
    require_proxy_auth,
    resolve_provider,
)
from .gemini_model_catalog import build_gemini_models_payload, find_gemini_model_entry
from .handlers import GeminiHandler
from .ports import ApiServices
from .request_errors import ordinary_application_error_response
from .request_ids import get_request_id
from .response_streams import bind_response_lifetime
from .wire_surfaces import GEMINI_ENDPOINT_PREFIX

gemini_router = APIRouter()


def _provider_resolver(lease: RequestRuntimeLease) -> ProviderResolver:
    return lambda provider_type: resolve_provider(provider_type, lease=lease)


def _model_router(services: ApiServices, lease: RequestRuntimeLease) -> ModelRouter:
    """Build a router that can see what the cached models actually accept."""

    return ModelRouter(
        lease.settings,
        vision_lookup=services.requests.cached_model_supports_vision,
        reasoning_capability_lookup=services.requests.model_reasoning_capability,
        reasoning_dialect_lookup=services.requests.model_reasoning_dialect,
        output_limit_lookup=services.requests.model_output_limit,
        context_length_lookup=services.requests.model_context_length,
    )


def _not_found(message: str) -> JSONResponse:
    return JSONResponse(
        status_code=404, content=gemini_error_payload(message=message, code=404)
    )


def _probe_response(allow: str) -> Response:
    return Response(status_code=204, headers={"Allow": allow})


async def _create_gemini_response(
    services: ApiServices,
    request_data: GeminiGenerateContentRequest,
    *,
    endpoint: str,
    stream: bool,
    request_id: str,
    headers: Mapping[str, str] | None = None,
) -> object:
    lease: RequestRuntimeLease | None = None
    try:
        lease = await services.requests.acquire()
        handler = GeminiHandler(
            lease.settings,
            provider_resolver=_provider_resolver(lease),
            model_router=_model_router(services, lease),
            generation_id=lease.generation_id,
        )
        response = await handler.create(
            request_data,
            endpoint=endpoint,
            stream=stream,
            request_id=request_id,
            headers=headers,
        )
    except ApplicationError as exc:
        if lease is not None:
            await lease.release()
        return ordinary_application_error_response(
            exc,
            wire_api="gemini",
            request_id=request_id,
        )
    except BaseException:
        if lease is not None:
            await lease.release()
        raise
    assert lease is not None
    return await bind_response_lifetime(response, lease.release)


@gemini_router.get(GEMINI_ENDPOINT_PREFIX)
async def list_gemini_models(
    services: ApiServices = Depends(get_services),
    settings: Settings = Depends(get_settings),
    _auth=Depends(require_proxy_auth),
):
    """List every routable model in Google's ``Model`` resource shape."""

    trace_event(
        stage="ingress", event="my_claude_code.api.gemini.models.list", source="api"
    )
    return build_gemini_models_payload(settings, services.requests)


@gemini_router.api_route(GEMINI_ENDPOINT_PREFIX, methods=["HEAD", "OPTIONS"])
async def probe_gemini_models(_auth=Depends(require_proxy_auth)):
    return _probe_response("GET, HEAD, OPTIONS")


@gemini_router.post(GEMINI_ENDPOINT_PREFIX + "/{model_method:path}")
async def gemini_generate_content(
    request: Request,
    model_method: str,
    request_data: GeminiGenerateContentRequest,
    services: ApiServices = Depends(get_services),
    settings: Settings = Depends(get_settings),
    _auth=Depends(require_proxy_auth),
):
    """Serve ``:generateContent``, ``:streamGenerateContent`` and ``:countTokens``.

    ``?alt=sse`` is not what decides whether the answer streams: the *method*
    does. ``@google/genai`` force-sets ``alt=sse`` on its streaming call and
    omits it on the unary one, but a hand-built request that asks for
    ``:streamGenerateContent`` without the parameter still wants a stream, and
    one that asks for ``:generateContent`` with it still wants a single JSON
    body. Reading the method rather than the query is the only reading that is
    right for both.
    """

    parsed = parse_model_method_path(model_method)
    if parsed is None:
        return _not_found(
            f"Unknown path: /v1beta/models/{model_method}. Gemini methods are "
            "named after a colon, e.g. models/<model>:generateContent."
        )
    if parsed.method == COUNT_TOKENS:
        return _count_tokens(request_data.with_model(parsed.model), settings=settings)
    if parsed.method not in {GENERATE_CONTENT, STREAM_GENERATE_CONTENT}:
        return _not_found(
            f"Unsupported method: {parsed.method}. MCC serves generateContent, "
            "streamGenerateContent and countTokens."
        )

    return await _create_gemini_response(
        services,
        request_data.with_model(parsed.model),
        endpoint=f"{GEMINI_ENDPOINT_PREFIX}/{parsed.model}:{parsed.method}",
        stream=parsed.method == STREAM_GENERATE_CONTENT,
        request_id=get_request_id(request),
        headers=request.headers,
    )


@gemini_router.api_route(
    GEMINI_ENDPOINT_PREFIX + "/{model_method:path}", methods=["HEAD", "OPTIONS"]
)
async def probe_gemini_model(_auth=Depends(require_proxy_auth)):
    return _probe_response("GET, POST, HEAD, OPTIONS")


@gemini_router.get(GEMINI_ENDPOINT_PREFIX + "/{model_path:path}")
async def describe_gemini_model(
    model_path: str,
    services: ApiServices = Depends(get_services),
    settings: Settings = Depends(get_settings),
    _auth=Depends(require_proxy_auth),
):
    """Describe one model, the way ``GET /v1beta/models/{model}`` does."""

    model_id = strip_models_prefix(model_path)
    entry = find_gemini_model_entry(settings, services.requests, model_id)
    if entry is None:
        return _not_found(f"Model not found: models/{model_id}")
    return entry


def _count_tokens(
    request_data: GeminiGenerateContentRequest, *, settings: Settings
) -> JSONResponse:
    """Answer ``:countTokens`` from the same estimator ``/v1/messages`` uses.

    Gemini CLI calls this only when a turn carries ``inlineData`` or
    ``fileData``, and falls back to its own local estimate when the call
    fails -- so an approximate answer is strictly better than an error, and a
    conversion failure here degrades to zero rather than to a 400 the client
    would have to interpret.
    """

    adapter = GeminiApiAdapter()
    try:
        conversion = adapter.to_anthropic_payload(request_data)
        payload = dict(conversion.payload)
        payload["model"] = request_data.model or "count-tokens"
        counted = MessagesRequest(**payload)
        tokens = get_token_count(counted.messages, counted.system, counted.tools)
    except GeminiApiAdapter.ConversionError:
        tokens = 0
    trace_event(
        stage="ingress",
        event="my_claude_code.api.gemini.count_tokens.completed",
        source="api",
        model=request_data.model,
        input_tokens=tokens,
    )
    del settings
    return JSONResponse(content=gemini_count_tokens_payload(tokens))
