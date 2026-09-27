"""The canonical media request, the rails it routes on, and one routed attempt.

Every media endpoint parses its wire body into one :class:`MediaRequest`, the
way every chat surface converts to one ``MessagesRequest``. The rail decides
which chain of models may serve it; the operation decides which declared
provider surface (``config/media_surfaces.py``) an attempt goes out through.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from my_claude_code.application.routing import ResolvedModel


class MediaRail(StrEnum):
    """A media rail on Model Config. Deliberately not a ``ModelTier``.

    A tier member would make ``mcc/image`` a valid *chat* model on
    ``/v1/messages`` and list it in every tier picker; a media rail is only
    ever reached through a media endpoint.
    """

    IMAGE = "image"


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
}


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

    @property
    def prompt(self) -> str | None:
        value = self.body.get("prompt")
        return value if isinstance(value, str) else None


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


#: What a media provider yields: one buffered response, or raw stream frames.
MediaChunk = MediaResponse | bytes
