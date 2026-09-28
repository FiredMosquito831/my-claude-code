"""OpenAI-compatible video endpoints (7.64.0): ``/v1/videos``.

A video is a job. ``POST /v1/videos`` routes on the Video rail (``MODEL_VIDEO``
+ ``MODEL_VIDEO_FALLBACKS``, minus ``MODEL_VIDEO_PAUSED``) through the media
executor like every media request, so it falls back -- invisibly, a buffered
answer -- until a provider *accepts* the job by naming it. From that moment the
job belongs to that provider, that key and that proxy leg: it is recorded in
``media_jobs`` before the client is answered, and every later call on it --
retrieve, content, delete -- goes out through that key only, one upstream call
per client call, never rotated and never resubmitted (user decision Q5: a job
that later fails is reported failed, not run again elsewhere).

The client only ever sees MCC's own job id (``video_<hex>``). The host's id and
any URL it serves the file at stay on the server.

``GET /v1/videos`` lists MCC's own jobs from ``media_jobs``: the jobs span
providers and keys, so no upstream list could answer it. No declared host
documents a delete, so ``DELETE`` forgets MCC's record of the job.
"""

import asyncio
import dataclasses
import json
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from loguru import logger

from my_claude_code.application.errors import ApplicationError
from my_claude_code.application.media.ports import MediaJobClient, MediaRuntimePort
from my_claude_code.application.media.request import (
    MediaChunk,
    MediaDownload,
    MediaRail,
    MediaRequest,
    MediaResponse,
    MediaUpload,
)
from my_claude_code.application.media_cost import MediaUsage
from my_claude_code.application.ports import RequestRuntimeLease
from my_claude_code.config.media_surfaces import (
    MEDIA_OPERATION_VIDEO_CONTENT,
    MEDIA_OPERATION_VIDEO_CREATE,
    MEDIA_OPERATION_VIDEO_DELETE,
    MEDIA_OPERATION_VIDEO_RETRIEVE,
    MediaSurface,
    surface_for,
)
from my_claude_code.config.provider_registry import get_provider_registry
from my_claude_code.config.settings import Settings
from my_claude_code.core.credential_attribution import current_credential
from my_claude_code.core.failures import (
    ExecutionFailure,
    FailureKind,
    find_execution_failure,
)
from my_claude_code.core.media_outputs import MediaOutputs
from my_claude_code.core.media_store import (
    MediaFileTee,
    MediaOutputRecord,
    media_file_path,
    media_root,
)
from my_claude_code.core.openai_videos import (
    STATUS_COMPLETED,
    UpstreamJob,
    parse_job,
    video_object,
)
from my_claude_code.core.request_log import (
    MediaJobRecord,
    RequestLogStore,
    store_from_settings,
)

from .dependencies import get_services, require_proxy_auth
from .media_capture import (
    MediaCapture,
    MediaPricing,
    media_pricing,
    media_store_cap_bytes,
    price_media_row,
    reported_usd,
)
from .media_routes import (
    OPENAI_WIRE,
    Complete,
    MediaWire,
    _failure_response,
    _invalid,
    _nothing_to_clean,
    _read_json_object,
    _serve,
    _uploads_from_form,
)
from .ports import ApiServices
from .request_ids import get_request_id
from .response_streams import ManagedStreamingResponse, bind_response_lifetime
from .wire_surfaces import VIDEOS_ENDPOINT

router = APIRouter()


#: Bytes handed to a worker thread per hash (and store) step of a download.
_TEE_BATCH_BYTES = 1024 * 1024
#: What a finished file is served as when neither the host nor the store says.
_DEFAULT_VIDEO_TYPE = "video/mp4"


# ------------------------------------------------------------------ answers


def _not_found(video_id: str, wire: MediaWire = OPENAI_WIRE) -> JSONResponse:
    message = f"No video found with id '{video_id}'."
    return wire.error(
        404,
        message,
        ExecutionFailure(
            kind=FailureKind.INVALID_REQUEST,
            status_code=404,
            message=message,
            retryable=False,
        ),
    )


def _conflict(message: str, wire: MediaWire = OPENAI_WIRE) -> JSONResponse:
    return wire.error(409, message, FailureKind.INVALID_REQUEST)


def _unavailable(message: str, wire: MediaWire = OPENAI_WIRE) -> JSONResponse:
    return wire.error(503, message, FailureKind.UNAVAILABLE)


def _no_media(wire: MediaWire = OPENAI_WIRE) -> JSONResponse:
    return _unavailable("Media routing is not available in this server.", wire)


def _no_store(
    wire: MediaWire = OPENAI_WIRE, endpoint: str = VIDEOS_ENDPOINT
) -> JSONResponse:
    return _unavailable(
        "Video jobs are kept in the request log, and REQUEST_LOG_ENABLED is "
        "off: a job could be created but never read back. Turn the request "
        f"log on to use {endpoint}.",
        wire,
    )


def _expired(
    video_id: str, provider: str, wire: MediaWire = OPENAI_WIRE
) -> JSONResponse:
    return wire.expired(
        f"The provider no longer holds this video ({video_id} on {provider})."
    )


def _declared(provider_id: str, operation: str) -> MediaSurface | None:
    """The surface ``provider_id`` declares for ``operation``, if any."""
    descriptor = get_provider_registry().all_descriptors().get(provider_id)
    if descriptor is None:
        return None
    return surface_for(descriptor.media_surfaces, operation)


def _base_type(content_type: str) -> str:
    return content_type.split(";")[0].strip().lower() or _DEFAULT_VIDEO_TYPE


# ------------------------------------------------------------------- create


def _job_row(record: MediaJobRecord) -> dict[str, Any]:
    """A freshly inserted job as the store would read it back."""
    row = dataclasses.asdict(record)
    row.update(
        content_sha=None, content_bytes=None, content_mime=None, row_seconds_written=0
    )
    return row


def _answering_key(
    media: MediaRuntimePort, settings: Settings, provider_id: str
) -> tuple[int | None, str | None, str | None]:
    """The key that answered this request: ``(index, label, fingerprint)``.

    Read from the credential attribution the capture installed; the
    fingerprint (never the key) is what finds the key again after a reorder.
    """
    key_index, key_label = current_credential()
    fingerprint = (
        media.key_fingerprint(settings, provider_id, key_index)
        if key_index is not None and key_index >= 0
        else None
    )
    return key_index, key_label, fingerprint


#: How an accepted, recorded job is shown to the client: ``(job row, host's answer)``.
JobAnswer = Callable[[Mapping[str, Any], UpstreamJob], Response]


def _video_answer(row: Mapping[str, Any], upstream: UpstreamJob) -> Response:
    """The OpenAI ``Video`` object (``POST /v1/videos``)."""
    return JSONResponse(video_object(row, upstream))


def _video_complete(
    media: MediaRuntimePort,
    media_request: MediaRequest,
    *,
    wire: MediaWire = OPENAI_WIRE,
    answer: JobAnswer = _video_answer,
) -> Complete:
    """Answer an accepted create: record the job, then show it to the client.

    ``answer`` shapes the reply (OpenAI's ``Video`` by default; the Gemini
    surface answers with an ``Operation``) and ``wire`` words its failures.
    """

    async def complete(
        first: MediaChunk,
        stream: AsyncIterator[MediaChunk],
        lease: RequestRuntimeLease,
        capture: MediaCapture,
        _parse: Callable[[MediaResponse], MediaOutputs],
    ) -> Response:
        settings = lease.settings
        try:
            async for _extra in stream:
                pass
        finally:
            await lease.release()
        if not isinstance(first, MediaResponse):
            await capture.finish("error", error=RuntimeError("unexpected stream frame"))
            return wire.error(
                502,
                "The provider streamed an answer that was not asked for.",
                FailureKind.UPSTREAM,
            )
        upstream = await asyncio.to_thread(parse_job, first.body)
        routed = capture.routed
        store = store_from_settings(settings)
        if upstream is None or upstream.id is None or routed is None or store is None:
            # The leaf refuses an answer without a job id, so this is only
            # reachable if the log was switched off mid-request.
            error = RuntimeError("the accepted video job could not be recorded")
            await capture.finish("error", error=error)
            return wire.error(502, str(error), FailureKind.UPSTREAM)
        provider_id = routed.resolved.provider_id
        key_index, key_label, fingerprint = _answering_key(media, settings, provider_id)
        now = time.time()
        size = upstream.size or media_request.body.get("size")
        record = MediaJobRecord(
            job_id=f"video_{uuid.uuid4().hex}",
            request_id=capture.request_id,
            provider=provider_id,
            model=routed.resolved.provider_model,
            upstream_id=upstream.id,
            created_at=now,
            requested_model=media_request.model or None,
            key_index=key_index,
            key_fingerprint=fingerprint,
            key_label=key_label,
            proxy_label=capture.proxy_label,
            status=upstream.status,
            status_raw=upstream.status_raw,
            progress=upstream.progress,
            seconds=upstream.seconds,
            size=size if isinstance(size, str) and size else None,
            prompt=media_request.prompt,
            error=upstream.error_message,
            usage_json=None if upstream.usage is None else json.dumps(upstream.usage),
            updated_at=now,
            completed_at=now if upstream.status == STATUS_COMPLETED else None,
        )
        try:
            await asyncio.to_thread(store.insert_media_job, record)
        except Exception as exc:
            logger.error(
                "MEDIA JOB: {} accepted a video job that could not be recorded: {}",
                provider_id,
                type(exc).__name__,
            )
            await capture.finish("error", error=exc)
            return wire.error(
                500,
                f"{provider_id} accepted the video job, but MCC could not record "
                f"it ({type(exc).__name__}), so it cannot be read back.",
                FailureKind.UPSTREAM,
            )
        capture.set_job(record.job_id)
        await capture.finish("success", outputs=MediaOutputs())
        return answer(_job_row(record), upstream)

    return complete


@router.api_route(VIDEOS_ENDPOINT, methods=["HEAD", "OPTIONS"])
async def probe_videos(_auth=Depends(require_proxy_auth)):
    return Response(status_code=204, headers={"Allow": "GET, POST, HEAD, OPTIONS"})


@router.post(VIDEOS_ENDPOINT)
async def create_video(
    request: Request,
    services: ApiServices = Depends(get_services),
    _auth=Depends(require_proxy_auth),
):
    """Start a video job on the Video rail (OpenAI ``videos.create``).

    Multipart -- what the OpenAI SDK always sends, with an optional
    ``input_reference`` file -- or JSON.
    """
    media = services.media
    if (
        media is not None
        and store_from_settings(services.requests.current_settings()) is None
    ):
        return _no_store()
    cleanup = _nothing_to_clean
    uploads: tuple[MediaUpload, ...] = ()
    fields: dict[str, Any]
    multipart = request.headers.get("content-type", "").startswith(
        "multipart/form-data"
    )
    if multipart:
        form = await request.form()

        async def close_form() -> None:
            await form.close()

        try:
            fields, uploads = await _uploads_from_form(form)
        except BaseException:
            await close_form()
            raise
        cleanup = close_form
    else:
        payload = await _read_json_object(request)
        if payload is None:
            return _invalid(
                "The request body must be multipart/form-data or a JSON object."
            )
        fields = dict(payload)
    prompt = fields.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        await cleanup()
        return _invalid("'prompt' is required and must be a string.")
    media_request = MediaRequest(
        operation=MEDIA_OPERATION_VIDEO_CREATE,
        rail=MediaRail.VIDEO,
        model=str(fields.get("model") or ""),
        body={key: value for key, value in fields.items() if key != "model"},
        stream=False,
        uploads=uploads,
        multipart=multipart,
    )
    return await _serve(
        request,
        services,
        media_request,
        endpoint=VIDEOS_ENDPOINT,
        cleanup=cleanup,
        complete=None if media is None else _video_complete(media, media_request),
    )


# -------------------------------------------------------------- pinned calls


def _client_for(
    settings: Settings,
    media: MediaRuntimePort,
    job: Mapping[str, Any],
    wire: MediaWire = OPENAI_WIRE,
) -> MediaJobClient | JSONResponse:
    provider = str(job["provider"])
    gone = _conflict(
        f"The API key that created video {job['job_id']} on {provider} is no "
        "longer configured; a video job can only be read with the key that "
        "created it.",
        wire,
    )
    try:
        client = media.job_client(
            settings,
            provider,
            key_fingerprint=job.get("key_fingerprint"),
            key_index=job.get("key_index"),
            proxy_label=job.get("proxy_label"),
        )
    except ApplicationError:
        return gone
    return gone if client is None else client


def _changes(job: Mapping[str, Any], upstream: UpstreamJob) -> dict[str, Any]:
    """What one retrieve answer changes on the stored job."""
    now = time.time()
    changes: dict[str, Any] = {"updated_at": now}
    if upstream.status_raw is not None:
        changes["status_raw"] = upstream.status_raw
        changes["status"] = upstream.status
    if upstream.progress is not None:
        changes["progress"] = upstream.progress
    if upstream.seconds is not None:
        changes["seconds"] = upstream.seconds
    if upstream.size:
        changes["size"] = upstream.size
    if upstream.error_message:
        changes["error"] = upstream.error_message
    if upstream.usage is not None:
        changes["usage_json"] = json.dumps(upstream.usage)
    if upstream.status == STATUS_COMPLETED and job.get("completed_at") is None:
        changes["completed_at"] = (
            float(upstream.completed_at) if upstream.completed_at else now
        )
    return changes


def _job_usage(job: Mapping[str, Any]) -> dict[str, Any] | None:
    """The usage block a poll last recorded on the job, or None."""
    raw = job.get("usage_json")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        usage = json.loads(raw)
    except ValueError:
        return None
    return usage if isinstance(usage, dict) else None


async def _video_cost(
    pricing: MediaPricing | None, job: Mapping[str, Any], seconds: float
) -> tuple[float, str] | None:
    """The finished video's price, now that its length is known (7.69.0).

    The same ladder the create walked: the host's own ``usage.cost`` from the
    job, else a per-second rate for the seconds the host stated. ``None`` when
    pricing is off or nothing priced it -- the create row then keeps the
    ``unpriced`` it was written with.
    """
    if pricing is None:
        return None
    cost_usd, cost_source = await asyncio.to_thread(
        price_media_row,
        pricing,
        str(job["provider"]),
        str(job["model"]),
        MediaUsage(
            operation=MEDIA_OPERATION_VIDEO_CREATE, output_video_seconds=seconds
        ),
        reported_usd(_job_usage(job)),
    )
    if cost_usd is None or cost_source is None:
        return None
    return cost_usd, cost_source


async def _poll(
    store: RequestLogStore,
    client: MediaJobClient,
    job: Mapping[str, Any],
    request_id: str,
    *,
    pricing: MediaPricing | None = None,
) -> tuple[dict[str, Any], UpstreamJob]:
    """One retrieve on the job's own key; the stored job brought up to date.

    The poll that first reads a finished job's length writes it on the create
    row, and -- with ``pricing`` -- the video's price beside it.
    """
    answer = await client.call(
        MEDIA_OPERATION_VIDEO_RETRIEVE,
        str(job["upstream_id"]),
        model=str(job["model"]),
        request_id=request_id,
    )
    upstream = await asyncio.to_thread(parse_job, answer.body)
    if upstream is None:
        raise ExecutionFailure(
            kind=FailureKind.UPSTREAM,
            status_code=502,
            message=(
                f"{job['provider']} answered the video status request with "
                "something that is not a job."
            ),
            retryable=False,
        )
    job_id = str(job["job_id"])
    changes = _changes(job, upstream)
    await asyncio.to_thread(store.update_media_job, job_id, **changes)
    updated = {**job, **changes}
    seconds = updated.get("seconds")
    if (
        updated.get("status") == STATUS_COMPLETED
        and isinstance(seconds, int | float)
        and not updated.get("row_seconds_written")
    ):
        # The create row may not be flushed yet: then the next poll retries.
        cost = await _video_cost(pricing, updated, float(seconds))
        written = await asyncio.to_thread(
            store.set_request_video_seconds,
            str(job["request_id"]),
            float(seconds),
            cost=cost,
        )
        if written:
            await asyncio.to_thread(
                store.update_media_job, job_id, row_seconds_written=1
            )
            updated["row_seconds_written"] = 1
    return updated, upstream


async def _load(
    settings: Settings, video_id: str, wire: MediaWire = OPENAI_WIRE
) -> tuple[RequestLogStore, dict[str, Any]] | JSONResponse:
    store = store_from_settings(settings)
    if store is None:
        return _no_store(wire)
    job = await asyncio.to_thread(store.media_job, video_id)
    if job is None:
        return _not_found(video_id, wire)
    return store, job


@router.get(VIDEOS_ENDPOINT)
async def list_videos(
    request: Request,
    services: ApiServices = Depends(get_services),
    _auth=Depends(require_proxy_auth),
):
    """MCC's own video jobs (OpenAI ``videos.list``), newest first by default."""
    params = request.query_params
    raw_order = params.get("order") or "desc"
    if raw_order not in {"asc", "desc"}:
        return _invalid("'order' must be 'asc' or 'desc'.")
    order: Literal["asc", "desc"] = "asc" if raw_order == "asc" else "desc"
    limit: int | None = None
    if params.get("limit"):
        try:
            limit = int(params["limit"])
        except ValueError:
            limit = 0
        if limit < 1:
            return _invalid("'limit' must be a positive integer.")
    after = params.get("after") or None
    lease = await services.requests.acquire()
    try:
        store = store_from_settings(lease.settings)
        if store is None:
            return _no_store()
        rows = await asyncio.to_thread(
            store.list_media_jobs,
            after=after,
            limit=None if limit is None else limit + 1,
            order=order,
        )
    finally:
        await lease.release()
    has_more = limit is not None and len(rows) > limit
    data = [video_object(row) for row in rows[:limit]]
    return JSONResponse(
        {
            "object": "list",
            "data": data,
            "first_id": data[0]["id"] if data else None,
            "last_id": data[-1]["id"] if data else None,
            "has_more": has_more,
        }
    )


@router.get(VIDEOS_ENDPOINT + "/{video_id}")
async def retrieve_video(
    video_id: str,
    request: Request,
    services: ApiServices = Depends(get_services),
    _auth=Depends(require_proxy_auth),
):
    """One job's status, read from the host that holds it (``videos.retrieve``)."""
    media = services.media
    if media is None:
        return _no_media()
    lease = await services.requests.acquire()
    try:
        loaded = await _load(lease.settings, video_id)
        if isinstance(loaded, JSONResponse):
            return loaded
        store, job = loaded
        client = _client_for(lease.settings, media, job)
        if isinstance(client, JSONResponse):
            return client
        try:
            updated, upstream = await _poll(
                store,
                client,
                job,
                get_request_id(request),
                pricing=media_pricing(lease.settings),
            )
        except Exception as exc:
            return _failure_response(exc)
        return JSONResponse(video_object(updated, upstream))
    finally:
        await lease.release()


@router.delete(VIDEOS_ENDPOINT + "/{video_id}")
async def delete_video(
    video_id: str,
    request: Request,
    services: ApiServices = Depends(get_services),
    _auth=Depends(require_proxy_auth),
):
    """Delete a job (``videos.delete``): upstream where declared, then MCC's record."""
    media = services.media
    if media is None:
        return _no_media()
    lease = await services.requests.acquire()
    try:
        loaded = await _load(lease.settings, video_id)
        if isinstance(loaded, JSONResponse):
            return loaded
        store, job = loaded
        if _declared(str(job["provider"]), MEDIA_OPERATION_VIDEO_DELETE) is not None:
            client = _client_for(lease.settings, media, job)
            if isinstance(client, JSONResponse):
                return client
            try:
                await client.call(
                    MEDIA_OPERATION_VIDEO_DELETE,
                    str(job["upstream_id"]),
                    model=str(job["model"]),
                    request_id=get_request_id(request),
                )
            except Exception as exc:
                return _failure_response(exc)
        await asyncio.to_thread(store.delete_media_job, video_id)
    finally:
        await lease.release()
    return JSONResponse({"id": video_id, "object": "video.deleted", "deleted": True})


# ------------------------------------------------------------------ content


async def _abandon(download: MediaDownload, tee: MediaFileTee | None) -> None:
    """A download that did not finish: close the host, drop the partial copy."""
    try:
        await download.close()
    except Exception as exc:
        logger.debug("MEDIA JOB: download close failed: {}", type(exc).__name__)
    if tee is not None:
        await asyncio.to_thread(tee.abort)


async def _content_body(
    download: MediaDownload,
    tee: MediaFileTee | None,
    finished: Callable[[MediaFileTee], Awaitable[None]],
) -> AsyncIterator[bytes]:
    """Forward the host's bytes as they come; hash (and keep) them off the loop.

    A client that hangs up closes this generator with ``GeneratorExit``, and
    awaiting during that is unsafe: the cleanup then runs as a task.
    """
    pending = bytearray()
    complete = False
    try:
        async for chunk in download.chunks:
            if not chunk:
                continue
            yield chunk
            if tee is not None:
                pending.extend(chunk)
                if len(pending) >= _TEE_BATCH_BYTES:
                    await asyncio.to_thread(tee.feed, bytes(pending))
                    pending.clear()
        if tee is not None and pending:
            await asyncio.to_thread(tee.feed, bytes(pending))
            pending.clear()
        complete = True
    finally:
        if complete:
            await download.close()
            if tee is not None:
                try:
                    await finished(tee)
                except Exception as exc:
                    logger.warning(
                        "MEDIA JOB: could not record the downloaded video: {}",
                        type(exc).__name__,
                    )
        else:
            asyncio.ensure_future(_abandon(download, tee))


async def _open_download(
    store: RequestLogStore,
    client: MediaJobClient,
    job: Mapping[str, Any],
    *,
    variant: str,
    query: Mapping[str, str],
    request_id: str,
    wire: MediaWire = OPENAI_WIRE,
    pricing: MediaPricing | None = None,
) -> MediaDownload | JSONResponse:
    """Open the job's file on its own key: declared content path, else its URL."""
    video_id = str(job["job_id"])
    provider = str(job["provider"])
    content = _declared(provider, MEDIA_OPERATION_VIDEO_CONTENT)
    upstream: UpstreamJob | None = None
    current: Mapping[str, Any] = job
    if content is None or job.get("status") != STATUS_COMPLETED:
        # Not known finished, or the file lives at a URL only a fresh
        # retrieve answer carries: one retrieve first.
        try:
            current, upstream = await _poll(
                store, client, job, request_id, pricing=pricing
            )
        except Exception as exc:
            return wire.failure(exc)
    status = current.get("status")
    if status != STATUS_COMPLETED:
        return _conflict(
            f"Video {video_id} is not ready (status: {status or 'unknown'}).", wire
        )
    url = None if upstream is None else upstream.result_url
    if content is None and not url:
        return _conflict(
            f"Video {video_id} is not ready (status: {status}; {provider} gave "
            "no address for the file yet).",
            wire,
        )
    try:
        return await client.download(
            operation=None if content is None else MEDIA_OPERATION_VIDEO_CONTENT,
            upstream_id=str(job["upstream_id"]),
            url=None if content is not None else url,
            model=str(job["model"]),
            request_id=request_id,
            query=query,
        )
    except Exception as exc:
        failure = find_execution_failure(exc)
        if failure is not None and failure.status_code in {404, 410}:
            return _expired(video_id, provider, wire)
        return wire.failure(exc)


async def _content(
    lease: RequestRuntimeLease,
    media: MediaRuntimePort,
    video_id: str,
    query: Mapping[str, str],
    request_id: str,
    wire: MediaWire = OPENAI_WIRE,
) -> Response:
    settings = lease.settings
    variant = query.get("variant") or "video"
    loaded = await _load(settings, video_id, wire)
    if isinstance(loaded, JSONResponse):
        return loaded
    store, job = loaded
    provider = str(job["provider"])
    content = _declared(provider, MEDIA_OPERATION_VIDEO_CONTENT)
    if variant != "video" and (content is None or "variant" not in content.query):
        return wire.error(
            400,
            f"{provider} serves only the video (variant 'video'), not '{variant}'.",
            FailureKind.INVALID_REQUEST,
        )
    sha = job.get("content_sha")
    if variant == "video" and isinstance(sha, str) and sha:
        mime = job.get("content_mime") or _DEFAULT_VIDEO_TYPE
        path = media_file_path(media_root(store.db_path), sha, mime)
        if await asyncio.to_thread(path.is_file):
            return FileResponse(path, media_type=mime)
    client = _client_for(settings, media, job, wire)
    if isinstance(client, JSONResponse):
        return client
    opened = await _open_download(
        store,
        client,
        job,
        variant=variant,
        query=query,
        request_id=request_id,
        wire=wire,
        pricing=media_pricing(settings),
    )
    if isinstance(opened, JSONResponse):
        return opened
    download = opened
    mime = _base_type(download.content_type)
    tee: MediaFileTee | None = None
    if variant == "video":
        keep = bool(getattr(settings, "media_store_enabled", False))
        root = media_root(store.db_path) if keep else None
        try:
            tee = await asyncio.to_thread(MediaFileTee, root)
        except OSError as exc:
            logger.warning("MEDIA STORE: cannot keep the video: {}", exc)
            tee = await asyncio.to_thread(MediaFileTee, None)

    cap = media_store_cap_bytes(settings)

    async def finished(done: MediaFileTee) -> None:
        sha256, stored = await asyncio.to_thread(done.finish, mime)
        record = MediaOutputRecord(
            sha256=sha256, mime=mime, bytes=done.size, stored=stored
        )
        await asyncio.to_thread(
            store.record_media_job_content,
            video_id,
            str(job["request_id"]),
            record,
            at=time.time(),
        )
        if stored and cap > 0:
            await asyncio.to_thread(store.trim_media_store, cap)

    return ManagedStreamingResponse(
        _content_body(download, tee, finished), media_type=download.content_type
    )


@router.get(VIDEOS_ENDPOINT + "/{video_id}/content")
async def video_content(
    video_id: str,
    request: Request,
    services: ApiServices = Depends(get_services),
    _auth=Depends(require_proxy_auth),
):
    """The finished file (``videos.download_content``), streamed from the host.

    Served from the media store when this job's video was kept there.
    """
    media = services.media
    if media is None:
        return _no_media()
    lease = await services.requests.acquire()
    try:
        response = await _content(
            lease,
            media,
            video_id,
            dict(request.query_params),
            get_request_id(request),
        )
    except BaseException:
        await lease.release()
        raise
    bound = await bind_response_lifetime(response, lease.release)
    assert isinstance(bound, Response)
    return bound
