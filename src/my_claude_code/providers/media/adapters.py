"""Translate a canonical media request into one wire-shape family's body.

One function per shape FAMILY, chosen by the provider's declared surface --
never by provider id or model name. The OpenAI Images shape needs no
translation: the client already spoke it, so the body is the client's own
fields with ``model`` set to the provider's model id, in the client's own
encoding -- JSON stays JSON, and a multipart upload goes out as multipart,
streamed from the spooled files (``multipart.py``).

The OpenAI videos shape is the same passthrough, with two declared twists a
host may need (``MediaSurface.encoding`` / ``renames``): a host that documents
JSON only gets the SDK's multipart form re-encoded as JSON, and a field the
host names differently is renamed.
"""

from dataclasses import dataclass
from typing import Any

from my_claude_code.application.media.request import MediaAttempt
from my_claude_code.config.media_surfaces import (
    MEDIA_ENCODING_JSON,
    MEDIA_SHAPE_OPENAI_IMAGES,
    MEDIA_SHAPE_OPENAI_SPEECH,
    MEDIA_SHAPE_OPENAI_TRANSCRIPTIONS,
    MEDIA_SHAPE_OPENAI_VIDEOS,
    MediaSurface,
)

from .multipart import MultipartBody


@dataclass(frozen=True, slots=True)
class WireBody:
    """What one attempt sends: a JSON object, or a streamed multipart form."""

    json: dict[str, Any] | None = None
    multipart: MultipartBody | None = None


def _openai_images_body(attempt: MediaAttempt) -> dict[str, Any]:
    body = dict(attempt.request.body)
    body["model"] = attempt.resolved.provider_model
    if attempt.request.stream:
        body["stream"] = True
    else:
        body.pop("stream", None)
    return body


def _form_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _openai_multipart(
    attempt: MediaAttempt, renames: dict[str, str] | None = None
) -> MultipartBody:
    fields: list[tuple[str, str]] = [("model", attempt.resolved.provider_model)]
    for name, value in attempt.request.body.items():
        if name in {"model", "stream"}:
            continue
        values = value if isinstance(value, list) else [value]
        wire_name = name if renames is None else renames.get(name, name)
        fields.extend((wire_name, _form_value(item)) for item in values)
    if attempt.request.stream:
        fields.append(("stream", "true"))
    return MultipartBody.build(fields, attempt.request.uploads)


#: Form fields that are text by nature: never turned into numbers.
_TEXT_FIELDS = frozenset({"prompt", "model"})


def _from_form(value: Any) -> Any:
    """A form's string as the JSON value it stands for: digits, true/false."""

    if isinstance(value, list):
        return [_from_form(item) for item in value]
    if not isinstance(value, str):
        return value
    if value.isascii() and value.isdigit():
        return int(value)
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    return value


def _openai_videos_body(surface: MediaSurface, attempt: MediaAttempt) -> WireBody:
    """The client's create, in the host's declared encoding and field names.

    A JSON-only host gets JSON; a form's values arrive as strings, so only
    theirs are converted (a JSON client's types are kept as it sent them).
    Any other host gets the client's own encoding.
    """

    request = attempt.request
    renames = dict(surface.renames)
    form = bool(request.uploads) or request.multipart
    if surface.encoding != MEDIA_ENCODING_JSON and form:
        return WireBody(multipart=_openai_multipart(attempt, renames))
    body: dict[str, Any] = {}
    for name, value in request.body.items():
        if name in {"model", "stream"}:
            continue
        converted = _from_form(value) if form and name not in _TEXT_FIELDS else value
        body[renames.get(name, name)] = converted
    body["model"] = attempt.resolved.provider_model
    return WireBody(json=body)


def build_request_body(surface: MediaSurface, attempt: MediaAttempt) -> WireBody:
    """The body for ``attempt`` on ``surface``'s wire shape."""

    if surface.shape == MEDIA_SHAPE_OPENAI_IMAGES:
        if attempt.request.uploads:
            return WireBody(multipart=_openai_multipart(attempt))
        return WireBody(json=_openai_images_body(attempt))
    if surface.shape == MEDIA_SHAPE_OPENAI_SPEECH:
        # The client spoke this shape too: its fields (``stream_format``
        # included), the rail's model.
        body = dict(attempt.request.body)
        body["model"] = attempt.resolved.provider_model
        return WireBody(json=body)
    if surface.shape == MEDIA_SHAPE_OPENAI_TRANSCRIPTIONS:
        # Always an upload: the audio goes out as multipart, streamed.
        return WireBody(multipart=_openai_multipart(attempt))
    if surface.shape == MEDIA_SHAPE_OPENAI_VIDEOS:
        return _openai_videos_body(surface, attempt)
    raise ValueError(f"unknown media shape {surface.shape!r}")
