"""Tests for full capability extraction from provider ``/models`` payloads.

The Nous Portal payload below is the live 2026-08 response for
``tencent/hy3:free`` from ``inference-api.nousresearch.com/v1/models``, copied
verbatim. It is the shape the gateway actually serves, and everything MCC used
to discard from it is asserted here.
"""

import pytest

from my_claude_code.application.model_metadata import ProviderModelInfo
from my_claude_code.core.reasoning import ReasoningEffort
from my_claude_code.providers.commandcode import extract_commandcode_model_infos
from my_claude_code.providers.model_listing import (
    extract_openrouter_tool_model_infos,
    extract_tool_capable_model_infos,
)

_NOUS_HY3 = {
    "id": "tencent/hy3:free",
    "context_length": 262144,
    "top_provider": {"context_length": 262144, "max_completion_tokens": 128000},
    "supported_parameters": [
        "frequency_penalty",
        "include_reasoning",
        "logit_bias",
        "max_completion_tokens",
        "max_tokens",
        "min_p",
        "presence_penalty",
        "reasoning",
        "reasoning_effort",
        "repetition_penalty",
        "response_format",
        "seed",
        "stop",
        "structured_outputs",
        "temperature",
        "tool_choice",
        "tools",
        "top_k",
        "top_p",
    ],
    "reasoning": {
        "mandatory": False,
        "default_enabled": True,
        "supported_efforts": ["high", "low", "none"],
    },
    "default_parameters": {"temperature": 1, "top_p": 0.95},
}


def _one(payload: object) -> ProviderModelInfo:
    infos = extract_openrouter_tool_model_infos(payload, provider_name="NOUS_PORTAL")
    assert len(infos) == 1
    return next(iter(infos))


def test_full_payload_keeps_every_published_capability() -> None:
    info = _one({"data": [_NOUS_HY3]})

    assert info.model_id == "tencent/hy3:free"
    assert info.max_output_tokens == 128000
    assert info.context_length == 262144
    assert info.supports_thinking is True
    # The whole list, not just the reasoning boolean distilled out of it.
    assert info.supported_parameters is not None
    assert len(info.supported_parameters) == 19
    assert "reasoning_effort" in info.supported_parameters
    assert "structured_outputs" in info.supported_parameters


def test_default_parameters_carry_the_provider_pinned_values() -> None:
    """The class of pin that produced a live ``400 top_p is immutable``."""
    info = _one({"data": [_NOUS_HY3]})

    assert info.default_parameters == (("temperature", 1), ("top_p", 0.95))


def test_reasoning_block_maps_onto_the_neutral_capability() -> None:
    info = _one({"data": [_NOUS_HY3]})
    capability = info.reasoning_capability

    assert capability is not None
    assert capability.can_reason is True
    assert capability.mandatory is False
    assert capability.default_enabled is True
    assert capability.supports_effort_control is True
    # "none" is not an effort level, so it never becomes a ReasoningEffort.
    assert capability.supported_efforts == frozenset(
        {ReasoningEffort.LOW, ReasoningEffort.HIGH}
    )
    # It is read as the gateway saying thinking can be switched off instead.
    assert capability.supports_toggle_control is True


def test_none_is_the_only_effort_published() -> None:
    """A vocabulary of just "none" is a toggle, never an effort vocabulary."""
    info = _one(
        {
            "data": [
                {
                    "id": "vendor/toggle-only",
                    "supported_parameters": ["tools", "reasoning"],
                    "reasoning": {"supported_efforts": ["none"]},
                }
            ]
        }
    )
    capability = info.reasoning_capability

    assert capability is not None
    assert capability.supports_toggle_control is True
    assert capability.supports_effort_control is False
    assert capability.supported_efforts == frozenset()


def test_mandatory_true_means_thinking_cannot_be_switched_off() -> None:
    info = _one(
        {
            "data": [
                {
                    "id": "vendor/always-thinks",
                    "supported_parameters": ["tools", "reasoning"],
                    "reasoning": {"mandatory": True},
                }
            ]
        }
    )
    capability = info.reasoning_capability

    assert capability is not None
    assert capability.mandatory is True
    assert capability.supports_toggle_control is False
    # No vocabulary was published: unknown, not an empty one.
    assert capability.supported_efforts is None
    assert capability.supports_effort_control is None


def test_thin_payload_yields_unknown_not_false() -> None:
    """A gateway that publishes almost nothing must not look like a denial."""
    info = _one(
        {
            "data": [
                {
                    "id": "thin/model",
                    "context_length": 131072,
                    "supported_parameters": ["tools"],
                    "top_provider": None,
                    "reasoning": None,
                    "default_parameters": None,
                }
            ]
        }
    )

    assert info.context_length == 131072
    assert info.max_output_tokens is None
    assert info.reasoning_capability is None
    assert info.default_parameters is None
    # ``supported_parameters`` WAS published and lacks "reasoning", so this one
    # really is a known False rather than an unknown.
    assert info.supports_thinking is False


def test_zero_limits_read_as_unreported() -> None:
    info = _one(
        {
            "data": [
                {
                    "id": "zero/model",
                    "context_length": 0,
                    "supported_parameters": ["tools"],
                    "top_provider": {"context_length": 0, "max_completion_tokens": 0},
                }
            ]
        }
    )

    assert info.context_length is None
    assert info.max_output_tokens is None


def test_top_provider_context_wins_over_the_nominal_one() -> None:
    info = _one(
        {
            "data": [
                {
                    "id": "routed/model",
                    "context_length": 1000000,
                    "supported_parameters": ["tools"],
                    "top_provider": {"context_length": 131072},
                }
            ]
        }
    )

    assert info.context_length == 131072


def test_non_scalar_default_parameters_are_dropped_not_encoded() -> None:
    info = _one(
        {
            "data": [
                {
                    "id": "vendor/arrays",
                    "supported_parameters": ["tools"],
                    "default_parameters": {"stop": ["\\n"], "temperature": 0.7},
                }
            ]
        }
    )

    assert info.default_parameters == (("temperature", 0.7),)


def test_empty_default_parameters_object_is_a_statement_not_a_gap() -> None:
    info = _one(
        {
            "data": [
                {
                    "id": "vendor/no-pins",
                    "supported_parameters": ["tools"],
                    "default_parameters": {},
                }
            ]
        }
    )

    assert info.default_parameters == ()


def test_generic_tool_capable_extractor_reads_the_same_fields() -> None:
    """The non-OpenRouter extractor shares one record builder, minus vision."""
    infos = extract_tool_capable_model_infos(
        {"data": [_NOUS_HY3]}, provider_name="GENERIC"
    )
    info = next(iter(infos))

    assert info.max_output_tokens == 128000
    assert info.supported_parameters is not None
    assert info.reasoning_capability is not None
    # Vision is deliberately not read here: the generic dialect has no
    # ``architecture`` block, so claiming anything would be an invention.
    assert info.supports_vision is None


@pytest.mark.parametrize("payload_key", ["reasoning", "default_parameters"])
def test_malformed_blocks_are_unknown_rather_than_fatal(payload_key: str) -> None:
    info = _one(
        {
            "data": [
                {
                    "id": "vendor/malformed",
                    "supported_parameters": ["tools"],
                    payload_key: "not-an-object",
                }
            ]
        }
    )

    assert info.reasoning_capability is None
    assert info.default_parameters is None


def test_command_code_thin_catalog_reports_nothing_it_does_not_know() -> None:
    """Command Code publishes ``context_length`` and nothing else."""
    infos = extract_commandcode_model_infos(
        {"data": [{"id": "claude-sonnet-4-5", "context_length": 200000}]},
        provider_name="COMMANDCODE",
    )
    info = next(iter(infos))

    assert info.context_length == 200000
    assert info.max_output_tokens is None
    assert info.supported_parameters is None
    assert info.default_parameters is None
    assert info.reasoning_capability is None
    assert info.supports_thinking is None
