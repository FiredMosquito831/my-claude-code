"""7.91.0 (PR-S4 + PR-S5): VPN accounts, commercial gateways, proxy lists.

Each kind turns what the operator confirmed into ordinary offers: unique
names, logins only inside the owner-only URLs, never in a payload. Readers run
on the committed fixtures (see ``tests/config/test_proxy_source_readers.py``);
no vendor is contacted -- the one fetch function is driven through
``httpx.MockTransport``.
"""

import json
import os
import stat
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx
import pytest

from my_claude_code.application.proxy_sources import (
    SourceEditError,
    remove_source,
    sources_document,
)
from my_claude_code.application.proxy_vendor_sources import (
    AccountEdit,
    FetchedText,
    FetchPlan,
    GatewayEdit,
    ListEdit,
    apply_fetched,
    due_source_ids,
    fetch_plan,
    fetch_vendor_text,
    gateway_login,
    mint_sessions,
    record_fetch_failure,
    save_account_source,
    save_gateway_source,
    save_list_source,
    seconds_until_due,
)
from my_claude_code.config.proxy_chains import (
    ProxyChain,
    ProxyChainEntry,
    ProxyChains,
    ProxyEndpoint,
)
from my_claude_code.config.proxy_feeds import FEED_MAX_BYTES
from my_claude_code.config.proxy_presets import (
    BRIGHTDATA_REFUSAL,
    PRESET_ACCOUNT,
    PRESETS,
    VendorPreset,
    preset,
)
from my_claude_code.config.proxy_sources import (
    AccountSettings,
    GatewaySettings,
    HostList,
    ListSettings,
    ProxySource,
    ProxySources,
    SourceSecret,
    load_proxy_sources,
    save_proxy_sources,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "proxy_sources"
NORD_JSON = (FIXTURES / "nordvpn_servers_socks.json").read_text(encoding="utf-8")
WEBSHARE = (FIXTURES / "webshare_list.txt").read_text(encoding="utf-8")
SERVICE_USER = "nord-service-user-9f3k"
SERVICE_PASS = "nord-service-pass-x7Q!"
GATEWAY_PASS = "gw-zone-password-55"
TOKEN = "TOKEN-a9f8e7d6c5b4"
LIST_URL = f"https://proxy.webshare.io/api/v2/proxy/list/download/{TOKEN}/-/any/username/direct/-/"
NORD_URL = (
    "https://api.nordvpn.com/v1/servers"
    "?filters[servers_technologies][identifier]=socks&limit=200"
)
AT = "2026-10-09T12:00:00Z"


def _login(url: str) -> tuple[str, str]:
    parsed = urlsplit(url)
    return unquote(parsed.username or ""), unquote(parsed.password or "")


def _offer_urls(chains: ProxyChains, source_id: str) -> list[str]:
    return [chains.proxies[pid].url for pid in chains.source_offers[source_id]]


def _labels(chains: ProxyChains, source_id: str) -> list[str]:
    return [chains.ledger_label(pid) for pid in chains.source_offers[source_id]]


def _src(sources: ProxySources, source_id: str) -> ProxySource:
    source = sources.source(source_id)
    assert source is not None
    return source


def _acct(sources: ProxySources, source_id: str) -> AccountSettings:
    account = _src(sources, source_id).account
    assert account is not None
    return account


def _hl(sources: ProxySources, source_id: str) -> HostList:
    host_list = _acct(sources, source_id).host_list
    assert host_list is not None
    return host_list


def _gw(sources: ProxySources, source_id: str) -> GatewaySettings:
    gateway = _src(sources, source_id).gateway
    assert gateway is not None
    return gateway


def _lst(sources: ProxySources, source_id: str) -> ListSettings:
    listed = _src(sources, source_id).proxy_list
    assert listed is not None
    return listed


def _secret(sources: ProxySources, secret_id: str) -> SourceSecret:
    secret = sources.secret(secret_id)
    assert secret is not None
    return secret


def _plan(sources: ProxySources, source_id: str) -> FetchPlan:
    plan = fetch_plan(_src(sources, source_id), sources)
    assert plan is not None
    return plan


def _vendor(preset_id: str) -> VendorPreset:
    vendor = preset(preset_id)
    assert vendor is not None
    return vendor


def _nord(**changes: Any) -> AccountEdit:
    base = AccountEdit(
        preset="nordvpn",
        hosts="nl.socks.nordhold.net\nse.socks.nordhold.net",
        username=SERVICE_USER,
        password=SERVICE_PASS,
    )
    return replace(base, **changes)


# ----------------------------------------------------------------- accounts


def test_an_account_offers_each_host_with_its_service_login() -> None:
    chains, sources, source_id = save_account_source(
        ProxyChains(), ProxySources(), "", _nord(), at=AT
    )

    assert source_id == "src_account_nordvpn"
    assert _offer_urls(chains, source_id) == [
        f"socks5h://nord-service-user-9f3k:nord-service-pass-x7Q%21@{host}:1080"
        for host in ("nl.socks.nordhold.net", "se.socks.nordhold.net")
    ]
    assert _labels(chains, source_id) == [
        "NordVPN · nl.socks.nordhold.net",
        "NordVPN · se.socks.nordhold.net",
    ]
    for pid in chains.source_offers[source_id]:
        assert chains.proxies[pid].source == "source"
        assert chains.proxies[pid].source_id == source_id
    secret = _secret(sources, _acct(sources, source_id).secret)
    assert (secret.username, secret.password) == (SERVICE_USER, SERVICE_PASS)


def test_the_account_card_never_carries_the_login() -> None:
    chains, sources, source_id = save_account_source(
        ProxyChains(), ProxySources(), "", _nord(), at=AT
    )

    document = sources_document(chains, sources)
    text = json.dumps(document)

    for secret in (SERVICE_USER, SERVICE_PASS, "nord-service", "x7Q"):
        assert secret not in text
    card = document["sources"][0]
    assert card["secret_set"] is True
    assert card["secret_label"] == "nord…9f3k"
    assert card["doc"] == "https://support.nordvpn.com/hc/en-us/articles/20195967385745"
    assert [row["label"] for row in card["offers"]] == _labels(chains, source_id)
    assert all("url" not in row for row in card["offers"])


@pytest.mark.parametrize(
    "preset_id", [item.id for item in PRESETS if item.kind == PRESET_ACCOUNT]
)
def test_every_account_preset_builds_its_documented_shape(preset_id: str) -> None:
    vendor = _vendor(preset_id)
    username = "" if vendor.in_tunnel else "svc-user"
    password = "" if vendor.in_tunnel else "svc-pass"

    chains, sources, source_id = save_account_source(
        ProxyChains(),
        ProxySources(),
        "",
        AccountEdit(
            preset=preset_id,
            scheme=vendor.scheme,
            port=vendor.port,
            hosts="\n".join(vendor.hosts),
            username=username,
            password=password,
        ),
        at=AT,
    )

    userinfo = "" if vendor.in_tunnel else "svc-user:svc-pass@"
    assert _offer_urls(chains, source_id) == [
        f"{vendor.scheme}://{userinfo}{host}:{vendor.port}" for host in vendor.hosts
    ]
    card = sources_document(chains, sources)["sources"][0]
    assert card["in_tunnel"] is vendor.in_tunnel
    assert ("tunnel" in card) is vendor.in_tunnel
    assert card["observed"] == vendor.observed


def test_one_host_on_two_ports_is_two_addresses() -> None:
    chains, _, source_id = save_account_source(
        ProxyChains(),
        ProxySources(),
        "",
        AccountEdit(
            preset="torguard",
            hosts="proxy.torguard.org proxy.torguard.org:1085",
            username="tg-user",
            password="tg-pass",
        ),
    )

    assert _labels(chains, source_id) == [
        "TorGuard · proxy.torguard.org",
        "TorGuard · proxy.torguard.org:1085",
    ]
    assert [urlsplit(url).port for url in _offer_urls(chains, source_id)] == [
        1080,
        1085,
    ]


def test_mullvad_in_tunnel_offers_say_they_need_the_tunnel() -> None:
    chains, sources, source_id = save_account_source(
        ProxyChains(),
        ProxySources(),
        "",
        AccountEdit(
            preset="mullvad",
            hosts="10.64.0.1\nnl-ams-wg-socks5-001.relays.mullvad.net",
        ),
        at=AT,
    )

    assert _offer_urls(chains, source_id) == [
        "socks5h://10.64.0.1:1080",
        "socks5h://nl-ams-wg-socks5-001.relays.mullvad.net:1080",
    ]
    card = sources_document(chains, sources)["sources"][0]
    assert card["secret_set"] is False
    assert "Mullvad app's tunnel is connected" in card["tunnel"]


def test_nord_https_on_port_89_is_not_offered_by_the_preset() -> None:
    vendor = _vendor("nordvpn")
    assert (vendor.scheme, vendor.port) == ("socks5h", 1080)
    assert "89" in vendor.note and "untested" in vendor.note


def test_bad_hosts_and_an_http_list_url_are_refused() -> None:
    with pytest.raises(SourceEditError, match="not/ahost"):
        save_account_source(
            ProxyChains(), ProxySources(), "", _nord(hosts="ok.example not/ahost")
        )
    with pytest.raises(SourceEditError, match="at least one host"):
        save_account_source(ProxyChains(), ProxySources(), "", _nord(hosts=""))
    with pytest.raises(SourceEditError, match="https://"):
        save_account_source(
            ProxyChains(),
            ProxySources(),
            "",
            _nord(hosts="", list_url="http://api.nordvpn.com/v1/servers"),
        )


def _nord_with_list(
    countries: tuple[str, ...] = (),
) -> tuple[ProxyChains, ProxySources, str]:
    return save_account_source(
        ProxyChains(),
        ProxySources(),
        "",
        _nord(hosts="", list_url=NORD_URL, countries=countries),
        at=AT,
    )


def test_a_saved_server_list_contacts_nothing_and_offers_nothing_yet() -> None:
    chains, sources, source_id = _nord_with_list()

    assert source_id not in chains.source_offers
    plan = _plan(sources, source_id)
    assert (plan.url, plan.reader) == (NORD_URL, "nordvpn_servers")
    card = sources_document(chains, sources)["sources"][0]
    assert card["host_list"]["url_host"] == "https://api.nordvpn.com"
    assert NORD_URL not in json.dumps(card)


def test_a_fetched_server_list_offers_the_confirmed_countries() -> None:
    chains, sources, source_id = _nord_with_list(countries=("NL",))

    chains, sources, outcome = apply_fetched(
        chains, sources, source_id, FetchedText(ok=True, text=NORD_JSON), at=AT
    )

    assert outcome.ok
    assert outcome.sentence == "Fetched: 4 SOCKS5 servers (NL, SE, US); 2 kept for NL."
    assert _labels(chains, source_id) == [
        "NordVPN · socks-nl1.nordvpn.com",
        "NordVPN · socks-nl2.nordvpn.com",
    ]
    host_list = _hl(sources, source_id)
    assert host_list.found == ("NL", "SE", "US")
    assert host_list.fetch.ok and host_list.fetch.fetched_at == AT
    first_ids = list(chains.source_offers[source_id])

    # Widening the countries needs no fetch: every listed host is kept.
    chains, sources, _ = save_account_source(
        chains, sources, source_id, _nord(hosts="", countries=()), at=AT
    )
    assert len(chains.source_offers[source_id]) == 4
    assert list(chains.source_offers[source_id])[:2] == first_ids

    # A second fetch finds the same rows under the same ids.
    again, _, _ = apply_fetched(
        chains, sources, source_id, FetchedText(ok=True, text=NORD_JSON), at=AT
    )
    assert again.source_offers[source_id] == chains.source_offers[source_id]


def test_an_answer_that_does_not_parse_leaves_the_offers() -> None:
    chains, sources, source_id = _nord_with_list()
    chains, sources, _ = apply_fetched(
        chains, sources, source_id, FetchedText(ok=True, text=NORD_JSON), at=AT
    )
    before = chains.source_offers[source_id]

    after, sources, outcome = apply_fetched(
        chains, sources, source_id, FetchedText(ok=True, text="<html>"), at=AT
    )

    assert not outcome.ok
    assert after.source_offers[source_id] == before
    assert "changed shape" in _hl(sources, source_id).fetch.note


def test_a_refused_fetch_is_noted_on_the_source() -> None:
    _, sources, source_id = _nord_with_list()

    sources, outcome = record_fetch_failure(
        sources, source_id, "Not sent: Zen's chain has Direct fallback off.", at=AT
    )

    fetch = _hl(sources, source_id).fetch
    assert (fetch.fetched_at, fetch.ok) == (AT, False)
    assert "Direct fallback off" in fetch.note
    assert not outcome.ok


def test_a_new_login_rewrites_the_urls_in_place() -> None:
    chains, sources, source_id = save_account_source(
        ProxyChains(), ProxySources(), "", _nord(), at=AT
    )
    ids = chains.source_offers[source_id]

    chains, sources, _ = save_account_source(
        chains, sources, source_id, _nord(username="new-user", password="new-pass")
    )

    assert chains.source_offers[source_id] == ids
    assert {_login(url) for url in _offer_urls(chains, source_id)} == {
        ("new-user", "new-pass")
    }
    # An empty user name and password keep the stored login.
    chains, sources, _ = save_account_source(
        chains, sources, source_id, _nord(username="", password="")
    )
    assert {_login(url) for url in _offer_urls(chains, source_id)} == {
        ("new-user", "new-pass")
    }


# ----------------------------------------------------------------- gateways


def _gateway(preset_id: str, **changes: Any) -> GatewayEdit:
    vendor = preset(preset_id)
    assert vendor is not None
    base = GatewayEdit(
        preset=preset_id,
        host=vendor.host,
        port=vendor.port,
        scheme=vendor.scheme,
        user="hl_4c1d9e" if preset_id == "brightdata" else "acme-user",
        password=GATEWAY_PASS,
        minutes=vendor.minutes,
        count=3,
    )
    if preset_id == "brightdata":
        base = replace(base, zone="dc_zone1", zone_type="datacenter")
    return replace(base, **changes)


@pytest.mark.parametrize(
    ("preset_id", "country", "expected_user", "expected_password"),
    [
        (
            "brightdata",
            "us",
            "brd-customer-hl_4c1d9e-zone-dc_zone1-country-us-session-{s}",
            GATEWAY_PASS,
        ),
        (
            "oxylabs",
            "us",
            "customer-acme-user-cc-US-sessid-{s}-sesstime-30",
            GATEWAY_PASS,
        ),
        (
            "iproyal",
            "de",
            "acme-user",
            GATEWAY_PASS + "_country-de_session-{s}_lifetime-30m",
        ),
        (
            "decodo",
            "fr",
            "user-acme-user-country-fr-session-{s}-sessionduration-30",
            GATEWAY_PASS,
        ),
    ],
)
def test_each_gateway_spells_its_vendor_s_session_syntax(
    preset_id: str, country: str, expected_user: str, expected_password: str
) -> None:
    chains, sources, source_id = save_gateway_source(
        ProxyChains(), ProxySources(), "", _gateway(preset_id, country=country), at=AT
    )

    gateway = _gw(sources, source_id)
    vendor = _vendor(preset_id)
    urls = _offer_urls(chains, source_id)
    assert len(urls) == 3
    for session, url in zip(gateway.sessions, urls, strict=True):
        parsed = urlsplit(url)
        assert parsed.scheme == vendor.scheme
        assert (parsed.hostname, parsed.port) == (vendor.host, vendor.port)
        assert _login(url) == (
            expected_user.format(s=session),
            expected_password.format(s=session),
        )
    assert _labels(chains, source_id) == [
        f"{vendor.name} · session {session}" for session in gateway.sessions
    ]


def test_n_sessions_are_n_distinct_addresses_and_names() -> None:
    chains, sources, source_id = save_gateway_source(
        ProxyChains(), ProxySources(), "", _gateway("oxylabs", count=12), at=AT
    )

    sessions = _gw(sources, source_id).sessions
    assert len(set(sessions)) == 12
    assert len(set(_offer_urls(chains, source_id))) == 12
    assert len(set(_labels(chains, source_id))) == 12
    assert len(set(chains.source_offers[source_id])) == 12


def test_renewing_never_reuses_a_session_id() -> None:
    chains, sources, source_id = save_gateway_source(
        ProxyChains(), ProxySources(), "", _gateway("iproyal", count=4), at=AT
    )
    seen = set(_gw(sources, source_id).sessions)
    assert all(len(session) == 8 for session in seen)

    for _ in range(5):
        chains, sources, _ = save_gateway_source(
            chains,
            sources,
            source_id,
            _gateway("iproyal", count=4, user="", password="", renew=True),
        )
        fresh = set(_gw(sources, source_id).sessions)
        assert not fresh & seen
        seen |= fresh
    assert _gw(sources, source_id).minted == 24


def test_changing_n_keeps_the_first_sessions() -> None:
    chains, sources, source_id = save_gateway_source(
        ProxyChains(), ProxySources(), "", _gateway("decodo", count=3), at=AT
    )
    first = _gw(sources, source_id).sessions

    chains, sources, _ = save_gateway_source(
        chains, sources, source_id, _gateway("decodo", count=5, user="", password="")
    )
    grown = _gw(sources, source_id).sessions
    assert grown[:3] == first and len(set(grown)) == 5

    chains, sources, _ = save_gateway_source(
        chains, sources, source_id, _gateway("decodo", count=2, user="", password="")
    )
    assert _gw(sources, source_id).sessions == first[:2]
    assert len(chains.source_offers[source_id]) == 2


def test_the_session_counter_only_goes_up() -> None:
    sessions, last = mint_sessions("oxylabs", 3, 41, ())

    assert last == 44
    assert [session[-4:] for session in sessions] == ["0016", "0017", "0018"]
    assert all(len(session) == 12 for session in sessions)


@pytest.mark.parametrize("zone_type", ["residential", "mobile"])
def test_bright_data_residential_and_mobile_are_refused_with_the_reason(
    zone_type: str,
) -> None:
    with pytest.raises(SourceEditError) as refused:
        save_gateway_source(
            ProxyChains(),
            ProxySources(),
            "",
            _gateway("brightdata", zone_type=zone_type),
        )

    assert str(refused.value) == BRIGHTDATA_REFUSAL
    assert "certificate" in BRIGHTDATA_REFUSAL


def test_bright_data_needs_its_zone_type_said() -> None:
    with pytest.raises(SourceEditError, match="datacenter or ISP"):
        save_gateway_source(
            ProxyChains(), ProxySources(), "", _gateway("brightdata", zone_type="")
        )


def test_a_gateway_needs_its_login_and_a_sane_count() -> None:
    with pytest.raises(SourceEditError, match="user name and password"):
        save_gateway_source(
            ProxyChains(), ProxySources(), "", _gateway("oxylabs", user="", password="")
        )
    with pytest.raises(SourceEditError, match="sessions"):
        save_gateway_source(
            ProxyChains(), ProxySources(), "", _gateway("oxylabs", count=0)
        )
    with pytest.raises(SourceEditError, match="1440"):
        save_gateway_source(
            ProxyChains(), ProxySources(), "", _gateway("oxylabs", minutes=5000)
        )


def test_the_gateway_card_carries_no_password_and_no_session_password() -> None:
    chains, sources, _ = save_gateway_source(
        ProxyChains(), ProxySources(), "", _gateway("iproyal", count=2), at=AT
    )

    text = json.dumps(sources_document(chains, sources))

    assert GATEWAY_PASS not in text
    assert "_session-" not in text
    assert "acme-user" not in text
    card = sources_document(chains, sources)["sources"][0]
    assert card["secret_label"] == "acme…user"
    assert card["count"] == 2


def test_gateway_login_strips_a_pasted_prefix() -> None:
    vendor = _vendor("oxylabs")
    _, sources, source_id = save_gateway_source(
        ProxyChains(), ProxySources(), "", _gateway("oxylabs", count=1), at=AT
    )
    gateway = _gw(sources, source_id)

    assert gateway_login(gateway, "customer-acme", "pw", "abc12345")[0] == (
        f"customer-acme-sessid-abc12345-sesstime-{vendor.minutes}"
    )


# -------------------------------------------------------------------- lists


def test_a_pasted_list_offers_each_row_with_its_own_login() -> None:
    chains, sources, source_id = save_list_source(
        ProxyChains(),
        ProxySources(),
        "",
        ListEdit(preset="webshare", paste=WEBSHARE),
        at=AT,
    )

    assert source_id == "src_list_webshare"
    urls = _offer_urls(chains, source_id)
    assert urls[0] == "socks5h://wsuser-a1:wspass-first@198.51.100.10:6540"
    assert _login(urls[3]) == ("wsuser-a1", "pass:with:colons")
    assert _labels(chains, source_id) == [
        "Webshare · 198.51.100.10:6540",
        "Webshare · 198.51.100.11:6541",
        "Webshare · 198.51.100.12:6542",
        "Webshare · 198.51.100.14:6543",
    ]
    rows = _lst(sources, source_id).rows
    assert all(row.login for row in rows)
    # The rows' logins live only in the chain store's URLs.
    assert "wspass" not in json.dumps(sources.as_document())
    assert "wspass" not in json.dumps(sources_document(chains, sources))


def test_changing_the_scheme_rebuilds_the_urls_from_the_stored_logins() -> None:
    chains, sources, source_id = save_list_source(
        ProxyChains(), ProxySources(), "", ListEdit(preset="webshare", paste=WEBSHARE)
    )
    ids = chains.source_offers[source_id]

    chains, sources, _ = save_list_source(
        chains, sources, source_id, ListEdit(preset="webshare", scheme="http")
    )

    assert chains.source_offers[source_id] == ids
    assert _offer_urls(chains, source_id)[0] == (
        "http://wsuser-a1:wspass-first@198.51.100.10:6540"
    )


def test_a_download_link_is_a_secret_shown_only_by_its_host() -> None:
    chains, sources, source_id = save_list_source(
        ProxyChains(), ProxySources(), "", ListEdit(preset="webshare", url=LIST_URL)
    )

    card = sources_document(chains, sources)["sources"][0]
    assert card["url_set"] is True
    assert card["url_host"] == "https://proxy.webshare.io"
    assert TOKEN not in json.dumps(sources_document(chains, sources))
    stored = _lst(sources, source_id)
    assert TOKEN not in json.dumps(stored.as_document())
    assert _secret(sources, stored.secret).password == LIST_URL
    assert _plan(sources, source_id).url == LIST_URL

    chains, sources, outcome = apply_fetched(
        chains, sources, source_id, FetchedText(ok=True, text=WEBSHARE), at=AT
    )
    assert outcome.ok and outcome.offered == 4
    assert outcome.sentence == "Fetched: 4 proxies."


def test_a_list_needs_rows_or_a_link_and_an_https_link() -> None:
    with pytest.raises(SourceEditError, match="Paste the list"):
        save_list_source(ProxyChains(), ProxySources(), "", ListEdit())
    with pytest.raises(SourceEditError, match="ip:port"):
        save_list_source(ProxyChains(), ProxySources(), "", ListEdit(paste="hello"))
    with pytest.raises(SourceEditError, match="https://"):
        save_list_source(
            ProxyChains(), ProxySources(), "", ListEdit(url="http://example.com/l")
        )


# --------------------------------------------------- names, offers, removal


def test_two_sources_never_share_a_name_or_an_address_name() -> None:
    chains, sources, first = save_list_source(
        ProxyChains(), ProxySources(), "", ListEdit(preset="webshare", paste=WEBSHARE)
    )
    chains, sources, second = save_list_source(
        chains, sources, "", ListEdit(preset="webshare", paste=WEBSHARE)
    )

    assert (first, second) == ("src_list_webshare", "src_list_webshare_2")
    assert _src(sources, second).name == "Webshare 2"
    labels = list(chains.ledger_labels().values())
    assert len(labels) == len(set(labels)) == 8


def test_a_name_an_operator_already_gave_is_stepped_around() -> None:
    taken = ProxyChains(
        proxies={
            "px_mine": ProxyEndpoint(
                url="socks5h://203.0.113.9:1080",
                label="NordVPN · nl.socks.nordhold.net",
            )
        },
        chains={"zen": ProxyChain(entries=(ProxyChainEntry(proxy="px_mine"),))},
    )

    chains, _, source_id = save_account_source(taken, ProxySources(), "", _nord())

    assert _labels(chains, source_id)[0] == "NordVPN · nl.socks.nordhold.net (2)"


def test_offers_survive_prune_and_a_feed_pass_and_removal_keeps_chained_ones() -> None:
    chains, sources, source_id = save_gateway_source(
        ProxyChains(), ProxySources(), "", _gateway("oxylabs", count=3), at=AT
    )
    offered = list(chains.source_offers[source_id])
    chained = offered[0]
    chains = chains.with_chain(
        "zen", ProxyChain(entries=(ProxyChainEntry(proxy=chained),))
    )

    assert set(chains.pruned().proxies) >= set(offered)
    after_feed = chains.with_candidates([])
    assert set(after_feed.proxies) >= set(offered)
    assert after_feed.source_offers[source_id] == tuple(offered)

    removed, sources_after = remove_source(chains, sources, source_id)
    assert source_id not in removed.source_offers
    assert set(removed.proxies) == {chained}
    assert sources_after.secrets == {}


# --------------------------------------------------------------- schedule


def test_no_schedule_is_on_by_default_and_none_is_due() -> None:
    _, sources, _ = _nord_with_list()
    now = datetime(2026, 10, 9, 12, tzinfo=UTC)

    assert seconds_until_due(sources, now) is None
    assert due_source_ids(sources, now) == []


def test_a_switched_on_schedule_is_due_after_its_hours() -> None:
    chains, sources, source_id = save_account_source(
        ProxyChains(),
        ProxySources(),
        "",
        _nord(hosts="", list_url=NORD_URL, refresh_hours=6),
        at=AT,
    )
    now = datetime(2026, 10, 9, 12, tzinfo=UTC)
    # Never fetched: switching a schedule on asks for a fetch.
    assert seconds_until_due(sources, now) == 0.0
    assert due_source_ids(sources, now) == [source_id]

    chains, sources, _ = apply_fetched(
        chains, sources, source_id, FetchedText(ok=True, text=NORD_JSON), at=AT
    )
    assert seconds_until_due(sources, now) == 6 * 3600
    assert due_source_ids(sources, now + timedelta(hours=5)) == []
    assert due_source_ids(sources, now + timedelta(hours=6)) == [source_id]


def test_a_pasted_list_has_nothing_to_schedule() -> None:
    _, sources, _ = save_list_source(
        ProxyChains(),
        ProxySources(),
        "",
        ListEdit(paste=WEBSHARE, refresh_hours=6),
    )

    assert seconds_until_due(sources, datetime.now(UTC)) is None


# ------------------------------------------------------------------ fetch


@pytest.mark.asyncio
async def test_the_fetch_reads_the_confirmed_url_strictly() -> None:
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=WEBSHARE)

    fetched = await fetch_vendor_text(
        LIST_URL, proxy=None, timeout=5, transport=httpx.MockTransport(answer)
    )

    assert fetched.ok and fetched.text == WEBSHARE
    assert [str(request.url) for request in seen] == [LIST_URL]


@pytest.mark.asyncio
async def test_a_redirect_is_not_followed_and_errors_never_quote_the_url() -> None:
    def redirect(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://elsewhere.example/"})

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"could not reach {request.url}")

    moved = await fetch_vendor_text(
        LIST_URL, proxy=None, timeout=5, transport=httpx.MockTransport(redirect)
    )
    failed = await fetch_vendor_text(
        LIST_URL, proxy=None, timeout=5, transport=httpx.MockTransport(broken)
    )

    assert (moved.ok, moved.note) == (False, "answered 302")
    assert (failed.ok, failed.note) == (False, "did not answer (ConnectError)")
    assert TOKEN not in failed.note


@pytest.mark.asyncio
async def test_an_http_url_is_never_fetched_and_a_huge_answer_is_cut() -> None:
    calls: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, content=b"x" * (FEED_MAX_BYTES + 4096))

    plain = await fetch_vendor_text(
        "http://proxy.webshare.io/l",
        proxy=None,
        timeout=5,
        transport=httpx.MockTransport(answer),
    )
    huge = await fetch_vendor_text(
        LIST_URL, proxy=None, timeout=5, transport=httpx.MockTransport(answer)
    )

    assert not plain.ok and len(calls) == 1
    assert len(huge.text) == FEED_MAX_BYTES


# ------------------------------------------------------------------ store


def _all_kinds() -> ProxySources:
    _, sources, _ = save_account_source(
        ProxyChains(), ProxySources(), "", _nord(list_url=NORD_URL, refresh_hours=24)
    )
    _, sources, _ = save_gateway_source(
        ProxyChains(), sources, "", _gateway("brightdata", country="us")
    )
    _, sources, _ = save_list_source(
        ProxyChains(), sources, "", ListEdit(preset="webshare", url=LIST_URL)
    )
    return sources


def test_every_kind_round_trips_byte_for_byte(tmp_path: Path) -> None:
    path = tmp_path / "proxy_sources.json"
    sources = _all_kinds()

    save_proxy_sources(sources, path)
    first = path.read_bytes()
    loaded = load_proxy_sources(path)
    save_proxy_sources(loaded, path)

    assert path.read_bytes() == first
    assert loaded.as_document() == sources.as_document()
    assert {source.kind for source in loaded.sources.values()} == {
        "account",
        "gateway",
        "list",
    }


def test_a_7_90_reader_keeps_these_sources_and_their_logins() -> None:
    """7.89.0 and 7.90.0 keep an unbuilt kind verbatim and honour its
    top-level ``secret``: every vendor kind names its one credential there."""

    document = _all_kinds().as_document()

    for source in document["sources"].values():
        assert source["secret"] in document["secrets"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_the_store_with_every_kind_is_0600_on_posix(tmp_path: Path) -> None:
    old = os.umask(0o022)
    try:
        path = tmp_path / "proxy_sources.json"
        save_proxy_sources(_all_kinds(), path)
    finally:
        os.umask(old)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.skipif(sys.platform != "win32", reason="Windows access lists")
@pytest.mark.spawns_process
@pytest.mark.local_serial
def test_the_store_with_every_kind_is_owner_only_on_windows(tmp_path: Path) -> None:
    path = tmp_path / "proxy_sources.json"
    save_proxy_sources(_all_kinds(), path)

    completed = subprocess.run(
        ["icacls", str(path)], capture_output=True, text=True, check=True
    )
    entries = [
        line.strip()
        for line in completed.stdout.replace(str(path), "").splitlines()
        if ":(" in line
    ]
    assert len(entries) == 3, entries
    assert not any("(I)" in entry for entry in entries), entries
