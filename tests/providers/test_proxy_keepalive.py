"""Keep a proxied leg's idle connections open (7.94.0, X1 / P-2).

``PROXY_KEEPALIVE_SECONDS`` reaches the clients of a chain leg that has an
address, and nothing else. The proof is the arguments every HTTP client
constructor receives, recorded at ``httpx.AsyncClient.__init__`` (the OpenAI
SDK's client is a subclass, so it is recorded too, after the SDK's own
defaults are applied):

* an unproxied provider, a chain's Direct entry and its Direct fallback get
  identical arguments with the setting unset, 0 and 60;
* a proxied leg with the setting unset or 0 gets no ``limits`` argument from
  MCC at all -- the SDK's client keeps the SDK's own -- and with 60 gets
  exactly one difference: ``limits`` with the client's own pool sizes and a
  60-second keep-alive;
* the built pool really holds that keep-alive, through an HTTP and a SOCKS5
  proxy alike (the SOCKS handshake bound wraps the backend, not the pool).
"""

from collections.abc import Callable
from typing import Any

import httpx
import openai
import pytest

from my_claude_code.config.provider_catalog import PROVIDER_CATALOG
from my_claude_code.config.settings import Settings
from my_claude_code.providers.base import ProviderConfig, ProxyChainPlan, ProxyLeg
from my_claude_code.providers.media.registry import MediaRegistry
from my_claude_code.providers.openai_chat import OpenAIChatProvider
from my_claude_code.providers.proxy_keepalive import (
    HTTPX_DEFAULT_LIMITS,
    leg_pool_kwargs,
    proxied_leg_keepalive,
)
from my_claude_code.providers.runtime.factory import _create_single_provider

PROXY_URL = "http://203.0.113.7:8080"
SOCKS_URL = "socks5://203.0.113.8:1080"

#: One provider per client family a proxied leg can own: the OpenAI SDK chat
#: client with its Responses and Messages transports (nvidia_nim), the
#: Anthropic provider's own client and its Messages client, the ChatGPT OAuth
#: client, and the model-list clients of Cloudflare, GitHub Models and Vertex.
PROVIDERS = (
    "nvidia_nim",
    "anthropic",
    "chatgpt_oauth",
    "cloudflare",
    "github_models",
    "vertex",
)

#: Vertex's model-list client passes ``proxy=config.proxy`` where every other
#: client passes ``config.proxy or None``, and ``httpx`` 0.28 refuses an empty
#: proxy URL -- so an UNPROXIED Vertex provider fails to construct, on 7.93.0
#: exactly as here (pre-existing; reported for its own PR, not changed by
#: this one). It is covered where it has an address: the proxied-leg test.
BUILDS_WITHOUT_A_PROXY = tuple(p for p in PROVIDERS if p != "vertex")

Recorded = list[tuple[str, dict[str, Any]]]


@pytest.fixture
def recorded(monkeypatch) -> Recorded:
    calls: Recorded = []
    original = httpx.AsyncClient.__init__

    def recording(self, *args: Any, **kwargs: Any) -> None:
        assert not args, "clients are built from keyword arguments only"
        calls.append((type(self).__name__, dict(kwargs)))
        original(self, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", recording)
    return calls


def _settings(keepalive: object | None) -> Settings:
    values: dict[str, object] = {
        "nvidia_nim_api_key": "nvapi-test",
        "CLOUDFLARE_ACCOUNT_ID": "acct-test",
        "VERTEX_PROJECT_ID": "project-test",
    }
    if keepalive is not None:
        values["PROXY_KEEPALIVE_SECONDS"] = keepalive
    return Settings.model_validate(values)


def _config(*, chained: bool, proxy: str = PROXY_URL) -> ProviderConfig:
    plan = (
        ProxyChainPlan(
            legs=(
                ProxyLeg(url=proxy, label="203.0.113.7:8080"),
                ProxyLeg(url="", label=""),
            ),
            policy="failover",
            on=frozenset(),
            scope="provider",
            max_switches=2,
        )
        if chained
        else None
    )
    return ProviderConfig(
        api_key="test-key",
        base_url="http://upstream.test/v1",
        proxy_chain=plan,
        http_connect_timeout=45.0,
        http_read_timeout=123.0,
        http_write_timeout=77.0,
    )


def _open_every_client(provider: Any) -> None:
    """Build the lazily-built transports too, so every client is recorded."""

    inner = getattr(provider, "leaf", provider)
    if isinstance(inner, OpenAIChatProvider):
        _ = inner._responses
        _ = inner._messages


def _normal(value: object) -> object:
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    if isinstance(value, httpx.Timeout | httpx.Limits):
        return repr(value)
    if isinstance(value, dict):
        return {str(key): _normal(item) for key, item in sorted(value.items())}
    return type(value).__name__


def _record(
    recorded: Recorded, build: Callable[[], Any]
) -> list[tuple[str, dict[str, object]]]:
    start = len(recorded)
    _open_every_client(build())
    return [
        (name, {key: _normal(value) for key, value in kwargs.items()})
        for name, kwargs in recorded[start:]
    ]


def _chat(provider_id: str, settings: Settings, *, chained: bool, leg: int = 0):
    descriptor = PROVIDER_CATALOG[provider_id]

    def build() -> Any:
        node: Any = _create_single_provider(
            descriptor, _config(chained=chained), settings
        )
        return node._pool.get(leg) if chained else node

    return build


# ----------------------------------------------------------------- helper


def test_the_helper_says_nothing_unless_a_keepalive_is_set() -> None:
    assert leg_pool_kwargs(None) == {}
    assert leg_pool_kwargs(0) == {}
    assert leg_pool_kwargs(0.0) == {}
    assert leg_pool_kwargs(-1.0) == {}
    limits = leg_pool_kwargs(60.0)["limits"]
    assert limits == httpx.Limits(
        max_connections=HTTPX_DEFAULT_LIMITS.max_connections,
        max_keepalive_connections=HTTPX_DEFAULT_LIMITS.max_keepalive_connections,
        keepalive_expiry=60.0,
    )
    sdk = leg_pool_kwargs(60, base=openai.DEFAULT_CONNECTION_LIMITS)["limits"]
    assert sdk.max_connections == openai.DEFAULT_CONNECTION_LIMITS.max_connections
    assert (
        sdk.max_keepalive_connections
        == openai.DEFAULT_CONNECTION_LIMITS.max_keepalive_connections
    )
    assert sdk.keepalive_expiry == 60.0


def test_the_default_is_read_from_the_library_not_restated() -> None:
    transport: Any = httpx.AsyncClient()._transport
    pool = transport._pool
    assert pool._max_connections == HTTPX_DEFAULT_LIMITS.max_connections
    assert pool._max_keepalive_connections == (
        HTTPX_DEFAULT_LIMITS.max_keepalive_connections
    )
    assert pool._keepalive_expiry == HTTPX_DEFAULT_LIMITS.keepalive_expiry == 5.0


def test_the_setting_is_off_at_zero_and_for_anything_not_a_number() -> None:
    assert Settings.model_validate({}).proxy_keepalive_seconds == 0.0
    assert proxied_leg_keepalive(_settings(None)) is None
    assert proxied_leg_keepalive(_settings(0)) is None
    assert proxied_leg_keepalive(_settings(60)) == 60.0
    assert proxied_leg_keepalive(object()) is None


# --------------------------------------------------- byte-equal elsewhere


@pytest.mark.parametrize("provider_id", BUILDS_WITHOUT_A_PROXY)
def test_an_unproxied_provider_is_built_identically(recorded, provider_id) -> None:
    unset = _record(recorded, _chat(provider_id, _settings(None), chained=False))
    assert unset, "no client was built"
    for value in (0, 60):
        assert (
            _record(recorded, _chat(provider_id, _settings(value), chained=False))
            == unset
        )
    assert not any(
        name == "AsyncClient" and "limits" in kwargs for name, kwargs in unset
    )


@pytest.mark.parametrize("provider_id", BUILDS_WITHOUT_A_PROXY)
@pytest.mark.parametrize("leg", [1, 2], ids=["direct-entry", "direct-fallback"])
def test_a_direct_leg_is_built_identically(recorded, provider_id, leg) -> None:
    unset = _record(
        recorded, _chat(provider_id, _settings(None), chained=True, leg=leg)
    )
    assert unset, "no client was built"
    for value in (0, 60):
        assert (
            _record(
                recorded,
                _chat(provider_id, _settings(value), chained=True, leg=leg),
            )
            == unset
        )


@pytest.mark.parametrize("provider_id", PROVIDERS)
def test_a_proxied_leg_differs_by_the_keepalive_and_nothing_else(
    recorded, provider_id
) -> None:
    unset = _record(recorded, _chat(provider_id, _settings(None), chained=True))
    zero = _record(recorded, _chat(provider_id, _settings(0), chained=True))
    sixty = _record(recorded, _chat(provider_id, _settings(60), chained=True))
    assert unset and zero == unset
    assert [name for name, _ in sixty] == [name for name, _ in unset]
    for (name, before), (_, after) in zip(unset, sixty, strict=True):
        assert after["proxy"] == PROXY_URL
        if name == "AsyncClient":
            # A bare httpx client: MCC said nothing about limits before.
            assert "limits" not in before
            base = HTTPX_DEFAULT_LIMITS
        else:
            # The SDK's client: the SDK's own limits before, its sizes after.
            assert before["limits"] == repr(openai.DEFAULT_CONNECTION_LIMITS)
            base = openai.DEFAULT_CONNECTION_LIMITS
        assert after["limits"] == repr(
            httpx.Limits(
                max_connections=base.max_connections,
                max_keepalive_connections=base.max_keepalive_connections,
                keepalive_expiry=60.0,
            )
        )
        assert {k: v for k, v in after.items() if k != "limits"} == {
            k: v for k, v in before.items() if k != "limits"
        }


# ------------------------------------------------------------------ media


def _media(settings: Settings, *, leg: int, proxy: str = PROXY_URL):
    descriptor = PROVIDER_CATALOG["open_router"]

    def build() -> Any:
        node: Any = MediaRegistry()._single(
            descriptor, _config(chained=True, proxy=proxy), settings
        )
        return node._pool.get(leg)

    return build


def test_media_legs_follow_the_same_rule(recorded) -> None:
    for leg in (1, 2):
        unset = _record(recorded, _media(_settings(None), leg=leg))
        assert _record(recorded, _media(_settings(60), leg=leg)) == unset
    unset = _record(recorded, _media(_settings(None), leg=0))
    sixty = _record(recorded, _media(_settings(60), leg=0))
    assert [kwargs.get("limits") for _, kwargs in unset] == [None]
    assert [kwargs["limits"] for _, kwargs in sixty] == [
        repr(leg_pool_kwargs(60.0)["limits"])
    ]


# --------------------------------------------------- the pool really has it


def _pools(client: Any) -> list[Any]:
    return [transport._pool for transport in client._mounts.values() if transport]


@pytest.mark.parametrize("proxy", [PROXY_URL, SOCKS_URL], ids=["http", "socks5"])
def test_the_built_pool_keeps_idle_connections_for_the_setting(proxy) -> None:
    descriptor = PROVIDER_CATALOG["nvidia_nim"]
    for value, expected in ((None, 5.0), (60, 60.0)):
        node: Any = _create_single_provider(
            descriptor, _config(chained=True, proxy=proxy), _settings(value)
        )
        leg = node._pool.get(0)
        assert isinstance(leg, OpenAIChatProvider)
        clients = [
            leg._client._client,
            leg._responses._client,
            leg._messages._provider._client,
        ]
        for client in clients:
            pools = _pools(client)
            assert pools, "a proxied client mounts its proxy transport"
            assert {pool._keepalive_expiry for pool in pools} == {expected}
