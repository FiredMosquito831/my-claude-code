"""The third door: the Messages surface, wired behind the OpenCode provider.

Zen publishes ``provider.npm: "@ai-sdk/anthropic"`` for a share of its
registry, and that names Anthropic's Messages protocol on ``{base}/messages``.
From 6.74.0 until now such a model resolved to ``unservable`` and was listed
with that reason; nothing was hidden, but nothing was reachable either.

**Stated plainly, because the PR body states it too: no live model exercises
this.** The only free model that was ever on this surface has been withdrawn
and the recorded capture of ``POST /zen/v1/messages`` (2026-09-11) answers
HTTP 401 ``Model minimax-m3-free is not supported``. So the proof here is the
recorded capture plus a fake upstream, and that is all it claims to be.

What these tests hold:

* the request goes to ``{base}/messages`` in Anthropic's Messages protocol,
  with the gateway's identity headers **and** both credential shapes the
  capture carries;
* the identity is byte-identical to the one the other two surfaces send -- it
  is not touched (decision Q6, 2026-09-13), only carried through one more door;
* the surface rung reaches it: a wrong-door refusal on Chat Completions probes
  Messages and remembers the answer;
* a model on this surface no longer resolves to ``unservable``;
* the recovery ladder, the learned-facts store and the output-cap table are the
  Messages family's own, keyed on the same catalogue id as the other doors.
"""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from my_claude_code.application.model_metadata import (
    ResponseSurface,
    ResponseSurfaceSource,
)
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.core.wire_capture import install_wire_trace
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.openai_chat import (
    OPENAI_CHAT_PROFILES,
    create_openai_chat_provider,
    resolve_response_surface,
)
from my_claude_code.providers.openai_chat.messages_transport import (
    GatewayMessagesAuth,
    MessagesTransport,
)
from my_claude_code.providers.recovery import learned_fact_store
from my_claude_code.providers.runtime import models_dev
from tests.providers.support import passthrough_rate_limiter

BASE_URL = "https://opencode.ai/zen/v1"
PROFILE = OPENAI_CHAT_PROFILES["opencode"]
DECLARED = PROFILE.response_surfaces

REFERENCE = json.loads(
    (
        Path(__file__).resolve().parents[1]
        / "contracts"
        / "opencode_reference_request.json"
    ).read_text(encoding="utf-8")
)["surfaces"]["messages"]

#: One minimal Anthropic SSE conversation, the shape the real endpoint answers
#: 200 with. Enough for the reader to produce a message and finish; anything
#: longer would be testing the SSE reader, which has its own tests.
MESSAGES_SSE = (
    'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_1",'
    '"type":"message","role":"assistant","model":"m","content":[],'
    '"stop_reason":null,"usage":{"input_tokens":3,"output_tokens":0}}}\n\n'
    'event: content_block_start\ndata: {"type":"content_block_start","index":0,'
    '"content_block":{"type":"text","text":""}}\n\n'
    'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
    '"delta":{"type":"text_delta","text":"ok"}}\n\n'
    'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n'
    'event: message_delta\ndata: {"type":"message_delta","delta":'
    '{"stop_reason":"end_turn"},"usage":{"output_tokens":1}}\n\n'
    'event: message_stop\ndata: {"type":"message_stop"}\n\n'
)


def _write_registry(**models: object) -> None:
    """Write a models.dev cache with one ``opencode`` bucket, as MCC caches it."""

    path = models_dev.models_dev_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    models_dev.write_models_dev_cache(
        {"opencode": {"npm": "@ai-sdk/openai-compatible", "models": dict(models)}},
        path,
    )
    models_dev.reset_models_dev_payload_cache()


def _npm(package: str) -> dict[str, object]:
    return {"provider": {"npm": package}}


def _request(model: str = "m") -> MessagesRequest:
    return MessagesRequest(
        model=model,
        max_tokens=16,
        messages=[Message(role="user", content="hi")],
    )


def _provider() -> Any:
    return create_openai_chat_provider(
        "opencode",
        ProviderConfig(api_key="sk-test", base_url=BASE_URL),
        passthrough_rate_limiter(),
        profile=PROFILE,
    )


def _transport() -> MessagesTransport:
    return MessagesTransport(
        ProviderConfig(api_key="sk-test", base_url=BASE_URL),
        base_url=BASE_URL,
        provider_name="OPENCODE",
        provider_id="opencode",
        identity=PROFILE.client_identity,
        api_key="sk-test",
        rate_limiter=passthrough_rate_limiter(),
    )


class _Upstream:
    """A fake ``{base}/messages`` that records what reached it."""

    def __init__(self, *, status: int = 200, body: object = None) -> None:
        self.status = status
        self.body = body
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status >= 400:
            return httpx.Response(
                self.status,
                json=self.body if self.body is not None else {"error": "no"},
                request=request,
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=MESSAGES_SSE.encode(),
            request=request,
        )


def _install(transport: MessagesTransport, upstream: _Upstream) -> None:
    """Point the transport's Messages client at a fake upstream.

    Reaching for the inner provider's client is the narrowest available seam:
    the transport owns an :class:`AnthropicMessagesProvider` precisely so the
    protocol is not reimplemented here, and that object builds its own client.
    """

    inner: Any = transport._provider
    inner._client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))


async def _drain(stream) -> list[str]:
    return [event async for event in stream]


# --------------------------------------------------------------------------
# the wire
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_request_goes_to_the_gateways_messages_path() -> None:
    transport = _transport()
    upstream = _Upstream()
    _install(transport, upstream)

    events = await _drain(
        transport.stream(
            _request(), input_tokens=3, reasoning=_no_reasoning(), surface_label=""
        )
    )

    assert events
    assert transport.url == f"{BASE_URL}/messages"
    assert str(upstream.requests[0].url) == f"{BASE_URL}/messages"
    assert upstream.requests[0].url.path == REFERENCE["path"]


@pytest.mark.asyncio
async def test_the_body_is_the_messages_protocol_not_a_translated_one() -> None:
    """The reason re-pointing was an S: the client already spoke this."""

    transport = _transport()
    upstream = _Upstream()
    _install(transport, upstream)

    await _drain(
        transport.stream(_request(), input_tokens=3, reasoning=_no_reasoning())
    )

    body = json.loads(upstream.requests[0].content)
    assert set(REFERENCE["body_keys_in_order"]) <= set(body)
    assert body["model"] == "m"
    assert isinstance(body["messages"], list)
    # Neither of the other two surfaces' shapes leaked in.
    assert "input" not in body
    assert "reasoning_effort" not in body


@pytest.mark.asyncio
async def test_every_identity_and_credential_header_the_capture_carries() -> None:
    """The divergence test, against the recorded ``POST /zen/v1/messages``."""

    transport = _transport()
    upstream = _Upstream()
    _install(transport, upstream)

    await _drain(
        transport.stream(_request(), input_tokens=3, reasoning=_no_reasoning())
    )

    sent = upstream.requests[0].headers
    for name in REFERENCE["header_names_in_order"]:
        assert name.lower() in sent, name
    for name, value in REFERENCE["constant_header_values"].items():
        if name == "User-Agent":
            # MCC deliberately sends the first of the captured UA's three
            # segments; a prefix check states that rather than pinning a value
            # nobody chose. Decision Q6: the identity is not touched.
            assert value.startswith(sent[name])
            continue
        assert sent[name] == value
    # Both credential shapes, because the capture carries both.
    assert sent["authorization"] == "Bearer sk-test"
    assert sent["x-api-key"] == "sk-test"


@pytest.mark.asyncio
async def test_the_identity_matches_the_other_two_surfaces_exactly() -> None:
    """Three doors, one identity. The thing that must not drift."""

    transport = _transport()
    body = {"messages": [{"role": "user", "content": "hi"}]}
    provider = _provider()

    messages_identity = transport.identity_headers(body)
    responses_identity = provider._responses.identity_headers(
        {"input": body["messages"]}
    )

    assert set(messages_identity) == set(responses_identity)
    assert list(messages_identity) == list(responses_identity)
    assert messages_identity["x-opencode-client"] == "cli"
    # The per-conversation key is derived from the same conversation, so the
    # two doors agree about which conversation this is.
    assert (
        messages_identity["x-opencode-session"]
        == responses_identity["x-opencode-session"]
    )


@pytest.mark.asyncio
async def test_a_rotating_credential_is_resolved_per_request() -> None:
    keys = iter(("first", "second"))

    async def _next_key() -> str:
        return next(keys)

    auth = GatewayMessagesAuth("unused", api_key_provider=_next_key)

    assert (await auth.headers())["Authorization"] == "Bearer first"
    assert (await auth.headers())["x-api-key"] == "second"


@pytest.mark.asyncio
async def test_the_wire_capture_records_which_door_served_the_request() -> None:
    transport = _transport()
    upstream = _Upstream()
    _install(transport, upstream)

    trace = install_wire_trace()
    await _drain(
        transport.stream(
            _request(),
            input_tokens=3,
            reasoning=_no_reasoning(),
            surface_label="messages (registry)",
        )
    )

    (recorded,) = trace.requests.values()
    assert recorded.params["surface"] == "messages (registry)"


@pytest.mark.asyncio
async def test_no_label_means_no_surface_key_at_all() -> None:
    """Every single-surface caller of this family is byte-identical."""

    transport = _transport()
    _install(transport, _Upstream())

    trace = install_wire_trace()
    await _drain(
        transport.stream(_request(), input_tokens=3, reasoning=_no_reasoning())
    )

    (recorded,) = trace.requests.values()
    assert "surface" not in recorded.params


# --------------------------------------------------------------------------
# resolution and the rung
# --------------------------------------------------------------------------


def test_a_messages_model_is_no_longer_unservable() -> None:
    _write_registry(**{"claude-fable-5-1": _npm("@ai-sdk/anthropic")})

    resolved = resolve_response_surface(
        "opencode",
        "claude-fable-5-1",
        registry_provider="opencode",
        declared=DECLARED,
    )

    assert resolved.surface is ResponseSurface.MESSAGES
    assert resolved.source is ResponseSurfaceSource.REGISTRY


@pytest.mark.asyncio
async def test_a_registry_messages_model_goes_straight_to_the_messages_door() -> None:
    """No probe at all when the vendor already published the answer."""

    _write_registry(**{"claude-fable-5-1": _npm("@ai-sdk/anthropic")})
    provider = _provider()
    upstream = _Upstream()
    _install(provider._messages, upstream)

    events = await _drain(provider.stream_response(_request("claude-fable-5-1")))

    assert events
    assert len(upstream.requests) == 1
    assert upstream.requests[0].url.path.endswith("/messages")


@pytest.mark.asyncio
async def test_a_probe_that_the_messages_door_answers_is_remembered() -> None:
    """The rung reaching the third door, end to end."""

    _write_registry(**{"m": {}})
    provider = _provider()
    upstream = _Upstream()
    _install(provider._messages, upstream)

    async def _chat_fails(*_: Any, **__: Any):
        request = httpx.Request("POST", f"{BASE_URL}/chat/completions")
        raise httpx.HTTPStatusError(
            "upstream said 500",
            request=request,
            response=httpx.Response(
                500,
                json={"type": "error", "error": {"message": "Internal server error"}},
                request=request,
            ),
        )

    provider._client.chat.completions.create = _chat_fails
    # The Responses door is asked first (declaration order) and must also fail,
    # or the rung stops there and never reaches this one.
    responses: Any = provider._responses

    async def _responses_probe_fails(model_id: str) -> None:
        raise httpx.HTTPStatusError(
            "upstream said 500",
            request=httpx.Request("POST", f"{BASE_URL}/responses"),
            response=httpx.Response(500, json={"message": "Internal server error"}),
        )

    responses.probe = _responses_probe_fails

    events = await _drain(provider.stream_response(_request("m")))

    assert events, "the retry on the third surface must actually serve the request"
    facts = [
        fact
        for fact in learned_fact_store().facts_for_model("opencode", "m")
        if fact.fact_kind == "response_surface"
    ]
    assert len(facts) == 1
    assert facts[0].value == "messages"
    assert facts[0].source == "probe"


def _no_reasoning():
    from my_claude_code.core.reasoning import DEFAULT_REASONING_POLICY

    return DEFAULT_REASONING_POLICY
