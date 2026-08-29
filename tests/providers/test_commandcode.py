"""Protocol-faithful tests for the Command Code Provider API."""

import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from my_claude_code.application.errors import InvalidRequestError
from my_claude_code.config.model_overrides import ModelParameterOverrides
from my_claude_code.config.provider_catalog import (
    COMMANDCODE_DEFAULT_BASE,
    PROVIDER_CATALOG,
)
from my_claude_code.core.anthropic import ReasoningReplayMode
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.core.reasoning import ReasoningEffort, ReasoningPolicy
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.commandcode import (
    CommandCodeProvider,
    extract_commandcode_model_infos,
    is_anthropic_messages_model,
)
from my_claude_code.providers.commandcode.client import _PROFILE as _COMMANDCODE_PROFILE
from my_claude_code.providers.model_listing import ModelListResponseError
from my_claude_code.providers.openai_chat.profiles import OPENAI_CHAT_PROFILES
from my_claude_code.providers.openai_chat.request_policy import (
    build_openai_chat_request_body,
)
from my_claude_code.providers.rate_limit import ProviderRateLimiter
from my_claude_code.providers.runtime.factory import create_provider
from my_claude_code.providers.runtime.rotating import RotatingProvider


def _provider() -> CommandCodeProvider:
    config = ProviderConfig(
        api_key="user_secret_commandcode_key",
        base_url=COMMANDCODE_DEFAULT_BASE,
        rate_limit=100,
        rate_window=60,
        max_concurrency=5,
        retry_attempts=1,
        early_retry_attempts=1,
        commit_holdback_seconds=0,
    )
    provider = CommandCodeProvider(
        config,
        rate_limiter=ProviderRateLimiter(
            rate_limit=100,
            rate_window=60,
            max_concurrency=5,
            max_retries=0,
        ),
    )
    return provider


def _request(model: str) -> MessagesRequest:
    return MessagesRequest(
        model=model,
        max_tokens=32,
        messages=[Message(role="user", content="ping")],
        stream=True,
    )


def test_catalog_descriptor_and_base_url() -> None:
    descriptor = PROVIDER_CATALOG["commandcode"]

    assert COMMANDCODE_DEFAULT_BASE == "https://api.commandcode.ai/provider/v1"
    assert descriptor.credential_env == "COMMANDCODE_API_KEY"
    assert descriptor.credential_attr == "commandcode_api_key"
    assert descriptor.proxy_attr == "commandcode_proxy"
    assert descriptor.group == "gateway"


def test_factory_preserves_key_pool_rotation_and_proxy(monkeypatch) -> None:
    from my_claude_code.config.settings import Settings

    monkeypatch.setenv("COMMANDCODE_API_KEY_ROTATION", "round_robin")
    settings = Settings.model_validate(
        {
            "COMMANDCODE_API_KEY": "key-one,key-two",
            "COMMANDCODE_PROXY": "http://proxy.test:8080",
            "MESSAGING_PLATFORM": "none",
        }
    )

    provider = create_provider("commandcode", settings)

    assert isinstance(provider, RotatingProvider)
    assert len(provider._providers) == 2
    assert all(isinstance(item, CommandCodeProvider) for item in provider._providers)
    assert [item._config.api_key for item in provider._providers] == [
        "key-one",
        "key-two",
    ]
    assert all(
        item._config.proxy == "http://proxy.test:8080" for item in provider._providers
    )
    assert provider._state.policy == "round_robin"


def test_protocol_classifier_is_narrow_and_case_insensitive() -> None:
    assert is_anthropic_messages_model("claude-sonnet-5")
    assert is_anthropic_messages_model(" CLAUDE-OPUS-5 ")
    assert not is_anthropic_messages_model("anthropic/claude-sonnet-5")
    assert not is_anthropic_messages_model("deepseek/deepseek-v4-flash")


def test_model_catalog_preserves_context_metadata() -> None:
    infos = extract_commandcode_model_infos(
        {
            "object": "list",
            "data": [
                {"id": "claude-sonnet-5", "context_length": 1_000_000},
                {
                    "id": "deepseek/deepseek-v4-flash",
                    "context_length": 1_000_000,
                },
            ],
        },
        provider_name="COMMANDCODE",
    )

    assert {(item.model_id, item.context_length) for item in infos} == {
        ("claude-sonnet-5", 1_000_000),
        ("deepseek/deepseek-v4-flash", 1_000_000),
    }
    assert all(item.supports_thinking is None for item in infos)


@pytest.mark.parametrize(
    "payload",
    [
        {"data": "wrong"},
        {"data": []},
        {"data": [{"id": "", "context_length": 100}]},
        {"data": [{"id": "model", "context_length": 0}]},
    ],
)
def test_model_catalog_rejects_malformed_payload(payload: object) -> None:
    with pytest.raises(ModelListResponseError, match="COMMANDCODE model-list"):
        extract_commandcode_model_infos(payload, provider_name="COMMANDCODE")


@pytest.mark.asyncio
async def test_list_model_infos_uses_live_openai_models_endpoint() -> None:
    provider = _provider()
    provider._openai._client.models.list = AsyncMock(
        return_value={
            "data": [
                {"id": "claude-sonnet-5", "context_length": 1_000_000},
                {"id": "gpt-5.6-sol", "context_length": 1_050_000},
            ]
        }
    )
    try:
        infos = await provider.list_model_infos()
    finally:
        await provider.cleanup()

    assert {item.model_id for item in infos} == {"claude-sonnet-5", "gpt-5.6-sol"}


@pytest.mark.asyncio
async def test_claude_models_use_native_messages_with_bearer_auth() -> None:
    captured_path = ""
    captured_authorization: str | None = None
    captured_body: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_path, captured_authorization, captured_body
        captured_path = request.url.path
        captured_authorization = request.headers.get("authorization")
        parsed_body = json.loads(request.content)
        assert isinstance(parsed_body, dict)
        captured_body = parsed_body
        frames = [
            {
                "type": "message_start",
                "message": {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": "claude-sonnet-5",
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 3, "output_tokens": 1},
                },
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "pong"},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 2},
            },
            {"type": "message_stop"},
        ]
        content = "".join(
            f"event: {item['type']}\ndata: {json.dumps(item)}\n\n" for item in frames
        )
        return httpx.Response(200, text=content)

    provider = _provider()
    original_client = provider._anthropic._client
    provider._anthropic._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        timeout=30,
    )
    try:
        events = [
            event
            async for event in provider.stream_response(
                _request("claude-sonnet-5"),
                reasoning=ReasoningPolicy.provider_default(),
            )
        ]
    finally:
        await provider.cleanup()
        await original_client.aclose()

    assert captured_path == "/provider/v1/messages"
    assert captured_authorization == "Bearer user_secret_commandcode_key"
    assert captured_body["stream"] is True
    assert captured_body["model"] == "claude-sonnet-5"
    assert any('"text":"pong"' in event for event in events)


@pytest.mark.asyncio
async def test_non_claude_models_delegate_to_chat_completions() -> None:
    provider = _provider()
    provider._openai.stream_response = MagicMock()
    sentinel = _events("openai")
    provider._openai.stream_response.return_value = sentinel
    request = _request("deepseek/deepseek-v4-flash")

    stream = provider.stream_response(request)

    assert stream is sentinel
    provider._openai.stream_response.assert_called_once()
    await provider.cleanup()


async def _events(value: str) -> AsyncIterator[str]:
    yield value


def _chat_body(model: str, reasoning: ReasoningPolicy, **overrides: object) -> dict:
    data: dict[str, object] = {
        "model": model,
        "max_tokens": 32000,
        "messages": [{"role": "user", "content": "ping"}],
        "stream": True,
    }
    data.update(overrides)
    return build_openai_chat_request_body(
        MessagesRequest.model_validate(data),
        reasoning=reasoning,
        policy=_COMMANDCODE_PROFILE.request_policy,
        postprocessors=_COMMANDCODE_PROFILE.request_postprocessors,
        provider_id="commandcode",
        overrides=ModelParameterOverrides(),
    )


def test_effort_max_sends_the_gateway_effort_enum() -> None:
    """The probed dialect: a top-level ``reasoning_effort`` string, nothing else."""
    body = _chat_body(
        "minimax/minimax-m3-free",
        ReasoningPolicy.on(effort=ReasoningEffort.MAX),
    )

    assert body["reasoning_effort"] == "max"
    # The gateway parses none of these: it returns 200 and silently discards
    # them, so emitting one would look like reasoning was requested when it
    # was not.
    assert "reasoning" not in body
    assert "thinking" not in body
    assert "chat_template_kwargs" not in body.get("extra_body", {})


@pytest.mark.parametrize(
    ("effort", "wire"),
    [
        (ReasoningEffort.MINIMAL, "low"),
        (ReasoningEffort.LOW, "low"),
        (ReasoningEffort.MEDIUM, "medium"),
        (ReasoningEffort.HIGH, "high"),
        (ReasoningEffort.XHIGH, "xhigh"),
        (ReasoningEffort.MAX, "max"),
    ],
)
def test_every_effort_encodes_inside_the_published_enum(
    effort: ReasoningEffort, wire: str
) -> None:
    """The gateway 400s on anything outside low|medium|high|xhigh|max."""
    body = _chat_body("z-ai/glm-5.3-flash", ReasoningPolicy.on(effort=effort))

    assert body["reasoning_effort"] == wire
    assert wire in {"low", "medium", "high", "xhigh", "max"}


def test_effort_clamped_to_a_model_vocabulary_still_encodes() -> None:
    """The 5.68.1 resolution ladder can hand back a clamped effort."""
    for effort in (ReasoningEffort.LOW, ReasoningEffort.MEDIUM, ReasoningEffort.HIGH):
        body = _chat_body("minimax/minimax-m3-free", ReasoningPolicy.on(effort=effort))
        assert body["reasoning_effort"] == effort.value


def test_reasoning_off_sends_no_reasoning_field() -> None:
    """Command Code publishes no 'off' rung; "none" and "minimal" both 400."""
    body = _chat_body("z-ai/glm-5.3-flash", ReasoningPolicy.off())

    assert "reasoning_effort" not in body
    assert "reasoning" not in body
    assert "thinking" not in body


def test_reasoning_on_without_an_effort_names_the_strongest_rung() -> None:
    """Bare requests reason LESS here, so a level-less "on" must name a rung.

    This test asserted the opposite until 5.71.0, on the 5.69.0 belief that a
    bare request already reasons the most. A live A/B on 2026-08-29 refuted it
    -- identical prompt at ``max_tokens: 3000``, both HTTP 200:
    ``deepseek/deepseek-v4-flash`` returned 132 reasoning tokens bare against
    1,046 under ``reasoning_effort: "max"``, and ``xiaomi/mimo-v2.5`` 7 against
    17. Without an on-value this encoder emitted nothing at all for the policy
    per-model gating produces most often here (control ON, effort discarded).
    """
    body = _chat_body("z-ai/glm-5.3-flash", ReasoningPolicy.on())

    assert body["reasoning_effort"] == "max"


def test_caller_extra_body_reaches_the_gateway_but_cannot_forge_reasoning() -> None:
    body = _chat_body(
        "z-ai/glm-5.3-flash",
        ReasoningPolicy.on(effort=ReasoningEffort.HIGH),
        extra_body={"provider_hint": "commandcode"},
    )
    assert body["extra_body"] == {"provider_hint": "commandcode"}

    with pytest.raises(InvalidRequestError):
        _chat_body(
            "z-ai/glm-5.3-flash",
            ReasoningPolicy.on(effort=ReasoningEffort.HIGH),
            extra_body={"reasoning_effort": "max"},
        )


def test_reasoning_is_read_and_replayed_on_the_field_it_arrives_on() -> None:
    """The gateway streams ``reasoning`` deltas, never ``<think>`` tags."""
    assert _COMMANDCODE_PROFILE.reasoning_delta_field == "reasoning"
    assert (
        _COMMANDCODE_PROFILE.request_policy.reasoning_replay
        is ReasoningReplayMode.REASONING
    )

    delta = SimpleNamespace(reasoning="thought", reasoning_content=None)
    assert _COMMANDCODE_PROFILE.reasoning_delta(delta) == "thought"

    body = _chat_body(
        "z-ai/glm-5.3-flash",
        ReasoningPolicy.on(effort=ReasoningEffort.HIGH),
        messages=[
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "earlier thought"},
                    {"type": "text", "text": "earlier answer"},
                ],
            },
            {"role": "user", "content": "again"},
        ],
    )
    assistant = next(m for m in body["messages"] if m["role"] == "assistant")
    assert assistant["reasoning"] == "earlier thought"
    assert "reasoning_content" not in assistant
    assert "<think>" not in json.dumps(assistant)


def _profile_body(name: str, reasoning: ReasoningPolicy | None = None) -> dict:
    profile = OPENAI_CHAT_PROFILES[name]
    return build_openai_chat_request_body(
        MessagesRequest.model_validate(
            {
                "model": "some-model",
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "ping"}],
            }
        ),
        reasoning=reasoning or ReasoningPolicy.on(effort=ReasoningEffort.MAX),
        policy=profile.request_policy,
        postprocessors=profile.request_postprocessors,
        provider_id=name,
        overrides=ModelParameterOverrides(),
    )


def test_untouched_providers_keep_their_own_reasoning_shape() -> None:
    """Regression guard: this change is scoped to Command Code alone.

    Command Code's own contribution was its *probed on-value*: a rung named
    when the policy names none. That is what must not spread. Since 6.5.0
    ``bedrock`` and ``cline`` do carry the standard field -- like every
    OpenAI-compatible host -- but they must never gain an on-value, because
    nobody probed one for them.
    """
    for name in ("bedrock", "cline"):
        assert "reasoning_effort" not in _profile_body(name, ReasoningPolicy.on())
        body = _profile_body(name)
        assert "reasoning" not in body
        assert "reasoning" not in body.get("extra_body", {})

    # Ollama: its own named-effort vocabulary, which tops out at "max".
    assert _profile_body("ollama")["reasoning_effort"] == "max"
    # Zenmux: an extra_body reasoning *object*, which Command Code's gateway
    # would accept and silently discard.
    assert _profile_body("zenmux")["extra_body"]["reasoning"] == {"effort": "xhigh"}
    assert "reasoning_effort" not in _profile_body("zenmux")
