"""``/v1beta/models`` is the same product as the other three surfaces."""

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from my_claude_code.api.dependencies import get_settings
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic.streaming import format_sse_event
from my_claude_code.core.failures import ExecutionFailure, FailureKind
from tests.api.support import create_test_app

_MODEL = "nvidia_nim/test-model"
_GENERATE = f"/v1beta/models/{_MODEL}:generateContent"
_STREAM = f"/v1beta/models/{_MODEL}:streamGenerateContent?alt=sse"


class FakeProvider:
    def __init__(self, chunks: list[str]) -> None:
        self.chunks = chunks
        self.preflight_stream = MagicMock()
        self.requests: list[Any] = []
        self.stream_kwargs: list[dict[str, Any]] = []

    @property
    def credential_label(self) -> str | None:
        return None

    async def stream_response(self, request_data, **kwargs):
        self.requests.append(request_data)
        self.stream_kwargs.append(kwargs)
        for chunk in self.chunks:
            yield chunk


class PreStartFailingProvider(FakeProvider):
    def __init__(self) -> None:
        super().__init__([])

    async def stream_response(self, request_data, **kwargs):
        self.requests.append(request_data)
        self.stream_kwargs.append(kwargs)
        raise ExecutionFailure(
            kind=FailureKind.RATE_LIMIT,
            status_code=429,
            message="upstream is busy",
            retryable=True,
        )
        yield "unreachable"


def _anthropic_text_stream(text: str) -> list[str]:
    return [
        format_sse_event(
            "message_start",
            {
                "type": "message_start",
                "message": {"usage": {"input_tokens": 3, "output_tokens": 0}},
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
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 4},
            },
        ),
        format_sse_event("message_stop", {"type": "message_stop"}),
    ]


def _anthropic_tool_stream() -> list[str]:
    return [
        format_sse_event(
            "message_start",
            {"type": "message_start", "message": {"usage": {"input_tokens": 3}}},
        ),
        format_sse_event(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "toolu_1", "name": "echo"},
            },
        ),
        format_sse_event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '{"value":'},
            },
        ),
        format_sse_event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '"FCC"}'},
            },
        ),
        format_sse_event(
            "content_block_stop", {"type": "content_block_stop", "index": 0}
        ),
        format_sse_event(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 5},
            },
        ),
        format_sse_event("message_stop", {"type": "message_stop"}),
    ]


def _payload(**extra) -> dict[str, Any]:
    return {
        "contents": [{"role": "user", "parts": [{"text": "Hello"}]}],
        "generationConfig": {"maxOutputTokens": 32},
        **extra,
    }


def _data_frames(text: str) -> list[dict[str, Any]]:
    frames = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        assert block.startswith("data: "), block
        frames.append(json.loads(block[len("data: ") :]))
    return frames


@pytest.fixture
def gemini_client():
    provider = FakeProvider(_anthropic_text_stream("Hello from provider"))
    app = create_test_app()
    with (
        patch(
            "my_claude_code.api.gemini_routes.resolve_provider", return_value=provider
        ),
        TestClient(app) as client,
    ):
        yield client, provider


def test_probe_methods_are_answered(gemini_client) -> None:
    client, _provider = gemini_client

    assert client.head("/v1beta/models").status_code == 204
    assert client.options("/v1beta/models").status_code == 204
    assert client.head(f"/v1beta/models/{_MODEL}:generateContent").status_code == 204


def test_a_streaming_request_routes_through_the_shared_provider_path(
    gemini_client,
) -> None:
    client, provider = gemini_client

    response = client.post(_STREAM, json=_payload())

    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]
    # ``x-request-id`` is OpenAI's spelling and stays on OpenAI's paths; a
    # Gemini client reads neither, and MCC's own ``request-id`` is still here.
    assert response.headers["request-id"].startswith("req_")
    assert "x-request-id" not in response.headers
    # Gemini has no terminal sentinel; ``[DONE]`` is OpenAI's convention and
    # @google/genai would try to JSON.parse it.
    assert "[DONE]" not in response.text

    frames = _data_frames(response.text)
    assert (
        "".join(
            part.get("text", "")
            for frame in frames
            for part in frame["candidates"][0]["content"]["parts"]
        )
        == "Hello from provider"
    )
    assert frames[-1]["candidates"][0]["finishReason"] == "STOP"
    assert frames[-1]["usageMetadata"]["promptTokenCount"] == 3

    # The same executor path every other surface uses.
    assert provider.preflight_stream.called
    routed = provider.requests[0]
    assert routed.model == "test-model"
    assert routed.messages[0].role == "user"
    assert routed.max_tokens == 32
    assert provider.stream_kwargs[0]["request_id"] == response.headers["request-id"]


def test_a_non_streaming_request_returns_one_generate_content_response(
    gemini_client,
) -> None:
    client, _provider = gemini_client

    response = client.post(_GENERATE, json=_payload())

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert body["candidates"][0]["content"] == {
        "role": "model",
        "parts": [{"text": "Hello from provider"}],
    }
    assert body["candidates"][0]["finishReason"] == "STOP"
    assert body["modelVersion"] == _MODEL
    assert body["usageMetadata"] == {
        "promptTokenCount": 3,
        "candidatesTokenCount": 4,
        "totalTokenCount": 7,
    }


def test_the_method_decides_whether_the_answer_streams_not_alt_sse() -> None:
    """A hand-built request without ``?alt=sse`` still wants its stream."""

    provider = FakeProvider(_anthropic_text_stream("hi"))
    app = create_test_app()
    with (
        patch(
            "my_claude_code.api.gemini_routes.resolve_provider", return_value=provider
        ),
        TestClient(app) as client,
    ):
        streamed = client.post(
            f"/v1beta/models/{_MODEL}:streamGenerateContent", json=_payload()
        )
        unary = client.post(f"{_GENERATE}?alt=sse", json=_payload())

    assert "text/event-stream" in streamed.headers["content-type"]
    assert unary.headers["content-type"].startswith("application/json")


def test_a_models_prefixed_path_reaches_the_same_model() -> None:
    provider = FakeProvider(_anthropic_text_stream("hi"))
    app = create_test_app()
    with (
        patch(
            "my_claude_code.api.gemini_routes.resolve_provider", return_value=provider
        ),
        TestClient(app) as client,
    ):
        response = client.post(
            f"/v1beta/models/models/{_MODEL}:generateContent", json=_payload()
        )

    assert response.status_code == 200
    assert provider.requests[0].model == "test-model"


def test_a_tool_call_arrives_as_one_whole_function_call_part() -> None:
    provider = FakeProvider(_anthropic_tool_stream())
    app = create_test_app()
    with (
        patch(
            "my_claude_code.api.gemini_routes.resolve_provider", return_value=provider
        ),
        TestClient(app) as client,
    ):
        response = client.post(
            _STREAM,
            json=_payload(
                tools=[
                    {
                        "functionDeclarations": [
                            {"name": "echo", "parameters": {"type": "OBJECT"}}
                        ]
                    }
                ]
            ),
        )

    frames = _data_frames(response.text)
    calls = [
        part["functionCall"]
        for frame in frames
        for part in frame["candidates"][0]["content"]["parts"]
        if "functionCall" in part
    ]
    assert calls == [{"name": "echo", "args": {"value": "FCC"}}]
    assert frames[-1]["candidates"][0]["finishReason"] == "STOP"
    assert provider.requests[0].tools[0].name == "echo"
    assert provider.requests[0].tools[0].input_schema == {"type": "object"}


def test_a_non_streaming_tool_call_carries_the_same_function_call() -> None:
    provider = FakeProvider(_anthropic_tool_stream())
    app = create_test_app()
    with (
        patch(
            "my_claude_code.api.gemini_routes.resolve_provider", return_value=provider
        ),
        TestClient(app) as client,
    ):
        response = client.post(
            _GENERATE,
            json=_payload(tools=[{"functionDeclarations": [{"name": "echo"}]}]),
        )

    body = response.json()
    assert body["candidates"][0]["content"]["parts"] == [
        {"functionCall": {"name": "echo", "args": {"value": "FCC"}}}
    ]


def test_a_pre_start_failure_answers_with_a_status_line_and_googles_envelope() -> None:
    provider = PreStartFailingProvider()
    app = create_test_app()
    with (
        patch(
            "my_claude_code.api.gemini_routes.resolve_provider", return_value=provider
        ),
        TestClient(app) as client,
    ):
        response = client.post(_STREAM, json=_payload())

    assert response.status_code == 429
    assert response.json() == {
        "error": {
            "code": 429,
            "message": "upstream is busy",
            "status": "RESOURCE_EXHAUSTED",
        }
    }


def test_a_malformed_request_is_a_400_in_googles_envelope(gemini_client) -> None:
    client, _provider = gemini_client

    response = client.post(_GENERATE, json={"contents": []})

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["status"] == "INVALID_ARGUMENT"
    assert body["error"]["code"] == 400
    assert "contents" in body["error"]["message"]


def test_an_unsupported_method_is_a_404_naming_what_is_served(gemini_client) -> None:
    client, _provider = gemini_client

    response = client.post(f"/v1beta/models/{_MODEL}:embedContent", json=_payload())

    assert response.status_code == 404
    assert response.json()["error"]["status"] == "NOT_FOUND"
    assert "generateContent" in response.json()["error"]["message"]


def test_count_tokens_answers_from_the_same_estimator(gemini_client) -> None:
    client, _provider = gemini_client

    response = client.post(f"/v1beta/models/{_MODEL}:countTokens", json=_payload())

    assert response.status_code == 200
    assert response.json()["totalTokens"] > 0


def test_the_model_listing_publishes_the_ladders_own_limits() -> None:
    app = create_test_app(Settings(model="nvidia_nim/first-model"))
    with TestClient(app) as client:
        response = client.get("/v1beta/models")

    assert response.status_code == 200
    models = response.json()["models"]
    assert models
    for entry in models:
        assert entry["name"].startswith("models/")
        assert entry["supportedGenerationMethods"] == [
            "generateContent",
            "streamGenerateContent",
            "countTokens",
        ]
        # Unknown stays unknown: a limit nobody published is absent, never 0.
        assert entry.get("inputTokenLimit") != 0
        assert entry.get("outputTokenLimit") != 0


def test_one_model_can_be_described_and_an_unknown_one_is_a_404() -> None:
    app = create_test_app(Settings(model="nvidia_nim/first-model"))
    with TestClient(app) as client:
        listed = client.get("/v1beta/models").json()["models"]
        name = listed[0]["name"].removeprefix("models/")
        found = client.get(f"/v1beta/models/{name}")
        missing = client.get("/v1beta/models/no/such/model")

    assert found.status_code == 200
    assert found.json()["name"] == f"models/{name}"
    assert missing.status_code == 404
    assert missing.json()["error"]["status"] == "NOT_FOUND"


def test_every_gemini_auth_form_is_accepted_and_a_wrong_key_is_googles_401() -> None:
    """``x-goog-api-key`` is what @google/genai sends; ``?key=`` is the docs'."""

    app = create_test_app()
    settings = Settings()
    settings.anthropic_auth_token = "s3cr3t"
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        with TestClient(app) as client:
            unauthenticated = client.get("/v1beta/models")
            wrong = client.get("/v1beta/models", headers={"x-goog-api-key": "wrong"})
            accepted = [
                client.get("/v1beta/models", headers={"x-goog-api-key": "s3cr3t"}),
                client.get("/v1beta/models", headers={"x-api-key": "s3cr3t"}),
                client.get(
                    "/v1beta/models", headers={"Authorization": "Bearer s3cr3t"}
                ),
                client.get("/v1beta/models?key=s3cr3t"),
            ]
    finally:
        app.dependency_overrides.clear()

    assert unauthenticated.status_code == 401
    assert unauthenticated.json() == {
        "error": {
            "code": 401,
            "message": "Missing proxy authentication token",
            "status": "UNAUTHENTICATED",
        }
    }
    assert wrong.status_code == 401
    assert wrong.json()["error"]["message"] == "Invalid proxy authentication token"
    assert [response.status_code for response in accepted] == [200, 200, 200, 200]


def test_the_other_surfaces_keep_their_own_401_body() -> None:
    """Only the Gemini surface is reshaped; the rest keep FastAPI's detail."""

    app = create_test_app()
    settings = Settings()
    settings.anthropic_auth_token = "s3cr3t"
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        with TestClient(app) as client:
            response = client.get("/v1/models")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401
    assert response.json() == {"detail": "Missing proxy authentication token"}


def test_the_request_log_records_the_gemini_protocol_and_the_real_path() -> None:
    provider = FakeProvider(_anthropic_text_stream("hi"))
    app = create_test_app()
    captured: list[Any] = []
    with (
        patch(
            "my_claude_code.api.gemini_routes.resolve_provider", return_value=provider
        ),
        patch(
            "my_claude_code.api.handlers.gemini.build_capture",
            side_effect=_recording_build_capture(captured),
        ),
        TestClient(app) as client,
    ):
        client.post(_GENERATE, json=_payload())

    assert captured
    assert captured[0]["protocol"] == "gemini"
    assert captured[0]["endpoint"] == f"/v1beta/models/{_MODEL}:generateContent"


def _recording_build_capture(captured: list[Any]):
    from my_claude_code.api import request_capture as module

    original = module.build_capture

    def recording(settings, request, **kwargs):
        captured.append(kwargs)
        return original(settings, request, **kwargs)

    return recording
