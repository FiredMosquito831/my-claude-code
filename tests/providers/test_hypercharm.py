"""Tests for the HyperCharm OpenAI-chat gateway provider profile.

``_LIVE_MODELS_PAYLOAD`` is the verbatim ``GET https://hyper.charm.land/v1/models``
response captured on 2026-09-02 with the user's own key. It holds no credential
of any kind -- the response carries only catalogue metadata -- and it is kept
whole rather than trimmed because the point of the assertions below is that the
five vendor-specific keys it publishes (``display_name``, ``context_window``,
``max_output_tokens``, ``capabilities``, ``pricing``, ``reasoning``) are
*ignored* by the generic extractor rather than mis-parsed. A trimmed fixture
could not fail that way.
"""

import json
from typing import Any

import httpx
import openai
import pytest

from my_claude_code.config.provider_catalog import (
    HYPERCHARM_DEFAULT_BASE,
    PROVIDER_CATALOG,
)
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.core.anthropic.upstream_errors import anthropic_stream_failure
from my_claude_code.core.failures import FailureKind
from my_claude_code.core.reasoning import ReasoningEffort, ReasoningPolicy
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.failure_policy import classify_provider_failure
from my_claude_code.providers.model_listing import extract_openai_model_infos
from my_claude_code.providers.openai_chat import OpenAIChatProvider
from my_claude_code.providers.openai_chat.profiles import (
    OPENAI_CHAT_PROFILES,
    OPENAI_STANDARD_REASONING,
)
from my_claude_code.providers.recovery.complaint import upstream_complaint
from my_claude_code.providers.recovery.output_cap import parse_output_token_cap
from my_claude_code.providers.runtime.models_dev import PROVIDER_ID_ALIASES
from tests.providers.support import (
    passthrough_rate_limiter,
    profiled_provider,
    reasoning_for,
)

_LIVE_MODELS_PAYLOAD: dict[str, Any] = {
    "data": [
        {
            "capabilities": {"vision": False},
            "context_window": 1000000,
            "created": 1783361967,
            "display_name": "DeepSeek V4 Flash",
            "id": "deepseek-v4-flash",
            "max_output_tokens": 384000,
            "object": "model",
            "owned_by": "hyper",
            "pricing": {
                "cache_create": 0,
                "cache_hit": 0.04,
                "input": 0.2,
                "output": 0.4,
            },
            "reasoning": {
                "default_effort_level": "high",
                "effort_levels": [
                    {"display": "High", "value": "high"},
                    {"display": "X-High", "value": "xhigh"},
                ],
            },
        },
        {
            "capabilities": {"vision": False},
            "context_window": 1000000,
            "created": 1785637554,
            "display_name": "DeepSeek V4 Flash 0731",
            "id": "deepseek-v4-flash-0731",
            "max_output_tokens": 384000,
            "object": "model",
            "owned_by": "hyper",
            "pricing": {
                "cache_create": 0,
                "cache_hit": 0.044,
                "input": 0.44,
                "output": 1.32,
            },
            "reasoning": {
                "default_effort_level": "high",
                "effort_levels": [
                    {"display": "None", "value": "none"},
                    {"display": "Low", "value": "low"},
                    {"display": "High", "value": "high"},
                    {"display": "Max", "value": "max"},
                ],
            },
        },
        {
            "capabilities": {"vision": False},
            "context_window": 1000000,
            "created": 1783361951,
            "display_name": "DeepSeek V4 Pro",
            "id": "deepseek-v4-pro",
            "max_output_tokens": 384000,
            "object": "model",
            "owned_by": "hyper",
            "pricing": {
                "cache_create": 0,
                "cache_hit": 0.2,
                "input": 2.4,
                "output": 4.8,
            },
            "reasoning": {
                "default_effort_level": "high",
                "effort_levels": [
                    {"display": "High", "value": "high"},
                    {"display": "X-High", "value": "xhigh"},
                ],
            },
        },
        {
            "capabilities": {"vision": False},
            "context_window": 1048576,
            "created": 1786714715,
            "display_name": "DeepSeek V4 Pro 0813",
            "id": "deepseek-v4-pro-0813",
            "max_output_tokens": 262144,
            "object": "model",
            "owned_by": "hyper",
            "pricing": {
                "cache_create": 0,
                "cache_hit": 0.0479072,
                "input": 1.437216,
                "output": 4.311648,
            },
            "reasoning": {
                "default_effort_level": "high",
                "effort_levels": [
                    {"display": "None", "value": "none"},
                    {"display": "Low", "value": "low"},
                    {"display": "High", "value": "high"},
                    {"display": "Max", "value": "max"},
                ],
            },
        },
        {
            "capabilities": {"vision": False},
            "context_window": 256000,
            "created": 1777561863,
            "display_name": "Gemma 4 26B A4B",
            "id": "gemma-4-26b-a4b-it",
            "max_output_tokens": 25600,
            "object": "model",
            "owned_by": "hyper",
            "pricing": {
                "cache_create": 0.058,
                "cache_hit": 0,
                "input": 0.116,
                "output": 0.38,
            },
        },
        {
            "capabilities": {"vision": False},
            "context_window": 202752,
            "created": 1776094962,
            "display_name": "GLM-5",
            "id": "glm-5",
            "max_output_tokens": 20275,
            "object": "model",
            "owned_by": "hyper",
            "pricing": {
                "cache_create": 0.425,
                "cache_hit": 0,
                "input": 0.85,
                "output": 2.774,
            },
        },
        {
            "capabilities": {"vision": False},
            "context_window": 202750,
            "created": 1780587255,
            "display_name": "GLM-5.1",
            "id": "glm-5.1",
            "max_output_tokens": 3276,
            "object": "model",
            "owned_by": "hyper",
            "pricing": {
                "cache_create": 0.645,
                "cache_hit": 0,
                "input": 1.29,
                "output": 4.22,
            },
        },
        {
            "capabilities": {"vision": False},
            "context_window": 1000000,
            "created": 1782853752,
            "display_name": "GLM-5.2",
            "id": "glm-5.2",
            "max_output_tokens": 32768,
            "object": "model",
            "owned_by": "hyper",
            "pricing": {
                "cache_create": 0,
                "cache_hit": 0.152432,
                "input": 1.52432,
                "output": 4.79072,
            },
            "reasoning": {
                "default_effort_level": "high",
                "effort_levels": [
                    {"display": "High", "value": "high"},
                    {"display": "X-High", "value": "xhigh"},
                ],
            },
        },
        {
            "capabilities": {"vision": False},
            "context_window": 1048576,
            "created": 1787936238,
            "display_name": "GLM 5.3",
            "id": "glm-5.3",
            "max_output_tokens": 262144,
            "object": "model",
            "owned_by": "hyper",
            "pricing": {
                "cache_create": 0,
                "cache_hit": 0.283088,
                "input": 1.52432,
                "output": 4.79072,
            },
            "reasoning": {
                "default_effort_level": "high",
                "effort_levels": [
                    {"display": "Low", "value": "low"},
                    {"display": "High", "value": "high"},
                    {"display": "Max", "value": "max"},
                ],
            },
        },
        {
            "capabilities": {"vision": True},
            "context_window": 1048576,
            "created": 1787775352,
            "display_name": "GLM 5.3 Flash",
            "id": "glm-5.3-flash",
            "max_output_tokens": 131072,
            "object": "model",
            "owned_by": "hyper",
            "pricing": {
                "cache_create": 0,
                "cache_hit": 0.031575200000000005,
                "input": 0.16332,
                "output": 0.5444,
            },
            "reasoning": {
                "default_effort_level": "high",
                "effort_levels": [
                    {"display": "Low", "value": "low"},
                    {"display": "High", "value": "high"},
                    {"display": "Max", "value": "max"},
                ],
            },
        },
        {
            "capabilities": {"vision": False},
            "context_window": 128072,
            "created": 1776094962,
            "display_name": "gpt-oss-120b",
            "id": "gpt-oss-120b",
            "max_output_tokens": 13107,
            "object": "model",
            "owned_by": "hyper",
            "pricing": {
                "cache_create": 0.094,
                "cache_hit": 0,
                "input": 0.188,
                "output": 0.7,
            },
            "reasoning": {
                "default_effort_level": "medium",
                "effort_levels": [
                    {"display": "None", "value": "none"},
                    {"display": "minimal", "value": "minimal"},
                    {"display": "Low", "value": "low"},
                    {"display": "Medium", "value": "medium"},
                    {"display": "High", "value": "high"},
                    {"display": "X-High", "value": "xhigh"},
                    {"display": "Max", "value": "max"},
                ],
            },
        },
        {
            "capabilities": {"vision": False},
            "context_window": 262144,
            "created": 1776094962,
            "display_name": "Kimi K2.5",
            "id": "kimi-k2.5",
            "max_output_tokens": 26214,
            "object": "model",
            "owned_by": "hyper",
            "pricing": {
                "cache_create": 0.2642,
                "cache_hit": 0,
                "input": 0.5284,
                "output": 2.785,
            },
        },
        {
            "capabilities": {"vision": True},
            "context_window": 262000,
            "created": 1783106224,
            "display_name": "Kimi K2.6",
            "id": "kimi-k2.6",
            "max_output_tokens": 26214,
            "object": "model",
            "owned_by": "hyper",
            "pricing": {
                "cache_create": 0,
                "cache_hit": 0.174208,
                "input": 1.03436,
                "output": 4.3552,
            },
            "reasoning": {
                "default_effort_level": "medium",
                "effort_levels": [
                    {"display": "Low", "value": "low"},
                    {"display": "Medium", "value": "medium"},
                    {"display": "High", "value": "high"},
                ],
            },
        },
        {
            "capabilities": {"vision": True},
            "context_window": 262000,
            "created": 1783106198,
            "display_name": "Kimi K2.7 Code",
            "id": "kimi-k2.7-code",
            "max_output_tokens": 16000,
            "object": "model",
            "owned_by": "hyper",
            "pricing": {
                "cache_create": 0,
                "cache_hit": 0.206872,
                "input": 1.03436,
                "output": 4.3552,
            },
        },
        {
            "capabilities": {"vision": False},
            "context_window": 262144,
            "created": 1788377536,
            "display_name": "Kimi K2 Thinking",
            "id": "kimi-k2-thinking",
            "max_output_tokens": 26214,
            "object": "model",
            "owned_by": "hyper",
            "pricing": {
                "cache_create": 0.3,
                "cache_hit": 0,
                "input": 0.6,
                "output": 2.5,
            },
        },
        {
            "capabilities": {"vision": True},
            "context_window": 1048576,
            "created": 1785168688,
            "display_name": "Kimi K3",
            "id": "kimi-k3",
            "max_output_tokens": 16000,
            "object": "model",
            "owned_by": "hyper",
            "pricing": {
                "cache_create": 0,
                "cache_hit": 0.32664,
                "input": 3.2664,
                "output": 16.332,
            },
            "reasoning": {
                "default_effort_level": "max",
                "effort_levels": [
                    {"display": "Low", "value": "low"},
                    {"display": "High", "value": "high"},
                    {"display": "Max", "value": "max"},
                ],
            },
        },
        {
            "capabilities": {"vision": False},
            "context_window": 128000,
            "created": 1777561928,
            "display_name": "Llama 3.3 70B Instruct",
            "id": "llama-3.3-70b-instruct",
            "max_output_tokens": 12800,
            "object": "model",
            "owned_by": "hyper",
            "pricing": {
                "cache_create": 0.3033,
                "cache_hit": 0,
                "input": 0.6066,
                "output": 1.0386,
            },
        },
        {
            "capabilities": {"vision": False},
            "context_window": 430000,
            "created": 1777561912,
            "display_name": "Llama 4 Maverick 17B 128E Instruct FP8",
            "id": "llama-4-maverick-17b-128e-instruct-fp8",
            "max_output_tokens": 43000,
            "object": "model",
            "owned_by": "hyper",
            "pricing": {
                "cache_create": 0.137,
                "cache_hit": 0,
                "input": 0.274,
                "output": 0.8992,
            },
        },
        {
            "capabilities": {"vision": False},
            "context_window": 262100,
            "created": 1780690458,
            "display_name": "MiniMax M2.7",
            "id": "minimax-m2.7",
            "max_output_tokens": 6553,
            "object": "model",
            "owned_by": "hyper",
            "pricing": {
                "cache_create": 0.213,
                "cache_hit": 0,
                "input": 0.426,
                "output": 1.62,
            },
        },
        {
            "capabilities": {"vision": True},
            "context_window": 512000,
            "created": 1785435783,
            "display_name": "MiniMax M3",
            "id": "minimax-m3",
            "max_output_tokens": 512000,
            "object": "model",
            "owned_by": "hyper",
            "pricing": {
                "cache_create": 0,
                "cache_hit": 0.0642392,
                "input": 0.32664,
                "output": 1.30656,
            },
            "reasoning": {
                "default_effort_level": "medium",
                "effort_levels": [
                    {"display": "Low", "value": "low"},
                    {"display": "Medium", "value": "medium"},
                    {"display": "High", "value": "high"},
                ],
            },
        },
        {
            "capabilities": {"vision": True},
            "context_window": 1000000,
            "created": 1779315082,
            "display_name": "Qwen3.6-Flash",
            "id": "qwen3.6-flash",
            "max_output_tokens": 64000,
            "object": "model",
            "owned_by": "hyper",
            "pricing": {
                "cache_create": 1.25,
                "cache_hit": 0.1,
                "input": 1,
                "output": 4,
            },
        },
        {
            "capabilities": {"vision": False},
            "context_window": 256000,
            "created": 1779315048,
            "display_name": "Qwen3.6-Max",
            "id": "qwen3.6-max",
            "max_output_tokens": 64000,
            "object": "model",
            "owned_by": "hyper",
            "pricing": {
                "cache_create": 2.5,
                "cache_hit": 0.2,
                "input": 2,
                "output": 12,
            },
        },
        {
            "capabilities": {"vision": True},
            "context_window": 1000000,
            "created": 1779315065,
            "display_name": "Qwen3.6-Plus",
            "id": "qwen3.6-plus",
            "max_output_tokens": 64000,
            "object": "model",
            "owned_by": "hyper",
            "pricing": {"cache_create": 2.5, "cache_hit": 0.2, "input": 2, "output": 6},
        },
        {
            "capabilities": {"vision": True},
            "context_window": 1000000,
            "created": 1785179814,
            "display_name": "Qwen3.7-Flash",
            "id": "qwen3.7-flash",
            "max_output_tokens": 64000,
            "object": "model",
            "owned_by": "hyper",
            "pricing": {
                "cache_create": 0,
                "cache_hit": 0.04,
                "input": 0.2,
                "output": 0.8,
            },
        },
        {
            "capabilities": {"vision": False},
            "context_window": 1000000,
            "created": 1779932695,
            "display_name": "Qwen3.7-Max",
            "id": "qwen3.7-max",
            "max_output_tokens": 64000,
            "object": "model",
            "owned_by": "hyper",
            "pricing": {
                "cache_create": 0,
                "cache_hit": 0.5,
                "input": 2.5,
                "output": 7.5,
            },
        },
        {
            "capabilities": {"vision": True},
            "context_window": 1000000,
            "created": 1781545491,
            "display_name": "Qwen3.7-Plus",
            "id": "qwen3.7-plus",
            "max_output_tokens": 64000,
            "object": "model",
            "owned_by": "hyper",
            "pricing": {
                "cache_create": 0,
                "cache_hit": 0.24,
                "input": 1.2,
                "output": 4.8,
            },
        },
        {
            "capabilities": {"vision": False},
            "context_window": 1000000,
            "created": 1787762237,
            "display_name": "Qwen3.8-2.4T-A95B",
            "id": "qwen3.8-2.4t-a95b",
            "max_output_tokens": 128000,
            "object": "model",
            "owned_by": "hyper",
            "pricing": {"cache_create": 0, "cache_hit": 0.25, "input": 2, "output": 6},
        },
        {
            "capabilities": {"vision": True},
            "context_window": 1000000,
            "created": 1787762218,
            "display_name": "Qwen3.8-27B",
            "id": "qwen3.8-27b",
            "max_output_tokens": 128000,
            "object": "model",
            "owned_by": "hyper",
            "pricing": {"cache_create": 0, "cache_hit": 0.1, "input": 0.5, "output": 3},
        },
        {
            "capabilities": {"vision": True},
            "context_window": 1000000,
            "created": 1787765263,
            "display_name": "Qwen3.8-Flash",
            "id": "qwen3.8-flash",
            "max_output_tokens": 128000,
            "object": "model",
            "owned_by": "hyper",
            "pricing": {
                "cache_create": 0,
                "cache_hit": 0.016,
                "input": 0.15,
                "output": 0.47,
            },
        },
        {
            "capabilities": {"vision": True},
            "context_window": 1000000,
            "created": 1785762588,
            "display_name": "Qwen3.8-Max",
            "id": "qwen3.8-max",
            "max_output_tokens": 65536,
            "object": "model",
            "owned_by": "hyper",
            "pricing": {"cache_create": 0, "cache_hit": 0.25, "input": 2, "output": 6},
        },
        {
            "capabilities": {"vision": False},
            "context_window": 106000,
            "created": 1777561986,
            "display_name": "Qwen3 Coder 480B A35B Instruct INT4 Mixed AR",
            "id": "qwen3-coder-480b-a35b-instruct-int4-mixed-ar",
            "max_output_tokens": 10600,
            "object": "model",
            "owned_by": "hyper",
            "pricing": {
                "cache_create": 0.2225,
                "cache_hit": 0,
                "input": 0.445,
                "output": 2.145,
            },
        },
        {
            "capabilities": {"vision": False},
            "context_window": 262144,
            "created": 1777562059,
            "display_name": "Qwen3 Next 80B A3B Instruct",
            "id": "qwen3-next-80b-a3b-instruct",
            "max_output_tokens": 26214,
            "object": "model",
            "owned_by": "hyper",
            "pricing": {
                "cache_create": 0.05875,
                "cache_hit": 0,
                "input": 0.1175,
                "output": 1.136,
            },
        },
    ],
    "object": "list",
}

_LIVE_MODEL_IDS: frozenset[str] = frozenset(
    str(entry["id"]) for entry in _LIVE_MODELS_PAYLOAD["data"]
)


@pytest.fixture
def hypercharm_provider():
    return profiled_provider(
        "hypercharm",
        ProviderConfig(
            api_key="test-hypercharm-key",
            base_url=HYPERCHARM_DEFAULT_BASE,
            rate_limit=10,
            rate_window=60,
        ),
        rate_limiter=passthrough_rate_limiter(),
    )


def _body(reasoning: ReasoningPolicy, provider) -> dict:
    request = MessagesRequest.model_validate(
        {
            "model": "deepseek-v4-flash",
            "max_tokens": 100,
            "messages": [Message(role="user", content="Hello")],
        }
    )
    return provider._build_request_body(request, reasoning=reasoning)


def _status_error(status: int, body: object, cls):
    request = httpx.Request("POST", f"{HYPERCHARM_DEFAULT_BASE}/chat/completions")
    response = httpx.Response(status, json=body, request=request)
    return cls(f"Error code: {status} - {body}", response=response, body=body)


# --------------------------------------------------------------------------
# Catalogue and transport
# --------------------------------------------------------------------------


def test_init_uses_documented_endpoint(hypercharm_provider):
    assert isinstance(hypercharm_provider, OpenAIChatProvider)
    assert hypercharm_provider._api_key == "test-hypercharm-key"
    assert hypercharm_provider._base_url == HYPERCHARM_DEFAULT_BASE
    assert hypercharm_provider._provider_name == "HYPERCHARM"


def test_default_base_url_constant():
    assert HYPERCHARM_DEFAULT_BASE == "https://hyper.charm.land/v1"


def test_catalog_entry_is_an_overridable_gateway():
    descriptor = PROVIDER_CATALOG["hypercharm"]

    assert descriptor.display_name == "HyperCharm"
    assert descriptor.group == "gateway"
    assert descriptor.local is False
    assert descriptor.credential_env == "HYPERCHARM_API_KEY"
    assert descriptor.credential_attr == "hypercharm_api_key"
    assert descriptor.base_url_attr == "hypercharm_base_url"
    assert descriptor.proxy_attr == "hypercharm_proxy"
    assert descriptor.default_base_url == HYPERCHARM_DEFAULT_BASE


def test_build_request_body_openai_chat(hypercharm_provider):
    request = MessagesRequest.model_validate(
        {
            "model": "deepseek-v4-flash",
            "max_tokens": 100,
            "messages": [Message(role="user", content="Hello")],
            "thinking": {"type": "enabled"},
        }
    )

    body = hypercharm_provider._build_request_body(
        request, reasoning=reasoning_for(request)
    )

    assert body["model"] == "deepseek-v4-flash"
    # ``max_tokens``, not ``max_completion_tokens``: the profile declares no
    # ``max_tokens_field`` override, which is the generic default.
    assert body["max_tokens"] == 100
    assert "max_completion_tokens" not in body
    assert body["messages"] == [{"role": "user", "content": "Hello"}]


# --------------------------------------------------------------------------
# The profile is the generic one, and is asserted to *be* the generic one
# --------------------------------------------------------------------------


def test_profile_declares_the_standard_dialect_and_nothing_bespoke():
    profile = OPENAI_CHAT_PROFILES["hypercharm"]

    # The same object every generic gateway declares, not a lookalike.
    assert profile.reasoning is OPENAI_STANDARD_REASONING
    # No bespoke listing parser: the host's /models payload is a third dialect
    # and the vendor keys are deliberately left unread (see below).
    assert profile.model_listing.path is None
    assert profile.model_listing.thinking_boolean_path is None
    assert profile.model_listing.tags_field is None
    assert profile.postprocessors == ()
    assert profile.structured_reasoning_details is False
    # Reasoning streams back as ``reasoning_content``, which is the default.
    assert profile.reasoning_delta_field == "reasoning_content"
    assert profile.request_policy.include_extra_body is True
    assert profile.request_policy.extra_body_validator is not None
    assert profile.request_policy.max_tokens_field == "max_tokens"


@pytest.mark.parametrize("effort", list(ReasoningEffort))
def test_every_effort_emits_the_standard_field(effort, hypercharm_provider):
    body = _body(ReasoningPolicy.on(effort=effort), hypercharm_provider)

    assert body["reasoning_effort"] in {"minimal", "low", "medium", "high"}
    # None of the shapes this host accepts-and-discards is ever emitted.
    assert "reasoning" not in body
    assert "thinking" not in body
    assert "reasoning" not in body.get("extra_body", {})
    assert "chat_template_kwargs" not in body.get("extra_body", {})


def test_off_sends_no_reasoning_key_at_all(hypercharm_provider):
    body = _body(ReasoningPolicy.off(), hypercharm_provider)

    assert "reasoning_effort" not in body
    assert "reasoning" not in body
    assert "thinking" not in body


def test_on_without_an_effort_names_no_rung(hypercharm_provider):
    body = _body(ReasoningPolicy.on(), hypercharm_provider)

    assert "reasoning_effort" not in body


# --------------------------------------------------------------------------
# /v1/models: the generic extractor, on the real payload
# --------------------------------------------------------------------------


def test_generic_extractor_reads_every_live_id():
    infos = extract_openai_model_infos(_LIVE_MODELS_PAYLOAD, provider_name="HYPERCHARM")

    assert {info.model_id for info in infos} == _LIVE_MODEL_IDS
    assert len(infos) == 32


def test_vendor_keys_are_ignored_rather_than_mis_parsed():
    """The five vendor keys are a third dialect; none of them is read here.

    ``reasoning`` in particular must NOT become "supports thinking": 21 of the
    32 entries omit it while still reasoning on the wire, so reading it would
    mark 21 reasoning-capable models as non-reasoning. Capability comes from
    the models.dev bucket instead.
    """
    infos = {
        info.model_id: info
        for info in extract_openai_model_infos(
            _LIVE_MODELS_PAYLOAD, provider_name="HYPERCHARM"
        )
    }

    published_ladder = infos["gpt-oss-120b"]
    no_ladder = infos["qwen3.6-flash"]

    for info in (published_ladder, no_ladder):
        assert info.supports_thinking is None
        assert info.supports_vision is None
        assert info.context_length is None
        assert info.max_output_tokens is None
        assert info.input_price is None
        assert info.output_price is None
        assert info.supported_parameters is None


def test_the_payload_really_is_the_third_dialect():
    """Guards the fixture itself: if the host changes shape, this says so."""
    keys = {key for entry in _LIVE_MODELS_PAYLOAD["data"] for key in entry}

    assert keys == {
        "capabilities",
        "context_window",
        "created",
        "display_name",
        "id",
        "max_output_tokens",
        "object",
        "owned_by",
        "pricing",
        "reasoning",
    }
    # Not OpenRouter-shaped: none of the keys the OpenRouter extractors read.
    assert "supported_parameters" not in keys
    assert "context_length" not in keys
    assert "top_provider" not in keys
    # ``capabilities`` carries only ``vision``, so ``thinking_boolean_path``
    # has nothing to point at.
    assert {
        key for entry in _LIVE_MODELS_PAYLOAD["data"] for key in entry["capabilities"]
    } == {"vision"}
    # No ``:free``/``:batch`` tags anywhere.
    assert not any(":" in model_id for model_id in _LIVE_MODEL_IDS)


def test_models_dev_alias_points_at_the_vendors_own_bucket():
    assert PROVIDER_ID_ALIASES["hypercharm"] == "hyper"


# --------------------------------------------------------------------------
# Error envelopes: two shapes, one reader
# --------------------------------------------------------------------------


def test_bare_string_error_is_read_as_authentication_not_swallowed():
    """The 401 body is ``{"error": "<string>"}``, not ``{"error": {...}}``."""
    exc = _status_error(
        401, {"error": "authentication failed"}, openai.AuthenticationError
    )

    failure = classify_provider_failure(
        exc,
        provider_name="HYPERCHARM",
        read_timeout_s=None,
        request_id=None,
        mark_rate_limited=lambda *args, **kwargs: None,
    )

    assert failure.kind is FailureKind.AUTHENTICATION
    assert failure.status_code == 401
    assert failure.retryable is False
    # The complaint reader finds the host's words under a bare string too,
    # which is what keeps the quota/billing phrase matcher working on hosts
    # that spell errors this way.
    assert upstream_complaint(exc) == "authentication failed"


def test_object_error_envelope_still_reads_the_same_way():
    exc = _status_error(
        404,
        {
            "error": {
                "message": "model not found: no-such-model-xyz",
                "type": "invalid_request_error",
                "code": None,
            }
        },
        openai.NotFoundError,
    )

    assert "model not found: no-such-model-xyz" in upstream_complaint(exc)


def test_anthropic_stream_reader_keeps_a_bare_string_error_message():
    """The shared SSE reader used to discard the host's words on this shape."""
    failure = anthropic_stream_failure({"error": "authentication failed"})

    assert failure.message == "authentication failed"
    # No ``type`` is available in that shape, so the kind stays the default.
    assert failure.kind is FailureKind.UPSTREAM


def test_anthropic_stream_reader_unchanged_for_the_object_shape():
    failure = anthropic_stream_failure(
        {"error": {"type": "rate_limit_error", "message": "slow down"}}
    )

    assert failure.kind is FailureKind.RATE_LIMIT
    assert failure.status_code == 429
    assert failure.message == "slow down"


def test_anthropic_stream_reader_falls_back_when_nothing_is_readable():
    assert anthropic_stream_failure({"error": "   "}).message == (
        "Provider stream failed."
    )
    assert anthropic_stream_failure(None).message == "Provider stream failed."


@pytest.mark.parametrize(
    "body",
    [
        {"error": {"message": "Invalid max_tokens input.", "type": "x", "code": None}},
        {"error": "Invalid max_tokens input."},
    ],
)
def test_cap_error_naming_no_number_teaches_the_net_nothing(body):
    """ "Invalid max_tokens input." names no ceiling, so nothing may be learned.

    The wrong outcome here is not a crash but a *wrong* learn: a parser that
    fell back to any integer in the body would pin a cap the host never stated.
    """
    exc = _status_error(400, body, openai.BadRequestError)

    assert parse_output_token_cap(exc) is None
    assert json.dumps(body)  # the body is plain JSON, nothing exotic
