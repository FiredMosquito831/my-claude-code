"""Comprehensive unit and integration tests for PR1, PR2, PR3, and T2-NEW."""

import json
from typing import Any

import httpx
import pytest

from my_claude_code.application.reasoning import (
    client_reasoning_policy,
    resolve_reasoning_policy,
)
from my_claude_code.config.admin.manifest import FIELDS
from my_claude_code.config.constants import (
    REASONING_ADAPTIVE_CHOICES,
    REASONING_ADAPTIVE_DEFAULT,
)
from my_claude_code.config.nim import NimSettings
from my_claude_code.config.reasoning import ReasoningPreference
from my_claude_code.config.settings import (
    Settings,
    configured_reasoning_adaptive,
    get_settings,
)
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.gemini_api import (
    GeminiApiAdapter,
    GeminiGenerateContentRequest,
)
from my_claude_code.core.openai_chat_completions import (
    OpenAIChatCompletionRequest,
    OpenAIChatCompletionsAdapter,
)
from my_claude_code.core.reasoning import (
    ReasoningControl,
    ReasoningEffort,
    ReasoningPolicy,
    narrow_dialect_by_rejections,
)
from my_claude_code.core.wire_capture import (
    install_wire_trace,
    is_reasoning_key,
    reasoning_was_emitted,
    set_reasoning_suppressed,
    summarize_wire_body,
)
from my_claude_code.providers.anthropic_messages import (
    ANTHROPIC_GATEWAY_REASONING_DIALECT,
    ANTHROPIC_REASONING_DIALECT,
    AnthropicMessagesProvider,
    build_anthropic_messages_body,
)
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.chatgpt_oauth.conversion import (
    build_chatgpt_oauth_request_body,
)
from my_claude_code.providers.deepseek import DeepSeekProvider
from my_claude_code.providers.gemini import GeminiProvider
from my_claude_code.providers.google_openai.reasoning import VertexReasoningEncoder
from my_claude_code.providers.mistral import MistralProvider
from my_claude_code.providers.nvidia_nim import NvidiaNimProvider
from my_claude_code.providers.nvidia_nim.request_options import build_nim_request_body
from my_claude_code.providers.openai_chat import (
    GENERIC_OPENAI_PROFILE,
    OPENAI_CHAT_PROFILES,
    create_openai_chat_provider,
    profile_with_declared_surfaces,
)
from my_claude_code.providers.openai_chat.reasoning import (
    NamedEffortReasoning,
)
from my_claude_code.providers.openai_chat.responses_transport import (
    RESPONSES_REASONING_DIALECT,
    ResponsesTransport,
)
from my_claude_code.providers.openai_responses import build_responses_request_body
from my_claude_code.providers.recovery import RecoveryMemory
from tests.providers.support import (
    ImmediateRetryProviderRateLimiter,
    passthrough_rate_limiter,
)


def _anthropic_request(
    *,
    model: str = "claude-sonnet-5",
    max_tokens: int = 8192,
    thinking: dict[str, Any] | None = None,
    output_config: dict[str, Any] | None = None,
) -> MessagesRequest:
    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "stream": True,
        "messages": [{"role": "user", "content": "hello"}],
    }
    if thinking is not None:
        payload["thinking"] = thinking
    if output_config is not None:
        payload["output_config"] = output_config
    return MessagesRequest.model_validate(payload)


# ---------------------------------------------------------------------------
# PR1 (T2-Q2): Anthropic output_config sanitization & wire_capture
# ---------------------------------------------------------------------------


def test_pr1_off_and_provider_default_strip_effort_and_preserve_format() -> None:
    fmt = {"type": "json_schema", "schema": {"type": "object"}}
    req = _anthropic_request(
        thinking={"type": "enabled", "budget_tokens": 2048},
        output_config={"effort": "high", "format": fmt},
    )

    off_body = build_anthropic_messages_body(req, reasoning=ReasoningPolicy.off())
    assert "thinking" not in off_body
    assert off_body["output_config"] == {"format": fmt}

    bare_req = _anthropic_request(
        thinking={"type": "enabled", "budget_tokens": 2048},
        output_config={"effort": "high"},
    )
    bare_off = build_anthropic_messages_body(bare_req, reasoning=ReasoningPolicy.off())
    assert "thinking" not in bare_off
    assert "output_config" not in bare_off

    default_body = build_anthropic_messages_body(
        req, reasoning=ReasoningPolicy.provider_default()
    )
    assert default_body["thinking"] == {"type": "enabled", "budget_tokens": 2048}
    assert default_body["output_config"] == {"format": fmt}

    suppressed_body = build_anthropic_messages_body(
        bare_req, reasoning=ReasoningPolicy.provider_default(), suppress_thinking=True
    )
    assert "thinking" not in suppressed_body
    assert "output_config" not in suppressed_body

    set_reasoning_suppressed(True)
    try:
        ctx_suppressed = build_anthropic_messages_body(
            bare_req, reasoning=ReasoningPolicy.provider_default()
        )
    finally:
        set_reasoning_suppressed(False)
    assert "thinking" not in ctx_suppressed
    assert "output_config" not in ctx_suppressed


def test_pr1_manual_budget_omits_effort_while_adaptive_clamps_minimal_and_drops_none() -> (
    None
):
    fmt = {"type": "json_schema", "schema": {"type": "object"}}
    req = _anthropic_request(output_config={"effort": "high", "format": fmt})

    manual = build_anthropic_messages_body(
        req,
        reasoning=ReasoningPolicy.on(effort=ReasoningEffort.HIGH, budget_tokens=2048),
    )
    assert manual["thinking"] == {"type": "enabled", "budget_tokens": 2048}
    assert manual["output_config"] == {"format": fmt}

    minimal = build_anthropic_messages_body(
        req,
        reasoning=ReasoningPolicy(
            control=ReasoningControl.ADAPTIVE, effort=ReasoningEffort.MINIMAL
        ),
    )
    assert minimal["thinking"] == {"type": "adaptive"}
    assert minimal["output_config"] == {"effort": "low", "format": fmt}

    none_req = _anthropic_request(output_config={"effort": "none", "format": fmt})
    none_effort = build_anthropic_messages_body(
        none_req,
        reasoning=ReasoningPolicy.adaptive(),
    )
    assert none_effort["thinking"] == {"type": "adaptive"}
    assert none_effort["output_config"] == {"format": fmt}


def test_pr1_wire_capture_detects_output_config_effort_only() -> None:
    assert is_reasoning_key("output_config") is True
    assert reasoning_was_emitted({"output_config": {"effort": "high"}}) is True
    assert reasoning_was_emitted({"output_config": {"effort": "none"}}) is False
    assert (
        reasoning_was_emitted(
            {"output_config": {"format": {"type": "json_schema", "schema": {}}}}
        )
        is False
    )
    summary = json.loads(
        summarize_wire_body(
            {"model": "m", "output_config": {"effort": "max", "format": {"type": "x"}}}
        )
    )
    assert summary["output_config"] == {"effort": "max", "format": {"type": "x"}}


def test_pr1_narrow_dialect_clears_adaptive_on_rejection() -> None:
    narrowed_val = narrow_dialect_by_rejections(
        ANTHROPIC_REASONING_DIALECT, {}, {"thinking.type": {"adaptive"}}
    )
    assert narrowed_val.adaptive is False
    narrowed_field = narrow_dialect_by_rejections(
        ANTHROPIC_REASONING_DIALECT, {"thinking.type": "bad"}, {}
    )
    assert narrowed_field.adaptive is False
    assert narrowed_field.toggle is False


# ---------------------------------------------------------------------------
# PR2 (T2-Q3): Surface-aware OpenAIChatProvider + ResponsesTransport recovery
# ---------------------------------------------------------------------------


def test_pr2_single_surface_fast_path_and_multi_surface_scoping() -> None:
    groq = create_openai_chat_provider(
        "groq",
        ProviderConfig(api_key="sk-test", base_url="https://api.groq.com/openai/v1"),
        passthrough_rate_limiter(),
        profile=OPENAI_CHAT_PROFILES["groq"],
    )
    assert (
        groq.reasoning_dialect("gpt-oss")
        == OPENAI_CHAT_PROFILES["groq"].reasoning.dialect
    )

    opencode_go = create_openai_chat_provider(
        "opencode_go",
        ProviderConfig(api_key="sk-test", base_url="https://opencode.ai/zen/go/v1"),
        passthrough_rate_limiter(),
        profile=OPENAI_CHAT_PROFILES["opencode_go"],
    )
    # muse-spark-1.3-contributor has a measured Messages surface on opencode_go.
    go_dialect = opencode_go.reasoning_dialect("muse-spark-1.3-contributor")
    assert go_dialect == ANTHROPIC_GATEWAY_REASONING_DIALECT

    responses_profile = profile_with_declared_surfaces(
        GENERIC_OPENAI_PROFILE, ("responses", "chat_completions")
    )
    # Build an OpenAIChatProvider with RESPONSES declared alone so resolution lands on RESPONSES.
    responses_only_profile = profile_with_declared_surfaces(
        GENERIC_OPENAI_PROFILE, ("responses",)
    )
    assert responses_profile.response_surfaces
    multi = create_openai_chat_provider(
        "custom_multi",
        ProviderConfig(api_key="sk-test", base_url="https://multi.invalid/v1"),
        passthrough_rate_limiter(),
        profile=responses_only_profile,
    )
    assert multi.reasoning_dialect("gpt-5") == RESPONSES_REASONING_DIALECT

    # Poisoning responses recovery memory must not poison chat_completions.
    multi._recovery_memory.remember_rejected_values(
        "gpt-5", "reasoning.effort", {"xhigh"}, surface="responses"
    )
    multi._recovery_memory.remember_rejection(
        "gpt-5", "reasoning.effort", evidence="bad", surface="responses"
    )
    assert ReasoningEffort.XHIGH not in (
        multi.reasoning_dialect("gpt-5").effort_values or frozenset()
    )
    assert multi._recovery_memory.rejections_for("gpt-5") is None
    resp_rejections = multi._recovery_memory.rejections_for(
        "gpt-5", surface="responses"
    )
    assert resp_rejections is not None
    assert "reasoning.effort" in resp_rejections
    chat_profile_provider = create_openai_chat_provider(
        "custom_multi",
        ProviderConfig(api_key="sk-test", base_url="https://multi.invalid/v1"),
        passthrough_rate_limiter(),
        profile=responses_profile,
    )
    chat_profile_provider._recovery_memory = multi._recovery_memory
    assert (
        chat_profile_provider.reasoning_dialect("gpt-5")
        == chat_profile_provider._declared_reasoning_dialect()
    )


@pytest.mark.asyncio
async def test_pr2_responses_transport_recovers_effort_and_summary_and_learns() -> None:
    memory = RecoveryMemory()
    seen_bodies: list[dict[str, Any]] = []
    sse = (
        b"event: response.completed\n"
        b'data: {"type":"response.completed","response":{"id":"resp_1","model":"gpt-5",'
        b'"output":[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"ok"}]}],'
        b'"usage":{"input_tokens":5,"output_tokens":2}}}\n\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen_bodies.append(body)
        reasoning_obj = body.get("reasoning") or {}
        if "summary" in reasoning_obj:
            return httpx.Response(
                400,
                json={"error": {"message": "Unknown parameter: 'reasoning.summary'"}},
                request=request,
            )
        if reasoning_obj.get("effort") == "xhigh":
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": "Unsupported value: 'xhigh' for 'reasoning.effort'"
                    }
                },
                request=request,
            )

        async def _sse_body():
            yield sse

        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse_body(),
        )

    transport = ResponsesTransport(
        ProviderConfig(api_key="sk-test", base_url="https://multi.invalid/v1"),
        base_url="https://multi.invalid/v1",
        provider_name="MULTI",
        identity=None,
        api_key=None,
        rate_limiter=ImmediateRetryProviderRateLimiter(),
        memory=memory,
    )
    transport._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    req = _anthropic_request(model="gpt-5")
    try:
        install_wire_trace()
        policy = ReasoningPolicy.on(effort=ReasoningEffort.XHIGH)
        body1, headers1 = transport.build_body(
            req, reasoning=policy, max_output_tokens=512
        )
        async for _ev in transport.stream(
            req,
            input_tokens=5,
            reasoning=policy,
            body=body1,
            headers=headers1,
            surface_label="responses",
        ):
            pass
        assert len(seen_bodies) == 3
        assert seen_bodies[0]["reasoning"]["summary"] == "auto"
        assert "summary" not in (seen_bodies[1].get("reasoning") or {})
        assert seen_bodies[1]["reasoning"]["effort"] == "xhigh"
        assert "reasoning" not in seen_bodies[2]
        rejected = memory.rejected_values_for("gpt-5", surface="responses") or {}
        assert "xhigh" in rejected.get("reasoning.effort", set())
        # Subsequent request clamps xhigh and drops summary up-front without extra 400.
        seen_bodies.clear()
        body2, headers2 = transport.build_body(
            req, reasoning=policy, max_output_tokens=512
        )
        async for _ev in transport.stream(
            req,
            input_tokens=5,
            reasoning=policy,
            body=body2,
            headers=headers2,
            surface_label="responses",
        ):
            pass
        assert len(seen_bodies) == 1
        assert seen_bodies[0]["reasoning"]["effort"] != "xhigh"
        assert "summary" not in seen_bodies[0]["reasoning"]
    finally:
        await transport._client.aclose()


# ---------------------------------------------------------------------------
# PR3 (T2-Q4, T2-Q6, T2-Q7): DeepSeek/NIM/Mistral, _LOW_TO_MAX, Chat/Gemini
# ---------------------------------------------------------------------------


def test_pr3_deepseek_and_nim_recovery_narrowing_and_adaptive() -> None:
    ds = DeepSeekProvider(
        ProviderConfig(api_key="sk-test", base_url="https://api.deepseek.com/v1"),
        rate_limiter=passthrough_rate_limiter(),
    )
    ds._recovery_memory.remember_rejection(
        "deepseek-chat", "thinking", evidence="unsupported"
    )
    assert ds.reasoning_dialect("deepseek-chat").toggle is False

    ds_body = ds._build_request_body(
        _anthropic_request(model="deepseek-chat"),
        reasoning=ReasoningPolicy.adaptive(),
    )
    assert ds_body["extra_body"]["thinking"] == {"type": "enabled"}

    nim = NvidiaNimProvider(
        ProviderConfig(
            api_key="sk-test", base_url="https://integrate.api.nvidia.com/v1"
        ),
        nim_settings=NimSettings(),
        rate_limiter=passthrough_rate_limiter(),
    )
    nim._recovery_memory.remember_rejection(
        "meta/llama-3", "chat_template_kwargs.thinking", evidence="unsupported"
    )
    assert nim.reasoning_dialect("meta/llama-3").toggle is False

    nim_body = build_nim_request_body(
        _anthropic_request(model="deepseek-ai/deepseek-v3.2"),
        NimSettings(),
        reasoning=ReasoningPolicy.adaptive(),
        provider_id="nvidia_nim",
    )
    assert nim_body["extra_body"]["chat_template_kwargs"]["thinking"] is True


def test_pr3_mistral_adaptive_top_rung_and_narrowed_rung() -> None:
    mistral = MistralProvider(
        ProviderConfig(api_key="sk-test", base_url="https://api.mistral.ai/v1"),
        rate_limiter=passthrough_rate_limiter(),
    )
    trace = install_wire_trace()
    body = mistral._build_request_body(
        _anthropic_request(model="mistral-large-latest"),
        reasoning=ReasoningPolicy.adaptive(),
    )
    assert body["reasoning_effort"] == "max"
    assert trace.reasoning_adaptations

    mistral._recovery_memory.remember_rejected_values(
        "mistral-small-latest", "reasoning_effort", {"max", "xhigh"}
    )
    narrowed_body = mistral._build_request_body(
        _anthropic_request(model="mistral-small-latest"),
        reasoning=ReasoningPolicy.adaptive(),
    )
    assert narrowed_body["reasoning_effort"] == "high"


def test_pr3_low_to_max_clamps_xhigh_to_high() -> None:
    for provider_id in ("wafer", "ollama", "ollama_cloud"):
        profile = OPENAI_CHAT_PROFILES[provider_id]
        handler = profile.reasoning
        assert isinstance(handler, NamedEffortReasoning)
        assert handler.dialect.effort_values is not None
        assert ReasoningEffort.XHIGH not in handler.dialect.effort_values
        body: dict[str, Any] = {}
        handler.encode(body, ReasoningPolicy.on(effort=ReasoningEffort.XHIGH))
        assert body["reasoning_effort"] == "high"


def test_pr3_chat_completions_nested_reasoning_ingress() -> None:
    adapter = OpenAIChatCompletionsAdapter()
    req_effort = OpenAIChatCompletionRequest.model_validate(
        {
            "model": "claude-sonnet-5",
            "messages": [{"role": "user", "content": "hi"}],
            "reasoning": {"effort": "high", "exclude": True},
        }
    )
    converted_effort = adapter.to_anthropic_payload(req_effort)
    assert converted_effort["output_config"] == {"effort": "high"}
    assert "thinking" not in converted_effort

    # Top-level reasoning_effort wins on conflict.
    req_conflict = OpenAIChatCompletionRequest.model_validate(
        {
            "model": "claude-sonnet-5",
            "messages": [{"role": "user", "content": "hi"}],
            "reasoning_effort": "low",
            "reasoning": {"effort": "high"},
        }
    )
    converted_conflict = adapter.to_anthropic_payload(req_conflict)
    assert converted_conflict["output_config"] == {"effort": "low"}

    # reasoning.max_tokens maps to enabled + budget_tokens when no effort present.
    req_budget = OpenAIChatCompletionRequest.model_validate(
        {
            "model": "claude-sonnet-5",
            "messages": [{"role": "user", "content": "hi"}],
            "reasoning": {"max_tokens": 4096},
        }
    )
    converted_budget = adapter.to_anthropic_payload(req_budget)
    assert converted_budget["thinking"] == {"type": "enabled", "budget_tokens": 4096}


def test_pr3_gemini_xhigh_and_thinking_level_over_budget_conflict() -> None:
    adapter = GeminiApiAdapter()
    parsed_xhigh = adapter.to_anthropic_payload(
        GeminiGenerateContentRequest.model_validate(
            {
                "model": "gemini-3-pro",
                "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
                "generationConfig": {"thinkingConfig": {"thinkingLevel": "XHIGH"}},
            }
        )
    ).payload
    assert parsed_xhigh["output_config"] == {"effort": "xhigh"}

    # Explicit thinkingLevel overrides numeric thinkingBudget.
    parsed_conflict = adapter.to_anthropic_payload(
        GeminiGenerateContentRequest.model_validate(
            {
                "model": "gemini-3-pro",
                "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
                "generationConfig": {
                    "thinkingConfig": {"thinkingLevel": "LOW", "thinkingBudget": 16000}
                },
            }
        )
    ).payload
    assert parsed_conflict["output_config"] == {"effort": "low"}
    assert "thinking" not in parsed_conflict

    # OFF / NONE thinkingLevel overrides positive thinkingBudget to disabled.
    parsed_off = adapter.to_anthropic_payload(
        GeminiGenerateContentRequest.model_validate(
            {
                "model": "gemini-3-pro",
                "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
                "generationConfig": {
                    "thinkingConfig": {"thinkingLevel": "OFF", "thinkingBudget": 4096}
                },
            }
        )
    ).payload
    assert parsed_off["thinking"] == {"type": "disabled", "enabled": False}


# ---------------------------------------------------------------------------
# T2-NEW: REASONING_ADAPTIVE setting, hot-apply, manifest, runtime adherence
# ---------------------------------------------------------------------------


def test_t2_new_setting_manifest_and_wire_adherence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert REASONING_ADAPTIVE_DEFAULT == "client"
    assert REASONING_ADAPTIVE_CHOICES == ("client", "on", "off")
    spec_keys = [spec.key for spec in FIELDS]
    assert (
        spec_keys.index("REASONING_ADAPTIVE") == spec_keys.index("REASONING_POLICY") + 1
    )

    req_adaptive = _anthropic_request(thinking={"type": "adaptive"})
    monkeypatch.delenv("REASONING_ADAPTIVE", raising=False)
    get_settings.cache_clear()
    try:
        assert Settings().reasoning_adaptive == "client"
        assert configured_reasoning_adaptive() == "client"

        # on: emits adaptive + mapped output_config.effort even for ON(effort=MAX)
        monkeypatch.setenv("REASONING_ADAPTIVE", "on")
        get_settings.cache_clear()
        assert configured_reasoning_adaptive() == "on"
        on_body = build_anthropic_messages_body(
            req_adaptive, reasoning=ReasoningPolicy.on(effort=ReasoningEffort.MAX)
        )
        assert on_body["thinking"] == {"type": "adaptive"}
        assert on_body["output_config"] == {"effort": "max"}

        # off: forces manual enabled+budget on Anthropic instead of adaptive
        monkeypatch.setenv("REASONING_ADAPTIVE", "off")
        get_settings.cache_clear()
        assert configured_reasoning_adaptive() == "off"
        off_adaptive = build_anthropic_messages_body(
            req_adaptive,
            reasoning=ReasoningPolicy(
                control=ReasoningControl.ADAPTIVE, effort=ReasoningEffort.HIGH
            ),
        )
        assert off_adaptive["thinking"]["type"] == "enabled"
        assert "output_config" not in off_adaptive

        off_passthrough = build_anthropic_messages_body(
            req_adaptive, reasoning=ReasoningPolicy.provider_default()
        )
        assert off_passthrough["thinking"]["type"] == "enabled"

        # Responses & ChatGPT OAuth respect REASONING_ADAPTIVE=off on client adaptive ask
        resp_body = build_responses_request_body(
            req_adaptive, reasoning=ReasoningPolicy.adaptive()
        )
        assert resp_body["reasoning"]["effort"] == "medium"

        oauth_body = build_chatgpt_oauth_request_body(
            req_adaptive, reasoning=ReasoningPolicy.adaptive()
        )
        assert oauth_body["reasoning"]["effort"] in {
            effort.value for effort in ReasoningEffort
        }

        # Policy OFF still disables even when REASONING_ADAPTIVE=on
        monkeypatch.setenv("REASONING_ADAPTIVE", "on")
        get_settings.cache_clear()
        still_off = build_anthropic_messages_body(
            req_adaptive, reasoning=ReasoningPolicy.off()
        )
        assert "thinking" not in still_off
        assert "output_config" not in still_off
    finally:
        monkeypatch.delenv("REASONING_ADAPTIVE", raising=False)
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# PR4 (T2-Q1 Option A): Client adaptive preservation & per-door explicit effort
# ---------------------------------------------------------------------------


def test_pr4_anthropic_client_adaptive_with_effort_and_legacy_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fmt = {"type": "json_schema", "schema": {"type": "object"}}
    req = _anthropic_request(
        model="claude-opus-4-7",
        thinking={"type": "adaptive", "display": "summarized"},
        output_config={"effort": "high", "format": fmt},
    )
    policy = resolve_reasoning_policy(req, ReasoningPreference.CLIENT)
    assert policy == ReasoningPolicy(
        control=ReasoningControl.ADAPTIVE,
        effort=ReasoningEffort.HIGH,
        budget_tokens=None,
    )

    # Adaptive-capable model (Opus 4.7): preserves thinking.type="adaptive",
    # display="summarized", output_config.effort="high", and output_config.format.
    adaptive_body = build_anthropic_messages_body(
        req,
        reasoning=policy,
        forward_thinking_display=True,
        adaptive_capable=True,
    )
    assert adaptive_body["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert adaptive_body["output_config"] == {"effort": "high", "format": fmt}

    # Legacy non-adaptive model (Haiku 4.5 / adaptive_capable=False): falls back
    # to manual enabled + budget_tokens=2048 and strips output_config.effort.
    haiku_req = _anthropic_request(
        model="claude-haiku-4-5-20251001",
        thinking={"type": "adaptive"},
        output_config={"effort": "high", "format": fmt},
    )
    haiku_policy = resolve_reasoning_policy(haiku_req, ReasoningPreference.CLIENT)
    legacy_body = build_anthropic_messages_body(
        haiku_req,
        reasoning=haiku_policy,
        adaptive_capable=False,
    )
    assert legacy_body["thinking"] == {"type": "enabled", "budget_tokens": 2048}
    assert legacy_body["output_config"] == {"format": fmt}

    # With REASONING_ADAPTIVE="off": falls back to enabled + budget_tokens=2048
    # and strips output_config.effort even when adaptive_capable is True.
    monkeypatch.setenv("REASONING_ADAPTIVE", "off")
    get_settings.cache_clear()
    try:
        off_body = build_anthropic_messages_body(
            req,
            reasoning=policy,
            adaptive_capable=True,
        )
        assert off_body["thinking"] == {"type": "enabled", "budget_tokens": 2048}
        assert off_body["output_config"] == {"format": fmt}
    finally:
        monkeypatch.delenv("REASONING_ADAPTIVE", raising=False)
        get_settings.cache_clear()


def test_pr4_non_anthropic_doors_client_adaptive_bare_and_explicit_effort() -> None:
    req_bare = _anthropic_request(
        model="gpt-5",
        thinking={"type": "adaptive"},
    )
    policy_bare = resolve_reasoning_policy(req_bare, ReasoningPreference.CLIENT)
    assert policy_bare == ReasoningPolicy.adaptive()

    req_low = _anthropic_request(
        model="gpt-5",
        thinking={"type": "adaptive"},
        output_config={"effort": "low"},
    )
    policy_low = resolve_reasoning_policy(req_low, ReasoningPreference.CLIENT)
    assert policy_low == ReasoningPolicy.adaptive(effort=ReasoningEffort.LOW)

    req_high = _anthropic_request(
        model="gpt-5",
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
    )
    policy_high = resolve_reasoning_policy(req_high, ReasoningPreference.CLIENT)
    assert policy_high == ReasoningPolicy.adaptive(effort=ReasoningEffort.HIGH)

    # 1. NamedEffortReasoning (OpenAI Chat / groq / opencode / fireworks / vercel / llamacpp)
    for profile_id, expected_top in (
        ("groq", "high"),
        ("opencode", "max"),
        ("fireworks", "high"),
    ):
        chat_bare: dict[str, Any] = {"model": "m", "messages": []}
        OPENAI_CHAT_PROFILES[profile_id].reasoning.encode(chat_bare, policy_bare)
        assert chat_bare["reasoning_effort"] == expected_top, profile_id

        chat_low: dict[str, Any] = {"model": "m", "messages": []}
        OPENAI_CHAT_PROFILES[profile_id].reasoning.encode(chat_low, policy_low)
        assert chat_low["reasoning_effort"] == "low", profile_id

    vercel_low: dict[str, Any] = {"model": "m", "messages": []}
    OPENAI_CHAT_PROFILES["vercel"].reasoning.encode(vercel_low, policy_low)
    assert vercel_low["extra_body"]["reasoning"] == {"effort": "low"}

    llama_low: dict[str, Any] = {"model": "m", "messages": []}
    OPENAI_CHAT_PROFILES["llamacpp"].reasoning.encode(llama_low, policy_low)
    assert llama_low["extra_body"]["thinking_budget_tokens"] == 1024

    # 2. MistralProvider: bare adaptive -> top rung ("max") + SUBSTITUTED note;
    # explicit effort="low" -> honors "low" without forcing "max".
    mistral = MistralProvider(
        ProviderConfig(api_key="sk-test", base_url="https://api.mistral.ai/v1"),
        rate_limiter=passthrough_rate_limiter(),
    )
    trace_mistral_bare = install_wire_trace()
    mistral_bare = mistral._build_request_body(req_bare, reasoning=policy_bare)
    assert mistral_bare["reasoning_effort"] == "max"
    assert len(trace_mistral_bare.reasoning_adaptations) == 1

    trace_mistral_low = install_wire_trace()
    mistral_low = mistral._build_request_body(req_low, reasoning=policy_low)
    assert mistral_low["reasoning_effort"] == "low"
    assert trace_mistral_low.reasoning_adaptations == []

    # 3. Responses / build_responses_request_body:
    # - bare adaptive (effort=None): sends "medium" and records SUBSTITUTED adaptation note.
    # - adaptive + effort="low" or "high": sends explicit "low" / "high" and records NO bogus "medium" note.
    trace_resp_bare = install_wire_trace()
    resp_bare = build_responses_request_body(req_bare, reasoning=policy_bare)
    assert resp_bare["reasoning"]["effort"] == "medium"
    assert len(trace_resp_bare.reasoning_adaptations) == 1
    assert "medium" in (trace_resp_bare.reasoning_adaptations[0].message or "")

    trace_resp_low = install_wire_trace()
    resp_low = build_responses_request_body(req_low, reasoning=policy_low)
    assert resp_low["reasoning"]["effort"] == "low"
    assert trace_resp_low.reasoning_adaptations == []

    trace_resp_high = install_wire_trace()
    resp_high = build_responses_request_body(req_high, reasoning=policy_high)
    assert resp_high["reasoning"]["effort"] == "high"
    assert trace_resp_high.reasoning_adaptations == []

    # 4. GeminiProvider & VertexReasoningEncoder:
    gemini = GeminiProvider(
        ProviderConfig(
            api_key="sk-test",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        ),
        rate_limiter=passthrough_rate_limiter(),
    )
    gemini_bare = gemini._build_request_body(req_bare, reasoning=policy_bare)
    assert gemini_bare["reasoning_effort"] == "high"
    gemini_low = gemini._build_request_body(req_low, reasoning=policy_low)
    assert gemini_low["reasoning_effort"] == "low"

    vertex_bare: dict[str, Any] = {"model": "gemini-2.5-pro", "messages": []}
    VertexReasoningEncoder().encode(vertex_bare, policy_bare)
    assert vertex_bare["extra_body"]["extra_body"]["google"]["thinking_config"] == {
        "include_thoughts": True
    }
    vertex_low: dict[str, Any] = {"model": "gemini-2.5-pro", "messages": []}
    VertexReasoningEncoder().encode(vertex_low, policy_low)
    assert vertex_low["extra_body"]["extra_body"]["google"]["thinking_config"] == {
        "thinking_budget": 1024,
        "include_thoughts": True,
    }

    # 5.Mixed adaptive + explicit budget_tokens stays ON(budget_tokens=4096)
    req_mixed = _anthropic_request(
        thinking={"type": "adaptive", "budget_tokens": 4096},
    )
    assert client_reasoning_policy(req_mixed) == ReasoningPolicy.on(budget_tokens=4096)


@pytest.mark.asyncio
async def test_pr4_anthropic_messages_provider_recovers_from_adaptive_rejection_to_manual_budget() -> (
    None
):
    seen_bodies: list[dict[str, Any]] = []
    sse = (
        b"event: message_start\n"
        b'data: {"type":"message_start","message":{"id":"msg_1","type":"message","role":"assistant","content":[],"model":"claude-haiku-4-5-20251001","stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":10,"output_tokens":1}}}\n\n'
        b"event: content_block_start\n"
        b'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
        b"event: content_block_delta\n"
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}\n\n'
        b"event: content_block_stop\n"
        b'data: {"type":"content_block_stop","index":0}\n\n'
        b"event: message_delta\n"
        b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"output_tokens":2}}\n\n'
        b"event: message_stop\n"
        b'data: {"type":"message_stop"}\n\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen_bodies.append(body)
        thinking = body.get("thinking") or {}
        if thinking.get("type") == "adaptive":
            return httpx.Response(
                400,
                json={
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "message": "thinking.type: 'adaptive' thinking is not supported for claude-haiku-4-5-20251001",
                    },
                },
                request=request,
            )

        async def _sse_body():
            yield sse

        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse_body(),
        )

    provider = AnthropicMessagesProvider(
        ProviderConfig(api_key="sk-ant-test", base_url="https://api.anthropic.com"),
        provider_name="ANTHROPIC",
        rate_limiter=ImmediateRetryProviderRateLimiter(),
        provider_id="anthropic",
    )
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    req = _anthropic_request(
        model="claude-haiku-4-5-20251001",
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
    )
    policy = resolve_reasoning_policy(req, ReasoningPreference.CLIENT)
    try:
        install_wire_trace()
        async for _ev in provider.stream_response(
            req,
            input_tokens=10,
            reasoning=policy,
        ):
            pass
        assert len(seen_bodies) == 2
        assert seen_bodies[0]["thinking"] == {"type": "adaptive"}
        assert seen_bodies[0]["output_config"] == {"effort": "high"}
        assert seen_bodies[1]["thinking"] == {"type": "enabled", "budget_tokens": 2048}
        assert "output_config" not in seen_bodies[1]
        assert provider.reasoning_dialect("claude-haiku-4-5-20251001").adaptive is False

        # Subsequent request uses adaptive_capable=False up-front without extra 400!
        seen_bodies.clear()
        async for _ev in provider.stream_response(
            req,
            input_tokens=10,
            reasoning=policy,
        ):
            pass
        assert len(seen_bodies) == 1
        assert seen_bodies[0]["thinking"] == {"type": "enabled", "budget_tokens": 2048}
        assert "output_config" not in seen_bodies[0]
    finally:
        await provider._client.aclose()
