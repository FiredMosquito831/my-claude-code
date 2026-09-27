"""OpenAI-compatible media endpoints (7.60.0: image generation).

``POST /v1/images/generations`` routes on the Image rail (``MODEL_IMAGE`` +
``MODEL_IMAGE_FALLBACKS``, minus ``MODEL_IMAGE_PAUSED``) through the media
executor -- a separate copy of the chat executor's rules with its own health
books. Every request takes a runtime lease like the chat surfaces, so graceful
shutdown drains it, and writes one request-log row.

A non-streaming request is answered only once a model has produced the whole
answer, so any failure before that falls back invisibly. A streaming request
commits on the first forwarded event; an upstream failure after that ends the
stream with an OpenAI ``error`` event.
"""

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from my_claude_code.application.errors import ApplicationError
from my_claude_code.application.execution import route_execution_policy
from my_claude_code.application.media.executor import (
    MediaExecutor,
    media_route_health_registry,
)
from my_claude_code.application.media.ports import MediaProviderResolver
from my_claude_code.application.media.rails import MediaRouter, chat_probe_candidates
from my_claude_code.application.media.request import (
    MediaChunk,
    MediaRail,
    MediaRequest,
    MediaResponse,
)
from my_claude_code.application.ports import RequestRuntimeLease
from my_claude_code.config.media_surfaces import MEDIA_OPERATION_IMAGE_GENERATE
from my_claude_code.core.diagnostics import safe_exception_message
from my_claude_code.core.failures import (
    ExecutionFailure,
    FailureKind,
    failure_kind,
    find_execution_failure,
)
from my_claude_code.core.openai_common.errors import (
    openai_error_payload,
    openai_error_type_for_failure,
)
from my_claude_code.core.openai_images import (
    ImageOutputs,
    images_error_frame,
    parse_images_response,
    parse_images_stream,
)

from .dependencies import get_services, require_proxy_auth, resolve_provider
from .media_capture import MediaCapture
from .ports import ApiServices
from .request_ids import get_request_id
from .response_streams import ManagedStreamingResponse, bind_response_lifetime
from .wire_surfaces import IMAGES_GENERATIONS_ENDPOINT

router = APIRouter()


def _error_response(
    status_code: int, message: str, kind: FailureKind | ExecutionFailure
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=openai_error_payload(
            message=message, error_type=openai_error_type_for_failure(kind)
        ),
    )


def _failure_response(exc: BaseException) -> JSONResponse:
    """The OpenAI error envelope for whatever ended the route."""
    execution = find_execution_failure(exc)
    if execution is not None:
        return _error_response(execution.status_code, execution.message, execution)
    if isinstance(exc, ApplicationError):
        # As an ExecutionFailure, so a 404 reads as not_found_error the way
        # the chat surfaces' own mapping reads it.
        as_failure = ExecutionFailure(
            kind=exc.kind,
            status_code=exc.status_code,
            message=exc.message,
            retryable=False,
        )
        return _error_response(exc.status_code, exc.message, as_failure)
    kind = failure_kind(exc) or FailureKind.UPSTREAM
    return _error_response(502, safe_exception_message(exc), kind)


def _commit_error_frames(exc: BaseException) -> Sequence[bytes]:
    execution = find_execution_failure(exc)
    kind: FailureKind | ExecutionFailure = (
        execution
        if execution is not None
        else (failure_kind(exc) or FailureKind.UPSTREAM)
    )
    return (
        images_error_frame(
            safe_exception_message(exc), openai_error_type_for_failure(kind)
        ),
    )


def _throttle_lookup(resolver: MediaProviderResolver):
    def lookup(provider_id: str) -> float | None:
        try:
            return resolver(provider_id).throttle_remaining()
        except Exception:
            return None

    return lookup


def _executor(
    lease: RequestRuntimeLease, resolver: MediaProviderResolver
) -> MediaExecutor:
    settings = lease.settings
    return MediaExecutor(
        resolver,
        policy=route_execution_policy(settings),
        health=media_route_health_registry(settings),
        retry_first=settings.fallback_retry_first,
        provider_lookup=_throttle_lookup(resolver),
        chat_provider_resolver=lambda provider_id: resolve_provider(
            provider_id, lease=lease
        ),
    )


async def _read_json_object(request: Request) -> Mapping[str, Any] | None:
    try:
        payload = await request.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    return {str(key): value for key, value in payload.items()}


@router.post(IMAGES_GENERATIONS_ENDPOINT)
async def create_image(
    request: Request,
    services: ApiServices = Depends(get_services),
    _auth=Depends(require_proxy_auth),
):
    """Generate images through the Image rail (OpenAI ``images.generate``)."""
    request_id = get_request_id(request)
    payload = await _read_json_object(request)
    if payload is None:
        return _error_response(
            400, "The request body must be a JSON object.", FailureKind.INVALID_REQUEST
        )
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return _error_response(
            400,
            "'prompt' is required and must be a string.",
            FailureKind.INVALID_REQUEST,
        )
    media = services.media
    if media is None:
        return _error_response(
            503,
            "Media routing is not available in this server.",
            FailureKind.UNAVAILABLE,
        )
    media_request = MediaRequest(
        operation=MEDIA_OPERATION_IMAGE_GENERATE,
        rail=MediaRail.IMAGE,
        model=str(payload.get("model") or ""),
        body={key: value for key, value in payload.items() if key != "model"},
        stream=payload.get("stream") is True,
    )
    lease: RequestRuntimeLease | None = None
    capture: MediaCapture | None = None
    try:
        lease = await services.requests.acquire()
        settings = lease.settings
        capture = MediaCapture(
            settings,
            request_id=request_id,
            endpoint=IMAGES_GENERATIONS_ENDPOINT,
            request=media_request,
            headers=request.headers,
        )
        plan = MediaRouter(
            settings, probe_candidates=lambda: chat_probe_candidates(settings)
        ).plan(media_request)
        capture.set_plan(plan)
        stream = _executor(lease, media.resolver(settings)).execute(
            plan,
            request_id=request_id,
            on_attempt=capture.on_attempt,
            on_attempt_result=capture.record_attempt_result,
            commit_error_frames=_commit_error_frames,
        )
        first = await anext(stream)
    except StopAsyncIteration as exc:
        if lease is not None:
            await lease.release()
        if capture is not None:
            await capture.finish("error", error=exc)
        return _error_response(
            502, "The provider returned an empty answer.", FailureKind.UPSTREAM
        )
    except Exception as exc:
        if lease is not None:
            await lease.release()
        if capture is not None:
            await capture.finish("error", error=exc)
        return _failure_response(exc)
    except BaseException:
        if lease is not None:
            await lease.release()
        if capture is not None:
            await capture.finish("cancelled")
        raise

    if not media_request.stream:
        return await _complete_response(first, stream, lease, capture)
    response = ManagedStreamingResponse(
        _stream_body(first, stream, capture), media_type="text/event-stream"
    )
    return await bind_response_lifetime(response, lease.release)


async def _complete_response(
    first: MediaChunk,
    stream: AsyncIterator[MediaChunk],
    lease: RequestRuntimeLease,
    capture: MediaCapture,
) -> Response:
    """Serve a buffered answer; the lease is released before returning."""
    try:
        async for _extra in stream:
            pass
    finally:
        await lease.release()
    if not isinstance(first, MediaResponse):
        await capture.finish("error", error=RuntimeError("unexpected stream frame"))
        return _error_response(
            502,
            "The provider streamed an answer that was not asked for.",
            FailureKind.UPSTREAM,
        )
    outputs: ImageOutputs = await asyncio.to_thread(parse_images_response, first.body)
    await capture.finish("success", outputs=outputs)
    return Response(
        content=first.body,
        status_code=first.status_code,
        media_type=first.content_type or "application/json",
    )


async def _stream_body(
    first: MediaChunk,
    stream: AsyncIterator[MediaChunk],
    capture: MediaCapture,
) -> AsyncIterator[bytes]:
    """Forward SSE frames; measure the completed images once the stream ends.

    The row is written from a task rather than awaited here: a client that
    hangs up closes this generator with ``GeneratorExit``, and awaiting during
    that is unsafe.
    """
    seen = bytearray()
    status = "success"
    error: BaseException | None = None
    try:
        data = first.body if isinstance(first, MediaResponse) else first
        seen.extend(data)
        yield data
        async for chunk in stream:
            data = chunk.body if isinstance(chunk, MediaResponse) else chunk
            seen.extend(data)
            yield data
    except Exception as exc:
        status, error = "error", exc
        yield _commit_error_frames(exc)[0]
    except BaseException:
        status = "cancelled"
        raise
    finally:
        if status == "success" and b"event: error" in seen:
            status = "error"
        asyncio.ensure_future(_finish_stream(capture, status, error, bytes(seen)))


async def _finish_stream(
    capture: MediaCapture, status: str, error: BaseException | None, seen: bytes
) -> None:
    outputs = await asyncio.to_thread(parse_images_stream, seen)
    if status == "success":
        await capture.finish("success", outputs=outputs)
    elif status == "cancelled":
        await capture.finish("cancelled", outputs=outputs)
    else:
        await capture.finish("error", error=error, outputs=outputs)


@router.api_route(IMAGES_GENERATIONS_ENDPOINT, methods=["HEAD", "OPTIONS"])
async def probe_images_generations(_auth=Depends(require_proxy_auth)):
    return Response(status_code=204, headers={"Allow": "POST, HEAD, OPTIONS"})
