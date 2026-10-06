"""What kind of model a ref is -- chat, or one media rail's -- from declared data only.

Before 7.78.2 every discovered model was offered as a chat model: an image or
video model on a provider's ``/models`` list reached ``/v1/models``, all
thirteen generated harness catalogues (Aider's even marked it
``"mode": "chat"``) and every chat picker on Model Config, where choosing it
could only produce a failed request. And every media rail's picker offered all
of the chat models, which a media endpoint can never use.

A *kind* is one of :data:`MODEL_KINDS`: ``chat``, or the value of the
:class:`~my_claude_code.application.media.request.MediaRail` it belongs on
(``image``, ``tts``, ``asr``, ``video``). A model may have several -- a Gemini
model that hears audio and writes text is a chat model *and* can transcribe --
or none of them (an embedding model).

**Declared data only, never a name.** Two sources, in this order:

1. What models.dev catalogues the model as accepting and producing, read down
   the same capability ladder every other field walks
   (``declared_modalities_tiered``): the provider's own bucket, then the
   OpenRouter reference, then the quorum-guarded cross-provider vote.
2. Where the operator put it: a ref saved on a media rail and on no chat rail
   is that rail's kind, because the operator said so -- consulted only when
   models.dev is silent, so it can never override a published declaration.

Nothing else. No ``"image" in model_id``, no provider-wide guess: a provider
that serves image generation also serves chat models, so what it declares says
nothing about any one model.

**Unknown is not unsupported.** When no source states a kind,
:attr:`ModelKind.kinds` is ``None`` and the model stays everywhere it was
before: in every chat list, and selectable on every media rail under a "kind
not known" group. Only a *stated* kind removes a model from a list.

**Hide-only.** This module decides what a list offers, never what a request
does. Routing, request bodies and the bytes sent upstream are untouched, and a
ref already saved on any rail keeps being listed where it is saved.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from my_claude_code.application.media.rails import rail_refs
from my_claude_code.application.media.request import RAIL_SETTINGS, MediaRail
from my_claude_code.application.model_metadata import DeclaredModalities
from my_claude_code.config.harness_tiers import HarnessTiers, current_harness_tiers
from my_claude_code.config.model_refs import (
    configured_chat_model_refs,
    parse_model_name,
    parse_provider_type,
)
from my_claude_code.config.settings import Settings
from my_claude_code.core.model_ids import ResolutionTier

#: The kind every chat rail, tier alias and harness catalogue serves.
CHAT_KIND = "chat"

#: Every kind, in display order: chat first, then the media rails in the order
#: Model Config draws them.
MODEL_KINDS: tuple[str, ...] = (CHAT_KIND, *(rail.value for rail in MediaRail))

#: The words the dashboard uses for each kind -- the rail labels themselves, so
#: a kind and the rail it belongs on can never be called two different things.
KIND_LABELS: Mapping[str, str] = {
    CHAT_KIND: "Chat",
    **{rail.value: RAIL_SETTINGS[rail].label for rail in MediaRail},
}

#: Where a stated kind came from.
KIND_SOURCE_MODELS_DEV = "models_dev"
KIND_SOURCE_MEDIA_RAIL = "media_rail"

KIND_SOURCE_LABELS: Mapping[str, str] = {
    KIND_SOURCE_MODELS_DEV: "models.dev modalities",
    KIND_SOURCE_MEDIA_RAIL: "your media rail",
}

#: ``(provider_id, model_id) -> (modalities, rung)``: what the catalogue
#: declares for one model, or ``(None, None)``. Injected because the ladder
#: lives in ``providers``, which ``application`` may not import.
type ModalitiesLookup = Callable[
    [str, str], tuple[DeclaredModalities | None, ResolutionTier | None]
]


@dataclass(frozen=True, slots=True)
class ModelKind:
    """The kinds one ref is stated to be, and who stated them.

    ``kinds is None`` means no source said anything, which is never the same
    as an empty set (a source said it is none of the five).
    """

    kinds: frozenset[str] | None
    source: str | None = None
    tier: ResolutionTier | None = None

    @property
    def known(self) -> bool:
        """Whether any source stated this model's kind."""

        return self.kinds is not None

    def states(self, kind: str) -> bool | None:
        """Whether a source stated ``kind``; ``None`` when nothing was stated."""

        return None if self.kinds is None else kind in self.kinds

    def offered_on(self, kind: str) -> bool:
        """Whether a list of ``kind`` models may offer this one.

        True for a stated match and for an unknown kind -- an unknown model
        stays wherever it was -- and False only for a stated mismatch.
        """

        return self.kinds is None or kind in self.kinds

    @property
    def chat_listable(self) -> bool:
        """Whether a chat list (``/v1/models``, a catalogue, a chat picker) offers it."""

        return self.offered_on(CHAT_KIND)


UNKNOWN_KIND = ModelKind(kinds=None)


def kinds_from_modalities(modalities: DeclaredModalities) -> frozenset[str]:
    """The kinds a declared input/output pair amounts to.

    - chat: reads text and writes text;
    - image / video: writes an image / a video;
    - speech (``tts``): writes audio;
    - transcription (``asr``): hears audio and writes text.

    A row can satisfy several (a model writing ``text`` and ``image`` is a chat
    model that also draws), and a row satisfying none -- an embedding model --
    is a stated "none of these", not an unknown.
    """

    inputs = {item.strip().lower() for item in modalities.inputs}
    outputs = {item.strip().lower() for item in modalities.outputs}
    kinds: set[str] = set()
    if "text" in inputs and "text" in outputs:
        kinds.add(CHAT_KIND)
    if "image" in outputs:
        kinds.add(MediaRail.IMAGE.value)
    if "audio" in outputs:
        kinds.add(MediaRail.TTS.value)
    if "audio" in inputs and "text" in outputs:
        kinds.add(MediaRail.ASR.value)
    if "video" in outputs:
        kinds.add(MediaRail.VIDEO.value)
    return frozenset(kinds)


def chat_placed_refs(settings: Settings, harness_tiers: HarnessTiers) -> frozenset[str]:
    """Every ref saved on a chat rail: global tiers, vision, any agent's tiers."""

    refs = {ref.model_ref for ref in configured_chat_model_refs(settings)}
    for tiers in harness_tiers.harnesses.values():
        for override in tiers.values():
            if override.model:
                refs.add(override.model)
            refs.update(override.fallbacks)
    return frozenset(refs)


def media_rail_placements(
    settings: Settings, harness_tiers: HarnessTiers | None = None
) -> dict[str, frozenset[str]]:
    """Refs saved on a media rail and on no chat rail, with their rails.

    A ref the operator saved on both kinds of rail has told us nothing about
    which it is, so it is left out here and stays unknown -- listed wherever it
    was -- unless models.dev states its kind.
    """

    chat = chat_placed_refs(
        settings,
        harness_tiers if harness_tiers is not None else current_harness_tiers(),
    )
    placed: dict[str, set[str]] = {}
    for rail in MediaRail:
        for ref in rail_refs(settings, rail):
            if ref not in chat:
                placed.setdefault(ref, set()).add(rail.value)
    return {ref: frozenset(rails) for ref, rails in placed.items()}


def resolve_model_kind(
    model_ref: str,
    *,
    modalities: ModalitiesLookup,
    placements: Mapping[str, frozenset[str]],
) -> ModelKind:
    """The stated kind of one ``provider/model`` ref, or :data:`UNKNOWN_KIND`.

    models.dev first; the operator's media-rail placement only where models.dev
    is silent, so a placement can never contradict a published declaration --
    a chat model someone put on the Image rail is still a chat model, and the
    Image rail's picker marks it rather than the chat lists losing it.
    """

    if "/" in model_ref:
        declared, tier = modalities(
            parse_provider_type(model_ref), parse_model_name(model_ref)
        )
        if declared is not None:
            return ModelKind(
                kinds=kinds_from_modalities(declared),
                source=KIND_SOURCE_MODELS_DEV,
                tier=tier,
            )
    placed = placements.get(model_ref)
    if placed:
        return ModelKind(kinds=placed, source=KIND_SOURCE_MEDIA_RAIL)
    return UNKNOWN_KIND


def chat_listing_filter(
    settings: Settings,
    modalities: ModalitiesLookup,
    harness_tiers: HarnessTiers | None = None,
) -> Callable[[str], bool]:
    """The one predicate every chat listing applies to a *discovered* ref.

    ``/v1/models`` and every harness catalogue build their lists from the same
    enumeration (``application/catalogue_model``), and they must keep agreeing
    on which models exist; both take this predicate, built once per listing.
    Configured chat refs never pass through it: a ref saved on a chat rail is
    listed whatever its kind, because hiding it would hide the route the
    operator chose.
    """

    placements = media_rail_placements(settings, harness_tiers)

    def listable(model_ref: str) -> bool:
        return resolve_model_kind(
            model_ref, modalities=modalities, placements=placements
        ).chat_listable

    return listable
