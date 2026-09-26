"""Tests for the Agnes AI OpenAI-chat provider profile."""

import json
from typing import Any

import pytest

from my_claude_code.config.constants import ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS
from my_claude_code.config.provider_catalog import (
    AGNES_DEFAULT_BASE,
    PROVIDER_CATALOG,
    ProviderDescriptor,
)
from my_claude_code.config.provider_registry import (
    CustomProviderEntry,
    ProviderRegistry,
)
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.reasoning import (
    ReasoningDialectOrigin,
    ReasoningEffort,
    ReasoningPolicy,
)
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.openai_chat import (
    OPENAI_CHAT_PROFILES,
    NamedEffortReasoning,
    OpenAIChatProvider,
)
from my_claude_code.providers.openai_chat.learned_dialect import (
    learned_effort_values,
    learned_named_effort_reasoning,
)
from my_claude_code.providers.runtime.factory import (
    _create_leaf_provider,
    create_provider,
)
from my_claude_code.providers.runtime.models_dev import (
    PROVIDER_ID_ALIASES,
    model_reasoning_capability_from_models_dev,
    write_models_dev_cache,
)
from tests.providers.support import (
    REASONING_DEFAULT,
    REASONING_OFF,
    REASONING_ON,
    passthrough_rate_limiter,
    profiled_provider,
    reasoning_for,
)


@pytest.fixture
def agnes_provider():
    return profiled_provider(
        "agnes",
        ProviderConfig(
            api_key="test-agnes-key",
            base_url=AGNES_DEFAULT_BASE,
            rate_limit=10,
            rate_window=60,
        ),
        rate_limiter=passthrough_rate_limiter(),
    )


def _request(**overrides: Any) -> MessagesRequest:
    payload: dict[str, Any] = {
        "model": "agnes-2.0-flash",
        "messages": [{"role": "user", "content": "Hello"}],
    }
    payload.update(overrides)
    return MessagesRequest.model_validate(payload)


def test_init_uses_documented_endpoint(agnes_provider):
    assert agnes_provider._api_key == "test-agnes-key"
    assert agnes_provider._base_url == AGNES_DEFAULT_BASE
    assert agnes_provider._provider_name == "AGNES"


def test_build_request_body_sends_the_hosts_effort_word_for_max(agnes_provider):
    body = agnes_provider._build_request_body(
        _request(), reasoning=ReasoningPolicy.on(effort=ReasoningEffort.MAX)
    )

    assert body["reasoning_effort"] == "max"
    assert "extra_body" not in body


def test_build_request_body_spells_off_as_none(agnes_provider):
    body = agnes_provider._build_request_body(_request(), reasoning=REASONING_OFF)

    assert body["reasoning_effort"] == "none"
    assert "extra_body" not in body


def test_build_request_body_never_sends_the_chat_template_flag(agnes_provider):
    """The flag the page documents is not what this host was measured reading."""
    for reasoning in (REASONING_ON, REASONING_OFF, REASONING_DEFAULT):
        body = agnes_provider._build_request_body(_request(), reasoning=reasoning)
        assert "chat_template_kwargs" not in body
        assert "chat_template_kwargs" not in body.get("extra_body", {})


def test_build_request_body_omits_thinking_control_for_provider_default(agnes_provider):
    body = agnes_provider._build_request_body(
        _request(),
        reasoning=ReasoningPolicy.provider_default(),
    )

    assert "extra_body" not in body
    assert "reasoning_effort" not in body


def test_declared_table_is_the_one_a_custom_provider_learns_from_the_same_host():
    """Rung for rung, what ``custom_agnes`` learned on 2026-09-22."""
    declared = OPENAI_CHAT_PROFILES["agnes"].reasoning
    learned = learned_named_effort_reasoning(AGNES_LEARNED_WORDS)

    assert isinstance(declared, NamedEffortReasoning)
    assert learned is not None
    assert declared.efforts == learned.efforts
    assert declared.efforts == learned_effort_values(("low", "medium", "high", "max"))
    assert declared.disabled_value == learned.disabled_value == "none"
    assert declared.enabled_value == learned.enabled_value
    assert declared.field == learned.field == "reasoning_effort"
    assert declared.budget_field == learned.budget_field
    assert declared.use_extra_body == learned.use_extra_body
    # Provenance is the one thing that differs, and it is display-only: the
    # Models page says "declared by this provider" instead of "learned".
    assert declared.origin is ReasoningDialectOrigin.DECLARED
    assert learned.origin is ReasoningDialectOrigin.LEARNED


# ---------------------------------------------------------------------------
# Byte identity with the custom provider the user runs on the same host.
#
# ``custom_agnes`` is a custom provider on https://apihub.agnes-ai.com/v1 whose
# probe learned ``none|low|medium|high|max``. Both providers below are built by
# the real factory from their real descriptors, so this compares what each one
# would put on the wire, serialised, for the same request and the same
# reasoning intent.
# ---------------------------------------------------------------------------

AGNES_LEARNED_WORDS = ("none", "low", "medium", "high", "max")

PARITY_POLICIES = [
    pytest.param(ReasoningPolicy.provider_default(), id="default"),
    pytest.param(ReasoningPolicy.off(), id="off"),
    pytest.param(ReasoningPolicy.on(), id="on"),
    pytest.param(ReasoningPolicy.adaptive(), id="adaptive"),
    pytest.param(ReasoningPolicy.on(budget_tokens=2048), id="budget"),
    *(
        pytest.param(ReasoningPolicy.on(effort=effort), id=f"effort-{effort.value}")
        for effort in ReasoningEffort
    ),
]


def _leaf(descriptor: ProviderDescriptor, tmp_path, monkeypatch):
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    settings = Settings.model_validate({})
    return _create_leaf_provider(
        descriptor,
        ProviderConfig(
            api_key="test-agnes-key",
            base_url=AGNES_DEFAULT_BASE,
            rate_limit=10,
            rate_window=60,
        ),
        settings,
    )


@pytest.fixture
def parity_pair(tmp_path, monkeypatch):
    builtin = _leaf(PROVIDER_CATALOG["agnes"], tmp_path, monkeypatch)
    custom = _leaf(
        ProviderRegistry.descriptor_for(
            CustomProviderEntry(
                provider_id="custom_agnes",
                display_name="Agnes",
                base_url=AGNES_DEFAULT_BASE,
                api_keys=("test-agnes-key",),
                reasoning_effort_enum=AGNES_LEARNED_WORDS,
                surfaces=("chat_completions",),
            )
        ),
        tmp_path,
        monkeypatch,
    )
    return builtin, custom


def _rich_request() -> MessagesRequest:
    """The shape Claude Code sends: system, tools, images, prior thinking."""
    return MessagesRequest.model_validate(
        {
            "model": "agnes-3.0-flash",
            "max_tokens": 32000,
            "system": [{"type": "text", "text": "You are a coding agent."}],
            "tools": [
                {
                    "name": "Read",
                    "description": "Read a file.",
                    "input_schema": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                }
            ],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "What is in this picture?"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": "iVBORw0KGgo=",
                            },
                        },
                    ],
                },
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "Look at the file first."},
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "Read",
                            "input": {"path": "a.py"},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_1",
                            "content": "print('hi')",
                        }
                    ],
                },
            ],
        }
    )


def _wire(provider, request: MessagesRequest, reasoning: ReasoningPolicy) -> bytes:
    body = provider._build_request_body(request, reasoning=reasoning)
    return json.dumps(body, sort_keys=False, separators=(",", ":")).encode()


@pytest.mark.parametrize("reasoning", PARITY_POLICIES)
def test_builtin_body_is_byte_identical_to_the_custom_provider(
    parity_pair, reasoning: ReasoningPolicy
):
    builtin, custom = parity_pair
    request = _rich_request()

    assert _wire(builtin, request, reasoning) == _wire(custom, request, reasoning)


def test_the_only_difference_is_the_default_output_cap_for_a_request_without_one(
    parity_pair,
):
    """An Anthropic request always carries ``max_tokens``; an OpenAI one may not.

    The built-in fills a missing one with ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS,
    as it always has; the generic custom profile sends none. Every other byte
    is the same.
    """
    builtin, custom = parity_pair
    request = _request(model="agnes-3.0-flash")
    reasoning = ReasoningPolicy.on(effort=ReasoningEffort.MAX)

    ours = builtin._build_request_body(request, reasoning=reasoning)
    theirs = custom._build_request_body(request, reasoning=reasoning)

    assert ours.pop("max_tokens") == ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS
    assert "max_tokens" not in theirs
    assert ours == theirs


def test_agnes_needs_no_models_dev_alias(tmp_path):
    """models.dev's bucket id is ``agnes`` already; no alias line is needed."""
    path = tmp_path / "models-dev.json"
    write_models_dev_cache(
        {
            "agnes": {
                "api": AGNES_DEFAULT_BASE,
                "models": {"agnes-2.5-flash": {"reasoning": True}},
            }
        },
        path,
    )

    assert "agnes" not in PROVIDER_ID_ALIASES
    capability = model_reasoning_capability_from_models_dev(
        "agnes", "agnes-2.5-flash", path
    )
    assert capability is not None
    assert capability.can_reason is True


def test_base_url_is_a_setting():
    descriptor = PROVIDER_CATALOG["agnes"]

    assert descriptor.base_url_attr == "agnes_base_url"
    field = Settings.model_fields["agnes_base_url"]
    assert field.validation_alias == "AGNES_BASE_URL"
    assert field.default == AGNES_DEFAULT_BASE


def test_base_url_setting_reaches_the_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    settings = Settings.model_validate(
        {"AGNES_API_KEY": "test-agnes-key", "AGNES_BASE_URL": "https://tp.example/v1"}
    )

    provider = create_provider("agnes", settings)

    assert isinstance(provider, OpenAIChatProvider)
    assert provider._base_url == "https://tp.example/v1"


def test_build_request_body_applies_default_max_tokens(agnes_provider):
    body = agnes_provider._build_request_body(
        _request(),
        reasoning=reasoning_for(_request()),
    )

    assert body["max_tokens"] == ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS


def test_build_request_body_replays_reasoning_in_content(agnes_provider):
    request = _request(
        messages=[
            {"role": "user", "content": "Solve it."},
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "Work through it."},
                    {"type": "text", "text": "The answer is 42."},
                ],
            },
            {"role": "user", "content": "Continue."},
        ]
    )

    body = agnes_provider._build_request_body(
        request,
        reasoning=reasoning_for(request),
    )

    assert body["messages"][1] == {
        "role": "assistant",
        "content": "<think>\nWork through it.\n</think>\n\nThe answer is 42.",
    }


def test_default_base_url_constant():
    assert AGNES_DEFAULT_BASE == "https://apihub.agnes-ai.com/v1"
