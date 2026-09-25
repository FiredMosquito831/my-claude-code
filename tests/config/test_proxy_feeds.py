"""The readers, and the real bodies they were written against.

Every fixture in this file is a trimmed copy of what one real public proxy
list actually returned on 2026-09-15, field names and all. That is the point:
a parser written against a README is a parser that finds out it was wrong in a
bug report, and each of these bodies was fetched before a line of the reader
that reads it was written.

MCC ships no list of anybody's endpoints -- the URLs those bodies came from are
not in the product -- so what is pinned here is the *format* side: that every
reader the picker offers exists, that each still reads the shape it was written
for, and that a body of the wrong shape yields nothing rather than an
exception.
"""

import json

import pytest

from my_claude_code.config import proxy_feeds
from my_claude_code.config.proxy_feeds import (
    FEED_NAME_MAX_LENGTH,
    FEED_PROTOCOLS,
    PARSER_IDS,
    PARSERS,
    CustomFeed,
    FeedEndpoint,
    ProxyFeed,
    detect_parser,
    is_valid_feed_url,
    normalise_parser,
    probe_feed,
    proposed_parser,
)

PROXYSCRAPE = json.dumps(
    {
        "shown_records": 3,
        "proxies": [
            {
                "alive": True,
                "anonymity": "elite",
                "ip": "45.86.229.39",
                "port": 11000,
                "protocol": "socks5",
                "ssl": True,
                "timeout": 261.4,
                "uptime": 13.79,
                "ip_data": {"countryCode": "ES", "as": "AS62005 BlueVPS OU"},
            },
            # socks4 is dropped: httpx cannot dial it, so an address in that
            # scheme could only ever be a candidate nobody can use.
            {
                "alive": True,
                "anonymity": "elite",
                "ip": "202.179.76.105",
                "port": 51951,
                "protocol": "socks4",
                "ssl": True,
                "ip_data": {"countryCode": "IN"},
            },
            # A dead row is dropped even though the feed still lists it.
            {
                "alive": False,
                "ip": "1.2.3.4",
                "port": 8080,
                "protocol": "http",
                "ip_data": {},
            },
        ],
    }
)

HPROXY = json.dumps(
    [
        {
            "proxy": "135.125.232.151:1080",
            "ip": "135.125.232.151",
            "port": 1080,
            "protocols": ["socks5"],
            "anonymity": "elite",
            "country": "FR",
            "city": None,
            "latency_ms": 34,
            "uptime_pct": 99.2,
            "alive": True,
        }
    ]
)

DATABAY = json.dumps(
    {
        "data": [
            {
                "ip": "146.19.49.195",
                "port": 1080,
                "country": "United States",
                "iso": "US",
                "protocol": "Socks5",
                "ssl": True,
                "anonymity": "elite",
                "google": False,
                "latency": 1605,
                "uptime": 88.6,
                "lastChecked": "2026-09-15T19:26:44Z",
            }
        ]
    }
)

PROXIFLY = json.dumps(
    [
        {
            "proxy": "socks5://208.102.51.6:58208",
            "protocol": "socks5",
            "ip": "208.102.51.6",
            "port": 58208,
            "https": False,
            "anonymity": "transparent",
            "score": 1,
            "geolocation": {"country": "US", "city": "Alexandria"},
        }
    ]
)

MONOSANS = json.dumps(
    [
        {
            "protocol": "http",
            "username": None,
            "password": None,
            "host": "5.129.254.51",
            "port": 8888,
            "timeout": 0.17,
            "exit_ip": "5.129.254.51",
            "asn": {
                "autonomous_system_number": 9123,
                "autonomous_system_organization": "Jsc timeweb",
            },
            "geolocation": {"country": {"names": {"en": "Russia"}}},
        }
    ]
)

GEONODE = json.dumps(
    {
        "data": [
            {
                "ip": "112.5.173.148",
                "anonymityLevel": "elite",
                "asn": "AS9808",
                "country": "CN",
                "lastChecked": 1789499911,
                "latency": 278.4,
                "port": "63180",
                "protocols": ["socks5"],
                "upTime": 94.5,
            }
        ]
    }
)

VPSLAB = (
    "# Updated Proxies: 2026-09-15 19:23 UTC\n"
    "# Protocol: http | SSL: yes | Anonymity: elite\n"
    "\n"
    "8.215.25.3:2080\n"
    "77.239.123.239:3128\n"
    "not-an-address\n"
)


def feed_for(parser_id: str) -> ProxyFeed:
    """A feed an operator could have added, pointed at ``parser_id``.

    The catalogue is gone, so a test that wants to read a body has to say what
    a stored custom feed would have said. The ``lines`` reader gets the three
    assumptions the plain-text list it was written against carried in its own
    filename -- ``http``, SSL yes, elite -- because a bare ``ip:port`` body can
    never state them and without them that reader parses to nothing.
    """

    if parser_id == "lines":
        return ProxyFeed(
            id="fd_lines",
            name="A plain-text list",
            url="https://example.invalid/http.txt",
            parser="lines",
            homepage="",
            assume_protocol="http",
            assume_https_ok=True,
            assume_anonymity="elite",
        )
    return ProxyFeed(
        id=f"fd_{parser_id}",
        name=f"A {parser_id} list",
        url=f"https://example.invalid/{parser_id}.json",
        parser=parser_id,
        homepage="",
    )


def parse(parser_id: str, body: str) -> tuple[FeedEndpoint, ...]:
    return feed_for(parser_id).parse(body)


def test_every_reader_the_picker_offers_is_a_reader_that_exists():
    """The both-ways pin: a reader cannot be in one table and not the other.

    :data:`PARSERS` is what the page offers and ``_PARSERS`` is what actually
    reads a body. A reader in the second and not the first is unreachable; one
    in the first and not the second is offered and then silently yields
    nothing, which reads on the page as "this feed is down" forever.
    """

    assert len(PARSER_IDS) == 7
    assert len(set(PARSER_IDS)) == 7
    for parser in PARSERS:
        assert parser.label, parser.id
        assert parser.shape, parser.id
    assert set(PARSER_IDS) == set(proxy_feeds._PARSERS)


@pytest.mark.parametrize(
    ("parser_id", "body", "url", "country", "anonymity", "https_ok"),
    [
        (
            "proxyscrape",
            PROXYSCRAPE,
            "socks5h://45.86.229.39:11000",
            "ES",
            "elite",
            True,
        ),
        ("hproxy", HPROXY, "socks5h://135.125.232.151:1080", "FR", "elite", True),
        ("databay", DATABAY, "socks5h://146.19.49.195:1080", "US", "elite", True),
        (
            "proxifly",
            PROXIFLY,
            "socks5h://208.102.51.6:58208",
            "US",
            "transparent",
            False,
        ),
        ("monosans", MONOSANS, "http://5.129.254.51:8888", "Russia", "", False),
        ("geonode", GEONODE, "socks5h://112.5.173.148:63180", "CN", "elite", True),
        ("lines", VPSLAB, "http://8.215.25.3:2080", "", "elite", True),
    ],
)
def test_each_reader_reads_the_real_body_it_was_written_against(
    parser_id, body, url, country, anonymity, https_ok
):
    found = parse(parser_id, body)
    assert found, parser_id
    first = found[0]
    assert first.url == url
    assert first.country == country
    assert first.anonymity == anonymity
    assert first.https_ok is https_ok
    assert first.protocol in FEED_PROTOCOLS


def test_socks4_and_dead_rows_are_dropped():
    """Both appear in ProxyScrape's real answer and neither is usable."""

    found = parse("proxyscrape", PROXYSCRAPE)
    assert [endpoint.ip for endpoint in found] == ["45.86.229.39"]


def test_a_line_feed_takes_its_scheme_from_the_file_it_came_from():
    """A bare ``ip:port`` cannot say how to dial it, so the feed says.

    Guessing between http and socks5 is how a chain ends up unable to connect
    while every row on the page looks fine.
    """

    found = parse("lines", VPSLAB)
    assert [endpoint.url for endpoint in found] == [
        "http://8.215.25.3:2080",
        "http://77.239.123.239:3128",
    ]


def test_a_feed_that_changed_shape_yields_nothing_rather_than_raising():
    """One feed's bad morning must not cost the pass the other feeds.

    Every reader, against the three bodies a URL that stopped being a proxy
    list actually returns: an error page, a JSON document of the wrong shape,
    and a bare ``null``.
    """

    assert PARSER_IDS
    for parser_id in PARSER_IDS:
        feed = probe_feed(parser_id)
        assert feed.parse("<html>502 Bad Gateway</html>") == (), parser_id
        assert feed.parse('{"data": {"unexpected": true}}') == (), parser_id
        assert feed.parse("null") == (), parser_id


def test_a_monosans_timeout_in_seconds_becomes_milliseconds():
    """Measured: this feed publishes 0.17 for a proxy answering in 170 ms."""

    assert parse("monosans", MONOSANS)[0].latency_ms == 170


def test_a_format_this_install_cannot_read_normalises_to_nothing():
    """A store written by a later release must still load in an earlier one.

    Empty rather than a guessed default: a feed whose reader was retired shows
    up on the page asking for a format, where a guess would make it look like a
    feed whose publisher went quiet -- and those want different answers.
    """

    assert normalise_parser("databay") == "databay"
    assert normalise_parser("  GEONODE  ") == "geonode"
    assert normalise_parser("a-reader-that-retired") == ""
    assert normalise_parser(None) == ""
    assert normalise_parser("") == ""


def test_a_feed_url_is_read_over_https_or_not_at_all():
    """The list decides which strangers end up in front of a credential.

    Reading it over plain http would let anyone on the path choose that, which
    is a downgrade with no case for it.
    """

    assert is_valid_feed_url("https://example.com/proxies.json") is True
    assert is_valid_feed_url("http://example.com/proxies.json") is False
    assert is_valid_feed_url("ftp://example.com/proxies.json") is False
    assert is_valid_feed_url("not a url") is False
    assert is_valid_feed_url("") is False


def test_detection_proposes_the_reader_that_made_most_of_the_body():
    """The Detect button's whole job, against two real bodies."""

    assert proposed_parser(detect_parser(GEONODE)) == "geonode"
    assert proposed_parser(detect_parser(VPSLAB)) == "lines"


def test_a_body_no_reader_recognises_proposes_nothing_and_still_reports():
    """ "Nothing here reads this" is an answer, not a failure.

    The feed is still addable -- a URL that answers with an error page today
    may be a proxy list again tomorrow -- so the page needs every trial back,
    with its zero, rather than an exception.
    """

    trials = detect_parser("<html>not json</html>")
    assert proposed_parser(trials) == ""
    assert len(trials) == len(PARSER_IDS)
    assert {trial.parser for trial in trials} == set(PARSER_IDS)
    assert all(trial.count == 0 for trial in trials)
    assert not any(trial.plausible for trial in trials)


def test_detection_always_reports_every_reader_it_tried():
    """The picker is shown either way, so it needs the whole list either way."""

    for body in (GEONODE, VPSLAB, "", "null"):
        trials = detect_parser(body)
        assert len(trials) == len(PARSER_IDS)
        assert {trial.parser for trial in trials} == set(PARSER_IDS)


def _stored(**rest) -> CustomFeed:
    fields = {
        "id": "fd_abcd1234",
        "name": "My list",
        "url": "https://example.com/proxies.json",
        "parser": "databay",
        "enabled": True,
        "added_at": "2026-09-15T19:26:44Z",
        "tls_strict": True,
        "assume_protocol": "http",
        "assume_https_ok": True,
        "assume_anonymity": "elite",
        "observed": "A trial read found 40 addresses.",
    }
    fields.update(rest)
    return CustomFeed(**fields)


def test_a_stored_feed_survives_a_round_trip_through_the_file_whole():
    """Every field, not just the four on the form.

    The ``assume_*`` three are set by detection and by the 7.18.0 migration and
    never by the page, so a round trip that quietly dropped them would turn a
    working plain-text feed into one that fetches fine and offers nothing.
    """

    feed = _stored()
    again = CustomFeed.from_document(feed.as_document(), "feeds[0]")
    assert again == feed


def test_a_stored_feed_without_an_https_url_is_dropped_on_the_way_in():
    """A feed with nowhere safe to fetch from is not a feed."""

    raw = _stored().as_document()
    raw["url"] = "http://example.com/proxies.json"
    assert CustomFeed.from_document(raw, "feeds[0]") is None


def test_a_stored_feed_with_a_retired_reader_is_kept_and_blanked():
    """The row stays and asks for a format; it does not disappear.

    A switch that silently vanished would be the product deciding an operator's
    feed no longer exists, when what actually happened is that this install
    stopped shipping the reader for it.
    """

    raw = _stored().as_document()
    raw["parser"] = "a-reader-that-retired"
    again = CustomFeed.from_document(raw, "feeds[0]")
    assert again is not None
    assert again.parser == ""
    assert again.readable is False
    assert again.url == "https://example.com/proxies.json"


def test_a_stored_feed_name_cannot_push_the_rest_of_its_row_off_the_page():
    raw = _stored().as_document()
    raw["name"] = "n" * 200
    again = CustomFeed.from_document(raw, "feeds[0]")
    assert again is not None
    assert again.name == "n" * FEED_NAME_MAX_LENGTH


def test_a_stored_feed_with_no_name_is_called_by_its_url():
    """Better a URL on the row than a blank cell nobody can identify."""

    raw = _stored().as_document()
    raw["name"] = ""
    again = CustomFeed.from_document(raw, "feeds[0]")
    assert again is not None
    assert again.name == "https://example.com/proxies.json"


# ------------------------- 7.52.3: a feed's "https" label is an HTTP proxy


def test_https_protocol_is_dialled_as_http():
    """Feeds label a CONNECT proxy that can tunnel HTTPS as ``https``.

    Dialled as a TLS-wrapped proxy none of 160 such addresses passed; dialled
    as plain ``http://`` 23 did. What the feed said about HTTPS is kept.
    """

    labelled = FeedEndpoint(
        protocol="https", ip="203.0.113.7", port=8080, https_ok=True
    )
    assert labelled.url == "http://203.0.113.7:8080"
    assert labelled.protocol == "https"
    assert labelled.https_ok is True

    # Every reader that turns a published protocol field into an endpoint.
    found = parse(
        "proxifly",
        json.dumps(
            [
                {
                    "proxy": "https://203.0.113.8:3128",
                    "protocol": "https",
                    "ip": "203.0.113.8",
                    "port": 3128,
                    "https": True,
                    "anonymity": "elite",
                    "score": 1,
                    "geolocation": {"country": "ZZ", "city": "Unknown"},
                }
            ]
        ),
    )
    assert [endpoint.url for endpoint in found] == ["http://203.0.113.8:3128"]

    # A line feed whose *file* says https is a label too.
    label_feed = ProxyFeed(
        id="fd_https_lines",
        name="An https list",
        url="https://example.invalid/https.txt",
        parser="lines",
        homepage="",
        assume_protocol="https",
    )
    assert [endpoint.url for endpoint in label_feed.parse("203.0.113.9:443\n")] == [
        "http://203.0.113.9:443"
    ]


def test_explicit_https_scheme_in_a_line_is_kept():
    """``https://`` written in front of the address is kept as written."""

    feed = feed_for("lines")
    found = feed.parse("https://203.0.113.10:8443\n203.0.113.11:8080\n")
    assert [endpoint.url for endpoint in found] == [
        "https://203.0.113.10:8443",
        "http://203.0.113.11:8080",
    ]


def test_the_other_schemes_are_dialled_exactly_as_before():
    assert FeedEndpoint(protocol="http", ip="203.0.113.12", port=80).url == (
        "http://203.0.113.12:80"
    )
    assert FeedEndpoint(protocol="socks5", ip="203.0.113.13", port=1080).url == (
        "socks5h://203.0.113.13:1080"
    )
    written = feed_for("lines").parse("socks5://203.0.113.14:1080\n")
    assert [endpoint.url for endpoint in written] == ["socks5h://203.0.113.14:1080"]
