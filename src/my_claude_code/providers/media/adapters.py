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
    MEDIA_OPERATION_IMAGE_VARIATION,
    MEDIA_SHAPE_GEMINI_GENERATE_CONTENT_IMAGE,
    MEDIA_SHAPE_GEMINI_NATIVE_OCR,
    MEDIA_SHAPE_GEMINI_PREDICT_IMAGE,
    MEDIA_SHAPE_GEMINI_TRANSCRIBE,
    MEDIA_SHAPE_GEMINI_TTS,
    MEDIA_SHAPE_GEMINI_VEO_LRO,
    MEDIA_SHAPE_MISTRAL_IMAGE_TOOL,
    MEDIA_SHAPE_MISTRAL_OCR,
    MEDIA_SHAPE_OPENAI_IMAGES,
    MEDIA_SHAPE_OPENAI_SPEECH,
    MEDIA_SHAPE_OPENAI_TRANSCRIPTIONS,
    MEDIA_SHAPE_OPENAI_VIDEOS,
    MEDIA_SHAPE_OPENROUTER_CHAT_IMAGE,
    MEDIA_SHAPE_VISION_OCR_CHAT,
    MISTRAL_IMAGE_TOOL_TYPE,
    MediaSurface,
)
from my_claude_code.core.gemini_native_media import (
    image_generate_request,
    image_predict_request,
    speech_request,
    transcribe_instruction,
    transcribe_request,
    veo_lro_request,
)
from my_claude_code.core.mistral_media import image_tool_request
from my_claude_code.core.ocr_media import (
    gemini_ocr_request,
    mistral_ocr_request,
    vision_chat_ocr_request,
)
from my_claude_code.core.openrouter_media import openrouter_chat_image_request

from .multipart import MultipartBody
from .translation import (
    data_uris_to_uploads,
    extract_image_url,
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
_OPENROUTER_IMAGE_USED = frozenset(
    {"prompt", "size", "aspect_ratio", "image_size", "image_config", "images", "image"}
)
_GEMINI_IMAGE_USED = frozenset(
    {"prompt", "size", "aspect_ratio", "image_size", "image_config", "images", "image"}
)
_GEMINI_PREDICT_USED = frozenset(
    {"prompt", "n", "size", "aspect_ratio", "image_config"}
)
_GEMINI_VEO_USED = frozenset(
    {
        "prompt",
        "seconds",
        "size",
        "aspect_ratio",
        "resolution",
        "negative_prompt",
        "seed",
        "input_reference",
        "video",
    }
)
_MISTRAL_OCR_USED = frozenset(
    {"file", "document", "document_url", "image_url", "pages", "include_image_base64"}
)
_GEMINI_OCR_USED = frozenset(
    {"file", "document", "document_url", "image_url", "prompt", "output_format"}
)
_VISION_OCR_CHAT_USED = frozenset(
    {"file", "document", "document_url", "image_url", "prompt", "output_format"}
)
_OFF_LOOP_SHAPES = frozenset(
    {
        MEDIA_SHAPE_GEMINI_TRANSCRIBE,
        MEDIA_SHAPE_OPENROUTER_CHAT_IMAGE,
        MEDIA_SHAPE_GEMINI_GENERATE_CONTENT_IMAGE,
        MEDIA_SHAPE_GEMINI_PREDICT_IMAGE,
        MEDIA_SHAPE_GEMINI_VEO_LRO,
        MEDIA_SHAPE_MISTRAL_OCR,
        MEDIA_SHAPE_GEMINI_NATIVE_OCR,
        MEDIA_SHAPE_VISION_OCR_CHAT,
    }
)


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


def _prompt_text(attempt: MediaAttempt) -> str:
    prompt = attempt.request.body.get("prompt")
    if isinstance(prompt, str) and prompt.strip():
        return prompt
    if attempt.request.operation == MEDIA_OPERATION_IMAGE_VARIATION:
        return "Create a variation of this image."
    return ""


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        num = int(value.strip())
        return num if num > 0 else None
    return None


def _read_upload_bytes(upload: MediaUpload) -> tuple[str, bytes]:
    upload.file.seek(0)
    data = upload.file.read()
    upload.file.seek(0)
    return (upload.content_type or "image/png"), data


def _extract_input_images(attempt: MediaAttempt) -> list[tuple[str, bytes]]:
    if attempt.request.uploads:
        return [_read_upload_bytes(upload) for upload in attempt.request.uploads]
    raw_images = attempt.request.body.get("images")
    if isinstance(raw_images, list | tuple) and has_decodable_data_uris(
        attempt.request.body
    ):
        decoded = data_uris_to_uploads(raw_images)
        try:
            return [_read_upload_bytes(upload) for upload in decoded]
        finally:
            for upload in decoded:
                upload.file.close()
    return []


def _image_config_fields(body: dict[str, Any]) -> tuple[str | None, str | None]:
    config = body.get("image_config")
    cfg = config if isinstance(config, dict) else {}
    aspect = (
        body.get("aspect_ratio") or cfg.get("aspect_ratio") or cfg.get("aspectRatio")
    )
    img_size = body.get("image_size") or cfg.get("image_size") or cfg.get("imageSize")
    return (
        aspect.strip() if isinstance(aspect, str) and aspect.strip() else None,
        img_size.strip() if isinstance(img_size, str) and img_size.strip() else None,
    )


def _openrouter_chat_image_body(
    attempt: MediaAttempt, *, approximate_mapping: bool = False
) -> WireBody:
    raw_body = dict(attempt.request.body)
    cfg_aspect, image_size = _image_config_fields(raw_body)
    if cfg_aspect and "aspect_ratio" not in raw_body:
        raw_body["aspect_ratio"] = cfg_aspect
    translated, adaptations, size_nf = translate_size_and_aspect(
        raw_body,
        target_fields=("aspect_ratio", "resolution"),
        approximate=approximate_mapping,
    )
    aspect_ratio = translated.get("aspect_ratio")
    aspect_str = (
        aspect_ratio.strip()
        if isinstance(aspect_ratio, str) and aspect_ratio.strip()
        else None
    )
    uris: list[str] = []
    if attempt.request.uploads:
        uris.extend(upload_to_data_uri(upload) for upload in attempt.request.uploads)
    raw_images = attempt.request.body.get("images")
    if isinstance(raw_images, list | tuple):
        for entry in raw_images:
            url = extract_image_url(entry)
            if isinstance(url, str) and url.strip():
                uris.append(url.strip())
    single_image = attempt.request.body.get("image")
    if not uris and single_image is not None:
        url = extract_image_url(single_image)
        if isinstance(url, str) and url.strip():
            uris.append(url.strip())
    notes = list(size_nf)
    for name in attempt.request.body:
        if name not in _OPENROUTER_IMAGE_USED and name not in notes:
            notes.append(name)
    payload = openrouter_chat_image_request(
        _prompt_text(attempt),
        attempt.resolved.provider_model,
        image_data_uris=uris,
        aspect_ratio=aspect_str,
        image_size=image_size,
    )
    return WireBody(
        json=payload,
        encoded=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        not_forwarded=tuple(notes),
        adaptations=adaptations,
    )


def _gemini_generate_content_image_body(
    attempt: MediaAttempt, *, approximate_mapping: bool = False
) -> WireBody:
    raw_body = dict(attempt.request.body)
    cfg_aspect, image_size = _image_config_fields(raw_body)
    if cfg_aspect and "aspect_ratio" not in raw_body:
        raw_body["aspect_ratio"] = cfg_aspect
    translated, adaptations, size_nf = translate_size_and_aspect(
        raw_body,
        target_fields=("aspect_ratio",),
        approximate=approximate_mapping,
    )
    aspect_ratio = translated.get("aspect_ratio")
    aspect_str = (
        aspect_ratio.strip()
        if isinstance(aspect_ratio, str) and aspect_ratio.strip()
        else None
    )
    input_images = _extract_input_images(attempt)
    notes = list(size_nf)
    for name in attempt.request.body:
        if name not in _GEMINI_IMAGE_USED and name not in notes:
            notes.append(name)
    payload = image_generate_request(
        _prompt_text(attempt),
        aspect_ratio=aspect_str,
        image_size=image_size,
        input_images=input_images,
    )
    return WireBody(
        json=payload,
        encoded=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        not_forwarded=tuple(notes),
        adaptations=adaptations,
    )


def _gemini_predict_image_body(
    attempt: MediaAttempt, *, approximate_mapping: bool = False
) -> WireBody:
    raw_body = dict(attempt.request.body)
    cfg_aspect, _image_size = _image_config_fields(raw_body)
    if cfg_aspect and "aspect_ratio" not in raw_body:
        raw_body["aspect_ratio"] = cfg_aspect
    translated, adaptations, size_nf = translate_size_and_aspect(
        raw_body,
        target_fields=("aspect_ratio",),
        approximate=approximate_mapping,
    )
    aspect_ratio = translated.get("aspect_ratio")
    aspect_str = (
        aspect_ratio.strip()
        if isinstance(aspect_ratio, str) and aspect_ratio.strip()
        else None
    )
    n = _positive_int(raw_body.get("n"))
    notes = list(size_nf)
    for name in attempt.request.body:
        if name not in _GEMINI_PREDICT_USED and name not in notes:
            notes.append(name)
    payload = image_predict_request(
        _prompt_text(attempt),
        n=n,
        aspect_ratio=aspect_str,
    )
    return WireBody(
        json=payload,
        encoded=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        not_forwarded=tuple(notes),
        adaptations=adaptations,
    )


def _gemini_veo_lro_body(
    attempt: MediaAttempt, *, approximate_mapping: bool = False
) -> WireBody:
    raw_body = dict(attempt.request.body)
    translated, adaptations, size_nf = translate_size_and_aspect(
        raw_body,
        target_fields=("aspect_ratio", "resolution"),
        approximate=approximate_mapping,
    )
    params: dict[str, Any] = {}
    seconds_val = translated.get("seconds")
    if seconds_val is not None and not isinstance(seconds_val, bool):
        if isinstance(seconds_val, int | float):
            params["durationSeconds"] = (
                int(seconds_val)
                if float(seconds_val).is_integer()
                else float(seconds_val)
            )
        elif isinstance(seconds_val, str) and seconds_val.strip():
            try:
                num = float(seconds_val.strip())
                params["durationSeconds"] = int(num) if num.is_integer() else num
            except ValueError:
                pass
    aspect_ratio = translated.get("aspect_ratio")
    if isinstance(aspect_ratio, str) and aspect_ratio.strip():
        params["aspectRatio"] = aspect_ratio.strip()
    resolution = translated.get("resolution")
    if isinstance(resolution, str) and resolution.strip():
        params["resolution"] = resolution.strip()
    negative_prompt = translated.get("negative_prompt")
    if isinstance(negative_prompt, str) and negative_prompt.strip():
        params["negativePrompt"] = negative_prompt.strip()
    seed = translated.get("seed")
    if seed is not None and not isinstance(seed, bool):
        if isinstance(seed, int):
            params["seed"] = seed
        elif isinstance(seed, str) and seed.strip().isdigit():
            params["seed"] = int(seed.strip())

    input_image: tuple[str, bytes] | None = None
    if attempt.request.uploads:
        input_image = _read_upload_bytes(attempt.request.uploads[0])
    elif raw_body.get("input_reference") is not None:
        ref = raw_body["input_reference"]
        decoded = data_uris_to_uploads([ref])
        try:
            if decoded:
                input_image = _read_upload_bytes(decoded[0])
        finally:
            for upload in decoded:
                upload.file.close()

    notes = list(size_nf)
    for name in attempt.request.body:
        if name not in _GEMINI_VEO_USED and name not in notes:
            notes.append(name)
    payload = veo_lro_request(
        _prompt_text(attempt),
        params=params or None,
        input_image=input_image,
    )
    return WireBody(
        json=payload,
        encoded=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        not_forwarded=tuple(notes),
        adaptations=adaptations,
    )


def _parse_ocr_pages_form(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped:
        return None
    if stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, list):
                return parsed
        except ValueError:
            pass
    parts = [part.strip() for part in stripped.split(",") if part.strip()]
    if parts and all(part.isdigit() for part in parts):
        return [int(part) for part in parts]
    return value


def _mistral_ocr_body(attempt: MediaAttempt) -> WireBody:
    raw_body = dict(attempt.request.body)
    if attempt.request.multipart or attempt.request.uploads:
        if "pages" in raw_body:
            parsed_pages = _parse_ocr_pages_form(raw_body["pages"])
            if parsed_pages is None:
                raw_body.pop("pages", None)
            else:
                raw_body["pages"] = parsed_pages
        if "include_image_base64" in raw_body:
            raw_body["include_image_base64"] = _from_form(
                raw_body["include_image_base64"]
            )
    data_uri = (
        upload_to_data_uri(attempt.request.uploads[0])
        if attempt.request.uploads
        else None
    )
    adaptations: tuple[str, ...] = (
        ("encoding:multipart->json",) if attempt.request.uploads else ()
    )
    notes = tuple(name for name in raw_body if name not in _MISTRAL_OCR_USED)
    payload = mistral_ocr_request(
        raw_body, model=attempt.resolved.provider_model, data_uri=data_uri
    )
    return WireBody(
        json=payload,
        encoded=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        not_forwarded=notes,
        adaptations=adaptations,
    )


def _gemini_ocr_body(attempt: MediaAttempt) -> WireBody:
    raw_body = dict(attempt.request.body)
    data: bytes | None = None
    mime_type: str | None = None
    if attempt.request.uploads:
        mime_type, data = _read_upload_bytes(attempt.request.uploads[0])
    adaptations: tuple[str, ...] = (
        ("encoding:multipart->json",) if attempt.request.uploads else ()
    )
    notes = tuple(name for name in raw_body if name not in _GEMINI_OCR_USED)
    payload = gemini_ocr_request(raw_body, data=data, mime_type=mime_type)
    return WireBody(
        json=payload,
        encoded=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        not_forwarded=notes,
        adaptations=adaptations,
    )


def _vision_ocr_chat_body(attempt: MediaAttempt) -> WireBody:
    raw_body = dict(attempt.request.body)
    data_uri: str | None = None
    filename = "document.pdf"
    if attempt.request.uploads:
        upload = attempt.request.uploads[0]
        data_uri = upload_to_data_uri(upload)
        if upload.filename:
            filename = upload.filename
    adaptations: tuple[str, ...] = (
        ("encoding:multipart->json",) if attempt.request.uploads else ()
    )
    notes = tuple(name for name in raw_body if name not in _VISION_OCR_CHAT_USED)
    payload = vision_chat_ocr_request(
        raw_body,
        model=attempt.resolved.provider_model,
        data_uri=data_uri,
        filename=filename,
    )
    return WireBody(
        json=payload,
        encoded=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        not_forwarded=notes,
        adaptations=adaptations,
    )


def _needs_off_loop_encoding(
    surface: MediaSurface,
    attempt: MediaAttempt,
    *,
    encoding_override: str | None = None,
) -> bool:
    if surface.shape in _OFF_LOOP_SHAPES:
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
    if surface.shape == MEDIA_SHAPE_OPENROUTER_CHAT_IMAGE:
        return _openrouter_chat_image_body(
            attempt, approximate_mapping=approximate_mapping
        )
    if surface.shape == MEDIA_SHAPE_GEMINI_GENERATE_CONTENT_IMAGE:
        return _gemini_generate_content_image_body(
            attempt, approximate_mapping=approximate_mapping
        )
    if surface.shape == MEDIA_SHAPE_GEMINI_PREDICT_IMAGE:
        return _gemini_predict_image_body(
            attempt, approximate_mapping=approximate_mapping
        )
    if surface.shape == MEDIA_SHAPE_GEMINI_VEO_LRO:
        return _gemini_veo_lro_body(attempt, approximate_mapping=approximate_mapping)
    if surface.shape == MEDIA_SHAPE_MISTRAL_OCR:
        return _mistral_ocr_body(attempt)
    if surface.shape == MEDIA_SHAPE_GEMINI_NATIVE_OCR:
        return _gemini_ocr_body(attempt)
    if surface.shape == MEDIA_SHAPE_VISION_OCR_CHAT:
        return _vision_ocr_chat_body(attempt)
    raise ValueError(f"unknown media shape {surface.shape!r}")
