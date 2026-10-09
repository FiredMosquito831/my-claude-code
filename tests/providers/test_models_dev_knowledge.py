"""Every models.dev rung's own statement about one model (7.86.0).

The ladder stops at the first rung that answers a field and keeps only that
answer. ``models_dev_knowledge`` reads, off the very same indexes, what EVERY
rung says -- the provider's own bucket, models.dev's OpenRouter copy, the
cross-provider vote with its sample -- plus what discovery fills into the
record, and the rows themselves. Read-only: these hold that each statement is
the one the ladder would read at that rung, that a rung the ladder never reads
for a provider is marked so, and that asking writes no "approximate match" log
line about a provider the vote was never run for.
"""

from pathlib import Path
from typing import Any

import pytest

from my_claude_code.application.model_metadata import (
    DeclaredModalities,
    ProviderModelInfo,
)
from my_claude_code.providers.runtime import models_dev
from my_claude_code.providers.runtime.models_dev import (
    MODELS_DEV_RUNG_BUCKET,
    MODELS_DEV_RUNG_REFERENCE,
    MODELS_DEV_RUNG_VOTE,
    enrich_model_infos,
    model_context_length_tiered,
    model_display_facts_lookup,
    model_output_limit_tiered,
    model_reasoning_capability_tiered,
    models_dev_knowledge,
    read_models_dev_cache,
    write_models_dev_cache,
)


def _row(output: int, **extra: Any) -> dict[str, Any]:
    return {
        "limit": {"context": 100_000, "output": output},
        "modalities": {"input": ["text"], "output": ["text"]},
        "reasoning": True,
        "tool_call": True,
        **extra,
    }


MODELS_DEV: dict[str, Any] = {
    # ``novita`` has a bucket of its own.
    "novita-ai": {
        "id": "novita-ai",
        "models": {
            "acme/bucketed": _row(
                8_000,
                cost={"input": 1.0, "output": 2.0},
                description="The bucket's words.",
                knowledge="2025-06",
                modalities={"input": ["image", "text"], "output": ["text"]},
            ),
        },
    },
    "openrouter": {
        "id": "openrouter",
        "models": {
            "acme/bucketed": _row(9_000, description="The copy's words."),
            "acme/copied": _row(4_000, cost={"input": 3.0, "output": 4.0}),
        },
    },
    # Same-named rows elsewhere: the vote's sample.
    **{
        name: {
            "id": name,
            "models": {
                "acme/copied": _row(output),
                "acme/voted": _row(output, description=f"{name} words"),
            },
        }
        for name, output in (("one", 1_000), ("two", 1_000), ("three", 2_000))
    },
}


@pytest.fixture(autouse=True)
def _catalogue() -> None:
    write_models_dev_cache(MODELS_DEV)


def _by_rung(statements: Any) -> dict[str, Any]:
    return {statement.rung: statement for statement in statements}


def test_a_provider_with_a_bucket_is_answered_from_it_and_the_rest_is_shown() -> None:
    knowledge = models_dev_knowledge("novita", "acme/bucketed")
    assert knowledge.has_bucket is True
    output = _by_rung(knowledge.statements["max_output_tokens"])
    assert output[MODELS_DEV_RUNG_BUCKET].value == 8_000
    assert output[MODELS_DEV_RUNG_BUCKET].consulted is True
    # The ladder reads exactly the bucket's statement.
    value, tier = model_output_limit_tiered("novita", "acme/bucketed")
    assert (value, tier) == (8_000, output[MODELS_DEV_RUNG_BUCKET].tier)
    # The OpenRouter copy says something else, and is never read for novita.
    assert output[MODELS_DEV_RUNG_REFERENCE].value == 9_000
    assert output[MODELS_DEV_RUNG_REFERENCE].consulted is False
    description = _by_rung(knowledge.statements["description"])
    assert description[MODELS_DEV_RUNG_BUCKET].value == "The bucket's words."
    assert description[MODELS_DEV_RUNG_REFERENCE].value == "The copy's words."
    pair = _by_rung(knowledge.statements["declared_modalities"])
    assert pair[MODELS_DEV_RUNG_BUCKET].value == DeclaredModalities(
        ("image", "text"), ("text",)
    )
    rows = [
        (row.rung, row.bucket, row.model_key, row.consulted) for row in knowledge.rows
    ]
    assert rows[0] == (MODELS_DEV_RUNG_BUCKET, "novita-ai", "acme/bucketed", True)
    assert rows[1] == (
        MODELS_DEV_RUNG_REFERENCE,
        "openrouter",
        "acme/bucketed",
        False,
    )
    assert knowledge.rows[0].row["description"] == "The bucket's words."


def test_without_a_bucket_the_copy_then_the_vote_with_its_sample() -> None:
    copied = models_dev_knowledge("nous_portal", "acme/copied")
    assert copied.has_bucket is False
    output = _by_rung(copied.statements["max_output_tokens"])
    assert output[MODELS_DEV_RUNG_REFERENCE].consulted is True
    assert model_output_limit_tiered("nous_portal", "acme/copied") == (
        output[MODELS_DEV_RUNG_REFERENCE].value,
        output[MODELS_DEV_RUNG_REFERENCE].tier,
    )
    voted = models_dev_knowledge("nous_portal", "acme/voted")
    vote = _by_rung(voted.statements["max_output_tokens"])[MODELS_DEV_RUNG_VOTE]
    assert (vote.value, vote.reporters, vote.consulted) == (1_000, 3, True)
    assert vote.agreement == pytest.approx(2 / 3)
    assert model_output_limit_tiered("nous_portal", "acme/voted") == (
        vote.value,
        vote.tier,
    )
    context = _by_rung(voted.statements["context_length"])[MODELS_DEV_RUNG_VOTE]
    assert model_context_length_tiered("nous_portal", "acme/voted") == (
        context.value,
        context.tier,
    )
    # Three reporters clear the quorum even when they all disagree; the split
    # goes to the longer text, exactly as the page's own lookup answers it.
    words = _by_rung(voted.statements["description"])[MODELS_DEV_RUNG_VOTE]
    assert (words.value, words.reporters) == ("three words", 3)
    assert model_display_facts_lookup()("nous_portal", "acme/voted").description == (
        words.value,
        words.tier,
    )
    assert {row.bucket for row in voted.rows} == {"one", "two", "three"}


def test_reasoning_is_stated_per_field_and_rung() -> None:
    knowledge = models_dev_knowledge("nous_portal", "acme/voted")
    can_reason = knowledge.statements["reasoning.can_reason"]
    assert [statement.value for statement in can_reason] == [True]
    capability, tiers = model_reasoning_capability_tiered("nous_portal", "acme/voted")
    assert capability is not None
    assert can_reason[0].tier == tiers["can_reason"]


def test_the_discovery_fill_is_what_discovery_fills() -> None:
    cache = read_models_dev_cache()
    assert cache is not None
    (filled,) = enrich_model_infos(
        (ProviderModelInfo("acme/bucketed"),), cache.index, "novita"
    )
    knowledge = models_dev_knowledge("novita", "acme/bucketed")
    assert knowledge.discovery_fill == {
        "context_length": filled.context_length,
        "input_price": filled.input_price,
        "output_price": filled.output_price,
        "supports_vision": filled.supports_vision,
    }


def test_with_no_catalogue_on_disk_nothing_is_known(tmp_path: Path) -> None:
    knowledge = models_dev_knowledge("novita", "acme/bucketed", tmp_path / "none.json")
    assert knowledge.statements == {}
    assert knowledge.rows == ()
    assert knowledge.fetched_at is None
    assert knowledge.discovery_fill == {}


def test_asking_never_writes_an_approximate_match_line(monkeypatch) -> None:
    """The vote is read off its index directly, never through the logged walk."""

    def refuse(*_args: Any) -> None:
        raise AssertionError("models_dev_knowledge logged an approximate match")

    monkeypatch.setattr(models_dev, "_log_cross_provider_match", refuse)
    models_dev_knowledge("novita", "acme/bucketed")
    models_dev_knowledge("nous_portal", "acme/voted")
