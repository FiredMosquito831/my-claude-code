"""The proxy-list *formats* this install knows how to read.

**MCC ships readers, not sources.** This module holds seven parsers -- code
that knows how to read a shape -- and no list of anybody's endpoints. Which
URLs to fetch is the operator's choice, stored in their own
``~/.mcc/proxy_chains.json`` as named custom feeds, and a fresh install
contacts nobody at all because it has been told about nobody at all.

That is the distinction, and it is deliberate: a *parser* is a fact about a
file format, where a *catalogue* is a claim that some third party's endpoint
will be at some URL tomorrow. Earlier releases shipped seven concrete feeds and
so made a release depend on seven strangers' uptime and on their URLs staying
put. Removing the catalogue removes that dependency without removing any
ability: every format those feeds published is still readable here, and an
operator who wants one of them types its URL and picks -- or is offered -- the
matching reader.

**The seven shapes, and what each reader expects.** These were read off real
responses on 2026-09-15 rather than off anybody's documentation, which is why
they are described by structure rather than by publisher:

======================  =====================================================
``proxyscrape``         ``{"proxies": [...]}``; rows carry ``ip``, ``port``,
                        ``protocol``, ``anonymity``, ``ssl``, ``uptime``,
                        ``timeout`` and an ``ip_data`` object with
                        ``countryCode`` and ``as``.
``hproxy``              A bare array of ``{proxy, ip, port, protocols[],
                        anonymity, country, latency_ms, uptime_pct, alive}``.
``databay``             ``{"data": [...]}`` with ``ip``, ``port``,
                        ``protocol``, ``iso``, ``ssl``, ``latency``,
                        ``uptime`` and ``lastChecked``.
``proxifly``            A bare array of ``{proxy, protocol, ip, port, https,
                        anonymity, score, geolocation{country, city}}``.
``monosans``            A bare array of ``{protocol, host, port, timeout,
                        exit_ip, asn{...}, geolocation{country{names{en}}}}``
                        -- note ``host`` rather than ``ip``, and a timeout in
                        **seconds**.
``geonode``             ``{"data": [...]}`` with ``protocols[]``,
                        ``anonymityLevel``, ``country``, ``asn``, ``latency``,
                        ``upTime`` and ``lastChecked``.
``lines``               Plain text, one ``ip:port`` per line, ``#`` a comment.
                        The body of such a file can never say which scheme
                        dials it, so the feed carries that as
                        :attr:`ProxyFeed.assume_protocol`; a line that brings
                        its own ``scheme://`` is honoured over it.
======================  =====================================================

:func:`detect_parser` tries all seven against a body an operator has just
pointed at and reports which of them yield plausible addresses. It **proposes**
-- the page shows the picker either way, pre-set to the proposal, and the
operator's choice is what is stored. A format nothing recognises is still
addable: a URL that 404s today may answer tomorrow.

**What is not claimed.** Nothing here measures anything. A feed says an address
is alive; the checker in :mod:`~my_claude_code.application.proxy_check` is the
only thing in this product that finds out.

``socks4`` rows are dropped on the way in. ``httpx[socks]`` dials ``http``,
``https``, ``socks5`` and ``socks5h`` and nothing else
(:data:`~my_claude_code.config.proxy_chains.PROXY_URL_SCHEMES`), so a socks4
address in the candidate list would be one that can never be added to a chain.
"""

import json
import secrets
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Self
from urllib.parse import urlsplit

from loguru import logger

#: The day the seven shapes above were read off real responses. Kept as
#: provenance for the parsers, not as a claim about anybody's current URL.
FEEDS_OBSERVED_ON = "2026-09-15"

#: The longest display name a feed may carry. Long enough for a sentence's
#: worth of "which list is this", short enough that one cannot be used to push
#: the rest of a row off the page.
FEED_NAME_MAX_LENGTH = 60

#: The most bytes one feed may return. A response larger than this is truncated
#: and what parsed out of the truncated text is kept, because a feed that grew
#: a megabyte overnight is a feed to read less of, not one to stop reading. The
#: largest list measured while these readers were written was 1.8 MB.
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
    #: destination's certificate checkable -- some lists publish such a filter.
    #: It lives in the feed's URL rather than in a comment, and it is a
    #: preference rather than a verdict: only the checker finds out.
    tls_strict: bool = False
    #: For a feed whose rows are bare ``ip:port``: what its own filename says
    #: about them. A plain list can never carry a scheme, and guessing between
    #: ``http`` and ``socks5`` is how a chain ends up unable to connect.
    assume_protocol: str = ""
    assume_https_ok: bool = False
    assume_anonymity: str = ""
    #: What a trial read of this URL found, in one sentence, rendered on the
    #: page beside the switch so the operator can see what they pointed MCC at
    #: without leaving it.
    observed: str = ""

    def parse(self, text: str) -> tuple[FeedEndpoint, ...]:
        """Read this feed's body into normalised addresses.

        Never raises. A feed that changed shape, went to an error page or came
        back truncated yields nothing, and the caller reports "0 addresses"
        rather than failing the whole pass: six working feeds should not be
        lost to a seventh's bad morning.
        """

        parser = _PARSERS.get(self.parser)
        if parser is None:
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


def mint_feed_id(existing: Iterable[str]) -> str:
    """Return a feed id no feed in this store already holds."""

    taken = set(existing)
    while True:
        feed_id = f"fd_{secrets.token_hex(4)}"
        if feed_id not in taken:
            return feed_id


@dataclass(frozen=True, slots=True)
class CustomFeed:
    """One proxy list the operator told this install about.

    Name, URL and a reader: the three things MCC cannot work out on its own.
    Detection *proposes* the reader by fetching the URL once and seeing which
    of the seven yields addresses, but what is stored here is whatever the
    operator left the picker on -- the proposal is never written behind their
    back.

    The three ``assume_*`` fields exist for the ``lines`` reader, whose file
    cannot say which scheme dials it. They are not offered on the Add form;
    they are set by detection and, for a feed converted from a pre-7.18.0
    built-in, carried across from what that built-in declared, so a converted
    plain-text feed parses exactly as many addresses after the migration as
    before it.
    """

    id: str
    name: str
    url: str
    parser: str
    enabled: bool = False
    added_at: str = ""
    #: What the URL's own filter selects for, where the operator said so. Only
    #: ever a preference -- the checker is the only thing that finds out.
    tls_strict: bool = False
    assume_protocol: str = ""
    assume_https_ok: bool = False
    assume_anonymity: str = ""
    #: What a trial parse of this URL found, in one sentence, rendered on the
    #: row so the operator can see what they pointed MCC at without leaving
    #: the page.
    observed: str = ""

    @property
    def readable(self) -> bool:
        """Whether this install still ships a reader for this feed."""

        return bool(self.parser)

    def as_proxy_feed(self) -> ProxyFeed:
        """This row as the thing :mod:`proxy_ingest` knows how to fetch."""

        return ProxyFeed(
            id=self.id,
            name=self.name,
            url=self.url,
            parser=self.parser,
            homepage="",
            tls_strict=self.tls_strict,
            assume_protocol=self.assume_protocol,
            assume_https_ok=self.assume_https_ok,
            assume_anonymity=self.assume_anonymity,
            observed=self.observed,
        )

    def as_document(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "url": self.url,
            "parser": self.parser,
            "enabled": self.enabled,
            "added_at": self.added_at,
            "tls_strict": self.tls_strict,
            "assume_protocol": self.assume_protocol,
            "assume_https_ok": self.assume_https_ok,
            "assume_anonymity": self.assume_anonymity,
            "observed": self.observed,
        }

    @classmethod
    def from_document(cls, raw: object, where: str) -> Self | None:
        """Read one stored feed, or ``None`` for a row that cannot be one.

        A row is dropped only when it has no usable URL, because a feed with
        nowhere to fetch from is not a feed. An **unknown parser is kept**,
        blanked: that is a feed whose reader this install no longer ships, and
        the honest answer is a row on the page saying "pick a format" rather
        than a switch that silently disappeared.
        """

        if not isinstance(raw, Mapping):
            logger.warning("PROXY CHAINS: '{}' is not an object; ignoring it", where)
            return None
        url = str(raw.get("url") or "").strip()
        if not is_valid_feed_url(url):
            logger.warning(
                "PROXY CHAINS: '{}' is not a usable https feed URL; ignoring it",
                where,
            )
            return None
        name = str(raw.get("name") or "").strip()[:FEED_NAME_MAX_LENGTH]
        return cls(
            id=str(raw.get("id") or "").strip(),
            name=name or url,
            url=url,
            parser=normalise_parser(raw.get("parser")),
            enabled=bool(raw.get("enabled")),
            added_at=str(raw.get("added_at") or "").strip(),
            tls_strict=bool(raw.get("tls_strict")),
            assume_protocol=str(raw.get("assume_protocol") or "").strip(),
            assume_https_ok=bool(raw.get("assume_https_ok")),
            assume_anonymity=str(raw.get("assume_anonymity") or "").strip(),
            observed=str(raw.get("observed") or "").strip(),
        )


def is_valid_feed_url(url: str) -> bool:
    """Whether a string is a URL this product will fetch a proxy list from.

    **https only.** A feed is a list of addresses that will end up in front of
    a credential, fetched from a machine on the open internet; reading it over
    plain http would let anyone on the path choose which proxies this install
    considers, which is a downgrade with no case for it. The rest of this
    module's schemes are about what MCC *dials through*, which is a different
    question from what it *trusts a list from*.
    """

    candidate = url.strip()
    if not candidate:
        return False
    parsed = urlsplit(candidate)
    if parsed.scheme != "https":
        return False
    try:
        host = parsed.hostname
    except ValueError:
        return False
    return bool(host)


# ------------------------------------------------------------------ pickers

#: What a bare ``ip:port`` list is read as when nothing else says. ``http`` is
#: the commonest such file by a wide margin and it is what the removed VPSLab
#: entry assumed; it is a *stated* assumption rendered on the page beside the
#: picker, never a silent one, and a line carrying its own ``scheme://``
#: overrides it.
LINES_DEFAULT_PROTOCOL = "http"


@dataclass(frozen=True, slots=True)
class FeedParser:
    """One reader, named for the shape it reads rather than for a publisher.

    ``shape`` is the sentence the Proxying page renders beside the picker, in
    the same voice the feed rows have always used, so an operator choosing a
    reader is choosing against a description of their own file rather than
    against a bare identifier.
    """

    id: str
    label: str
    shape: str


#: Every reader this install ships, in the order the picker offers them: the
#: JSON shapes that carry per-address metadata first, the bare address list
#: last, because it is the one that can say the least.
PARSERS: tuple[FeedParser, ...] = (
    FeedParser(
        "proxyscrape",
        "JSON: proxies[] with ip_data",
        'JSON with per-address metadata under a "proxies" key: protocol, '
        "anonymity, SSL support, uptime, timeout, country and ASN.",
    ),
    FeedParser(
        "databay",
        'JSON: data[] with "iso" and "ssl"',
        'JSON with per-address metadata under a "data" key: protocol, country '
        "as an ISO code, an SSL flag, latency, uptime and a last-checked time.",
    ),
    FeedParser(
        "geonode",
        'JSON: data[] with "protocols" and "anonymityLevel"',
        'JSON with per-address metadata under a "data" key: a list of '
        "protocols, an anonymity level, country, ASN, latency and uptime.",
    ),
    FeedParser(
        "hproxy",
        'JSON array with "protocols" and "alive"',
        "A plain JSON array of addresses, each with a list of protocols, an "
        "anonymity level, country, latency and observed uptime.",
    ),
    FeedParser(
        "proxifly",
        'JSON array with "https" and "geolocation"',
        "A plain JSON array of addresses, each with one protocol, an https "
        "flag, an anonymity level and a country.",
    ),
    FeedParser(
        "monosans",
        'JSON array with "host" and seconds',
        'A plain JSON array of addresses keyed by "host" rather than "ip", '
        "with the response time in seconds and a nested ASN and country.",
    ),
    FeedParser(
        "lines",
        "Plain text: one ip:port per line",
        "Plain ip:port lines with # for a comment. The file cannot say which "
        f"scheme dials it, so these are read as {LINES_DEFAULT_PROTOCOL}; a "
        "line that carries its own scheme:// keeps it.",
    ),
)

PARSERS_BY_ID: dict[str, FeedParser] = {parser.id: parser for parser in PARSERS}

#: The picker's ids, which are exactly :data:`_PARSERS`' keys. Pinned together
#: in ``tests/config/test_proxy_feeds.py`` so a reader can never be added to
#: one and forgotten in the other -- a parser missing from ``PARSERS`` would be
#: unreachable from the page, and one missing from ``_PARSERS`` would be
#: offered and then silently yield nothing.
PARSER_IDS: tuple[str, ...] = tuple(parser.id for parser in PARSERS)


def is_known_parser(parser_id: object) -> bool:
    """Whether ``parser_id`` names a reader this install ships."""

    return str(parser_id or "").strip().lower() in PARSERS_BY_ID


def normalise_parser(value: object) -> str:
    """Return a reader id, or ``""`` for anything this install cannot read.

    Empty rather than a default: guessing a reader for a stored feed would
    make a feed that silently yields nothing look like a feed whose publisher
    went quiet, and those want different answers from the operator.
    """

    parser_id = str(value or "").strip().lower()
    return parser_id if parser_id in PARSERS_BY_ID else ""


def parser_shape(parser_id: str) -> str:
    """The one-sentence description of what a reader expects."""

    parser = PARSERS_BY_ID.get(str(parser_id or "").strip().lower())
    return parser.shape if parser is not None else ""


def probe_feed(parser_id: str) -> ProxyFeed:
    """A throwaway feed carrying just enough to run ``parser_id`` once.

    Detection has no stored feed to parse against yet, and ``lines`` cannot
    produce an address without being told a protocol, so the probe states the
    same assumption the picker renders.
    """

    return ProxyFeed(
        id="",
        name="",
        url="",
        parser=str(parser_id or "").strip().lower(),
        homepage="",
        assume_protocol=LINES_DEFAULT_PROTOCOL,
    )


@dataclass(frozen=True, slots=True)
class ParserTrial:
    """What one reader made of a body, in the words the page reports it with."""

    parser: str
    label: str
    shape: str
    count: int

    @property
    def plausible(self) -> bool:
        return self.count > 0

    def as_document(self) -> dict[str, Any]:
        return {
            "parser": self.parser,
            "label": self.label,
            "shape": self.shape,
            "count": self.count,
        }


def detect_parser(text: str) -> tuple[ParserTrial, ...]:
    """Try every reader against ``text``; best first, all of them reported.

    **This proposes and never decides.** The caller shows the picker either
    way, pre-set to the first trial that found anything, and stores whatever
    the operator left it on. A body nothing reads comes back as seven trials of
    zero -- which is an answer ("no reader here recognises this"), not a
    failure, and the feed is still addable.

    ``lines`` is tried last among equals on purpose: it accepts almost any text
    containing ``host:port``, so on a JSON body it can score a spurious hit off
    the punctuation. Ordering by count first and by :data:`PARSERS` order
    second means a JSON reader that genuinely parsed the document outranks it.
    """

    trials: list[ParserTrial] = []
    for parser in PARSERS:
        try:
            found = probe_feed(parser.id).parse(text)
        except Exception:  # pragma: no cover - ``parse`` already swallows these
            found = ()
        trials.append(
            ParserTrial(
                parser=parser.id,
                label=parser.label,
                shape=parser.shape,
                count=len(found),
            )
        )
    # ``sorted`` is stable, so equal counts keep PARSERS order and ``lines``
    # stays last among them.
    return tuple(sorted(trials, key=lambda trial: -trial.count))


def proposed_parser(trials: Sequence[ParserTrial]) -> str:
    """The reader detection proposes, or ``""`` when nothing recognised it."""

    for trial in trials:
        if trial.plausible:
            return trial.parser
    return ""


__all__ = [
    "FEEDS_OBSERVED_ON",
    "FEED_MAX_BYTES",
    "FEED_MAX_ENDPOINTS",
    "FEED_NAME_MAX_LENGTH",
    "FEED_PROTOCOLS",
    "LINES_DEFAULT_PROTOCOL",
    "PARSERS",
    "PARSERS_BY_ID",
    "PARSER_IDS",
    "CustomFeed",
    "FeedEndpoint",
    "FeedParser",
    "ParserTrial",
    "ProxyFeed",
    "detect_parser",
    "is_known_parser",
    "is_valid_feed_url",
    "mint_feed_id",
    "normalise_parser",
    "parser_shape",
    "probe_feed",
    "proposed_parser",
]
