"""The bundled feed catalogue, and the parsers built against real answers.

Every fixture in this file is a trimmed copy of what that feed actually
returned on 2026-09-15, field names and all. That is the point: a parser
written against a README is a parser that finds out it was wrong in a bug
report, and these seven were each fetched before a line of them was written.
"""

import json

import pytest

from my_claude_code.config.proxy_feeds import (
    CATALOGUE,
    FEED_PROTOCOLS,
    FEEDS_BY_ID,
    FeedEndpoint,
    known_feed_ids,
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


def parse(feed_id: str, body: str) -> tuple[FeedEndpoint, ...]:
    return FEEDS_BY_ID[feed_id].parse(body)


def test_every_bundled_feed_has_an_https_url_and_a_working_parser():
    """Nothing ships that was not fetched and read."""

    assert len(CATALOGUE) == 7
    assert len({feed.id for feed in CATALOGUE}) == 7
    for feed in CATALOGUE:
        assert feed.url.startswith("https://"), feed.id
        assert feed.homepage.startswith("https://"), feed.id
        assert feed.observed, f"{feed.id} must say what it actually returned"
        # A parser that is not in the table would silently yield nothing,
        # which reads on the page as "this feed is down" forever.
        assert feed.parse("") == ()


def test_exactly_one_feed_claims_a_tls_strict_filter():
    """Databay, and only Databay -- verified by fetching it both ways.

    The claim is load-bearing: it is the one feed-side filter that selects for
    the property this product needs, and a second feed quietly acquiring the
    badge without anybody checking would be a claim about somebody else's
    server that nobody measured.
    """

    strict = [feed.id for feed in CATALOGUE if feed.tls_strict]
    assert strict == ["databay"]
    assert "ssl=strict" in FEEDS_BY_ID["databay"].url


@pytest.mark.parametrize(
    ("feed_id", "body", "url", "country", "anonymity", "https_ok"),
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
        ("vpslab", VPSLAB, "http://8.215.25.3:2080", "", "elite", True),
    ],
)
def test_each_parser_reads_its_own_feeds_real_answer(
    feed_id, body, url, country, anonymity, https_ok
):
    found = parse(feed_id, body)
    assert found, feed_id
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

    found = parse("vpslab", VPSLAB)
    assert [endpoint.url for endpoint in found] == [
        "http://8.215.25.3:2080",
        "http://77.239.123.239:3128",
    ]


def test_a_feed_that_changed_shape_yields_nothing_rather_than_raising():
    """Six working feeds must not be lost to a seventh's bad morning."""

    for feed in CATALOGUE:
        assert feed.parse("<html>502 Bad Gateway</html>") == ()
        assert feed.parse('{"data": {"unexpected": true}}') == ()
        assert feed.parse("null") == ()


def test_a_monosans_timeout_in_seconds_becomes_milliseconds():
    """Measured: this feed publishes 0.17 for a proxy answering in 170 ms."""

    assert parse("monosans", MONOSANS)[0].latency_ms == 170


def test_an_unknown_feed_name_is_dropped_not_raised():
    """A store written by a later release must still load in an earlier one."""

    assert known_feed_ids(["databay", "a-feed-that-retired"]) == ("databay",)
    assert known_feed_ids("databay") == ()
    assert known_feed_ids(None) == ()
    # Catalogue order, not the caller's.
    assert known_feed_ids(["geonode", "proxyscrape"]) == ("proxyscrape", "geonode")
