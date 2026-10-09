"""Each list's own numbers and flags are rung 1 of the record (7.83.0, PR-K4).

The user's decision (2026-10-08 20:57-21:00): provider official first, then gap
filling down the whole ladder, field by field; a new source is an added rung,
never a replacement. So the generic reader every listing parser runs also keeps
the row's own context window, output limit, two listed prices, reasoning and
tool-call support, and ``record_with_declared`` puts them on the record wherever
the parser's own dialect left the field unset. models.dev only fills what is
still unset after that, exactly as it always has.

The rows are trimmed from the real keyless listings of 2026-10-08.
"""

from dataclasses import replace
from typing import Any

import pytest

from my_claude_code.application.model_metadata import (
    ProviderModelDeclaration,
    ProviderModelInfo,
)
from my_claude_code.providers.anthropic.models import extract_anthropic_model_infos
from my_claude_code.providers.commandcode.models import extract_commandcode_model_infos
from my_claude_code.providers.model_listing import (
    declared_from_row,
    extract_openai_model_infos,
    extract_openrouter_tool_model_infos,
    published_parameters_from_row,
    record_with_declared,
)
from my_claude_code.providers.runtime.models_dev import enrich_model_infos

NOVITA_ROW: dict[str, Any] = {
    "id": "zai-org/glm-5.3-flash",
    "input_token_price_per_m": 1500,
    "output_token_price_per_m": 5000,
    "pricing": {
        "prompt": {"price_per_m": 1500, "price_per_m_decimal": "0.15"},
        "completion": {"price_per_m": 5000, "price_per_m_decimal": "0.5"},
        "input_cache_read": {"price_per_m": 300, "price_per_m_decimal": "0.03"},
    },
    "context_size": 1048576,
    "model_type": "chat",
    "max_output_tokens": 131072,
    "features": ["function-calling", "structured-outputs", "reasoning", "serverless"],
    "endpoints": ["chat/completions", "anthropic", "responses"],
    "input_modalities": ["text", "image", "video"],
    "output_modalities": ["text"],
}
#: Novita bills this one by tier: no ``pricing`` object, and ``0`` in the
#: integer field that is NOT read -- "0" there is not a free price.
NOVITA_TIERED_ROW: dict[str, Any] = {
    "id": "qwen/qwen3.6-plus",
    "input_token_price_per_m": 0,
    "output_token_price_per_m": 0,
    "is_tiered_billing": True,
    "context_size": 1000000,
    "model_type": "chat",
    "max_output_tokens": 65536,
    "features": ["function-calling", "reasoning"],
}
#: Novita states ``reasoning`` and no ``function-calling``: a missing word is
#: not a "no".
NOVITA_EURYALE_ROW: dict[str, Any] = {
    "id": "sao10k/l31-70b-euryale-v2.2",
    "pricing": {
        "prompt": {"price_per_m_decimal": "1.48"},
        "completion": {"price_per_m_decimal": "1.48"},
    },
    "context_size": 8192,
    "max_output_tokens": 8192,
    "features": ["structured-outputs", "reasoning", "serverless"],
}
HYPERCHARM_ROW: dict[str, Any] = {
    "id": "deepseek-v4.1-flash",
    "created": 0,
    "context_window": 1048576,
    "max_output_tokens": 262144,
    "capabilities": {"vision": True},
    "reasoning": {
        "effort_levels": [{"value": "low"}, {"value": "high"}, {"value": "xhigh"}],
        "default_effort_level": "high",
    },
    "pricing": {"input": 0.33, "output": 1.31, "cache_create": 0, "cache_hit": 0.03},
}
VERCEL_ROW: dict[str, Any] = {
    "id": "alibaba/qwen-3-14b",
    "context_window": 40960,
    "max_tokens": 16384,
    "type": "language",
    "tags": ["reasoning", "tool-use", "structured-output"],
    "modalities": {"input": ["text"], "output": ["text"]},
    "supported_parameters": ["max_tokens", "tools", "tool_choice", "reasoning"],
    "reasoning_options": [{"type": "toggle"}],
    "pricing": {"input": "0.00000012", "output": "0.00000024"},
}
OPENROUTER_ROW: dict[str, Any] = {
    "id": "stepfun/step-5-preview",
    "context_length": 1000000,
    "pricing": {
        "prompt": "0.000001",
        "completion": "0.0000027",
        "input_cache_read": "0.00000005",
    },
    "top_provider": {"context_length": 1000000, "max_completion_tokens": 64000},
    "supported_parameters": ["reasoning", "reasoning_effort", "tools"],
    "reasoning": {"mandatory": True, "supported_efforts": ["high", "medium", "low"]},
}
#: OpenRouter publishes ``{"mandatory": false}`` alone for a model that does not
#: reason, and no ``reasoning`` parameter; the dialect's own ``False`` stands.
OPENROUTER_NON_REASONING_ROW: dict[str, Any] = {
    "id": "qwen/qwen3-max",
    "context_length": 262144,
    "pricing": {"prompt": "0.0000012", "completion": "0.000006"},
    "top_provider": {"context_length": 262144, "max_completion_tokens": 32768},
    "supported_parameters": ["tools", "tool_choice", "max_tokens"],
    "reasoning": {"mandatory": False},
}
#: Kilo's auto-router lists ``"-1"``: its price depends on the model it picks.
KILO_AUTO_ROW: dict[str, Any] = {
    "id": "kilo-auto/efficient",
    "context_length": 1000000,
    "top_provider": {"context_length": 1000000, "max_completion_tokens": 65536},
    "pricing": {"prompt": "-1", "completion": "-1", "request": "0"},
    "supported_parameters": ["max_tokens", "tools", "reasoning"],
}


def _one(infos: frozenset[ProviderModelInfo]) -> ProviderModelInfo:
    (info,) = infos
    return info


def _generic(row: dict[str, Any]) -> ProviderModelInfo:
    return _one(extract_openai_model_infos({"data": [row]}, provider_name="G"))


def test_novita_states_its_limits_prices_and_flags_and_they_fill_the_record() -> None:
    info = _generic(NOVITA_ROW)

    declared = info.declared
    assert declared is not None
    assert (declared.context_length, declared.max_output_tokens) == (1048576, 131072)
    assert (declared.input_price, declared.output_price) == (0.15, 0.5)
    assert (declared.reasoning, declared.tool_calls) == (True, True)
    # Rung 1 of the record: the same values, in the fields every lookup reads.
    assert info.context_length == 1048576
    assert info.max_output_tokens == 131072
    assert (info.input_price, info.output_price) == (0.15, 0.5)
    assert info.supports_thinking is True
    # A generic row that publishes no parameter list keeps saying so.
    assert info.supported_parameters is None


def test_a_novita_row_billed_by_tier_states_no_price() -> None:
    info = _generic(NOVITA_TIERED_ROW)

    assert info.input_price is None and info.output_price is None
    assert info.context_length == 1000000
    assert info.max_output_tokens == 65536


def test_a_capability_word_list_never_states_no() -> None:
    declared = declared_from_row(NOVITA_EURYALE_ROW)

    assert declared is not None
    assert declared.reasoning is True
    # ``function-calling`` is not in the list: that is silence, not a denial.
    assert declared.tool_calls is None


def test_hypercharm_numbers_are_per_million_and_its_effort_list_says_it_reasons() -> (
    None
):
    info = _generic(HYPERCHARM_ROW)

    assert (info.input_price, info.output_price) == (0.33, 1.31)
    assert (info.context_length, info.max_output_tokens) == (1048576, 262144)
    assert info.supports_thinking is True
    # Vision is not one of the fields 7.83.0 reads.
    assert info.supports_vision is None


def test_vercel_strings_are_per_token_and_its_parameter_list_is_kept() -> None:
    info = _generic(VERCEL_ROW)

    assert (info.input_price, info.output_price) == (0.12, 0.24)
    assert (info.context_length, info.max_output_tokens) == (40960, 16384)
    assert info.supported_parameters == frozenset(
        {"max_tokens", "tools", "tool_choice", "reasoning"}
    )
    assert info.supports_thinking is True
    assert info.declared is not None and info.declared.tool_calls is True


def test_the_openrouter_dialect_gains_only_its_two_listed_prices() -> None:
    info = _one(
        extract_openrouter_tool_model_infos(
            {"data": [OPENROUTER_ROW]}, provider_name="O"
        )
    )

    # Exactly, not 2.6999999999999997: the decimal string is converted as one.
    assert (info.input_price, info.output_price) == (1.0, 2.7)
    without_prices = replace(info, input_price=None, output_price=None)
    # Everything else is what the dialect has always read off this row.
    assert without_prices.context_length == 1000000
    assert without_prices.max_output_tokens == 64000
    assert without_prices.supports_thinking is True
    assert without_prices.reasoning_capability is not None


def test_the_dialects_own_reading_is_never_replaced() -> None:
    info = _one(
        extract_openrouter_tool_model_infos(
            {"data": [OPENROUTER_NON_REASONING_ROW]}, provider_name="O"
        )
    )

    # ``{"mandatory": false}`` alone states nothing, and the dialect's own
    # ``False`` (no ``reasoning`` parameter) stands.
    assert info.declared is not None and info.declared.reasoning is None
    assert info.supports_thinking is False
    assert (info.input_price, info.output_price) == (1.2, 6.0)


def test_record_with_declared_fills_only_what_the_parser_left_unset() -> None:
    declared = ProviderModelDeclaration(
        context_length=100, max_output_tokens=10, input_price=1.0, reasoning=True
    )
    own = ProviderModelInfo(
        "m", supports_thinking=False, context_length=200, declared=declared
    )

    filled = record_with_declared(own)

    assert filled.context_length == 200
    assert filled.supports_thinking is False
    assert filled.max_output_tokens == 10
    assert filled.input_price == 1.0
    assert filled.output_price is None
    unchanged = ProviderModelInfo(
        "m", declared=ProviderModelDeclaration(model_type="x")
    )
    assert record_with_declared(unchanged) is unchanged


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0.0000027", 2.7),
        ("0.00000015", 0.15),
        ("0", 0.0),
        ("-1", None),
        ("", None),
        ("free", None),
        ("NaN", None),
        ("Infinity", None),
        ("1e999", None),
        (True, None),
        ([], None),
    ],
)
def test_a_listed_string_price_is_usd_per_token(value: Any, expected: Any) -> None:
    declared = declared_from_row({"pricing": {"prompt": value}})
    assert (None if declared is None else declared.input_price) == expected


def test_a_negative_price_states_nothing_and_zero_is_a_free_price() -> None:
    info = _one(
        extract_openrouter_tool_model_infos(
            {"data": [KILO_AUTO_ROW]}, provider_name="K"
        )
    )
    assert (info.input_price, info.output_price) == (None, None)
    free = declared_from_row({"pricing": {"prompt": "0", "completion": 0}})
    assert free is not None
    assert (free.input_price, free.output_price) == (0.0, 0.0)


def test_explicit_booleans_state_either_answer_and_non_reasoning_says_no() -> None:
    explicit = declared_from_row(
        {"capabilities": {"reasoning": False, "function_calling": False}}
    )
    assert explicit is not None
    assert (explicit.reasoning, explicit.tool_calls) == (False, False)
    tagged = declared_from_row({"tags": ["non-reasoning"]})
    assert tagged is not None and tagged.reasoning is False


@pytest.mark.parametrize(
    "row",
    [
        {"context_length": 0, "max_output_tokens": -5},
        {"context_window": "1M", "max_tokens": 1.5},
        {"context_size": True, "max_completion_tokens": None},
        {"pricing": {"prompt": {"price_per_m_decimal": "x"}}},
        {"pricing": "0.15"},
        {"features": "reasoning", "tags": [None]},
        {"reasoning": {"mandatory": False}, "reasoning_options": []},
    ],
)
def test_an_unreadable_number_or_flag_states_nothing(row: dict[str, Any]) -> None:
    assert declared_from_row({"id": "x", **row}) is None


def test_models_dev_fills_only_what_the_provider_left_unset() -> None:
    """Rung 1, then the discovery-time models.dev fill, field by field."""

    index = {
        "novita": {
            "models": {
                "zai-org/glm-5.3-flash": {
                    "limit": {"context": 200000, "output": 8000},
                    "cost": {"input": 9.0, "output": 9.0},
                    "modalities": {"input": ["text"], "output": ["text"]},
                },
                "qwen/qwen3.6-plus": {
                    "limit": {"context": 131072},
                    "cost": {"input": 0.4, "output": 1.2},
                },
            }
        }
    }
    infos = sorted(
        extract_openai_model_infos(
            {"data": [NOVITA_ROW, NOVITA_TIERED_ROW]}, provider_name="N"
        ),
        key=lambda info: info.model_id,
    )

    tiered, glm = enrich_model_infos(infos, index, "novita")

    # The provider stated all four: models.dev's different numbers do not move them.
    assert (glm.context_length, glm.input_price, glm.output_price) == (
        1048576,
        0.15,
        0.5,
    )
    # The provider stated no price: models.dev fills it, exactly as before.
    assert (tiered.input_price, tiered.output_price) == (0.4, 1.2)
    assert tiered.context_length == 1000000


def test_every_parser_runs_the_same_reader() -> None:
    commandcode = _one(
        extract_commandcode_model_infos(
            {
                "data": [
                    {
                        "id": "gpt-6-astra",
                        "context_length": 1050000,
                        "max_output_tokens": 128000,
                        "supported_endpoints": ["/chat/completions"],
                    }
                ]
            },
            provider_name="CC",
        )
    )
    assert commandcode.context_length == 1050000
    assert commandcode.max_output_tokens == 128000

    anthropic = _one(
        extract_anthropic_model_infos(
            {
                "data": [
                    {
                        "type": "model",
                        "id": "claude-opus-5-5",
                        "max_input_tokens": 1000000,
                        "max_tokens": 128000,
                    }
                ]
            },
            provider_name="A",
        )
    )
    # ``max_input_tokens`` is not a context window and is not read as one.
    assert anthropic.context_length is None
    assert anthropic.max_output_tokens == 128000


def test_an_alias_carries_its_rows_numbers() -> None:
    infos = {
        info.model_id: info
        for info in extract_openai_model_infos(
            {
                "models": [
                    {
                        "id": "grok-5",
                        "aliases": ["grok-5-latest"],
                        "context_length": 256000,
                        "pricing": {"input": 3, "output": 15},
                    }
                ]
            },
            provider_name="X",
            collection_field="models",
            aliases_field="aliases",
        )
    }
    assert infos["grok-5-latest"].context_length == 256000
    assert (infos["grok-5-latest"].input_price, infos["grok-5"].output_price) == (
        3.0,
        15.0,
    )


def test_published_parameters_read_like_the_dialect() -> None:
    assert published_parameters_from_row({"supported_parameters": ["tools", 7]}) == (
        frozenset({"tools"})
    )
    assert published_parameters_from_row({"supported_parameters": "tools"}) is None
    assert published_parameters_from_row({}) is None
