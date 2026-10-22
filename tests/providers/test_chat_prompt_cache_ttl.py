"""The prompt-cache lifetime on OpenAI-shaped hosts (7.106.0).

Pinned here on the bytes the OpenAI SDK actually posts (a real ``AsyncOpenAI``
over an ``httpx.MockTransport``):

* nothing set -> no lifetime step changes anything, no field appears, nothing
  is recorded; the step given nothing returns the very body it was given;
* a lifetime set on a Claude model on OpenRouter -> one top-level
  ``cache_control`` with ``5m`` (1-5 min) or ``1h`` (6-60), alias ids too; the
  client's own top-level marker (in its ``extra_body``) is re-timed instead;
* a lifetime set on Azure OpenAI -> ``prompt_cache_retention`` ``in_memory``
  (1-5 min) or ``24h`` (6-60);
* every other host -- OpenRouter's non-Claude models, the hosts that cache
  automatically, the ones nothing is known about -- gets nothing;
* a 400 naming the field records a learned refusal, the refused request
  makes no extra call, and the next request sends the host's next accepted
  lifetime (or nothing once every one is refused); any other 400 records
  nothing.
"""

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI

from my_claude_code.application.prompt_cache import choose_prompt_cache_lifetime
from my_claude_code.config.model_overrides import (
    ModelParameterOverrides,
    save_model_overrides,
)
from my_claude_code.config.prompt_cache_lifetimes import (
    OPENROUTER_ANTHROPIC_LIFETIMES,
    PROMPT_CACHE_LIFETIME_FAMILIES,
)
from my_claude_code.core.anthropic.models import MessagesRequest, PromptCacheAsk
from my_claude_code.core.failures import ExecutionFailure
from my_claude_code.core.wire_capture import install_wire_trace
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.open_router import OpenRouterProvider
from my_claude_code.providers.openai_chat import (
    OPENAI_CHAT_PROFILES,
    create_openai_chat_provider,
)
from my_claude_code.providers.openai_chat import provider as provider_module
from my_claude_code.providers.openai_chat.prompt_cache import (
    PROMPT_CACHE_TTL_NOTE,
    apply_chat_prompt_cache_lifetime,
    chat_prompt_cache_choice,
)
from my_claude_code.providers.recovery import (
    FACT_PROMPT_CACHE_LIFETIME_REFUSED,
    learned_fact_store,
)
from tests.providers.request_factory import make_messages_request
from tests.providers.support import passthrough_rate_limiter

OR_CLAUDE = "anthropic/claude-haiku-5.5"
OR_ALIAS = "~anthropic/claude-sonnet-latest"
OR_OPEN = "moonshotai/kimi-k2.6"
AZURE_DEPLOYMENT = "my-gpt-5-4"
ASK_30 = PromptCacheAsk(30, "PROMPT_CACHE_TTL_OPUS")
ASK_3 = PromptCacheAsk(3, "PROMPT_CACHE_TTL")
OPENROUTER = PROMPT_CACHE_LIFETIME_FAMILIES[OPENROUTER_ANTHROPIC_LIFETIMES]

_SSE = (
    'data: {"id":"c1","object":"chat.completion.chunk","created":1,"model":"m",'
    '"choices":[{"index":0,"delta":{"role":"assistant","content":"hi"},'
    '"finish_reason":null}]}\n\n'
    'data: {"id":"c1","object":"chat.completion.chunk","created":1,"model":"m",'
    '"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
    "data: [DONE]\n\n"
)


class _ChatHost:
    """Records every posted body; answers with ``refusal`` while one is set."""

    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []
        self.refusal: Callable[[dict[str, Any]], httpx.Response | None] | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        if self.refusal is not None:
            refused = self.refusal(body)
            if refused is not None:
                return refused
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_SSE.encode(),
        )


def _attach(provider: Any, host: _ChatHost) -> Any:
    provider._client = AsyncOpenAI(
        api_key="sk-or-v1-test",
        base_url="https://chat.invalid/v1",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(host)),
    )
    return provider


def _open_router(host: _ChatHost) -> OpenRouterProvider:
    provider = OpenRouterProvider(
        ProviderConfig(api_key="sk-or-v1-test", base_url="https://chat.invalid/v1"),
        rate_limiter=passthrough_rate_limiter(),
    )
    return _attach(provider, host)


def _profiled(provider_id: str, host: _ChatHost) -> Any:
    provider = create_openai_chat_provider(
        provider_id,
        ProviderConfig(api_key="sk-test", base_url="https://chat.invalid/v1"),
        passthrough_rate_limiter(),
        profile=OPENAI_CHAT_PROFILES[provider_id],
    )
    return _attach(provider, host)


def _request(
    model: str, ask: PromptCacheAsk | None = None, **overrides: Any
) -> MessagesRequest:
    request = make_messages_request(model, **overrides)
    if ask is not None:
        request.set_prompt_cache_ask(ask)
    return request


async def _send(provider: Any, request: MessagesRequest) -> dict[str, Any]:
    trace = install_wire_trace()
    async for _event in provider.stream_response(request):
        pass
    return trace.requests[max(trace.requests)].params


def _overrides(models: dict | None = None, providers: dict | None = None) -> None:
    save_model_overrides(
        ModelParameterOverrides.from_document(
            {"models": models or {}, "providers": providers or {}}
        )
    )


def _refused(provider_id: str, model_id: str) -> list[str]:
    return sorted(
        fact.detail
        for fact in learned_fact_store().facts_for_model(provider_id, model_id)
        if fact.fact_kind == FACT_PROMPT_CACHE_LIFETIME_REFUSED
    )


# -- nothing set: nothing moves --------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("factory", "model"),
    [
        (_open_router, OR_CLAUDE),
        (_open_router, OR_OPEN),
        (lambda host: _profiled("azure_openai", host), AZURE_DEPLOYMENT),
    ],
)
async def test_nothing_set_posts_no_lifetime_and_records_nothing(
    factory: Callable[[_ChatHost], Any], model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Any] = []
    real = provider_module.apply_chat_prompt_cache_lifetime

    def spy(body: dict[str, Any], choice: Any, notes: Any = None) -> dict[str, Any]:
        seen.append(choice)
        returned = real(body, choice, notes)
        assert returned is body
        return returned

    monkeypatch.setattr(provider_module, "apply_chat_prompt_cache_lifetime", spy)
    host = _ChatHost()
    provider = factory(host)

    params = await _send(provider, _request(model))

    assert seen == [None]
    assert "cache_control" not in host.bodies[0]
    assert "prompt_cache_retention" not in host.bodies[0]
    assert PROMPT_CACHE_TTL_NOTE not in params
    await provider.cleanup()


def test_the_step_given_nothing_returns_the_same_body() -> None:
    body: dict[str, Any] = {"model": "m", "extra_body": {"reasoning": {}}}
    notes: dict[str, Any] = {}

    assert apply_chat_prompt_cache_lifetime(body, None, notes) is body
    assert body == {"model": "m", "extra_body": {"reasoning": {}}}
    assert notes == {}


# -- OpenRouter: Claude upstreams get the one top-level marker -------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ask", "model", "wire"),
    [(ASK_30, OR_CLAUDE, "1h"), (ASK_3, OR_CLAUDE, "5m"), (ASK_30, OR_ALIAS, "1h")],
)
async def test_a_claude_model_on_openrouter_gets_the_top_level_marker(
    ask: PromptCacheAsk, model: str, wire: str
) -> None:
    host = _ChatHost()
    provider = _open_router(host)

    await _send(provider, _request(model))
    params = await _send(provider, _request(model, ask))

    plain, posted = host.bodies
    assert posted.pop("cache_control") == {"type": "ephemeral", "ttl": wire}
    # Nothing else of the body moved.
    assert json.dumps(posted) == json.dumps(plain)
    note = params[PROMPT_CACHE_TTL_NOTE]
    assert note["requested"] == ask.minutes
    assert note["from"] == ask.source
    assert note["sent"] == wire
    assert note["host_lifetimes"] == ["5m", "1h"]
    assert note["declared_for"] == "OpenRouter, Claude upstream"
    assert note["marker_added"] is True
    await provider.cleanup()


@pytest.mark.asyncio
async def test_a_non_claude_model_on_openrouter_gets_nothing() -> None:
    host = _ChatHost()
    provider = _open_router(host)

    params = await _send(provider, _request(OR_OPEN, ASK_30))

    assert "cache_control" not in host.bodies[0]
    assert PROMPT_CACHE_TTL_NOTE not in params
    await provider.cleanup()


@pytest.mark.asyncio
async def test_the_clients_own_top_level_marker_is_retimed_not_doubled() -> None:
    host = _ChatHost()
    provider = _open_router(host)
    request = _request(
        OR_CLAUDE,
        ASK_30,
        extra_body={"cache_control": {"type": "ephemeral", "scope": "kept"}},
    )

    params = await _send(provider, request)

    assert host.bodies[0]["cache_control"] == {
        "type": "ephemeral",
        "scope": "kept",
        "ttl": "1h",
    }
    assert params[PROMPT_CACHE_TTL_NOTE]["marker_added"] is False
    assert params[PROMPT_CACHE_TTL_NOTE]["retimed"] == 1
    # The client's request object is never touched.
    assert request.extra_body == {
        "cache_control": {"type": "ephemeral", "scope": "kept"}
    }
    await provider.cleanup()


def test_the_marker_lands_in_a_new_extra_body_never_the_built_one() -> None:
    shared = {"reasoning": {"enabled": True}}
    body: dict[str, Any] = {"model": OR_CLAUDE, "extra_body": shared}
    choice = choose_prompt_cache_lifetime(ASK_30, OPENROUTER)

    returned = apply_chat_prompt_cache_lifetime(body, choice)

    assert returned is body
    assert body["extra_body"] is not shared
    assert shared == {"reasoning": {"enabled": True}}
    assert body["extra_body"] == {
        "reasoning": {"enabled": True},
        "cache_control": {"type": "ephemeral", "ttl": "1h"},
    }


@pytest.mark.asyncio
async def test_a_model_row_decides_on_openrouter_over_the_tier() -> None:
    _overrides(models={f"open_router/{OR_CLAUDE}": {"prompt_cache_ttl": 4}})
    host = _ChatHost()
    provider = _open_router(host)

    params = await _send(provider, _request(OR_CLAUDE, ASK_30))

    assert host.bodies[0]["cache_control"] == {"type": "ephemeral", "ttl": "5m"}
    assert params[PROMPT_CACHE_TTL_NOTE]["from"] == "model_override"
    await provider.cleanup()


@pytest.mark.asyncio
async def test_force_unset_on_the_provider_row_sends_nothing_over_a_tier() -> None:
    _overrides(providers={"open_router": {"prompt_cache_ttl": None}})
    host = _ChatHost()
    provider = _open_router(host)

    params = await _send(provider, _request(OR_CLAUDE, ASK_30))

    assert "cache_control" not in host.bodies[0]
    assert PROMPT_CACHE_TTL_NOTE not in params
    await provider.cleanup()


# -- Azure OpenAI: the retention word --------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(("ask", "wire"), [(ASK_30, "24h"), (ASK_3, "in_memory")])
async def test_azure_openai_gets_its_retention_word(
    ask: PromptCacheAsk, wire: str
) -> None:
    host = _ChatHost()
    provider = _profiled("azure_openai", host)

    params = await _send(provider, _request(AZURE_DEPLOYMENT, ask))

    assert host.bodies[0]["prompt_cache_retention"] == wire
    assert "cache_control" not in host.bodies[0]
    note = params[PROMPT_CACHE_TTL_NOTE]
    assert note["sent"] == wire
    assert note["host_lifetimes"] == ["in_memory", "24h"]
    assert note["field"] == "prompt_cache_retention"
    await provider.cleanup()


@pytest.mark.asyncio
async def test_a_provider_row_decides_on_azure() -> None:
    _overrides(providers={"azure_openai": {"prompt_cache_ttl": 45}})
    host = _ChatHost()
    provider = _profiled("azure_openai", host)

    params = await _send(provider, _request(AZURE_DEPLOYMENT))

    assert host.bodies[0]["prompt_cache_retention"] == "24h"
    assert params[PROMPT_CACHE_TTL_NOTE]["from"] == "provider_override"
    await provider.cleanup()


# -- every other host: nothing ---------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_id",
    # Automatic hosts (Agnes, OpenCode Zen's Chat door, B.AI, HyperCharm), and
    # hosts nothing is known about (Groq, Kilo's gateway, a custom provider).
    ["agnes", "opencode", "bai", "hypercharm", "groq", "kilo"],
)
async def test_every_other_host_gets_nothing(provider_id: str) -> None:
    _overrides(providers={provider_id: {"prompt_cache_ttl": 30}})
    request = _request("anthropic/claude-haiku-5.5", ASK_30)

    assert chat_prompt_cache_choice(request, provider_id=provider_id) is None


def test_the_custom_provider_profile_gets_nothing() -> None:
    request = _request("anthropic/claude-haiku-5.5", ASK_30)

    assert chat_prompt_cache_choice(request, provider_id="custom_agnes") is None
    assert chat_prompt_cache_choice(request, provider_id="") is None


# -- refusals ---------------------------------------------------------------


def _openrouter_refusal(
    refused: str,
) -> Callable[[dict[str, Any]], httpx.Response | None]:
    """OpenRouter's envelope around Anthropic's own sentence, as it relays it."""

    def answer(body: dict[str, Any]) -> httpx.Response | None:
        marker = body.get("cache_control") or {}
        if marker.get("ttl") != refused:
            return None
        raw = json.dumps(
            {
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "message": f"cache_control.ttl: Input should be not {refused}",
                },
            }
        )
        return httpx.Response(
            400,
            json={
                "error": {
                    "code": 400,
                    "message": "Provider returned error",
                    "metadata": {"raw": raw, "provider_name": "Anthropic"},
                }
            },
        )

    return answer


@pytest.mark.asyncio
async def test_an_openrouter_refusal_is_learned_and_the_next_request_takes_5m() -> None:
    host = _ChatHost()
    host.refusal = _openrouter_refusal("1h")
    provider = _open_router(host)

    with pytest.raises(ExecutionFailure):
        await _send(provider, _request(OR_CLAUDE, ASK_30))

    assert len(host.bodies) == 1  # no new retry
    assert _refused("open_router", OR_CLAUDE) == ["1h"]

    params = await _send(provider, _request(OR_CLAUDE, ASK_30))
    assert host.bodies[-1]["cache_control"] == {"type": "ephemeral", "ttl": "5m"}
    assert params[PROMPT_CACHE_TTL_NOTE]["refused_by_host"] == ["1h"]

    # Refusing 5m too leaves nothing to send: the client's bytes go.
    host.refusal = _openrouter_refusal("5m")
    with pytest.raises(ExecutionFailure):
        await _send(provider, _request(OR_CLAUDE, ASK_30))
    assert _refused("open_router", OR_CLAUDE) == ["1h", "5m"]
    host.refusal = None
    params = await _send(provider, _request(OR_CLAUDE, ASK_30))
    assert "cache_control" not in host.bodies[-1]
    assert params[PROMPT_CACHE_TTL_NOTE]["sent"] is None

    # Another Claude model on the same host is not affected.
    await _send(provider, _request("anthropic/claude-opus-5.5", ASK_30))
    assert host.bodies[-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    await provider.cleanup()


def _azure_refusal(refused: str) -> Callable[[dict[str, Any]], httpx.Response | None]:
    def answer(body: dict[str, Any]) -> httpx.Response | None:
        if body.get("prompt_cache_retention") != refused:
            return None
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": f"Invalid value: '{refused}'. Supported values are: '24h'.",
                    "type": "invalid_request_error",
                    "param": "prompt_cache_retention",
                    "code": "invalid_value",
                }
            },
        )

    return answer


@pytest.mark.asyncio
async def test_an_azure_refusal_is_learned_and_the_next_request_takes_24h() -> None:
    host = _ChatHost()
    host.refusal = _azure_refusal("in_memory")
    provider = _profiled("azure_openai", host)

    with pytest.raises(ExecutionFailure):
        await _send(provider, _request(AZURE_DEPLOYMENT, ASK_3))

    assert len(host.bodies) == 1
    assert _refused("azure_openai", AZURE_DEPLOYMENT) == ["in_memory"]
    await _send(provider, _request(AZURE_DEPLOYMENT, ASK_3))
    assert host.bodies[-1]["prompt_cache_retention"] == "24h"
    await provider.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "payload"),
    [
        (400, {"error": {"message": "max_tokens is too large", "param": "max_tokens"}}),
        (429, {"error": {"message": "slow down; cache_control is fine"}}),
    ],
)
async def test_any_other_refusal_records_nothing(status: int, payload: dict) -> None:
    host = _ChatHost()
    host.refusal = lambda _body: httpx.Response(status, json=payload)
    provider = _open_router(host)

    with pytest.raises(ExecutionFailure):
        await _send(provider, _request(OR_CLAUDE, ASK_30))

    assert _refused("open_router", OR_CLAUDE) == []
    await provider.cleanup()
