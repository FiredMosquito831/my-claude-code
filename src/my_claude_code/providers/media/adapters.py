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

Gemini's native ``generateContent`` (7.66.0) is the one shape that is
translated: speech becomes a text part with ``AUDIO`` out and the voice as a
prebuilt voice, a transcription the audio inline with a fixed instruction
(``core/gemini_native_media.py``). A field with no exact equivalent there is
not sent, and is named in ``WireBody.not_forwarded`` for the request log.
"""

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from my_claude_code.application.media.request import MediaAttempt, MediaUpload
from my_claude_code.config.media_surfaces import (
    MEDIA_ENCODING_JSON,
    MEDIA_SHAPE_GEMINI_TRANSCRIBE,
    MEDIA_SHAPE_GEMINI_TTS,
    MEDIA_SHAPE_MISTRAL_IMAGE_TOOL,
    MEDIA_SHAPE_OPENAI_IMAGES,
    MEDIA_SHAPE_OPENAI_SPEECH,
    MEDIA_SHAPE_OPENAI_TRANSCRIPTIONS,
    MEDIA_SHAPE_OPENAI_VIDEOS,
    MISTRAL_IMAGE_TOOL_TYPE,
    MediaSurface,
)
from my_claude_code.core.gemini_native_media import (
    speech_request,
    transcribe_instruction,
    transcribe_request,
)
from my_claude_code.core.mistral_media import image_tool_request

from .multipart import MultipartBody
from .translation import (
    data_uris_to_uploads,
    has_decodable_data_uris,
    translate_size_and_aspect,
    upload_to_data_uri,
    uploads_to_data_uris,
)

#: Speech fields the Gemini TTS translation consumes: the text, and the
#: framing the leaf honours (a ``stream_format: "sse"`` never gets here).
_GEMINI_SPEECH_USED = frozenset({"input", "response_format", "stream_format"})
#: Transcription fields the Gemini translation consumes (a streamed request
#: never gets here).
_GEMINI_TRANSCRIBE_USED = frozenset({"language", "response_format", "stream"})
#: Image fields the Mistral tool translation consumes: the prompt, and nothing
#: else. A chat call has no ``n``, ``size``, ``quality`` or ``response_format``
#: to put them in, and MCC never translates one by guess -- each is named in
#: ``not_forwarded`` (spec §4.2, the rule ``_GEMINI_TRANSCRIBE_USED`` follows).
_MISTRAL_IMAGE_USED = frozenset({"prompt"})


@dataclass(frozen=True, slots=True)
class WireBody:
    """What one attempt sends: a JSON object, or a streamed multipart form."""

    json: dict[str, Any] | None = None
    multipart: MultipartBody | None = None
    #: A JSON body already encoded, off the loop (it carries base64 audio).
    encoded: bytes | None = None
    #: Client fields this shape has no exact place for, so not sent.
    not_forwarded: tuple[str, ...] = ()
    #: Lossless or opt-in adaptations applied while building this body.
    adaptations: tuple[str, ...] = ()


def _openai_images_body(
    attempt: MediaAttempt,
    *,
    surface: MediaSurface | None = None,
    approximate_mapping: bool = False,
) -> tuple[dict[str, Any], tuple[str, ...], tuple[str, ...]]:
    raw_body = dict(attempt.request.body)
    adaptations: tuple[str, ...] = ()
    not_forwarded: tuple[str, ...] = ()
    renames = dict(surface.renames) if surface is not None else {}
    if "size" in renames and renames["size"] == "aspect_ratio":
        raw_body, adaptations, not_forwarded = translate_size_and_aspect(
            raw_body,
            target_fields=("aspect_ratio", "resolution"),
            approximate=approximate_mapping,
        )
    elif "aspect_ratio" in renames and renames["aspect_ratio"] == "size":
        raw_body, adaptations, not_forwarded = translate_size_and_aspect(
            raw_body,
            target_fields=("size",),
            approximate=approximate_mapping,
        )
    body = (
        {renames.get(key, key): value for key, value in raw_body.items()}
        if renames
        else raw_body
    )
    body["model"] = attempt.resolved.provider_model
    if attempt.request.stream:
        body["stream"] = True
    else:
        body.pop("stream", None)
    return body, adaptations, not_forwarded


def _form_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _openai_multipart(
    attempt: MediaAttempt,
    renames: dict[str, str] | None = None,
    *,
    uploads: tuple[MediaUpload, ...] | None = None,
    skip_fields: frozenset[str] = frozenset(),
) -> MultipartBody:
    fields: list[tuple[str, str]] = [("model", attempt.resolved.provider_model)]
    for name, value in attempt.request.body.items():
        if name in {"model", "stream"} or name in skip_fields:
            continue
        values = value if isinstance(value, list) else [value]
        wire_name = name if renames is None else renames.get(name, name)
        fields.extend((wire_name, _form_value(item)) for item in values)
    if attempt.request.stream:
        fields.append(("stream", "true"))
    return MultipartBody.build(
        fields, attempt.request.uploads if uploads is None else uploads
    )


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


def _openai_videos_body(
    surface: MediaSurface,
    attempt: MediaAttempt,
    *,
    encoding_override: str | None = None,
) -> WireBody:
    """The client's create, in the host's declared encoding and field names.

    A JSON-only host gets JSON (encoding spooled uploads off-loop as ``data:``
    URIs); a form's values arrive as strings, so only theirs are converted (a
    JSON client's types are kept as it sent them). Any other host gets the
    client's own encoding.
    """

    request = attempt.request
    renames = dict(surface.renames)
    effective_encoding = encoding_override or surface.encoding
    form = bool(request.uploads) or request.multipart
    if effective_encoding != MEDIA_ENCODING_JSON and form:
        return WireBody(multipart=_openai_multipart(attempt, renames))
    body: dict[str, Any] = {}
    for name, value in request.body.items():
        if name in {"model", "stream"}:
            continue
        converted = _from_form(value) if form and name not in _TEXT_FIELDS else value
        body[renames.get(name, name)] = converted
    adaptations: list[str] = []
    if request.uploads and effective_encoding == MEDIA_ENCODING_JSON:
        for upload in request.uploads:
            field_name = renames.get(upload.field, upload.field)
            if field_name not in body:
                body[field_name] = upload_to_data_uri(upload)
        adaptations.append("encoding:multipart->json")
        body["model"] = attempt.resolved.provider_model
        return WireBody(
            json=body,
            encoded=json.dumps(body).encode("utf-8"),
            adaptations=tuple(adaptations),
        )
    body["model"] = attempt.resolved.provider_model
    return WireBody(json=body)


def _gemini_speech_body(surface: MediaSurface, attempt: MediaAttempt) -> WireBody:
    """Native TTS: the text, ``AUDIO`` out, a documented voice when one is named.

    A voice the host does not document is not sent (its default voice
    speaks); ``instructions``, ``speed`` and anything else with no exact
    equivalent are not sent either. Each is named in ``not_forwarded``.
    """

    body = attempt.request.body
    text = body.get("input")
    voice = body.get("voice")
    forwarded = (
        voice
        if isinstance(voice, str)
        and voice
        and (surface.voices is None or voice in surface.voices)
        else None
    )
    notes = tuple(
        name
        for name in body
        if name not in _GEMINI_SPEECH_USED
        and not (name == "voice" and forwarded is not None)
    )
    return WireBody(
        json=speech_request(text if isinstance(text, str) else "", forwarded),
        not_forwarded=notes,
    )


def _audio_upload(uploads: tuple[MediaUpload, ...]) -> MediaUpload:
    for upload in uploads:
        if upload.field == "file":
            return upload
    return uploads[0]


def _gemini_transcribe_body(attempt: MediaAttempt) -> WireBody:
    """Native ASR: the instruction and the audio inline. Reads the upload.

    Runs in a worker thread (``build_wire_body``): the spooled copy is read
    whole and base64-encoded into an already encoded JSON body.
    """

    request = attempt.request
    audio = _audio_upload(request.uploads)
    language = request.body.get("language")
    named = language.strip() if isinstance(language, str) else ""
    notes = [name for name in request.body if name not in _GEMINI_TRANSCRIBE_USED]
    if language is not None and not named:
        notes.append("language")
    notes.extend(upload.field for upload in request.uploads if upload is not audio)
    audio.file.seek(0)
    data = audio.file.read()
    audio.file.seek(0)
    return WireBody(
        encoded=transcribe_request(
            transcribe_instruction(named or None),
            audio.content_type or "application/octet-stream",
            data,
        ),
        not_forwarded=tuple(notes),
    )


def _mistral_image_body(attempt: MediaAttempt) -> WireBody:
    """The ``image_generation`` built-in tool on Chat Completions.

    Only the prompt is forwarded: it becomes one user message. Every other
    field the client sent has no place in a chat call and is named in
    ``not_forwarded`` rather than dropped silently or translated by guess.
    """

    body = attempt.request.body
    prompt = body.get("prompt")
    notes = tuple(name for name in body if name not in _MISTRAL_IMAGE_USED)
    return WireBody(
        json=image_tool_request(
            prompt if isinstance(prompt, str) else "",
            attempt.resolved.provider_model,
            MISTRAL_IMAGE_TOOL_TYPE,
        ),
        not_forwarded=notes,
    )


def _needs_off_loop_encoding(
    surface: MediaSurface,
    attempt: MediaAttempt,
    *,
    encoding_override: str | None = None,
) -> bool:
    if surface.shape == MEDIA_SHAPE_GEMINI_TRANSCRIBE:
        return True
    effective_encoding = encoding_override or surface.encoding
    if effective_encoding == MEDIA_ENCODING_JSON and bool(attempt.request.uploads):
        return True
    return (
        effective_encoding == "multipart"
        and not attempt.request.uploads
        and has_decodable_data_uris(attempt.request.body)
    )


async def build_wire_body(
    surface: MediaSurface,
    attempt: MediaAttempt,
    *,
    encoding_override: str | None = None,
    approximate_mapping: bool = False,
) -> WireBody:
    """``build_request_body``, off the loop when the body reads or decodes uploads."""

    if _needs_off_loop_encoding(surface, attempt, encoding_override=encoding_override):
        return await asyncio.to_thread(
            build_request_body,
            surface,
            attempt,
            encoding_override=encoding_override,
            approximate_mapping=approximate_mapping,
        )
    return build_request_body(
        surface,
        attempt,
        encoding_override=encoding_override,
        approximate_mapping=approximate_mapping,
    )


def build_request_body(
    surface: MediaSurface,
    attempt: MediaAttempt,
    *,
    encoding_override: str | None = None,
    approximate_mapping: bool = False,
) -> WireBody:
    """The body for ``attempt`` on ``surface``'s wire shape.

    Operations that read or base64-encode/decode uploads run off-loop when
    called through :func:`build_wire_body`.
    """

    effective_encoding = encoding_override or surface.encoding
    if surface.shape == MEDIA_SHAPE_OPENAI_IMAGES:
        if attempt.request.uploads:
            if effective_encoding == MEDIA_ENCODING_JSON:
                body, adaptations, not_forwarded = _openai_images_body(
                    attempt,
                    surface=surface,
                    approximate_mapping=approximate_mapping,
                )
                if "images" not in body:
                    body["images"] = uploads_to_data_uris(attempt.request.uploads)
                return WireBody(
                    json=body,
                    encoded=json.dumps(body).encode("utf-8"),
                    not_forwarded=not_forwarded,
                    adaptations=(*adaptations, "encoding:multipart->json"),
                )
            renames = dict(surface.renames) or None
            return WireBody(multipart=_openai_multipart(attempt, renames))
        if effective_encoding == "multipart" and has_decodable_data_uris(
            attempt.request.body
        ):
            decoded = data_uris_to_uploads(attempt.request.body.get("images", ()))
            if decoded:
                renames = dict(surface.renames) or None
                return WireBody(
                    multipart=_openai_multipart(
                        attempt,
                        renames,
                        uploads=decoded,
                        skip_fields=frozenset({"images"}),
                    ),
                    adaptations=("encoding:json->multipart",),
                )
        body, adaptations, not_forwarded = _openai_images_body(
            attempt,
            surface=surface,
            approximate_mapping=approximate_mapping,
        )
        return WireBody(
            json=body,
            not_forwarded=not_forwarded,
            adaptations=adaptations,
        )
    if surface.shape == MEDIA_SHAPE_OPENAI_SPEECH:
        # The client spoke this shape too: its fields (``stream_format``
        # included), the rail's model.
        body = dict(attempt.request.body)
        body["model"] = attempt.resolved.provider_model
        return WireBody(json=body)
    if surface.shape == MEDIA_SHAPE_OPENAI_TRANSCRIPTIONS:
        if attempt.request.uploads and effective_encoding == MEDIA_ENCODING_JSON:
            renames = dict(surface.renames)
            body = {
                renames.get(name, name): value
                for name, value in attempt.request.body.items()
                if name not in {"model", "stream"}
            }
            for upload in attempt.request.uploads:
                field_name = renames.get(upload.field, upload.field)
                if field_name not in body:
                    body[field_name] = upload_to_data_uri(upload)
            body["model"] = attempt.resolved.provider_model
            return WireBody(
                json=body,
                encoded=json.dumps(body).encode("utf-8"),
                adaptations=("encoding:multipart->json",),
            )
        # Always an upload: the audio goes out as multipart, streamed.
        return WireBody(multipart=_openai_multipart(attempt))
    if surface.shape == MEDIA_SHAPE_OPENAI_VIDEOS:
        return _openai_videos_body(
            surface, attempt, encoding_override=encoding_override
        )
    if surface.shape == MEDIA_SHAPE_GEMINI_TTS:
        return _gemini_speech_body(surface, attempt)
    if surface.shape == MEDIA_SHAPE_GEMINI_TRANSCRIBE:
        return _gemini_transcribe_body(attempt)
    if surface.shape == MEDIA_SHAPE_MISTRAL_IMAGE_TOOL:
        return _mistral_image_body(attempt)
    raise ValueError(f"unknown media shape {surface.shape!r}")
