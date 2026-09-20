"""A proxied provider must be configured like an un-proxied one.

The Chat Completions surface gives the OpenAI SDK an ``http_client`` only when
a proxy chain is in play -- there is no other way to name a proxy. Up to
7.35.0 that client was a bare ``httpx.AsyncClient``, and a bare
``httpx.AsyncClient`` is not the client the SDK builds for itself: the SDK sets
a pool sized for a gateway (1000 connections, 100 kept alive) and follows
redirects, and ``httpx``'s own defaults are 100, 20 and *not* following. So the
same provider, on the same host, ran on a different connection pool and a
different redirect policy depending on whether a chain happened to be
configured for it -- and the proxied case, the one under load from a rotation,
got the smaller pool.

The assertions below are comparisons, never literals: nothing here restates
what the SDK's defaults are, it only requires that the two clients agree. A
future SDK that changes its pool changes both sides at once.

Nothing in this module says anything about TLS, which is the point: neither
construction path does either.
"""

from typing import Any

import httpx
import pytest

from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.openai_chat.profiles import OPENAI_CHAT_PROFILES
from my_claude_code.providers.openai_chat.provider import OpenAIChatProvider
from my_claude_code.providers.rate_limit import ProviderRateLimiter

PROVIDER = "xai"
BASE_URL = "https://example.invalid/v1"
PROXY = "http://127.0.0.1:9/"


def _provider(proxy: str) -> OpenAIChatProvider:
    return OpenAIChatProvider(
        ProviderConfig(api_key="k", base_url=BASE_URL, proxy=proxy),
        profile=OPENAI_CHAT_PROFILES[PROVIDER],
        rate_limiter=ProviderRateLimiter(),
        provider_id=PROVIDER,
    )


def _httpx_client(provider: OpenAIChatProvider) -> httpx.AsyncClient:
    client: Any = provider._client
    return client._client


def _pool(client: httpx.AsyncClient) -> Any:
    """The connection pool a request to this provider actually runs on.

    Reached through the transport rather than read back off the client because
    ``httpx`` keeps no public record of the limits it was handed: the pool is
    where the number ends up, and the pool is what a request meets.

    It has to be resolved *for the URL*. A proxied client keeps two transports
    -- the plain one and a mounted proxy one -- and it is the mounted one every
    request to the provider is routed to, so comparing the plain transports
    would have compared two objects neither provider ever uses.
    """
    transport: Any = client._transport_for_url(httpx.URL(BASE_URL))
    return transport._pool


@pytest.fixture
def clients() -> tuple[httpx.AsyncClient, httpx.AsyncClient]:
    return _httpx_client(_provider("")), _httpx_client(_provider(PROXY))


def test_the_proxied_client_keeps_the_pool_the_direct_one_has(
    clients: tuple[httpx.AsyncClient, httpx.AsyncClient],
) -> None:
    """The size of the pool, and how much of it is kept warm, are the same."""

    direct, proxied = (_pool(client) for client in clients)

    assert proxied._max_connections == direct._max_connections
    assert proxied._max_keepalive_connections == direct._max_keepalive_connections
    assert proxied._keepalive_expiry == direct._keepalive_expiry


def test_the_proxied_client_keeps_the_redirect_policy(
    clients: tuple[httpx.AsyncClient, httpx.AsyncClient],
) -> None:
    """A redirect is followed, or not, for the same reason on both paths."""

    direct, proxied = clients

    assert proxied.follow_redirects == direct.follow_redirects


def test_the_proxied_client_keeps_the_timeouts(
    clients: tuple[httpx.AsyncClient, httpx.AsyncClient],
) -> None:
    """Connect, read and write, all four numbers, on both paths."""

    direct, proxied = clients

    assert proxied.timeout == direct.timeout


def test_the_proxied_client_is_the_one_that_carries_the_proxy(
    clients: tuple[httpx.AsyncClient, httpx.AsyncClient],
) -> None:
    """The sanity check under the three above: they really are two paths.

    Without this, a regression that dropped the proxy entirely would make every
    comparison pass.
    """

    direct, proxied = (_pool(client) for client in clients)

    assert type(direct).__name__ == "AsyncConnectionPool"
    assert type(proxied).__name__ == "AsyncHTTPProxy"
    assert proxied._proxy_url.host == b"127.0.0.1"
    assert proxied._proxy_url.port == 9


def test_the_un_proxied_provider_still_lets_the_sdk_build_its_own_client() -> None:
    """Nothing was added to the path that was never broken.

    The direct provider hands the SDK no client at all, exactly as before, so
    the object it runs on is the SDK's own wrapper -- not something this
    codebase assembled that happens to match.
    """

    client = _httpx_client(_provider(""))

    assert type(client).__name__ == "AsyncHttpxClientWrapper"
