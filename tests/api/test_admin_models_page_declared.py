"""The Models page shows what the provider's own list says a model is (7.79.0).

Three rows join the capability panel, each with the source and rung the rest of
the panel already uses: what the model accepts and produces (the provider's row
first, then models.dev down its ladder), the provider's type word and the
provider's endpoint words (only a provider publishes those two). They are shown,
never acted on, and every row the panel carried before is unchanged.
"""

from my_claude_code.api.model_admin import capability_payload, declared_payload
from my_claude_code.application.model_metadata import (
    DeclaredModalities,
    ProviderModelDeclaration,
    ProviderModelInfo,
)
from my_claude_code.core.model_ids import ResolutionTier

DECLARED_KEYS = ("declared_modalities", "declared_type", "declared_endpoints")

NOVITA = ProviderModelDeclaration(
    modalities=DeclaredModalities(inputs=("image", "text", "video"), outputs=("text",)),
    model_type="chat",
    endpoints=("anthropic", "chat/completions", "responses"),
)


def _models_dev_says(declared: DeclaredModalities | None, tier: ResolutionTier | None):
    def lookup(provider_id: str, model_id: str):
        return declared, tier

    return lookup


def test_the_provider_row_answers_first_at_its_own_rung() -> None:
    payload = capability_payload(
        "novita",
        "zai-org/glm-5.3-flash",
        ProviderModelInfo("zai-org/glm-5.3-flash", declared=NOVITA),
        modalities_lookup=_models_dev_says(
            DeclaredModalities(inputs=("text",), outputs=("text",)),
            ResolutionTier.MODELS_DEV_BUCKET_EXACT,
        ),
    )

    assert payload["declared_modalities"] == {
        "value": "image, text, video → text",
        "source": "provider",
        "source_label": "provider /models",
        "approximate": False,
        "reference": False,
        "tier": 1,
        "tier_label": "provider /models, exact id",
        "inputs": ["image", "text", "video"],
        "outputs": ["text"],
    }
    assert payload["declared_type"]["value"] == "chat"
    assert payload["declared_type"]["source"] == "provider"
    assert payload["declared_type"]["tier"] == 1
    assert payload["declared_endpoints"]["value"] == [
        "anthropic",
        "chat/completions",
        "responses",
    ]
    assert payload["declared_endpoints"]["source"] == "provider"


def test_a_tag_stripped_record_reports_tier_two() -> None:
    rows = declared_payload(
        "nous_portal",
        "x/model:free",
        NOVITA,
        ResolutionTier.PROVIDER_TAG_STRIPPED,
        _models_dev_says(None, None),
    )
    assert {rows[key]["tier"] for key in DECLARED_KEYS} == {2}


def test_models_dev_answers_the_modalities_where_the_provider_is_silent() -> None:
    payload = capability_payload(
        "nvidia_nim",
        "meta/llama-5",
        ProviderModelInfo("meta/llama-5"),
        modalities_lookup=_models_dev_says(
            DeclaredModalities(inputs=("image", "text"), outputs=("text",)),
            ResolutionTier.MODELS_DEV_BUCKET_EXACT,
        ),
    )

    modalities = payload["declared_modalities"]
    assert modalities["value"] == "image, text → text"
    assert modalities["source"] == "models_dev"
    assert modalities["tier"] == 3
    assert modalities["inputs"] == ["image", "text"]
    assert modalities["outputs"] == ["text"]
    # Only a provider publishes these two: silence is "unknown", never a guess.
    assert payload["declared_type"]["value"] is None
    assert payload["declared_type"]["source"] == "unknown"
    assert payload["declared_endpoints"]["value"] is None
    assert payload["declared_endpoints"]["source"] == "unknown"


def test_an_approximate_models_dev_answer_is_badged_approximate() -> None:
    rows = declared_payload(
        "custom_b_ai",
        "kimi-k3",
        None,
        None,
        _models_dev_says(
            DeclaredModalities(inputs=("text",), outputs=("text",)),
            ResolutionTier.CROSS_PROVIDER_EXACT,
        ),
    )
    assert rows["declared_modalities"]["source"] == "approximate"
    assert rows["declared_modalities"]["approximate"] is True


def test_nobody_saying_anything_is_unknown_on_all_three() -> None:
    rows = declared_payload(
        "kimi_coding", "k3", None, None, _models_dev_says(None, None)
    )
    for key in DECLARED_KEYS:
        assert rows[key]["value"] is None
        assert rows[key]["source"] == "unknown"
    assert rows["declared_modalities"]["inputs"] is None
    assert rows["declared_modalities"]["outputs"] is None


def test_the_three_rows_are_appended_and_change_no_other_row(monkeypatch) -> None:
    """The panel without the three keys is the panel 7.78.10 drew."""

    monkeypatch.setattr(
        "my_claude_code.api.model_admin.declared_modalities_tiered",
        lambda provider_id, model_id: (None, None),
    )
    bare = ProviderModelInfo("m", context_length=32768, max_output_tokens=4096)
    with_words = ProviderModelInfo(
        "m", context_length=32768, max_output_tokens=4096, declared=NOVITA
    )

    before = capability_payload("open_router", "m", bare)
    after = capability_payload("open_router", "m", with_words)

    # The three rows close the 7.79.0 panel; only 7.85.0's four display rows
    # (description, knowledge cutoff, retirement, publication) follow them.
    assert list(after)[-7:-4] == list(DECLARED_KEYS)
    assert list(after)[-4:] == [
        "description",
        "knowledge_cutoff",
        "retires_at",
        "published_at",
    ]
    for key in DECLARED_KEYS:
        before.pop(key)
        after.pop(key)
    assert after == before
