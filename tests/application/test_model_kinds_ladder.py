"""The kind ladder reads the provider's own list first (7.80.0, PR-K2).

Rung order: the provider's modality pair (tier 1-2) -> models.dev's pair
(tiers 3-10) -> the provider's type and endpoint words (coarse, only where
both pairs are silent) -> the media rail the operator saved it on -> unknown.
Every key and ref here is fake.
"""

from collections.abc import Mapping

from my_claude_code.application.model_kinds import (
    CHAT_KIND,
    KIND_SOURCE_LABELS,
    KIND_SOURCE_MEDIA_RAIL,
    KIND_SOURCE_MODELS_DEV,
    KIND_SOURCE_PROVIDER_LISTING,
    KIND_SOURCE_PROVIDER_WORDS,
    KIND_WORDS,
    MODEL_KINDS,
    UNKNOWN_KIND,
    chat_listing_filter,
    declaration_words,
    kind_word_key,
    kinds_from_modalities,
    kinds_from_words,
    modalities_source,
    provider_first_modalities,
    provider_kind_words,
    resolve_model_kind,
)
from my_claude_code.application.model_metadata import (
    DeclaredModalities,
    ProviderModelDeclaration,
)
from my_claude_code.config.harness_tiers import EMPTY_HARNESS_TIERS
from my_claude_code.config.settings import Settings
from my_claude_code.core.model_ids import ResolutionTier

TEXT = DeclaredModalities(inputs=("text",), outputs=("text",))
HEARS = DeclaredModalities(inputs=("audio", "text"), outputs=("text",))
DRAWS = DeclaredModalities(inputs=("text",), outputs=("image",))


def _catalogue(table: dict[str, DeclaredModalities], tier: ResolutionTier):
    def lookup(
        provider_id: str, model_id: str
    ) -> tuple[DeclaredModalities | None, ResolutionTier | None]:
        found = table.get(f"{provider_id}/{model_id}")
        return (None, None) if found is None else (found, tier)

    return lookup


def _records(
    table: Mapping[str, ProviderModelDeclaration | None],
    tier: ResolutionTier = ResolutionTier.PROVIDER_EXACT,
):
    def declared_at(
        provider_id: str, model_id: str
    ) -> tuple[ProviderModelDeclaration | None, ResolutionTier] | None:
        ref = f"{provider_id}/{model_id}"
        if ref not in table:
            return None
        return table[ref], tier

    return declared_at


def _resolve(
    ref: str,
    *,
    records: Mapping[str, ProviderModelDeclaration | None] | None = None,
    catalogue: dict[str, DeclaredModalities] | None = None,
    placements: dict[str, frozenset[str]] | None = None,
    record_tier: ResolutionTier = ResolutionTier.PROVIDER_EXACT,
    catalogue_tier: ResolutionTier = ResolutionTier.MODELS_DEV_BUCKET_EXACT,
):
    declared_at = _records(records or {}, record_tier)
    return resolve_model_kind(
        ref,
        modalities=provider_first_modalities(
            declared_at, _catalogue(catalogue or {}, catalogue_tier)
        ),
        placements=placements or {},
        kind_words=provider_kind_words(declared_at),
    )


# ------------------------------------------------------------- the word table


def test_every_word_of_the_spec_table_maps_to_its_kind() -> None:
    expected = {
        CHAT_KIND: (
            "chat language text-generation chat/completions /chat/completions "
            "/v1/chat/completions completions /completions responses /responses "
            "anthropic messages /messages openai openai-response gemini"
        ),
        "image": (
            "image image-generation image_generation images/generations "
            "/images/generations image_edit /v1/images/edits"
        ),
        "video": "video openai-video video_generation video-generation /v1/videos",
        "tts": "speech audio_speech audio/speech /audio/speech",
        "asr": (
            "transcription audio_transcription audio/transcriptions "
            "/audio/transcriptions /v1/audio/translations"
        ),
    }
    for kind, words in expected.items():
        for word in words.split():
            assert kinds_from_words((word,)) == {kind}, word


def test_words_that_name_no_kind_are_a_stated_none() -> None:
    """An embedding or rerank model is stated "none of these", as its pair is."""

    for word in ("embedding", "embeddings", "rerank", "reranking", "jina-rerank"):
        assert kinds_from_words((word,)) == frozenset(), word
    assert kinds_from_words(("moderation",)) == frozenset()


def test_unknown_words_state_nothing() -> None:
    """Anthropic's ``type: "model"`` and Novita's ``batch-api`` say no kind."""

    for words in (
        ("model",),
        ("batch-api",),
        ("realtime",),
        ("evaluation",),
        ("ocr",),
        ("search",),
        ("model", "batch-api"),
        (),
    ):
        assert kinds_from_words(words) is None, words


def test_recognised_words_union_and_unknown_ones_are_skipped() -> None:
    assert kinds_from_words(("chat", "/images/generations")) == {CHAT_KIND, "image"}
    assert kinds_from_words(("batch-api", "chat/completions")) == {CHAT_KIND}
    assert kinds_from_words(("embeddings", "chat")) == {CHAT_KIND}


def test_word_keys_drop_only_a_leading_slash_and_a_v1_segment() -> None:
    assert kind_word_key("/v1/Chat/Completions") == "chat/completions"
    assert kind_word_key("/chat/completions") == "chat/completions"
    assert kind_word_key(" Responses ") == "responses"
    assert kind_word_key("openai-video") == "openai-video"
    # Only one leading version segment; nothing in the middle is rewritten.
    assert kind_word_key("/v2/chat/completions") == "v2/chat/completions"


def test_the_table_only_names_real_kinds() -> None:
    for word, kinds in KIND_WORDS.items():
        assert kinds <= set(MODEL_KINDS), word
        assert word == kind_word_key(word), word


def test_a_lists_speech_and_transcription_read_as_audio_and_text() -> None:
    """OpenRouter writes a speech model's output as ``speech``, a transcriber's
    as ``transcription``; models.dev's vocabulary never uses either word."""

    speaks = DeclaredModalities(inputs=("text",), outputs=("speech",))
    transcribes = DeclaredModalities(inputs=("audio",), outputs=("transcription",))
    assert kinds_from_modalities(speaks) == {"tts"}
    assert kinds_from_modalities(transcribes) == {"asr"}


def test_the_two_novita_ming_rows_are_what_novita_declares() -> None:
    """User decision 15:43: they stay as Novita declares them -- every kind."""

    ming = DeclaredModalities(
        inputs=("audio", "image", "text", "video"),
        outputs=("audio", "image", "text", "video"),
    )
    assert kinds_from_modalities(ming) == set(MODEL_KINDS)


def test_declaration_words_are_the_type_then_the_endpoints() -> None:
    declaration = ProviderModelDeclaration(
        model_type="chat", endpoints=("/chat/completions", "/responses")
    )
    assert declaration_words(declaration) == (
        "chat",
        "/chat/completions",
        "/responses",
    )
    assert declaration_words(None) == ()
    assert declaration_words(ProviderModelDeclaration(modalities=TEXT)) == ()


# -------------------------------------------------------------- the rung order


def test_the_providers_own_pair_is_the_first_rung() -> None:
    """Tier 1 outranks models.dev, labelled as the provider's list."""

    kind = _resolve(
        "p/m",
        records={"p/m": ProviderModelDeclaration(modalities=HEARS)},
        catalogue={"p/m": TEXT},
    )

    assert kind.kinds == {CHAT_KIND, "asr"}
    assert kind.source == KIND_SOURCE_PROVIDER_LISTING
    assert kind.tier is ResolutionTier.PROVIDER_EXACT
    assert KIND_SOURCE_LABELS[kind.source] == "the provider's model list"


def test_a_tag_stripped_record_answers_at_tier_2() -> None:
    kind = _resolve(
        "p/m:free",
        records={"p/m:free": ProviderModelDeclaration(modalities=DRAWS)},
        record_tier=ResolutionTier.PROVIDER_TAG_STRIPPED,
    )

    assert kind.kinds == {"image"}
    assert kind.source == KIND_SOURCE_PROVIDER_LISTING
    assert kind.tier is ResolutionTier.PROVIDER_TAG_STRIPPED
    assert not kind.chat_listable


def test_models_dev_answers_where_the_record_states_no_pair() -> None:
    """A record with only words, a record with nothing, no record at all."""

    for records in (
        {"p/m": ProviderModelDeclaration(model_type="image")},
        {"p/m": None},
        {},
    ):
        kind = _resolve("p/m", records=records, catalogue={"p/m": HEARS})
        assert kind.kinds == {CHAT_KIND, "asr"}, records
        assert kind.source == KIND_SOURCE_MODELS_DEV, records
        assert kind.tier is ResolutionTier.MODELS_DEV_BUCKET_EXACT, records


def test_coarse_words_never_outrank_a_finer_models_dev_pair() -> None:
    """The 13 Command Code rows: models.dev says chat + transcription and the
    endpoint list implies only chat. Words first would drop Transcription."""

    kind = _resolve(
        "commandcode/inkling",
        records={
            "commandcode/inkling": ProviderModelDeclaration(
                endpoints=("/chat/completions", "/responses")
            )
        },
        catalogue={"commandcode/inkling": HEARS},
    )

    assert kind.kinds == {CHAT_KIND, "asr"}
    assert kind.source == KIND_SOURCE_MODELS_DEV


def test_words_fill_only_where_both_pairs_are_silent() -> None:
    kind = _resolve(
        "commandcode/x",
        records={"commandcode/x": ProviderModelDeclaration(endpoints=("/messages",))},
    )

    assert kind.kinds == {CHAT_KIND}
    assert kind.source == KIND_SOURCE_PROVIDER_WORDS
    assert kind.tier is ResolutionTier.PROVIDER_EXACT
    assert KIND_SOURCE_LABELS[kind.source] == "the provider's model type or endpoints"


def test_words_outrank_the_media_rail() -> None:
    kind = _resolve(
        "custom_x/draw",
        records={
            "custom_x/draw": ProviderModelDeclaration(
                endpoints=("image-generation", "openai-video")
            )
        },
        placements={"custom_x/draw": frozenset({"tts"})},
    )

    assert kind.kinds == {"image", "video"}
    assert kind.source == KIND_SOURCE_PROVIDER_WORDS


def test_unrecognised_words_fall_through_to_the_rail_then_unknown() -> None:
    """Anthropic's ``model`` is not a kind: the ladder goes on."""

    records = {"anthropic_oauth/claude-x": ProviderModelDeclaration(model_type="model")}
    placed = _resolve(
        "anthropic_oauth/claude-x",
        records=records,
        placements={"anthropic_oauth/claude-x": frozenset({"image"})},
    )
    assert placed.kinds == {"image"}
    assert placed.source == KIND_SOURCE_MEDIA_RAIL

    unknown = _resolve("anthropic_oauth/claude-x", records=records)
    assert unknown is UNKNOWN_KIND
    assert unknown.chat_listable


def test_a_stated_none_word_is_stated_not_unknown() -> None:
    kind = _resolve(
        "vercel/embed",
        records={"vercel/embed": ProviderModelDeclaration(model_type="embedding")},
    )

    assert kind.known
    assert kind.kinds == frozenset()
    assert not kind.chat_listable


def test_without_the_words_lookup_the_ladder_is_7_78_2s() -> None:
    """``kind_words=None`` and a models.dev-tier lookup: exactly the old answer."""

    lookup = _catalogue({"p/m": DRAWS}, ResolutionTier.CROSS_PROVIDER_EXACT)
    kind = resolve_model_kind("p/m", modalities=lookup, placements={})

    assert kind.kinds == {"image"}
    assert kind.source == KIND_SOURCE_MODELS_DEV
    assert kind.tier is ResolutionTier.CROSS_PROVIDER_EXACT
    assert resolve_model_kind("p/none", modalities=lookup, placements={}) is (
        UNKNOWN_KIND
    )


def test_the_source_follows_the_tier() -> None:
    assert modalities_source(ResolutionTier.PROVIDER_EXACT) == (
        KIND_SOURCE_PROVIDER_LISTING
    )
    assert modalities_source(ResolutionTier.PROVIDER_TAG_STRIPPED) == (
        KIND_SOURCE_PROVIDER_LISTING
    )
    for tier in (
        ResolutionTier.MODELS_DEV_BUCKET_EXACT,
        ResolutionTier.OPENROUTER_EXACT,
        ResolutionTier.CROSS_PROVIDER_BARE_UNTAGGED,
        None,
    ):
        assert modalities_source(tier) == KIND_SOURCE_MODELS_DEV, tier


def test_the_listing_filter_reads_the_words_too() -> None:
    settings = Settings().model_copy(update={"model": "deepseek/deepseek-chat"})
    declared_at = _records(
        {
            "p/draws": ProviderModelDeclaration(endpoints=("/images/generations",)),
            "p/typed": ProviderModelDeclaration(model_type="model"),
        }
    )
    listable = chat_listing_filter(
        settings,
        provider_first_modalities(declared_at, _catalogue({}, ResolutionTier(3))),
        EMPTY_HARNESS_TIERS,
        kind_words=provider_kind_words(declared_at),
    )

    assert not listable("p/draws")
    assert listable("p/typed")
    assert listable("p/never-described")


def test_the_words_lookup_reports_nothing_for_a_silent_record() -> None:
    lookup = provider_kind_words(
        _records({"p/pair-only": ProviderModelDeclaration(modalities=TEXT)})
    )

    assert lookup("p", "pair-only") == (None, None)
    assert lookup("p", "absent") == (None, None)
