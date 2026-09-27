"""The OpenAI videos wire shape: a job, then reads of the job.

``POST /videos`` answers with a job, not a video. Every host that takes the
OpenAI create names the job with an ``id`` and reports progress through
``GET /videos/{id}``, but each one words its status its own way -- and the
OpenAI SDK's ``poll`` keeps waiting ONLY while the status reads ``queued`` or
``in_progress``: a host's ``processing`` would end the client's wait at once.
So the status is normalised here, from the values the hosts document, and a
value nobody documented is shown exactly as the host said it (never guessed).

What a client sees is always MCC's own job (``video_object``): its own id, and
never the upstream id or a URL the host serves the file at -- those stay
server-side, where the pinned calls use them.

Synchronous and pure: callers run it through ``asyncio.to_thread`` or inline
on a few hundred bytes of JSON.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: Documented status words, by what the OpenAI SDK understands them as.
_STATUS: dict[str, str] = {
    # Accepted, not started. ``pending``: OpenRouter; ``queued``: OpenAI;
    # ``submitted``: job hosts whose status is a free string (DeepInfra).
    "pending": "queued",
    "queued": "queued",
    "submitted": "queued",
    # Running. ``processing``: Gemini's OpenAI layer; ``running``: free-string
    # job hosts; ``in_progress``: OpenAI and OpenRouter.
    "processing": "in_progress",
    "running": "in_progress",
    "in_progress": "in_progress",
    # Done. ``completed``: OpenAI, Gemini, OpenRouter; ``succeeded`` and
    # ``success``: free-string job hosts.
    "completed": "completed",
    "succeeded": "completed",
    "success": "completed",
    # Over without a video. ``failed``: every host; ``error``: free-string
    # hosts; ``cancelled``/``canceled`` and ``expired``: OpenRouter.
    "failed": "failed",
    "error": "failed",
    "cancelled": "failed",
    "canceled": "failed",
    "expired": "failed",
}

STATUS_QUEUED = "queued"
STATUS_IN_PROGRESS = "in_progress"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"


def normalise_status(raw: str) -> str:
    """The OpenAI status for a host's word; an unknown word is returned as-is."""

    return _STATUS.get(raw.strip().lower(), raw)


@dataclass(frozen=True, slots=True)
class UpstreamJob:
    """What one host answer says about a job. ``None`` = the host did not say."""

    id: str | None
    status_raw: str | None = None
    status: str | None = None
    progress: int | None = None
    seconds: float | None = None
    size: str | None = None
    error_message: str | None = None
    #: Where the host serves the finished file. Server-side only.
    result_url: str | None = None
    usage: dict[str, Any] | None = None
    created_at: int | None = None
    completed_at: int | None = None
    expires_at: int | None = None


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _epoch(value: Any) -> int | None:
    number = None if isinstance(value, str) else _number(value)
    return None if number is None else int(number)


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _object(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {str(key): item for key, item in value.items()}


def _error_message(value: Any) -> str | None:
    text = _text(value)
    if text is not None:
        return text
    nested = _object(value)
    return None if nested is None else _text(nested.get("message"))


def _result_url(data: Mapping[str, Any]) -> str | None:
    """``url`` (Gemini), ``video.url``, ``unsigned_urls[0]`` (OpenRouter), ..."""

    direct = _text(data.get("url"))
    if direct is not None:
        return direct
    video = _object(data.get("video"))
    if video is not None and _text(video.get("url")) is not None:
        return _text(video.get("url"))
    urls = data.get("unsigned_urls")
    if isinstance(urls, list):
        for url in urls:
            if _text(url) is not None:
                return url
    for holder in ("outputs", "content"):
        nested = _object(data.get(holder))
        if nested is not None and _text(nested.get("video_url")) is not None:
            return _text(nested.get("video_url"))
    return None


def parse_job(body: bytes) -> UpstreamJob | None:
    """Read a create or retrieve answer; ``None`` when it is not a JSON object.

    A job whose ``id`` is missing or not a non-empty string parses with
    ``id=None``: on a create that is "not accepted".
    """

    try:
        payload = json.loads(body)
    except ValueError:
        return None
    data = _object(payload)
    if data is None:
        return None
    status_raw = _text(data.get("status"))
    status = None if status_raw is None else normalise_status(status_raw)
    progress_value = _number(data.get("progress"))
    progress = None if progress_value is None else max(0, min(100, int(progress_value)))
    if status == STATUS_COMPLETED:
        progress = 100
    seconds: float | None = None
    for name in ("seconds", "duration", "duration_seconds"):
        seconds = _number(data.get(name))
        if seconds is not None:
            break
    identifier = _text(data.get("id"))
    return UpstreamJob(
        id=None if identifier is None else identifier.strip(),
        status_raw=status_raw,
        status=status,
        progress=progress,
        seconds=seconds,
        size=_text(data.get("size")),
        error_message=_error_message(data.get("error")),
        result_url=_result_url(data),
        usage=_object(data.get("usage")),
        created_at=_epoch(data.get("created_at")),
        completed_at=_epoch(data.get("completed_at")),
        expires_at=_epoch(data.get("expires_at")),
    )


def seconds_text(seconds: float) -> str:
    """Seconds the way OpenAI types them: a string, ``"8"`` rather than 8.0."""

    return str(int(seconds)) if float(seconds).is_integer() else f"{seconds:g}"


def video_object(
    job: Mapping[str, Any], upstream: UpstreamJob | None = None
) -> dict[str, Any]:
    """The OpenAI ``Video`` object for one MCC job.

    ``job`` is the stored row (``media_jobs``); ``upstream`` the host's latest
    answer, whose facts win over the stored ones. The id is MCC's; the
    upstream id and any URL never leave the server.
    """

    fresh = upstream or UpstreamJob(id=None)
    status = fresh.status or job.get("status") or STATUS_QUEUED
    progress = fresh.progress if fresh.progress is not None else job.get("progress")
    if progress is None:
        progress = 100 if status == STATUS_COMPLETED else 0
    model = job.get("requested_model") or f"{job.get('provider')}/{job.get('model')}"
    video: dict[str, Any] = {
        "id": job["job_id"],
        "object": "video",
        "model": model,
        "status": status,
        "progress": int(progress),
        "created_at": int(job.get("created_at") or 0),
    }
    completed_at = fresh.completed_at or _epoch(job.get("completed_at"))
    if completed_at is not None:
        video["completed_at"] = completed_at
    if fresh.expires_at is not None:
        video["expires_at"] = fresh.expires_at
    seconds = fresh.seconds if fresh.seconds is not None else job.get("seconds")
    if isinstance(seconds, int | float) and not isinstance(seconds, bool):
        video["seconds"] = seconds_text(float(seconds))
    size = fresh.size or job.get("size")
    if size:
        video["size"] = size
    prompt = job.get("prompt")
    if prompt:
        video["prompt"] = prompt
    video["error"] = None
    if status == STATUS_FAILED:
        message = fresh.error_message or job.get("error")
        raw = fresh.status_raw or job.get("status_raw") or STATUS_FAILED
        video["error"] = {
            "code": raw,
            "message": message or f"The provider reported the video as {raw}.",
        }
    return video
