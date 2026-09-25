"""A proxy chain's dials, recorded through the pool's own ``record_proxy`` call.

``providers/runtime/proxy_rotating.py`` is frozen, and it does not need to
move: it already calls ``record_proxy`` immediately before every dial. These
tests drive the real :class:`ProxyRotatingProvider` over legs that commit
through the real retry frame (``ProviderRateLimiter.execute_with_retry``) and,
in the second half, over real ``httpx`` clients dialling local SOCKS5 and HTTP
``CONNECT`` listeners -- the rig from ``tests/support``, which binds
``127.0.0.1`` on port 0 and never reaches the network.

What they pin:

* a chain that dials three rungs writes three rows, each saying what the
  address answered and why the chain moved on;
* a dial that never completes leaves a ``dialing`` row, and a failure the
  chain never switched away from says how long nothing happened -- the two
  shapes the 09-16 park could not be told apart by;
* the try count, the tries themselves and the attributed label are exactly
  what they are with the per-dial observer switched off;
* connect and handshake times arrive from the socket, and the bytes a
  proxied request receives are the ones it received before.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator

import httpcore
import httpx
import pytest

from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.failures import ExecutionFailure, FailureKind
from my_claude_code.core.proxy_attribution import (
    _CURRENT,
    current_proxy,
    install_proxy_attribution,
    record_proxy,
)
from my_claude_code.core.proxy_rotation import reset_proxy_health
from my_claude_code.core.reasoning import DEFAULT_REASONING_POLICY, ReasoningPolicy
from my_claude_code.core.upstream_ladder import (
    _LADDER,
    install_ladder_trace,
    ladder_payload,
    ladder_proxy_label,
    record_proxy_dial,
)
from my_claude_code.providers.openai_chat.provider import _proxied_http_client
from my_claude_code.providers.proxy_dial_clock import _DialClockBackend
from my_claude_code.providers.rate_limit import ProviderRateLimiter
from my_claude_code.providers.socks_deadline import (
    _HandshakeDeadlineBackend,
    bound_socks_handshake,
)
from tests.providers.test_credential_rotation import _FakeProvider, _request
from tests.providers.test_proxy_rotation import _pool
from tests.support.fake_http_proxy import FakeHttpProxy
from tests.support.fake_socks5 import (
    HONEST,
    SILENT,
    FakeSocks5Server,
    FakeUpstream,
)

pytestmark = pytest.mark.asyncio

#: The proxied leg's connect budget. Short so the stalled rung is quick.
BUDGET = 0.6
READ = 0.4
#: The suite's own bound on anything that could park.
OUTER = 6.0


@pytest.fixture(autouse=True)
def _clean():
    reset_proxy_health()
    _LADDER.set(None)
    _CURRENT.set(None)
    yield
    reset_proxy_health()
    _LADDER.set(None)
    _CURRENT.set(None)


def _track(*, per_dial: bool = True):
    """The two slots as ``RequestCapture`` installs them, observer optional."""

    install_proxy_attribution(on_dial=record_proxy_dial if per_dial else None)
    return install_ladder_trace()


def _rate_limited() -> ExecutionFailure:
    return ExecutionFailure(
        kind=FailureKind.RATE_LIMIT,
        status_code=429,
        message="upstream 429",
        retryable=True,
    )


class _Leg(_FakeProvider):
    """One rung that commits through the real retry frame, like every leaf."""

    def __init__(self, behaviour: str) -> None:
        super().__init__()
        self.behaviour = behaviour
        self.parked = asyncio.Event()
        self._limiter = ProviderRateLimiter(
            rate_limit=0, rate_window=60, max_retries=0, routes_around_model=True
        )

    async def _call(self) -> str:
        if self.behaviour == "429":
            raise _rate_limited()
        if self.behaviour == "refused":
            raise httpx.ConnectError("connection refused")
        if self.behaviour == "hang":
            self.parked.set()
            await asyncio.Event().wait()
        return "chunk"

    def stream_response(
        self,
        request: MessagesRequest,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> AsyncIterator[str]:
        async def _gen() -> AsyncIterator[str]:
            yield await self._limiter.execute_with_retry(self._call)

        return _gen()


LABELS = ("10.0.0.1:1080", "10.0.0.2:3128", "10.0.0.3:1080")


async def _drain(provider) -> list[str]:
    return [chunk async for chunk in provider.stream_response(_request())]


# ------------------------------------------------------ through the real pool


async def test_a_chain_that_dials_three_rungs_writes_three_rows() -> None:
    ladder = _track()
    pool = _pool([_Leg("429"), _Leg("refused"), _Leg("ok")], LABELS)

    assert await _drain(pool) == ["chunk"]

    payload = ladder_payload(ladder.ladders[0])
    rows = payload["dials"]
    assert [row["proxy"] for row in rows] == list(LABELS)
    assert [row["outcome"] for row in rows] == ["switched", "switched", "answered"]
    assert [row.get("reason") for row in rows] == ["429", "ConnectError", None]
    assert [row["at_try"] for row in rows] == [0, 1, 2]
    # Each try still carries its own address, exactly as before.
    assert [row["proxy"] for row in payload["tries"]] == list(LABELS)


def _untimed(payload: dict) -> dict:
    tries = [
        {k: v for k, v in row.items() if k != "upstream_ms"} for row in payload["tries"]
    ]
    summary = {k: v for k, v in payload["summary"].items() if k != "time_upstream_ms"}
    return payload | {"tries": tries, "summary": summary}


async def test_the_tries_and_the_label_are_what_they_were_without_it() -> None:
    """``tries`` count, rows and ``proxy_label`` identical, observer on or off."""

    observed = []
    for per_dial in (False, True):
        reset_proxy_health()
        ladder = _track(per_dial=per_dial)
        pool = _pool([_Leg("429"), _Leg("refused"), _Leg("ok")], LABELS)
        assert await _drain(pool) == ["chunk"]
        payload = ladder_payload(ladder.ladders[0])
        observed.append((payload, current_proxy()))

    (before, label_before), (after, label_after) = observed
    assert "dials" not in before
    # Everything but the two wall-clock measurements, which differ between any
    # two runs of the same code.
    assert _untimed({k: v for k, v in after.items() if k != "dials"}) == _untimed(
        before
    )
    assert after["summary"]["tries"] == before["summary"]["tries"] == 3
    assert ladder_proxy_label(after) == ladder_proxy_label(before) == LABELS[2]
    assert label_after == label_before == LABELS[2]


async def test_a_dial_that_never_completes_leaves_a_dialing_row() -> None:
    ladder = _track()
    parked = _Leg("hang")
    pool = _pool([_Leg("429"), parked, _Leg("ok")], LABELS)

    task = asyncio.create_task(_drain(pool))
    await asyncio.wait_for(parked.parked.wait(), OUTER)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    payload = ladder_payload(ladder.ladders[0])
    rows = payload["dials"]
    assert [row["outcome"] for row in rows] == ["switched", "dialing"]
    assert rows[0]["reason"] == "429"
    assert rows[1]["proxy"] == LABELS[1]
    assert rows[1]["elapsed_ms"] >= 0
    # The try count is still the one completed try.
    assert payload["summary"]["tries"] == 1


async def test_a_park_before_the_switch_is_told_apart_from_one_in_the_dial(
    monkeypatch,
) -> None:
    """The 09-16 shape: a 429 on rung one, then no second dial at all.

    ``report_failure`` stands in for whichever of the three awaits held the
    nine requests; parking it is the only way to reproduce the shape without
    touching the frozen loop.
    """

    ladder = _track()
    pool = _pool([_Leg("429"), _Leg("ok")], LABELS[:2])
    parked = asyncio.Event()

    async def _stuck(*_args, **_kwargs):
        parked.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(pool._state, "report_failure", _stuck)
    task = asyncio.create_task(_drain(pool))
    await asyncio.wait_for(parked.wait(), OUTER)
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    rows = ladder_payload(ladder.ladders[0])["dials"]
    assert len(rows) == 1
    assert rows[0]["outcome"] == "failed"
    assert rows[0]["reason"] == "429"
    assert rows[0]["idle_ms"] >= 50.0


async def test_the_direct_fallback_is_a_dial_too() -> None:
    ladder = _track()
    labels = LABELS[:2]
    legs: list[_FakeProvider] = [_Leg("refused"), _Leg("refused"), _Leg("ok")]
    # The third, one past the chain, is the Direct leg.
    pool = _pool(legs, labels)

    assert await _drain(pool) == ["chunk"]

    rows = ladder_payload(ladder.ladders[0])["dials"]
    assert [row["proxy"] for row in rows] == [*labels, "direct"]
    assert rows[-1]["outcome"] == "answered"


async def test_an_unlogged_request_records_the_label_and_nothing_else() -> None:
    install_proxy_attribution()
    pool = _pool([_Leg("429"), _Leg("ok")], LABELS[:2])
    assert await _drain(pool) == ["chunk"]
    assert current_proxy() == LABELS[1]
    assert _LADDER.get() is None


# -------------------------------------------------- real sockets, local rig


def _timeout() -> httpx.Timeout:
    return httpx.Timeout(READ, connect=BUDGET, read=READ, write=READ, pool=READ)


async def _close(client: httpx.AsyncClient) -> None:
    with contextlib.suppress(BaseException):
        await asyncio.wait_for(client.aclose(), 3.0)


async def _get(client: httpx.AsyncClient, url: str):
    """One GET under the suite's own bound; the outcome, never a hang."""

    task = asyncio.create_task(client.get(url))
    done, _ = await asyncio.wait({task}, timeout=OUTER)
    if not done:
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        raise AssertionError("still pending")
    try:
        response = task.result()
    except Exception as error:
        return type(error), None
    return response.status_code, response.text


async def test_a_socks5_dial_reports_its_connect_and_its_handshake() -> None:
    upstream = FakeUpstream()
    await upstream.start()
    socks = FakeSocks5Server(HONEST, upstream_port=upstream.port)
    await socks.start()
    client = _proxied_http_client(socks.url, upstream.url, _timeout())
    try:
        ladder = _track()
        record_proxy("127.0.0.1:socks")
        assert await _get(client, upstream.url) == (200, "rig")
        dial = ladder.ladders[0].dials[0]
        assert dial.connect_ms is not None and dial.connect_ms >= 0
        assert dial.handshake_ms is not None and dial.handshake_ms >= 0
    finally:
        await _close(client)
        await socks.stop()
        await upstream.stop()


async def test_a_stalled_socks5_dial_has_a_connect_and_no_handshake() -> None:
    upstream = FakeUpstream()
    await upstream.start()
    socks = FakeSocks5Server(SILENT, upstream_port=upstream.port)
    await socks.start()
    client = _proxied_http_client(socks.url, upstream.url, _timeout())
    try:
        ladder = _track()
        record_proxy("127.0.0.1:socks")
        outcome, _ = await _get(client, upstream.url)
        assert outcome is httpx.ConnectTimeout  # the 7.36.0 bound, unchanged
        dial = ladder.ladders[0].dials[0]
        assert dial.connect_ms is not None
        assert dial.handshake_ms is None
    finally:
        await _close(client)
        await socks.stop()
        await upstream.stop()


async def test_an_http_connect_dial_reports_its_handshake_at_the_tls_start() -> None:
    """The rig's origin speaks no TLS, so the request fails *after* the tunnel.

    That is enough to show the order: the proxy answered ``CONNECT``, the
    handshake was reported, and only then did TLS fail -- with the class it
    fails with on a client that carries no stopwatch.
    """

    upstream = FakeUpstream()
    await upstream.start()
    proxy = FakeHttpProxy(HONEST, upstream_port=upstream.port)
    await proxy.start()
    origin = f"https://127.0.0.1:{upstream.port}/"
    clocked = _proxied_http_client(proxy.url, origin, _timeout())
    bare = httpx.AsyncClient(proxy=proxy.url, timeout=_timeout())
    try:
        ladder = _track()
        record_proxy("127.0.0.1:http")
        with_clock, _ = await _get(clocked, origin)
        dial = ladder.ladders[0].dials[0]
        assert dial.connect_ms is not None
        assert dial.handshake_ms is not None
        without_clock, _ = await _get(bare, origin)
        assert with_clock is without_clock
    finally:
        await _close(clocked)
        await _close(bare)
        await proxy.stop()
        await upstream.stop()


async def test_a_proxied_answer_is_byte_identical_with_the_stopwatch() -> None:
    upstream = FakeUpstream(body=b'{"ok": true, "bytes": "\\u00e9"}')
    await upstream.start()
    socks = FakeSocks5Server(HONEST, upstream_port=upstream.port)
    await socks.start()
    clocked = _proxied_http_client(socks.url, upstream.url, _timeout())
    bare = httpx.AsyncClient(proxy=socks.url, timeout=_timeout())
    try:
        _track()
        record_proxy("127.0.0.1:socks")
        assert await _get(clocked, upstream.url) == await _get(bare, upstream.url)
    finally:
        await _close(clocked)
        await _close(bare)
        await socks.stop()
        await upstream.stop()


async def test_a_real_chain_switches_off_a_stalled_socks5_rung() -> None:
    """Pool, retry frame, sockets and ladder together, end to end.

    Rung one is a SOCKS5 listener that accepts and never answers; rung two is
    an honest one. The ladder reads: dialled the first, connected, no
    handshake, ``ConnectTimeout``, switched; dialled the second, connected,
    handshake done, answered.
    """

    upstream = FakeUpstream()
    await upstream.start()
    stalled = FakeSocks5Server(SILENT, upstream_port=upstream.port)
    honest = FakeSocks5Server(HONEST, upstream_port=upstream.port)
    await stalled.start()
    await honest.start()
    clients = [
        _proxied_http_client(server.url, upstream.url, _timeout())
        for server in (stalled, honest)
    ]

    class _HttpLeg(_Leg):
        def __init__(self, client: httpx.AsyncClient) -> None:
            super().__init__("ok")
            self._client = client

        async def _call(self) -> str:
            response = await self._client.get(upstream.url)
            return response.text

    try:
        ladder = _track()
        labels = ("127.0.0.1:stalled", "127.0.0.1:honest")
        pool = _pool([_HttpLeg(client) for client in clients], labels)
        assert await asyncio.wait_for(_drain(pool), OUTER) == ["rig"]

        payload = ladder_payload(ladder.ladders[0])
        first, second = payload["dials"]
        assert (first["proxy"], first["outcome"], first["reason"]) == (
            labels[0],
            "switched",
            "ConnectTimeout",
        )
        assert "connect_ms" in first and "handshake_ms" not in first
        assert (second["proxy"], second["outcome"]) == (labels[1], "answered")
        assert "connect_ms" in second and "handshake_ms" in second
        assert payload["summary"]["tries"] == 2
    finally:
        for client in clients:
            await _close(client)
        await stalled.stop()
        await honest.stop()
        await upstream.stop()


# ------------------------------------------------------ what is installed


def _pools(client: httpx.AsyncClient, kind: type) -> list[object]:
    pools = [getattr(transport, "_pool", None) for transport in client._mounts.values()]
    return [pool for pool in pools if isinstance(pool, kind)]


def _backend(holder: object) -> object:
    """The backend a pool or a wrapping backend holds, by its private name."""

    return getattr(holder, "_network_backend", None) or getattr(
        holder, "_backend", None
    )


async def test_an_http_proxy_pool_gets_the_stopwatch_and_no_deadline() -> None:
    client = _proxied_http_client(
        "http://127.0.0.1:9", "http://rig.invalid/v1", _timeout()
    )
    try:
        pools = _pools(client, httpcore.AsyncHTTPProxy)
        assert pools
        for pool in pools:
            clock = _backend(pool)
            assert isinstance(clock, _DialClockBackend)
            assert not isinstance(_backend(clock), _HandshakeDeadlineBackend)
        # Binding again changes nothing: timed once, not twice.
        before = [_backend(pool) for pool in pools]
        bound_socks_handshake(client)
        assert [_backend(pool) for pool in pools] == before
    finally:
        await _close(client)


async def test_a_socks_pool_has_the_stopwatch_inside_the_deadline() -> None:
    client = _proxied_http_client(
        "socks5://127.0.0.1:9", "http://rig.invalid/v1", _timeout()
    )
    try:
        pools = _pools(client, httpcore.AsyncSOCKSProxy)
        assert pools
        before = [_backend(pool) for pool in pools]
        bound_socks_handshake(client)
        assert [_backend(pool) for pool in pools] == before
        for pool in pools:
            deadline = _backend(pool)
            assert isinstance(deadline, _HandshakeDeadlineBackend)
            clock = _backend(deadline)
            assert isinstance(clock, _DialClockBackend)
            assert not isinstance(_backend(clock), _DialClockBackend)
    finally:
        await _close(client)


async def test_an_unproxied_client_gets_no_stopwatch() -> None:
    client = httpx.AsyncClient(timeout=_timeout())
    try:
        transport = client._transport
        assert bound_socks_handshake(client) is client
        assert client._transport is transport
        pool = getattr(transport, "_pool", None)
        backend = getattr(pool, "_network_backend", None)
        assert not isinstance(backend, _DialClockBackend)
    finally:
        await _close(client)
