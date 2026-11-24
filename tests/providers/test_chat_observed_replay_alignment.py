"""Runtime observed reasoning-channel replay alignment & safety invariants (8.40.0)."""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from my_claude_code.api.model_admin import (
    FACT_KIND_LABELS,
    PAGE_TABLE_UNLISTED_FACT_KINDS,
)
from my_claude_code.config.nim import NimSettings
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.reasoning import ReasoningEffort, ReasoningPolicy
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.deepseek import DeepSeekProvider
from my_claude_code.providers.google_openai import GoogleOpenAIProvider
from my_claude_code.providers.mistral import MistralProvider
from my_claude_code.providers.nvidia_nim import NvidiaNimProvider
from my_claude_code.providers.openai_chat import (
    GENERIC_OPENAI_PROFILE,
    OPENAI_CHAT_PROFILES,
    OpenAIChatProvider,
)
from my_claude_code.providers.openrouter_gateway import openrouter_gateway_profile
from my_claude_code.providers.recovery import (
    ALLOWED_FACT_KINDS,
    FACT_REASONING_CHANNEL,
    INFERRED_FACT_TTL_SECONDS,
    SOURCE_OBSERVATION,
)
from my_claude_code.providers.recovery.facts import FACT_TTL_SECONDS, LearnedFact
from my_claude_code.providers.recovery.store import (
    LearnedFactStore,
    set_learned_fact_store,
)
from tests.providers.support import passthrough_rate_limiter


def _config() -> ProviderConfig:
    return ProviderConfig(
        api_key="sk-test-replay",
        base_url="https://replay.invalid/v1",
        rate_limit=100,
        rate_window=60,
        commit_holdback_seconds=0,
    )


def _turn1_request(model: str = "model-a") -> MessagesRequest:
    return MessagesRequest.model_validate(
        {
            "model": model,
            "max_tokens": 128,
            "messages": [{"role": "user", "content": "hi"}],
        }
    )


def _turn2_request(
    model: str = "model-a", *, with_redacted_detail: bool = False
) -> MessagesRequest:
    content: list[dict] = [
        {"type": "thinking", "thinking": "turn 1 thought"},
        {"type": "text", "text": "turn 1 answer"},
    ]
    if with_redacted_detail:
        content.insert(
            0,
            {
                "type": "redacted_thinking",
                "data": json.dumps({"type": "reasoning.encrypted", "data": "enc-1"}),
            },
        )
    return MessagesRequest.model_validate(
        {
            "model": model,
            "max_tokens": 128,
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": content},
                {"role": "user", "content": "continue"},
            ],
        }
    )


def _chunk(
    *,
    content: object = None,
    reasoning_content: object = None,
    reasoning: object = None,
    finish_reason: str | None = None,
) -> SimpleNamespace:
    delta = SimpleNamespace(
        content=content,
        reasoning_content=reasoning_content,
        reasoning=reasoning,
        tool_calls=None,
    )
    usage = (
        SimpleNamespace(prompt_tokens=10, completion_tokens=5)
        if finish_reason is not None
        else None
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)],
        usage=usage,
    )


async def _stream(*chunks: SimpleNamespace) -> AsyncIterator[SimpleNamespace]:
    for chunk in chunks:
        yield chunk


def test_fact_reasoning_channel_registry_and_admin_invariants() -> None:
    assert FACT_REASONING_CHANNEL == "reasoning_channel"
    assert FACT_REASONING_CHANNEL in ALLOWED_FACT_KINDS
    assert FACT_TTL_SECONDS[FACT_REASONING_CHANNEL] == INFERRED_FACT_TTL_SECONDS
    assert FACT_REASONING_CHANNEL in FACT_KIND_LABELS
    assert FACT_REASONING_CHANNEL in PAGE_TABLE_UNLISTED_FACT_KINDS


@pytest.mark.asyncio
async def test_custom_provider_cold_start_then_adapts_per_model_to_observed_channel(
    tmp_path,
) -> None:
    """M4: custom_* starts on THINK_TAGS, then Turn 2+ aligns per model to reasoning_content / reasoning."""
    store = LearnedFactStore(flush_debounce_seconds=0.0)
    store.enable_persistence(tmp_path / "learned_facts.json")
    set_learned_fact_store(store)

    provider = OpenAIChatProvider(
        _config(),
        profile=GENERIC_OPENAI_PROFILE,
        rate_limiter=passthrough_rate_limiter(),
        provider_id="custom_gateway",
    )

    # Cold start for model-rc and model-r matches GENERIC_OPENAI_PROFILE (THINK_TAGS).
    cold_rc = provider._build_request_body(_turn2_request("model-rc"))
    assert "<think>\nturn 1 thought\n</think>" in cold_rc["messages"][1]["content"]
    assert "reasoning_content" not in cold_rc["messages"][1]

    # Turn 1 on model-rc streams delta.reasoning_content
    with patch.object(
        provider._client.chat.completions,
        "create",
        new_callable=AsyncMock,
        return_value=_stream(
            _chunk(reasoning_content="rc thought"),
            _chunk(content="ok", finish_reason="stop"),
        ),
    ):
        _ = [ev async for ev in provider.stream_response(_turn1_request("model-rc"))]

    # Turn 1 on model-r streams delta.reasoning (via bidirectional fallback)
    with patch.object(
        provider._client.chat.completions,
        "create",
        new_callable=AsyncMock,
        return_value=_stream(
            _chunk(reasoning="r thought"),
            _chunk(content="ok", finish_reason="stop"),
        ),
    ):
        _ = [ev async for ev in provider.stream_response(_turn1_request("model-r"))]

    store.flush()
    facts = {
        (f.model_id, f.fact_kind): f for f in store.facts_for_provider("custom_gateway")
    }
    assert facts[("model-rc", FACT_REASONING_CHANNEL)].value == "reasoning_content"
    assert facts[("model-rc", FACT_REASONING_CHANNEL)].source == SOURCE_OBSERVATION
    assert facts[("model-rc", FACT_REASONING_CHANNEL)].detail == "chat_completions"
    assert facts[("model-r", FACT_REASONING_CHANNEL)].value == "reasoning"
    assert facts[("model-r", FACT_REASONING_CHANNEL)].source == SOURCE_OBSERVATION

    # Turn 2 replays onto each model's observed wire channel without <think> tags.
    body_rc = provider._build_request_body(_turn2_request("model-rc"))
    assert body_rc["messages"][1]["reasoning_content"] == "turn 1 thought"
    assert "reasoning" not in body_rc["messages"][1]
    assert "<think>" not in body_rc["messages"][1]["content"]

    body_r = provider._build_request_body(_turn2_request("model-r"))
    assert body_r["messages"][1]["reasoning"] == "turn 1 thought"
    assert "reasoning_content" not in body_r["messages"][1]
    assert "<think>" not in body_r["messages"][1]["content"]

    # Persistence across restart (reloaded LearnedFactStore)
    reopened = LearnedFactStore(flush_debounce_seconds=0.0)
    reopened.enable_persistence(tmp_path / "learned_facts.json")
    set_learned_fact_store(reopened)
    restarted_provider = OpenAIChatProvider(
        _config(),
        profile=GENERIC_OPENAI_PROFILE,
        rate_limiter=passthrough_rate_limiter(),
        provider_id="custom_gateway",
    )
    body_after_restart = restarted_provider._build_request_body(
        _turn2_request("model-r")
    )
    assert body_after_restart["messages"][1]["reasoning"] == "turn 1 thought"


@pytest.mark.asyncio
async def test_channel_observation_updates_in_place_and_expires_after_ttl(
    tmp_path,
) -> None:
    store = LearnedFactStore(flush_debounce_seconds=0.0)
    store.enable_persistence(tmp_path / "learned_facts.json")
    set_learned_fact_store(store)

    provider = OpenAIChatProvider(
        _config(),
        profile=OPENAI_CHAT_PROFILES["bedrock"],
        rate_limiter=passthrough_rate_limiter(),
        provider_id="bedrock",
    )

    # First stream uses <think> tags -> stays THINK_TAGS
    with patch.object(
        provider._client.chat.completions,
        "create",
        new_callable=AsyncMock,
        return_value=_stream(
            _chunk(content="<think>t</think>ok", finish_reason="stop")
        ),
    ):
        _ = [ev async for ev in provider.stream_response(_turn1_request("m1"))]
    body_think = provider._build_request_body(_turn2_request("m1"))
    assert "<think>\nturn 1 thought\n</think>" in body_think["messages"][1]["content"]

    # Later stream switches to delta.reasoning_content -> updates in place
    with patch.object(
        provider._client.chat.completions,
        "create",
        new_callable=AsyncMock,
        return_value=_stream(
            _chunk(reasoning_content="native", content="ok", finish_reason="stop")
        ),
    ):
        _ = [ev async for ev in provider.stream_response(_turn1_request("m1"))]
    body_rc = provider._build_request_body(_turn2_request("m1"))
    assert body_rc["messages"][1]["reasoning_content"] == "turn 1 thought"
    assert "<think>" not in body_rc["messages"][1]["content"]

    # Expired fact (>7 days) restores cold-start default (THINK_TAGS for bedrock)
    stale_iso = (
        (datetime.now(UTC) - timedelta(days=10))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    stale_fact = LearnedFact(
        provider_id="bedrock",
        model_id="m1",
        fact_kind=FACT_REASONING_CHANNEL,
        value="reasoning_content",
        learned_at=stale_iso,
        last_confirmed_at=stale_iso,
        source=SOURCE_OBSERVATION,
        detail="chat_completions",
    )
    stale_store = LearnedFactStore(flush_debounce_seconds=0.0)
    stale_store.load_document({"version": 2, "facts": [stale_fact.as_row()]})
    set_learned_fact_store(stale_store)
    provider_expired = OpenAIChatProvider(
        _config(),
        profile=OPENAI_CHAT_PROFILES["bedrock"],
        rate_limiter=passthrough_rate_limiter(),
        provider_id="bedrock",
    )
    body_expired = provider_expired._build_request_body(_turn2_request("m1"))
    assert "<think>\nturn 1 thought\n</think>" in body_expired["messages"][1]["content"]
    assert "reasoning_content" not in body_expired["messages"][1]


@pytest.mark.asyncio
async def test_open_router_preserves_reasoning_details_source_when_channel_adapts() -> (
    None
):
    provider = OpenAIChatProvider(
        _config(),
        profile=openrouter_gateway_profile("OPEN_ROUTER"),
        rate_limiter=passthrough_rate_limiter(),
        provider_id="open_router",
    )
    with patch.object(
        provider._client.chat.completions,
        "create",
        new_callable=AsyncMock,
        return_value=_stream(
            _chunk(reasoning="or reasoning", content="ok", finish_reason="stop")
        ),
    ):
        _ = [ev async for ev in provider.stream_response(_turn1_request("or-model"))]

    body = provider._build_request_body(
        _turn2_request("or-model", with_redacted_detail=True)
    )
    assistant = body["messages"][1]
    assert assistant["reasoning"] == "turn 1 thought"
    assert "reasoning_content" not in assistant
    assert assistant["reasoning_details"] == [
        {"type": "reasoning.encrypted", "data": "enc-1"}
    ]


@pytest.mark.asyncio
async def test_disabled_profiles_never_overridden_by_observed_channel() -> None:
    """M5: DISABLED profiles (cline, lmstudio, wandb) stay DISABLED even after stream observation."""
    for provider_id in ("cline", "wandb", "chutes"):
        provider = OpenAIChatProvider(
            _config(),
            profile=OPENAI_CHAT_PROFILES[provider_id],
            rate_limiter=passthrough_rate_limiter(),
            provider_id=provider_id,
        )
        with patch.object(
            provider._client.chat.completions,
            "create",
            new_callable=AsyncMock,
            return_value=_stream(
                _chunk(reasoning="streamed", content="ok", finish_reason="stop")
            ),
        ):
            _ = [
                ev
                async for ev in provider.stream_response(_turn1_request("m-disabled"))
            ]

        body = provider._build_request_body(
            _turn2_request("m-disabled", with_redacted_detail=(provider_id == "cline"))
        )
        assistant = body["messages"][1]
        assert "reasoning" not in assistant
        assert "reasoning_content" not in assistant
        assert "<think>" not in (assistant.get("content") or "")
        if provider_id == "cline":
            assert assistant["reasoning_details"] == [
                {"type": "reasoning.encrypted", "data": "enc-1"}
            ]


def test_fixed_dialect_subclasses_are_exempt_from_channel_override() -> None:
    """M6: DeepSeekProvider, MistralProvider, NvidiaNimProvider ignore observed channel overrides."""
    deepseek = DeepSeekProvider(_config(), rate_limiter=passthrough_rate_limiter())
    mistral = MistralProvider(_config(), rate_limiter=passthrough_rate_limiter())
    nim = NvidiaNimProvider(
        _config(), nim_settings=NimSettings(), rate_limiter=passthrough_rate_limiter()
    )

    for prov in (deepseek, mistral, nim):
        prov._recovery_memory.remember_reasoning_channel(
            "deepseek-reasoner", "reasoning", surface="chat_completions"
        )
        prov._recovery_memory.remember_reasoning_channel(
            "deepseek-reasoner", "think_tags"
        )
        effective = prov.effective_request_policy_for(
            "deepseek-reasoner", prov._profile.request_policy
        )
        assert effective is prov._profile.request_policy

    # DeepSeek tool-call history keeps reasoning_content (never rewritten to 'reasoning')
    tool_req = MessagesRequest.model_validate(
        {
            "model": "deepseek-reasoner",
            "max_tokens": 128,
            "messages": [
                {"role": "user", "content": "run"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "plan"},
                        {
                            "type": "tool_use",
                            "id": "call_1",
                            "name": "Echo",
                            "input": {},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "call_1",
                            "content": "ok",
                        }
                    ],
                },
            ],
        }
    )
    ds_body = deepseek._build_request_body(
        tool_req, reasoning=ReasoningPolicy.on(effort=ReasoningEffort.HIGH)
    )
    assert ds_body["messages"][1]["reasoning_content"] == "plan"
    assert "reasoning" not in ds_body["messages"][1]

    # Mistral keeps typed thinking chunks
    ms_body = mistral._build_request_body(
        _turn2_request("mistral-large"),
        reasoning=ReasoningPolicy.on(effort=ReasoningEffort.HIGH),
    )
    assert any(
        isinstance(part, dict) and part.get("type") == "thinking"
        for part in ms_body["messages"][1]["content"]
    )


def test_google_openai_provider_consumes_observed_reasoning_channel() -> None:
    provider = GoogleOpenAIProvider(
        _config(),
        profile=GENERIC_OPENAI_PROFILE,
        rate_limiter=passthrough_rate_limiter(),
        provider_id="google_custom",
    )
    provider._recovery_memory.remember_reasoning_channel(
        "gemini-2.5-flash", "reasoning_content", surface="chat_completions"
    )
    body = provider._build_request_body(_turn2_request("gemini-2.5-flash"))
    assert body["messages"][1]["reasoning_content"] == "turn 1 thought"
    assert "<think>" not in body["messages"][1]["content"]
