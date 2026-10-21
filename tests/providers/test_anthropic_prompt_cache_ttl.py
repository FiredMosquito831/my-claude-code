"""The prompt-cache lifetime on the Anthropic Messages path (7.104.0).

Pinned here, on the outbound copy only:

* nothing chosen -> the builder never reaches the lifetime step, and the step
  given nothing returns the very body it was given;
* a lifetime chosen -> every marker the client wrote carries it (other keys
  kept, none added or removed); a marker without ``ttl`` stays without one
  when the choice is Anthropic's default ``5m``; a request with no marker at
  all gets one top-level marker;
* only the two first-party Anthropic providers ever choose one: Command Code
  and the custom / OpenCode Messages doors send the client's bytes;
* a host's 400 naming the lifetime field records a learned refusal and the
  next request adapts to the lifetime that remains; a 400 mentioning
  ``cache_control`` for any other reason records nothing; the refused request
  fails exactly as an ordinary 400 does, with no extra call.
"""

import copy
import json
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import httpx
import pytest

from my_claude_code.application.prompt_cache import (
    PromptCacheLifetimeChoice,
    choose_prompt_cache_lifetime,
)
from my_claude_code.config.prompt_cache_lifetimes import (
    ANTHROPIC_MESSAGES_LIFETIMES,
    PROMPT_CACHE_LIFETIME_FAMILIES,
)
from my_claude_code.core.anthropic.models import MessagesRequest, PromptCacheAsk
from my_claude_code.core.failures import ExecutionFailure
from my_claude_code.core.reasoning import DEFAULT_REASONING_POLICY
from my_claude_code.core.wire_capture import install_wire_trace
from my_claude_code.providers.anthropic import AnthropicProvider
from my_claude_code.providers.anthropic_messages import AnthropicMessagesProvider
from my_claude_code.providers.anthropic_messages import request as request_module
from my_claude_code.providers.anthropic_messages.prompt_cache import (
    prompt_cache_refusal,
    refused_prompt_cache_lifetimes,
)
from my_claude_code.providers.anthropic_messages.request import (
    PROMPT_CACHE_TTL_NOTE,
    apply_prompt_cache_lifetime,
    build_anthropic_messages_body,
)
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.commandcode import CommandCodeProvider
from my_claude_code.providers.recovery import (
    FACT_PROMPT_CACHE_LIFETIME_REFUSED,
    learned_fact_store,
)
from tests.providers.support import passthrough_rate_limiter

ANTHROPIC = PROMPT_CACHE_LIFETIME_FAMILIES[ANTHROPIC_MESSAGES_LIFETIMES]
MODEL = "claude-opus-5-5"
_EPHEMERAL = {"type": "ephemeral"}
_SSE = (
    b'event: message_start\ndata: {"type":"message_start"}\n\n'
    b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
)


def _claude_code_payload(**extra: Any) -> dict[str, Any]:
    """Markers where Claude Code puts them, some with its own ``1h``."""

    return {
        "model": MODEL,
        "max_tokens": 16000,
        "system": [
            {"type": "text", "text": "Billing header."},
            {
                "type": "text",
                "text": "You are Claude Code.",
                "cache_control": {"type": "ephemeral", "ttl": "1h", "scope": "global"},
            },
        ],
        "tools": [
            {
                "name": "Read",
                "description": "Read a file.",
                "input_schema": {"type": "object", "properties": {}},
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            }
        ],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "Read a.py"}]},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "Read",
                        "input": {"file_path": "a.py"},
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
                                "type": "text",
                                "text": "x = 1",
                                "cache_control": _EPHEMERAL,
                            }
                        ],
                    },
                    {
                        "type": "text",
                        "text": "Explain it.",
                        "cache_control": {
                            "type": "ephemeral",
                            "ttl": "1h",
                            "evict_on_complete": True,
                        },
                    },
                ],
            },
        ],
        **extra,
    }


def _bare_payload() -> dict[str, Any]:
    return {
        "model": MODEL,
        "max_tokens": 1000,
        "system": "Plain system prompt.",
        "messages": [{"role": "user", "content": "Hello."}],
    }


def _request(
    payload: dict[str, Any], ask: PromptCacheAsk | None = None
) -> MessagesRequest:
    request = MessagesRequest.model_validate(copy.deepcopy(payload))
    if ask is not None:
        request.set_prompt_cache_ask(ask)
    return request


def _choice(minutes: int, refused: tuple[str, ...] = ()) -> PromptCacheLifetimeChoice:
    choice = choose_prompt_cache_lifetime(
        PromptCacheAsk(minutes, "PROMPT_CACHE_TTL"), ANTHROPIC, refused=refused
    )
    assert choice is not None
    return choice


def _markers(body: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "cache_control" and isinstance(value, dict):
                    found.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(body)
    return found


def _wire(body: Any) -> bytes:
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()


# -- nothing chosen: identity ----------------------------------------------


def test_the_step_given_nothing_returns_the_very_body() -> None:
    body = build_anthropic_messages_body(
        _request(_claude_code_payload()), reasoning=DEFAULT_REASONING_POLICY
    )
    before = copy.deepcopy(body)
    notes: dict[str, Any] = {}

    assert apply_prompt_cache_lifetime(body, None, notes) is body
    assert body == before
    assert notes == {}


def test_the_builder_never_reaches_the_step_without_a_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the lifetime step ran with nothing chosen")

    monkeypatch.setattr(request_module, "apply_prompt_cache_lifetime", refuse)

    for payload in (_claude_code_payload(), _bare_payload()):
        # A stamped request is not a choice: only a provider makes one.
        request = _request(payload, PromptCacheAsk(30, "PROMPT_CACHE_TTL"))
        build_anthropic_messages_body(request, reasoning=DEFAULT_REASONING_POLICY)
        build_anthropic_messages_body(
            request, reasoning=DEFAULT_REASONING_POLICY, prompt_cache=None
        )


# -- a lifetime chosen -----------------------------------------------------


def test_thirty_minutes_puts_one_hour_on_every_marker_and_keeps_the_rest() -> None:
    plain = build_anthropic_messages_body(
        _request(_claude_code_payload()), reasoning=DEFAULT_REASONING_POLICY
    )
    notes: dict[str, Any] = {}
    sent = build_anthropic_messages_body(
        _request(_claude_code_payload()),
        reasoning=DEFAULT_REASONING_POLICY,
        wire_notes=notes,
        prompt_cache=_choice(30),
    )

    markers = _markers(sent)
    assert len(markers) == len(_markers(plain)) == 4
    assert all(marker["ttl"] == "1h" for marker in markers)
    assert sent["system"][1]["cache_control"] == {
        "type": "ephemeral",
        "ttl": "1h",
        "scope": "global",
    }
    assert sent["messages"][2]["content"][1]["cache_control"]["evict_on_complete"]
    assert "cache_control" not in sent
    for marker in _markers(plain):
        marker["ttl"] = "1h"
    assert sent == plain
    assert notes[PROMPT_CACHE_TTL_NOTE] == {
        "requested": 30,
        "from": "PROMPT_CACHE_TTL",
        "sent": "1h",
        "host_lifetimes": ["5m", "1h"],
        "declared_for": "Anthropic Messages",
        "markers": 4,
        "retimed": 1,
        "marker_added": False,
    }


def test_three_minutes_writes_five_minutes_over_a_clients_hour() -> None:
    notes: dict[str, Any] = {}
    sent = build_anthropic_messages_body(
        _request(_claude_code_payload()),
        reasoning=DEFAULT_REASONING_POLICY,
        wire_notes=notes,
        prompt_cache=_choice(3),
    )

    assert sent["system"][1]["cache_control"]["ttl"] == "5m"
    assert sent["tools"][0]["cache_control"] == {"type": "ephemeral", "ttl": "5m"}
    # The marker the client wrote without a lifetime already has Anthropic's
    # default one, and stays exactly as written.
    assert (
        sent["messages"][2]["content"][0]["content"][0]["cache_control"] == _EPHEMERAL
    )
    assert notes[PROMPT_CACHE_TTL_NOTE]["retimed"] == 3


def test_a_request_with_no_marker_gets_exactly_one_top_level_marker() -> None:
    plain = build_anthropic_messages_body(
        _request(_bare_payload()), reasoning=DEFAULT_REASONING_POLICY
    )
    notes: dict[str, Any] = {}
    sent = build_anthropic_messages_body(
        _request(_bare_payload()),
        reasoning=DEFAULT_REASONING_POLICY,
        wire_notes=notes,
        prompt_cache=_choice(45),
    )

    assert sent.pop("cache_control") == {"type": "ephemeral", "ttl": "1h"}
    assert sent == plain
    assert notes[PROMPT_CACHE_TTL_NOTE]["marker_added"] is True


def test_a_clients_top_level_marker_is_retimed_never_doubled() -> None:
    payload = {**_bare_payload(), "cache_control": {"type": "ephemeral"}}
    sent = build_anthropic_messages_body(
        _request(payload), reasoning=DEFAULT_REASONING_POLICY, prompt_cache=_choice(60)
    )

    assert sent["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert len(_markers(sent)) == 1


def test_a_host_that_refused_everything_gets_the_clients_bytes() -> None:
    plain = build_anthropic_messages_body(
        _request(_claude_code_payload()), reasoning=DEFAULT_REASONING_POLICY
    )
    notes: dict[str, Any] = {}
    sent = build_anthropic_messages_body(
        _request(_claude_code_payload()),
        reasoning=DEFAULT_REASONING_POLICY,
        wire_notes=notes,
        prompt_cache=_choice(30, refused=("5m", "1h")),
    )

    assert _wire(sent) == _wire(plain)
    assert notes[PROMPT_CACHE_TTL_NOTE]["sent"] is None


# -- which providers choose ------------------------------------------------


class _FakeHost:
    """Records every body; refuses a ``ttl`` it does not know, naming the field."""

    def __init__(self, accepted: frozenset[str] = frozenset({"5m", "1h"})) -> None:
        self.accepted = accepted
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        for path, marker in _marker_paths(body):
            ttl = marker.get("ttl")
            if ttl is not None and ttl not in self.accepted:
                return httpx.Response(
                    400,
                    json={
                        "type": "error",
                        "error": {
                            "type": "invalid_request_error",
                            "message": f"{path}.cache_control.ephemeral.ttl: Input "
                            f"should be {' or '.join(sorted(self.accepted))}",
                        },
                    },
                )
        return httpx.Response(200, content=_SSE)


def _marker_paths(body: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Each marker with the field path Anthropic names it by (block tags in)."""

    if isinstance(body.get("cache_control"), dict):
        yield "", body["cache_control"]
    for key in ("system", "tools"):
        for index, item in enumerate(body.get(key) or []):
            if isinstance(item, dict) and isinstance(item.get("cache_control"), dict):
                tag = item.get("type", "custom")
                yield f"{key}.{index}.{tag}", item["cache_control"]
    for m_index, message in enumerate(body.get("messages") or []):
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for b_index, block in enumerate(content):
            prefix = f"messages.{m_index}.content.{b_index}.{block.get('type')}"
            if isinstance(block.get("cache_control"), dict):
                yield prefix, block["cache_control"]
            for n_index, inner in enumerate(block.get("content") or []):
                if isinstance(inner, dict) and isinstance(
                    inner.get("cache_control"), dict
                ):
                    yield (
                        f"{prefix}.content.{n_index}.{inner.get('type')}",
                        inner["cache_control"],
                    )


def _anthropic(host: Callable[[httpx.Request], httpx.Response]) -> AnthropicProvider:
    provider = AnthropicProvider(
        ProviderConfig(
            api_key="sk-ant-api03-test", base_url="https://api.anthropic.invalid/v1"
        ),
        rate_limiter=passthrough_rate_limiter(),
        provider_id="anthropic",
    )
    provider._messages._client = httpx.AsyncClient(transport=httpx.MockTransport(host))
    return provider


def _gateway(
    host: Callable[[httpx.Request], httpx.Response],
) -> AnthropicMessagesProvider:
    """Command Code's / a custom Messages door's shape: no declared lifetimes."""

    provider = AnthropicMessagesProvider(
        ProviderConfig(api_key="user_test", base_url="https://gateway.invalid/v1"),
        provider_name="COMMANDCODE",
        rate_limiter=passthrough_rate_limiter(),
        provider_id="commandcode",
    )
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(host))
    return provider


async def _drain(stream: AsyncIterator[str]) -> list[str]:
    return [frame async for frame in stream]


@pytest.mark.asyncio
async def test_the_anthropic_provider_sends_the_chosen_lifetime() -> None:
    host = _FakeHost()
    provider = _anthropic(host)
    trace = install_wire_trace()

    await _drain(
        provider.stream_response(
            _request(
                _claude_code_payload(), PromptCacheAsk(30, "PROMPT_CACHE_TTL_OPUS")
            )
        )
    )

    assert len(host.bodies) == 1
    assert all(marker["ttl"] == "1h" for marker in _markers(host.bodies[0]))
    note = trace.requests[max(trace.requests)].params[PROMPT_CACHE_TTL_NOTE]
    assert note["requested"] == 30
    assert note["from"] == "PROMPT_CACHE_TTL_OPUS"
    assert note["sent"] == "1h"
    assert note["host_lifetimes"] == ["5m", "1h"]
    await provider.cleanup()


@pytest.mark.asyncio
async def test_an_unstamped_request_goes_out_in_the_builders_own_bytes() -> None:
    host = _FakeHost()
    provider = _anthropic(host)
    trace = install_wire_trace()

    await _drain(provider.stream_response(_request(_claude_code_payload())))

    expected = build_anthropic_messages_body(
        _request(_claude_code_payload()), reasoning=DEFAULT_REASONING_POLICY
    )
    assert _wire(host.bodies[0]) == _wire(expected)
    assert PROMPT_CACHE_TTL_NOTE not in trace.requests[max(trace.requests)].params
    await provider.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [_claude_code_payload(), _bare_payload()])
async def test_a_gateway_without_declared_lifetimes_sends_the_clients_bytes(
    payload: dict[str, Any],
) -> None:
    stamped, plain = _FakeHost(), _FakeHost()
    for host, ask in ((stamped, PromptCacheAsk(30, "PROMPT_CACHE_TTL")), (plain, None)):
        provider = _gateway(host)
        trace = install_wire_trace()
        await _drain(provider.stream_response(_request(payload, ask)))
        assert PROMPT_CACHE_TTL_NOTE not in trace.requests[max(trace.requests)].params
        await provider.cleanup()

    assert _wire(stamped.bodies[0]) == _wire(plain.bodies[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [_claude_code_payload(), _bare_payload()])
async def test_the_real_command_code_provider_sends_the_clients_bytes(
    payload: dict[str, Any],
) -> None:
    """Command Code's ``claude-*`` route, on the shipped provider class.

    Command Code has no base-URL setting, so no scratch server can point it
    at a fake; this drives its own composed Messages delegate instead.
    """

    stamped, plain = _FakeHost(), _FakeHost()
    for host, ask in ((stamped, PromptCacheAsk(30, "PROMPT_CACHE_TTL")), (plain, None)):
        provider = CommandCodeProvider(
            ProviderConfig(
                api_key="user_test", base_url="https://commandcode.invalid/v1"
            ),
            rate_limiter=passthrough_rate_limiter(),
        )
        provider._anthropic._client = httpx.AsyncClient(
            transport=httpx.MockTransport(host)
        )
        trace = install_wire_trace()
        await _drain(provider.stream_response(_request(payload, ask)))
        assert PROMPT_CACHE_TTL_NOTE not in trace.requests[max(trace.requests)].params
        await provider.cleanup()

    assert len(stamped.bodies) == len(plain.bodies) == 1
    assert _wire(stamped.bodies[0]) == _wire(plain.bodies[0])


# -- a host that refuses ---------------------------------------------------


@pytest.mark.asyncio
async def test_a_refused_hour_is_learned_and_the_next_request_sends_five_minutes() -> (
    None
):
    host = _FakeHost(accepted=frozenset({"5m"}))
    provider = _anthropic(host)
    ask = PromptCacheAsk(30, "PROMPT_CACHE_TTL")

    with pytest.raises(ExecutionFailure) as refused:
        await _drain(provider.stream_response(_request(_claude_code_payload(), ask)))

    assert len(host.bodies) == 1  # no new retry
    assert refused_prompt_cache_lifetimes("anthropic", MODEL) == frozenset({"1h"})
    fact = next(
        fact
        for fact in learned_fact_store().facts_for_model("anthropic", MODEL)
        if fact.fact_kind == FACT_PROMPT_CACHE_LIFETIME_REFUSED
    )
    assert fact.detail == "1h"
    assert "cache_control.ephemeral.ttl" in fact.evidence

    trace = install_wire_trace()
    await _drain(provider.stream_response(_request(_claude_code_payload(), ask)))

    sent = host.bodies[-1]
    assert sent["system"][1]["cache_control"]["ttl"] == "5m"
    assert sent["tools"][0]["cache_control"]["ttl"] == "5m"
    assert (
        sent["messages"][2]["content"][0]["content"][0]["cache_control"] == _EPHEMERAL
    )
    note = trace.requests[max(trace.requests)].params[PROMPT_CACHE_TTL_NOTE]
    assert note["sent"] == "5m"
    assert note["refused_by_host"] == ["1h"]
    assert refused.value is not None
    await provider.cleanup()


@pytest.mark.asyncio
async def test_the_refused_request_fails_exactly_as_an_ordinary_400() -> None:
    """Same exception, same kind, same status, same number of calls."""

    learned_host = _FakeHost(accepted=frozenset({"5m"}))
    learned = _anthropic(learned_host)
    with pytest.raises(ExecutionFailure) as with_lifetime:
        await _drain(
            learned.stream_response(
                _request(_claude_code_payload(), PromptCacheAsk(30, "PROMPT_CACHE_TTL"))
            )
        )
    # The client's own "1h", nothing chosen: the same 400 MCC passes on today.
    plain_host = _FakeHost(accepted=frozenset({"5m"}))
    plain = _anthropic(plain_host)
    with pytest.raises(ExecutionFailure) as without:
        await _drain(plain.stream_response(_request(_claude_code_payload())))

    assert type(with_lifetime.value) is type(without.value)
    assert with_lifetime.value.kind is without.value.kind
    assert with_lifetime.value.status_code == without.value.status_code == 400
    assert with_lifetime.value.retryable == without.value.retryable
    assert len(learned_host.bodies) == len(plain_host.bodies) == 1
    # Nothing was chosen on the plain run, so nothing was learned from it.
    assert len(learned_fact_store().all_facts()) == 1
    await learned.cleanup()
    await plain.cleanup()


@pytest.mark.asyncio
async def test_forgetting_the_refusal_sends_the_hour_again() -> None:
    host = _FakeHost(accepted=frozenset({"5m"}))
    provider = _anthropic(host)
    ask = PromptCacheAsk(30, "PROMPT_CACHE_TTL")
    with pytest.raises(ExecutionFailure):
        await _drain(provider.stream_response(_request(_claude_code_payload(), ask)))

    learned_fact_store().forget(
        "anthropic", MODEL, FACT_PROMPT_CACHE_LIFETIME_REFUSED, "1h"
    )
    host.accepted = frozenset({"5m", "1h"})
    await _drain(provider.stream_response(_request(_claude_code_payload(), ask)))

    assert all(marker["ttl"] == "1h" for marker in _markers(host.bodies[-1]))
    await provider.cleanup()


# -- what counts as naming the lifetime ------------------------------------


def _sent_body(minutes: int, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return build_anthropic_messages_body(
        _request(payload or _claude_code_payload()),
        reasoning=DEFAULT_REASONING_POLICY,
        prompt_cache=_choice(minutes),
    )


def _refusal(message: str) -> bytes:
    return json.dumps(
        {
            "type": "error",
            "error": {"type": "invalid_request_error", "message": message},
        }
    ).encode()


@pytest.mark.parametrize(
    "message",
    [
        "system.1.text.cache_control.ttl: Input should be '5m'",
        "system.1.cache_control.ephemeral.ttl: Input should be '5m'",
        "tools.0.custom.cache_control.ttl: Input should be '5m'",
        "messages.2.content.1.text.cache_control.ephemeral.ttl: Input should be '5m'",
        "messages.2.content.0.tool_result.content.0.text.cache_control.ttl: bad",
        "body.messages.2.content.1.cache_control.ttl: Extra inputs are not permitted",
    ],
)
def test_a_400_naming_a_markers_lifetime_field_is_a_refusal(message: str) -> None:
    body = _sent_body(30)
    choice = _choice(30)

    found = prompt_cache_refusal(
        400, _refusal(message), body=body, choice=choice, marker_added=False
    )

    assert found == ("1h", message)


@pytest.mark.parametrize(
    "message",
    [
        "A maximum of 4 blocks with cache_control may be provided. Found 5.",
        "messages.2.content.1.text.cache_control: Extra inputs are not permitted",
        "cache_control: Extra inputs are not permitted",
        "messages.0.content.0.text.cache_control.ttl: Input should be '5m'",
        "messages.9.content.0.text.cache_control.ttl: Input should be '5m'",
        "thinking.adaptive.enabled: Extra inputs are not permitted",
        "Prompt caching with cache_control.ttl is not available.",
    ],
)
def test_a_400_about_cache_control_for_another_reason_is_not(message: str) -> None:
    """Nothing here names the lifetime field of a marker carrying ``1h``.

    ``messages.0.content.0`` carries no marker at all, ``messages.9`` does not
    exist, a marker refused as a whole is the client's marker, and a top-level
    ``cache_control`` MCC did not add is the client's too.
    """

    found = prompt_cache_refusal(
        400,
        _refusal(message),
        body=_sent_body(30),
        choice=_choice(30),
        marker_added=False,
    )

    assert found is None


def test_a_lifetime_message_on_a_marker_mcc_left_alone_is_not_a_refusal() -> None:
    """At ``5m`` the ttl-less tool_result marker keeps no ttl: nothing was sent there."""

    message = "messages.2.content.0.tool_result.content.0.text.cache_control.ttl: bad"
    found = prompt_cache_refusal(
        400,
        _refusal(message),
        body=_sent_body(3),
        choice=_choice(3),
        marker_added=False,
    )

    assert found is None


@pytest.mark.parametrize("status", [401, 403, 404, 413, 422, 429, 500, 529])
def test_only_a_400_is_read(status: int) -> None:
    message = "system.1.cache_control.ttl: Input should be '5m'"

    found = prompt_cache_refusal(
        status,
        _refusal(message),
        body=_sent_body(30),
        choice=_choice(30),
        marker_added=False,
    )

    assert found is None


def test_the_top_level_marker_mcc_added_refused_as_a_field_is_a_refusal() -> None:
    body = _sent_body(30, _bare_payload())
    for message in (
        "cache_control: Extra inputs are not permitted",
        "cache_control.ttl: Input should be '5m'",
    ):
        assert prompt_cache_refusal(
            400, _refusal(message), body=body, choice=_choice(30), marker_added=True
        ) == ("1h", message)


def test_nothing_chosen_or_nothing_sent_reads_nothing() -> None:
    message = _refusal("system.1.cache_control.ttl: Input should be '5m'")
    body = _sent_body(30)

    assert (
        prompt_cache_refusal(400, message, body=body, choice=None, marker_added=False)
        is None
    )
    assert (
        prompt_cache_refusal(
            400,
            message,
            body=body,
            choice=_choice(30, refused=("5m", "1h")),
            marker_added=False,
        )
        is None
    )
    assert (
        prompt_cache_refusal(
            400, b"not json", body=body, choice=_choice(30), marker_added=False
        )
        is None
    )
