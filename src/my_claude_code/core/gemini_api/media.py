"""Gemini-shaped media requests, and the answers a Gemini client reads back.

A Gemini client asks for a picture or for speech the way it asks for text --
``:generateContent`` with ``generationConfig.responseModalities`` naming
``IMAGE`` or ``AUDIO`` -- and for a video with Veo's ``:predictLongRunning``.
MCC serves all three on its media rails, and every upstream surface on those
rails is OpenAI-shaped, so this module turns the Gemini body into the OpenAI
fields the rail sends and builds the Gemini answer from what came back.

Nothing is translated by guess. A Gemini field with no OpenAI field that means
exactly the same thing (``imageConfig.aspectRatio``, ``temperature``,
``personGeneration``, ...) is not sent; it is named in ``not_forwarded``, which
the request log keeps under ``media.not_forwarded``. An aspect ratio is never
turned into a pixel size.

Pure and synchronous. The functions that touch multi-megabyte base64 or JSON
(``openai_image_parts``, ``fetched_image_part``, ``audio_part``,
``encode_media_answer``) are only ever called through ``asyncio.to_thread``.
"""

import base64
import binascii
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from my_claude_code.core.media_outputs import GeneratedMedia
from my_claude_code.core.media_store import sha256_hex
from my_claude_code.core.openai_images import sniff_image_mime
from my_claude_code.core.openai_videos import STATUS_COMPLETED, STATUS_FAILED

from .errors import GeminiConversionError
from .events import format_gemini_sse_data
from .models import GeminiGenerateContentRequest, GeminiGenerationConfig

#: ``responseModalities`` members that send a ``generateContent`` to a media rail.
MODALITY_IMAGE = "IMAGE"
MODALITY_AUDIO = "AUDIO"

#: The ``@type`` Google's finished Veo operation carries in ``response``.
PREDICT_LONG_RUNNING_RESPONSE_TYPE = (
    "type.googleapis.com/google.ai.generativelanguage.v1beta.PredictLongRunningResponse"
)

#: ``google.rpc.Code.INTERNAL``, the code a failed job's ``error`` carries: the
#: host's own words are the message, and no host names a code class for them.
FAILED_JOB_CODE = 13

#: Veo ``parameters`` with an exact OpenAI-videos field, by the names Gemini's
#: own OpenAI layer documents for its create. Every other parameter is listed
#: in ``not_forwarded``.
_VIDEO_PARAMETERS: dict[str, str] = {
    "durationSeconds": "seconds",
    "aspectRatio": "aspect_ratio",
    "resolution": "resolution",
    "negativePrompt": "negative_prompt",
    "seed": "seed",
}

#: Part keys this module reads (or deliberately passes over, like a thought).
_PART_KEYS_READ = frozenset(
    {
        "text",
        "inlineData",
        "inline_data",
        "thought",
        "thoughtSignature",
        "thought_signature",
    }
)

#: Declared request fields no media rail has a use for: ``(attribute, wire name)``.
_UNUSED_REQUEST_FIELDS = (
    ("tools", "tools"),
    ("tool_config", "toolConfig"),
    ("safety_settings", "safetySettings"),
    ("cached_content", "cachedContent"),
)

#: What an OpenAI ``output_format`` names, as a MIME type.
_FORMAT_MIME: dict[str, str] = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "jpg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
}

#: MCC's own video job id (``media_jobs.job_id``).
_MCC_JOB_ID = re.compile(r"video_[0-9a-f]{32}")
#: The same id as a video's ``uri`` names it (``file_id_for_job``).
_FILE_ID = re.compile(r"video([0-9a-f]{32})")


@dataclass(frozen=True, slots=True)
class InlineMedia:
    """One ``inlineData`` blob a client sent: its declared type and its base64."""

    mime_type: str
    #: Base64 exactly as the client sent it; decoded only off the event loop.
    data: str


@dataclass(frozen=True, slots=True)
class GeminiMediaAsk:
    """What one Gemini body asks a media rail for, as OpenAI-shaped fields."""

    body: dict[str, Any]
    #: Images to upload (an edit's pictures, a video's first frame).
    images: tuple[InlineMedia, ...] = ()
    #: Every field the client sent that is not carried upstream.
    not_forwarded: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _Contents:
    texts: tuple[str, ...]
    media: tuple[InlineMedia, ...]
    notes: tuple[str, ...]


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(word[:1].upper() + word[1:] for word in rest)


def _mapping(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {str(key): item for key, item in value.items()}


def _pick(mapping: Mapping[str, Any], *names: str) -> Any:
    """The first of ``names`` present: Google accepts camelCase and snake_case."""

    for name in names:
        if name in mapping:
            return mapping[name]
    return None


def _unique(notes: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(notes))


def _text_parts(parts: Sequence[Mapping[str, Any]] | None) -> list[str]:
    texts: list[str] = []
    for part in parts or ():
        text = part.get("text")
        if isinstance(text, str) and text and not part.get("thought"):
            texts.append(text)
    return texts


def response_modalities(request: GeminiGenerateContentRequest) -> frozenset[str]:
    """The ``responseModalities`` a request names, upper-cased; empty when none."""

    config = request.generation_config
    extra = None if config is None else config.model_extra
    if not extra:
        return frozenset()
    raw = _pick(extra, "responseModalities", "response_modalities")
    values: list[Any] = (
        [raw] if isinstance(raw, str) else list(raw) if isinstance(raw, list) else []
    )
    return frozenset(
        value.strip().upper()
        for value in values
        if isinstance(value, str) and value.strip()
    )


def media_output(request: GeminiGenerateContentRequest) -> str | None:
    """``IMAGE`` or ``AUDIO`` when the request wants media; ``None`` means chat.

    Absent, or ``TEXT`` only, is the chat path exactly as before. Both media
    kinds at once cannot be served by one rail, so it is a 400.
    """

    modalities = response_modalities(request)
    image = MODALITY_IMAGE in modalities
    audio = MODALITY_AUDIO in modalities
    if image and audio:
        raise GeminiConversionError(
            "generationConfig.responseModalities names both IMAGE and AUDIO. "
            "MCC routes an image request to the Image rail (MODEL_IMAGE) and a "
            "speech request to the Speech rail (MODEL_TTS); ask for one per "
            "request.",
            field="generationConfig.responseModalities",
        )
    if image:
        return MODALITY_IMAGE
    if audio:
        return MODALITY_AUDIO
    return None


def _inline(part: Mapping[str, Any]) -> InlineMedia | None:
    blob = _mapping(_pick(part, "inlineData", "inline_data"))
    if blob is None:
        return None
    data = blob.get("data")
    if not isinstance(data, str) or not data:
        return None
    mime = _pick(blob, "mimeType", "mime_type")
    return InlineMedia(
        mime_type=mime.strip().lower() if isinstance(mime, str) else "", data=data
    )


def _read_contents(request: GeminiGenerateContentRequest) -> _Contents:
    """The user's text, every inline blob, and the parts nothing reads."""

    texts: list[str] = []
    media: list[InlineMedia] = []
    notes: list[str] = []
    for content in request.content_list:
        role = (content.role or "user").strip().lower()
        for part in content.parts or ():
            blob = _inline(part)
            if blob is not None:
                media.append(blob)
            text = part.get("text")
            if isinstance(text, str) and text and not part.get("thought"):
                if role == "user":
                    texts.append(text)
                else:
                    notes.append(f"contents[role={role}].text")
            notes.extend(
                f"contents.parts.{key}" for key in part if key not in _PART_KEYS_READ
            )
    return _Contents(tuple(texts), tuple(media), tuple(notes))


def _system_text(request: GeminiGenerateContentRequest) -> str:
    value = request.system_instruction
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return "\n".join(_text_parts(value.parts))


def _config_notes(
    config: GeminiGenerationConfig | None, used: frozenset[str]
) -> list[str]:
    """Every ``generationConfig`` field set, except the ``used`` ones."""

    if config is None:
        return []
    notes: list[str] = []
    for name in GeminiGenerationConfig.model_fields:
        if name not in config.model_fields_set or getattr(config, name) is None:
            continue
        if _camel(name) not in used:
            notes.append(f"generationConfig.{_camel(name)}")
    for key, value in (config.model_extra or {}).items():
        if _camel(key) in used:
            continue
        nested = _mapping(value)
        if nested:
            notes.extend(f"generationConfig.{key}.{sub}" for sub in nested)
        else:
            notes.append(f"generationConfig.{key}")
    return notes


def _request_notes(
    request: GeminiGenerateContentRequest,
    *,
    system_used: bool,
    read: frozenset[str] = frozenset(),
) -> list[str]:
    """The request's own fields no rail carries (``read``: extras used here)."""

    notes: list[str] = []
    if not system_used and request.system_instruction is not None:
        notes.append("systemInstruction")
    notes.extend(
        label
        for attribute, label in _UNUSED_REQUEST_FIELDS
        if getattr(request, attribute) is not None
    )
    notes.extend(str(key) for key in request.model_extra or {} if key not in read)
    return notes


def image_ask(request: GeminiGenerateContentRequest) -> GeminiMediaAsk:
    """An ``IMAGE`` request as the OpenAI Images fields.

    ``prompt`` is the user's text parts joined with newlines, the system
    instruction first when there is one; ``n`` is ``candidateCount``; the
    answer is asked for as ``b64_json`` because a Gemini client needs the
    bytes. Inline images in ``contents`` make it an edit (their uploads).
    """

    contents = _read_contents(request)
    notes = list(contents.notes)
    images: list[InlineMedia] = []
    for blob in contents.media:
        if blob.mime_type.startswith("image/"):
            images.append(blob)
        else:
            notes.append(
                f"contents.parts.inlineData({blob.mime_type or 'no mimeType'})"
            )
    prompt = "\n".join(
        text for text in (_system_text(request), *contents.texts) if text
    )
    if not prompt.strip():
        raise GeminiConversionError(
            "An image request needs a text prompt: no text part was found in contents.",
            field="contents",
        )
    body: dict[str, Any] = {"prompt": prompt}
    config = request.generation_config
    if config is not None and config.candidate_count is not None:
        body["n"] = config.candidate_count
    body["response_format"] = "b64_json"
    notes.extend(
        _config_notes(config, frozenset({"responseModalities", "candidateCount"}))
    )
    notes.extend(_request_notes(request, system_used=True))
    return GeminiMediaAsk(body=body, images=tuple(images), not_forwarded=_unique(notes))


def _voice(config: GeminiGenerationConfig | None) -> tuple[str | None, list[str]]:
    """``speechConfig``'s voice name, and the parts of it that are not a voice."""

    speech = (
        None
        if config is None
        else _pick(config.model_extra or {}, "speechConfig", "speech_config")
    )
    speech_config = _mapping(speech)
    if speech_config is None:
        return None, []
    notes = [
        f"generationConfig.speechConfig.{key}"
        for key in speech_config
        if _camel(key) != "voiceConfig"
    ]
    voice_config = _mapping(_pick(speech_config, "voiceConfig", "voice_config"))
    if voice_config is None:
        return None, notes
    voice: str | None = None
    prebuilt = _mapping(
        _pick(voice_config, "prebuiltVoiceConfig", "prebuilt_voice_config")
    )
    if prebuilt is not None:
        name = _pick(prebuilt, "voiceName", "voice_name")
        if isinstance(name, str) and name.strip():
            voice = name.strip()
    direct = voice_config.get("voice")
    if voice is None and isinstance(direct, str) and direct.strip():
        voice = direct.strip()
    notes.extend(
        f"generationConfig.speechConfig.voiceConfig.{key}"
        for key in voice_config
        if _camel(key) not in {"prebuiltVoiceConfig", "voice"}
    )
    return voice, notes


def speech_ask(request: GeminiGenerateContentRequest) -> GeminiMediaAsk:
    """An ``AUDIO`` request as the OpenAI speech fields.

    ``input`` is the user's text parts joined; ``voice`` is the prebuilt
    voice's name when one is given. No ``response_format``: the host answers
    in its own format, never transcoded (user decision 9).
    """

    contents = _read_contents(request)
    notes = list(contents.notes)
    if contents.media:
        notes.append("contents.parts.inlineData")
    text = "\n".join(contents.texts)
    if not text.strip():
        raise GeminiConversionError(
            "A speech request needs the text to speak: no text part was found "
            "in contents.",
            field="contents",
        )
    body: dict[str, Any] = {"input": text}
    config = request.generation_config
    voice, voice_notes = _voice(config)
    if voice is not None:
        body["voice"] = voice
    notes.extend(
        _config_notes(config, frozenset({"responseModalities", "speechConfig"}))
    )
    notes.extend(voice_notes)
    notes.extend(_request_notes(request, system_used=False))
    return GeminiMediaAsk(body=body, not_forwarded=_unique(notes))


def _form_text(value: Any) -> str | None:
    """A scalar as the form field the OpenAI SDK would send; ``None`` otherwise."""

    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _reference_image(value: Any) -> tuple[InlineMedia | None, list[str]]:
    image = _mapping(value)
    if image is None:
        return None, []
    data = _pick(image, "bytesBase64Encoded", "bytes_base64_encoded")
    if not isinstance(data, str) or not data:
        return None, []
    mime = _pick(image, "mimeType", "mime_type")
    notes = [
        f"instances[0].image.{key}"
        for key in image
        if _camel(key) not in {"bytesBase64Encoded", "mimeType"}
    ]
    return (
        InlineMedia(
            mime_type=mime.strip().lower() if isinstance(mime, str) else "",
            data=data,
        ),
        notes,
    )


def video_ask(request: GeminiGenerateContentRequest) -> GeminiMediaAsk:
    """A Veo ``:predictLongRunning`` body as the OpenAI videos create fields.

    ``instances[0].prompt`` is the prompt, ``instances[0].image`` the
    ``input_reference`` upload; ``parameters`` map by the names Gemini's OpenAI
    layer documents (``seconds`` as the string the OpenAI SDK sends).
    ``lastFrame``, ``referenceImages``, ``personGeneration``, ``sampleCount``
    and the rest have no OpenAI field and are not sent.
    """

    extra = request.model_extra or {}
    instances = extra.get("instances")
    first = (
        _mapping(instances[0]) if isinstance(instances, list) and instances else None
    )
    prompt = None if first is None else first.get("prompt")
    if first is None or not isinstance(prompt, str) or not prompt.strip():
        raise GeminiConversionError(
            "A :predictLongRunning request needs instances[0].prompt, the "
            "video to make.",
            field="instances",
        )
    notes: list[str] = []
    if isinstance(instances, list) and len(instances) > 1:
        notes.append("instances[1:]")
    images: list[InlineMedia] = []
    for key, value in first.items():
        if key == "prompt":
            continue
        if _camel(key) == "image":
            image, image_notes = _reference_image(value)
            if image is not None:
                images.append(image)
                notes.extend(image_notes)
                continue
        notes.append(f"instances[0].{key}")
    body: dict[str, Any] = {"prompt": prompt}
    for key, value in (_mapping(extra.get("parameters")) or {}).items():
        field = _VIDEO_PARAMETERS.get(_camel(key))
        text = _form_text(value)
        if field is None or text is None:
            notes.append(f"parameters.{key}")
            continue
        body[field] = text
    if request.contents is not None:
        notes.append("contents")
    if request.generation_config is not None:
        notes.append("generationConfig")
    notes.extend(
        _request_notes(
            request, system_used=False, read=frozenset({"instances", "parameters"})
        )
    )
    return GeminiMediaAsk(body=body, images=tuple(images), not_forwarded=_unique(notes))


# ------------------------------------------------------------------ answers


def inline_part(mime_type: str, data: str) -> dict[str, Any]:
    """One ``inlineData`` part: base64 ``data`` with its MIME type."""

    return {"inlineData": {"mimeType": mime_type, "data": data}}


def _base64_mime(data: str) -> str | None:
    """The image type from the first bytes of a base64 string (a few bytes only)."""

    head = data[:24]
    head = head[: len(head) - len(head) % 4]
    try:
        raw = base64.b64decode(head, validate=False)
    except binascii.Error, ValueError:
        return None
    return sniff_image_mime(raw)


def _base_type(content_type: str | None) -> str | None:
    if not content_type:
        return None
    return content_type.split(";")[0].strip().lower() or None


def openai_image_parts(body: bytes) -> tuple[dict[str, Any] | str, ...]:
    """An OpenAI Images answer as Gemini parts, in order. Run off the loop.

    A ``b64_json`` item becomes an ``inlineData`` part with the base64 passed
    through untouched; a URL-only item stays its URL, for the caller to fetch.
    """

    try:
        payload = _mapping(json.loads(body))
    except ValueError, UnicodeDecodeError:
        return ()
    if payload is None:
        return ()
    declared = payload.get("output_format")
    fallback = _FORMAT_MIME.get(declared.lower()) if isinstance(declared, str) else None
    items = payload.get("data")
    parts: list[dict[str, Any] | str] = []
    for item in items if isinstance(items, list) else ():
        entry = _mapping(item)
        if entry is None:
            continue
        encoded = entry.get("b64_json")
        if isinstance(encoded, str) and encoded:
            # OpenAI's documented default output is PNG.
            mime = _base64_mime(encoded) or fallback or "image/png"
            parts.append(inline_part(mime, encoded))
            continue
        url = entry.get("url")
        if isinstance(url, str) and url:
            parts.append(url)
    return tuple(parts)


def fetched_image_part(
    data: bytes, content_type: str | None
) -> tuple[dict[str, Any], GeneratedMedia]:
    """A downloaded image as an ``inlineData`` part, and measured. Run off the loop."""

    mime = sniff_image_mime(data) or _base_type(content_type) or "image/png"
    part = inline_part(mime, base64.b64encode(data).decode("ascii"))
    return part, GeneratedMedia(sha256=sha256_hex(data), mime=mime, data=data)


def audio_part(data: bytes, content_type: str | None) -> dict[str, Any]:
    """Synthesized audio as an ``inlineData`` part, typed as the host typed it."""

    mime = (content_type or "").strip() or "application/octet-stream"
    return inline_part(mime, base64.b64encode(data).decode("ascii"))


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def usage_metadata(usage: Mapping[str, Any] | None) -> dict[str, int] | None:
    """OpenAI's ``usage`` as Gemini's ``usageMetadata``; ``None`` when unknown."""

    if not usage:
        return None
    prompt = _count(usage.get("input_tokens"))
    output = _count(usage.get("output_tokens"))
    total = _count(usage.get("total_tokens"))
    if total is None and prompt is not None and output is not None:
        total = prompt + output
    metadata: dict[str, int] = {}
    if prompt is not None:
        metadata["promptTokenCount"] = prompt
    if output is not None:
        metadata["candidatesTokenCount"] = output
    if total is not None:
        metadata["totalTokenCount"] = total
    return metadata or None


def generate_content_media_response(
    parts: Sequence[Mapping[str, Any]],
    *,
    model: str,
    usage: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """One ``GenerateContentResponse`` whose candidate holds the media parts."""

    response: dict[str, Any] = {
        "candidates": [
            {
                "content": {"role": "model", "parts": list(parts)},
                "finishReason": "STOP",
                "index": 0,
            }
        ]
    }
    metadata = usage_metadata(usage)
    if metadata is not None:
        response["usageMetadata"] = metadata
    response["modelVersion"] = model
    return response


def encode_media_answer(payload: Mapping[str, Any], *, stream: bool) -> bytes:
    """The answer's bytes. Run off the loop: it carries megabytes of base64.

    Streamed, it is ONE event in the chat Gemini stream's own framing
    (``format_gemini_sse_data``), sent once the whole answer is in.
    """

    if stream:
        return format_gemini_sse_data(payload).encode("utf-8")
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )


# --------------------------------------------------------------- video jobs


def operation_name(job_id: str) -> str:
    """The Operation ``name`` a Veo client polls: ``operations/<MCC job id>``."""

    return f"operations/{job_id}"


def file_id_for_job(job_id: str) -> str:
    """The file id a finished video's ``uri`` names: the job id, no underscore.

    ``google-genai`` keeps only the first ``[a-z0-9]+`` run after ``files/``
    of an ``https://`` uri (``t_file_name``), so ``video_<hex>`` would reach
    MCC as ``video``; ``video<hex>`` arrives whole.
    """

    return job_id.replace("_", "")


def job_id_for_file(file_ref: str) -> str | None:
    """The MCC job id a ``files/<id>:download`` path names; ``None`` otherwise.

    Read from the LAST ``files/``: over plain ``http://`` the Python SDK does
    not shorten the uri and requests ``files/<the whole uri>:download``.
    """

    tail = file_ref.rsplit("files/", 1)[-1]
    name, _separator, method = tail.partition(":")
    if method != "download":
        return None
    if _MCC_JOB_ID.fullmatch(name):
        return name
    match = _FILE_ID.fullmatch(name)
    return None if match is None else f"video_{match.group(1)}"


def gemini_operation(job: Mapping[str, Any], *, download_uri: str) -> dict[str, Any]:
    """A stored video job as Google's ``Operation``.

    Running: ``done: false`` (``metadata.progress`` when the host reports
    it). Completed: the Veo response naming MCC's own download URL, never the
    host's. Failed: ``done: true`` with the host's message as the ``error``.
    """

    name = operation_name(str(job["job_id"]))
    status = job.get("status")
    if status == STATUS_COMPLETED:
        return {
            "name": name,
            "done": True,
            "response": {
                "@type": PREDICT_LONG_RUNNING_RESPONSE_TYPE,
                "generateVideoResponse": {
                    "generatedSamples": [{"video": {"uri": download_uri}}]
                },
            },
        }
    if status == STATUS_FAILED:
        message = job.get("error")
        return {
            "name": name,
            "done": True,
            "error": {
                "code": FAILED_JOB_CODE,
                "message": (
                    message
                    if isinstance(message, str) and message
                    else "The video job failed."
                ),
            },
        }
    metadata: dict[str, Any] = {}
    progress = _count(job.get("progress"))
    if progress is not None:
        metadata["progress"] = progress
    return {"name": name, "done": False, "metadata": metadata}
