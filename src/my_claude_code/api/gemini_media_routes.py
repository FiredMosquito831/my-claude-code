"""Gemini-shaped media requests (7.65.0): pictures, speech and Veo video jobs.

A Gemini client asks for media on its own surface. ``:generateContent`` /
``:streamGenerateContent`` with ``generationConfig.responseModalities`` naming
``IMAGE`` goes to the Image rail (an edit when ``contents`` carry inline
images), naming ``AUDIO`` to the Speech rail; ``:predictLongRunning`` (Veo) to
the Video rail. Each is the same media request the OpenAI routes build -- the
same executor, fallback, pause list, request-log row and, for a video, the same
acceptance, pinning and ``media_jobs`` row (``media_video_routes``) -- only
worded in Google's shapes, with failures in Google's error envelope.

The answer to a media ``generateContent`` is buffered (every upstream on these
rails answers whole) and returned as one ``GenerateContentResponse`` whose
parts are ``inlineData``; ``:streamGenerateContent`` gets that same object as
ONE SSE event once it is complete.

A video job is then read the way ``google-genai`` reads Veo's:
``GET /v1beta/operations/{id}`` (the operation's ``name``) is one pinned
retrieve on the key that accepted the job, and the finished video's ``uri`` is
MCC's own ``/v1beta/files/{id}:download``, served by the video content logic
(the media store first, else the job's own key and leg).
"""

import asyncio
import base64
import contextlib
import dataclasses
import tempfile
from collections.abc import AsyncIterator, Callable, Mapping
from typing import IO, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from starlette.formparsers import MultiPartParser

from my_claude_code.application.errors import ApplicationError
from my_claude_code.application.media.ports import MediaJobClient, MediaRuntimePort
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
    MEDIA_OPERATION_VIDEO_CREATE,
)
from my_claude_code.config.settings import Settings
from my_claude_code.core.diagnostics import safe_exception_message
from my_claude_code.core.failures import (
    ExecutionFailure,
    FailureKind,
    failure_kind,
    find_execution_failure,
)
from my_claude_code.core.gemini_api import (
    GEMINI_SSE_HEADERS,
    MODALITY_IMAGE,
    PREDICT_LONG_RUNNING,
    STREAM_GENERATE_CONTENT,
    GeminiConversionError,
    GeminiGenerateContentRequest,
    InlineMedia,
    audio_part,
    encode_media_answer,
    fetched_image_part,
    file_id_for_job,
    gemini_error_payload,
    gemini_failure_payload,
    gemini_operation,
    gemini_status_for_failure,
    generate_content_media_response,
    image_ask,
    job_id_for_file,
    openai_image_parts,
    operation_name,
    speech_ask,
    video_ask,
)
from my_claude_code.core.media_outputs import GeneratedMedia, MediaOutputs
from my_claude_code.core.media_store import sha256_hex
from my_claude_code.core.request_log import store_from_settings

from .dependencies import get_services, require_proxy_auth
from .media_capture import MediaCapture
from .media_routes import Cleanup, Complete, MediaWire, _serve
from .media_video_routes import (
    _answering_key,
    _client_for,
    _content,
    _load,
    _no_media,
    _no_store,
    _poll,
    _video_complete,
)
from .ports import ApiServices
from .request_ids import get_request_id
from .response_streams import bind_response_lifetime
from .wire_surfaces import (
    GEMINI_ENDPOINT_PREFIX,
    GEMINI_FILES_PREFIX,
    GEMINI_OPERATIONS_PREFIX,
)

router = APIRouter()

#: The ``protocol`` a Gemini-shaped media row is logged under.
GEMINI_PROTOCOL = "gemini"


# ------------------------------------------------------------------- errors


def _gemini_error(
    status_code: int, message: str, kind: FailureKind | ExecutionFailure
) -> JSONResponse:
    """Google's envelope; the canonical status from the classified failure."""
    return JSONResponse(
        status_code=status_code,
        content=gemini_error_payload(
            message=message, code=status_code, status=gemini_status_for_failure(kind)
        ),
    )


def _gemini_failure(exc: BaseException) -> JSONResponse:
    """Google's envelope for whatever ended the route."""
    execution = find_execution_failure(exc)
    if execution is None and isinstance(exc, ApplicationError):
        # As an ExecutionFailure, so a 404 (an empty rail) reads NOT_FOUND.
        execution = ExecutionFailure(
            kind=exc.kind,
            status_code=exc.status_code,
            message=exc.message,
            retryable=False,
        )
    if execution is not None:
        return JSONResponse(
            status_code=execution.status_code,
            content=gemini_failure_payload(execution),
        )
    kind = failure_kind(exc) or FailureKind.UPSTREAM
    return _gemini_error(502, safe_exception_message(exc), kind)


def _not_found(message: str) -> JSONResponse:
    return _gemini_error(
        404,
        message,
        ExecutionFailure(
            kind=FailureKind.INVALID_REQUEST,
            status_code=404,
            message=message,
            retryable=False,
        ),
    )


def _invalid(message: str) -> JSONResponse:
    return _gemini_error(400, message, FailureKind.INVALID_REQUEST)


GEMINI_WIRE = MediaWire(
    protocol=GEMINI_PROTOCOL,
    error=_gemini_error,
    failure=_gemini_failure,
    expired=_not_found,
)


# ------------------------------------------------------------------ uploads


def _spool(blob: InlineMedia) -> tuple[IO[bytes], str, int]:
    """Worker thread: one inline image decoded into a spooled file, measured.

    Spooled like a multipart upload (in memory up to the multipart parser's
    own threshold, on disk above it), so the adapters stream it the same way.
    """
    data = base64.b64decode(blob.data, validate=False)
    with contextlib.ExitStack() as stack:
        file = stack.enter_context(
            tempfile.SpooledTemporaryFile(max_size=MultiPartParser.spool_max_size)
        )
        file.write(data)
        file.seek(0)
        # Written: the file now belongs to the upload, closed by its cleanup.
        stack.pop_all()
    return file, sha256_hex(data), len(data)


def _close_uploads(uploads: tuple[MediaUpload, ...]) -> None:
    """Worker thread: close (and so delete) the spooled copies."""
    for upload in uploads:
        upload.file.close()


def _extension(mime_type: str) -> str:
    subtype = mime_type.partition("/")[2].partition(";")[0].strip()
    return {"jpeg": "jpg", "svg+xml": "svg"}.get(subtype, subtype) or "bin"


async def _uploads(
    blobs: tuple[InlineMedia, ...], *, field: str, name: str
) -> tuple[MediaUpload, ...]:
    """The client's inline images as uploads; decoded and hashed off the loop."""
    uploads: list[MediaUpload] = []
    try:
        for position, blob in enumerate(blobs):
            file, sha256, size = await asyncio.to_thread(_spool, blob)
            mime = blob.mime_type or "application/octet-stream"
            uploads.append(
                MediaUpload(
                    field=field,
                    filename=f"{name}-{position}.{_extension(mime)}",
                    content_type=mime,
                    size=size,
                    sha256=sha256,
                    file=file,
                )
            )
    except BaseException:
        await asyncio.to_thread(_close_uploads, tuple(uploads))
        raise
    return tuple(uploads)


def _closer(uploads: tuple[MediaUpload, ...]) -> Cleanup:
    async def close() -> None:
        if uploads:
            await asyncio.to_thread(_close_uploads, uploads)

    return close


async def _inline_uploads(
    blobs: tuple[InlineMedia, ...], *, field: str, name: str
) -> tuple[MediaUpload, ...] | JSONResponse:
    if not blobs:
        return ()
    try:
        return await _uploads(blobs, field=field, name=name)
    except ValueError:
        return _invalid("An inline image's data is not valid base64.")


# ------------------------------------------------------- generateContent


def _answer_client(
    media: MediaRuntimePort, settings: Settings, capture: MediaCapture
) -> tuple[MediaJobClient, str]:
    """The key and leg that answered, to fetch what its answer points at."""
    routed = capture.routed
    if routed is None:
        raise ExecutionFailure(
            kind=FailureKind.UPSTREAM,
            status_code=502,
            message="The image answer named URLs, and no provider answered it.",
            retryable=False,
        )
    provider_id = routed.resolved.provider_id
    key_index, _label, fingerprint = _answering_key(media, settings, provider_id)
    client = media.job_client(
        settings,
        provider_id,
        key_fingerprint=fingerprint,
        key_index=key_index,
        proxy_label=capture.proxy_label,
    )
    if client is None:
        raise ExecutionFailure(
            kind=FailureKind.UPSTREAM,
            status_code=502,
            message=(
                f"{provider_id} answered with image URLs, and the key that asked "
                "is no longer configured to fetch them."
            ),
            retryable=False,
        )
    return client, routed.resolved.provider_model


async def _fetch(
    client: MediaJobClient, url: str, *, model: str, request_id: str
) -> tuple[bytes, str]:
    """One URL-only image, downloaded whole: its bytes and the host's type.

    Through the answering key's own leaf, one call, never retried elsewhere;
    a key goes only to the provider's own host (a CDN URL is fetched bare).
    """
    download = await client.download(
        operation=None, upstream_id="", url=url, model=model, request_id=request_id
    )
    data = bytearray()
    try:
        async for chunk in download.chunks:
            data.extend(chunk)
    finally:
        await download.close()
    return bytes(data), download.content_type


async def _image_parts(
    media: MediaRuntimePort,
    settings: Settings,
    capture: MediaCapture,
    answer: MediaResponse,
) -> tuple[list[dict[str, Any]], tuple[GeneratedMedia, ...]]:
    """The images as ``inlineData`` parts; URL-only ones fetched and encoded."""
    items = await asyncio.to_thread(openai_image_parts, answer.body)
    parts: list[dict[str, Any]] = []
    fetched: list[GeneratedMedia] = []
    client: tuple[MediaJobClient, str] | None = None
    for item in items:
        if not isinstance(item, str):
            parts.append(item)
            continue
        if client is None:
            client = _answer_client(media, settings, capture)
        data, content_type = await _fetch(
            client[0], item, model=client[1], request_id=capture.request_id
        )
        part, generated = await asyncio.to_thread(
            fetched_image_part, data, content_type
        )
        parts.append(part)
        fetched.append(generated)
    return parts, tuple(fetched)


def _generate_complete(
    media: MediaRuntimePort, *, model: str, stream: bool, image: bool
) -> Complete:
    """Answer a media ``generateContent`` in Google's shape, whole."""

    async def complete(
        first: MediaChunk,
        rest: AsyncIterator[MediaChunk],
        lease: RequestRuntimeLease,
        capture: MediaCapture,
        parse: Callable[[MediaResponse], MediaOutputs],
    ) -> Response:
        try:
            try:
                async for _extra in rest:
                    pass
                if not isinstance(first, MediaResponse):
                    raise ExecutionFailure(
                        kind=FailureKind.UPSTREAM,
                        status_code=502,
                        message=(
                            "The provider streamed an answer that was not asked for."
                        ),
                        retryable=False,
                    )
                outputs = await asyncio.to_thread(parse, first)
                if image:
                    parts, fetched = await _image_parts(
                        media, lease.settings, capture, first
                    )
                    if fetched:
                        outputs = dataclasses.replace(
                            outputs, items=outputs.items + fetched
                        )
                else:
                    parts = [
                        await asyncio.to_thread(
                            audio_part, first.body, first.content_type
                        )
                    ]
                payload = generate_content_media_response(
                    parts, model=model, usage=outputs.usage
                )
                body = await asyncio.to_thread(
                    encode_media_answer, payload, stream=stream
                )
            finally:
                await lease.release()
        except Exception as exc:
            await capture.finish("error", error=exc)
            return _gemini_failure(exc)
        except BaseException:
            await capture.finish("cancelled")
            raise
        await capture.finish("success", outputs=outputs)
        if stream:
            return Response(
                content=body,
                media_type="text/event-stream",
                headers=GEMINI_SSE_HEADERS,
            )
        return Response(content=body, media_type="application/json")

    return complete


async def serve_media_generate(
    request: Request,
    services: ApiServices,
    request_data: GeminiGenerateContentRequest,
    *,
    output: str,
    method: str,
) -> Response:
    """A ``generateContent`` that asked for ``IMAGE`` or ``AUDIO``: a media rail."""
    model = request_data.model
    image = output == MODALITY_IMAGE
    try:
        ask = image_ask(request_data) if image else speech_ask(request_data)
    except GeminiConversionError as exc:
        return _invalid(str(exc))
    uploads = await _inline_uploads(ask.images, field="image[]", name="image")
    if isinstance(uploads, JSONResponse):
        return uploads
    if image:
        operation = (
            MEDIA_OPERATION_IMAGE_EDIT if uploads else MEDIA_OPERATION_IMAGE_GENERATE
        )
        rail = MediaRail.IMAGE
    else:
        operation = MEDIA_OPERATION_SPEECH
        rail = MediaRail.TTS
    media_request = MediaRequest(
        operation=operation,
        rail=rail,
        model=model,
        body=ask.body,
        stream=False,
        uploads=uploads,
        not_forwarded=ask.not_forwarded,
    )
    media = services.media
    return await _serve(
        request,
        services,
        media_request,
        endpoint=f"{GEMINI_ENDPOINT_PREFIX}/{model}:{method}",
        cleanup=_closer(uploads),
        complete=(
            None
            if media is None
            else _generate_complete(
                media,
                model=model,
                stream=method == STREAM_GENERATE_CONTENT,
                image=image,
            )
        ),
        wire=GEMINI_WIRE,
    )


# --------------------------------------------------------------- video jobs


def _operation_answer(row: Mapping[str, Any], _upstream: object) -> Response:
    """An accepted Veo job: the Operation ``name`` the client polls."""
    return JSONResponse({"name": operation_name(str(row["job_id"]))})


async def serve_predict_long_running(
    request: Request,
    services: ApiServices,
    request_data: GeminiGenerateContentRequest,
) -> Response:
    """Veo's ``:predictLongRunning``: a video job on the Video rail.

    Accepted, recorded and pinned exactly as ``POST /v1/videos`` is. The
    create travels as the OpenAI SDK's own multipart form would (the path M5
    proved on every declared host): a first-frame image is its
    ``input_reference`` upload, and a JSON-only host skips such a request
    uncharged.
    """
    model = request_data.model
    endpoint = f"{GEMINI_ENDPOINT_PREFIX}/{model}:{PREDICT_LONG_RUNNING}"
    media = services.media
    if (
        media is not None
        and store_from_settings(services.requests.current_settings()) is None
    ):
        return _no_store(GEMINI_WIRE, endpoint=f":{PREDICT_LONG_RUNNING}")
    try:
        ask = video_ask(request_data)
    except GeminiConversionError as exc:
        return _invalid(str(exc))
    uploads = await _inline_uploads(
        ask.images, field="input_reference", name="input_reference"
    )
    if isinstance(uploads, JSONResponse):
        return uploads
    media_request = MediaRequest(
        operation=MEDIA_OPERATION_VIDEO_CREATE,
        rail=MediaRail.VIDEO,
        model=model,
        body=ask.body,
        stream=False,
        uploads=uploads,
        multipart=True,
        not_forwarded=ask.not_forwarded,
    )
    return await _serve(
        request,
        services,
        media_request,
        endpoint=endpoint,
        cleanup=_closer(uploads),
        complete=(
            None
            if media is None
            else _video_complete(
                media, media_request, wire=GEMINI_WIRE, answer=_operation_answer
            )
        ),
        wire=GEMINI_WIRE,
    )


def _download_uri(request: Request, job_id: str) -> str:
    """The finished video's ``uri``: MCC's own download URL, on the client's base."""
    base = str(request.base_url).rstrip("/")
    return f"{base}{GEMINI_FILES_PREFIX}/{file_id_for_job(job_id)}:download?alt=media"


@router.get(GEMINI_OPERATIONS_PREFIX + "/{operation_id}")
async def get_operation(
    operation_id: str,
    request: Request,
    services: ApiServices = Depends(get_services),
    _auth=Depends(require_proxy_auth),
):
    """A Veo job's status (``operations.get``): one retrieve on its own key."""
    media = services.media
    if media is None:
        return _no_media(GEMINI_WIRE)
    lease = await services.requests.acquire()
    try:
        loaded = await _load(lease.settings, operation_id, GEMINI_WIRE)
        if isinstance(loaded, JSONResponse):
            return loaded
        store, job = loaded
        client = _client_for(lease.settings, media, job, GEMINI_WIRE)
        if isinstance(client, JSONResponse):
            return client
        try:
            updated, _upstream = await _poll(
                store, client, job, get_request_id(request)
            )
        except Exception as exc:
            return _gemini_failure(exc)
        return JSONResponse(
            gemini_operation(updated, download_uri=_download_uri(request, operation_id))
        )
    finally:
        await lease.release()


@router.get(GEMINI_FILES_PREFIX + "/{file_ref:path}")
async def download_file(
    file_ref: str,
    request: Request,
    services: ApiServices = Depends(get_services),
    _auth=Depends(require_proxy_auth),
):
    """A finished Veo video (``files.download``), streamed from the host.

    Served from the media store when this job's video was kept there.
    """
    job_id = job_id_for_file(file_ref)
    if job_id is None:
        return _not_found(
            f"Unknown file: files/{file_ref}. MCC serves files/<id>:download for "
            "the videos its own :predictLongRunning jobs made."
        )
    media = services.media
    if media is None:
        return _no_media(GEMINI_WIRE)
    lease = await services.requests.acquire()
    try:
        response = await _content(
            lease, media, job_id, {}, get_request_id(request), GEMINI_WIRE
        )
    except BaseException:
        await lease.release()
        raise
    bound = await bind_response_lifetime(response, lease.release)
    assert isinstance(bound, Response)
    return bound
