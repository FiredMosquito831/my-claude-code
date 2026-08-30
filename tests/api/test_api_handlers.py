import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.responses import JSONResponse, StreamingResponse

from my_claude_code.api.handlers import (
    MessagesHandler,
    ResponsesHandler,
    TokenCountHandler,
)
from my_claude_code.api.optimization_handlers import LocalOptimization
from my_claude_code.application.errors import InvalidRequestError
from my_claude_code.application.route_health import RouteHealthRegistry
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic.models import (
    ContentBlockText,
    Message,
    MessagesRequest,
    MessagesResponse,
    TokenCountRequest,
    Usage,
)
from my_claude_code.core.anthropic.streaming import format_sse_event
from my_claude_code.core.failures import ExecutionFailure, FailureKind
from my_claude_code.core.openai_responses import OpenAIResponsesRequest
from my_claude_code.core.reasoning import ReasoningPolicy

_CLASSIFIER_SYSTEM = (
    "You are a security monitor. Respond with <block>yes</block> or <block>no</block>."
)
_CLASSIFIER_USER = (
    "<transcript>\nUser: review the repo\nWebFetch https://example.com: fetch\n"
    "</transcript>\n<block> immediately."
)


class FakeProvider:
    def __init__(self, events: list[str] | None = None) -> None:
        self.preflight_calls: list[tuple[MessagesRequest, ReasoningPolicy]] = []
        self.requests: list[MessagesRequest] = []
        self.stream_kwargs: list[dict[str, Any]] = []
        self.events = events or [
            'event: message_start\ndata: {"type":"message_start"}\n\n',
            'event: message_stop\ndata: {"type":"message_stop"}\n\n',
        ]

    def throttle_remaining(self) -> float:
        return 0.0

    @property
    def credential_label(self) -> str | None:
        return None

    def preflight_stream(
        self, request: MessagesRequest, *, reasoning: ReasoningPolicy
    ) -> None:
        self.preflight_calls.append((request, reasoning))

    async def cleanup(self) -> None:
        return None

    async def list_model_ids(self) -> frozenset[str]:
        return frozenset({"test-model"})

    async def stream_response(
        self,
        request: MessagesRequest,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        reasoning: ReasoningPolicy,
    ) -> AsyncIterator[str]:
        self.requests.append(request)
        self.stream_kwargs.append(
            {
                "input_tokens": input_tokens,
                "request_id": request_id,
                "reasoning": reasoning,
            }
        )
        for event in self.events:
            yield event


async def _streaming_body_text(response: StreamingResponse) -> str:
    parts: list[str] = []
    async for chunk in response.body_iterator:
        if isinstance(chunk, bytes):
            parts.append(chunk.decode("utf-8"))
        else:
            parts.append(str(chunk))
    return "".join(parts)


def _json_response_content(response: JSONResponse) -> dict[str, Any]:
    content = json.loads(bytes(response.body).decode("utf-8"))
    assert isinstance(content, dict)
    return content


def _trace_events(trace_mock: MagicMock, event: str) -> list[dict[str, Any]]:
    return [
        dict(call.kwargs)
        for call in trace_mock.call_args_list
        if call.kwargs.get("event") == event
    ]


@pytest.mark.asyncio
async def test_messages_handler_passes_routed_request_and_stream_metadata() -> None:
    provider = FakeProvider()
    handler = MessagesHandler(Settings(), provider_resolver=lambda _: provider)
    request = MessagesRequest(
        model="nvidia_nim/test-model",
        max_tokens=100,
        stream=True,
        messages=[Message(role="user", content="hi")],
    )

    response = await handler.create(request)
    assert isinstance(response, StreamingResponse)

    body = await _streaming_body_text(response)
    assert "message_start" in body
    assert provider.requests[0].model == "test-model"
    assert provider.stream_kwargs[0]["input_tokens"] > 0
    assert provider.stream_kwargs[0]["request_id"].startswith("req_")
    assert provider.stream_kwargs[0]["reasoning"] == ReasoningPolicy.provider_default()
    assert len(provider.preflight_calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [True, False])
async def test_messages_handler_preflight_invalid_request_stays_http_error(
    stream: bool,
) -> None:
    class RejectPreflightProvider(FakeProvider):
        def preflight_stream(
            self,
            request: MessagesRequest,
            *,
            reasoning: ReasoningPolicy,
        ) -> None:
            raise InvalidRequestError("bad tool shape")

    provider = RejectPreflightProvider()
    handler = MessagesHandler(Settings(), provider_resolver=lambda _: provider)
    request = MessagesRequest(
        model="nvidia_nim/test-model",
        max_tokens=100,
        messages=[Message(role="user", content="hi")],
        stream=stream,
    )

    with pytest.raises(InvalidRequestError):
        await handler.create(request)


@pytest.mark.asyncio
async def test_messages_handler_aggregates_provider_stream_when_stream_false() -> None:
    provider = FakeProvider(
        [
            format_sse_event(
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg_test",
                        "type": "message",
                        "role": "assistant",
                        "content": [],
                        "model": "test-model",
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 7, "output_tokens": 1},
                    },
                },
            ),
            format_sse_event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
            ),
            format_sse_event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "OK"},
                },
            ),
            format_sse_event(
                "content_block_stop", {"type": "content_block_stop", "index": 0}
            ),
            format_sse_event(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"input_tokens": 7, "output_tokens": 2},
                },
            ),
            format_sse_event("message_stop", {"type": "message_stop"}),
        ]
    )
    handler = MessagesHandler(Settings(), provider_resolver=lambda _: provider)
    request = MessagesRequest(
        model="nvidia_nim/test-model",
        max_tokens=100,
        stream=False,
        messages=[Message(role="user", content="hi")],
    )

    response = await handler.create(request)

    assert isinstance(response, JSONResponse)
    assert response.headers["content-type"].startswith("application/json")
    body = _json_response_content(response)
    assert body["id"] == "msg_test"
    assert body["type"] == "message"
    assert body["role"] == "assistant"
    assert body["model"] == "test-model"
    assert body["content"] == [{"type": "text", "text": "OK"}]
    assert body["stop_reason"] == "end_turn"
    assert body["usage"] == {"input_tokens": 7, "output_tokens": 2}


@pytest.mark.asyncio
async def test_messages_handler_returns_error_json_for_stream_false_sse_error() -> None:
    provider = FakeProvider(
        [
            format_sse_event(
                "error",
                {
                    "type": "error",
                    "error": {"type": "api_error", "message": "upstream failed"},
                },
            )
        ]
    )
    handler = MessagesHandler(Settings(), provider_resolver=lambda _: provider)
    request = MessagesRequest(
        model="nvidia_nim/test-model",
        max_tokens=100,
        stream=False,
        messages=[Message(role="user", content="hi")],
    )

    response = await handler.create(request)

    assert isinstance(response, JSONResponse)
    assert response.status_code == 500
    assert response.headers["x-should-retry"] == "false"
    body = _json_response_content(response)
    assert body["type"] == "error"
    assert body["error"] == {"type": "api_error", "message": "upstream failed"}
    assert body["request_id"].startswith("req_")


@pytest.mark.asyncio
async def test_messages_handler_discards_partial_stream_false_output_on_error() -> None:
    provider = FakeProvider(
        [
            format_sse_event(
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg_partial",
                        "type": "message",
                        "role": "assistant",
                        "content": [],
                        "model": "test-model",
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                    },
                },
            ),
            format_sse_event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
            ),
            format_sse_event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "incomplete"},
                },
            ),
            format_sse_event(
                "error",
                {
                    "type": "error",
                    "error": {
                        "type": "overloaded_error",
                        "message": "provider overloaded",
                    },
                },
            ),
        ]
    )
    handler = MessagesHandler(Settings(), provider_resolver=lambda _: provider)
    request = MessagesRequest(
        model="nvidia_nim/test-model",
        max_tokens=100,
        stream=False,
        messages=[Message(role="user", content="hi")],
    )

    response = await handler.create(request)

    assert isinstance(response, JSONResponse)
    assert response.status_code == 529
    assert response.headers["x-should-retry"] == "false"
    body = _json_response_content(response)
    assert body["error"] == {
        "type": "overloaded_error",
        "message": "provider overloaded",
    }
    assert "content" not in body


@pytest.mark.asyncio
async def test_messages_handler_stream_false_provider_exception_keeps_status() -> None:
    class FailingProvider(FakeProvider):
        async def stream_response(
            self,
            request: Any,
            input_tokens: int = 0,
            *,
            request_id: str | None = None,
            reasoning: ReasoningPolicy,
        ) -> AsyncIterator[str]:
            self.requests.append(request)
            self.stream_kwargs.append(
                {
                    "input_tokens": input_tokens,
                    "request_id": request_id,
                    "reasoning": reasoning,
                }
            )
            raise ExecutionFailure(
                kind=FailureKind.RATE_LIMIT,
                status_code=429,
                message="upstream is busy",
                retryable=True,
            )
            yield "unreachable"

    provider = FailingProvider()
    handler = MessagesHandler(Settings(), provider_resolver=lambda _: provider)
    request = MessagesRequest(
        model="nvidia_nim/test-model",
        max_tokens=100,
        stream=False,
        messages=[Message(role="user", content="hi")],
    )

    response = await handler.create(request)

    assert isinstance(response, JSONResponse)
    assert response.status_code == 429
    assert response.headers["x-should-retry"] == "false"
    body = _json_response_content(response)
    assert body["error"] == {
        "type": "rate_limit_error",
        "message": "upstream is busy",
    }


@pytest.mark.asyncio
async def test_messages_handler_forces_no_thinking_for_safety_classifier() -> None:
    provider = FakeProvider()
    handler = MessagesHandler(Settings(), provider_resolver=lambda _: provider)
    request = MessagesRequest(
        model="nvidia_nim/test-model",
        max_tokens=100,
        stream=True,
        system=_CLASSIFIER_SYSTEM,
        messages=[Message(role="user", content=_CLASSIFIER_USER)],
    )

    with patch("my_claude_code.api.handlers.messages.trace_event") as trace_mock:
        response = await handler.create(request)
        assert isinstance(response, StreamingResponse)
        await _streaming_body_text(response)

    assert provider.preflight_calls[0][1] == ReasoningPolicy.off()
    assert provider.stream_kwargs[0]["reasoning"] == ReasoningPolicy.off()
    assert provider.requests[0].model == "test-model"
    assert provider.requests[0].system == _CLASSIFIER_SYSTEM
    assert _trace_events(
        trace_mock, "my_claude_code.api.optimization.safety_classifier_no_thinking"
    ) == [
        {
            "stage": "routing",
            "event": "my_claude_code.api.optimization.safety_classifier_no_thinking",
            "source": "api",
            "model": "test-model",
            "changed": True,
        }
    ]


@pytest.mark.asyncio
async def test_messages_handler_preserves_thinking_for_non_classifier() -> None:
    provider = FakeProvider()
    handler = MessagesHandler(Settings(), provider_resolver=lambda _: provider)
    request = MessagesRequest(
        model="nvidia_nim/test-model",
        max_tokens=100,
        stream=True,
        system="Explain XML formats.",
        messages=[
            Message(
                role="user",
                content=(
                    "Explain <transcript>...</transcript> and a <block> tag "
                    "without making a verdict."
                ),
            )
        ],
    )

    with patch("my_claude_code.api.handlers.messages.trace_event") as trace_mock:
        response = await handler.create(request)
        assert isinstance(response, StreamingResponse)
        await _streaming_body_text(response)

    assert provider.preflight_calls[0][1] == ReasoningPolicy.provider_default()
    assert provider.stream_kwargs[0]["reasoning"] == ReasoningPolicy.provider_default()
    assert (
        _trace_events(
            trace_mock,
            "my_claude_code.api.optimization.safety_classifier_no_thinking",
        )
        == []
    )


@pytest.mark.asyncio
async def test_messages_handler_keeps_existing_no_thinking_for_classifier() -> None:
    provider = FakeProvider()
    handler = MessagesHandler(Settings(), provider_resolver=lambda _: provider)
    request = MessagesRequest(
        model="claude-3-freecc-no-thinking/nvidia_nim/test-model",
        max_tokens=100,
        stream=True,
        system=_CLASSIFIER_SYSTEM,
        messages=[Message(role="user", content=_CLASSIFIER_USER)],
    )

    with patch("my_claude_code.api.handlers.messages.trace_event") as trace_mock:
        response = await handler.create(request)
        assert isinstance(response, StreamingResponse)
        await _streaming_body_text(response)

    assert provider.preflight_calls[0][1] == ReasoningPolicy.off()
    assert provider.stream_kwargs[0]["reasoning"] == ReasoningPolicy.off()
    assert _trace_events(
        trace_mock, "my_claude_code.api.optimization.safety_classifier_no_thinking"
    ) == [
        {
            "stage": "routing",
            "event": "my_claude_code.api.optimization.safety_classifier_no_thinking",
            "source": "api",
            "model": "test-model",
            "changed": False,
        }
    ]


@pytest.mark.asyncio
async def test_messages_handler_optimization_intercepts_before_provider_execution() -> (
    None
):
    provider_resolver = MagicMock()
    handler = MessagesHandler(Settings(), provider_resolver=provider_resolver)
    request = MessagesRequest(
        model="nvidia_nim/test-model",
        max_tokens=100,
        messages=[Message(role="user", content="quota check")],
    )
    optimized = LocalOptimization(
        rule="title_generation_skip",
        response=MessagesResponse(
            id="msg_test",
            model=request.model,
            content=[{"type": "text", "text": "Quota check passed."}],
            stop_reason="end_turn",
            usage=Usage(input_tokens=7, output_tokens=4),
        ),
        tokens_saved=7,
    )

    with patch(
        "my_claude_code.api.handlers.messages.try_optimizations",
        return_value=optimized,
    ):
        assert await handler.create(request) is optimized.response

    provider_resolver.assert_not_called()


@pytest.mark.asyncio
async def test_messages_handler_answers_model_routing_probe_without_a_provider() -> (
    None
):
    """The harness probe is answered locally end to end.

    The provider_resolver is a MagicMock: the route's health check may ask it
    for a throttle reading, but nothing may ask it for an answer, so the
    assertion is on the request-shaped calls rather than on the resolver
    itself. The response is the Message the harness parses, and its model id is
    the RESOLVED one ("test-model" stripped from "nvidia_nim/test-model" by
    routing), which is the honest echo: a proxy silently substituting a
    different model is still detected by the check.
    """
    provider_resolver = MagicMock()
    handler = MessagesHandler(Settings(), provider_resolver=provider_resolver)
    request = MessagesRequest(
        model="nvidia_nim/test-model",
        max_tokens=16,
        messages=[Message(role="user", content="Say OK")],
    )

    response = await handler.create(request)

    provider = provider_resolver.return_value
    provider.preflight_stream.assert_not_called()
    provider.stream_response.assert_not_called()
    assert isinstance(response, MessagesResponse)
    assert response.id.startswith("msg_")
    assert response.model == "test-model"
    block = response.content[0]
    assert isinstance(block, ContentBlockText)
    assert block.text == "OK"
    assert response.stop_reason == "end_turn"
    assert response.usage.input_tokens > 0
    assert response.usage.output_tokens == 1


def _probe_route_settings() -> Settings:
    """A two-model sonnet route, so "which model answers" has a real answer."""
    settings = Settings()
    settings.model_sonnet = "nvidia_nim/primary-model"
    settings.model_sonnet_fallbacks = "groq/fallback-model"
    return settings


def _probe_request() -> MessagesRequest:
    return MessagesRequest(
        model="sonnet",
        max_tokens=16,
        messages=[Message(role="user", content="Say OK")],
    )


@pytest.mark.asyncio
async def test_probe_reply_names_the_primary_when_nothing_is_benched() -> None:
    provider_resolver = MagicMock()
    handler = MessagesHandler(
        _probe_route_settings(), provider_resolver=provider_resolver
    )
    handler._provider_executor._health = RouteHealthRegistry(
        mode="consecutive", eject_after_failures=1, eject_seconds=60.0
    )

    response = await handler.create(_probe_request())

    provider_resolver.assert_not_called()
    assert isinstance(response, MessagesResponse)
    assert response.model == "primary-model"


@pytest.mark.asyncio
async def test_probe_reply_names_the_fallback_when_the_primary_is_benched() -> None:
    """The whole point of the probe: name the model that would really answer.

    With the primary benched, real traffic goes to the fallback. A reply that
    still named the primary would report exactly the substitution the harness
    exists to detect -- as a clean bill of health.
    """
    provider_resolver = MagicMock()
    handler = MessagesHandler(
        _probe_route_settings(), provider_resolver=provider_resolver
    )
    health = RouteHealthRegistry(
        mode="consecutive", eject_after_failures=1, eject_seconds=60.0
    )
    health.record_failure("nvidia_nim/primary-model")
    assert health.is_ejected("nvidia_nim/primary-model") is True
    handler._provider_executor._health = health

    response = await handler.create(_probe_request())

    provider_resolver.assert_not_called()
    assert isinstance(response, MessagesResponse)
    assert response.model == "fallback-model"


@pytest.mark.asyncio
async def test_messages_handler_probe_disabled_goes_upstream_normally() -> None:
    """The kill-switch restores the pre-feature behaviour exactly."""
    provider = FakeProvider()
    handler = MessagesHandler(
        Settings(),
        provider_resolver=lambda _: provider,
    )
    request = MessagesRequest(
        model="nvidia_nim/test-model",
        max_tokens=16,
        stream=True,
        messages=[Message(role="user", content="Say OK")],
    )
    settings = Settings()
    settings.enable_probe_auto_response = False
    handler._settings = settings

    response = await handler.create(request)
    assert isinstance(response, StreamingResponse)
    await _streaming_body_text(response)

    assert len(provider.preflight_calls) == 1


@pytest.mark.asyncio
async def test_responses_handler_bypasses_message_only_optimizations() -> None:
    provider = FakeProvider()
    handler = ResponsesHandler(Settings(), provider_resolver=lambda _: provider)

    with patch(
        "my_claude_code.api.handlers.messages.try_optimizations",
        side_effect=AssertionError("Responses must not use message optimizations"),
    ):
        response = await handler.create(
            OpenAIResponsesRequest(
                model="nvidia_nim/test-model",
                input="quota check",
            )
        )

    assert isinstance(response, StreamingResponse)
    body = await _streaming_body_text(response)
    assert "response.completed" in body
    assert provider.requests[0].messages[0].content == "quota check"


@pytest.mark.asyncio
async def test_responses_handler_does_not_apply_safety_classifier_policy() -> None:
    provider = FakeProvider()
    handler = ResponsesHandler(Settings(), provider_resolver=lambda _: provider)

    with patch("my_claude_code.api.handlers.messages.trace_event") as trace_mock:
        response = await handler.create(
            OpenAIResponsesRequest(
                model="nvidia_nim/test-model",
                input=_CLASSIFIER_USER,
                instructions=_CLASSIFIER_SYSTEM,
            )
        )

        assert isinstance(response, StreamingResponse)
        await _streaming_body_text(response)

    assert provider.preflight_calls[0][1] == ReasoningPolicy.provider_default()
    assert provider.stream_kwargs[0]["reasoning"] == ReasoningPolicy.provider_default()
    assert (
        _trace_events(
            trace_mock,
            "my_claude_code.api.optimization.safety_classifier_no_thinking",
        )
        == []
    )


def test_token_count_handler_routes_and_counts_tokens() -> None:
    handler = TokenCountHandler(
        Settings(),
        token_counter=lambda messages, system, tools: len(messages) + 41,
    )

    with patch("my_claude_code.api.handlers.token_count.trace_event") as trace:
        response = handler.count(
            TokenCountRequest(
                model="nvidia_nim/test-model",
                messages=[Message(role="user", content="hi")],
            ),
            request_id="req_ingress",
        )

    assert response.input_tokens == 42
    assert all(
        call.kwargs["request_id"] == "req_ingress" for call in trace.call_args_list
    )
