"""7.108.0 PR-R3/R4/R5: a WireGuard file becomes a runner source and offers.

What is pinned here: the private key lives only in the owner-only sources
store (and the config rendered from it), never in ``proxy_chains.json`` or
the Sources payload; the local login is random per source; ports and the
addresses' URLs and names are stable across edits; Mullvad's in-tunnel exits
are one address each; a WARP account file is read only to match keys.
"""

import json
from pathlib import Path

import pytest

from my_claude_code.application.proxy_runner_sources import (
    SOCKS_KEY,
    RunnerEdit,
    allocate_ports,
    remove_runner_source,
    runner_launch,
    save_runner_source,
    set_runner_enabled,
)
from my_claude_code.application.proxy_sources import SourceEditError, sources_document
from my_claude_code.config.proxy_chains import ProxyChains, ProxyEndpoint
from my_claude_code.config.proxy_sources import (
    ProxySources,
    RunnerSettings,
    SourceSecret,
    load_proxy_sources,
    save_proxy_sources,
    vendor_offer_id,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "wireguard"
PROTON = (FIXTURES / "wg-NL-FREE-12.conf").read_text("utf-8")
MULLVAD = (FIXTURES / "se-sto-wg-001.conf").read_text("utf-8")
WGCF = (FIXTURES / "wgcf-profile.conf").read_text("utf-8")
WGCF_ACCOUNT = (FIXTURES / "wgcf-account.toml").read_text("utf-8")
RANGE = "41000-41099"


def _key(text: str) -> str:
    return next(
        line.split("=", 1)[1].strip()
        for line in text.splitlines()
        if line.startswith("PrivateKey")
    )


def _free(port: int) -> bool:
    return True


def _import(
    text: str = PROTON,
    filename: str = "wg-NL-FREE-12.conf",
    *,
    chains: ProxyChains | None = None,
    sources: ProxySources | None = None,
    exits: str | None = None,
    account_text: str = "",
) -> tuple[ProxyChains, ProxySources, str]:
    return save_runner_source(
        chains or ProxyChains(),
        sources or ProxySources(),
        "",
        RunnerEdit(
            profile_text=text,
            filename=filename,
            exits=exits,
            account_text=account_text,
        ),
        port_range=RANGE,
        port_free=_free,
    )


def _runner(sources: ProxySources, source_id: str) -> RunnerSettings:
    runner = sources.sources[source_id].runner
    assert runner is not None
    return runner


def _login(sources: ProxySources, source_id: str) -> SourceSecret:
    login = sources.secret(_runner(sources, source_id).login)
    assert login is not None
    return login


def _offer(chains: ProxyChains, proxy_id: str) -> ProxyEndpoint:
    offer = chains.endpoint(proxy_id)
    assert offer is not None
    return offer


def test_a_proton_file_offers_one_named_loopback_address() -> None:
    chains, sources, source_id = _import()

    source = sources.sources[source_id]
    assert source_id == "src_runner_proton_nl_free_12"
    assert source.name == "Proton NL-FREE#12"
    assert source.enabled is False
    runner = _runner(sources, source_id)
    assert runner.vendor == "proton"
    assert (runner.socks_port, runner.health_port) == (41000, 41001)
    assert runner.auto_restart is False
    offer = _offer(chains, vendor_offer_id(source_id, SOCKS_KEY))
    login = _login(sources, source_id)
    assert offer.url == f"socks5h://{login.username}:{login.password}@127.0.0.1:41000"
    assert login.username.startswith("mcc-") and len(login.password) == 32
    assert offer.label == "Proton · NL-FREE#12"
    assert chains.source_offers[source_id] == (vendor_offer_id(source_id, SOCKS_KEY),)


def test_the_private_key_is_only_in_the_sources_store(tmp_path: Path) -> None:
    chains, sources, source_id = _import()
    key = _key(PROTON)

    path = tmp_path / "proxy_sources.json"
    save_proxy_sources(sources, path)
    assert key in path.read_text("utf-8")
    assert key not in json.dumps(chains.as_document())
    payload = json.dumps(sources_document(chains, sources))
    login = _login(sources, source_id)
    for secret in (key, key[:8], login.username, login.password):
        assert secret not in payload
    card = sources_document(chains, sources)["sources"][0]
    assert card["profile_set"] is True
    assert card["server"] == "192.0.2.12:51820"
    assert card["status"]["state"] == "stopped"
    assert "one device" in card["caveat"]
    assert any("UNVERIFIED" in line for line in card["lines"])


def test_two_imports_get_two_random_logins_and_unique_names() -> None:
    chains, sources, first = _import()
    chains, sources, second = _import(chains=chains, sources=sources)

    assert second == "src_runner_proton_nl_free_12_2"
    assert sources.sources[second].name == "Proton NL-FREE#12 2"
    assert _login(sources, first) != _login(sources, second)
    labels = {chains.ledger_label(proxy_id) for proxy_id in chains.offered_ids()}
    assert labels == {"Proton · NL-FREE#12", "Proton · NL-FREE#12 (2)"}
    assert set(_runner(sources, second).ports()).isdisjoint(
        _runner(sources, first).ports()
    )


def test_ports_urls_and_names_survive_edits_and_a_new_exit_gets_its_own_port() -> None:
    chains, sources, source_id = _import(MULLVAD, "se-sto-wg-001.conf")
    socks_id = vendor_offer_id(source_id, SOCKS_KEY)
    before = _offer(chains, socks_id)

    chains, sources, _ = save_runner_source(
        chains,
        sources,
        source_id,
        RunnerEdit(
            name="My Mullvad",
            exits=(
                "se-sto-wg-socks5-001.relays.mullvad.net\n"
                "nl-ams-wg-socks5-002.relays.mullvad.net:1080"
            ),
            auto_restart=True,
        ),
        port_range=RANGE,
        port_free=_free,
    )

    runner = _runner(sources, source_id)
    assert sources.sources[source_id].name == "My Mullvad"
    assert (runner.socks_port, runner.health_port) == (41000, 41001)
    assert [(item.host, item.port, item.local_port) for item in runner.exits] == [
        ("se-sto-wg-socks5-001.relays.mullvad.net", 1080, 41002),
        ("nl-ams-wg-socks5-002.relays.mullvad.net", 1080, 41003),
    ]
    assert runner.auto_restart is True
    assert chains.endpoint(socks_id) == before
    exit_id = vendor_offer_id(
        source_id, "exit:se-sto-wg-socks5-001.relays.mullvad.net:1080"
    )
    exit_offer = _offer(chains, exit_id)
    # Forwarded byte for byte to a login-free in-tunnel proxy: no login here.
    assert exit_offer.url == "socks5h://127.0.0.1:41002"
    assert exit_offer.label == "Mullvad · se-sto-wg-socks5-001"
    assert len(chains.source_offers[source_id]) == 3

    # Dropping the first exit keeps the second on its port; a new one is new.
    chains, sources, _ = save_runner_source(
        chains,
        sources,
        source_id,
        RunnerEdit(
            exits=(
                "nl-ams-wg-socks5-002.relays.mullvad.net\n"
                "de-fra-wg-socks5-003.relays.mullvad.net"
            )
        ),
        port_range=RANGE,
        port_free=_free,
    )
    runner = _runner(sources, source_id)
    assert [item.local_port for item in runner.exits] == [41003, 41002]
    assert exit_id not in chains.source_offers[source_id]

    # Pick new ports: every port chosen again (the old ones are held, here).
    held = set(runner.ports())
    chains, sources, _ = save_runner_source(
        chains,
        sources,
        source_id,
        RunnerEdit(repick_ports=True),
        port_range=RANGE,
        port_free=lambda port: port not in held,
    )
    repicked = _runner(sources, source_id)
    assert set(repicked.ports()).isdisjoint(held)
    assert _offer(chains, socks_id).url.endswith(f":{repicked.socks_port}")
    assert _offer(chains, socks_id).label == before.label


def test_ports_come_only_from_the_range_and_must_bind() -> None:
    chosen = allocate_ports(
        3, port_range="41000-41010", taken={41001}, port_free=lambda port: port != 41002
    )
    assert chosen == (41000, 41003, 41004)
    with pytest.raises(SourceEditError, match="Not enough free ports"):
        allocate_ports(3, port_range="41000-41001", taken=(), port_free=_free)


def test_a_store_with_a_runner_round_trips_byte_for_byte(tmp_path: Path) -> None:
    _, sources, _ = _import(
        MULLVAD, "se-sto-wg-001.conf", exits="se-sto-wg-socks5-001.relays.mullvad.net"
    )
    path = tmp_path / "proxy_sources.json"
    save_proxy_sources(sources, path)
    first = path.read_bytes()

    save_proxy_sources(load_proxy_sources(path), path)

    assert path.read_bytes() == first
    document = json.loads(first)
    stored = next(iter(document["sources"].values()))
    assert "auto_restart" not in stored  # off unless ticked
    assert stored["kind"] == "runner" and stored["runner"] == "wireproxy"
    secret_types = sorted(item["type"] for item in document["secrets"].values())
    assert secret_types == ["userpass", "wireguard"]


def test_the_launch_is_regenerated_from_the_store_and_knows_what_to_scrub() -> None:
    _, sources, source_id = _import()

    launch = runner_launch(sources.sources[source_id], sources)

    login = _login(sources, source_id)
    assert _key(PROTON) in launch.config_text
    assert f"Username = {login.username}" in launch.config_text
    assert set(launch.scrub) >= {_key(PROTON), login.username, login.password}
    assert launch.ports == (41000, 41001)
    assert launch.check_alive == "10.2.0.1"


def test_warp_imports_only_and_checks_the_account_key() -> None:
    chains, sources, source_id = _import(
        WGCF, "wgcf-profile.conf", account_text=WGCF_ACCOUNT
    )
    card = sources_document(chains, sources)["sources"][0]
    assert card["vendor"] == "warp"
    assert card["warp"] is True
    assert sources.sources[source_id].name == "WARP wgcf-profile"
    # The account file's token and licence are never stored.
    stored = json.dumps(sources.as_document())
    assert "fixture-access-token" not in stored
    assert "FIXTURE0-LICENSE0" not in stored

    other = WGCF_ACCOUNT.replace(_key(WGCF), "b" * 43 + "=")
    with pytest.raises(SourceEditError, match="two different registrations"):
        _import(WGCF, "wgcf-profile.conf", account_text=other)
    with pytest.raises(SourceEditError, match="never registers a WARP device"):
        save_runner_source(
            ProxyChains(),
            ProxySources(),
            "",
            RunnerEdit(account_text=WGCF_ACCOUNT),
            port_range=RANGE,
            port_free=_free,
        )


def test_a_bad_file_is_a_sentence_and_changes_nothing() -> None:
    with pytest.raises(SourceEditError, match="exactly one server"):
        _import(PROTON.split("[Peer]")[0])
    with pytest.raises(SourceEditError, match="Paste or upload"):
        save_runner_source(
            ProxyChains(),
            ProxySources(),
            "",
            RunnerEdit(),
            port_range=RANGE,
            port_free=_free,
        )


def test_removal_drops_both_secrets_and_the_offers_start_and_stop_record_enabled() -> (
    None
):
    chains, sources, source_id = _import()
    sources = set_runner_enabled(sources, source_id, True)
    assert sources.sources[source_id].enabled is True

    chains, sources = remove_runner_source(chains, sources, source_id)

    assert sources.sources == {} and sources.secrets == {}
    assert source_id not in chains.source_offers
    assert chains.proxies == {}
