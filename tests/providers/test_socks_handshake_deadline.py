"""A SOCKS5 rung that accepts and then says nothing must not park the request.

The defect, in the library rather than in this repo: ``httpcore`` 1.0.9's
``socks_proxy.py:244`` calls ``_init_socks5_connection`` with no timeout, and
the three ``stream.read(max_bytes=4096)`` calls inside it therefore run under
``anyio.fail_after(None)``. An address that completes the TCP accept and never
answers the greeting holds the request for ever, and none of
``HTTP_CONNECT_TIMEOUT``, ``HTTP_READ_TIMEOUT``, the pool timeout or
``PROXY_CONNECT_TIMEOUT_SECONDS`` reaches that read. With this operator's
fallback deadlines at ``0`` nothing above it ends the wait either.

The shape of every test here is the one 6.16.0 established for "this never
finishes": start the call as a task, wait on it with a bound the suite itself
owns, and assert on whether it is *still pending*. No test in this file can
hang the run, whichever way the code under test behaves.

The listeners are in ``tests/support/fake_socks5.py`` and
``tests/support/fake_http_proxy.py``; both bind ``127.0.0.1`` on port 0 and
nothing here reaches the network.
"""

import asyncio
import contextlib
import time

import httpx
import pytest

from my_claude_code.providers.anthropic_messages import AnthropicMessagesProvider
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.openai_chat.provider import _proxied_http_client
from my_claude_code.providers.openai_chat.responses_transport import ResponsesTransport
from my_claude_code.providers.rate_limit import ProviderRateLimiter
from my_claude_code.providers.runtime.proxy_rotating import proxy_reachability_failure
from my_claude_code.providers.socks_deadline import (
    bound_socks_handshake,
)
from tests.support.fake_http_proxy import FakeHttpProxy
from tests.support.fake_socks5 import (
    BAD_AUTH,
    CLOSE_MID,
    DRIBBLE,
    GREET_THEN_STALL,
    HONEST,
    SILENT,
    FakeSocks5Server,
    FakeUpstream,
)

pytestmark = pytest.mark.asyncio

#: The proxied leg's connect budget in these tests. Short so the suite is
#: quick; the mechanism does not care what the number is.
BUDGET = 1.0
#: How long a test waits before it calls something "still pending". Ten times
#: the budget and twenty times the read timeout below, so a pass is not a
#: coincidence of timing.
OUTER = 6.0
#: Deliberately *smaller* than the budget, so "it failed at the budget" cannot
#: be confused with "the read timeout caught it".
READ = 0.5

#: The three behaviours that hung before this change.
STALLING = (SILENT, GREET_THEN_STALL, DRIBBLE)


def _timeout() -> httpx.Timeout:
    return httpx.Timeout(READ, connect=BUDGET, read=READ, write=READ, pool=READ)


async def _rig(behaviour: str) -> tuple[FakeUpstream, FakeSocks5Server]:
    upstream = FakeUpstream()
    await upstream.start()
    socks = FakeSocks5Server(
        behaviour, upstream_port=upstream.port, dribble_interval=OUTER * 3
    )
    await socks.start()
    return upstream, socks


def _chat_client(proxy: str, base_url: str) -> httpx.AsyncClient:
    """The client 7.35.1 builds for a proxied Chat Completions provider."""

    return _proxied_http_client(proxy, base_url, _timeout())


def _responses_client(proxy: str, base_url: str) -> httpx.AsyncClient:
    """The client the Responses transport builds for itself."""

    transport = ResponsesTransport(
        _proxied_config(proxy),
        base_url=base_url,
        provider_name="RIG",
        identity=None,
        api_key="k",
        rate_limiter=ProviderRateLimiter(rate_limit=0, rate_window=60),
    )
    return transport._client


def _messages_client(proxy: str, base_url: str) -> httpx.AsyncClient:
    """The client the Anthropic Messages provider builds for itself."""

    provider = AnthropicMessagesProvider(
        _proxied_config(proxy, base_url=base_url),
        provider_name="RIG",
        rate_limiter=ProviderRateLimiter(rate_limit=0, rate_window=60, max_retries=0),
    )
    return provider._client


def _proxied_config(proxy: str, base_url: str = "http://rig.invalid/v1"):
    return ProviderConfig(
        api_key="k",
        base_url=base_url,
        proxy=proxy,
        http_connect_timeout=BUDGET,
        http_read_timeout=READ,
        http_write_timeout=READ,
    )


#: The three client construction sites a chat request can travel through.
BUILDERS = {
    "chat": _chat_client,
    "responses": _responses_client,
    "messages": _messages_client,
}


async def _attempt(
    client: httpx.AsyncClient, url: str
) -> tuple[bool, float, BaseException | None, httpx.Response | None]:
    """Run one request under the suite's own bound. Never hangs."""

    began = time.monotonic()
    task = asyncio.create_task(client.get(url))
    done, _ = await asyncio.wait({task}, timeout=OUTER)
    elapsed = time.monotonic() - began
    if not done:
        task.cancel()
        # The cancellation is the point; whatever it raises says nothing.
        with contextlib.suppress(BaseException):
            await task
        return False, elapsed, None, None
    try:
        return True, elapsed, None, task.result()
    except BaseException as error:
        # Every class is a result here, which is exactly what is being
        # measured -- the test asserts on which one, not on whether.
        return True, elapsed, error, None


async def _close(client: httpx.AsyncClient) -> None:
    # Tidying never decides a test.
    with contextlib.suppress(BaseException):
        await asyncio.wait_for(client.aclose(), 3.0)


# ---------------------------------------------------------------------------
# The defect itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("behaviour", STALLING)
async def test_the_library_parks_for_ever_on_a_stalling_socks5_rung(behaviour):
    """Before-picture, pinned: without the bound the request is still pending.

    This is the assertion that would start failing if a future ``httpcore``
    fixed the hole itself -- at which point the wrapper becomes belt and
    braces rather than the only bound, and this file should say so.
    """

    upstream, socks = await _rig(behaviour)
    client = httpx.AsyncClient(proxy=socks.url, timeout=_timeout())
    try:
        finished, elapsed, _, _ = await _attempt(client, upstream.url)
        assert not finished, f"finished after {elapsed:.2f}s"
        assert elapsed > BUDGET * 5
    finally:
        await _close(client)
        await socks.stop()
        await upstream.stop()


@pytest.mark.parametrize("surface", sorted(BUILDERS))
@pytest.mark.parametrize("behaviour", STALLING)
async def test_a_stalling_socks5_rung_fails_connect_shaped_within_the_budget(
    surface, behaviour
):
    """Every proxied client MCC builds ends the wait, and ends it the same way.

    ``httpx.ConnectTimeout`` is deliberate and load-bearing: it is the class a
    dead address's TCP dial already raises, it is already in
    ``proxy_rotating._REACHABILITY_TYPES``, and so the rung is benched and the
    chain switches exactly as it does for any other address that did not
    answer. No new failure kind is introduced by this fix.
    """

    upstream, socks = await _rig(behaviour)
    client = BUILDERS[surface](socks.url, upstream.url)
    try:
        finished, elapsed, error, _ = await _attempt(client, upstream.url)
        assert finished, f"still pending after {elapsed:.2f}s"
        assert isinstance(error, httpx.ConnectTimeout), repr(error)
        assert elapsed < BUDGET * 4, f"took {elapsed:.2f}s"
        # And the pool upstairs reads it as the address's fault, which is what
        # makes the chain move rather than the model.
        assert proxy_reachability_failure(error, proxied=True) is not None
    finally:
        await _close(client)
        await socks.stop()
        await upstream.stop()


@pytest.mark.parametrize("surface", sorted(BUILDERS))
async def test_an_honest_socks5_rung_is_untouched(surface):
    """The control. A proxy that behaves gets exactly the request it got."""

    upstream, socks = await _rig(HONEST)
    client = BUILDERS[surface](socks.url, upstream.url)
    try:
        finished, elapsed, error, response = await _attempt(client, upstream.url)
        assert finished and error is None, repr(error)
        assert response is not None and response.status_code == 200
        assert response.text == "rig"
        assert elapsed < BUDGET
        assert socks.accepted == 1
        assert upstream.requests == 1
    finally:
        await _close(client)
        await socks.stop()
        await upstream.stop()


@pytest.mark.parametrize("behaviour", [CLOSE_MID, BAD_AUTH])
async def test_a_rung_that_answers_badly_answers_exactly_as_before(behaviour):
    """The two behaviours that already failed fast fail identically.

    Same class with the bound installed and without it: this change is only
    about the wait that had no end, and a refusal that arrives in a
    millisecond was never that wait.
    """

    classes = []
    for bound in (False, True):
        upstream, socks = await _rig(behaviour)
        client = httpx.AsyncClient(proxy=socks.url, timeout=_timeout())
        if bound:
            bound_socks_handshake(client)
        try:
            finished, elapsed, error, _ = await _attempt(client, upstream.url)
            assert finished, f"still pending after {elapsed:.2f}s"
            assert elapsed < BUDGET
            classes.append(type(error))
        finally:
            await _close(client)
            await socks.stop()
            await upstream.stop()

    assert classes[0] is classes[1]
    assert classes[0] is not httpx.ConnectTimeout


# ---------------------------------------------------------------------------
# Everything this change is not allowed to touch
# ---------------------------------------------------------------------------


async def test_an_http_connect_rung_was_already_bounded_and_still_is():
    """The other rung type, pinned rather than asserted from the source.

    ``httpcore`` tunnels ``CONNECT`` through an ordinary HTTP/1.1 connection,
    so the request's own read timeout reaches it. A stalling HTTP rung ends on
    its own, with the same class and at the same moment, whether or not this
    module has been near the client -- and this module never is, because such
    a client holds no SOCKS pool.
    """

    outcomes = []
    for bound in (False, True):
        proxy = FakeHttpProxy("silent")
        await proxy.start()
        client = httpx.AsyncClient(proxy=proxy.url, timeout=_timeout())
        if bound:
            assert bound_socks_handshake(client) is client
        try:
            finished, elapsed, error, _ = await _attempt(
                client, "https://127.0.0.1:1/x"
            )
            assert finished, f"still pending after {elapsed:.2f}s"
            assert elapsed < BUDGET * 4, f"took {elapsed:.2f}s"
            assert proxy.accepted == 1
            outcomes.append(type(error))
        finally:
            await _close(client)
            await proxy.stop()

    assert outcomes[0] is outcomes[1]
    assert issubclass(outcomes[0], httpx.TimeoutException)


async def test_an_unproxied_client_is_the_object_it_was_before():
    """The equality proof, stated as identity: nothing is rebuilt or swapped."""

    client = httpx.AsyncClient(timeout=_timeout())
    try:
        before = (client._transport, dict(client._mounts), client.timeout)
        assert bound_socks_handshake(client) is client
        assert client._transport is before[0]
        assert dict(client._mounts) == before[1]
        assert client.timeout == before[2]
        pool = getattr(client._transport, "_pool", None)
        assert type(pool).__name__ == "AsyncConnectionPool"
        backend = getattr(pool, "_network_backend", None)
        assert type(backend).__name__ != "_HandshakeDeadlineBackend"
    finally:
        await _close(client)


async def test_an_http_proxied_client_is_the_object_it_was_before():
    """Same, one rung type over: only SOCKS pools get the deadline.

    Since 7.45.1 an HTTP proxy pool carries the dial stopwatch, which is
    pinned in ``tests/providers/test_proxy_dial_rows.py``.
    """

    client = httpx.AsyncClient(proxy="http://127.0.0.1:9", timeout=_timeout())
    try:
        mounts = dict(client._mounts)
        assert bound_socks_handshake(client) is client
        assert dict(client._mounts) == mounts
        for transport in client._mounts.values():
            pool = getattr(transport, "_pool", None)
            if pool is None:
                continue
            assert type(pool._network_backend).__name__ != "_HandshakeDeadlineBackend"
    finally:
        await _close(client)


@pytest.mark.parametrize("surface", sorted(BUILDERS))
async def test_every_proxied_client_site_actually_installs_the_bound(surface):
    """Structural, so a call site that loses the wrapper fails loudly here.

    The behavioural tests above cover the same ground, but they would also
    pass if only *one* of the three surfaces were wired; this one names each.
    """

    import httpcore

    from my_claude_code.providers.socks_deadline import _HandshakeDeadlineBackend

    client = BUILDERS[surface]("socks5://127.0.0.1:9", "http://rig.invalid/v1")
    try:
        pools = [
            pool
            for transport in client._mounts.values()
            if isinstance(
                pool := getattr(transport, "_pool", None), httpcore.AsyncSOCKSProxy
            )
        ]
        assert pools, "the client built no SOCKS pool"
        for pool in pools:
            assert isinstance(pool._network_backend, _HandshakeDeadlineBackend)
    finally:
        await _close(client)
