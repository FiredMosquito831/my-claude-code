"""Override rows choosing a prompt-cache lifetime on the real providers (7.105.0).

On the shipped provider classes, with a recording transport in place of the
host (no network):

* the Claude subscription: a model row, a provider row and a model row's
  Force unset each decide, in the user's order (model row -> provider row ->
  tier -> global -> client), and ``params.wire.prompt_cache_ttl.from`` names
  the row; the tier still applies to a sibling model;
* Command Code's ``claude-*`` models and OpenCode Zen's Messages models (the
  gateways of Q7): with no row they send exactly the client's bytes -- even
  with a Model Config lifetime stamped -- and with a row they send the mapped
  lifetime on the client's markers, adding none;
* a gateway's refusal is learned for the whole host and stops lifetimes on
  every model behind it, with no new call; Forget restores them.
"""

import copy
from typing import Any

import httpx
import pytest

from my_claude_code.config.model_overrides import (
    ModelParameterOverrides,
    save_model_overrides,
)
from my_claude_code.core.anthropic.models import MessagesRequest, PromptCacheAsk
from my_claude_code.core.failures import ExecutionFailure
from my_claude_code.core.reasoning import DEFAULT_REASONING_POLICY
from my_claude_code.core.wire_capture import install_wire_trace
from my_claude_code.providers.anthropic import AnthropicProvider
from my_claude_code.providers.anthropic_messages.request import (
    build_anthropic_messages_body,
)
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.commandcode import CommandCodeProvider
from my_claude_code.providers.openai_chat import create_openai_chat_provider
from my_claude_code.providers.recovery import (
    FACT_PROMPT_CACHE_LIFETIME_REFUSED,
    PROVIDER_WIDE_MODEL_ID,
    learned_fact_store,
)
from my_claude_code.providers.runtime import models_dev
from tests.providers.support import passthrough_rate_limiter
from tests.providers.test_anthropic_prompt_cache_ttl import (
    _bare_payload,
    _claude_code_payload,
    _FakeHost,
    _markers,
    _wire,
)

NOTE = "prompt_cache_ttl"
OPUS = "claude-opus-5-5"
HAIKU = "claude-haiku-5-5"
TIER = PromptCacheAsk(30, "PROMPT_CACHE_TTL_OPUS")


def _overrides(models: dict | None = None, providers: dict | None = None) -> None:
    save_model_overrides(
        ModelParameterOverrides.from_document(
            {"models": models or {}, "providers": providers or {}}
        )
    )


def _request(
    payload: dict[str, Any], ask: PromptCacheAsk | None = None, model: str = OPUS
) -> MessagesRequest:
    request = MessagesRequest.model_validate({**copy.deepcopy(payload), "model": model})
    if ask is not None:
        request.set_prompt_cache_ask(ask)
    return request


async def _drain(stream: Any) -> list[str]:
    return [frame async for frame in stream]


def _oauth(host: _FakeHost) -> AnthropicProvider:
    provider = AnthropicProvider(
        ProviderConfig(
            api_key="sk-ant-oat01-test", base_url="https://api.anthropic.invalid/v1"
        ),
        rate_limiter=passthrough_rate_limiter(),
        provider_id="anthropic_oauth",
    )
    provider._messages._client = httpx.AsyncClient(transport=httpx.MockTransport(host))
    return provider


def _commandcode(host: _FakeHost) -> CommandCodeProvider:
    provider = CommandCodeProvider(
        ProviderConfig(api_key="user_test", base_url="https://commandcode.invalid/v1"),
        rate_limiter=passthrough_rate_limiter(),
    )
    provider._anthropic._client = httpx.AsyncClient(transport=httpx.MockTransport(host))
    return provider


def _ttl_less_payload() -> dict[str, Any]:
    """Claude Code's markers with no ``ttl`` of their own (Claude Desktop's)."""

    def strip(node: Any) -> Any:
        if isinstance(node, dict):
            return {
                key: (
                    {k: v for k, v in value.items() if k != "ttl"}
                    if key == "cache_control" and isinstance(value, dict)
                    else strip(value)
                )
                for key, value in node.items()
            }
        if isinstance(node, list):
            return [strip(item) for item in node]
        return node

    return strip(_claude_code_payload())


async def _send(provider: Any, request: MessagesRequest) -> dict[str, Any] | None:
    trace = install_wire_trace()
    await _drain(provider.stream_response(request))
    params = trace.requests[max(trace.requests)].params
    return params.get(NOTE)


# -- the Claude subscription: the rows decide ------------------------------


@pytest.mark.asyncio
async def test_a_model_row_of_ten_minutes_sends_an_hour_from_the_model_row() -> None:
    """10 min is not a lifetime Anthropic keeps: the shortest at least 10 is 1 h."""

    _overrides(models={f"anthropic_oauth/{OPUS}": {"prompt_cache_ttl": 10}})
    host = _FakeHost()
    provider = _oauth(host)

    note = await _send(provider, _request(_claude_code_payload(), TIER))

    assert all(marker["ttl"] == "1h" for marker in _markers(host.bodies[0]))
    assert note is not None
    assert note["from"] == "model_override"
    assert note["requested"] == 10
    assert note["sent"] == "1h"
    await provider.cleanup()


@pytest.mark.asyncio
async def test_a_model_row_of_three_minutes_sends_five_minutes() -> None:
    _overrides(models={f"anthropic_oauth/{OPUS}": {"prompt_cache_ttl": 3}})
    host = _FakeHost()
    provider = _oauth(host)

    note = await _send(provider, _request(_claude_code_payload(), TIER))

    sent = host.bodies[0]
    # The client's own 1 h markers are rewritten to 5m; its ttl-less one stays.
    assert sent["system"][1]["cache_control"] == {
        "type": "ephemeral",
        "ttl": "5m",
        "scope": "global",
    }
    assert sent["tools"][0]["cache_control"]["ttl"] == "5m"
    assert sent["messages"][2]["content"][0]["content"][0]["cache_control"] == {
        "type": "ephemeral"
    }
    assert note is not None and note["from"] == "model_override"
    assert note["sent"] == "5m"
    await provider.cleanup()


@pytest.mark.asyncio
async def test_a_provider_row_decides_when_the_model_row_inherits() -> None:
    _overrides(providers={"anthropic_oauth": {"prompt_cache_ttl": 45}})
    host = _FakeHost()
    provider = _oauth(host)

    note = await _send(provider, _request(_bare_payload(), None))

    # No marker at all on the bare body: Anthropic's one top-level marker.
    assert host.bodies[0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert note is not None
    assert note["from"] == "provider_override"
    assert note["requested"] == 45
    await provider.cleanup()


@pytest.mark.asyncio
async def test_force_unset_on_a_model_row_sends_the_clients_bytes_over_a_tier() -> None:
    _overrides(models={f"anthropic_oauth/{OPUS}": {"prompt_cache_ttl": None}})
    unset_host, plain_host, sibling_host = _FakeHost(), _FakeHost(), _FakeHost()

    unset = _oauth(unset_host)
    note = await _send(unset, _request(_bare_payload(), PromptCacheAsk(30, "T")))
    plain = _oauth(plain_host)
    await _send(plain, _request(_bare_payload(), None))
    sibling = _oauth(sibling_host)
    sibling_note = await _send(
        sibling, _request(_bare_payload(), PromptCacheAsk(30, "T"), model=HAIKU)
    )

    assert note is None
    assert _wire(unset_host.bodies[0]) == _wire(plain_host.bodies[0])
    # The tier still applies to the sibling model with no row of its own.
    assert sibling_host.bodies[0]["cache_control"] == {
        "type": "ephemeral",
        "ttl": "1h",
    }
    assert sibling_note is not None and sibling_note["from"] == "T"
    for provider in (unset, plain, sibling):
        await provider.cleanup()


# -- the gateways (Q7): nothing unless a row says so -----------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [_claude_code_payload(), _bare_payload()])
async def test_command_code_with_no_row_sends_the_clients_bytes(
    payload: dict[str, Any],
) -> None:
    stamped, plain = _FakeHost(), _FakeHost()
    for host, ask in ((stamped, TIER), (plain, None)):
        provider = _commandcode(host)
        assert await _send(provider, _request(payload, ask)) is None
        await provider.cleanup()

    assert _wire(stamped.bodies[0]) == _wire(plain.bodies[0])


@pytest.mark.asyncio
async def test_command_code_with_a_row_sends_the_mapped_lifetime() -> None:
    _overrides(models={f"commandcode/{OPUS}": {"prompt_cache_ttl": 20}})
    host = _FakeHost()
    provider = _commandcode(host)

    note = await _send(provider, _request(_claude_code_payload(), None))

    expected = _request(_claude_code_payload(), None).model_dump(exclude_none=True)
    assert len(_markers(host.bodies[0])) == len(_markers(expected))
    assert all(marker["ttl"] == "1h" for marker in _markers(host.bodies[0]))
    assert note is not None
    assert note["from"] == "model_override"
    assert note["sent"] == "1h"
    assert note["host_lifetimes_known"] is False
    assert note["declared_for"] == "Anthropic Messages gateway"
    await provider.cleanup()


@pytest.mark.asyncio
async def test_a_gateway_with_a_row_adds_no_marker_to_a_body_that_has_none() -> None:
    _overrides(providers={"commandcode": {"prompt_cache_ttl": 20}})
    with_row, without = _FakeHost(), _FakeHost()

    provider = _commandcode(with_row)
    note = await _send(provider, _request(_bare_payload(), None))
    await provider.cleanup()
    _overrides()
    provider = _commandcode(without)
    await _send(provider, _request(_bare_payload(), None))
    await provider.cleanup()

    assert _wire(with_row.bodies[0]) == _wire(without.bodies[0])
    assert note is not None
    assert note["marker_added"] is False
    assert note["markers"] == 0


@pytest.mark.asyncio
async def test_command_codes_open_models_never_see_a_lifetime() -> None:
    """A provider row on Command Code reaches only its Messages delegate."""

    _overrides(providers={"commandcode": {"prompt_cache_ttl": 20}})
    provider = _commandcode(_FakeHost())

    assert provider._delegate("deepseek/deepseek-v4-flash") is provider._openai
    assert provider._delegate(OPUS) is provider._anthropic
    assert provider._openai is not provider._anthropic
    await provider.cleanup()


def _write_zen_registry(**models: object) -> None:
    path = models_dev.models_dev_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    models_dev.write_models_dev_cache(
        {"opencode": {"npm": "@ai-sdk/openai-compatible", "models": dict(models)}},
        path,
    )
    models_dev.reset_models_dev_payload_cache()


def _zen(host: _FakeHost) -> Any:
    _write_zen_registry(**{OPUS: {"provider": {"npm": "@ai-sdk/anthropic"}}})
    provider = create_openai_chat_provider(
        "opencode",
        ProviderConfig(api_key="sk-test", base_url="https://opencode.invalid/zen/v1"),
        passthrough_rate_limiter(),
    )
    inner: Any = provider._messages._provider
    inner._client = httpx.AsyncClient(transport=httpx.MockTransport(host))
    return provider


@pytest.mark.asyncio
async def test_zens_messages_door_follows_the_same_rule() -> None:
    plain_host, row_host = _FakeHost(), _FakeHost()

    plain = _zen(plain_host)
    assert await _send(plain, _request(_claude_code_payload(), TIER)) is None
    await plain.cleanup()
    _overrides(providers={"opencode": {"prompt_cache_ttl": 4}})
    with_row = _zen(row_host)
    note = await _send(with_row, _request(_claude_code_payload(), TIER))
    await with_row.cleanup()

    assert plain_host.bodies[0]["system"][1]["cache_control"]["ttl"] == "1h"
    assert all(
        marker.get("ttl") in (None, "5m") for marker in _markers(row_host.bodies[0])
    )
    assert row_host.bodies[0]["system"][1]["cache_control"]["ttl"] == "5m"
    assert note is not None and note["from"] == "provider_override"


# -- a gateway that refuses ------------------------------------------------


class _RefusingGateway(_FakeHost):
    """Refuses every ``ttl`` in Anthropic's own wording, naming the field."""

    def __init__(self) -> None:
        super().__init__(accepted=frozenset())


@pytest.mark.asyncio
async def test_a_gateway_refusal_is_learned_for_the_host_and_forget_restores() -> None:
    _overrides(providers={"commandcode": {"prompt_cache_ttl": 20}})
    host = _RefusingGateway()
    provider = _commandcode(host)

    with pytest.raises(ExecutionFailure):
        await _drain(provider.stream_response(_request(_ttl_less_payload())))

    assert len(host.bodies) == 1  # no new retry
    assert all(marker["ttl"] == "1h" for marker in _markers(host.bodies[0]))
    facts = [
        fact
        for fact in learned_fact_store().facts_for_model(
            "commandcode", PROVIDER_WIDE_MODEL_ID
        )
        if fact.fact_kind == FACT_PROMPT_CACHE_LIFETIME_REFUSED
    ]
    assert [fact.detail for fact in facts] == ["1h"]
    assert learned_fact_store().facts_for_model("commandcode", OPUS) == ()

    # The next request -- and one to another model of the same host -- carries
    # no lifetime of MCC's at all.
    host.accepted = frozenset({"5m", "1h"})
    client_bytes = _wire(
        build_anthropic_messages_body(
            _request(_ttl_less_payload()), reasoning=DEFAULT_REASONING_POLICY
        )
    )
    note = await _send(provider, _request(_ttl_less_payload()))
    other = await _send(provider, _request(_ttl_less_payload(), model=HAIKU))
    assert _wire(host.bodies[1]) == client_bytes
    assert all("ttl" not in marker for marker in _markers(host.bodies[2]))
    assert note is not None and note["sent"] is None
    assert note["refused_by_host"] == ["1h"]
    assert other is not None and other["sent"] is None

    learned_fact_store().forget(
        "commandcode", PROVIDER_WIDE_MODEL_ID, FACT_PROMPT_CACHE_LIFETIME_REFUSED, "1h"
    )
    restored = await _send(provider, _request(_ttl_less_payload()))
    assert restored is not None and restored["sent"] == "1h"
    assert all(marker["ttl"] == "1h" for marker in _markers(host.bodies[-1]))
    await provider.cleanup()
