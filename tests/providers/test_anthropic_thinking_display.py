"""Claude Code's choice of how thinking is shown reaches Anthropic (7.102.5).

Claude Code asks Anthropic how to show the thinking it does with
``thinking.display`` -- ``"summarized"`` when the user turned on
``showThinkingSummaries``, ``"omitted"``, or ``"updates"``. ``ThinkingConfig``
never declared the key, so it was dropped at parse time: through MCC, turning
the setting on changed nothing. On Claude 5 Anthropic's own default is
``omitted``, so the thinking stayed hidden however the user asked.

Pinned here:

* the field parses, and is excluded from every dump -- so the request log,
  every other dialect and every body that dumps the request are unchanged;
* only the Claude-subscription provider puts it back on the outgoing
  ``thinking``, on whatever object goes out (passed through or MCC-built) as
  long as it asks for thinking; no client choice means no key, ever;
* the Console API-key provider and the custom/OpenCode Messages door never
  send it.
"""

import json
from typing import Any

import httpx
import pytest

from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.reasoning import ReasoningPolicy
from my_claude_code.providers.anthropic import AnthropicProvider
from my_claude_code.providers.anthropic_messages.request import (
    build_anthropic_messages_body,
)
from my_claude_code.providers.anthropic_oauth import (
    AnthropicOAuthAuth,
    AnthropicOAuthProvider,
    OAuthTokens,
)
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.openai_chat.messages_transport import MessagesTransport
from my_claude_code.providers.rate_limit import ProviderRateLimiter
from tests.providers.support import passthrough_rate_limiter

CLI_MARKER = "x-anthropic-billing-header: cc_version=2.1.304; cc_entrypoint=cli;"
FOREIGN = {"type": "thinking", "thinking": "agnes planned it", "signature": ""}
TOOL_USE = {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"p": "a"}}
TOOL_RESULT = {"type": "tool_result", "tool_use_id": "toolu_1", "content": "x"}


def _request(
    thinking: dict[str, Any] | None,
    messages: list[dict[str, Any]] | None = None,
) -> MessagesRequest:
    payload: dict[str, Any] = {
        "model": "claude-haiku-5-5",
        "max_tokens": 16000,
        "system": f"{CLI_MARKER}\nYou are Claude Code.",
        "messages": messages or [{"role": "user", "content": "hi"}],
    }
    if thinking is not None:
        payload["thinking"] = thinking
    return MessagesRequest.model_validate(payload)


def _thinking(
    thinking: dict[str, Any] | None,
    policy: ReasoningPolicy,
    *,
    forward: bool = True,
    messages: list[dict[str, Any]] | None = None,
) -> Any:
    body = build_anthropic_messages_body(
        _request(thinking, messages),
        reasoning=policy,
        forward_thinking_display=forward,
    )
    return body.get("thinking", "<absent>")


# --------------------------------------------------------------------------
# The field
# --------------------------------------------------------------------------


def test_display_is_parsed_and_survives_a_deep_copy() -> None:
    request = _request({"type": "adaptive", "display": "summarized"})

    assert request.thinking is not None
    assert request.thinking.display == "summarized"
    copied = request.model_copy(deep=True).thinking
    assert copied is not None and copied.display == "summarized"


@pytest.mark.parametrize("mode", ["python", "json"])
def test_display_is_in_no_dump(mode: str) -> None:
    request = _request({"type": "adaptive", "display": "summarized"})

    for dumped in (
        request.model_dump(mode=mode),
        request.model_dump(mode=mode, exclude_none=True),
    ):
        assert "display" not in dumped["thinking"]


def test_a_display_the_client_got_wrong_is_ignored_not_refused() -> None:
    sent = _thinking(
        {"type": "adaptive", "display": 7}, ReasoningPolicy.provider_default()
    )

    assert sent == {"type": "adaptive"}


# --------------------------------------------------------------------------
# The builder, on the Claude-subscription path
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("client", "policy", "sent"),
    [
        (
            {"type": "adaptive", "display": "summarized"},
            ReasoningPolicy.provider_default(),
            {"type": "adaptive", "display": "summarized"},
        ),
        (
            {"type": "adaptive", "display": "updates"},
            ReasoningPolicy.on(),
            {"type": "adaptive", "display": "updates"},
        ),
        (
            {"type": "enabled", "budget_tokens": 4000, "display": "omitted"},
            ReasoningPolicy.provider_default(),
            {"type": "enabled", "budget_tokens": 4000, "display": "omitted"},
        ),
        (
            {"type": "adaptive", "display": "summarized"},
            ReasoningPolicy.adaptive(),
            {"type": "adaptive", "display": "summarized"},
        ),
        (
            {"type": "adaptive", "display": "summarized"},
            ReasoningPolicy.on(budget_tokens=2048),
            {"type": "enabled", "budget_tokens": 2048, "display": "summarized"},
        ),
    ],
    ids=["passed-through", "on-passed-through", "manual", "adaptive-tier", "budget"],
)
def test_the_clients_display_rides_on_whatever_thinking_goes_out(
    client: dict[str, Any], policy: ReasoningPolicy, sent: dict[str, Any]
) -> None:
    thinking = _thinking(client, policy)

    assert thinking == sent
    assert list(thinking) == list(sent)


@pytest.mark.parametrize(
    ("client", "policy", "sent"),
    [
        (
            {"type": "adaptive"},
            ReasoningPolicy.provider_default(),
            {"type": "adaptive"},
        ),
        (None, ReasoningPolicy.adaptive(), {"type": "adaptive"}),
        (
            {"type": "disabled", "display": "summarized"},
            ReasoningPolicy.provider_default(),
            {"type": "disabled"},
        ),
        (
            {"type": "adaptive", "display": "summarized"},
            ReasoningPolicy.off(),
            "<absent>",
        ),
        (
            {"type": "adaptive", "display": "   "},
            ReasoningPolicy.provider_default(),
            {"type": "adaptive"},
        ),
    ],
    ids=["no-choice", "mcc-built-no-choice", "disabled", "off", "blank"],
)
def test_mcc_never_picks_a_display_of_its_own(
    client: dict[str, Any] | None, policy: ReasoningPolicy, sent: Any
) -> None:
    assert _thinking(client, policy) == sent


def test_a_manual_tool_follow_up_that_drops_the_field_drops_the_display_too() -> None:
    history = [
        {"role": "user", "content": "Read a.py"},
        {"role": "assistant", "content": [FOREIGN, TOOL_USE]},
        {"role": "user", "content": [TOOL_RESULT]},
    ]
    client = {"type": "enabled", "budget_tokens": 4000, "display": "summarized"}

    assert _thinking(client, ReasoningPolicy.provider_default(), messages=history) == (
        "<absent>"
    )


@pytest.mark.parametrize(
    "policy",
    [
        ReasoningPolicy.provider_default(),
        ReasoningPolicy.on(),
        ReasoningPolicy.adaptive(),
        ReasoningPolicy.on(budget_tokens=2048),
    ],
)
def test_without_the_flag_the_body_is_the_one_without_a_display(
    policy: ReasoningPolicy,
) -> None:
    with_choice = build_anthropic_messages_body(
        _request({"type": "adaptive", "display": "summarized"}), reasoning=policy
    )
    without = build_anthropic_messages_body(
        _request({"type": "adaptive"}), reasoning=policy
    )

    assert json.dumps(with_choice) == json.dumps(without)


# --------------------------------------------------------------------------
# Which providers send it
# --------------------------------------------------------------------------

_SSE = (
    b'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_1","usage":{}}}\n\n'
    b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
)


def _config() -> ProviderConfig:
    return ProviderConfig(
        api_key="",
        base_url="https://upstream.invalid/v1",
        rate_limit=100,
        rate_window=60,
        max_concurrency=5,
        retry_attempts=1,
        early_retry_attempts=1,
        commit_holdback_seconds=0,
    )


def _capture(messages_provider: Any) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, content=_SSE)

    messages_provider._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )
    return sent


def _limiter() -> ProviderRateLimiter:
    return ProviderRateLimiter(
        rate_limit=100, rate_window=60, max_concurrency=5, max_retries=0
    )


@pytest.mark.asyncio
async def test_the_claude_subscription_sends_the_clients_display() -> None:
    provider = AnthropicOAuthProvider(
        _config(),
        rate_limiter=_limiter(),
        auth=AnthropicOAuthAuth(
            OAuthTokens(access_token="sk-ant-oat01-test", source="test")
        ),
    )
    sent = _capture(provider._messages)

    [
        frame
        async for frame in provider.stream_response(
            _request({"type": "adaptive", "display": "summarized"})
        )
    ]
    await provider.cleanup()

    assert sent[0]["thinking"] == {"type": "adaptive", "display": "summarized"}


@pytest.mark.asyncio
async def test_the_console_api_key_provider_does_not() -> None:
    config = _config()
    provider = AnthropicProvider(config, rate_limiter=_limiter())
    sent = _capture(provider._messages)

    [
        frame
        async for frame in provider.stream_response(
            _request({"type": "adaptive", "display": "summarized"})
        )
    ]
    await provider.cleanup()

    assert sent[0]["thinking"] == {"type": "adaptive"}


def test_the_custom_and_opencode_messages_door_does_not() -> None:
    transport = MessagesTransport(
        ProviderConfig(api_key="sk-test", base_url="https://upstream.invalid/v1"),
        base_url="https://upstream.invalid/v1",
        provider_name="CUSTOM",
        provider_id="custom_host",
        identity=None,
        api_key="sk-test",
        rate_limiter=passthrough_rate_limiter(),
    )

    assert transport._provider._forward_thinking_display is False
