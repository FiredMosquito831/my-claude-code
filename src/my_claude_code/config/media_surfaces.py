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

#: The media operations MCC routes. Only image generation ships in 7.60.0;
#: the rest arrive one release each and are named here so the vocabulary is
#: one list.
MEDIA_OPERATION_IMAGE_GENERATE = "image_generate"
MEDIA_OPERATION_IMAGE_EDIT = "image_edit"
MEDIA_OPERATION_SPEECH = "speech"
MEDIA_OPERATION_TRANSCRIBE = "transcribe"
MEDIA_OPERATION_TRANSLATE = "translate"

#: Wire-shape families: one adapter per family, never per provider.
MEDIA_SHAPE_OPENAI_IMAGES = "openai_images"
MEDIA_SHAPE_OPENAI_SPEECH = "openai_speech"
MEDIA_SHAPE_OPENAI_TRANSCRIPTIONS = "openai_transcriptions"

MEDIA_OPERATIONS: tuple[str, ...] = (
    MEDIA_OPERATION_IMAGE_GENERATE,
    MEDIA_OPERATION_IMAGE_EDIT,
    MEDIA_OPERATION_SPEECH,
    MEDIA_OPERATION_TRANSCRIBE,
    MEDIA_OPERATION_TRANSLATE,
)
MEDIA_SHAPES: tuple[str, ...] = (
    MEDIA_SHAPE_OPENAI_IMAGES,
    MEDIA_SHAPE_OPENAI_SPEECH,
    MEDIA_SHAPE_OPENAI_TRANSCRIPTIONS,
)


@dataclass(frozen=True, slots=True)
class MediaSurface:
    """One media endpoint a provider serves.

    ``path`` is joined onto the provider's configured base URL (so a user who
    points a provider at a gateway moves its media endpoints with it), unless
    it is an absolute ``https://`` URL.

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
