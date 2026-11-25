"""Module-level adapter parity tests for PR 9 (EM-PR1)."""

import base64
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from google.auth.credentials import Credentials
from openai import AsyncOpenAI

from my_claude_code.config import settings as config_settings
from my_claude_code.config.nim import NimSettings
from my_claude_code.config.provider_catalog import (
    DEEPSEEK_DEFAULT_BASE,
    GEMINI_DEFAULT_BASE,
    MISTRAL_DEFAULT_BASE,
    NVIDIA_NIM_DEFAULT_BASE,
    VERTEX_DEFAULT_BASE,
)
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.reasoning import ReasoningPolicy
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.deepseek import DeepSeekProvider
from my_claude_code.providers.deepseek.compat import build_deepseek_request_body
from my_claude_code.providers.gemini import GeminiProvider
from my_claude_code.providers.google_openai import GoogleOpenAIProvider
from my_claude_code.providers.mistral import MistralProvider
from my_claude_code.providers.nvidia_nim import NvidiaNimProvider
from my_claude_code.providers.nvidia_nim.request_options import build_nim_request_body
from my_claude_code.providers.openai_chat import GENERIC_OPENAI_PROFILE
from my_claude_code.providers.recovery import (
    FACT_STRUCTURED_OUTPUT_UNSUPPORTED,
    LearnedFactStore,
    reset_learned_fact_store,
    set_learned_fact_store,
)
from my_claude_code.providers.runtime import models_dev
from my_claude_code.providers.vertex import VertexProvider
from my_claude_code.providers.vertex.auth import GoogleAccessTokenProvider
from tests.providers.support import passthrough_rate_limiter

_SAMPLE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}

_PDF_B64 = base64.b64encode(b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n").decode("ascii")


def _chat_sse_bytes() -> bytes:
    chunk_1 = {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 1700000000,
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": "ok"},
                "finish_reason": None,
            }
        ],
    }
    chunk_2 = {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 1700000000,
        "model": "test-model",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
    }
    return (
        f"data: {json.dumps(chunk_1)}\n\n"
        f"data: {json.dumps(chunk_2)}\n\n"
        "data: [DONE]\n\n"
    ).encode()


def _structured_output_request(model: str = "test-model") -> MessagesRequest:
    return MessagesRequest.model_validate(
        {
            "model": model,
            "max_tokens": 128,
            "messages": [{"role": "user", "content": "hi"}],
            "output_config": {
                "format": {"type": "json_schema", "schema": _SAMPLE_SCHEMA}
            },
        }
    )


def _pdf_request(model: str = "test-model") -> MessagesRequest:
    return MessagesRequest.model_validate(
        {
            "model": model,
            "max_tokens": 128,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "summarize"},
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": _PDF_B64,
                            },
                        },
                    ],
                }
            ],
        }
    )


def _attach_mock_chat_client(provider: Any, captured: list[dict[str, Any]]) -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        captured.append(body)
        if len(captured) == 1:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": "response_format is not supported for this model",
                        "param": "response_format",
                    }
                },
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_chat_sse_bytes(),
        )

    provider._client = AsyncOpenAI(
        api_key="sk-test",
        base_url="https://example.invalid/v1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(_handler)),
    )


class _FakeGoogleCredentials(Credentials):
    def __init__(self) -> None:
        super().__init__()
        self.token = "tok"

    @property
    def expired(self) -> bool:
        return False

    def refresh(self, request: Any) -> None:
        del request


def _build_adapter(kind: str) -> tuple[Any, str]:
    limiter = passthrough_rate_limiter()
    if kind == "google_openai":
        return (
            GoogleOpenAIProvider(
                ProviderConfig(
                    api_key="sk-test", base_url="https://example.invalid/v1"
                ),
                profile=GENERIC_OPENAI_PROFILE,
                rate_limiter=limiter,
                provider_id="google_openai_test",
            ),
            "google_openai_test",
        )
    if kind == "vertex":
        return (
            VertexProvider(
                ProviderConfig(api_key="", base_url=VERTEX_DEFAULT_BASE),
                project_id="proj-1",
                location="global",
                rate_limiter=limiter,
                access_token_provider=GoogleAccessTokenProvider(_FakeGoogleCredentials),
            ),
            "vertex",
        )
    if kind == "gemini":
        return (
            GeminiProvider(
                ProviderConfig(api_key="sk-test", base_url=GEMINI_DEFAULT_BASE),
                rate_limiter=limiter,
            ),
            "gemini",
        )
    if kind == "mistral":
        return (
            MistralProvider(
                ProviderConfig(api_key="sk-test", base_url=MISTRAL_DEFAULT_BASE),
                rate_limiter=limiter,
            ),
            "mistral",
        )
    if kind == "nvidia_nim":
        return (
            NvidiaNimProvider(
                ProviderConfig(api_key="sk-test", base_url=NVIDIA_NIM_DEFAULT_BASE),
                nim_settings=NimSettings(),
                rate_limiter=limiter,
            ),
            "nvidia_nim",
        )
    if kind == "deepseek":
        return (
            DeepSeekProvider(
                ProviderConfig(api_key="sk-test", base_url=DEEPSEEK_DEFAULT_BASE),
                rate_limiter=limiter,
            ),
            "deepseek",
        )
    raise AssertionError(f"Unexpected adapter kind: {kind}")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    [
        "google_openai",
        "vertex",
        "gemini",
        "mistral",
        "nvidia_nim",
        "deepseek",
    ],
)
async def test_bypassed_adapters_suppress_structured_output_after_refusal(
    kind: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Turn 1 400 learns structured_output_unsupported; Turn 2 pre-suppresses in _build_request_body."""
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(config_settings, "get_settings", Settings)
    store = LearnedFactStore(path=tmp_path / "learned_facts.json")
    set_learned_fact_store(store)
    try:
        provider, expected_provider_id = _build_adapter(kind)
        captured: list[dict[str, Any]] = []
        _attach_mock_chat_client(provider, captured)

        req = _structured_output_request("test-so-model")

        # Turn 1: initial body carries response_format, upstream refuses 400, retry drops it.
        initial_body = provider._build_request_body(
            req, reasoning=ReasoningPolicy.off()
        )
        assert "response_format" in initial_body

        async for _ in provider.stream_response(req, reasoning=ReasoningPolicy.off()):
            pass

        assert len(captured) == 2
        assert "response_format" in captured[0]
        assert "response_format" not in captured[1]
        assert any(
            f.fact_kind == FACT_STRUCTURED_OUTPUT_UNSUPPORTED
            and f.provider_id == expected_provider_id
            and f.model_id == "test-so-model"
            for f in store.all_facts()
        )

        # Turn 2: _build_request_body itself must pre-suppress response_format and inject prompt fallback.
        turn2_built = provider._build_request_body(req, reasoning=ReasoningPolicy.off())
        assert "response_format" not in turn2_built
        assert any(
            m["role"] == "system" and "valid JSON" in str(m.get("content", ""))
            for m in turn2_built["messages"]
        )

        async for _ in provider.stream_response(req, reasoning=ReasoningPolicy.off()):
            pass
        assert len(captured) == 3
        assert "response_format" not in captured[2]
    finally:
        reset_learned_fact_store()


@pytest.mark.parametrize("kind", ["mistral", "nvidia_nim", "deepseek"])
def test_mistral_nim_deepseek_forward_accepts_documents_from_models_dev(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PDF document blocks survive when models.dev declares PDF support for the provider/model."""
    provider, expected_provider_id = _build_adapter(kind)
    req = _pdf_request("pdf-capable-model")

    monkeypatch.setattr(
        models_dev,
        "model_accepts_pdf",
        lambda pid, mid: pid == expected_provider_id and mid == "pdf-capable-model",
    )
    accepted_body = provider._build_request_body(req, reasoning=ReasoningPolicy.off())
    user_content = accepted_body["messages"][0]["content"]
    assert isinstance(user_content, list)
    assert any(
        isinstance(part, dict) and part.get("type") == "file" for part in user_content
    )

    monkeypatch.setattr(models_dev, "model_accepts_pdf", lambda _pid, _mid: False)
    rejected_body = provider._build_request_body(req, reasoning=ReasoningPolicy.off())
    rejected_content = rejected_body["messages"][0]["content"]
    assert isinstance(rejected_content, str)
    assert (
        "[attachment omitted: this model does not accept documents]" in rejected_content
    )


def test_direct_nim_and_deepseek_builders_preserve_false_defaults() -> None:
    """Direct unit callers keep accepts_documents=False & suppress_structured_output=False defaults."""
    req = _pdf_request("pdf-capable-model")
    req.output_config = {"format": {"type": "json_schema", "schema": _SAMPLE_SCHEMA}}

    nim_body = build_nim_request_body(
        req, NimSettings(), reasoning=ReasoningPolicy.off(), provider_id="nvidia_nim"
    )
    ds_body = build_deepseek_request_body(
        req, reasoning=ReasoningPolicy.off(), provider_id="deepseek"
    )

    for body in (nim_body, ds_body):
        assert "response_format" in body
        user_content = body["messages"][0]["content"]
        assert isinstance(user_content, str)
        assert (
            "[attachment omitted: this model does not accept documents]" in user_content
        )


def test_deepseek_preserves_chat_envelope_across_sanitization() -> None:
    """DeepSeekProvider and build_deepseek_request_body must not drop exclude=True chat_envelope."""
    envelope = {
        "seed": 42,
        "frequency_penalty": 0.25,
        "presence_penalty": 0.5,
        "logprobs": True,
        "top_logprobs": 3,
        "service_tier": "default",
        "store": False,
        "prompt_cache_key": "ck-deepseek-1",
        "parallel_tool_calls": False,
    }
    req = MessagesRequest.model_validate(
        {
            "model": "deepseek-chat",
            "max_tokens": 128,
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [
                {
                    "name": "Read",
                    "description": "Read a file",
                    "input_schema": {"type": "object", "properties": {}},
                }
            ],
            "chat_envelope": dict(envelope),
        }
    )

    provider = DeepSeekProvider(
        ProviderConfig(api_key="sk-test", base_url=DEEPSEEK_DEFAULT_BASE),
        rate_limiter=passthrough_rate_limiter(),
    )
    body = provider._build_request_body(req, reasoning=ReasoningPolicy.off())

    for key, val in envelope.items():
        assert body[key] == val, f"missing or mismatched {key}"
    assert "chat_envelope" not in body
    assert req.chat_envelope == envelope
