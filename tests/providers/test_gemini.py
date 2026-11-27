"""Tests for Google AI Studio Gemini (OpenAI-compatible) provider."""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from my_claude_code.config.provider_catalog import GEMINI_DEFAULT_BASE
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.gemini import GeminiProvider
from my_claude_code.providers.gemini.quirks import (
    GEMINI_SKIP_THOUGHT_SIGNATURE_VALIDATOR,
    GEMINI_THOUGHT_SIGNATURE_SKIPPED,
)
from tests.providers.request_factory import make_messages_request
from tests.providers.support import passthrough_rate_limiter, reasoning_for


def make_request(**overrides):
    return make_messages_request("models/gemini-3.1-flash-lite", **overrides)


def _simulate_openai_sdk_wire_json(body: dict) -> dict:
    wire = {key: value for key, value in body.items() if key != "extra_body"}
    sdk_extra = body.get("extra_body")
    if isinstance(sdk_extra, dict):
        wire.update(sdk_extra)
    return wire


@pytest.fixture
def gemini_config():
    return ProviderConfig(
        api_key="test_gemini_key",
        base_url=GEMINI_DEFAULT_BASE,
        rate_limit=10,
        rate_window=60,
    )


@pytest.fixture
def gemini_provider(gemini_config):
    return GeminiProvider(gemini_config, rate_limiter=passthrough_rate_limiter())


def test_init(gemini_config):
    """Test provider initialization."""
    with patch(
        "my_claude_code.providers.openai_chat.provider.AsyncOpenAI"
    ) as mock_openai:
        provider = GeminiProvider(
            gemini_config, rate_limiter=passthrough_rate_limiter()
        )
        assert provider._api_key == "test_gemini_key"
        assert (
            provider._base_url
            == "https://generativelanguage.googleapis.com/v1beta/openai"
        )
        mock_openai.assert_called_once()


def test_default_base_url_constant():
    assert GEMINI_DEFAULT_BASE == (
        "https://generativelanguage.googleapis.com/v1beta/openai/"
    )


def test_build_request_body_basic(gemini_provider):
    """Basic body conversion attaches Gemini thinking fields when thinking is on."""
    req = make_request()
    body = gemini_provider._build_request_body(req, reasoning=reasoning_for(req))

    assert body["model"] == "models/gemini-3.1-flash-lite"
    assert body["messages"][0]["role"] == "system"
    assert "reasoning_effort" not in body
    eb = body.get("extra_body")
    assert isinstance(eb, dict)
    literal_extra_body = eb.get("extra_body")
    assert isinstance(literal_extra_body, dict)
    gc = literal_extra_body.get("google")
    assert isinstance(gc, dict)
    tc = gc.get("thinking_config")
    assert isinstance(tc, dict)
    assert tc.get("include_thoughts") is True
    assert "google" not in eb


def test_build_request_body_sdk_wire_json_has_literal_extra_body(gemini_provider):
    """Regression for issue #542: SDK merge must not send top-level google."""
    req = make_request()

    body = gemini_provider._build_request_body(req, reasoning=reasoning_for(req))
    wire_json = _simulate_openai_sdk_wire_json(body)

    assert "reasoning_effort" not in wire_json
    assert "google" not in wire_json
    literal_extra_body = wire_json.get("extra_body")
    assert isinstance(literal_extra_body, dict)
    google = literal_extra_body.get("google")
    assert isinstance(google, dict)
    thinking_config = google.get("thinking_config")
    assert isinstance(thinking_config, dict)
    assert thinking_config.get("include_thoughts") is True


def test_build_request_body_reasoning_off_sets_reasoning_none():
    """When thinking is off, Gemini uses reasoning_effort none (Gemini 2.5 convention)."""
    provider = GeminiProvider(
        ProviderConfig(
            api_key="test_gemini_key",
            base_url=GEMINI_DEFAULT_BASE,
            rate_limit=10,
            rate_window=60,
        ),
        rate_limiter=passthrough_rate_limiter(),
    )
    req = make_request(thinking={"type": "disabled"})
    body = provider._build_request_body(req, reasoning=reasoning_for(req))

    assert body["reasoning_effort"] == "none"
    roles = [m.get("role") for m in body.get("messages", [])]
    assert "assistant_reasoning_content" not in roles


def test_build_request_body_preserves_caller_extra_body(gemini_provider):
    req = make_request(extra_body={"metadata": {"user": "u1"}})

    body = gemini_provider._build_request_body(req, reasoning=reasoning_for(req))

    assert "reasoning_effort" not in body
    eb = body.get("extra_body")
    assert isinstance(eb, dict)
    assert eb.get("metadata") == {"user": "u1"}
    literal_extra_body = eb.get("extra_body")
    assert isinstance(literal_extra_body, dict)
    google = literal_extra_body.get("google")
    assert isinstance(google, dict)


def test_build_request_body_merges_caller_nested_google(gemini_provider):
    req = make_request(
        extra_body={
            "metadata": {"user": "u1"},
            "extra_body": {
                "google": {
                    "thinking_config": {"budget_tokens": 128},
                    "cached_content": "cachedContents/example",
                }
            },
        }
    )

    body = gemini_provider._build_request_body(req, reasoning=reasoning_for(req))

    assert "reasoning_effort" not in body
    eb = body.get("extra_body")
    assert isinstance(eb, dict)
    assert eb.get("metadata") == {"user": "u1"}
    literal_extra_body = eb.get("extra_body")
    assert isinstance(literal_extra_body, dict)
    google = literal_extra_body.get("google")
    assert isinstance(google, dict)
    assert google.get("cached_content") == "cachedContents/example"
    thinking_config = google.get("thinking_config")
    assert isinstance(thinking_config, dict)
    assert thinking_config.get("budget_tokens") == 128
    assert thinking_config.get("include_thoughts") is True


def test_build_request_body_preserves_tool_call_extra_content(gemini_provider):
    req = make_request(
        system=None,
        messages=[
            {"role": "user", "content": "Find files"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "function-call-1",
                        "name": "Glob",
                        "input": {"pattern": "*.py"},
                        "extra_content": {
                            "google": {"thought_signature": "sig-from-client"}
                        },
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "function-call-1",
                        "content": "[]",
                    },
                ],
            },
        ],
    )

    body = gemini_provider._build_request_body(req, reasoning=reasoning_for(req))

    tool_call = body["messages"][1]["tool_calls"][0]
    assert tool_call["extra_content"] == {
        "google": {"thought_signature": "sig-from-client"}
    }


def test_build_request_body_uses_cached_tool_call_signature(gemini_provider):
    gemini_provider._record_tool_call_extra_content(
        "function-call-1", {"google": {"thought_signature": "sig-from-cache"}}
    )
    req = make_request(
        system=None,
        messages=[
            {"role": "user", "content": "Find files"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "function-call-1",
                        "name": "Glob",
                        "input": {"pattern": "*.py"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "function-call-1",
                        "content": "[]",
                    },
                ],
            },
        ],
    )

    body = gemini_provider._build_request_body(req, reasoning=reasoning_for(req))

    tool_call = body["messages"][1]["tool_calls"][0]
    assert tool_call["extra_content"] == {
        "google": {"thought_signature": "sig-from-cache"}
    }


def test_build_request_body_adds_current_turn_fallback_signature(
    gemini_provider,
):
    req = make_request(
        system=None,
        messages=[
            {"role": "user", "content": "Find files"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "function-call-1",
                        "name": "Glob",
                        "input": {"pattern": "*.py"},
                    },
                    {
                        "type": "tool_use",
                        "id": "function-call-2",
                        "name": "Read",
                        "input": {"file_path": "a.py"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "function-call-1",
                        "content": "[]",
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "function-call-2",
                        "content": "contents",
                    },
                ],
            },
        ],
    )

    body = gemini_provider._build_request_body(req, reasoning=reasoning_for(req))

    tool_calls = body["messages"][1]["tool_calls"]
    assert tool_calls[0]["extra_content"] == {
        "google": {"thought_signature": GEMINI_SKIP_THOUGHT_SIGNATURE_VALIDATOR}
    }
    assert "extra_content" not in tool_calls[1]


class TestTheSkipValidatorIsCountable:
    """8.19.0: writing the sentinel records a wire note, counts only.

    The sentinel is an escape hatch, not an implementation: Google validates a
    ``thoughtSignature`` on the current turn's first tool call, and MCC writes
    the sentinel whenever it has none to replay. Until this note existed an
    operator could not tell a working Gemini tool loop from one that was
    silently bypassing validation.

    The note is on the **wire record**, never in the body, and the sentinel's
    value is untouched -- ``test_build_request_body_adds_current_turn_fallback_
    signature`` above still pins the literal.
    """

    @staticmethod
    def _no_signature_request():
        return make_request(
            system=None,
            messages=[
                {"role": "user", "content": "Find files"},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "function-call-1",
                            "name": "Glob",
                            "input": {"pattern": "*.py"},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "function-call-1",
                            "content": "[]",
                        }
                    ],
                },
            ],
        )

    @staticmethod
    def _signed_request():
        return make_request(
            system=None,
            messages=[
                {"role": "user", "content": "Find files"},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "function-call-1",
                            "name": "Glob",
                            "input": {"pattern": "*.py"},
                            "extra_content": {
                                "google": {"thought_signature": "sig-from-client"}
                            },
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "function-call-1",
                            "content": "[]",
                        }
                    ],
                },
            ],
        )

    def test_a_missing_signature_writes_the_sentinel_and_counts_one(
        self, gemini_provider
    ) -> None:
        req = self._no_signature_request()
        notes: dict = {}
        body = gemini_provider._build_request_body(
            req, reasoning=reasoning_for(req), wire_notes=notes
        )
        assert body["messages"][1]["tool_calls"][0]["extra_content"] == {
            "google": {"thought_signature": GEMINI_SKIP_THOUGHT_SIGNATURE_VALIDATOR}
        }
        note = notes[GEMINI_THOUGHT_SIGNATURE_SKIPPED]
        assert "1 tool call" in note
        assert GEMINI_SKIP_THOUGHT_SIGNATURE_VALIDATOR not in note

    def test_a_captured_signature_writes_neither(self, gemini_provider) -> None:
        req = self._signed_request()
        notes: dict = {}
        body = gemini_provider._build_request_body(
            req, reasoning=reasoning_for(req), wire_notes=notes
        )
        assert body["messages"][1]["tool_calls"][0]["extra_content"] == {
            "google": {"thought_signature": "sig-from-client"}
        }
        assert GEMINI_THOUGHT_SIGNATURE_SKIPPED not in notes

    def test_a_cached_signature_writes_neither(self, gemini_provider) -> None:
        """The other way a signature can be present: captured off a previous
        turn and restored from the cache. It is not a skip."""

        gemini_provider._record_tool_call_extra_content(
            "function-call-1", {"google": {"thought_signature": "sig-from-cache"}}
        )
        # The same history as ``_signed_request`` with the client's own copy of
        # the signature left off, so only the cache can supply one. Built here
        # rather than stripped from the signed request: the block is a typed
        # model and a field it does not declare is not assignable.
        req = make_request(
            system=None,
            messages=[
                {"role": "user", "content": "Find files"},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "function-call-1",
                            "name": "Glob",
                            "input": {"pattern": "*.py"},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "function-call-1",
                            "content": "[]",
                        }
                    ],
                },
            ],
        )
        notes: dict = {}
        body = gemini_provider._build_request_body(
            req, reasoning=reasoning_for(req), wire_notes=notes
        )
        assert body["messages"][1]["tool_calls"][0]["extra_content"] == {
            "google": {"thought_signature": "sig-from-cache"}
        }
        assert GEMINI_THOUGHT_SIGNATURE_SKIPPED not in notes

    def test_the_note_is_never_in_the_body(self, gemini_provider) -> None:
        req = self._no_signature_request()
        notes: dict = {}
        body = gemini_provider._build_request_body(
            req, reasoning=reasoning_for(req), wire_notes=notes
        )
        assert GEMINI_THOUGHT_SIGNATURE_SKIPPED not in json.dumps(body)

    def test_the_note_is_not_a_reasoning_key(self) -> None:
        """The wire recorder reads any key beginning ``reasoning``/``thinking``
        as a reasoning *instruction*. A note about a signature must not count
        as reasoning sent -- the rule ``history_carrier_*`` keeps."""

        from my_claude_code.core.wire_capture import is_reasoning_key

        assert is_reasoning_key(GEMINI_THOUGHT_SIGNATURE_SKIPPED) is False

    def test_the_body_is_unchanged_by_the_note(self, gemini_provider) -> None:
        """The equality claim: recording the note moves no body byte.

        The same request built twice -- once with nowhere to record, once with
        a mapping -- and the bodies must be byte-identical. The note is on the
        record, never on the wire.
        """

        req = self._no_signature_request()
        with_notes: dict = {}
        left = gemini_provider._build_request_body(req, reasoning=reasoning_for(req))
        right = gemini_provider._build_request_body(
            req, reasoning=reasoning_for(req), wire_notes=with_notes
        )
        assert json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)
        assert GEMINI_THOUGHT_SIGNATURE_SKIPPED in with_notes


@pytest.mark.asyncio
async def test_stream_response_text(gemini_provider):
    req = make_request(thinking={"type": "enabled"})

    mock_chunk = MagicMock()
    mock_chunk.choices = [
        MagicMock(
            delta=MagicMock(
                content="Hello back!",
                reasoning_content=None,
                tool_calls=None,
            ),
            finish_reason="stop",
        )
    ]
    mock_chunk.usage = MagicMock(completion_tokens=5, prompt_tokens=10)

    async def mock_stream():
        yield mock_chunk

    with patch.object(
        gemini_provider._client.chat.completions, "create", new_callable=AsyncMock
    ) as mock_create:
        mock_create.return_value = mock_stream()

        events = [
            event
            async for event in gemini_provider.stream_response(
                req, reasoning=reasoning_for(req)
            )
        ]

        assert any(
            '"text_delta"' in event and "Hello back!" in event for event in events
        )
        kwargs = mock_create.call_args.kwargs
        assert "reasoning_effort" not in kwargs
        extra_body = kwargs.get("extra_body")
        assert isinstance(extra_body, dict)
        literal_extra_body = extra_body.get("extra_body")
        assert isinstance(literal_extra_body, dict)
        google = literal_extra_body.get("google")
        assert isinstance(google, dict)
        thinking_config = google.get("thinking_config")
        assert isinstance(thinking_config, dict)
        assert thinking_config.get("include_thoughts") is True


@pytest.mark.asyncio
async def test_stream_response_preserves_tool_call_extra_content(gemini_provider):
    req = make_request()

    mock_tc = MagicMock()
    mock_tc.index = 0
    mock_tc.id = "function-call-1"
    mock_tc.extra_content = {"google": {"thought_signature": "sig-stream"}}
    mock_tc.function = MagicMock()
    mock_tc.function.name = "Glob"
    mock_tc.function.arguments = '{"pattern":"*.py"}'

    mock_chunk = MagicMock()
    mock_chunk.choices = [
        MagicMock(
            delta=MagicMock(
                content=None,
                reasoning_content=None,
                tool_calls=[mock_tc],
            ),
            finish_reason="tool_calls",
        )
    ]
    mock_chunk.usage = MagicMock(completion_tokens=5, prompt_tokens=10)

    async def mock_stream():
        yield mock_chunk

    with patch.object(
        gemini_provider._client.chat.completions, "create", new_callable=AsyncMock
    ) as mock_create:
        mock_create.return_value = mock_stream()

        events = [event async for event in gemini_provider.stream_response(req)]

    tool_starts = [
        event
        for event in events
        if '"content_block_start"' in event and '"tool_use"' in event
    ]
    assert any(
        '"extra_content"' in event and "sig-stream" in event for event in tool_starts
    )
    assert gemini_provider._tool_call_extra_content_by_id["function-call-1"] == {
        "google": {"thought_signature": "sig-stream"}
    }


@pytest.mark.asyncio
async def test_stream_response_reasoning_content(gemini_provider):
    req = make_request()

    mock_chunk = MagicMock()
    mock_chunk.choices = [
        MagicMock(
            delta=MagicMock(
                content=None,
                reasoning_content="Thinking...",
                tool_calls=None,
            ),
            finish_reason="stop",
        )
    ]
    mock_chunk.usage = MagicMock(completion_tokens=2, prompt_tokens=10)

    async def mock_stream():
        yield mock_chunk

    with patch.object(
        gemini_provider._client.chat.completions, "create", new_callable=AsyncMock
    ) as mock_create:
        mock_create.return_value = mock_stream()

        events = [event async for event in gemini_provider.stream_response(req)]

        assert any(
            '"thinking_delta"' in event and "Thinking..." in event for event in events
        )


@pytest.mark.asyncio
async def test_cleanup(gemini_provider):
    gemini_provider._client = AsyncMock()

    await gemini_provider.cleanup()

    gemini_provider._client.close.assert_called_once()


def test_gemini_tool_result_image_goes_as_an_image_part(gemini_provider):
    """Gemini gets the fix for free: it subclasses the OpenAI chat provider.

    There is no Gemini-specific code in this PR at all. This test exists to
    prove that, and to fail loudly if someone ever gives the Google providers
    their own converter.
    """
    req = make_request(
        messages=[
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "take_screenshot",
                        "input": {},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": "QUJDRA==",
                                },
                            },
                        ],
                    }
                ],
            },
        ]
    )

    body = gemini_provider._build_request_body(req, reasoning=reasoning_for(req))

    roles = [message["role"] for message in body["messages"]]
    assert roles[-2:] == ["tool", "user"]
    hoisted = body["messages"][-1]["content"]
    assert hoisted[1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,QUJDRA=="},
    }
    assert "QUJDRA==" not in str(body["messages"][-2])


def test_adaptive_thinking_sets_include_thoughts_and_keeps_default_unchanged(
    gemini_provider,
) -> None:
    from my_claude_code.core.reasoning import ReasoningPolicy

    adaptive_req = make_request(thinking={"type": "adaptive"})
    adaptive_body = gemini_provider._build_request_body(
        adaptive_req, reasoning=reasoning_for(adaptive_req)
    )
    assert (
        adaptive_body["extra_body"]["extra_body"]["google"]["thinking_config"][
            "include_thoughts"
        ]
        is True
    )

    default_req = make_request()
    default_body = gemini_provider._build_request_body(
        default_req, reasoning=ReasoningPolicy.provider_default()
    )
    assert "extra_body" not in default_body


def test_gemini_envelope_restores_tool_call_signature_without_skip_sentinel(
    gemini_provider,
) -> None:
    from my_claude_code.core.anthropic.models import MessagesRequest
    from my_claude_code.core.gemini_api import (
        GeminiGenerateContentRequest,
    )
    from my_claude_code.core.gemini_api.input import (
        convert_request_to_anthropic_payload,
    )

    gem_req = GeminiGenerateContentRequest.model_validate(
        {
            "contents": [
                {"role": "user", "parts": [{"text": "find"}]},
                {
                    "role": "model",
                    "parts": [
                        {
                            "functionCall": {
                                "id": "call_from_turn_1",
                                "name": "Glob",
                                "args": {"pattern": "*.py"},
                            },
                            "thoughtSignature": "sig-from-gemini-envelope",
                        }
                    ],
                },
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "id": "call_from_turn_1",
                                "name": "Glob",
                                "response": {"output": "[]"},
                            }
                        }
                    ],
                },
            ]
        }
    ).with_model("gemini-2.5-pro")

    conv = convert_request_to_anthropic_payload(gem_req)
    req = MessagesRequest(**conv.payload)
    # Exclude from model_dump so non-Google upstreams never see gemini_envelope.
    assert "gemini_envelope" not in req.model_dump()

    notes: dict[str, Any] = {}
    body = gemini_provider._build_request_body(
        req, reasoning=reasoning_for(req), wire_notes=notes
    )
    assert body["messages"][1]["tool_calls"][0]["extra_content"] == {
        "google": {"thought_signature": "sig-from-gemini-envelope"}
    }
    assert GEMINI_THOUGHT_SIGNATURE_SKIPPED not in notes


def test_image_only_followup_user_turn_establishes_turn_boundary(
    gemini_provider,
) -> None:
    from my_claude_code.providers.gemini.quirks import (
        _message_has_standard_user_content,
    )

    assert _message_has_standard_user_content(
        {
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": "https://x/a.png"}}],
        }
    )
    assert _message_has_standard_user_content(
        {"role": "user", "content": [{"type": "input_audio", "input_audio": {}}]}
    )
    assert _message_has_standard_user_content(
        {"role": "user", "content": [{"type": "file", "file": {}}]}
    )
    assert not _message_has_standard_user_content(
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
        }
    )

    req = make_request(
        system=None,
        messages=[
            {"role": "user", "content": "Find files"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call_prior",
                        "name": "Glob",
                        "input": {"pattern": "*.py"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call_prior",
                        "content": "[]",
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "url",
                            "url": "https://example.test/shot.png",
                        },
                    }
                ],
            },
        ],
    )
    notes: dict[str, Any] = {}
    body = gemini_provider._build_request_body(
        req, reasoning=reasoning_for(req), wire_notes=notes
    )
    assert "extra_content" not in body["messages"][1]["tool_calls"][0]
    assert GEMINI_THOUGHT_SIGNATURE_SKIPPED not in notes


@pytest.mark.asyncio
async def test_gemini_native_models_enrichment_preserves_generation_methods():
    from types import SimpleNamespace

    import httpx

    from my_claude_code.application.media.capabilities import (
        MediaCapabilityInputs,
        resolve_media_capability_profile,
    )
    from my_claude_code.application.model_kinds import resolve_model_kind
    from my_claude_code.config.provider_catalog import PROVIDER_CATALOG
    from my_claude_code.core.model_ids import ResolutionTier

    seen_requests: list[httpx.Request] = []

    def transport_handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        if request.url.path == "/v1beta/models":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "models/gemini-2.5-flash",
                            "inputTokenLimit": 1048576,
                            "outputTokenLimit": 65536,
                            "supportedGenerationMethods": [
                                "generateContent",
                                "predict",
                            ],
                        },
                        {
                            "name": "models/imagen-3.0-generate-002",
                            "supportedGenerationMethods": ["predict"],
                        },
                        {
                            "name": "models/veo-2.0-generate-001",
                            "supportedGenerationMethods": ["predictLongRunning"],
                        },
                    ]
                },
            )
        return httpx.Response(404)

    provider = GeminiProvider(
        ProviderConfig(
            api_key="test_gemini_key",
            base_url=GEMINI_DEFAULT_BASE,
            rate_limit=10,
            rate_window=60,
        ),
        rate_limiter=passthrough_rate_limiter(),
    )
    provider._client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(transport_handler)
    )
    provider._client.models.list = AsyncMock(
        return_value=SimpleNamespace(
            data=[
                SimpleNamespace(
                    id="gemini-2.5-flash", object="model", owned_by="google"
                )
            ]
        )
    )

    infos = await provider.list_model_infos()
    by_id = {info.model_id: info for info in infos}

    assert set(by_id) == {
        "gemini-2.5-flash",
        "imagen-3.0-generate-002",
        "veo-2.0-generate-001",
    }
    assert any(
        req.url.path == "/v1beta/models"
        and req.headers.get("x-goog-api-key") == "test_gemini_key"
        for req in seen_requests
    )

    flash = by_id["gemini-2.5-flash"]
    assert flash.context_length == 1048576
    assert flash.max_output_tokens == 65536
    # Chat compatibility: _generation_methods strips predict when generateContent is present.
    assert flash.declared is not None
    assert flash.declared.endpoints == ("generatecontent",)
    # Media capability resolution still sees raw predict in published_row.
    flash_profile = resolve_media_capability_profile(
        "gemini/gemini-2.5-flash",
        MediaCapabilityInputs(
            provider_info=flash,
            surfaces=PROVIDER_CATALOG["gemini"].media_surfaces,
        ),
    )
    assert flash_profile.supported_generation_methods == frozenset(
        {"generateContent", "predict"}
    )
    assert "image_generate" in flash_profile.operations

    veo_profile = resolve_media_capability_profile(
        "gemini/veo-2.0-generate-001",
        MediaCapabilityInputs(
            provider_info=by_id["veo-2.0-generate-001"],
            surfaces=PROVIDER_CATALOG["gemini"].media_surfaces,
        ),
    )
    assert veo_profile.supported_generation_methods == frozenset({"predictLongRunning"})
    assert veo_profile.operations == frozenset({"video_create"})
    assert veo_profile.sync_mode == "async_job"

    # Native-only image/video models stay out of chat catalogues.
    def kind_words(provider_id: str, model_id: str):
        info = by_id.get(model_id)
        if provider_id != "gemini" or info is None or info.declared is None:
            return None, None
        return info.declared.endpoints, ResolutionTier.PROVIDER_EXACT

    def no_modalities(_provider_id: str, _model_id: str):
        return None, None

    veo_kind = resolve_model_kind(
        "gemini/veo-2.0-generate-001",
        modalities=no_modalities,
        placements={},
        kind_words=kind_words,
    )
    assert veo_kind.known and not veo_kind.chat_listable
    assert veo_kind.offered_on("video")

    imagen_kind = resolve_model_kind(
        "gemini/imagen-3.0-generate-002",
        modalities=no_modalities,
        placements={},
        kind_words=kind_words,
    )
    assert imagen_kind.known and not imagen_kind.chat_listable
    assert imagen_kind.offered_on("image")


@pytest.mark.asyncio
async def test_gemini_native_models_404_gracefully_falls_back():
    from types import SimpleNamespace

    import httpx

    provider = GeminiProvider(
        ProviderConfig(
            api_key="test_gemini_key",
            base_url=GEMINI_DEFAULT_BASE,
            rate_limit=10,
            rate_window=60,
        ),
        rate_limiter=passthrough_rate_limiter(),
    )
    provider._client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _req: httpx.Response(404))
    )
    provider._client.models.list = AsyncMock(
        return_value=SimpleNamespace(
            data=[
                SimpleNamespace(
                    id="gemini-2.5-flash", object="model", owned_by="google"
                )
            ]
        )
    )

    infos = await provider.list_model_infos()
    assert {info.model_id for info in infos} == {"gemini-2.5-flash"}
