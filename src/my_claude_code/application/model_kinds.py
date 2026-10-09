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

**Declared data only, never a name.** The kind walks the same ladder every
other capability field walks -- the provider's own ``/models`` row first,
then the catalogues, gap filling down -- in this order (7.80.0):

1. What the provider's own model list states the model accepts and produces
   (``ProviderModelInfo.declared.modalities``, kept since 7.79.0), found at
   tier 1 (exact id) or tier 2 (pricing tag stripped). The provider's own
   record outranks every catalogue, as it does for every field.
2. What models.dev catalogues the model as accepting and producing, read down
   its own rungs (``declared_modalities_tiered``): the provider's own bucket,
   then the OpenRouter reference, then the quorum-guarded cross-provider vote.
   **OpenRouter's own live model list** (7.84.0) sits here too, for every
   provider but OpenRouter itself (whose list IS rung 1): for a provider with
   no models.dev bucket it answers before models.dev's OpenRouter copy and
   the vote; for a provider with a bucket only where the bucket is silent --
   it never overrides a bucket (user decisions 2026-10-08 21:00 and 21:33).
3. The provider's coarser words for it: its model type (Novita
   ``model_type``, Vercel ``type``) and the endpoints it says serve the model
   (Command Code ``supported_endpoints``, new-api ``supported_endpoint_types``,
   Novita ``endpoints``), mapped through :data:`KIND_WORDS`. Only where both
   modality rungs are silent: "served on ``/chat/completions``" says less
   than "hears audio, writes text", so a coarse word never outranks a finer
   statement (it would have taken Transcription away from thirteen models
   that models.dev states can transcribe). A word the table does not know --
   Anthropic's ``type: "model"`` -- states nothing and the ladder goes on.
4. Where the operator put it: a ref saved on a media rail and on no chat rail
   is that rail's kind, because the operator said so -- consulted only when
   every published source is silent, so it can never override a declaration.

Nothing else. No ``"image" in model_id``, no provider-wide guess: a provider
that serves image generation also serves chat models, so what it declares says
nothing about any one model. Each answer carries the rung that stated it
(:attr:`ModelKind.source`, :attr:`ModelKind.tier`), which the Models page
shows beside the kind.

**Unknown is not unsupported.** When no source states a kind,
:attr:`ModelKind.kinds` is ``None`` and the model stays everywhere it was
before: in every chat list, and selectable on every media rail under a "kind
not known" group. Only a *stated* kind removes a model from a list.

**Hide-only.** This module decides what a list offers, never what a request
does. Routing, request bodies and the bytes sent upstream are untouched, and a
ref already saved on any rail keeps being listed where it is saved.
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from my_claude_code.application.media.rails import rail_refs
from my_claude_code.application.media.request import RAIL_SETTINGS, MediaRail
from my_claude_code.application.model_metadata import (
    DeclaredModalities,
    ProviderModelDeclaration,
)
from my_claude_code.application.openrouter_live import (
    LiveLookup,
    LiveModel,
    live_wins_intrinsic,
)
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

#: Where a stated kind came from, in ladder order. The strings are part of the
#: admin API (``kind.source``); the two provider rungs are 7.80.0's.
KIND_SOURCE_PROVIDER_LISTING = "provider_listing"
KIND_SOURCE_MODELS_DEV = "models_dev"
KIND_SOURCE_PROVIDER_WORDS = "provider_words"
KIND_SOURCE_MEDIA_RAIL = "media_rail"
#: 7.84.0: OpenRouter's own live model list, for a provider that is not
#: OpenRouter.
KIND_SOURCE_OPENROUTER_LIVE = "openrouter_live"

KIND_SOURCE_LABELS: Mapping[str, str] = {
    KIND_SOURCE_MODELS_DEV: "models.dev modalities",
    KIND_SOURCE_MEDIA_RAIL: "your media rail",
    KIND_SOURCE_PROVIDER_LISTING: "the provider's model list",
    KIND_SOURCE_PROVIDER_WORDS: "the provider's model type or endpoints",
    KIND_SOURCE_OPENROUTER_LIVE: "OpenRouter's live model list",
}

#: ``(provider_id, model_id) -> (modalities, rung)``: what the ladder declares
#: one model accepts and produces, or ``(None, None)``. Rungs 1-2 are the
#: provider's own list, 3-10 models.dev. Injected because the ladder lives in
#: ``providers``, which ``application`` may not import.
type ModalitiesLookup = Callable[
    [str, str], tuple[DeclaredModalities | None, ResolutionTier | None]
]

#: ``(provider_id, model_id) -> (words, rung)``: the provider's own words for
#: one model -- its model type, then the endpoints it says serve it -- exactly
#: as its list published them, or ``(None, None)``. Rung 1 or 2 only: no
#: catalogue publishes these, so there is nothing further down to ask.
type KindWordsLookup = Callable[
    [str, str], tuple[tuple[str, ...] | None, ResolutionTier | None]
]

#: ``(provider_id, model_id) -> (declaration, rung)``: the provider's own
#: ``/models`` record for one model, found at tier 1 or 2, and what it
#: declares (``None`` when the row stated nothing); ``None`` when the provider's
#: list has no record of the model at all.
type DeclarationLookup = Callable[
    [str, str], tuple[ProviderModelDeclaration | None, ResolutionTier] | None
]

#: The provider's own words for a kind, as the coarse rung reads them (7.80.0).
#:
#: Declared data, not a branch per provider: every key is a word some list
#: publishes as a model type or an endpoint (Novita, Vercel, Command Code,
#: new-api gateways, LiteLLM proxies, OpenAI's own paths), compared after
#: :func:`kind_word_key` has dropped a leading ``/`` and ``v1/``. A word that
#: names no kind -- an embedding or rerank model's -- maps to the empty set,
#: which is a stated "none of these", exactly as an embedding model's
#: modalities already are. A word that is not here at all (``batch-api``,
#: ``realtime``, ``evaluation``, Anthropic's ``model``, ``ocr``, ``search``)
#: states nothing.
KIND_WORDS: Mapping[str, frozenset[str]] = {
    **dict.fromkeys(
        (
            "chat",
            "language",
            "text-generation",
            "chat/completions",
            "completions",
            "responses",
            "anthropic",
            "messages",
            "openai",
            "openai-response",
            "gemini",
        ),
        frozenset({CHAT_KIND}),
    ),
    **dict.fromkeys(
        (
            "image",
            "image-generation",
            "image_generation",
            "images/generations",
            "image_edit",
            "images/edits",
        ),
        frozenset({MediaRail.IMAGE.value}),
    ),
    **dict.fromkeys(
        (
            "video",
            "openai-video",
            "video_generation",
            "video-generation",
            "videos",
        ),
        frozenset({MediaRail.VIDEO.value}),
    ),
    **dict.fromkeys(
        ("speech", "audio_speech", "audio/speech"),
        frozenset({MediaRail.TTS.value}),
    ),
    **dict.fromkeys(
        (
            "transcription",
            "audio_transcription",
            "audio/transcriptions",
            "audio/translations",
        ),
        frozenset({MediaRail.ASR.value}),
    ),
    **dict.fromkeys(
        (
            "embedding",
            "embeddings",
            "rerank",
            "reranking",
            "jina-rerank",
            "moderation",
        ),
        frozenset(),
    ),
}

#: Words a modality list may use for what models.dev calls ``audio`` and
#: ``text``: OpenRouter-dialect lists write a speech model's output as
#: ``speech`` and a transcriber's as ``transcription``. models.dev's own
#: vocabulary is only ``text image audio video pdf``, so no models.dev answer
#: can change by this.
MODALITY_SYNONYMS: Mapping[str, str] = {
    "speech": "audio",
    "transcription": "text",
}


@dataclass(frozen=True, slots=True)
class ModelKind:
    """The kinds one ref is stated to be, and who stated them.

    ``kinds is None`` means no source said anything, which is never the same
    as an empty set (a source said it is none of the five).
    """

    kinds: frozenset[str] | None
    source: str | None = None
    tier: ResolutionTier | None = None
    #: How OpenRouter's live list met the id (``"OpenRouter live, exact id"``)
    #: when that rung stated the kind (7.84.0); it has no ladder tier.
    match: str | None = None

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
    is a stated "none of these", not an unknown. A provider list's
    ``speech``/``transcription`` read as ``audio``/``text``
    (:data:`MODALITY_SYNONYMS`).
    """

    inputs = _modality_words(modalities.inputs)
    outputs = _modality_words(modalities.outputs)
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


def _modality_words(items: Iterable[str]) -> set[str]:
    words = {item.strip().lower() for item in items}
    return {MODALITY_SYNONYMS.get(word, word) for word in words}


def kind_word_key(word: str) -> str:
    """One published word as :data:`KIND_WORDS` spells it.

    Lower-cased, and an endpoint path loses its leading ``/`` and a ``v1/``
    version segment, so ``/v1/chat/completions``, ``/chat/completions`` and
    ``chat/completions`` are one word. Nothing else is rewritten.
    """

    key = word.strip().lower().lstrip("/")
    return key.removeprefix("v1/")


def kinds_from_words(words: Iterable[str]) -> frozenset[str] | None:
    """The kinds a provider's own type and endpoint words amount to.

    The union over every recognised word -- a model listed as ``chat`` and
    served on ``/images/generations`` is both -- and ``None`` when no word is
    recognised, because a list that said only things this table cannot read
    has not stated a kind. Recognised words that name no kind (``embedding``)
    give the empty set: a stated "none of these".
    """

    stated: set[str] | None = None
    for word in words:
        kinds = KIND_WORDS.get(kind_word_key(word))
        if kinds is None:
            continue
        stated = (stated or set()) | kinds
    return None if stated is None else frozenset(stated)


def declaration_words(declaration: ProviderModelDeclaration | None) -> tuple[str, ...]:
    """A declaration's model type, then its endpoints, as published."""

    if declaration is None:
        return ()
    words: list[str] = []
    if declaration.model_type is not None:
        words.append(declaration.model_type)
    words.extend(declaration.endpoints or ())
    return tuple(words)


def provider_first_modalities(
    declared_at: DeclarationLookup, catalogue: ModalitiesLookup
) -> ModalitiesLookup:
    """The modalities ladder with the provider's own list as its first rung.

    ``declared_at`` finds the provider's own record (tier 1 or 2) and
    ``catalogue`` is models.dev's lookup (tiers 3-10), consulted only where
    the record states no modality pair. A provider pair is both halves from
    one row (the reader never keeps half of one), so a provider half is never
    mixed with a catalogue half.
    """

    def lookup(
        provider_id: str, model_id: str
    ) -> tuple[DeclaredModalities | None, ResolutionTier | None]:
        found = declared_at(provider_id, model_id)
        if found is not None:
            declaration, tier = found
            if declaration is not None and declaration.modalities is not None:
                return declaration.modalities, tier
        return catalogue(provider_id, model_id)

    return lookup


def provider_kind_words(declared_at: DeclarationLookup) -> KindWordsLookup:
    """The provider's own type and endpoint words, at the rung they were found."""

    def lookup(
        provider_id: str, model_id: str
    ) -> tuple[tuple[str, ...] | None, ResolutionTier | None]:
        found = declared_at(provider_id, model_id)
        if found is None:
            return None, None
        declaration, tier = found
        words = declaration_words(declaration)
        return (words, tier) if words else (None, None)

    return lookup


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
    was -- unless a published source (its provider's list, models.dev) states
    its kind.
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


def modalities_source(tier: ResolutionTier | None) -> str:
    """Which rung a modality pair came from: the provider's list (tier 1-2) or models.dev."""

    if tier is not None and tier <= ResolutionTier.PROVIDER_TAG_STRIPPED:
        return KIND_SOURCE_PROVIDER_LISTING
    return KIND_SOURCE_MODELS_DEV


#: Every modality word a kind can be read from (7.84.0): models.dev's own
#: (``text image audio video pdf``), the OpenRouter-dialect synonyms
#: (:data:`MODALITY_SYNONYMS`), ``file`` (OpenRouter's ``pdf``), and the
#: outputs that state "none of the five" (``embeddings``, ``rerank``).
#: OpenRouter's live list also writes words no kind rule reads -- ``decisions``
#: on 15 rows -- and a pair carrying one states no kind on that rung: an
#: unread word must not take a model out of a list (unknown is not
#: unsupported), so such a model keeps whatever the rest of the ladder says.
LIVE_KIND_MODALITY_WORDS = frozenset(
    {
        "text",
        "image",
        "audio",
        "video",
        "pdf",
        "file",
        "speech",
        "transcription",
        "embeddings",
        "embedding",
        "rerank",
    }
)


def _live_pair(live: LiveModel | None) -> DeclaredModalities | None:
    """The pair OpenRouter's live list states, if it may decide a kind."""

    if live is None or not live.feeds_ladder or live.modalities is None:
        return None
    pair = live.modalities
    words = {word.strip().lower() for word in (*pair.inputs, *pair.outputs)}
    if not words <= LIVE_KIND_MODALITY_WORDS:
        return None
    return pair


def live_kind_alternative(kind: ModelKind, live: LiveModel | None) -> ModelKind | None:
    """OpenRouter's live statement where it differs from the kind shown (7.84.0).

    For the Models page only, which shows both statements wherever the live
    list says something other than the rung that decided the kind -- the
    provider's own list or a models.dev bucket, which it never overrides.
    """

    pair = _live_pair(live)
    if pair is None or live is None or kind.source == KIND_SOURCE_OPENROUTER_LIVE:
        return None
    stated = kinds_from_modalities(pair)
    if stated == kind.kinds:
        return None
    return ModelKind(
        kinds=stated, source=KIND_SOURCE_OPENROUTER_LIVE, match=live.tier_label
    )


def resolve_model_kind(
    model_ref: str,
    *,
    modalities: ModalitiesLookup,
    placements: Mapping[str, frozenset[str]],
    kind_words: KindWordsLookup | None = None,
    live: LiveLookup | None = None,
) -> ModelKind:
    """The stated kind of one ``provider/model`` ref, or :data:`UNKNOWN_KIND`.

    The modality pair first, down its own ladder (the provider's list, then
    models.dev); then the provider's type and endpoint words, where both are
    silent; the operator's media-rail placement only where every published
    source is silent, so a placement can never contradict a declaration -- a
    chat model someone put on the Image rail is still a chat model, and the
    Image rail's picker marks it rather than the chat lists losing it.

    ``live`` is OpenRouter's live list (7.84.0): below the provider's own
    pair, above models.dev's tiers 5-10 (which only a provider with no bucket
    reaches) and below a bucket's 3-4, and above the coarse words. ``None``
    is the ladder before 7.84.0, exactly.
    """

    if "/" in model_ref:
        provider_id = parse_provider_type(model_ref)
        model_id = parse_model_name(model_ref)
        declared, tier = modalities(provider_id, model_id)
        if declared is None or modalities_source(tier) != KIND_SOURCE_PROVIDER_LISTING:
            answer = None if live is None else live(provider_id, model_id)
            pair = _live_pair(answer)
            if (
                answer is not None
                and pair is not None
                and live_wins_intrinsic(declared, tier, pair)
            ):
                return ModelKind(
                    kinds=kinds_from_modalities(pair),
                    source=KIND_SOURCE_OPENROUTER_LIVE,
                    match=answer.tier_label,
                )
        if declared is not None:
            return ModelKind(
                kinds=kinds_from_modalities(declared),
                source=modalities_source(tier),
                tier=tier,
            )
        if kind_words is not None:
            words, words_tier = kind_words(provider_id, model_id)
            stated = None if words is None else kinds_from_words(words)
            if stated is not None:
                return ModelKind(
                    kinds=stated, source=KIND_SOURCE_PROVIDER_WORDS, tier=words_tier
                )
    placed = placements.get(model_ref)
    if placed:
        return ModelKind(kinds=placed, source=KIND_SOURCE_MEDIA_RAIL)
    return UNKNOWN_KIND


def chat_listing_filter(
    settings: Settings,
    modalities: ModalitiesLookup,
    harness_tiers: HarnessTiers | None = None,
    kind_words: KindWordsLookup | None = None,
    live: LiveLookup | None = None,
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
            model_ref,
            modalities=modalities,
            placements=placements,
            kind_words=kind_words,
            live=live,
        ).chat_listable

    return listable
