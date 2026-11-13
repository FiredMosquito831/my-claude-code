"""Every ``/v1/models`` entry carries the record the ladder resolved (8.21.0).

The snapshot contract (``tests/contracts/``) proves the payload moved in
exactly one place. It cannot prove the payload says the *right* thing: a
digest is satisfied by any bytes. This module asserts the value, per entry
class, by resolving the catalogue independently and comparing each entry's
``metadata`` to it field for field -- the OWNEDBY lesson (a grep, or a digest,
cannot see what the code emits).
"""

from typing import Any

from fastapi.testclient import TestClient

from my_claude_code.application.catalogue_model import (
    CatalogueModel,
    build_catalogue_models,
)
from my_claude_code.application.model_metadata import (
    ModelReasoningCapability,
    ProviderModelInfo,
)
from my_claude_code.config.harness_tiers import current_harness_tiers
from my_claude_code.config.settings import Settings
from my_claude_code.core.reasoning import ReasoningEffort
from my_claude_code.core.tier_refs import TIER_ORDER
from tests.api.support import create_test_app, provider_manager_for_app

#: The route the tier aliases collapse onto when their own settings are blank.
REF = "nvidia_nim/test-model"
#: A model nobody said anything about, beside the one that says everything.
UNKNOWN_REF = "nvidia_nim/quiet-model"

ALL_NULL_METADATA: dict[str, Any] = {
    "context_length": None,
    "max_output_tokens": None,
    "supports_vision": None,
    "supports_tool_calls": None,
    "reasoning": None,
    "input_price": None,
    "output_price": None,
    "cache_read_price": None,
    "cache_write_price": None,
    "supported_parameters": None,
    "default_parameters": None,
}


def _expected(record: CatalogueModel) -> dict[str, Any]:
    """The metadata a record must produce, spelled out here rather than reused.

    Written as an explicit mapping on purpose: calling the production
    conversion would make this test compare a function to itself.
    """

    reasoning = record.reasoning
    return {
        "context_length": record.context_length,
        "max_output_tokens": record.max_output_tokens,
        "supports_vision": record.supports_vision,
        "supports_tool_calls": record.supports_tool_calls,
        "reasoning": (
            None
            if reasoning is None
            else {
                "can_reason": reasoning.can_reason,
                "supports_effort_control": reasoning.supports_effort_control,
                "supports_toggle_control": reasoning.supports_toggle_control,
                "supports_budget_control": reasoning.supports_budget_control,
                "supported_efforts": (
                    None
                    if reasoning.supported_efforts is None
                    else sorted(effort.value for effort in reasoning.supported_efforts)
                ),
                "mandatory": reasoning.mandatory,
                "default_enabled": reasoning.default_enabled,
            }
        ),
        "input_price": record.input_price,
        "output_price": record.output_price,
        "cache_read_price": record.cache_read_price,
        "cache_write_price": record.cache_write_price,
        "supported_parameters": (
            None
            if record.supported_parameters is None
            else sorted(record.supported_parameters)
        ),
        "default_parameters": (
            None
            if record.default_parameters is None
            else [list(pair) for pair in record.default_parameters]
        ),
    }


def _stated_info() -> ProviderModelInfo:
    """One model with every rung-1 answer stated, including a stated zero."""

    return ProviderModelInfo(
        model_id="test-model",
        supports_thinking=True,
        supports_vision=True,
        context_length=1_000_000,
        # A provider that publishes free states 0.0. It is an answer, and it
        # must not be confused with the unknown cache rate below.
        input_price=0.0,
        output_price=15.0,
        max_output_tokens=65_536,
        supported_parameters=frozenset({"temperature", "tools", "tool_choice"}),
        default_parameters=(("temperature", 1), ("top_p", 0.95)),
        reasoning_capability=ModelReasoningCapability(
            can_reason=True,
            supports_effort_control=True,
            supports_toggle_control=False,
            supports_budget_control=None,
            supported_efforts=frozenset({ReasoningEffort.LOW, ReasoningEffort.HIGH}),
            mandatory=None,
            default_enabled=None,
        ),
    )


def _listing(monkeypatch, tmp_path) -> tuple[Settings, Any, dict[str, Any]]:
    settings = Settings.model_validate({"MODEL": REF})
    app = create_test_app(settings)
    manager = provider_manager_for_app(app)
    manager.cache_model_infos(
        "nvidia_nim", (_stated_info(), ProviderModelInfo(model_id="quiet-model"))
    )
    payload = TestClient(app, client=("127.0.0.1", 50000)).get("/v1/models").json()
    return settings, manager, {entry["id"]: entry for entry in payload["data"]}


def test_every_entry_carries_the_record_the_ladder_resolved(
    monkeypatch, tmp_path
) -> None:
    settings, manager, entries = _listing(monkeypatch, tmp_path)
    records = {
        record.gateway_id: record
        for record in build_catalogue_models(
            settings, manager, harness_tiers=current_harness_tiers()
        )
    }
    # Anti-vacuity: the fixture must resolve a populated record, or every
    # comparison below is "all nulls equal all nulls".
    stated = records[f"anthropic/{REF}"]
    assert stated.context_length == 1_000_000
    assert stated.reasoning is not None and stated.reasoning.can_reason is True

    normal = entries[f"anthropic/{REF}"]
    assert normal["metadata"] == _expected(stated)
    # A stated free price survives; the cache rates, which no provider row
    # carries, stay unknown (IV.16 in one entry).
    assert normal["metadata"]["input_price"] == 0.0
    assert normal["metadata"]["cache_read_price"] is None
    assert normal["metadata"]["cache_write_price"] is None
    assert normal["metadata"]["supported_parameters"] == [
        "temperature",
        "tool_choice",
        "tools",
    ]
    assert normal["metadata"]["default_parameters"] == [
        ["temperature", 1],
        ["top_p", 0.95],
    ]


def test_the_no_thinking_twin_carries_the_record_without_reasoning(
    monkeypatch, tmp_path
) -> None:
    settings, manager, entries = _listing(monkeypatch, tmp_path)
    records = {
        record.gateway_id: record
        for record in build_catalogue_models(
            settings, manager, harness_tiers=current_harness_tiers()
        )
    }
    twin_id = f"claude-3-freecc-no-thinking/{REF}"
    twin = records[twin_id]
    # The catalogue builds the twin with ``reasoning: None`` -- the id exists
    # precisely to run without reasoning -- so the wire says the same thing.
    assert twin.reasoning is None
    assert entries[twin_id]["metadata"] == _expected(twin)
    assert entries[twin_id]["metadata"]["reasoning"] is None
    # ... and the rest of the record is the model's, not a copy of nothing.
    assert entries[twin_id]["metadata"]["context_length"] == 1_000_000
    assert entries[twin_id]["metadata"] == {
        **entries[f"anthropic/{REF}"]["metadata"],
        "reasoning": None,
    }


def test_both_tier_spellings_carry_the_primary_record_verbatim(
    monkeypatch, tmp_path
) -> None:
    settings, manager, entries = _listing(monkeypatch, tmp_path)
    records = {
        record.gateway_id: record
        for record in build_catalogue_models(
            settings, manager, harness_tiers=current_harness_tiers()
        )
    }
    primary = entries[f"anthropic/{REF}"]["metadata"]
    for tier in TIER_ORDER:
        bare = entries[f"mcc/{tier}"]
        gateway = entries[f"anthropic/mcc/{tier}"]
        assert bare["metadata"] == _expected(records[f"anthropic/mcc/{tier}"])
        # One route, two spellings: they may not disagree with each other, nor
        # with the record of the model they point at.
        assert bare["metadata"] == gateway["metadata"]
        assert bare["metadata"] == primary


def test_the_claude_protocol_aliases_carry_no_record(monkeypatch, tmp_path) -> None:
    _, _, entries = _listing(monkeypatch, tmp_path)
    aliases = [
        entry
        for entry in entries.values()
        if entry["id"].startswith("claude-") and "freecc" not in entry["id"]
    ]
    # The ten fixed protocol names, and nothing else, are null.
    assert len(aliases) == 10
    for alias in aliases:
        assert alias["metadata"] is None, alias["id"]
    assert entries["claude-opus-4-20250514"]["metadata"] is None


def test_a_model_nobody_described_carries_unknown_never_zero(
    monkeypatch, tmp_path
) -> None:
    _, _, entries = _listing(monkeypatch, tmp_path)
    unknown = entries[f"anthropic/{UNKNOWN_REF}"]["metadata"]
    assert unknown == ALL_NULL_METADATA
