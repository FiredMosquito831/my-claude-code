"""LiteLLM's price map as a rung: units, key namespacing, and integrity.

Three measured traps, one test group each. The unit trap is the expensive one:
LiteLLM publishes USD per *single token* and models.dev publishes USD per
*million*, so a normalisation in the wrong place is a 1,000,000x error that
still looks like a plausible number.
"""

import json

import pytest

from my_claude_code.application.cost import SOURCE_LITELLM
from my_claude_code.providers.runtime.litellm_prices import (
    LITELLM_MIN_ENTRIES,
    LITELLM_PINNED_COMMIT,
    LITELLM_PINNED_URL,
    litellm_rate_card,
    payload_passes_integrity,
    read_litellm_cache,
    write_litellm_cache,
)

_ANTHROPIC_ENTRY = {
    "litellm_provider": "anthropic",
    "input_cost_per_token": 3e-06,
    "output_cost_per_token": 1.5e-05,
    "cache_read_input_token_cost": 3e-07,
    "cache_creation_input_token_cost": 3.75e-06,
}

# The trap, verbatim from the pinned commit: the bare key is the *Vertex* row.
_VERTEX_GEMINI_ENTRY = {
    "litellm_provider": "vertex_ai-language-models",
    "input_cost_per_token": 1.25e-06,
    "output_cost_per_token": 1e-05,
}

_OPENROUTER_ENTRY = {
    "litellm_provider": "openrouter",
    "input_cost_per_token": 3e-06,
    "output_cost_per_token": 1.5e-05,
}


@pytest.fixture
def cache_path(tmp_path):
    return tmp_path / "litellm-prices.json"


def _seed(path, index):
    write_litellm_cache(index, path, etag='"abc"', source_url=LITELLM_PINNED_URL)
    return path


def test_a_bare_key_prices_when_its_own_provider_agrees(cache_path):
    _seed(cache_path, {"claude-sonnet-4-5": _ANTHROPIC_ENTRY})
    card = litellm_rate_card("anthropic", "claude-sonnet-4-5", cache_path)
    assert card is not None
    assert card.source == SOURCE_LITELLM
    assert card.input_price == 3e-06
    assert card.cache_write_price == 3.75e-06


def test_a_bare_litellm_key_is_rejected_when_litellm_provider_disagrees(cache_path):
    """Bare ``gemini-2.5-pro`` is Vertex's row, not Google's.

    Splitting a key on ``/`` to infer a provider -- or accepting a bare key on
    name alone -- would price a Google route from Vertex's card, which is a
    different deployment at a different price.
    """
    _seed(cache_path, {"gemini-2.5-pro": _VERTEX_GEMINI_ENTRY})
    assert litellm_rate_card("google", "gemini-2.5-pro", cache_path) is None
    # The provider whose row it actually is still gets it.
    assert litellm_rate_card("vertex", "gemini-2.5-pro", cache_path) is not None


def test_the_litellm_key_is_tried_prefixed_before_bare(cache_path):
    """3,220 of 3,850 keys are prefixed, so prefix-first is the higher-hit order."""
    _seed(
        cache_path,
        {
            "openrouter/anthropic/claude-sonnet-4.5": {
                **_OPENROUTER_ENTRY,
                "input_cost_per_token": 9.9e-06,
            },
            "anthropic/claude-sonnet-4.5": _ANTHROPIC_ENTRY,
        },
    )
    card = litellm_rate_card("open_router", "anthropic/claude-sonnet-4.5", cache_path)
    assert card is not None
    assert card.input_price == 9.9e-06, "the prefixed key for the routed provider"
    assert "openrouter/anthropic/claude-sonnet-4.5" in (card.tier_label or "")


def test_a_provider_id_is_matched_across_spelling_conventions(cache_path):
    """MCC spells it ``open_router``; LiteLLM spells it ``openrouter``."""
    _seed(cache_path, {"some-gateway-model": _OPENROUTER_ENTRY})
    assert litellm_rate_card("open_router", "some-gateway-model", cache_path)


def test_the_units_are_per_token_and_are_not_divided_again(cache_path):
    """models.dev's ``3`` and LiteLLM's ``3e-06`` must land on the same card."""
    _seed(cache_path, {"claude-sonnet-4-5": _ANTHROPIC_ENTRY})
    card = litellm_rate_card("anthropic", "claude-sonnet-4-5", cache_path)
    assert card is not None
    assert card.input_price is not None
    assert card.input_price * 1_000_000 == pytest.approx(3.0)


def test_the_schema_sample_never_prices_a_model(cache_path):
    """``sample_spec`` prices everything at zero and is not a model."""
    _seed(
        cache_path,
        {"sample_spec": {"litellm_provider": "anthropic", "input_cost_per_token": 0.0}},
    )
    assert litellm_rate_card("anthropic", "sample_spec", cache_path) is None


def test_an_absent_cache_prices_nothing(cache_path):
    assert litellm_rate_card("anthropic", "claude-sonnet-4-5", cache_path) is None


def test_a_shrunken_litellm_payload_is_rejected_and_the_snapshot_is_kept():
    """LiteLLM's own two rules: a floor on entries, and no halving."""
    good = {f"model-{index}": {} for index in range(LITELLM_MIN_ENTRIES + 500)}
    assert payload_passes_integrity(good, None)
    assert not payload_passes_integrity({"a": {}}, None)
    halved = {f"model-{index}": {} for index in range(LITELLM_MIN_ENTRIES + 10)}
    # Above the floor on its own, but less than half of what is already cached.
    previous = {f"model-{index}": {} for index in range(LITELLM_MIN_ENTRIES * 3)}
    assert not payload_passes_integrity(halved, previous)
    assert not payload_passes_integrity(["not", "a", "map"], None)


def test_the_cache_round_trips_with_its_provenance(cache_path):
    _seed(cache_path, {"claude-sonnet-4-5": _ANTHROPIC_ENTRY})
    cache = read_litellm_cache(cache_path)
    assert cache is not None
    assert cache.etag == '"abc"'
    assert cache.source_url == LITELLM_PINNED_URL
    assert cache.fresh


def test_a_corrupt_cache_reads_as_absent(cache_path):
    cache_path.write_text("{not json", encoding="utf-8")
    assert read_litellm_cache(cache_path) is None
    cache_path.write_text(json.dumps({"index": "wrong shape"}), encoding="utf-8")
    assert read_litellm_cache(cache_path) is None


def test_the_pinned_commit_is_an_immutable_forty_character_sha():
    assert len(LITELLM_PINNED_COMMIT) == 40
    assert all(character in "0123456789abcdef" for character in LITELLM_PINNED_COMMIT)
    assert LITELLM_PINNED_COMMIT in LITELLM_PINNED_URL
