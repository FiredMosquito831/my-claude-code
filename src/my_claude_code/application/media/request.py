"""The canonical media request, the rails it routes on, and one routed attempt.

Every media endpoint parses its wire body into one :class:`MediaRequest`, the
way every chat surface converts to one ``MessagesRequest``. The rail decides
which chain of models may serve it; the operation decides which declared
provider surface (``config/media_surfaces.py``) an attempt goes out through.
"""

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import IO, Any

from my_claude_code.application.routing import ResolvedModel
from my_claude_code.config.media_surfaces import (
    MEDIA_OPERATION_IMAGE_EDIT,
    MEDIA_OPERATION_IMAGE_GENERATE,
    MEDIA_OPERATION_SPEECH,
    MEDIA_OPERATION_TRANSCRIBE,
    MEDIA_OPERATION_TRANSLATE,
    MEDIA_OPERATION_VIDEO_CREATE,
)


class MediaRail(StrEnum):
    """A media rail on Model Config. Deliberately not a ``ModelTier``.

    A tier member would make ``mcc/image`` a valid *chat* model on
    ``/v1/messages`` and list it in every tier picker; a media rail is only
    ever reached through a media endpoint.
    """

    IMAGE = "image"
    TTS = "tts"
    ASR = "asr"
    VIDEO = "video"


@dataclass(frozen=True, slots=True)
class MediaRailSettings:
    """The three settings that make up one rail, by attribute and env name."""

    label: str
    model_attr: str
    model_env: str
    fallbacks_attr: str
    fallbacks_env: str
    paused_attr: str
    paused_env: str


RAIL_SETTINGS: Mapping[MediaRail, MediaRailSettings] = {
    MediaRail.IMAGE: MediaRailSettings(
        label="Image",
        model_attr="model_image",
        model_env="MODEL_IMAGE",
        fallbacks_attr="model_image_fallbacks",
        fallbacks_env="MODEL_IMAGE_FALLBACKS",
        paused_attr="model_image_paused",
        paused_env="MODEL_IMAGE_PAUSED",
    ),
    MediaRail.TTS: MediaRailSettings(
        label="Speech",
        model_attr="model_tts",
        model_env="MODEL_TTS",
        fallbacks_attr="model_tts_fallbacks",
        fallbacks_env="MODEL_TTS_FALLBACKS",
        paused_attr="model_tts_paused",
        paused_env="MODEL_TTS_PAUSED",
    ),
    MediaRail.ASR: MediaRailSettings(
        label="Transcription",
        model_attr="model_asr",
        model_env="MODEL_ASR",
        fallbacks_attr="model_asr_fallbacks",
        fallbacks_env="MODEL_ASR_FALLBACKS",
        paused_attr="model_asr_paused",
        paused_env="MODEL_ASR_PAUSED",
    ),
    MediaRail.VIDEO: MediaRailSettings(
        label="Video",
        model_attr="model_video",
        model_env="MODEL_VIDEO",
        fallbacks_attr="model_video_fallbacks",
        fallbacks_env="MODEL_VIDEO_FALLBACKS",
        paused_attr="model_video_paused",
        paused_env="MODEL_VIDEO_PAUSED",
    ),
}

#: The routed operations each rail carries (7.67.0, for the dashboard). A
#: video's retrieve/content/delete are calls on an accepted job, pinned to the
#: provider that took it, so the rail itself routes only the create.
RAIL_OPERATIONS: Mapping[MediaRail, tuple[str, ...]] = {
    MediaRail.IMAGE: (MEDIA_OPERATION_IMAGE_GENERATE, MEDIA_OPERATION_IMAGE_EDIT),
    MediaRail.TTS: (MEDIA_OPERATION_SPEECH,),
    MediaRail.ASR: (MEDIA_OPERATION_TRANSCRIBE, MEDIA_OPERATION_TRANSLATE),
    MediaRail.VIDEO: (MEDIA_OPERATION_VIDEO_CREATE,),
}

#: The rail a logged ``media_operation`` is counted under.
OPERATION_RAILS: Mapping[str, MediaRail] = {
    operation: rail
    for rail, operations in RAIL_OPERATIONS.items()
    for operation in operations
}


@dataclass(frozen=True, slots=True)
class MediaUpload:
    """One file a client uploaded (an image to edit, a mask, later audio).

    ``file`` is the server's spooled copy -- in memory below the multipart
    library's own threshold, on disk above it -- and is re-read from the
    start for every attempt, off the event loop, never loaded whole.
    ``sha256`` and ``size`` were measured off the loop when it arrived.
    """

    field: str
    filename: str
    content_type: str
    size: int
    sha256: str
    file: IO[bytes] = field(repr=False, compare=False)
    #: The length a WAV upload's header states; ``None`` for anything else.
    audio_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class MediaRequest:
    """One media request, independent of the wire it arrived on.

    ``body`` is the client's own JSON body minus ``model``: an OpenAI-shaped
    upstream receives it field for field, so a parameter MCC has never heard
    of still reaches a host that understands it. ``model`` is what the client
    asked for -- a ``provider/model`` ref pins one candidate, anything else
    (a vendor model name, ``mcc/image``, nothing) means "the rail's chain".
    """

    operation: str
    rail: MediaRail
    model: str
    body: Mapping[str, Any] = field(default_factory=dict)
    stream: bool = False
    #: Uploaded files, in the order the client sent them. Present only when
    #: the client used multipart; ``body`` then holds its text fields.
    uploads: tuple[MediaUpload, ...] = ()
    #: The client sent multipart/form-data, so every text field in ``body``
    #: arrived as a string. Read by a surface that re-encodes a form as JSON
    #: (only a form's digit strings become numbers there).
    multipart: bool = False
    #: What the client asked for that this request does not carry upstream,
    #: because no OpenAI-shaped field means exactly the same thing (a Gemini
    #: body's ``imageConfig.aspectRatio``, ``temperature``, ...). Logged under
    #: ``media.not_forwarded``; never translated by guess.
    not_forwarded: tuple[str, ...] = ()
    #: Download an image answer's URL-only pictures inside the attempt, before
    #: it commits, and hand them on as ``b64_json`` (7.68.0). Set only by the
    #: Gemini IMAGE branch, whose client needs bytes, and only while
    #: ``MEDIA_FALLBACK_ON_UNDOWNLOADABLE`` is on: a picture that cannot be
    #: downloaded then fails the attempt, and the next model is tried.
    inline_urls: bool = False

    @property
    def prompt(self) -> str | None:
        """The request's text: an image ``prompt``, or the ``input`` to speak."""
        for key in ("prompt", "input"):
            value = self.body.get(key)
            if isinstance(value, str):
                return value
        return None


@dataclass(frozen=True, slots=True)
class MediaAttempt:
    """One media request bound to one resolved model."""

    request: MediaRequest
    resolved: ResolvedModel


@dataclass(frozen=True, slots=True)
class MediaPlan:
    """The ordered candidates for one media request, with its pause list.

    ``probe_candidates`` maps a provider id to one *chat* model the operator
    configured on it: the diagnostic probe after a 429 asks that model a
    16-token question on the same key, exactly as the chat executor does,
    because a chat question is the cheapest thing a key can be asked -- a
    probe that generated an image would be billed as one.
    """

    attempts: tuple[MediaAttempt, ...]
    paused_refs: frozenset[str] = frozenset()
    paused_env_var: str = "MODEL_IMAGE_PAUSED"
    probe_candidates: Mapping[str, ResolvedModel] = field(default_factory=dict)

    @property
    def primary(self) -> MediaAttempt:
        return self.attempts[0]

    def resolved_models(self) -> tuple[ResolvedModel, ...]:
        return tuple(attempt.resolved for attempt in self.attempts)

    def model_refs(self) -> tuple[str, ...]:
        return tuple(attempt.resolved.provider_model_ref for attempt in self.attempts)


@dataclass(frozen=True, slots=True)
class MediaResponse:
    """A complete, buffered upstream answer (the non-streaming unit).

    A non-streaming media call yields exactly one of these, so "the first
    chunk arrived" and "the whole answer arrived" are the same moment -- the
    commit point the chat executor has for a non-streaming client.
    """

    status_code: int
    content_type: str
    body: bytes
    #: Upstream response headers worth keeping for the log (never auth).
    headers: Mapping[str, str] = field(default_factory=dict)
    #: The host's token usage, when the translated body cannot carry it (a
    #: Gemini native answer: the audio itself, or a transcript as text).
    usage: Mapping[str, Any] | None = None
    #: The audio length measured while translating (raw PCM states none).
    audio_seconds: float | None = None
    #: Request fields this answer's surface had no exact place for.
    not_forwarded: tuple[str, ...] = ()


#: What a media provider yields: one buffered response, or raw stream frames.
MediaChunk = MediaResponse | bytes


@dataclass(frozen=True, slots=True)
class MediaDownload:
    """A file the host is sending, still open: read ``chunks``, then ``close``.

    ``close`` must be awaited exactly once whatever happened to the body: it
    closes the upstream response and frees the concurrency slot it holds.
    """

    status_code: int
    content_type: str
    chunks: AsyncIterator[bytes] = field(repr=False)
    close: Callable[[], Awaitable[None]] = field(repr=False)
