"""A model's kind comes from declared data only, and unknown is never hidden (7.78.2)."""

from my_claude_code.application.model_kinds import (
    CHAT_KIND,
    KIND_SOURCE_MEDIA_RAIL,
    KIND_SOURCE_MODELS_DEV,
    UNKNOWN_KIND,
    chat_listing_filter,
    kinds_from_modalities,
    media_rail_placements,
    resolve_model_kind,
)
from my_claude_code.application.model_metadata import DeclaredModalities
from my_claude_code.config.harness_tiers import (
    EMPTY_HARNESS_TIERS,
    HarnessTierOverride,
    HarnessTiers,
)
from my_claude_code.config.settings import Settings
from my_claude_code.core.model_ids import ResolutionTier


def _declared(inputs: tuple[str, ...], outputs: tuple[str, ...]) -> DeclaredModalities:
    return DeclaredModalities(inputs=inputs, outputs=outputs)


def _lookup(table: dict[str, DeclaredModalities]):
    def lookup(
        provider_id: str, model_id: str
    ) -> tuple[DeclaredModalities | None, ResolutionTier | None]:
        found = table.get(f"{provider_id}/{model_id}")
        return found, None if found is None else ResolutionTier.MODELS_DEV_BUCKET_EXACT

    return lookup


def _settings(**update: object) -> Settings:
    return Settings().model_copy(update={"model": "deepseek/deepseek-chat", **update})


def test_each_declared_pair_maps_to_the_rail_it_belongs_on() -> None:
    cases = {
        (("text",), ("text",)): {CHAT_KIND},
        (("text", "image"), ("text",)): {CHAT_KIND},
        (("text", "image"), ("image",)): {"image"},
        (("text", "image"), ("text", "image")): {CHAT_KIND, "image"},
        (("text",), ("audio",)): {"tts"},
        (("audio",), ("text",)): {"asr"},
        (("text", "audio"), ("text",)): {CHAT_KIND, "asr"},
        (("text", "audio"), ("text", "audio")): {CHAT_KIND, "tts", "asr"},
        (("text", "image"), ("video",)): {"video"},
    }
    for (inputs, outputs), expected in cases.items():
        assert kinds_from_modalities(_declared(inputs, outputs)) == expected, (
            inputs,
            outputs,
        )


def test_a_declaration_that_fits_no_rail_is_stated_none_not_unknown() -> None:
    """An embedding model is a stated "none of these", which hides it from chat."""

    stated = kinds_from_modalities(_declared(("text",), ("embedding",)))
    assert stated == frozenset()
    kind = resolve_model_kind(
        "p/embed",
        modalities=_lookup({"p/embed": _declared(("text",), ("embedding",))}),
        placements={},
    )
    assert kind.known
    assert not kind.chat_listable


def test_unknown_is_offered_everywhere() -> None:
    kind = resolve_model_kind("p/quiet", modalities=_lookup({}), placements={})

    assert kind is UNKNOWN_KIND
    assert not kind.known
    assert kind.chat_listable
    assert kind.offered_on("image")
    assert kind.offered_on("video")


def test_models_dev_outranks_where_the_operator_put_it() -> None:
    """A chat model saved on the Image rail is still a chat model."""

    kind = resolve_model_kind(
        "p/chatty",
        modalities=_lookup({"p/chatty": _declared(("text",), ("text",))}),
        placements={"p/chatty": frozenset({"image"})},
    )

    assert kind.kinds == {CHAT_KIND}
    assert kind.source == KIND_SOURCE_MODELS_DEV
    assert kind.chat_listable
    assert not kind.offered_on("image")


def test_a_media_rail_states_the_kind_only_where_models_dev_is_silent() -> None:
    kind = resolve_model_kind(
        "custom_x/draw-2",
        modalities=_lookup({}),
        placements={"custom_x/draw-2": frozenset({"image"})},
    )

    assert kind.kinds == {"image"}
    assert kind.source == KIND_SOURCE_MEDIA_RAIL
    assert not kind.chat_listable


def test_a_ref_on_a_chat_rail_too_is_not_a_media_placement() -> None:
    """Saved on both kinds of rail, the operator has said nothing about which."""

    settings = _settings(
        model_image="custom_x/both",
        model_haiku_fallbacks="custom_x/both",
        model_video="custom_x/video-only",
    )

    placements = media_rail_placements(settings, EMPTY_HARNESS_TIERS)

    assert placements == {"custom_x/video-only": frozenset({"video"})}


def test_an_agent_tier_counts_as_a_chat_rail() -> None:
    settings = _settings(model_image="custom_x/drawn", model_tts="custom_x/spoken")
    tiers = HarnessTiers(
        harnesses={
            "codex": {
                "best": HarnessTierOverride(model="custom_x/drawn"),
            },
            "pi": {
                "fast": HarnessTierOverride(fallbacks=("custom_x/spoken",)),
            },
        }
    )

    assert media_rail_placements(settings, tiers) == {}


def test_the_listing_filter_hides_only_a_stated_non_chat_kind() -> None:
    settings = _settings(model_video="custom_x/clip")
    listable = chat_listing_filter(
        settings,
        _lookup(
            {
                "p/chat": _declared(("text",), ("text",)),
                "p/draw": _declared(("text",), ("image",)),
                "p/both": _declared(("text",), ("text", "image")),
            }
        ),
        EMPTY_HARNESS_TIERS,
    )

    assert listable("p/chat")
    assert listable("p/both")
    assert listable("p/never-described")
    assert not listable("p/draw")
    assert not listable("custom_x/clip")


def test_no_name_decides_a_kind() -> None:
    """``image`` in an id is not a declaration: silence stays unknown."""

    lookup = _lookup({})
    for ref in (
        "custom_agnes/agnes-image-2.5-flash",
        "custom_agnes/agnes-video-2.5",
        "novita/ming-image-0.1-design",
        "groq/whisper-large-v3",
        "p/tts-1",
    ):
        kind = resolve_model_kind(ref, modalities=lookup, placements={})
        assert kind is UNKNOWN_KIND, ref
        assert kind.chat_listable, ref
