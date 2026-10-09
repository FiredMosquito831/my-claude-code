"""A tor the user runs: its SOCKS ports as chain addresses (7.90.0).

Decision 5(a) of 2026-10-06: several Tor ports are several chain entries.
Each port is offered as an ordinary ``socks5h://127.0.0.1:<port>`` address
with a name of its own -- distinct ports, distinct names, so every ledger
keeps one book per identity -- through the same offer path a scanned listener
uses. Saving contacts nothing. Ports are whatever the user typed: on the
user's own machine 9050, 9052 and 9150 sit in a Windows reserved range.
"""

import json

import httpx
import pytest

from my_claude_code.application.proxy_sources import (
    ScanAnswer,
    SourceEditError,
    apply_local_scan,
    local_offer_id,
    remove_source,
    sources_document,
)
from my_claude_code.application.tor_control import (
    NEWNYM_GUARD,
    TOR_READINGS,
    TorReading,
)
from my_claude_code.application.tor_source import (
    TorEdit,
    save_tor_source,
    tor_label,
    tor_offer_id,
)
from my_claude_code.config.proxy_chains import (
    ProxyChain,
    ProxyChainEntry,
    ProxyChains,
    ProxyEndpoint,
)
from my_claude_code.config.proxy_sources import SECRET_PASSWORD, ProxySources
from tests.support.fake_socks5 import FakeSocks5Server, FakeUpstream
from tests.support.masking_harness import closed_port

PORTS = (19250, 19251, 19252)
CONTROL = 19260
PASSWORD = "tor-control-pw-391"


def _save(
    chains: ProxyChains | None = None,
    sources: ProxySources | None = None,
    source_id: str = "",
    *,
    ports: tuple[int, ...] = PORTS,
    control: int = CONTROL,
    auth: str = "cookie",
    password: str = "",
) -> tuple[ProxyChains, ProxySources, str]:
    return save_tor_source(
        chains or ProxyChains(),
        sources or ProxySources(),
        source_id,
        TorEdit(socks_ports=ports, control_port=control, auth=auth, password=password),
        at="2026-10-09T12:00:00Z",
    )


def _loopback_rows(chains: ProxyChains, port: int) -> list[str]:
    return [
        proxy_id
        for proxy_id, endpoint in chains.proxies.items()
        if endpoint.url.endswith(f"127.0.0.1:{port}")
    ]


def test_each_socks_port_is_one_offer_with_its_own_name() -> None:
    chains, sources, source_id = _save()

    assert source_id == "src_tor"
    source = sources.source(source_id)
    assert source is not None and source.tor is not None
    assert source.tor.ports == PORTS
    ids = [tor_offer_id(source_id, port) for port in PORTS]
    assert chains.source_offers == {source_id: tuple(ids)}
    assert [item.proxy for item in source.tor.socks_ports] == ids
    endpoints = [chains.proxies[proxy_id] for proxy_id in ids]
    assert [endpoint.url for endpoint in endpoints] == [
        f"socks5h://127.0.0.1:{port}" for port in PORTS
    ]
    assert [endpoint.label for endpoint in endpoints] == [
        "Tor · 127.0.0.1:19250",
        "Tor · 127.0.0.1:19251",
        "Tor · 127.0.0.1:19252",
    ]
    labels = [chains.ledger_label(proxy_id) for proxy_id in ids]
    assert labels == [tor_label(port) for port in PORTS]
    assert len(set(labels)) == len(PORTS)
    assert all(endpoint.source_id == source_id for endpoint in endpoints)
    assert all(chains.is_on_offer(proxy_id) for proxy_id in ids)


def test_ports_are_deduplicated_in_the_order_given() -> None:
    _chains, sources, source_id = _save(ports=(19251, 19250, 19251))

    source = sources.source(source_id)
    assert source is not None and source.tor is not None
    assert source.tor.ports == (19251, 19250)


@pytest.mark.parametrize(
    ("ports", "control", "auth", "words"),
    [
        ((), CONTROL, "cookie", "Give at least one SOCKS port"),
        ((0,), CONTROL, "cookie", "SOCKS port 0 is not a TCP port"),
        ((70000,), CONTROL, "cookie", "SOCKS port 70000 is not a TCP port"),
        ((19250,), 0, "cookie", "Control port 0 is not a TCP port"),
        (
            (19250, 19260),
            19260,
            "cookie",
            "cannot be both a SOCKS port and the control",
        ),
        (
            (19250,),
            CONTROL,
            "magic",
            "cookie file tor names, or with a control password",
        ),
        ((19250,), CONTROL, "password", "Type the control password"),
    ],
)
def test_a_bad_form_is_refused_with_a_sentence(
    ports: tuple[int, ...], control: int, auth: str, words: str
) -> None:
    with pytest.raises(SourceEditError, match=words):
        _save(ports=ports, control=control, auth=auth)


def test_a_second_tor_cannot_take_the_first_one_s_ports() -> None:
    chains, sources, _ = _save()

    with pytest.raises(SourceEditError, match="Port 19251 already belongs"):
        _save(chains, sources, ports=(19251, 19300), control=19301)
    with pytest.raises(SourceEditError, match="Port 19260 already belongs"):
        _save(chains, sources, ports=(19300,), control=CONTROL)
    chains, sources, second = _save(chains, sources, ports=(19300,), control=19301)

    assert second == "src_tor_2"
    assert set(chains.source_offers) == {"src_tor", "src_tor_2"}


def test_a_control_password_is_a_secret_kept_until_the_login_changes() -> None:
    chains, sources, source_id = _save(auth="password", password=PASSWORD)
    source = sources.source(source_id)
    assert source is not None and source.tor is not None
    secret = sources.secret(source.tor.secret)
    assert secret is not None
    assert secret.type == SECRET_PASSWORD
    assert secret.password == PASSWORD
    assert secret.as_document() == {"type": "password", "password": PASSWORD}
    assert secret.label == ""

    # Saving again with the box empty keeps the stored password.
    chains, sources, _ = _save(
        chains, sources, source_id, ports=(19250,), auth="password"
    )
    kept = sources.source(source_id)
    assert kept is not None and kept.tor is not None
    assert kept.tor.secret == source.tor.secret
    assert sources.secret(kept.tor.secret) == secret

    # Back to the cookie: the password is not kept for nothing.
    chains, sources, _ = _save(chains, sources, source_id, auth="cookie")
    assert sources.secrets == {}


def test_the_payload_never_carries_the_password_and_shows_the_torrc() -> None:
    chains, sources, source_id = _save(auth="password", password=PASSWORD)

    document = sources_document(chains, sources)
    text = json.dumps(document)

    assert PASSWORD not in text
    tor = document["sources"][0]
    assert tor["kind"] == "tor"
    assert tor["id"] == source_id
    assert tor["auth"] == "password"
    assert tor["secret_set"] is True
    assert tor["control_port"] == CONTROL
    assert [row["port"] for row in tor["ports"]] == list(PORTS)
    assert [row["label"] for row in tor["ports"]] == [tor_label(p) for p in PORTS]
    assert all(row["offered"] for row in tor["ports"])
    assert "status" not in tor
    assert "newnym" not in tor
    assert tor["torrc"] == (
        "# My Claude Code: one SocksPort per identity, and the control port\n"
        "SocksPort 127.0.0.1:19250\n"
        "SocksPort 127.0.0.1:19251\n"
        "SocksPort 127.0.0.1:19252\n"
        "ControlPort 127.0.0.1:19260\n"
        "# Replace the value with what `tor --hash-password <password>` prints\n"
        "HashedControlPassword 16:PASTE-THE-HASH-HERE\n"
    )
    assert tor["sees"].startswith("Tor's first relay sees your address")
    assert document["tor_line"].startswith("The Tor Project asks")


def test_the_cookie_torrc_names_cookie_authentication() -> None:
    chains, sources, _ = _save(ports=(19250,))

    tor = sources_document(chains, sources)["sources"][0]

    assert tor["torrc"] == (
        "# My Claude Code: one SocksPort per identity, and the control port\n"
        "SocksPort 127.0.0.1:19250\n"
        "ControlPort 127.0.0.1:19260\n"
        "CookieAuthentication 1\n"
    )


def test_a_reading_and_a_new_identity_appear_once_they_happened() -> None:
    chains, sources, source_id = _save()
    TOR_READINGS.record(
        source_id,
        TorReading(
            at="2026-10-09T12:01:00Z",
            ok=True,
            sentence="Tor 0.4.8.13 · circuit established.",
            circuit_established=True,
            socks_listeners=(19250, 19251),
        ),
    )
    assert NEWNYM_GUARD.reserve(CONTROL) == 0
    NEWNYM_GUARD.accepted(CONTROL)

    tor = sources_document(chains, sources)["sources"][0]

    assert tor["status"]["circuit_established"] is True
    assert tor["status"]["socks_listeners"] == [19250, 19251]
    assert tor["newnym"]["wait_seconds"] == 10
    assert tor["newnym"]["at"].endswith("Z")


def test_dropping_a_port_withdraws_its_offer_and_a_chain_keeps_it() -> None:
    chains, sources, source_id = _save()
    middle = tor_offer_id(source_id, 19251)
    chains = ProxyChains(
        proxies=chains.proxies,
        chains={
            "opencode": ProxyChain(
                enabled=True, entries=(ProxyChainEntry(proxy=middle),)
            )
        },
        source_offers=chains.source_offers,
    )

    chains, sources, _ = _save(chains, sources, source_id, ports=(19250,))

    assert chains.source_offers == {source_id: (tor_offer_id(source_id, 19250),)}
    assert middle in chains.proxies, "the chain's address was dropped"
    assert tor_offer_id(source_id, 19252) not in chains.proxies


def test_removing_the_source_withdraws_every_offer_and_its_password() -> None:
    chains, sources, source_id = _save(auth="password", password=PASSWORD)

    chains, sources = remove_source(chains, sources, source_id)

    assert chains.source_offers == {}
    assert chains.proxies == {}
    assert sources.is_empty


def test_an_address_typed_by_hand_for_that_port_is_reused() -> None:
    typed = ProxyChains(
        proxies={
            "px_typed": ProxyEndpoint(url="socks5h://127.0.0.1:19250", label="my tor")
        }
    )

    chains, sources, source_id = _save(typed, ports=(19250, 19251))

    assert _loopback_rows(chains, 19250) == ["px_typed"]
    assert chains.source_offers[source_id][0] == "px_typed"
    assert chains.proxies["px_typed"].label == "my tor"
    source = sources.source(source_id)
    assert source is not None and source.tor is not None
    assert source.tor.socks_ports[0].proxy == "px_typed"


def test_a_login_on_the_same_port_is_another_identity_and_not_reused() -> None:
    typed = ProxyChains(
        proxies={"px_iso": ProxyEndpoint(url="socks5h://iso-1:x@127.0.0.1:19250")},
        chains={
            "opencode": ProxyChain(
                enabled=True, entries=(ProxyChainEntry(proxy="px_iso"),)
            )
        },
    )

    chains, _sources, source_id = _save(typed, ports=(19250,))

    assert sorted(_loopback_rows(chains, 19250)) == sorted(
        ["px_iso", tor_offer_id(source_id, 19250)]
    )


@pytest.mark.parametrize("tor_first", [True, False])
def test_the_scan_and_a_tor_source_share_one_row_per_port(tor_first: bool) -> None:
    """A tor on a port the scan also knocks on (9050 where it can bind)."""

    answers = [ScanAnswer(9050, True, "socks5", "none")]
    chains, sources = ProxyChains(), ProxySources()
    if tor_first:
        chains, sources, _ = _save(chains, sources, ports=(9050,), control=9051)
        chains, sources = apply_local_scan(chains, sources, answers)
    else:
        chains, sources = apply_local_scan(chains, sources, answers)
        chains, sources, _ = _save(chains, sources, ports=(9050,), control=9051)

    rows = _loopback_rows(chains, 9050)
    assert len(rows) == 1, rows
    assert rows[0] in chains.source_offers["src_local"]
    assert rows[0] in chains.source_offers["src_tor"]
    expected = (
        tor_offer_id("src_tor", 9050) if tor_first else local_offer_id(9050, "socks5")
    )
    assert rows[0] == expected


@pytest.mark.asyncio
@pytest.mark.local_serial
async def test_each_tor_port_carries_traffic_through_the_fake_socks5_rig() -> None:
    """Two SOCKS5 listeners standing in for two Tor ports: each offer dials its own."""

    upstream = FakeUpstream(b"through tor")
    await upstream.start()
    exits = [FakeSocks5Server(upstream_port=upstream.port) for _ in range(2)]
    for server in exits:
        await server.start()
    try:
        chains, _sources, source_id = _save(
            ports=tuple(server.port for server in exits), control=closed_control()
        )
        for index, server in enumerate(exits):
            endpoint = chains.proxies[tor_offer_id(source_id, server.port)]
            async with httpx.AsyncClient(proxy=endpoint.url, timeout=10.0) as client:
                response = await client.get(upstream.url)
            assert response.status_code == 200
            assert response.text == "through tor"
            assert [item.accepted for item in exits] == [
                1 if other <= index else 0 for other in range(2)
            ]
    finally:
        for server in exits:
            await server.stop()
        await upstream.stop()


def closed_control() -> int:
    """A control port number nothing uses here: saving never dials it anyway."""

    return closed_port()
