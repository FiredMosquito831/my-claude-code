"""The named public proxy feeds this install knows how to read.

Seven sources, each one **fetched once by hand before it was written down**, on
2026-09-15, and built against what it actually returned rather than against
what its documentation claims. Where a documented capability turned out not to
exist it was dropped rather than coded around, and the note on each entry says
what was seen. A bundled feed that 404s is worse than one that was never
shipped.

**Nothing here is fetched unless the operator asks.** No feed is enabled on a
fresh install, the scheduled refresh is off, and an operator who never opens
the Proxying page makes no outbound request they did not choose. Turning a feed
on is a list of addresses arriving in a **candidate** list -- it is never a
chain member, and it never carries a credential until the operator puts it in a
chain and the checker has verified its tunnel.

**What was verified, per feed** (2026-09-15, plain ``curl``):

======================  =====================================================
ProxyScrape             ``api.proxyscrape.com/v4`` answered 200 with
                        ``{"proxies": [...]}``; each row carries ``ip``,
                        ``port``, ``protocol``, ``anonymity``, ``ssl``,
                        ``uptime``, ``timeout`` and an ``ip_data`` object with
                        ``countryCode`` and ``as``. ``limit=`` and
                        ``protocol=`` both narrow the result -- measured:
                        2564 records unfiltered, 1569 for ``protocol=socks5``.
HProxy                  The **GitHub repository** answered 200 for
                        ``live.json``: an array of ``{proxy, ip, port,
                        protocols[], anonymity, country, latency_ms,
                        uptime_pct, alive}``. Its documented keyless API host
                        ``api.hproxy.com`` **does not resolve** -- that claim
                        is dropped, and this entry reads the repository only.
Databay                 ``databay.com/api/v1/proxy-list`` answered 200 with
                        ``{"data": [...]}`` and its ``ssl=strict`` filter is
                        **real**: without it rows come back carrying
                        ``"ssl": false``, with it every row is ``"ssl": true``.
                        That is the one feed-side filter that selects for the
                        property this product needs, so it is in the URL. The
                        repository's documented ``https.txt`` is **not**
                        there (404); ``http.txt``/``socks5.txt`` are.
Proxifly                The jsDelivr mirror answered 200 with an array of
                        ``{proxy, protocol, ip, port, https, anonymity,
                        score, geolocation{country, city}}``.
VPSLab                  Raw GitHub text, ``ip:port`` per line with a ``#``
                        header naming the protocol, SSL and anonymity of the
                        file. ``http_ssl_elite.txt`` is read because the file
                        *is* the filter: its protocol is known from its name,
                        which a bare ``ip:port`` list can never say.
monosans                Raw GitHub ``proxies.json``: an array of
                        ``{protocol, host, port, timeout, exit_ip,
                        asn{...}, geolocation{country{names{en}}}}``.
Geonode                 ``proxylist.geonode.com/api/proxy-list`` answered 200
                        with ``{"data": [...]}``; rows carry ``protocols[]``,
                        ``anonymityLevel``, ``country``, ``asn``, ``latency``,
                        ``upTime`` and ``lastChecked``. ``limit`` and
                        ``protocols`` narrow it.
======================  =====================================================

**What is not claimed.** Nothing here has been checked for freshness beyond the
one fetch, no refresh interval in anybody's README was measured, and no
endpoint any of these feeds lists has been dialled by this module. The feeds
say an address is alive; the checker in :mod:`~my_claude_code.application.
proxy_check` is the only thing in this product that finds out.

``socks4`` rows are dropped on the way in. ``httpx[socks]`` dials ``http``,
``https``, ``socks5`` and ``socks5h`` and nothing else
(:data:`~my_claude_code.config.proxy_chains.PROXY_URL_SCHEMES`), so a socks4
address in the candidate list would be one that can never be added to a chain.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

#: The day every URL below was fetched and every shape read off the answer.
FEEDS_OBSERVED_ON = "2026-09-15"

#: The most bytes one feed may return. A response larger than this is truncated
#: and what parsed out of the truncated text is kept, because a feed that grew
#: a megabyte overnight is a feed to read less of, not one to stop reading. The
#: largest of the seven measured 1.8 MB.
FEED_MAX_BYTES = 6 * 1024 * 1024

#: The most addresses taken from any one feed in one pass. A feed listing six
#: thousand endpoints is not offering six thousand useful ones, and the store
#: is a catalogue an operator reads, not a database.
FEED_MAX_ENDPOINTS = 300

#: Schemes that survive normalisation. ``socks4`` is deliberately absent.
FEED_PROTOCOLS: frozenset[str] = frozenset({"http", "https", "socks5"})


@dataclass(frozen=True, slots=True)
class FeedEndpoint:
    """One address as a feed described it, normalised to the §6.2 field list.

    Everything but ``ip`` and ``port`` is what that feed happened to publish;
    a feed that says nothing about anonymity leaves it empty rather than
    guessing ``elite``, because the guess would be indistinguishable from an
    observation on the page that renders it.
    """

    protocol: str
    ip: str
    port: int
    country: str = ""
    anonymity: str = ""
    https_ok: bool = False
    latency_ms: int | None = None
    uptime_pct: float | None = None
    last_checked: str = ""
    asn: str = ""

    @property
    def address(self) -> str:
        """``ip:port`` -- the key two feeds have to agree on to be one entry."""

        return f"{self.ip}:{self.port}"

    @property
    def url(self) -> str:
        """The dialable URL for this address.

        ``socks5`` becomes ``socks5h``: resolving the destination's hostname at
        the proxy rather than here is what a SOCKS proxy is for, and the ``h``
        form is the one that does it.
        """

        scheme = "socks5h" if self.protocol == "socks5" else self.protocol
        return f"{scheme}://{self.address}"


@dataclass(frozen=True, slots=True)
class ProxyFeed:
    """One named source, its URL, and what fetching it actually returned."""

    id: str
    name: str
    url: str
    parser: str
    homepage: str
    #: Whether this feed's own URL selects for a tunnel that leaves the
    #: destination's certificate verifiable. Exactly one of the seven offers
    #: that, and it is in that feed's URL rather than in a comment.
    tls_strict: bool = False
    #: For a feed whose rows are bare ``ip:port``: what its own filename says
    #: about them. A plain list can never carry a scheme, and guessing between
    #: ``http`` and ``socks5`` is how a chain ends up unable to connect.
    assume_protocol: str = ""
    assume_https_ok: bool = False
    assume_anonymity: str = ""
    #: What the fetch on :data:`FEEDS_OBSERVED_ON` returned, in one sentence,
    #: rendered on the page beside the switch so the operator can see what they
    #: are turning on without leaving it.
    observed: str = ""

    def parse(self, text: str) -> tuple[FeedEndpoint, ...]:
        """Read this feed's body into normalised addresses.

        Never raises. A feed that changed shape, went to an error page or came
        back truncated yields nothing, and the caller reports "0 addresses"
        rather than failing the whole pass: six working feeds should not be
        lost to a seventh's bad morning.
        """

        parser = _PARSERS.get(self.parser)
        if parser is None:  # pragma: no cover - unreachable via CATALOGUE
            return ()
        try:
            found = parser(text, self)
        except Exception:
            return ()
        return tuple(found[:FEED_MAX_ENDPOINTS])


# --------------------------------------------------------------- primitives


def _port(value: object) -> int:
    try:
        port = int(str(value).strip())
    except TypeError, ValueError:
        return 0
    return port if 1 <= port <= 65535 else 0


def _ip(value: object) -> str:
    text = str(value or "").strip()
    # A host, not a URL and not a range. Feeds have shipped both.
    if not text or "/" in text or " " in text:
        return ""
    return text


def _protocol(value: object) -> str:
    text = str(value or "").strip().lower()
    if text in {"socks5h", "socks5"}:
        return "socks5"
    return text if text in FEED_PROTOCOLS else ""


def _first_protocol(values: object) -> str:
    """The best protocol from a feed that publishes a list of them.

    ``socks5`` first when it is offered: it tunnels anything, where an
    ``http`` proxy's CONNECT support is the thing that varies most between
    free endpoints.
    """

    if isinstance(values, str):
        return _protocol(values)
    if not isinstance(values, Sequence):
        return ""
    found = [_protocol(value) for value in values]
    for wanted in ("socks5", "https", "http"):
        if wanted in found:
            return wanted
    return ""


def _anonymity(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if text in {"elite", "anonymous", "transparent"} else ""


def _number(value: object) -> float | None:
    """A number out of whatever a feed put in the field, or ``None``.

    Feeds publish these as ints, floats and strings, and one of the seven
    publishes ``null`` for an address it has not timed yet.
    """

    if not isinstance(value, int | float | str) or isinstance(value, bool):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _latency(value: object) -> int | None:
    millis = _number(value)
    if millis is None:
        return None
    return int(millis) if 0 <= millis < 600_000 else None


def _uptime(value: object) -> float | None:
    percent = _number(value)
    if percent is None:
        return None
    return round(percent, 1) if 0 <= percent <= 100 else None


def _rows(text: str, key: str = "") -> list[Mapping[str, Any]]:
    """The list of objects in a JSON body, whether bare or under one key."""

    document = json.loads(text)
    if key and isinstance(document, Mapping):
        document = document.get(key)
    if not isinstance(document, Sequence) or isinstance(document, str):
        return []
    return [row for row in document if isinstance(row, Mapping)]


def _make(protocol: str, ip: str, port: int, **rest: Any) -> FeedEndpoint | None:
    if not protocol or not ip or not port:
        return None
    return FeedEndpoint(protocol=protocol, ip=ip, port=port, **rest)


def _keep(found: list[FeedEndpoint], made: FeedEndpoint | None) -> None:
    if made is not None:
        found.append(made)


# ------------------------------------------------------------------ parsers


def _parse_proxyscrape(text: str, feed: ProxyFeed) -> list[FeedEndpoint]:
    found: list[FeedEndpoint] = []
    for row in _rows(text, "proxies"):
        if row.get("alive") is False:
            continue
        data = row.get("ip_data")
        data = data if isinstance(data, Mapping) else {}
        _keep(
            found,
            _make(
                _protocol(row.get("protocol")),
                _ip(row.get("ip")),
                _port(row.get("port")),
                country=str(data.get("countryCode") or "").strip().upper(),
                anonymity=_anonymity(row.get("anonymity")),
                https_ok=bool(row.get("ssl")),
                latency_ms=_latency(row.get("timeout")),
                uptime_pct=_uptime(row.get("uptime")),
                asn=str(data.get("as") or "").strip(),
            ),
        )
    return found


def _parse_hproxy(text: str, feed: ProxyFeed) -> list[FeedEndpoint]:
    found: list[FeedEndpoint] = []
    for row in _rows(text):
        if row.get("alive") is False:
            continue
        protocol = _first_protocol(row.get("protocols"))
        _keep(
            found,
            _make(
                protocol,
                _ip(row.get("ip")),
                _port(row.get("port")),
                country=str(row.get("country") or "").strip().upper(),
                anonymity=_anonymity(row.get("anonymity")),
                https_ok=protocol in {"https", "socks5"},
                latency_ms=_latency(row.get("latency_ms")),
                uptime_pct=_uptime(row.get("uptime_pct")),
            ),
        )
    return found


def _parse_databay(text: str, feed: ProxyFeed) -> list[FeedEndpoint]:
    found: list[FeedEndpoint] = []
    for row in _rows(text, "data"):
        _keep(
            found,
            _make(
                _protocol(row.get("protocol")),
                _ip(row.get("ip")),
                _port(row.get("port")),
                country=str(row.get("iso") or "").strip().upper(),
                anonymity=_anonymity(row.get("anonymity")),
                # Only ever true on this feed, because the URL asks for
                # ssl=strict and the measurement above confirms the filter
                # does what it says. It is still read off the row rather than
                # assumed: a filter that stops working should show up here as
                # false addresses, not as a constant this file asserts.
                https_ok=bool(row.get("ssl")),
                latency_ms=_latency(row.get("latency")),
                uptime_pct=_uptime(row.get("uptime")),
                last_checked=str(row.get("lastChecked") or "").strip(),
            ),
        )
    return found


def _parse_proxifly(text: str, feed: ProxyFeed) -> list[FeedEndpoint]:
    found: list[FeedEndpoint] = []
    for row in _rows(text):
        geo = row.get("geolocation")
        geo = geo if isinstance(geo, Mapping) else {}
        _keep(
            found,
            _make(
                _protocol(row.get("protocol")),
                _ip(row.get("ip")),
                _port(row.get("port")),
                country=str(geo.get("country") or "").strip().upper(),
                anonymity=_anonymity(row.get("anonymity")),
                https_ok=bool(row.get("https")),
            ),
        )
    return found


def _parse_monosans(text: str, feed: ProxyFeed) -> list[FeedEndpoint]:
    found: list[FeedEndpoint] = []
    for row in _rows(text):
        geo = row.get("geolocation")
        geo = geo if isinstance(geo, Mapping) else {}
        country = geo.get("country")
        country = country if isinstance(country, Mapping) else {}
        asn = row.get("asn")
        asn = asn if isinstance(asn, Mapping) else {}
        protocol = _protocol(row.get("protocol"))
        seconds = row.get("timeout")
        _keep(
            found,
            _make(
                protocol,
                _ip(row.get("host")),
                _port(row.get("port")),
                country=str(
                    (country.get("names") or {}).get("en")
                    if isinstance(country.get("names"), Mapping)
                    else ""
                ).strip(),
                https_ok=protocol in {"https", "socks5"},
                # This feed publishes seconds, not milliseconds. Measured:
                # 0.18 for a proxy answering in under a fifth of a second.
                latency_ms=_latency(
                    float(seconds) * 1000.0
                    if isinstance(seconds, int | float)
                    else None
                ),
                asn=str(asn.get("autonomous_system_organization") or "").strip(),
            ),
        )
    return found


def _parse_geonode(text: str, feed: ProxyFeed) -> list[FeedEndpoint]:
    found: list[FeedEndpoint] = []
    for row in _rows(text, "data"):
        protocol = _first_protocol(row.get("protocols"))
        _keep(
            found,
            _make(
                protocol,
                _ip(row.get("ip")),
                _port(row.get("port")),
                country=str(row.get("country") or "").strip().upper(),
                anonymity=_anonymity(row.get("anonymityLevel")),
                https_ok=protocol in {"https", "socks5"},
                latency_ms=_latency(row.get("latency")),
                uptime_pct=_uptime(row.get("upTime")),
                asn=str(row.get("asn") or "").strip(),
            ),
        )
    return found


def _parse_lines(text: str, feed: ProxyFeed) -> list[FeedEndpoint]:
    """``ip:port`` per line, ``#`` a comment, the protocol from the filename.

    The scheme cannot come from the body of a file like this, which is why the
    feed that uses this parser names a file whose protocol is in its own name.
    A line that already carries a scheme is honoured, because some of these
    lists mix the two forms.
    """

    found: list[FeedEndpoint] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        protocol = feed.assume_protocol
        if "://" in line:
            scheme, _, line = line.partition("://")
            protocol = _protocol(scheme) or protocol
        host, separator, port = line.partition(":")
        if not separator:
            continue
        _keep(
            found,
            _make(
                _protocol(protocol),
                _ip(host),
                _port(port),
                anonymity=feed.assume_anonymity,
                https_ok=feed.assume_https_ok,
            ),
        )
    return found


_PARSERS = {
    "proxyscrape": _parse_proxyscrape,
    "hproxy": _parse_hproxy,
    "databay": _parse_databay,
    "proxifly": _parse_proxifly,
    "monosans": _parse_monosans,
    "geonode": _parse_geonode,
    "lines": _parse_lines,
}


# ---------------------------------------------------------------- catalogue

CATALOGUE: tuple[ProxyFeed, ...] = (
    ProxyFeed(
        id="proxyscrape",
        name="ProxyScrape",
        url=(
            "https://api.proxyscrape.com/v4/free-proxy-list/get"
            "?request=display_proxies&proxy_format=protocolipport"
            "&format=json&limit=300&protocol=http,socks5"
        ),
        parser="proxyscrape",
        homepage="https://github.com/ProxyScrape/free-proxy-list",
        observed=(
            "JSON with per-address metadata: protocol, anonymity, SSL "
            "support, uptime, timeout, country and ASN. 2,564 records on the "
            "day this was read, 1,857 of them in the two schemes this "
            "product can dial -- which is what the protocol filter above "
            "asks for, so the rest are never downloaded."
        ),
    ),
    ProxyFeed(
        id="hproxy",
        name="HProxy",
        url="https://raw.githubusercontent.com/hproxy-com/free-proxy-list/main/live.json",
        parser="hproxy",
        homepage="https://github.com/hproxy-com/free-proxy-list",
        observed=(
            "JSON from the GitHub repository: protocol, anonymity, country, "
            "latency and observed uptime per address. Its documented keyless "
            "API host did not resolve, so only the repository is read."
        ),
    ),
    ProxyFeed(
        id="databay",
        name="Databay (TLS-strict)",
        url=(
            "https://databay.com/api/v1/proxy-list?protocol=socks5&ssl=strict&limit=300"
        ),
        parser="databay",
        homepage="https://github.com/databay-labs/free-proxy-list",
        tls_strict=True,
        observed=(
            "JSON, and the only feed of the seven whose own filter selects "
            "for a tunnel that leaves the destination's certificate "
            "verifiable. Confirmed by fetching it both ways: without "
            "ssl=strict the answer contains addresses marked ssl:false, with "
            "it every address is ssl:true."
        ),
    ),
    ProxyFeed(
        id="proxifly",
        name="Proxifly",
        url=(
            "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main"
            "/proxies/protocols/socks5/data.json"
        ),
        parser="proxifly",
        homepage="https://github.com/proxifly/free-proxy-list",
        observed=(
            "JSON from the project's CDN mirror: protocol, anonymity, an "
            "https flag and a country per address. The SOCKS5 shard is read "
            "rather than the combined list, which is twice the size."
        ),
    ),
    ProxyFeed(
        id="vpslab",
        name="VPSLab (HTTP, SSL, elite)",
        url=(
            "https://raw.githubusercontent.com/VPSLabCloud/"
            "VPSLab-Free-Proxy-List/main/http_ssl_elite.txt"
        ),
        parser="lines",
        homepage="https://github.com/VPSLabCloud/VPSLab-Free-Proxy-List",
        assume_protocol="http",
        assume_https_ok=True,
        assume_anonymity="elite",
        observed=(
            "Plain ip:port lines behind a comment header naming the file's "
            "protocol, SSL and anonymity. This file is read rather than the "
            "combined one because a bare address list cannot say which "
            "scheme dials it, and this file's name can."
        ),
    ),
    ProxyFeed(
        id="monosans",
        name="monosans",
        url="https://raw.githubusercontent.com/monosans/proxy-list/main/proxies.json",
        parser="monosans",
        homepage="https://github.com/monosans/proxy-list",
        observed=(
            "JSON from the GitHub repository: protocol, host, port, the "
            "response time in seconds, the exit IP the project saw, and an "
            "ASN and country per address."
        ),
    ),
    ProxyFeed(
        id="geonode",
        name="Geonode",
        url=(
            "https://proxylist.geonode.com/api/proxy-list"
            "?limit=300&page=1&sort_by=lastChecked&sort_type=desc"
        ),
        parser="geonode",
        homepage="https://geonode.com/free-proxy-list",
        observed=(
            "JSON from the project's list API: protocols, anonymity level, "
            "country, ASN, latency, uptime percentage and a last-checked "
            "timestamp, newest first."
        ),
    ),
)

FEEDS_BY_ID: dict[str, ProxyFeed] = {feed.id: feed for feed in CATALOGUE}


def known_feed_ids(values: object) -> tuple[str, ...]:
    """The feeds among ``values`` that this install ships, in catalogue order.

    Unknown names are dropped rather than raised on: this is reached by a
    stored document that may predate a feed being removed, and a store that
    refuses to load because one source retired is a worse outcome than a
    switch quietly going away.
    """

    if isinstance(values, str) or not isinstance(values, Sequence):
        return ()
    wanted = {str(value).strip().lower() for value in values}
    return tuple(feed.id for feed in CATALOGUE if feed.id in wanted)


__all__ = [
    "CATALOGUE",
    "FEEDS_BY_ID",
    "FEEDS_OBSERVED_ON",
    "FEED_MAX_BYTES",
    "FEED_MAX_ENDPOINTS",
    "FEED_PROTOCOLS",
    "FeedEndpoint",
    "ProxyFeed",
    "known_feed_ids",
]
