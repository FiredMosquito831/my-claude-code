"""PR-S3 (7.89.0): the scan of this computer, and the offers it makes.

The scan knocks on ``127.0.0.1`` only, asks each port the opening question of
SOCKS5 and then of an HTTP proxy, and offers every one that answered as a
proxy. An offer is an ordinary catalogue address that a feed pass and a
prune leave alone; a port that stops answering is withdrawn.
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

import pytest

from my_claude_code.application import proxy_sources as sources_module
from my_claude_code.application.proxy_sources import (
    LOCAL_SCAN_PORTS,
    ScanAnswer,
    apply_local_scan,
    local_offer_id,
    probe_local_port,
    remove_source,
    scan_local,
    set_local_credential,
    sources_document,
)
from my_claude_code.config.proxy_chains import (
    ProxyChain,
    ProxyChainEntry,
    ProxyChains,
    ProxyEndpoint,
)
from my_claude_code.config.proxy_sources import (
    LOCAL_SOURCE_ID,
    ProxySources,
    SourceSecret,
)

Handler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


async def _socks5(method: bytes) -> Handler:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        with contextlib.suppress(Exception):
            await reader.readexactly(4)
            writer.write(b"\x05" + method)
            await writer.drain()
            await reader.read(1)
        writer.close()

    return handle


async def _http(status: bytes) -> Handler:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        with contextlib.suppress(Exception):
            first = await reader.read(4)
            if first.startswith(b"\x05"):
                writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            else:
                await reader.readuntil(b"\r\n\r\n")
                writer.write(b"HTTP/1.1 " + status + b"\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
        writer.close()

    return handle


async def _silent() -> Handler:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        with contextlib.suppress(Exception):
            await reader.read(1024)
            await asyncio.sleep(5)
        writer.close()

    return handle


@contextlib.asynccontextmanager
async def _listener(handler: Handler):
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(server.wait_closed(), 2)


def _closed_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.mark.asyncio
@pytest.mark.local_serial
@pytest.mark.parametrize(
    ("make", "expected"),
    [
        (lambda: _socks5(b"\x00"), ("socks5", "none", "")),
        (lambda: _socks5(b"\x02"), ("socks5", "userpass", "")),
        (lambda: _socks5(b"\xff"), ("socks5", "unsupported", "neither")),
        (lambda: _http(b"502 Bad Gateway"), ("http", "none", "")),
        (lambda: _http(b"407 Proxy Authentication Required"), ("http", "userpass", "")),
        (lambda: _http(b"404 Not Found"), ("", "none", "not as a proxy")),
    ],
)
async def test_each_listener_is_told_apart_by_its_answer(make, expected) -> None:
    async with _listener(await make()) as port:
        answer = await probe_local_port(port, timeout=1.0)

    protocol, auth, note = expected
    assert answer.answering
    assert (answer.protocol, answer.auth) == (protocol, auth)
    assert note in answer.note


@pytest.mark.asyncio
@pytest.mark.local_serial
async def test_a_listener_that_never_speaks_is_not_a_proxy() -> None:
    async with _listener(await _silent()) as port:
        answer = await probe_local_port(port, timeout=0.3)

    assert answer.answering
    assert answer.protocol == ""
    assert "not as a SOCKS5 or HTTP proxy" in answer.note


@pytest.mark.asyncio
async def test_nothing_listening_is_not_answering() -> None:
    answer = await probe_local_port(_closed_port(), timeout=0.5)

    assert answer == ScanAnswer(port=answer.port, answering=False)


@pytest.mark.asyncio
async def test_the_scan_dials_this_computer_only_and_only_its_ports(
    monkeypatch,
) -> None:
    dialled: list[tuple[str, int]] = []

    async def record(host, port, *args, **kwargs):
        dialled.append((host, port))
        raise ConnectionRefusedError

    monkeypatch.setattr(sources_module.asyncio, "open_connection", record)

    answers = await scan_local()

    assert {host for host, _ in dialled} == {"127.0.0.1"}
    assert sorted({port for _, port in dialled}) == sorted(
        item.port for item in LOCAL_SCAN_PORTS
    )
    assert sorted(item.port for item in LOCAL_SCAN_PORTS) == [
        1080,
        8888,
        9050,
        9052,
        9150,
        25344,
        40000,
    ]
    assert all(not answer.answering for answer in answers)


# ------------------------------------------------------------------ offers

FOUND = [
    ScanAnswer(1080, True, "socks5", "none"),
    ScanAnswer(9050, True, "socks5", "none"),
    ScanAnswer(40000, True, "socks5", "none"),
    ScanAnswer(8888, False),
    ScanAnswer(9150, False),
]


def test_three_listeners_three_offers_none_without_a_listener() -> None:
    chains, sources = apply_local_scan(
        ProxyChains(), ProxySources(), FOUND, at="2026-10-09T10:00:00Z"
    )

    offered = chains.source_offers[LOCAL_SOURCE_ID]
    assert offered == tuple(
        local_offer_id(port, "socks5") for port in (1080, 9050, 40000)
    )
    for port in (1080, 9050, 40000):
        endpoint = chains.endpoint(local_offer_id(port, "socks5"))
        assert endpoint is not None
        assert endpoint.url == f"socks5h://127.0.0.1:{port}"
        assert endpoint.label == f"Local SOCKS5 · 127.0.0.1:{port}"
        assert endpoint.source == "source"
        assert endpoint.source_id == LOCAL_SOURCE_ID
    source = sources.source(LOCAL_SOURCE_ID)
    assert source is not None
    assert [listener.port for listener in source.listeners] == [1080, 9050, 40000]
    # The document round-trips with its offers.
    assert ProxyChains.from_document(chains.as_document()).source_offers == (
        chains.source_offers
    )


def test_offers_survive_a_feed_pass_and_a_prune() -> None:
    chains, _ = apply_local_scan(ProxyChains(), ProxySources(), FOUND)
    feed = ProxyEndpoint(url="socks5h://192.0.2.9:1080", label="192.0.2.9:1080")

    after = chains.with_candidates([("px_feed", feed)]).with_candidates([]).pruned()

    assert after.source_offers == chains.source_offers
    for proxy_id in chains.source_offers[LOCAL_SOURCE_ID]:
        assert after.endpoint(proxy_id) is not None
        assert after.is_on_offer(proxy_id)
    assert "px_feed" not in after.proxies


def test_a_rescan_keeps_the_id_and_withdraws_a_port_that_stopped() -> None:
    chains, sources = apply_local_scan(ProxyChains(), ProxySources(), FOUND)
    tor = local_offer_id(9050, "socks5")
    chains = chains.with_chain(
        "opencode", ProxyChain(enabled=True, entries=(ProxyChainEntry(proxy=tor),))
    )

    again, _ = apply_local_scan(
        chains, sources, [ScanAnswer(9050, True, "socks5", "none")]
    )

    assert again.source_offers[LOCAL_SOURCE_ID] == (tor,)
    assert again.endpoint(local_offer_id(1080, "socks5")) is None
    # Nothing answers now: no offer -- but the chain keeps its entry.
    empty, _ = apply_local_scan(again, sources, [ScanAnswer(9050, False)])
    assert LOCAL_SOURCE_ID not in empty.source_offers
    assert empty.endpoint(tor) is not None
    chain = empty.chain("opencode")
    assert chain is not None
    assert chain.proxy_ids() == (tor,)


def test_a_listener_already_typed_by_hand_is_reused_not_duplicated() -> None:
    typed = ProxyEndpoint(url="socks5://127.0.0.1:9050")
    chains = ProxyChains(
        proxies={"px_typed": typed},
        chains={
            "opencode": ProxyChain(
                enabled=True, entries=(ProxyChainEntry(proxy="px_typed"),)
            )
        },
    )

    after, _ = apply_local_scan(chains, ProxySources(), FOUND[1:2])

    assert after.source_offers[LOCAL_SOURCE_ID] == ("px_typed",)
    assert after.endpoint("px_typed") == typed
    assert local_offer_id(9050, "socks5") not in after.proxies


def _url(chains: ProxyChains, proxy_id: str) -> str:
    endpoint = chains.endpoint(proxy_id)
    assert endpoint is not None
    return endpoint.url


def _secret_of(sources: ProxySources, port: int) -> SourceSecret | None:
    source = sources.source(LOCAL_SOURCE_ID)
    assert source is not None
    listener = source.listener(port)
    assert listener is not None
    return sources.secret(listener.secret)


def test_a_login_rides_in_the_url_and_never_in_the_payload() -> None:
    answers = [ScanAnswer(1080, True, "socks5", "userpass")]
    chains, sources = apply_local_scan(ProxyChains(), ProxySources(), answers)
    proxy_id = local_offer_id(1080, "socks5")
    assert _url(chains, proxy_id) == "socks5h://127.0.0.1:1080"

    chains, sources = set_local_credential(
        chains, sources, 1080, username="me@home", password="p:ss/w0rd"
    )

    url = _url(chains, proxy_id)
    assert url == "socks5h://me%40home:p%3Ass%2Fw0rd@127.0.0.1:1080"
    assert chains.ledger_label(proxy_id) == "Local SOCKS5 · 127.0.0.1:1080"
    secret = _secret_of(sources, 1080)
    assert secret is not None
    assert secret.password == "p:ss/w0rd"
    text = repr(sources_document(chains, sources))
    assert "me@home" not in text and "p:ss" not in text and "w0rd" not in text

    # A rescan keeps the login while the port still asks for one.
    rescanned, kept = apply_local_scan(chains, sources, answers)
    assert _url(rescanned, proxy_id) == url
    assert _secret_of(kept, 1080) is not None

    cleared, without = set_local_credential(rescanned, kept, 1080, clear=True)
    assert _url(cleared, proxy_id) == "socks5h://127.0.0.1:1080"
    assert without.secrets == {}


def test_removing_the_source_withdraws_its_offers_and_keeps_chains() -> None:
    chains, sources = apply_local_scan(ProxyChains(), ProxySources(), FOUND)
    tor = local_offer_id(9050, "socks5")
    chains = chains.with_chain(
        "opencode", ProxyChain(enabled=True, entries=(ProxyChainEntry(proxy=tor),))
    )

    after, gone = remove_source(chains, sources, LOCAL_SOURCE_ID)

    assert after.source_offers == {}
    assert gone.source(LOCAL_SOURCE_ID) is None
    assert after.endpoint(tor) is not None
    assert after.endpoint(local_offer_id(1080, "socks5")) is None
