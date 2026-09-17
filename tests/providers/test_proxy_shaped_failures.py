"""An error reply keeps its status, and a broken address moves the request.

Two bugs measured on one install, on one day: 297 events in which a refusal by
the *host* was filed against the *model*, and every dead address in a chain
dialled twice before the chain was told. Both are about the same thing --
evidence reaching the frame that can act on it -- so both are tested here.

The properties these tests hold down are mostly negative ones: an unproxied
attempt classifies exactly as it did, a success streams exactly the bytes it
did, and a failure after the first chunk never moves address.
"""

import gzip

import httpx
import pytest

from my_claude_code.core.proxy_attribution import DIRECT_PROXY_LABEL, record_proxy
from my_claude_code.core.proxy_rotation import PROXY_REACHABILITY, reset_proxy_health
from my_claude_code.core.upstream_ladder import (
    install_ladder_trace,
    ladder_payload,
    note_response_head,
    record_upstream_try,
)
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.http import (
    ERROR_BODY_RAW_MAX_BYTES,
    response_head_record,
)
from my_claude_code.providers.openai_chat.responses_transport import ResponsesTransport
from my_claude_code.providers.rate_limit import ProviderRateLimiter
from my_claude_code.providers.runtime.proxy_leg import ProxiedLegRateLimiter
from my_claude_code.providers.runtime.proxy_rotating import proxy_reachability_failure
from tests.providers.test_credential_rotation import _FakeProvider
from tests.providers.test_proxy_rotation import _drain, _pool


@pytest.fixture(autouse=True)
def _clean_ledgers():
    reset_proxy_health()
    record_proxy(None)
    note_response_head(None)
    yield
    reset_proxy_health()
    record_proxy(None)
    note_response_head(None)


class _MalformedSocksReply(Exception):
    """Stands in for ``socksio.exceptions.ProtocolError``.

    Matched by module name in the classifier, so the double has to *live* in a
    module called ``socksio`` -- which is what the assignment below arranges.
    It is the same shape the real one arrives in: raised by the SOCKS state
    machine, carried on ``__cause__`` by httpx.
    """


_MalformedSocksReply.__module__ = "socksio.exceptions"


def _socks_failure() -> httpx.ReadError:
    """The httpx wrapper a failed SOCKS handshake actually arrives in.

    Deliberately *not* one of the reachability types: the point is that the
    socks fault is found by walking the cause chain, the way the real one is
    carried up through httpcore.
    """

    cause = _MalformedSocksReply("Malformed reply")
    error = httpx.ReadError("handshake failed")
    error.__cause__ = cause
    return error


# ------------------------------------------------- 1. the status survives


class _RawStream(httpx.AsyncByteStream):
    """Bytes handed to httpx without letting it decode them at construction.

    ``httpx.Response(..., content=b"...")`` reads -- and therefore decodes --
    inside ``__init__``, so a double whose ``Content-Encoding`` lies cannot be
    built that way at all. That is itself the second half of the bug under
    test; here it just means the double has to arrive as a stream.
    """

    def __init__(self, data: bytes) -> None:
        self._data = data

    async def __aiter__(self):
        yield self._data

    async def aclose(self) -> None:
        return None


def _reply(status: int, headers: dict[str, str], raw: bytes) -> httpx.Response:
    return httpx.Response(status, headers=headers, stream=_RawStream(raw))


def _transport(handler) -> ResponsesTransport:
    transport = ResponsesTransport(
        ProviderConfig(api_key="k", base_url="http://x"),
        base_url="http://upstream.test/v1",
        provider_name="OPENCODE",
        identity=None,
        api_key="k",
        rate_limiter=ProviderRateLimiter(rate_limit=0, rate_window=60),
    )
    transport._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return transport


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 429])
async def test_a_body_labelled_gzip_that_is_not_gzip_keeps_its_status(status) -> None:
    """The measured bug: 293 refusals by the host, filed against the model.

    ``aread()`` decodes, so an edge that labels a refusal ``gzip`` when it is
    not raised ``DecodingError`` one frame before ``HTTPStatusError`` was
    built -- taking the status, the headers and the ``cf-ray`` with it.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return _reply(
            status,
            {
                "content-type": "application/json",
                "content-encoding": "gzip",
                "cf-ray": "a3c9a1dc-ORD",
                "retry-after": "25517",
            },
            b'{"error":{"type":"FreeUsageLimitError"}}',
        )

    transport = _transport(handler)
    with pytest.raises(httpx.HTTPStatusError) as raised:
        await transport.send({"stream": True}, {})

    assert raised.value.response.status_code == status
    assert raised.value.response.headers["cf-ray"] == "a3c9a1dc-ORD"
    assert raised.value.response.headers["retry-after"] == "25517"
    # The raw bytes are kept, so the operator still sees what came back.
    assert b"FreeUsageLimitError" in raised.value.response.content


@pytest.mark.asyncio
async def test_a_genuinely_gzipped_refusal_is_decoded_and_keeps_its_status() -> None:
    """The quieter half of the same bug.

    A decoded body handed back to ``httpx.Response`` together with the original
    ``Content-Encoding`` raises ``DecodingError`` at construction, so every
    correctly-gzipped 4xx on this surface used to be lost too.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return _reply(
            403,
            {"content-type": "application/json", "content-encoding": "gzip"},
            gzip.compress(b'{"error":{"type":"FreeTierError"}}'),
        )

    transport = _transport(handler)
    with pytest.raises(httpx.HTTPStatusError) as raised:
        await transport.send({"stream": True}, {})

    assert raised.value.response.status_code == 403
    assert raised.value.response.json()["error"]["type"] == "FreeTierError"


@pytest.mark.asyncio
async def test_a_successful_stream_is_returned_open_and_undecoded() -> None:
    """The 2xx path is not on this change's route at all."""

    body = b'event: x\ndata: {"a":1}\n\n'

    def handler(request: httpx.Request) -> httpx.Response:
        return _reply(200, {"content-type": "text/event-stream"}, body)

    transport = _transport(handler)
    response = await transport.send({"stream": True}, {})
    try:
        assert response.status_code == 200
        assert b"".join([chunk async for chunk in response.aiter_raw()]) == body
    finally:
        await response.aclose()


# --------------------------------------------------- 6. the response head


@pytest.mark.asyncio
async def test_the_response_head_reaches_the_ladder_capped_and_hexed() -> None:
    """Status, the transport headers, and the first bytes -- as hex."""

    raw = b"\x1f\x8b\x08\x00" + b"\xff" * 400

    def handler(request: httpx.Request) -> httpx.Response:
        return _reply(
            429,
            {
                "content-type": "text/plain",
                "content-encoding": "gzip",
                "server": "cloudflare",
                "cf-ray": "a3c9-CMH",
                "retry-after": "25517",
                "authorization": "Bearer super-secret",
            },
            raw,
        )

    install_ladder_trace()
    transport = _transport(handler)
    with pytest.raises(httpx.HTTPStatusError):
        await transport.send({"stream": True}, {})
    record_upstream_try(status=429, upstream_ms=1.0)

    from my_claude_code.core.upstream_ladder import current_ladder

    ladder = current_ladder()
    assert ladder is not None
    payload = ladder_payload(ladder.slot())
    head = payload["tries"][0]["response_head"]
    assert head["status"] == 429
    assert head["content_encoding"] == "gzip"
    assert head["cf_ray"] == "a3c9-CMH"
    assert head["retry_after"] == "25517"
    assert head["body_head_hex"].startswith("1f8b0800")
    # 128 bytes, no more, and rendered as hex rather than as text.
    assert len(head["body_head_hex"]) == 256
    assert "decompress" in head["decode_error"]
    # An allow-list, so a header that could carry a credential is not there.
    assert not any("secret" in str(value) for value in head.values())
    assert "authorization" not in head


def test_the_head_is_taken_once_and_never_leaks_onto_a_later_try() -> None:
    """A head describes one try. Popped, not read."""

    install_ladder_trace()
    note_response_head(response_head_record(403, httpx.Headers({}), b"\x00"))
    record_upstream_try(status=403)
    record_upstream_try(status=200)

    from my_claude_code.core.upstream_ladder import current_ladder

    ladder = current_ladder()
    assert ladder is not None
    rows = ladder_payload(ladder.slot())["tries"]
    assert "response_head" in rows[0]
    assert "response_head" not in rows[1]


@pytest.mark.asyncio
async def test_an_undecodable_body_is_truncated_before_it_is_carried() -> None:
    raw = b"\x1f\x8b" + b"z" * (ERROR_BODY_RAW_MAX_BYTES * 3)

    def handler(request: httpx.Request) -> httpx.Response:
        return _reply(500, {"content-encoding": "gzip"}, raw)

    transport = _transport(handler)
    with pytest.raises(httpx.HTTPStatusError) as raised:
        await transport.send({"stream": True}, {})
    assert len(raised.value.response.content) == ERROR_BODY_RAW_MAX_BYTES


# --------------------------------------- 2. proxy-shaped only when proxied


def test_a_socks_protocol_error_is_the_address_when_the_attempt_was_proxied() -> None:
    assert (
        proxy_reachability_failure(_socks_failure(), proxied=True)
        == "socks _MalformedSocksReply"
    )


def test_a_socks_protocol_error_unproxied_is_answered_exactly_as_before() -> None:
    """Direct has no address to blame, and this machine is not benched."""

    assert proxy_reachability_failure(_socks_failure(), proxied=False) is None


def test_a_decoding_error_is_the_address_only_before_the_first_chunk() -> None:
    error = httpx.DecodingError("Error -3 while decompressing data")
    assert proxy_reachability_failure(error, proxied=True) == "DecodingError"
    assert (
        proxy_reachability_failure(error, proxied=True, before_first_chunk=False)
        is None
    )
    assert proxy_reachability_failure(error, proxied=False) is None


def test_a_connect_error_is_answered_the_same_either_side_of_the_first_chunk() -> None:
    """The pre-7.20 classes are untouched by the new gate."""

    error = httpx.ConnectError("refused")
    assert proxy_reachability_failure(error, proxied=True) == "ConnectError"
    assert (
        proxy_reachability_failure(error, proxied=True, before_first_chunk=False)
        == "ConnectError"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [_socks_failure(), httpx.DecodingError("Error -3 while decompressing data")],
    ids=["socks", "decoding"],
)
async def test_a_proxy_shaped_failure_switches_and_benches_the_address(error) -> None:
    labels = ("203.0.113.7:1080", "198.51.100.9:1080")
    first = _FakeProvider(fail_before_first=error)
    second = _FakeProvider(chunks=("ok",))
    pool = _pool([first, second], labels)

    assert await _drain(pool) == ["ok"]
    assert first.calls == 1
    assert second.calls == 1
    assert PROXY_REACHABILITY.is_unhealthy(labels[0])
    assert not PROXY_REACHABILITY.is_unhealthy(labels[1])


@pytest.mark.asyncio
async def test_a_decoding_error_after_the_first_chunk_never_moves_address() -> None:
    """Rule 4 of the module contract, restated for the new class.

    Output has started, so moving address would duplicate or corrupt the
    response -- and a decode fault there is the origin's, not the route's.
    """

    labels = ("203.0.113.7:1080", "198.51.100.9:1080")
    first = _FakeProvider(
        chunks=("a", "b"),
        fail_after_first=httpx.DecodingError("Error -3 while decompressing data"),
    )
    second = _FakeProvider(chunks=("ok",))
    pool = _pool([first, second], labels)

    with pytest.raises(httpx.DecodingError):
        await _drain(pool)
    assert second.calls == 0
    assert not PROXY_REACHABILITY.is_unhealthy(labels[0])


@pytest.mark.asyncio
async def test_the_direct_rung_is_never_benched_for_a_proxy_shaped_failure() -> None:
    labels = ("203.0.113.7:1080", DIRECT_PROXY_LABEL)
    first = _FakeProvider(
        fail_before_first=httpx.DecodingError("Error -3 while decompressing data")
    )
    direct = _FakeProvider(
        fail_before_first=httpx.DecodingError("Error -3 while decompressing data")
    )
    pool = _pool([first, direct], labels)

    with pytest.raises(httpx.DecodingError):
        await _drain(pool)
    assert PROXY_REACHABILITY.is_unhealthy(labels[0])
    assert not PROXY_REACHABILITY.is_unhealthy(DIRECT_PROXY_LABEL)


# ------------------------------------------- 5. D4: it still ends at Direct


@pytest.mark.asyncio
async def test_proxy_shaped_failures_count_as_live_failures_and_end_direct() -> None:
    """The 7.19.0 bound governs the new classes too. No second bound."""

    labels = ("203.0.113.7:1080", "198.51.100.9:1080", "203.0.113.8:1080")
    legs = [
        _FakeProvider(fail_before_first=httpx.DecodingError("decompressing data")),
        _FakeProvider(fail_before_first=httpx.DecodingError("decompressing data")),
        _FakeProvider(fail_before_first=httpx.DecodingError("decompressing data")),
        _FakeProvider(chunks=("direct",)),
    ]
    pool = _pool(legs, labels)
    pool._max_live_failures = 2

    assert await _drain(pool) == ["direct"]
    assert legs[0].calls + legs[1].calls + legs[2].calls == 2
    assert legs[3].calls == 1


# ------------------------------- 3. D1: a dead address is dialled once


class _Dial(Exception):
    pass


@pytest.mark.asyncio
async def test_a_proxied_connect_failure_is_dialled_once() -> None:
    """``PROVIDER_RETRY_ATTEMPTS`` used to run its ladder inside the leg."""

    dials = 0

    async def dial():
        nonlocal dials
        dials += 1
        raise httpx.ConnectError("no route to host")

    limiter = ProxiedLegRateLimiter(
        rate_limit=0, rate_window=60, max_retries=2, backoff_base_seconds=0.0
    )
    with pytest.raises(httpx.ConnectError):
        await limiter.execute_with_retry(dial)
    assert dials == 1


@pytest.mark.asyncio
async def test_an_unproxied_leg_retries_a_connect_failure_exactly_as_before() -> None:
    dials = 0

    async def dial():
        nonlocal dials
        dials += 1
        raise httpx.ConnectError("no route to host")

    limiter = ProviderRateLimiter(
        rate_limit=0, rate_window=60, max_retries=2, backoff_base_seconds=0.0
    )
    with pytest.raises(httpx.ConnectError):
        await limiter.execute_with_retry(dial)
    assert dials == 3


@pytest.mark.asyncio
async def test_a_proxied_leg_still_retries_a_status_failure() -> None:
    """A 5xx means the address answered. Another knock can still help."""

    calls = 0

    async def dial():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise httpx.HTTPStatusError(
                "boom",
                request=httpx.Request("POST", "http://x"),
                response=httpx.Response(502, request=httpx.Request("POST", "http://x")),
            )
        return "ok"

    limiter = ProxiedLegRateLimiter(
        rate_limit=0, rate_window=60, max_retries=2, backoff_base_seconds=0.0
    )
    assert await limiter.execute_with_retry(dial) == "ok"
    assert calls == 3


@pytest.mark.asyncio
async def test_the_original_connect_error_is_what_reaches_the_pool() -> None:
    """The sentinel never escapes, and the cause chain is intact."""

    cause = _MalformedSocksReply("Malformed reply")
    original = httpx.ConnectError("handshake failed")
    original.__cause__ = cause

    async def dial():
        raise original

    limiter = ProxiedLegRateLimiter(
        rate_limit=0, rate_window=60, max_retries=2, backoff_base_seconds=0.0
    )
    with pytest.raises(httpx.ConnectError) as raised:
        await limiter.execute_with_retry(dial)
    assert raised.value is original
    assert raised.value.__cause__ is cause


# ----------------------------------- 4. D2: the connect timeout, proxied only


def _chained_pool(connect_timeout: float):
    from my_claude_code.config.provider_catalog import PROVIDER_CATALOG
    from my_claude_code.config.settings import Settings
    from my_claude_code.providers.base import ProxyChainPlan, ProxyLeg
    from my_claude_code.providers.runtime.factory import _create_single_provider

    plan = ProxyChainPlan(
        legs=(
            ProxyLeg(url="http://203.0.113.7:8080", label="203.0.113.7:8080"),
            ProxyLeg(url="http://198.51.100.9:8080", label="198.51.100.9:8080"),
        ),
        policy="failover",
        on=frozenset(),
        scope="provider",
        max_switches=2,
    )
    config = ProviderConfig(
        api_key="k",
        base_url="http://upstream.test/v1",
        proxy_chain=plan,
        http_connect_timeout=45.0,
        http_read_timeout=123.0,
        http_write_timeout=77.0,
    )
    # By alias, through ``model_validate``: the field carries a
    # ``validation_alias``, which is the name an operator sets and the name the
    # manifest publishes, so that is the name the test uses.
    settings = Settings.model_validate(
        {
            "nvidia_nim_api_key": "k",
            "PROXY_CONNECT_TIMEOUT_SECONDS": connect_timeout,
        }
    )
    return _create_single_provider(PROVIDER_CATALOG["nvidia_nim"], config, settings)


def test_the_connect_timeout_applies_to_a_proxied_leg_and_nothing_else() -> None:
    """Connect only, proxied only. Read and write stay the provider's."""

    pool = _chained_pool(7.0)
    proxied = pool._pool.get(0)
    direct = pool._pool.get(2)

    assert proxied._config.proxy == "http://203.0.113.7:8080"
    assert proxied._config.http_connect_timeout == 7.0
    assert proxied._config.http_read_timeout == 123.0
    assert proxied._config.http_write_timeout == 77.0

    # The direct rung is not proxied, so it keeps the provider's own number.
    assert direct._config.proxy == ""
    assert direct._config.http_connect_timeout == 45.0


def test_only_a_proxied_leg_gets_the_limiter_that_dials_once() -> None:
    pool = _chained_pool(7.0)
    assert isinstance(pool._pool.get(0)._rate_limiter, ProxiedLegRateLimiter)
    direct_limiter = pool._pool.get(2)._rate_limiter
    assert isinstance(direct_limiter, ProviderRateLimiter)
    assert not isinstance(direct_limiter, ProxiedLegRateLimiter)


def test_a_provider_with_no_chain_is_built_from_the_unchanged_line() -> None:
    """The safety argument: no chain, nothing new, not even the limiter."""

    from my_claude_code.config.settings import Settings
    from my_claude_code.providers.runtime.factory import create_provider

    provider = create_provider("nvidia_nim", Settings(nvidia_nim_api_key="k1"))
    limiter = getattr(provider, "_rate_limiter", None)
    assert type(limiter) is ProviderRateLimiter
    assert provider._config.proxy == ""
