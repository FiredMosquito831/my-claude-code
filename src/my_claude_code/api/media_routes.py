"""OpenAI-compatible media endpoints.

7.60.0: ``POST /v1/images/generations``. 7.61.0: ``POST /v1/images/edits`` --
multipart (``image`` / ``image[]``, ``mask``) or the JSON variant
(``images: [{image_url}]``).

Both route on the Image rail (``MODEL_IMAGE`` + ``MODEL_IMAGE_FALLBACKS``,
minus ``MODEL_IMAGE_PAUSED``) through the media executor -- a separate copy of
the chat executor's rules with its own health books. Every request takes a
runtime lease like the chat surfaces, so graceful shutdown drains it, and
writes one request-log row.

A non-streaming request is answered only once a model has produced the whole
answer, so any failure before that falls back invisibly. A streaming request
commits on the first forwarded event; an upstream failure after that ends the
stream with an OpenAI ``error`` event.

Uploads are parsed by Starlette's form parser, which spools a file to disk
above the multipart library's own threshold (user decision 10: the library
default) and writes rolled-over files from a worker thread. They are hashed
off the loop, streamed to the upstream from the spooled copy, and closed when
the request is finished with them.
"""

import asyncio
import dataclasses
import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import IO, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from starlette.datastructures import FormData, UploadFile

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
    MediaUpload,
)
from my_claude_code.application.ports import RequestRuntimeLease
from my_claude_code.config.media_surfaces import (
    MEDIA_OPERATION_IMAGE_EDIT,
    MEDIA_OPERATION_IMAGE_GENERATE,
    MEDIA_OPERATION_SPEECH,
    MEDIA_OPERATION_TRANSCRIBE,
    MEDIA_OPERATION_TRANSLATE,
)
from my_claude_code.core.diagnostics import safe_exception_message
from my_claude_code.core.failures import (
    ExecutionFailure,
    FailureKind,
    failure_kind,
    find_execution_failure,
)
from my_claude_code.core.media_outputs import MediaOutputs, sse_error_frame
from my_claude_code.core.openai_common.errors import (
    openai_error_payload,
    openai_error_type_for_failure,
)
from my_claude_code.core.openai_images import (
    parse_images_response,
    parse_images_stream,
)
from my_claude_code.core.openai_speech import parse_speech_response
from my_claude_code.core.openai_transcriptions import (
    parse_transcription_response,
    parse_transcription_stream,
    wav_file_seconds,
)

from .dependencies import get_services, require_proxy_auth, resolve_provider
from .media_capture import MEDIA_PROTOCOL_OPENAI, MediaCapture
from .ports import ApiServices
from .request_ids import get_request_id
from .response_streams import ManagedStreamingResponse, bind_response_lifetime
from .wire_surfaces import (
    AUDIO_SPEECH_ENDPOINT,
    AUDIO_TRANSCRIPTIONS_ENDPOINT,
    AUDIO_TRANSLATIONS_ENDPOINT,
    IMAGES_EDITS_ENDPOINT,
    IMAGES_GENERATIONS_ENDPOINT,
)

router = APIRouter()

#: Bytes hashed per worker-thread read of an uploaded file.
_HASH_CHUNK_BYTES = 1024 * 1024

Cleanup = Callable[[], Awaitable[None]]
#: How a buffered (non-streaming) answer is turned into the client's response.
#: ``_complete_response`` by default; the Video rail answers with its job.
Complete = Callable[
    [
        MediaChunk,
        AsyncIterator[MediaChunk],
        RequestRuntimeLease,
        MediaCapture,
        Callable[[MediaResponse], MediaOutputs],
    ],
    Awaitable[Response],
]


async def _nothing_to_clean() -> None:
    return None


def _error_response(
    status_code: int, message: str, kind: FailureKind | ExecutionFailure
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=openai_error_payload(
            message=message, error_type=openai_error_type_for_failure(kind)
        ),
    )


def _invalid(message: str) -> JSONResponse:
    return _error_response(400, message, FailureKind.INVALID_REQUEST)


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


def _expired_response(message: str) -> JSONResponse:
    """A job's finished file the host no longer holds (``video_expired``)."""
    payload = openai_error_payload(message=message, error_type="not_found_error")
    payload["error"]["code"] = "video_expired"
    return JSONResponse(status_code=404, content=payload)


@dataclass(frozen=True, slots=True)
class MediaWire:
    """How the client that asked speaks: its log protocol and its error envelope.

    OpenAI's by default (``OPENAI_WIRE``). The Gemini surface (7.65.0) routes
    onto the same rails with the same functions and answers every failure in
    Google's envelope instead; nothing else about a request differs.
    """

    protocol: str
    #: ``(status, message, kind)`` -> the error answer.
    error: Callable[[int, str, FailureKind | ExecutionFailure], JSONResponse]
    #: Whatever ended the route -> the error answer.
    failure: Callable[[BaseException], JSONResponse]
    #: A job's file the host no longer holds -> the 404 answer.
    expired: Callable[[str], JSONResponse]


OPENAI_WIRE = MediaWire(
    protocol=MEDIA_PROTOCOL_OPENAI,
    error=_error_response,
    failure=_failure_response,
    expired=_expired_response,
)


def _commit_error_frames(exc: BaseException) -> Sequence[bytes]:
    execution = find_execution_failure(exc)
    kind: FailureKind | ExecutionFailure = (
        execution
        if execution is not None
        else (failure_kind(exc) or FailureKind.UPSTREAM)
    )
    return (
        sse_error_frame(
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


def _measure(file: IO[bytes]) -> tuple[str, int, float | None]:
    """Runs in a worker thread: SHA-256, size and WAV length of one upload."""
    digest = hashlib.sha256()
    size = 0
    file.seek(0)
    while chunk := file.read(_HASH_CHUNK_BYTES):
        digest.update(chunk)
        size += len(chunk)
    file.seek(0)
    header = file.read(_HASH_CHUNK_BYTES)
    file.seek(0)
    return digest.hexdigest(), size, wav_file_seconds(header)


async def _uploads_from_form(
    form: FormData,
) -> tuple[dict[str, Any], tuple[MediaUpload, ...]]:
    """Split a parsed form into its text fields and its measured uploads."""
    fields: dict[str, Any] = {}
    uploads: list[MediaUpload] = []
    for name, value in form.multi_items():
        if isinstance(value, UploadFile):
            sha256, size, seconds = await asyncio.to_thread(_measure, value.file)
            uploads.append(
                MediaUpload(
                    field=name,
                    filename=value.filename or name,
                    content_type=value.content_type or "application/octet-stream",
                    size=size,
                    sha256=sha256,
                    file=value.file,
                    audio_seconds=seconds,
                )
            )
            continue
        if name in fields:
            existing = fields[name]
            fields[name] = (
                [*existing, value] if isinstance(existing, list) else [existing, value]
            )
        else:
            fields[name] = value
    return fields, tuple(uploads)


def _stream_flag(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return value is True


async def _serve(
    request: Request,
    services: ApiServices,
    media_request: MediaRequest,
    *,
    endpoint: str,
    cleanup: Cleanup = _nothing_to_clean,
    complete: Complete | None = None,
    wire: MediaWire = OPENAI_WIRE,
) -> Response:
    """Plan, execute and answer one media request; ``cleanup`` runs once at the end.

    ``complete`` answers a buffered request (default ``_complete_response``);
    it must release the lease and finish the capture, as the default does.
    ``wire`` is the client's protocol: the row's ``protocol`` and the
    envelope a failure before the answer is worded in.
    """
    request_id = get_request_id(request)
    media = services.media
    if media is None:
        await cleanup()
        return wire.error(
            503,
            "Media routing is not available in this server.",
            FailureKind.UNAVAILABLE,
        )
    lease: RequestRuntimeLease | None = None
    capture: MediaCapture | None = None
    try:
        lease = await services.requests.acquire()
        settings = lease.settings
        capture = MediaCapture(
            settings,
            request_id=request_id,
            endpoint=endpoint,
            request=media_request,
            headers=request.headers,
            protocol=wire.protocol,
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
        await cleanup()
        return wire.error(
            502, "The provider returned an empty answer.", FailureKind.UPSTREAM
        )
    except Exception as exc:
        if lease is not None:
            await lease.release()
        if capture is not None:
            await capture.finish("error", error=exc)
        await cleanup()
        return wire.failure(exc)
    except BaseException:
        if lease is not None:
            await lease.release()
        if capture is not None:
            await capture.finish("cancelled")
        await cleanup()
        raise

    if not media_request.stream:
        answer = _complete_response if complete is None else complete
        try:
            return await answer(
                first, stream, lease, capture, _parser_for(media_request)
            )
        finally:
            await cleanup()
    response = ManagedStreamingResponse(
        _stream_body(
            first, stream, capture, cleanup, _stream_parser_for(media_request)
        ),
        media_type="text/event-stream",
    )
    bound = await bind_response_lifetime(response, lease.release)
    assert isinstance(bound, Response)
    return bound


@router.post(IMAGES_GENERATIONS_ENDPOINT)
async def create_image(
    request: Request,
    services: ApiServices = Depends(get_services),
    _auth=Depends(require_proxy_auth),
):
    """Generate images through the Image rail (OpenAI ``images.generate``)."""
    payload = await _read_json_object(request)
    if payload is None:
        return _invalid("The request body must be a JSON object.")
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return _invalid("'prompt' is required and must be a string.")
    media_request = MediaRequest(
        operation=MEDIA_OPERATION_IMAGE_GENERATE,
        rail=MediaRail.IMAGE,
        model=str(payload.get("model") or ""),
        body={key: value for key, value in payload.items() if key != "model"},
        stream=payload.get("stream") is True,
    )
    return await _serve(
        request, services, media_request, endpoint=IMAGES_GENERATIONS_ENDPOINT
    )


@router.post(IMAGES_EDITS_ENDPOINT)
async def edit_image(
    request: Request,
    services: ApiServices = Depends(get_services),
    _auth=Depends(require_proxy_auth),
):
    """Edit images through the Image rail (OpenAI ``images.edit``)."""
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("multipart/form-data"):
        form = await request.form()

        async def close_form() -> None:
            await form.close()

        try:
            fields, uploads = await _uploads_from_form(form)
        except BaseException:
            await close_form()
            raise
        prompt = fields.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            await close_form()
            return _invalid("'prompt' is required and must be a string.")
        if not any(upload.field.startswith("image") for upload in uploads):
            await close_form()
            return _invalid("An 'image' file is required to edit an image.")
        media_request = MediaRequest(
            operation=MEDIA_OPERATION_IMAGE_EDIT,
            rail=MediaRail.IMAGE,
            model=str(fields.get("model") or ""),
            body={key: value for key, value in fields.items() if key != "model"},
            stream=_stream_flag(fields.get("stream")),
            uploads=uploads,
        )
        return await _serve(
            request,
            services,
            media_request,
            endpoint=IMAGES_EDITS_ENDPOINT,
            cleanup=close_form,
        )
    payload = await _read_json_object(request)
    if payload is None:
        return _invalid(
            "The request body must be multipart/form-data or a JSON object."
        )
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return _invalid("'prompt' is required and must be a string.")
    if not payload.get("images"):
        return _invalid("'images' is required to edit an image.")
    media_request = MediaRequest(
        operation=MEDIA_OPERATION_IMAGE_EDIT,
        rail=MediaRail.IMAGE,
        model=str(payload.get("model") or ""),
        body={key: value for key, value in payload.items() if key != "model"},
        stream=payload.get("stream") is True,
    )
    return await _serve(
        request, services, media_request, endpoint=IMAGES_EDITS_ENDPOINT
    )


def _parse_images(response: MediaResponse) -> MediaOutputs:
    return parse_images_response(response.body)


def _with_leaf_measures(outputs: MediaOutputs, response: MediaResponse) -> MediaOutputs:
    """What the leaf measured while translating a native answer.

    Only a Gemini native answer carries any (token usage its body cannot
    hold, raw PCM's length, fields its surface did not take); every other
    answer comes back unchanged.
    """
    changes: dict[str, Any] = {}
    if outputs.usage is None and response.usage is not None:
        changes["usage"] = {str(key): value for key, value in response.usage.items()}
    if outputs.audio_seconds is None and response.audio_seconds is not None:
        changes["audio_seconds"] = response.audio_seconds
    if response.not_forwarded:
        changes["not_forwarded"] = outputs.not_forwarded + response.not_forwarded
    return dataclasses.replace(outputs, **changes) if changes else outputs


def _parse_transcription(response: MediaResponse) -> MediaOutputs:
    return _with_leaf_measures(
        parse_transcription_response(response.body, response.content_type), response
    )


def _parse_speech(response: MediaResponse) -> MediaOutputs:
    return _with_leaf_measures(
        parse_speech_response(response.body, response.content_type), response
    )


def _parser_for(media_request: MediaRequest) -> Callable[[MediaResponse], MediaOutputs]:
    """How a buffered answer is measured, by the operation's wire shape."""
    if media_request.operation == MEDIA_OPERATION_SPEECH:
        return _parse_speech
    if media_request.operation in (
        MEDIA_OPERATION_TRANSCRIBE,
        MEDIA_OPERATION_TRANSLATE,
    ):
        return _parse_transcription
    return _parse_images


def _stream_parser_for(media_request: MediaRequest) -> Callable[[bytes], MediaOutputs]:
    if media_request.operation in (
        MEDIA_OPERATION_TRANSCRIBE,
        MEDIA_OPERATION_TRANSLATE,
    ):
        return parse_transcription_stream
    return parse_images_stream


@router.post(AUDIO_SPEECH_ENDPOINT)
async def create_speech(
    request: Request,
    services: ApiServices = Depends(get_services),
    _auth=Depends(require_proxy_auth),
):
    """Synthesize speech through the Speech rail (OpenAI ``audio.speech``).

    The answer is the audio itself, returned with the host's own Content-Type.
    ``stream_format: "sse"`` is routed only to a surface that declares it.
    """
    payload = await _read_json_object(request)
    if payload is None:
        return _invalid("The request body must be a JSON object.")
    text = payload.get("input")
    if not isinstance(text, str) or not text.strip():
        return _invalid("'input' is required and must be a string.")
    media_request = MediaRequest(
        operation=MEDIA_OPERATION_SPEECH,
        rail=MediaRail.TTS,
        model=str(payload.get("model") or ""),
        body={key: value for key, value in payload.items() if key != "model"},
        stream=payload.get("stream_format") == "sse",
    )
    return await _serve(
        request, services, media_request, endpoint=AUDIO_SPEECH_ENDPOINT
    )


def _audio_route(operation: str, endpoint: str):
    async def handle(
        request: Request,
        services: ApiServices = Depends(get_services),
        _auth=Depends(require_proxy_auth),
    ):
        if not request.headers.get("content-type", "").startswith(
            "multipart/form-data"
        ):
            return _invalid(
                "The request body must be multipart/form-data with a 'file'."
            )
        form = await request.form()

        async def close_form() -> None:
            await form.close()

        try:
            fields, uploads = await _uploads_from_form(form)
        except BaseException:
            await close_form()
            raise
        if not any(upload.field == "file" for upload in uploads):
            await close_form()
            return _invalid("A 'file' with the audio is required.")
        media_request = MediaRequest(
            operation=operation,
            rail=MediaRail.ASR,
            model=str(fields.get("model") or ""),
            body={key: value for key, value in fields.items() if key != "model"},
            stream=_stream_flag(fields.get("stream")),
            uploads=uploads,
        )
        return await _serve(
            request, services, media_request, endpoint=endpoint, cleanup=close_form
        )

    return handle


router.add_api_route(
    AUDIO_TRANSCRIPTIONS_ENDPOINT,
    _audio_route(MEDIA_OPERATION_TRANSCRIBE, AUDIO_TRANSCRIPTIONS_ENDPOINT),
    methods=["POST"],
    summary="Transcribe audio through the Transcription rail",
)
router.add_api_route(
    AUDIO_TRANSLATIONS_ENDPOINT,
    _audio_route(MEDIA_OPERATION_TRANSLATE, AUDIO_TRANSLATIONS_ENDPOINT),
    methods=["POST"],
    summary="Translate audio to English text through the Transcription rail",
)


async def _complete_response(
    first: MediaChunk,
    stream: AsyncIterator[MediaChunk],
    lease: RequestRuntimeLease,
    capture: MediaCapture,
    parse: Callable[[MediaResponse], MediaOutputs],
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
    outputs: MediaOutputs = await asyncio.to_thread(parse, first)
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
    cleanup: Cleanup,
    parse_stream: Callable[[bytes], MediaOutputs],
) -> AsyncIterator[bytes]:
    """Forward SSE frames; measure the completed images once the stream ends.

    The row is written (and the uploads closed) from a task rather than awaited
    here: a client that hangs up closes this generator with ``GeneratorExit``,
    and awaiting during that is unsafe.
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
        asyncio.ensure_future(
            _finish_stream(capture, status, error, bytes(seen), cleanup, parse_stream)
        )


async def _finish_stream(
    capture: MediaCapture,
    status: str,
    error: BaseException | None,
    seen: bytes,
    cleanup: Cleanup,
    parse_stream: Callable[[bytes], MediaOutputs],
) -> None:
    try:
        outputs = await asyncio.to_thread(parse_stream, seen)
        if status == "success":
            await capture.finish("success", outputs=outputs)
        elif status == "cancelled":
            await capture.finish("cancelled", outputs=outputs)
        else:
            await capture.finish("error", error=error, outputs=outputs)
    finally:
        await cleanup()


@router.api_route(IMAGES_GENERATIONS_ENDPOINT, methods=["HEAD", "OPTIONS"])
async def probe_images_generations(_auth=Depends(require_proxy_auth)):
    return Response(status_code=204, headers={"Allow": "POST, HEAD, OPTIONS"})


@router.api_route(AUDIO_TRANSCRIPTIONS_ENDPOINT, methods=["HEAD", "OPTIONS"])
async def probe_audio_transcriptions(_auth=Depends(require_proxy_auth)):
    return Response(status_code=204, headers={"Allow": "POST, HEAD, OPTIONS"})


@router.api_route(AUDIO_TRANSLATIONS_ENDPOINT, methods=["HEAD", "OPTIONS"])
async def probe_audio_translations(_auth=Depends(require_proxy_auth)):
    return Response(status_code=204, headers={"Allow": "POST, HEAD, OPTIONS"})


@router.api_route(AUDIO_SPEECH_ENDPOINT, methods=["HEAD", "OPTIONS"])
async def probe_audio_speech(_auth=Depends(require_proxy_auth)):
    return Response(status_code=204, headers={"Allow": "POST, HEAD, OPTIONS"})


@router.api_route(IMAGES_EDITS_ENDPOINT, methods=["HEAD", "OPTIONS"])
async def probe_images_edits(_auth=Depends(require_proxy_auth)):
    return Response(status_code=204, headers={"Allow": "POST, HEAD, OPTIONS"})
