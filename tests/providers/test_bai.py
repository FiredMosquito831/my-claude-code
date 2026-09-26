"""Tests for the B.AI provider: a generic OpenAI-chat gateway, nothing bespoke.

The contract this file pins is parity. Before B.AI was built in, the way to
use it was a custom provider on https://api.b.ai/v1 (``custom_b_ai`` on the
author's machine: about 70,000 requests, 2026-09-01 to 09-16). The built-in
must put the same bytes on the wire that such a custom provider puts there
today, for every reasoning intent, so choosing one over the other changes
nothing but the name in log lines.
"""

import dataclasses
import json
from pathlib import Path

import pytest

from my_claude_code.config.provider_catalog import (
    BAI_DEFAULT_BASE,
    PROVIDER_CATALOG,
    ProviderDescriptor,
)
from my_claude_code.config.provider_registry import (
    CustomProviderEntry,
    ProviderRegistry,
)
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.reasoning import ReasoningEffort, ReasoningPolicy
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.model_listing import extract_openai_model_infos
from my_claude_code.providers.openai_chat import (
    GENERIC_OPENAI_PROFILE,
    OPENAI_CHAT_PROFILES,
    OpenAIChatProvider,
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


def test_default_base_url_constant():
    assert BAI_DEFAULT_BASE == "https://api.b.ai/v1"


def test_catalog_entry_is_an_overridable_gateway():
    descriptor = PROVIDER_CATALOG["bai"]

    assert descriptor.display_name == "B.AI"
    assert descriptor.credential_env == "BAI_API_KEY"
    assert descriptor.credential_attr == "bai_api_key"
    assert descriptor.credential_url == "https://chat.b.ai/key"
    assert descriptor.default_base_url == BAI_DEFAULT_BASE
    assert descriptor.base_url_attr == "bai_base_url"
    assert descriptor.proxy_attr == "bai_proxy"
    assert descriptor.group == "gateway"
    assert descriptor.dynamic is False
    assert descriptor.response_surfaces == ()


def test_settings_fields_carry_the_documented_names():
    fields = Settings.model_fields

    assert fields["bai_api_key"].validation_alias == "BAI_API_KEY"
    assert fields["bai_base_url"].validation_alias == "BAI_BASE_URL"
    assert fields["bai_base_url"].default == BAI_DEFAULT_BASE
    assert fields["bai_proxy"].validation_alias == "BAI_PROXY"


def test_the_profile_is_the_generic_one_under_its_own_name():
    """Field for field the custom-provider profile; only the log name differs."""
    bai = OPENAI_CHAT_PROFILES["bai"]

    assert bai.request_policy.provider_name == "BAI"
    renamed = dataclasses.replace(
        bai,
        request_policy=dataclasses.replace(
            bai.request_policy,
            provider_name=GENERIC_OPENAI_PROFILE.request_policy.provider_name,
        ),
    )
    assert renamed == GENERIC_OPENAI_PROFILE


def test_no_models_dev_alias():
    """models.dev has no B.AI bucket, so there is nothing to alias onto."""
    assert "bai" not in PROVIDER_ID_ALIASES


def test_models_dev_answers_bai_exactly_as_it_answers_the_custom_provider(
    tmp_path: Path,
):
    """No bucket either way, so both read the same cross-provider rung."""
    path = tmp_path / "models-dev.json"
    write_models_dev_cache(
        {
            "zhipuai": {
                "models": {
                    "glm-5.3-flash": {
                        "reasoning": True,
                        "limit": {"context": 200000, "output": 131072},
                    }
                }
            }
        },
        path,
    )

    ours = model_reasoning_capability_from_models_dev("bai", "glm-5.3-flash", path)
    theirs = model_reasoning_capability_from_models_dev(
        "custom_b_ai", "glm-5.3-flash", path
    )

    assert ours == theirs


# ---------------------------------------------------------------------------
# Byte identity with a custom provider on the same host.
# ---------------------------------------------------------------------------

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
    return _create_leaf_provider(
        descriptor,
        ProviderConfig(
            api_key="test-bai-key",
            base_url=BAI_DEFAULT_BASE,
            rate_limit=10,
            rate_window=60,
        ),
        Settings.model_validate({}),
    )


@pytest.fixture
def parity_pair(tmp_path, monkeypatch):
    builtin = _leaf(PROVIDER_CATALOG["bai"], tmp_path, monkeypatch)
    # The custom provider as a stored entry looks today: Chat Completions,
    # and no learned effort list (its 2026-09-22 probe measured nothing).
    custom = _leaf(
        ProviderRegistry.descriptor_for(
            CustomProviderEntry(
                provider_id="custom_b_ai",
                display_name="B-AI",
                base_url=BAI_DEFAULT_BASE,
                api_keys=("test-bai-key",),
                surfaces=("chat_completions",),
            )
        ),
        tmp_path,
        monkeypatch,
    )
    return builtin, custom


def _rich_request(**overrides) -> MessagesRequest:
    payload = {
        "model": "glm-5.3-flash",
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
    payload.update(overrides)
    return MessagesRequest.model_validate(payload)


def _wire(provider, request: MessagesRequest, reasoning: ReasoningPolicy) -> bytes:
    body = provider._build_request_body(request, reasoning=reasoning)
    return json.dumps(body, separators=(",", ":")).encode()


@pytest.mark.parametrize("reasoning", PARITY_POLICIES)
def test_builtin_body_is_byte_identical_to_the_custom_provider(
    parity_pair, reasoning: ReasoningPolicy
):
    builtin, custom = parity_pair
    request = _rich_request()

    assert _wire(builtin, request, reasoning) == _wire(custom, request, reasoning)


@pytest.mark.parametrize("reasoning", PARITY_POLICIES)
def test_no_default_output_cap_either_way(parity_pair, reasoning: ReasoningPolicy):
    """HyperCharm fills a missing ``max_tokens``; the generic profile does not."""
    builtin, custom = parity_pair
    request = MessagesRequest.model_validate(
        {"model": "glm-5.3-flash", "messages": [{"role": "user", "content": "hi"}]}
    )

    ours = builtin._build_request_body(request, reasoning=reasoning)

    assert "max_tokens" not in ours
    assert _wire(builtin, request, reasoning) == _wire(custom, request, reasoning)


def test_max_clamps_to_high_like_the_generic_profile(parity_pair):
    builtin, _custom = parity_pair
    body = builtin._build_request_body(
        _rich_request(), reasoning=ReasoningPolicy.on(effort=ReasoningEffort.MAX)
    )

    assert body["reasoning_effort"] == "high"


def test_off_sends_no_reasoning_key(parity_pair):
    builtin, _custom = parity_pair
    body = builtin._build_request_body(_rich_request(), reasoning=ReasoningPolicy.off())

    assert "reasoning_effort" not in body


def test_prior_thinking_is_replayed_as_think_tags(parity_pair):
    builtin, _custom = parity_pair
    body = builtin._build_request_body(
        _rich_request(), reasoning=ReasoningPolicy.provider_default()
    )

    assistant = body["messages"][2]
    assert assistant["role"] == "assistant"
    assert "<think>\nLook at the file first.\n</think>" in assistant["content"]


def test_api_key_and_base_url_reach_the_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    settings = Settings.model_validate(
        {"BAI_API_KEY": "test-bai-key", "BAI_BASE_URL": "https://staging.example/v1"}
    )

    provider = create_provider("bai", settings)

    assert isinstance(provider, OpenAIChatProvider)
    assert provider._base_url == "https://staging.example/v1"
    assert provider._provider_name == "BAI"


# ---------------------------------------------------------------------------
# /v1/models. The envelope is the one docs.b.ai documents, and the row shape
# (id, object, created, owned_by, supported_endpoint_types) is what a direct
# probe of the host returned in September 2026; the values here are
# illustrative -- the endpoint-type strings were never recorded. The stock listing reads ids and nothing else, as it does for the
# custom provider.
# ---------------------------------------------------------------------------

_MODELS_PAYLOAD = {
    "object": "list",
    "success": True,
    "data": [
        {
            "id": "glm-5.3-flash",
            "object": "model",
            "created": 1780000000,
            "owned_by": "zhipu",
            "supported_endpoint_types": ["openai"],
        },
        {
            "id": "qwen3.8-flash",
            "object": "model",
            "created": 1780000000,
            "owned_by": "qwen",
            "supported_endpoint_types": ["openai"],
        },
        {
            "id": "claude-sonnet-4-5",
            "object": "model",
            "created": 1780000000,
            "owned_by": "anthropic",
            "supported_endpoint_types": ["anthropic", "openai"],
        },
    ],
}


def test_listing_is_the_stock_one():
    assert (
        OPENAI_CHAT_PROFILES["bai"].model_listing
        == GENERIC_OPENAI_PROFILE.model_listing
    )


def test_generic_extractor_reads_every_id():
    infos = extract_openai_model_infos(_MODELS_PAYLOAD, provider_name="BAI")

    assert {info.model_id for info in infos} == {
        "glm-5.3-flash",
        "qwen3.8-flash",
        "claude-sonnet-4-5",
    }
    for info in infos:
        assert info.input_price is None
        assert info.output_price is None
