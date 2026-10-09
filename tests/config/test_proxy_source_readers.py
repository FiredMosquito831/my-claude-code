"""7.91.0 (PR-S4 + PR-S5): the readers of what vendors publish.

``tests/fixtures/proxy_sources/nordvpn_servers_socks.json`` is shaped after
NordVPN's public server list (``api.nordvpn.com/v1/servers`` filtered to the
``socks`` technology): an array of server objects with ``hostname``,
``status``, ``locations[].country.code`` / ``.city.name`` and
``technologies[].identifier``, as the spec's investigation read it on
2026-09-25 (71 servers, ``socks-nl1.nordvpn.com`` ...) and the 2026-10-06
re-read confirmed. It is NOT a captured answer: no vendor was contacted for
this release (brief rule), so the addresses are documentation ones
(192.0.2.0/24) and three rows exist only to be refused -- an offline server, a
server without the ``socks`` technology, a duplicate and a broken host name.

``webshare_list.txt`` is the ``ip:port:username:password`` shape Webshare's
list download gives (apidocs.webshare.io/proxy-list/download, 2026-10-06),
with documentation addresses and fake logins.
"""

from pathlib import Path

from my_claude_code.config.proxy_feeds import FEED_MAX_ENDPOINTS
from my_claude_code.config.proxy_source_readers import (
    ListedHost,
    ListRow,
    TypedHost,
    normalise_host,
    parse_host_lines,
    parse_nordvpn_servers,
    parse_proxy_list,
    url_host,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "proxy_sources"


def test_the_nordvpn_list_yields_its_online_socks_servers_once_each() -> None:
    text = (FIXTURES / "nordvpn_servers_socks.json").read_text(encoding="utf-8")

    hosts = parse_nordvpn_servers(text)

    assert hosts == (
        ListedHost("socks-nl1.nordvpn.com", "NL", "Amsterdam"),
        ListedHost("socks-nl2.nordvpn.com", "NL", "Amsterdam"),
        ListedHost("socks-se1.nordvpn.com", "SE", "Stockholm"),
        ListedHost("socks-us1.nordvpn.com", "US", "Chicago"),
    )


def test_the_nordvpn_reader_accepts_the_servers_key_and_refuses_junk() -> None:
    wrapped = '{"servers": [{"hostname": "socks-se9.nordvpn.com"}]}'

    assert parse_nordvpn_servers(wrapped) == (ListedHost("socks-se9.nordvpn.com"),)
    assert parse_nordvpn_servers("<html>not json</html>") == ()
    assert parse_nordvpn_servers('{"error": "rate limited"}') == ()
    assert parse_nordvpn_servers("[1, 2, null]") == ()


def test_a_reader_keeps_at_most_the_catalogue_bound() -> None:
    many = (
        "["
        + ",".join(
            f'{{"hostname": "socks-us{n}.nordvpn.com"}}'
            for n in range(FEED_MAX_ENDPOINTS + 5)
        )
        + "]"
    )

    assert len(parse_nordvpn_servers(many)) == FEED_MAX_ENDPOINTS


def test_the_webshare_list_yields_rows_with_logins() -> None:
    text = (FIXTURES / "webshare_list.txt").read_text(encoding="utf-8")

    rows = parse_proxy_list(text)

    assert rows == (
        ListRow("198.51.100.10", 6540, "wsuser-a1", "wspass-first"),
        ListRow("198.51.100.11", 6541, "wsuser-a1", "wspass-second"),
        ListRow("198.51.100.12", 6542, "wsuser-a1", "wspass-third"),
        # Everything after the third ':' is the password.
        ListRow("198.51.100.14", 6543, "wsuser-a1", "pass:with:colons"),
    )


def test_a_list_authorised_by_source_address_has_no_logins() -> None:
    rows = parse_proxy_list("203.0.113.5:8080\n203.0.113.6:8081\n")

    assert rows == (ListRow("203.0.113.5", 8080), ListRow("203.0.113.6", 8081))


def test_a_password_may_hold_a_hash() -> None:
    assert parse_proxy_list("203.0.113.5:8080:user:p#ss") == (
        ListRow("203.0.113.5", 8080, "user", "p#ss"),
    )


def test_typed_hosts_parse_with_their_own_ports_and_name_the_bad_ones() -> None:
    good, bad = parse_host_lines(
        "nl.socks.nordhold.net\nse.socks.nordhold.net, proxy.torguard.org:1085\n"
        "10.64.0.1  not/ahost  user@evil.example  nl.socks.nordhold.net"
    )

    assert good == (
        TypedHost("nl.socks.nordhold.net"),
        TypedHost("se.socks.nordhold.net"),
        TypedHost("proxy.torguard.org", 1085),
        TypedHost("10.64.0.1"),
    )
    assert bad == ("not/ahost", "user@evil.example")


def test_a_host_never_carries_anything_that_breaks_out_of_a_url() -> None:
    for raw in ("a@b", "a/b", "a b", "[::1]", "::1", "a:1", "", "-bad.example", "123"):
        assert normalise_host(raw) == "", raw
    assert normalise_host("Brd.SuperProxy.IO.") == "brd.superproxy.io"
    assert normalise_host("192.0.2.1") == "192.0.2.1"


def test_a_url_shows_only_its_scheme_and_host() -> None:
    link = "https://proxy.webshare.io/api/v2/proxy/list/download/SECRET-TOKEN/-/any/username/direct/-/"

    assert url_host(link) == "https://proxy.webshare.io"
    assert "SECRET-TOKEN" not in url_host(link)
    assert url_host("not a url") == ""
