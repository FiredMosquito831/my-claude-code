"""POST /v1/chat/completions is the same product as the other two surfaces."""

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from my_claude_code.core.anthropic.streaming import format_sse_event
from my_claude_code.core.failures import ExecutionFailure, FailureKind
from my_claude_code.core.reasoning import ReasoningControl, ReasoningEffort
from tests.api.support import create_test_app

_PATH = "/v1/chat/completions"


class FakeProvider:
    def __init__(self, chunks: list[str]) -> None:
        self.chunks = chunks
        self.preflight_stream = MagicMock()
        self.requests: list[Any] = []
        self.stream_kwargs: list[dict[str, Any]] = []

    @property
    def credential_label(self) -> str | None:
        return None

    async def stream_response(self, request_data, **_kwargs):
        self.requests.append(request_data)
        self.stream_kwargs.append(_kwargs)
        for chunk in self.chunks:
            yield chunk


class PreStartFailingProvider(FakeProvider):
    def __init__(self) -> None:
        super().__init__([])

    async def stream_response(self, request_data, **_kwargs):
        self.requests.append(request_data)
        self.stream_kwargs.append(_kwargs)
        raise ExecutionFailure(
            kind=FailureKind.RATE_LIMIT,
            status_code=429,
            message="upstream is busy",
            retryable=True,
        )
        yield "unreachable"


@pytest.fixture
def chat_client():
    provider = FakeProvider(_anthropic_text_stream("Hello from provider"))
    app = create_test_app()
    with (
        patch("my_claude_code.api.routes.resolve_provider", return_value=provider),
        TestClient(app) as client,
    ):
        yield client, provider


def _payload(**extra) -> dict[str, Any]:
    return {
        "model": "nvidia_nim/test-model",
        "messages": [{"role": "user", "content": "Hello"}],
        "max_tokens": 32,
        **extra,
    }


def _data_frames(text: str) -> list[dict[str, Any]]:
    frames = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block or block == "data: [DONE]":
            continue
        assert block.startswith("data: "), block
        frames.append(json.loads(block[len("data: ") :]))
    return frames


def test_probe_methods_are_answered(chat_client) -> None:
    client, _provider = chat_client

    assert client.head(_PATH).status_code == 204
    assert client.options(_PATH).status_code == 204


def test_a_streaming_request_routes_through_the_shared_provider_path(
    chat_client,
) -> None:
    client, provider = chat_client

    response = client.post(_PATH, json=_payload(stream=True))

    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]
    assert response.headers["x-request-id"] == response.headers["request-id"]
    assert response.text.endswith("data: [DONE]\n\n")

    frames = _data_frames(response.text)
    assert frames[0]["object"] == "chat.completion.chunk"
    assert frames[0]["id"].startswith("chatcmpl-")
    assert frames[0]["model"] == "nvidia_nim/test-model"
    assert (
        "".join(frame["choices"][0]["delta"].get("content", "") for frame in frames)
        == "Hello from provider"
    )
    assert frames[-1]["choices"][0]["finish_reason"] == "stop"

    # The same executor path every other surface uses.
    assert provider.preflight_stream.called
    routed = provider.requests[0]
    assert routed.model == "test-model"
    assert routed.messages[0].role == "user"
    assert routed.messages[0].content == "Hello"
    assert routed.max_tokens == 32
    assert provider.stream_kwargs[0]["request_id"] == response.headers["request-id"]


def test_a_non_streaming_request_returns_one_chat_completion_object(
    chat_client,
) -> None:
    client, _provider = chat_client

    response = client.post(_PATH, json=_payload())

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["id"].startswith("chatcmpl-")
    assert body["model"] == "nvidia_nim/test-model"
    choice = body["choices"][0]
    assert choice["index"] == 0
    assert choice["finish_reason"] == "stop"
    assert choice["message"] == {
        "role": "assistant",
        "content": "Hello from provider",
    }
    assert body["usage"] == {
        "prompt_tokens": 3,
        "completion_tokens": 4,
        "total_tokens": 7,
    }


def test_a_tool_call_streams_with_incremental_arguments() -> None:
    provider = FakeProvider(_anthropic_tool_stream())
    app = create_test_app()
    with (
        patch("my_claude_code.api.routes.resolve_provider", return_value=provider),
        TestClient(app) as client,
    ):
        response = client.post(
            _PATH,
            json=_payload(
                stream=True,
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "echo",
                            "parameters": {"type": "object"},
                        },
                    }
                ],
            ),
        )

    assert response.status_code == 200
    frames = _data_frames(response.text)
    calls = [
        call
        for frame in frames
        for call in frame["choices"][0]["delta"].get("tool_calls", ())
    ]
    assert calls[0] == {
        "index": 0,
        "id": "toolu_1",
        "type": "function",
        "function": {"name": "echo", "arguments": ""},
    }
    assert json.loads(
        "".join(call["function"].get("arguments", "") for call in calls)
    ) == {"value": "FCC"}
    assert frames[-1]["choices"][0]["finish_reason"] == "tool_calls"
    assert provider.requests[0].tools[0].name == "echo"


def test_a_non_streaming_tool_call_is_reported_as_tool_calls() -> None:
    provider = FakeProvider(_anthropic_tool_stream())
    app = create_test_app()
    with (
        patch("my_claude_code.api.routes.resolve_provider", return_value=provider),
        TestClient(app) as client,
    ):
        response = client.post(_PATH, json=_payload())

    choice = response.json()["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] is None
    assert choice["message"]["tool_calls"][0]["function"]["name"] == "echo"


def test_thinking_is_delivered_as_reasoning_content() -> None:
    provider = FakeProvider(_anthropic_reasoning_stream())
    app = create_test_app()
    with (
        patch("my_claude_code.api.routes.resolve_provider", return_value=provider),
        TestClient(app) as client,
    ):
        response = client.post(_PATH, json=_payload(stream=True))

    deltas = [frame["choices"][0]["delta"] for frame in _data_frames(response.text)]
    assert any(delta.get("reasoning_content") == "weighing it" for delta in deltas)
    assert "".join(delta.get("content", "") for delta in deltas) == "the answer"


def test_an_image_part_reaches_the_provider_as_an_anthropic_image_block(
    chat_client,
) -> None:
    client, provider = chat_client

    response = client.post(
        _PATH,
        json=_payload(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what is this"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,aGk="},
                        },
                    ],
                }
            ]
        ),
    )

    assert response.status_code == 200
    blocks = provider.requests[0].messages[0].content
    assert [block.type for block in blocks] == ["text", "image"]
    assert blocks[1].source == {
        "type": "base64",
        "media_type": "image/png",
        "data": "aGk=",
    }


@pytest.mark.parametrize(
    ("effort", "expected_effort"),
    [("low", ReasoningEffort.LOW), ("high", ReasoningEffort.HIGH)],
)
def test_reasoning_effort_is_resolved_by_the_shared_routing_layer(
    chat_client, effort: str, expected_effort: ReasoningEffort
) -> None:
    client, provider = chat_client

    response = client.post(_PATH, json=_payload(reasoning_effort=effort))

    assert response.status_code == 200
    routed = provider.requests[0]
    # The named effort travels as output_config and the routing layer alone
    # turns it into a policy -- exactly as it does for /v1/responses.
    assert routed.output_config == {"effort": effort}
    assert routed.thinking is None
    assert provider.stream_kwargs[0]["reasoning"].effort is expected_effort


def test_reasoning_effort_none_turns_thinking_off(chat_client) -> None:
    client, provider = chat_client

    response = client.post(_PATH, json=_payload(reasoning_effort="none"))

    assert response.status_code == 200
    assert provider.stream_kwargs[0]["reasoning"].control is ReasoningControl.OFF


def test_include_usage_adds_the_trailing_usage_chunk(chat_client) -> None:
    client, _provider = chat_client

    response = client.post(
        _PATH,
        json=_payload(stream=True, stream_options={"include_usage": True}),
    )

    frames = _data_frames(response.text)
    assert frames[-1]["choices"] == []
    assert frames[-1]["usage"] == {
        "prompt_tokens": 3,
        "completion_tokens": 4,
        "total_tokens": 7,
    }


def test_the_request_log_records_the_new_endpoint_and_protocol(chat_client) -> None:
    client, _provider = chat_client

    with patch("my_claude_code.api.handlers.chat_completions.build_capture") as build:
        client.post(_PATH, json=_payload(stream=True))

    assert build.call_args.kwargs["endpoint"] == "/v1/chat/completions"
    assert build.call_args.kwargs["protocol"] == "openai_chat"
    assert build.call_args.kwargs["stream"] is True


def test_the_request_log_records_a_non_streaming_client_as_non_streaming(
    chat_client,
) -> None:
    """The internal request is always streaming; the row must not say so.

    Every surface converts to one streaming ``MessagesRequest``, so reading
    ``stream`` off that would label a client that asked for a complete JSON
    body as a streaming one, in the log, the filters and the export alike.
    """
    client, _provider = chat_client

    with patch("my_claude_code.api.handlers.chat_completions.build_capture") as build:
        client.post(_PATH, json=_payload())

    assert build.call_args.kwargs["stream"] is False


def test_more_than_one_choice_is_an_openai_shaped_400(chat_client) -> None:
    client, _provider = chat_client

    response = client.post(_PATH, json=_payload(n=2))

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["type"] == "invalid_request_error"
    assert "n must be 1" in error["message"]


def test_an_unsupported_tool_type_is_an_openai_shaped_400(chat_client) -> None:
    client, _provider = chat_client

    response = client.post(_PATH, json=_payload(tools=[{"type": "web_search_preview"}]))

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert "web_search_preview" in response.json()["error"]["message"]


def test_a_pre_start_provider_failure_is_a_terminal_openai_error() -> None:
    provider = PreStartFailingProvider()
    app = create_test_app()
    with (
        patch("my_claude_code.api.routes.resolve_provider", return_value=provider),
        TestClient(app) as client,
    ):
        response = client.post(_PATH, json=_payload(stream=True))

    request_id = response.headers["request-id"]
    assert response.status_code == 429
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["x-should-retry"] == "false"
    assert response.headers["x-request-id"] == request_id
    error = response.json()["error"]
    assert error["type"] == "rate_limit_error"
    assert error["message"].startswith("upstream is busy")
    assert error["param"] is None
    assert error["code"] is None


def test_unknown_top_level_fields_are_accepted_rather_than_rejected(
    chat_client,
) -> None:
    client, _provider = chat_client

    response = client.post(
        _PATH, json=_payload(store=False, service_tier="auto", future_field=1)
    )

    assert response.status_code == 200


def _anthropic_text_stream(text: str) -> list[str]:
    return [
        format_sse_event("message_start", {"type": "message_start", "message": {}}),
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
                "delta": {"type": "text_delta", "text": text},
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
                "usage": {"input_tokens": 3, "output_tokens": 4},
            },
        ),
        format_sse_event("message_stop", {"type": "message_stop"}),
    ]


def _anthropic_tool_stream() -> list[str]:
    return [
        format_sse_event("message_start", {"type": "message_start", "message": {}}),
        format_sse_event(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "echo",
                    "input": {},
                },
            },
        ),
        format_sse_event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": '{"value":"FCC"}',
                },
            },
        ),
        format_sse_event(
            "content_block_stop", {"type": "content_block_stop", "index": 0}
        ),
        format_sse_event(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                "usage": {"input_tokens": 3, "output_tokens": 4},
            },
        ),
        format_sse_event("message_stop", {"type": "message_stop"}),
    ]


def _anthropic_reasoning_stream() -> list[str]:
    return [
        format_sse_event("message_start", {"type": "message_start", "message": {}}),
        format_sse_event(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking", "thinking": ""},
            },
        ),
        format_sse_event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "weighing it"},
            },
        ),
        format_sse_event(
            "content_block_stop", {"type": "content_block_stop", "index": 0}
        ),
        format_sse_event(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        format_sse_event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "text_delta", "text": "the answer"},
            },
        ),
        format_sse_event(
            "content_block_stop", {"type": "content_block_stop", "index": 1}
        ),
        format_sse_event(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"input_tokens": 3, "output_tokens": 4},
            },
        ),
        format_sse_event("message_stop", {"type": "message_stop"}),
    ]
