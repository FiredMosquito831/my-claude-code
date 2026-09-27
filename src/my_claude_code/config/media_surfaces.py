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


def job_path(path: str, job_id: str) -> str:
    """``path`` with ``{id}`` replaced by the URL-quoted upstream job id."""

    return path.replace("{id}", quote(job_id, safe=""))


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
