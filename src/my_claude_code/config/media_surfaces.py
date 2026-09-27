"""Which media operations a provider serves, declared rather than guessed.

A model id says nothing about the endpoint that serves it: ``gpt-image-2`` is
an Images-API model and ``gemini-3.1-flash-image`` a generateContent one, and
both are listed with ``output: ["image"]``. So the media router never picks an
adapter from a model name. It reads what the *provider* declares here -- the
same way ``response_surfaces`` declares which chat doors a host has -- and a
candidate whose provider declares nothing for the requested operation is
skipped without being charged a failure.

Leaf module: ``config`` imports nothing first-party.
"""

from dataclasses import dataclass
from urllib.parse import quote

#: The media operations MCC routes. Only image generation ships in 7.60.0;
#: the rest arrive one release each and are named here so the vocabulary is
#: one list.
MEDIA_OPERATION_IMAGE_GENERATE = "image_generate"
MEDIA_OPERATION_IMAGE_EDIT = "image_edit"
MEDIA_OPERATION_SPEECH = "speech"
MEDIA_OPERATION_TRANSCRIBE = "transcribe"
MEDIA_OPERATION_TRANSLATE = "translate"
#: 7.64.0: a video is a job. ``video_create`` is the only routed operation
#: (the rail's chain decides who accepts it); the other three are calls on the
#: accepted job, pinned to the provider and key that accepted it.
MEDIA_OPERATION_VIDEO_CREATE = "video_create"
MEDIA_OPERATION_VIDEO_RETRIEVE = "video_retrieve"
MEDIA_OPERATION_VIDEO_CONTENT = "video_content"
MEDIA_OPERATION_VIDEO_DELETE = "video_delete"

#: Wire-shape families: one adapter per family, never per provider.
MEDIA_SHAPE_OPENAI_IMAGES = "openai_images"
MEDIA_SHAPE_OPENAI_SPEECH = "openai_speech"
MEDIA_SHAPE_OPENAI_TRANSCRIPTIONS = "openai_transcriptions"
MEDIA_SHAPE_OPENAI_VIDEOS = "openai_videos"
#: 7.66.0: Gemini's native ``generateContent`` serving speech (``AUDIO`` out)
#: and transcription (audio in, text out). Gemini's OpenAI-compatible layer
#: has neither ``audio/speech`` nor ``audio/transcriptions``.
MEDIA_SHAPE_GEMINI_TTS = "gemini_tts"
MEDIA_SHAPE_GEMINI_TRANSCRIBE = "gemini_transcribe"

#: A surface whose host documents a JSON body only (``MediaSurface.encoding``).
MEDIA_ENCODING_JSON = "json"

MEDIA_OPERATIONS: tuple[str, ...] = (
    MEDIA_OPERATION_IMAGE_GENERATE,
    MEDIA_OPERATION_IMAGE_EDIT,
    MEDIA_OPERATION_SPEECH,
    MEDIA_OPERATION_TRANSCRIBE,
    MEDIA_OPERATION_TRANSLATE,
    MEDIA_OPERATION_VIDEO_CREATE,
    MEDIA_OPERATION_VIDEO_RETRIEVE,
    MEDIA_OPERATION_VIDEO_CONTENT,
    MEDIA_OPERATION_VIDEO_DELETE,
)
MEDIA_SHAPES: tuple[str, ...] = (
    MEDIA_SHAPE_OPENAI_IMAGES,
    MEDIA_SHAPE_OPENAI_SPEECH,
    MEDIA_SHAPE_OPENAI_TRANSCRIPTIONS,
    MEDIA_SHAPE_OPENAI_VIDEOS,
    MEDIA_SHAPE_GEMINI_TTS,
    MEDIA_SHAPE_GEMINI_TRANSCRIBE,
)

#: The header Gemini's native API reads the key from (instead of Bearer).
GEMINI_API_KEY_HEADER = "x-goog-api-key"

#: Gemini's native ``generateContent``. Declared absolute: MCC has no Gemini
#: base-URL setting, and the catalogue's base is the OpenAI-compatible layer
#: (``.../v1beta/openai/``), not the native API.
GEMINI_GENERATE_CONTENT_PATH = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)

#: Gemini's prebuilt TTS voices, exactly as the "Prebuilt voices" table of
#: https://ai.google.dev/gemini-api/docs/speech-generation lists them (read
#: 2026-09-27: 30 voices). A client's ``voice`` outside this list is not
#: forwarded -- the host's default voice speaks -- and is logged under
#: ``media.not_forwarded``. The same page's Extended Voice Library and custom
#: ``voice_...`` ids are documented for the Interactions API, not for
#: ``generateContent``'s ``prebuiltVoiceConfig``, so they are not listed.
GEMINI_TTS_VOICES: tuple[str, ...] = (
    "Zephyr",
    "Puck",
    "Charon",
    "Kore",
    "Fenrir",
    "Leda",
    "Orus",
    "Aoede",
    "Callirrhoe",
    "Autonoe",
    "Enceladus",
    "Iapetus",
    "Umbriel",
    "Algieba",
    "Despina",
    "Erinome",
    "Algenib",
    "Rasalgethi",
    "Laomedeia",
    "Achernar",
    "Alnilam",
    "Schedar",
    "Gacrux",
    "Pulcherrima",
    "Achird",
    "Zubenelgenubi",
    "Vindemiatrix",
    "Sadachbia",
    "Sadaltager",
    "Sulafat",
)


@dataclass(frozen=True, slots=True)
class MediaSurface:
    """One media endpoint a provider serves.

    ``path`` is joined onto the provider's configured base URL (so a user who
    points a provider at a gateway moves its media endpoints with it), unless
    it is an absolute ``https://`` URL. ``{model}`` in it is the URL-quoted
    provider model id (``model_path``), for a host that names the model in
    the URL rather than in the body (Gemini's native API).

    ``stream`` is whether the endpoint is documented to stream partial results
    (OpenAI's ``image_generation.partial_image`` events). A client that asks
    for a stream is only routed to a surface that declares one; an
    undocumented stream is never assumed.
    """

    operation: str
    shape: str
    path: str
    stream: bool = False
    #: The output formats the host documents (``response_format``), or
    #: ``None`` when its reference does not list them. A client that NAMES
    #: a format outside a declared list skips this surface uncharged (user
    #: decision 9: no transcoding); with no list the host judges the name.
    formats: tuple[str, ...] | None = None
    #: ``"json"`` when the host documents a JSON body only: a client's
    #: multipart form is re-encoded as JSON, and a request carrying a file
    #: skips this surface uncharged (a file cannot go where only JSON is
    #: documented). ``None`` forwards the client's own encoding.
    encoding: str | None = None
    #: Client field -> host field, where the host names a parameter
    #: differently (OpenRouter's ``duration`` for OpenAI's ``seconds``).
    renames: tuple[tuple[str, str], ...] = ()
    #: The client query parameters forwarded on this surface; any other is
    #: dropped (DeepInfra's video content takes ``variant``).
    query: tuple[str, ...] = ()
    #: The header the key goes in, sent as the bare key; ``None`` is
    #: ``Authorization: Bearer <key>`` (Gemini's native API reads
    #: ``x-goog-api-key``).
    auth_header: str | None = None
    #: The voice names the host documents; a client's ``voice`` outside the
    #: list is not forwarded (and is logged as such). ``None``: the host
    #: judges the name.
    voices: tuple[str, ...] | None = None


def image_generation_surface(
    path: str = "images/generations", *, stream: bool = False
) -> MediaSurface:
    """The OpenAI ``POST /images/generations`` shape at ``path``."""

    return MediaSurface(
        operation=MEDIA_OPERATION_IMAGE_GENERATE,
        shape=MEDIA_SHAPE_OPENAI_IMAGES,
        path=path,
        stream=stream,
    )


def image_edit_surface(
    path: str = "images/edits", *, stream: bool = False
) -> MediaSurface:
    """The OpenAI ``POST /images/edits`` shape at ``path``.

    The client's own encoding is forwarded: a multipart upload goes out as
    multipart, streamed from the spooled files, and the JSON variant
    (``images: [{image_url}]``) goes out as JSON.
    """

    return MediaSurface(
        operation=MEDIA_OPERATION_IMAGE_EDIT,
        shape=MEDIA_SHAPE_OPENAI_IMAGES,
        path=path,
        stream=stream,
    )


def speech_surface(
    path: str = "audio/speech", *, formats: tuple[str, ...] | None = None
) -> MediaSurface:
    """The OpenAI ``POST /audio/speech`` shape: the answer is the audio itself."""

    return MediaSurface(
        operation=MEDIA_OPERATION_SPEECH,
        shape=MEDIA_SHAPE_OPENAI_SPEECH,
        path=path,
        formats=formats,
    )


def transcription_surface(
    path: str = "audio/transcriptions", *, stream: bool = False
) -> MediaSurface:
    """The OpenAI ``POST /audio/transcriptions`` shape (multipart upload)."""

    return MediaSurface(
        operation=MEDIA_OPERATION_TRANSCRIBE,
        shape=MEDIA_SHAPE_OPENAI_TRANSCRIPTIONS,
        path=path,
        stream=stream,
    )


def translation_surface(path: str = "audio/translations") -> MediaSurface:
    """The OpenAI ``POST /audio/translations`` shape: speech in, English text out."""

    return MediaSurface(
        operation=MEDIA_OPERATION_TRANSLATE,
        shape=MEDIA_SHAPE_OPENAI_TRANSCRIPTIONS,
        path=path,
    )


def gemini_speech_surface() -> MediaSurface:
    """Gemini's native TTS: ``generateContent`` with ``AUDIO`` out.

    The host answers raw 16-bit PCM or WAV; a client that names ``wav`` or
    ``pcm`` gets that framing (a header added or removed, never a transcode),
    and any other named format skips this surface uncharged.
    """

    return MediaSurface(
        operation=MEDIA_OPERATION_SPEECH,
        shape=MEDIA_SHAPE_GEMINI_TTS,
        path=GEMINI_GENERATE_CONTENT_PATH,
        formats=("wav", "pcm"),
        auth_header=GEMINI_API_KEY_HEADER,
        voices=GEMINI_TTS_VOICES,
    )


def gemini_transcription_surface() -> MediaSurface:
    """Gemini's native ASR: the audio inline in ``generateContent``, text out."""

    return MediaSurface(
        operation=MEDIA_OPERATION_TRANSCRIBE,
        shape=MEDIA_SHAPE_GEMINI_TRANSCRIBE,
        path=GEMINI_GENERATE_CONTENT_PATH,
        formats=("json", "text"),
        auth_header=GEMINI_API_KEY_HEADER,
    )


def video_surfaces(
    *,
    create: str = "videos",
    retrieve: str = "videos/{id}",
    content: str | None = None,
    delete: str | None = None,
    encoding: str | None = None,
    renames: tuple[tuple[str, str], ...] = (),
    content_query: tuple[str, ...] = (),
) -> tuple[MediaSurface, ...]:
    """The OpenAI ``/videos`` job shape: create, then calls on the job.

    ``{id}`` in a path is the upstream job id (``job_path``). A host with no
    ``content`` path serves the finished video at a URL in its retrieve
    answer; a host with no ``delete`` path documents no delete.
    """

    surfaces = [
        MediaSurface(
            operation=MEDIA_OPERATION_VIDEO_CREATE,
            shape=MEDIA_SHAPE_OPENAI_VIDEOS,
            path=create,
            encoding=encoding,
            renames=renames,
        ),
        MediaSurface(
            operation=MEDIA_OPERATION_VIDEO_RETRIEVE,
            shape=MEDIA_SHAPE_OPENAI_VIDEOS,
            path=retrieve,
        ),
    ]
    if content is not None:
        surfaces.append(
            MediaSurface(
                operation=MEDIA_OPERATION_VIDEO_CONTENT,
                shape=MEDIA_SHAPE_OPENAI_VIDEOS,
                path=content,
                query=content_query,
            )
        )
    if delete is not None:
        surfaces.append(
            MediaSurface(
                operation=MEDIA_OPERATION_VIDEO_DELETE,
                shape=MEDIA_SHAPE_OPENAI_VIDEOS,
                path=delete,
            )
        )
    return tuple(surfaces)


#: 7.67.0: the media endpoints a custom (hand-configured) provider may say it
#: serves, in the card's order. Each is OpenAI's own endpoint at its default
#: path, joined onto the provider's base URL; ``video`` is the job shape of
#: ``video_surfaces()`` (create + retrieve; the finished file is read from the
#: URL the host's retrieve answer carries). Declared by the operator, like the
#: chat ``surfaces`` beside it -- only they know what their gateway fronts.
CUSTOM_MEDIA_VIDEO = "video"
CUSTOM_MEDIA_OPERATIONS: tuple[str, ...] = (
    MEDIA_OPERATION_IMAGE_GENERATE,
    MEDIA_OPERATION_IMAGE_EDIT,
    MEDIA_OPERATION_SPEECH,
    MEDIA_OPERATION_TRANSCRIBE,
    MEDIA_OPERATION_TRANSLATE,
    CUSTOM_MEDIA_VIDEO,
)


def custom_media_surfaces(operations: tuple[str, ...]) -> tuple[MediaSurface, ...]:
    """The surfaces a custom provider's declared operations stand for.

    Canonical order whatever order they were declared in; a name outside
    ``CUSTOM_MEDIA_OPERATIONS`` stands for nothing (the registry refuses one
    an operator submits). Nothing declared is ``()``: the custom provider
    every earlier release built, skipped uncharged by every media rail.
    """

    surfaces: list[MediaSurface] = []
    for operation in CUSTOM_MEDIA_OPERATIONS:
        if operation not in operations:
            continue
        if operation == MEDIA_OPERATION_IMAGE_GENERATE:
            surfaces.append(image_generation_surface())
        elif operation == MEDIA_OPERATION_IMAGE_EDIT:
            surfaces.append(image_edit_surface())
        elif operation == MEDIA_OPERATION_SPEECH:
            surfaces.append(speech_surface())
        elif operation == MEDIA_OPERATION_TRANSCRIBE:
            surfaces.append(transcription_surface())
        elif operation == MEDIA_OPERATION_TRANSLATE:
            surfaces.append(translation_surface())
        else:
            surfaces.extend(video_surfaces())
    return tuple(surfaces)


def job_path(path: str, job_id: str) -> str:
    """``path`` with ``{id}`` replaced by the URL-quoted upstream job id."""

    return path.replace("{id}", quote(job_id, safe=""))


def model_path(path: str, model: str) -> str:
    """``path`` with ``{model}`` replaced by the URL-quoted provider model id."""

    return path.replace("{model}", quote(model, safe=""))


def surface_for(
    surfaces: tuple[MediaSurface, ...], operation: str
) -> MediaSurface | None:
    """The first declared surface serving ``operation``, or ``None``."""

    for surface in surfaces:
        if surface.operation == operation:
            return surface
    return None


def media_url(base_url: str, path: str) -> str:
    """Join a surface path onto a provider's base URL."""

    if path.startswith(("https://", "http://")):
        return path
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"
