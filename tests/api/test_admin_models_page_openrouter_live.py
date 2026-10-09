"""The Models page places OpenRouter's live list per field class (7.84.0).

Numbers fill gaps only; intrinsic flags go above models.dev's OpenRouter copy
and the vote (bucket-less providers) and never above a provider's own
statement or a models.dev bucket, with both statements shown where they
differ; three display rows are added while the rung is on. What MCC may be
told to decide (the preferences) and what a learned fact is compared with are
the ladder routing reads, before the live list is placed. Without the rung the
page is exactly what it was.
"""

from typing import Any

import pytest

from my_claude_code.api.model_admin import (
    _model_entry,
    attach_learned_facts,
    capability_payload,
)
from my_claude_code.application.model_metadata import (
    DeclaredModalities,
    ProviderModelInfo,
)
from my_claude_code.application.openrouter_live import (
    LIVE_MATCH_BARE_TAGGED,
    LIVE_MATCH_EXACT,
    LiveCatalogue,
    LiveModel,
)
from my_claude_code.config.model_overrides import ModelParameterOverrides
from my_claude_code.core.model_visibility import ModelVisibility
from my_claude_code.providers.runtime.models_dev import write_models_dev_cache

TEXT = DeclaredModalities(inputs=("text",), outputs=("text",))

#: ``novita`` has a models.dev bucket; ``nous_portal`` has none, so it reads
#: models.dev's OpenRouter copy (tiers 5-6).
MODELS_DEV: dict[str, Any] = {
    "novita": {
        "id": "novita",
        "models": {
            "acme/bucketed": {
                "id": "acme/bucketed",
                "attachment": False,
                "reasoning": True,
                "tool_call": True,
                "modalities": {"input": ["text"], "output": ["text"]},
                "limit": {"context": 100000, "output": 8000},
                "cost": {"input": 1.0, "output": 2.0},
            }
        },
    },
    "openrouter": {
        "id": "openrouter",
        "models": {
            "acme/copied": {
                "id": "acme/copied",
                "attachment": False,
                "reasoning": False,
                "tool_call": False,
                "modalities": {"input": ["text"], "output": ["text"]},
                "limit": {"context": 64000, "output": 4000},
                "cost": {"input": 3.0, "output": 4.0},
            }
        },
    },
}


@pytest.fixture(autouse=True)
def _models_dev() -> None:
    write_models_dev_cache(MODELS_DEV)


def _live_model(**fields: Any) -> LiveModel:
    base: dict[str, Any] = {
        "slugs": ("acme/model",),
        "match": LIVE_MATCH_EXACT,
        "modalities": TEXT,
        "supports_vision": True,
        "can_reason": True,
        "supports_tool_calls": True,
        "context_length": 131072,
        "max_output_tokens": 65536,
        "input_price": 0.5,
        "output_price": 1.5,
        "cache_read_price": 0.05,
        "cache_write_price": 0.6,
        "reasoning_price": 2.0,
        "description": "A model.",
        "knowledge_cutoff": "2025-03-31",
        "listed_at": "2026-02-11",
    }
    base.update(fields)
    return LiveModel(**base)


def _catalogue(answer: LiveModel | None) -> LiveCatalogue:
    return LiveCatalogue(
        mark="1:1", fetched_at=None, rows=1, lookup=lambda _p, _m: answer
    )


def test_without_the_rung_the_page_is_unchanged() -> None:
    before = capability_payload("novita", "acme/bucketed", None)
    assert capability_payload("novita", "acme/bucketed", None, live=None) == before
    for key in ("description", "knowledge_cutoff", "listed_on_openrouter"):
        assert key not in before


def test_numbers_fill_only_gaps() -> None:
    answer = _live_model()
    # The bucket states window, output and both prices; the live list fills
    # only the three rates the bucket does not state.
    payload = capability_payload(
        "novita", "acme/bucketed", None, live=_catalogue(answer)
    )
    assert payload["context_length"]["value"] == 100000
    assert payload["max_output_tokens"]["value"] == 8000
    assert payload["input_price"]["value"] == 1.0
    assert payload["output_price"]["value"] == 2.0
    for key, value in (
        ("cache_read_price", 0.05),
        ("cache_write_price", 0.6),
        ("reasoning_price", 2.0),
    ):
        row = payload[key]
        assert row["value"] == value, key
        assert row["source"] == "openrouter_live", key
        assert row["source_label"] == "OpenRouter live", key
        assert row["tier"] is None, key
        assert row["tier_label"] == "OpenRouter live, exact id", key
        assert row["ladder_value"] is None, key
        assert "routing" in row["note"], key


def test_a_gap_on_an_unknown_model_is_filled_whole() -> None:
    answer = _live_model(match=LIVE_MATCH_BARE_TAGGED)
    payload = capability_payload(
        "novita", "acme/unknown", None, live=_catalogue(answer)
    )
    assert payload["max_output_tokens"]["value"] == 65536
    assert payload["context_length"]["value"] == 131072
    assert payload["supports_vision"]["value"] is True
    assert payload["supports_tool_calls"]["value"] is True
    assert payload["reasoning"]["can_reason"]["value"] is True
    assert payload["reasoning"]["can_reason"]["source"] == "openrouter_live"
    # The other reasoning sub-fields are OpenRouter's normalisation of its
    # upstreams and are never read for another host.
    assert payload["reasoning"]["supported_efforts"]["value"] is None
    assert payload["declared_modalities"]["value"] == "text → text"
    assert (
        payload["supports_vision"]["tier_label"] == "OpenRouter live, bare model + tag"
    )


def test_a_bucket_is_never_overridden_and_both_answers_are_shown() -> None:
    answer = _live_model(supports_tool_calls=False, can_reason=False)
    payload = capability_payload(
        "novita", "acme/bucketed", None, live=_catalogue(answer)
    )
    tools = payload["supports_tool_calls"]
    assert tools["value"] is True
    assert tools["source"] == "models_dev"
    assert tools["also_stated"]["value"] is False
    assert tools["also_stated"]["source"] == "openrouter_live"
    reason = payload["reasoning"]["can_reason"]
    assert reason["value"] is True
    assert reason["also_stated"]["value"] is False


def test_without_a_bucket_the_live_list_answers_before_models_dev_s_copy() -> None:
    answer = _live_model(supports_tool_calls=True, can_reason=True)
    copy_only = capability_payload("nous_portal", "acme/copied", None)
    assert copy_only["supports_tool_calls"]["tier"] == 5
    payload = capability_payload(
        "nous_portal", "acme/copied", None, live=_catalogue(answer)
    )
    tools = payload["supports_tool_calls"]
    assert tools["value"] is True
    assert tools["source"] == "openrouter_live"
    # What it stood in for, shown beside it.
    assert tools["also_stated"]["value"] is False
    assert tools["also_stated"]["tier"] == 5
    assert tools["ladder_value"] is False
    # Numbers never go above models.dev, copy or not.
    assert payload["context_length"]["value"] == 64000
    assert payload["max_output_tokens"]["value"] == 4000


def test_the_provider_s_own_statement_is_never_replaced() -> None:
    record = ProviderModelInfo(
        "acme/unknown",
        supports_vision=False,
        supported_parameters=frozenset({"max_tokens"}),
        supports_thinking=False,
        max_output_tokens=1000,
    )
    payload = capability_payload(
        "nous_portal", "acme/unknown", record, live=_catalogue(_live_model())
    )
    assert payload["supports_vision"]["value"] is False
    assert payload["supports_vision"]["also_stated"]["value"] is True
    assert payload["supports_tool_calls"]["value"] is False
    assert payload["supports_tool_calls"]["source"] == "provider"
    assert payload["reasoning"]["can_reason"]["value"] is False
    assert payload["max_output_tokens"]["value"] == 1000
    assert "also_stated" not in payload["max_output_tokens"]


def test_the_three_display_rows() -> None:
    payload = capability_payload(
        "novita", "acme/unknown", None, live=_catalogue(_live_model())
    )
    assert payload["description"]["value"] == "A model."
    assert payload["knowledge_cutoff"]["value"] == "2025-03-31"
    listed = payload["listed_on_openrouter"]
    assert listed["value"] == "2026-02-11"
    assert "not the model's release date" in listed["note"]
    unmatched = capability_payload(
        "novita", "acme/unknown", None, live=_catalogue(None)
    )
    for key in ("description", "knowledge_cutoff", "listed_on_openrouter"):
        assert unmatched[key]["value"] is None
        assert unmatched[key]["source"] == "unknown"


def test_openrouter_s_own_models_get_only_the_display_rows() -> None:
    answer = _live_model(own_list=True)
    before = capability_payload("open_router", "acme/model", None)
    after = capability_payload(
        "open_router", "acme/model", None, live=_catalogue(answer)
    )
    added = {"description", "knowledge_cutoff", "listed_on_openrouter"}
    assert set(after) - set(before) == added
    assert {k: v for k, v in after.items() if k not in added} == before


def test_the_accepts_produces_row_keeps_its_words() -> None:
    """A stated pair is never re-worded; OpenRouter's differing pair is beside it."""

    answer = _live_model(
        modalities=DeclaredModalities(inputs=("file", "text"), outputs=("text",))
    )
    record = ProviderModelInfo("acme/copied")
    payload = capability_payload(
        "nous_portal", "acme/copied", record, live=_catalogue(answer)
    )
    row = payload["declared_modalities"]
    assert row["value"] == "text → text"
    assert row["source"] == "models_dev"
    assert row["also_stated"]["value"] == "file, text → text"


def test_preferences_and_learned_facts_read_the_ladder_routing_reads() -> None:
    """An output limit or a "does not reason" only OpenRouter states bounds nothing."""

    answer = _live_model(max_output_tokens=65536, can_reason=False)
    fact = {
        "fact_kind": "output_cap",
        "model_id": "acme/unknown",
        "value": 65536,
        "source": "rejection",
    }
    common: dict[str, Any] = {
        "visibility": ModelVisibility.from_raw("", ""),
        "overrides": ModelParameterOverrides(),
        "configured_refs": frozenset(),
        "learned": {"novita/acme/unknown": [fact]},
    }
    before = _model_entry("novita/acme/unknown", None, **common)
    after = _model_entry("novita/acme/unknown", None, live=_catalogue(answer), **common)
    assert after["preferences"] == before["preferences"]
    assert after["preferences"]["max_output_tokens"]["limit"] is None
    assert after["learned"] == before["learned"]
    assert after["learned"][0]["agrees"] is None
    assert after["capabilities"]["max_output_tokens"]["value"] == 65536
    assert after["capabilities"]["max_output_tokens"]["learned"]["agrees"] is None
    # The cached-page path attaches facts after the live list is placed, and
    # must reach the same comparison.
    cached = capability_payload("novita", "acme/unknown", None, live=_catalogue(answer))
    rendered = attach_learned_facts(cached, [fact])
    assert rendered[0]["agrees"] is None
