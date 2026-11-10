"""The provider's own modality pair is rung 1 of the vision ladder (8.18.0).

`supports_vision` was the one field of the metadata extraction ladder whose top
rung was never connected: `record_with_declared` folded the row's context
window, output limit, two listed prices and reasoning word into the record, and
skipped vision. So a provider that published `input_modalities: ["text",
"image"]` was answered by models.dev's own bucket instead, and the Models page
printed *"accepts: text, image"* beside *"reads images: no"* for the same
model.

These hold the reader against **both vocabularies a provider publishes** --
bare words (`image`) and MIME types (`image/gif`, as OpenCode Zen and Go do) --
because that distinction is the whole load-bearing part of the fix:

* a reader that spoke only the bare word would make **88** OpenCode records go
  ``True -> False``, and every one of those models reads images;
* with both vocabularies read, the fix moves **16** records on the operator's
  own catalogue, of which 8 gain vision and 8 lose it.

The three fixtures below are the proof bar: a real OpenCode MIME list, a
bare-word list, and a text-only list.
"""

from my_claude_code.application.model_metadata import (
    DeclaredModalities,
    ProviderModelDeclaration,
    ProviderModelInfo,
    declared_accepts_images,
)
from my_claude_code.providers.model_listing import (
    extract_openai_model_infos,
    record_with_declared,
)

#: A real OpenCode Zen row's input list, verbatim from the keyless listing:
#: MIME types, not bare words. Every one of these names an image.
OPENCODE_MIME_INPUTS = (
    "application/pdf",
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
    "text",
)
#: Novita's shape for the same statement: the bare word.
BARE_WORD_INPUTS = ("image", "text")
#: A row that states a pair and names no image at all.
TEXT_ONLY_INPUTS = ("text",)


def _declared(inputs: tuple[str, ...], outputs: tuple[str, ...] = ("text",)):
    return ProviderModelDeclaration(
        modalities=DeclaredModalities(inputs=tuple(sorted(inputs)), outputs=outputs)
    )


def test_a_real_opencode_mime_list_names_an_image() -> None:
    """The fixture the whole fix turns on: `image/png` is an image."""

    assert declared_accepts_images(_declared(OPENCODE_MIME_INPUTS)) is True


def test_a_bare_word_list_names_an_image() -> None:
    assert declared_accepts_images(_declared(BARE_WORD_INPUTS)) is True


def test_a_text_only_list_names_no_image() -> None:
    """`["text"]` is a real answer -- ``False``, not "unknown"."""

    assert declared_accepts_images(_declared(TEXT_ONLY_INPUTS)) is False


def test_a_row_that_states_no_pair_has_said_nothing() -> None:
    """None, never ``False``: silence must not blind a model."""

    assert declared_accepts_images(None) is None
    assert declared_accepts_images(ProviderModelDeclaration()) is None


def test_only_the_image_family_counts() -> None:
    """`audio/*`, `video/*` and `application/pdf` are not images."""

    assert (
        declared_accepts_images(
            _declared(("application/pdf", "audio/mpeg", "text", "video/mp4"))
        )
        is False
    )
    # A MIME type that merely *starts* with the word is not the family.
    assert declared_accepts_images(_declared(("imagex/png", "text"))) is False


def test_the_fold_fills_vision_and_returns_the_same_object_when_it_cannot() -> None:
    """Rung 1 is a FILL: a parser's own value wins, and silence changes nothing."""

    declared = _declared(OPENCODE_MIME_INPUTS)
    filled = record_with_declared(ProviderModelInfo(model_id="m", declared=declared))
    assert filled.supports_vision is True

    silent = record_with_declared(ProviderModelInfo(model_id="m"))
    assert silent.supports_vision is None

    # A parser that already read the row keeps its answer: the OpenRouter
    # dialect's `_openrouter_accepts_images` is not replaced by the fold.
    own = ProviderModelInfo(model_id="m", supports_vision=False, declared=declared)
    assert record_with_declared(own).supports_vision is False

    # And with nothing to fill, the very same object comes back.
    nothing = ProviderModelInfo(model_id="m")
    assert record_with_declared(nothing) is nothing


def test_the_generic_parser_puts_the_pair_on_the_record_at_rung_one() -> None:
    """End to end through the parser that 39 of the 41 providers use."""

    payload = {
        "data": [
            {
                "id": "muse-ember-1.0",
                "input_modalities": list(OPENCODE_MIME_INPUTS),
                "output_modalities": ["text"],
            },
            {
                "id": "bunny",
                "input_modalities": list(TEXT_ONLY_INPUTS),
                "output_modalities": ["text"],
            },
            {"id": "silent"},
        ]
    }
    infos = {
        info.model_id: info
        for info in extract_openai_model_infos(payload, provider_name="T")
    }
    assert infos["muse-ember-1.0"].supports_vision is True
    assert infos["bunny"].supports_vision is False
    assert infos["silent"].supports_vision is None


def test_the_enrichment_cannot_overwrite_what_the_provider_stated() -> None:
    """The ordering is load-bearing: models.dev fills only a field still ``None``.

    This is the half of the rule that makes the fold safe. `record_with_declared`
    runs inside the parser, on a freshly built record; `enrich_model_infos` runs
    after it and only ever writes a field that is still unset. Reverse either
    and the provider rung stops outranking models.dev -- which is exactly what
    the naive reader would have done to 88 OpenCode records.
    """

    from my_claude_code.providers.runtime.models_dev import enrich_model_infos

    index = {
        "some-bucket": {
            "models": {"muse-ember-1.0": {"modalities": {"input": ["text"]}}}
        }
    }
    parsed = record_with_declared(
        ProviderModelInfo(
            model_id="muse-ember-1.0", declared=_declared(OPENCODE_MIME_INPUTS)
        )
    )
    (enriched,) = enrich_model_infos((parsed,), index, "some-other-provider")
    assert enriched.supports_vision is True, "models.dev's text-only row must not win"
