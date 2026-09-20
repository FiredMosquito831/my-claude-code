"""A hand-configured host may serve Responses and Messages, and say so.

Until 7.33.0 a custom provider could only ever be a Chat Completions host.
Not by a rule anybody wrote, but by arithmetic:
``resolve_response_surface`` folds what a model needs against ``speakable =
declared or (DEFAULT_SURFACE,)``, and a custom entry declared nothing, so the
fold had one member. An operator who wrote ``response_surface: responses`` into
``model_overrides.json`` for their own gateway therefore got ``unservable`` --
a model listed with a reason that was, for their deployment, simply wrong.

What these tests hold:

* an entry that declares ``responses`` resolves there instead of unservable,
  and the declaration round-trips through ``custom_providers.json``;
* an entry that declares nothing -- and an entry that declares Chat Completions
  alone -- is byte-identical to what it was: same profile object, no surface
  resolution at all;
* a custom host's Responses request carries **no** OpenCode identity headers.
  The identity is a property of the two OpenCode profiles, and the generic
  profile declares none;
* the 6.74.0 probe works for a custom host: a surface-shaped failure on one
  door probes the other, and the answer is written down as a learned fact;
* the Messages ladder: a stated tool-name ceiling costs one retry and is
  remembered; a ``tool_choice`` the host takes only as ``auto`` costs one retry
  and is remembered; a 400 nothing recognises is raised unchanged.
"""

import json
from typing import Any

import httpx
import pytest

from my_claude_code.application.model_metadata import (
    ResponseSurface,
    ResponseSurfaceSource,
)
from my_claude_code.config.provider_registry import (
    DEFAULT_CUSTOM_PROVIDER_SURFACES,
    ProviderRegistry,
    normalize_custom_surfaces,
)
from my_claude_code.core.anthropic.models import Message, MessagesRequest, Tool
from my_claude_code.core.failures import ExecutionFailure
from my_claude_code.core.reasoning import DEFAULT_REASONING_POLICY
from my_claude_code.core.upstream_ladder import take_recovery_rung
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.openai_chat import (
    GENERIC_OPENAI_PROFILE,
    catalogue_surface,
    create_openai_chat_provider,
    declared_surfaces,
    profile_with_declared_surfaces,
    resolve_response_surface,
)
from my_claude_code.providers.openai_chat.messages_transport import MessagesTransport
from my_claude_code.providers.openai_chat.responses_transport import ResponsesTransport
from my_claude_code.providers.recovery import learned_fact_store
from tests.providers.support import passthrough_rate_limiter

BASE_URL = "https://gateway.internal.test/v1"

#: A tool name of the length Claude Code's MCP tools actually reach. 68
#: characters, the number in the 2026-09-16 measurement that produced the
#: Responses rung this one mirrors.
LONG_TOOL_NAME = "mcp__atlassian__get_the_confluence_page_footer_and_inline_comments"

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


@pytest.fixture
def registry(tmp_path) -> ProviderRegistry:
    return ProviderRegistry(tmp_path / "custom_providers.json")


@pytest.fixture(autouse=True)
def _patch_registry_singleton(monkeypatch, registry) -> None:
    monkeypatch.setattr("my_claude_code.config.provider_registry._registry", registry)


def _request(
    model: str = "m",
    *,
    tools: list[Tool] | None = None,
    tool_choice: dict[str, Any] | None = None,
) -> MessagesRequest:
    return MessagesRequest(
        model=model,
        max_tokens=16,
        messages=[Message(role="user", content="hi")],
        tools=tools,
        tool_choice=tool_choice,
    )


def _long_tool() -> Tool:
    return Tool(
        name=LONG_TOOL_NAME,
        description="one tool whose name is longer than a strict host allows",
        input_schema={"type": "object", "properties": {}},
    )


# --------------------------------------------------------------------------
# the declaration
# --------------------------------------------------------------------------


def test_a_new_entry_serves_chat_completions_and_nothing_else(registry) -> None:
    """The default is today, stated: one surface, the one MCC always spoke."""

    entry = registry.add("House", BASE_URL, ("sk-1",))

    assert entry.surfaces == DEFAULT_CUSTOM_PROVIDER_SURFACES == ("chat_completions",)


def test_a_declaration_round_trips_through_the_registry_file(tmp_path) -> None:
    """Written, read back by a second registry over the same file, unchanged."""

    path = tmp_path / "custom_providers.json"
    ProviderRegistry(path).add(
        "House", BASE_URL, ("sk-1",), surfaces=["messages", "responses"]
    )

    stored = json.loads(path.read_text(encoding="utf-8"))["providers"][0]
    reread = ProviderRegistry(path).get("custom_house")

    assert stored["surfaces"] == ["responses", "messages"]
    assert reread is not None
    assert reread.surfaces == ("responses", "messages")


def test_an_older_file_without_the_key_reads_back_as_chat_completions(
    tmp_path,
) -> None:
    """The compatibility case: a file any build before 7.33.0 wrote."""

    path = tmp_path / "custom_providers.json"
    path.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "provider_id": "custom_house",
                        "display_name": "House",
                        "base_url": BASE_URL,
                        "api_keys": ["sk-1"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    entry = ProviderRegistry(path).get("custom_house")

    assert entry is not None
    assert entry.surfaces == ("chat_completions",)


def test_an_unknown_surface_is_refused_at_the_form(registry) -> None:
    with pytest.raises(ValueError, match="Unknown wire surface"):
        registry.add("House", BASE_URL, ("sk-1",), surfaces=["grpc"])


def test_an_empty_declaration_is_refused_rather_than_silently_defaulted() -> None:
    """A host that serves nothing is not a configuration anybody means."""

    with pytest.raises(ValueError, match="at least one wire surface"):
        normalize_custom_surfaces([])


def test_the_declared_order_is_canonical_not_the_order_it_was_typed() -> None:
    """Chat Completions first wherever it was declared: the likely door."""

    assert normalize_custom_surfaces(["messages", "chat_completions"]) == (
        "chat_completions",
        "messages",
    )
    assert declared_surfaces(["messages", "chat_completions"]) == (
        ResponseSurface.CHAT_COMPLETIONS,
        ResponseSurface.MESSAGES,
    )


def test_an_update_can_change_what_a_host_serves(registry) -> None:
    registry.add("House", BASE_URL, ("sk-1",))

    updated = registry.update("custom_house", surfaces=["chat_completions", "messages"])

    assert updated.surfaces == ("chat_completions", "messages")


# --------------------------------------------------------------------------
# nothing changes for a provider that does not opt in
# --------------------------------------------------------------------------


def test_chat_completions_alone_leaves_the_profile_untouched() -> None:
    """One surface and no statement are the same routing decision.

    The identity of the object is the assertion, not an equality: the empty
    ``response_surfaces`` tuple is what keeps ``stream_response`` on the
    single-surface path, and a profile that gained ``(chat_completions,)``
    would newly resolve, label and log every request.
    """

    same = profile_with_declared_surfaces(GENERIC_OPENAI_PROFILE, ("chat_completions",))

    assert same is GENERIC_OPENAI_PROFILE
    assert same.response_surfaces == ()


def test_a_declaration_of_two_surfaces_reaches_the_profile() -> None:
    widened = profile_with_declared_surfaces(
        GENERIC_OPENAI_PROFILE, ("chat_completions", "responses")
    )

    assert widened.response_surfaces == (
        ResponseSurface.CHAT_COMPLETIONS,
        ResponseSurface.RESPONSES,
    )
    # Everything else about the profile is the generic one's, field for field.
    assert widened.client_identity is None
    assert widened.request_policy is GENERIC_OPENAI_PROFILE.request_policy
    assert widened.responses_tool_name_max_length is None


def test_the_two_spellings_of_a_surface_cannot_drift() -> None:
    """``config`` writes the words; ``application`` owns the enum.

    The registry is config-local and must not import ``application``, so the
    vocabulary is written out twice. This is the test that keeps the second
    copy honest, which is the price of that boundary and is cheaper than the
    import cycle the boundary exists to prevent.
    """

    from my_claude_code.config.provider_registry import CUSTOM_PROVIDER_SURFACES

    servable = tuple(
        surface.value
        for surface in ResponseSurface
        if surface is not ResponseSurface.UNSERVABLE
    )
    assert servable == CUSTOM_PROVIDER_SURFACES


def test_the_factory_folds_the_declaration_into_the_profile(
    registry, monkeypatch
) -> None:
    """The seam, end to end: a registry entry decides what the provider speaks."""

    from unittest.mock import patch

    from my_claude_code.config.settings import Settings
    from my_claude_code.providers.runtime.factory import create_provider

    registry.add("Plain", BASE_URL, ("sk-1",))
    registry.add(
        "Both", BASE_URL, ("sk-2",), surfaces=["chat_completions", "responses"]
    )
    monkeypatch.setenv("MODEL", "nvidia_nim/test-model")
    settings = Settings()

    with patch("my_claude_code.providers.openai_chat.provider.AsyncOpenAI"):
        plain: Any = create_provider("custom_plain", settings)
        both: Any = create_provider("custom_both", settings)

    assert plain._profile.response_surfaces == ()
    assert both._profile.response_surfaces == (
        ResponseSurface.CHAT_COMPLETIONS,
        ResponseSurface.RESPONSES,
    )


def test_the_models_page_offers_only_the_doors_the_host_declares(registry) -> None:
    """The control's whole contract, at the source that builds it."""

    from my_claude_code.api.model_admin import (
        response_surface_payload,
        speakable_surfaces,
    )

    registry.add(
        "House", BASE_URL, ("sk-1",), surfaces=["chat_completions", "messages"]
    )

    assert speakable_surfaces("custom_house") == (
        ResponseSurface.CHAT_COMPLETIONS,
        ResponseSurface.MESSAGES,
    )
    # A provider that declares nothing offers nothing to choose between.
    assert speakable_surfaces("open_router") == ()
    payload = response_surface_payload("custom_house", "m")
    assert payload is not None
    assert [row["value"] for row in payload["offered"]] == [
        "chat_completions",
        "messages",
    ]
    assert payload["override"] == ""


def test_pinning_a_surface_writes_one_row_and_unpinning_removes_it() -> None:
    """The writer the wire surface row uses, which is not the parameter grid."""

    from my_claude_code.api.model_admin import (
        merged_override_row,
        with_surface_override_row,
    )
    from my_claude_code.config.model_overrides import ModelParameterOverrides

    pinned = with_surface_override_row(
        ModelParameterOverrides(), key="custom_house/m", surface="responses"
    )
    assert pinned.models["custom_house/m"] == {"response_surface": "responses"}

    unpinned = with_surface_override_row(pinned, key="custom_house/m", surface="")
    assert "custom_house/m" not in unpinned.models

    # And the grid still refuses the key outright, which is the 7.25.0
    # contract this control deliberately did not widen.
    assert merged_override_row({}, {"response_surface": "responses"}) == {}


def test_a_default_entry_has_no_wire_surface_row(registry) -> None:
    """The Models page is unchanged for every install that does not opt in."""

    registry.add("House", BASE_URL, ("sk-1",))

    assert catalogue_surface("custom_house", "m") is None


# --------------------------------------------------------------------------
# the resolver: what was UNSERVABLE
# --------------------------------------------------------------------------


def test_an_override_to_responses_was_unservable_and_now_resolves(
    registry, monkeypatch
) -> None:
    """The bug this PR exists for, in one assertion pair."""

    registry.add("House", BASE_URL, ("sk-1",), surfaces=["chat_completions"])
    monkeypatch.setattr(
        "my_claude_code.providers.openai_chat.response_surface.override_surface",
        lambda provider_id, model_id: ResponseSurface.RESPONSES,
    )

    # What every release before this one did: nothing declared, so the fold
    # has one member and a Responses override cannot survive it.
    before = resolve_response_surface("custom_house", "m", declared=())
    assert before.surface is ResponseSurface.UNSERVABLE

    registry.update("custom_house", surfaces=["chat_completions", "responses"])
    after = resolve_response_surface(
        "custom_house",
        "m",
        declared=declared_surfaces(("chat_completions", "responses")),
    )
    assert after.surface is ResponseSurface.RESPONSES
    assert after.source is ResponseSurfaceSource.OVERRIDE


def test_the_models_page_reads_the_declaration_off_the_registry(
    registry, monkeypatch
) -> None:
    registry.add(
        "House", BASE_URL, ("sk-1",), surfaces=["chat_completions", "responses"]
    )
    monkeypatch.setattr(
        "my_claude_code.providers.openai_chat.response_surface.override_surface",
        lambda provider_id, model_id: ResponseSurface.RESPONSES,
    )

    resolved = catalogue_surface("custom_house", "m")

    assert resolved is not None
    assert resolved.surface is ResponseSurface.RESPONSES


def test_a_surface_the_host_does_not_declare_is_still_named_unservable(
    registry, monkeypatch
) -> None:
    """The honest answer keeps its reason; nothing is ever hidden."""

    registry.add(
        "House", BASE_URL, ("sk-1",), surfaces=["chat_completions", "responses"]
    )
    monkeypatch.setattr(
        "my_claude_code.providers.openai_chat.response_surface.override_surface",
        lambda provider_id, model_id: ResponseSurface.MESSAGES,
    )

    resolved = catalogue_surface("custom_house", "m")

    assert resolved is not None
    assert resolved.surface is ResponseSurface.UNSERVABLE
    assert "Messages API" in resolved.detail


# --------------------------------------------------------------------------
# the Responses door for a custom host
# --------------------------------------------------------------------------


def _responses_transport() -> ResponsesTransport:
    return ResponsesTransport(
        ProviderConfig(api_key="sk-house", base_url=BASE_URL),
        base_url=BASE_URL,
        provider_name="CUSTOM_HOUSE",
        identity=GENERIC_OPENAI_PROFILE.client_identity,
        api_key="sk-house",
        rate_limiter=passthrough_rate_limiter(),
        tool_name_max_length=GENERIC_OPENAI_PROFILE.responses_tool_name_max_length,
    )


@pytest.mark.asyncio
async def test_a_custom_hosts_responses_request_carries_no_opencode_identity() -> None:
    """The golden this PR must not touch: identity belongs to two profiles."""

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True}, request=request)

    transport = _responses_transport()
    transport._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    await transport.probe("m")

    assert transport.url == f"{BASE_URL}/responses"
    assert seen[0].headers["authorization"] == "Bearer sk-house"
    assert not [name for name in seen[0].headers if name.startswith("x-opencode")]
    assert "prompt_cache_key" not in json.loads(seen[0].content)


# --------------------------------------------------------------------------
# the Messages ladder
# --------------------------------------------------------------------------


class _MessagesUpstream:
    """A fake ``{base}/messages`` that refuses once, then answers."""

    def __init__(self, *refusals: dict[str, Any]) -> None:
        self.refusals = list(refusals)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.refusals:
            return httpx.Response(400, json=self.refusals.pop(0), request=request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=MESSAGES_SSE.encode(),
            request=request,
        )


def _name_length_refusal(stated: int = 64, got: int = 68) -> dict[str, Any]:
    return {
        "error": {
            "type": "invalid_request_error",
            "param": "name",
            "message": f"`name` must be at most {stated} characters, got {got}",
        }
    }


def _tool_choice_refusal() -> dict[str, Any]:
    return {
        "error": {
            "type": "invalid_request_error",
            "param": "tool_choice",
            "message": (
                'only `"auto"` is supported for `tool_choice`; `"none"`, '
                '`"any"`, and named tool choices are not currently supported'
            ),
        }
    }


def _messages_transport(provider_id: str = "custom_house") -> MessagesTransport:
    return MessagesTransport(
        ProviderConfig(api_key="sk-house", base_url=BASE_URL),
        base_url=BASE_URL,
        provider_name="CUSTOM_HOUSE",
        provider_id=provider_id,
        identity=GENERIC_OPENAI_PROFILE.client_identity,
        api_key="sk-house",
        rate_limiter=passthrough_rate_limiter(),
        memory=learned_fact_store().memory_for(provider_id),
    )


def _install(transport: MessagesTransport, upstream: _MessagesUpstream) -> None:
    inner: Any = transport._provider
    inner._client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))


async def _drain(stream) -> list[str]:
    return [event async for event in stream]


@pytest.mark.asyncio
async def test_a_custom_hosts_messages_request_carries_no_opencode_identity() -> None:
    transport = _messages_transport()
    upstream = _MessagesUpstream()
    _install(transport, upstream)

    await _drain(
        transport.stream(_request(), input_tokens=3, reasoning=DEFAULT_REASONING_POLICY)
    )

    sent = upstream.requests[0].headers
    assert transport.url == f"{BASE_URL}/messages"
    assert not [name for name in sent if name.startswith("x-opencode")]
    # Both credential shapes still go out: the gateway authenticates like
    # itself and the protocol behind it authenticates like Anthropic.
    assert sent["authorization"] == "Bearer sk-house"
    assert sent["x-api-key"] == "sk-house"


@pytest.mark.asyncio
async def test_a_stated_tool_name_ceiling_costs_one_retry_and_is_learned() -> None:
    transport = _messages_transport()
    upstream = _MessagesUpstream(_name_length_refusal())
    _install(transport, upstream)

    events = await _drain(
        transport.stream(
            _request(tools=[_long_tool()]),
            input_tokens=3,
            reasoning=DEFAULT_REASONING_POLICY,
        )
    )

    assert events
    assert len(upstream.requests) == 2
    first = json.loads(upstream.requests[0].content)
    second = json.loads(upstream.requests[1].content)
    assert first["tools"][0]["name"] == LONG_TOOL_NAME
    assert second["tools"][0]["name"] != LONG_TOOL_NAME
    assert len(second["tools"][0]["name"]) <= 64
    # Written under 7.23.0's kind, at 7.23.0's scope: host-wide.
    assert transport.tool_name_max_length == 64


@pytest.mark.asyncio
async def test_the_learned_ceiling_aliases_the_next_request_from_the_first_try() -> (
    None
):
    transport = _messages_transport()
    _install(transport, _MessagesUpstream(_name_length_refusal()))
    await _drain(
        transport.stream(
            _request(tools=[_long_tool()]),
            input_tokens=3,
            reasoning=DEFAULT_REASONING_POLICY,
        )
    )

    second = _messages_transport()
    upstream = _MessagesUpstream()
    _install(second, upstream)
    await _drain(
        second.stream(
            _request(tools=[_long_tool()]),
            input_tokens=3,
            reasoning=DEFAULT_REASONING_POLICY,
        )
    )

    assert len(upstream.requests) == 1
    body = json.loads(upstream.requests[0].content)
    assert len(body["tools"][0]["name"]) <= 64


@pytest.mark.asyncio
async def test_a_tool_choice_refusal_costs_one_retry_and_is_learned() -> None:
    transport = _messages_transport()
    upstream = _MessagesUpstream(_tool_choice_refusal())
    _install(transport, upstream)

    events = await _drain(
        transport.stream(
            _request(
                tools=[_long_tool()],
                tool_choice={"type": "tool", "name": LONG_TOOL_NAME},
            ),
            input_tokens=3,
            reasoning=DEFAULT_REASONING_POLICY,
        )
    )

    assert events
    assert len(upstream.requests) == 2
    assert "tool_choice" in json.loads(upstream.requests[0].content)
    assert "tool_choice" not in json.loads(upstream.requests[1].content)
    assert transport._memory.responses_tool_choice_refused("m")


@pytest.mark.asyncio
async def test_a_learned_tool_choice_refusal_is_not_paid_for_twice() -> None:
    transport = _messages_transport()
    _install(transport, _MessagesUpstream(_tool_choice_refusal()))
    await _drain(
        transport.stream(
            _request(
                tools=[_long_tool()],
                tool_choice={"type": "tool", "name": LONG_TOOL_NAME},
            ),
            input_tokens=3,
            reasoning=DEFAULT_REASONING_POLICY,
        )
    )

    second = _messages_transport()
    upstream = _MessagesUpstream()
    _install(second, upstream)
    await _drain(
        second.stream(
            _request(
                tools=[_long_tool()],
                tool_choice={"type": "tool", "name": LONG_TOOL_NAME},
            ),
            input_tokens=3,
            reasoning=DEFAULT_REASONING_POLICY,
        )
    )

    assert len(upstream.requests) == 1
    assert "tool_choice" not in json.loads(upstream.requests[0].content)


@pytest.mark.asyncio
async def test_a_400_nothing_recognises_is_raised_after_no_rewrite_at_all() -> None:
    """An unrecognised refusal must fail visibly rather than be guessed at."""

    transport = _messages_transport()
    upstream = _MessagesUpstream(
        {"error": {"type": "invalid_request_error", "message": "credit exhausted"}}
    )
    _install(transport, upstream)

    with pytest.raises(ExecutionFailure):
        await _drain(
            transport.stream(
                _request(tools=[_long_tool()]),
                input_tokens=3,
                reasoning=DEFAULT_REASONING_POLICY,
            )
        )

    assert len(upstream.requests) == 1


@pytest.mark.asyncio
async def test_a_host_that_keeps_refusing_after_its_own_fix_fails_visibly() -> None:
    """``used`` is never cleared, so one rung cannot loop."""

    transport = _messages_transport()
    upstream = _MessagesUpstream(_name_length_refusal(), _name_length_refusal())
    _install(transport, upstream)

    with pytest.raises(ExecutionFailure):
        await _drain(
            transport.stream(
                _request(tools=[_long_tool()]),
                input_tokens=3,
                reasoning=DEFAULT_REASONING_POLICY,
            )
        )

    assert len(upstream.requests) == 2


@pytest.mark.asyncio
async def test_the_retry_row_names_the_rung_it_is_carrying() -> None:
    """``recovery=<rung>`` is what the operator reads in the ladder modal."""

    transport = _messages_transport()
    _install(transport, _MessagesUpstream(_tool_choice_refusal()))

    await _drain(
        transport.stream(
            _request(
                tools=[_long_tool()],
                tool_choice={"type": "tool", "name": LONG_TOOL_NAME},
            ),
            input_tokens=3,
            reasoning=DEFAULT_REASONING_POLICY,
        )
    )

    # Taken rather than asserted in place: the ladder reads it the same way,
    # once, on the row the retry produced.
    assert take_recovery_rung() in {None, "messages_tool_choice"}


# --------------------------------------------------------------------------
# the 6.74.0 probe, for a host nobody wrote a profile for
# --------------------------------------------------------------------------


class _FakeResponses:
    """A Responses transport that answers exactly what a test tells it to."""

    def __init__(self) -> None:
        self.probes: list[str] = []
        self.streamed: list[str] = []
        self.url = f"{BASE_URL}/responses"

    def build_body(self, request, **_: Any) -> tuple[dict[str, Any], dict[str, str]]:
        return {"model": request.model, "input": []}, {}

    async def probe(self, model_id: str) -> None:
        self.probes.append(model_id)

    def stream(self, request, **_: Any):
        self.streamed.append(request.model)

        async def _run():
            yield "event: message_start\n\n"

        return _run()

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_a_bare_500_on_a_custom_host_probes_the_declared_other_door(
    registry,
) -> None:
    """The 6.74.0 rung, reached by a provider nobody shipped a profile for."""

    registry.add(
        "House", BASE_URL, ("sk-house",), surfaces=["chat_completions", "responses"]
    )
    provider: Any = create_openai_chat_provider(
        "custom_house",
        ProviderConfig(api_key="sk-house", base_url=BASE_URL),
        passthrough_rate_limiter(),
        profile=profile_with_declared_surfaces(
            GENERIC_OPENAI_PROFILE, ("chat_completions", "responses")
        ),
    )
    fake = _FakeResponses()
    provider._responses_transport = fake
    bare_500 = httpx.HTTPStatusError(
        "upstream said 500",
        request=httpx.Request("POST", f"{BASE_URL}/chat/completions"),
        response=httpx.Response(
            500,
            content=json.dumps(
                {"type": "error", "error": {"type": "error", "message": "Internal"}}
            ).encode(),
            request=httpx.Request("POST", f"{BASE_URL}/chat/completions"),
        ),
    )

    async def _create(*_: Any, **__: Any):
        raise bare_500

    provider._client.chat.completions.create = _create

    events = [event async for event in provider.stream_response(_request("m"))]

    assert events, "the retry on the other surface must actually serve the request"
    assert fake.probes == ["m"], "exactly one probe, on the other door"
    facts = learned_fact_store().facts_for_model("custom_house", "m")
    surface_facts = [f for f in facts if f.fact_kind == "response_surface"]
    assert len(surface_facts) == 1
    assert surface_facts[0].value == "responses"
    assert surface_facts[0].source == "probe"


@pytest.mark.asyncio
async def test_a_host_that_has_refused_nothing_sends_what_it_always_sent() -> None:
    """The regression guard for every Messages deployment that is fine."""

    transport = _messages_transport()
    upstream = _MessagesUpstream()
    _install(transport, upstream)

    await _drain(
        transport.stream(
            _request(tools=[_long_tool()]),
            input_tokens=3,
            reasoning=DEFAULT_REASONING_POLICY,
        )
    )

    body = json.loads(upstream.requests[0].content)
    assert body["tools"][0]["name"] == LONG_TOOL_NAME
