"""Read the named feeds an operator switched on, and rank what they offer.

One pass over the enabled feeds produces a **candidate list** and nothing else.
Say it plainly, because it is the whole safety argument of this module:

* An ingested address is a *candidate*, not a chain member. It lands in
  :attr:`~my_claude_code.config.proxy_chains.ProxyChains.candidates`, which no
  provider's chain references and which the runtime cannot select from. No
  credential goes through an address that arrived here.
* It becomes a chain member only when an operator moves one row into one
  provider's chain, and that move is gated by the checker: the address is
  measured against that provider's own host first, and an address whose tunnel
  breaks certificate validation is refused exactly as a typed one is.
* Nothing in this module runs unless the operator asked. A fresh install has no
  enabled feeds, so a pass over "every enabled feed" is a pass over nothing and
  makes no outbound request at all.

**The one signal worth having is agreement.** Each feed is read independently
and the results are merged on ``ip:port``; ``source_count`` is how many
distinct feeds listed the same address in the same pass. It costs nothing and
it is real: four feeds independently observing one endpoint is materially
better evidence than one scraper's row. It is the first term of the ranking
below, and it is rendered on the page beside the address, by name, so the
operator can see where what they are about to put in front of a credential
came from.

**HTTPS-capable first.** Some lists publish a filter that selects for exactly
the property this product needs: a tunnel that carries HTTPS without breaking
the destination's certificate checks. Where an operator pointed a feed at such
a URL, an address any feed marked HTTPS-capable outranks one none did. It is
the feed-side analogue of the checker, and it is a preference, not a verdict:
only the checker actually finds out.
"""

import asyncio
import hashlib
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

import httpx
from loguru import logger

from my_claude_code.config.constants import PROXY_FEED_TIMEOUT_SECONDS_DEFAULT
from my_claude_code.config.credentials import mask_proxy_label
from my_claude_code.config.proxy_chains import (
    SOURCE_FEED,
    ProxyChains,
    ProxyEndpoint,
    ProxyFeedFacts,
    load_proxy_chains,
    save_proxy_chains,
)
from my_claude_code.config.proxy_feeds import (
    FEED_MAX_BYTES,
    FeedEndpoint,
    ParserTrial,
    ProxyFeed,
    detect_parser,
    parser_shape,
    proposed_parser,
)

#: How long one feed has to answer. Short on purpose: a pass is serial, so a
#: minute per feed would make the Fetch button unbounded in the number of feeds
#: the operator added -- and a public list that cannot answer in fifteen
#: seconds is not the one to build a chain on.
#:
#: Since 7.24.0 this is the shipped default of the
#: ``PROXY_FEED_TIMEOUT_SECONDS`` setting on Limits & Resilience, and the
#: value every caller that is handed nothing still uses.
FEED_TIMEOUT_SECONDS = PROXY_FEED_TIMEOUT_SECONDS_DEFAULT

#: What MCC calls itself when it asks. These are other people's servers and an
#: unattributed scraper is the thing every one of their READMEs complains
#: about.
FEED_USER_AGENT = "my-claude-code (+https://github.com/FiredMosquito831/my-claude-code)"


@dataclass(frozen=True, slots=True)
class FeedResult:
    """What one feed did when it was asked. Never an exception."""

    feed_id: str
    name: str
    ok: bool
    count: int = 0
    detail: str = ""


@dataclass(frozen=True, slots=True)
class IngestRun:
    """One pass over the enabled feeds, as the page reports it."""

    at: str
    results: tuple[FeedResult, ...] = ()
    offered: int = 0
    #: Addresses at least two feeds agreed on. The headline number, because it
    #: is the one that distinguishes this from downloading a list.
    corroborated: int = 0

    @property
    def reached(self) -> int:
        return sum(1 for result in self.results if result.ok)

    def as_document(self) -> dict[str, object]:
        return {
            "at": self.at,
            "offered": self.offered,
            "corroborated": self.corroborated,
            "feeds": [
                {
                    "id": result.feed_id,
                    "name": result.name,
                    "ok": result.ok,
                    "count": result.count,
                    "detail": result.detail,
                }
                for result in self.results
            ],
        }


@dataclass
class _Merged:
    """One address, and every feed that offered it in this pass."""

    endpoint: FeedEndpoint
    sources: list[str] = field(default_factory=list)

    def absorb(self, other: FeedEndpoint, feed_id: str) -> None:
        if feed_id not in self.sources:
            self.sources.append(feed_id)
        # Keep the richest description of the same address. A feed that knows
        # the country and the ASN says more about it than one that published a
        # bare line, and neither is more authoritative about an address they
        # both list -- so fields are filled in, never overwritten.
        current = self.endpoint
        self.endpoint = replace(
            current,
            country=current.country or other.country,
            anonymity=current.anonymity or other.anonymity,
            https_ok=current.https_ok or other.https_ok,
            latency_ms=(
                other.latency_ms
                if current.latency_ms is None
                else min(current.latency_ms, other.latency_ms or current.latency_ms)
            ),
            uptime_pct=(
                other.uptime_pct
                if current.uptime_pct is None
                else max(current.uptime_pct, other.uptime_pct or 0.0)
            ),
            last_checked=current.last_checked or other.last_checked,
            asn=current.asn or other.asn,
        )


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


async def fetch_feed(
    feed: ProxyFeed,
    *,
    timeout: float = FEED_TIMEOUT_SECONDS,
    client: httpx.AsyncClient | None = None,
) -> tuple[FeedResult, tuple[FeedEndpoint, ...]]:
    """Ask one feed for its list. Never raises, never weakens TLS.

    An ordinary client with nothing said about trust, which is the same rule
    the rest of this package follows and the same one
    ``tests/contracts/test_tls_verification_is_never_weakened.py`` keeps true:
    these are public lists fetched over HTTPS and there is no reason for any of
    them to need a certificate this machine does not already trust.
    """

    owned = client is None
    session = client or httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers={"user-agent": FEED_USER_AGENT},
    )
    try:
        response = await session.get(feed.url)
        if response.status_code >= 400:
            return (
                FeedResult(
                    feed.id,
                    feed.name,
                    ok=False,
                    detail=f"answered {response.status_code}",
                ),
                (),
            )
        body = response.text
        if len(body.encode("utf-8", "ignore")) > FEED_MAX_BYTES:
            # Truncated rather than refused: a list that grew is still a list,
            # and what parsed out of the first few megabytes is real.
            body = body[: FEED_MAX_BYTES // 2]
    except Exception as exc:
        return (
            FeedResult(
                feed.id,
                feed.name,
                ok=False,
                detail=f"{type(exc).__name__}: {exc}",
            ),
            (),
        )
    finally:
        if owned:
            await session.aclose()

    endpoints = feed.parse(body)
    if not endpoints:
        return (
            FeedResult(
                feed.id,
                feed.name,
                ok=False,
                detail=(
                    "answered, but nothing in it parsed as an address -- this "
                    "feed has probably changed shape"
                ),
            ),
            (),
        )
    return FeedResult(feed.id, feed.name, ok=True, count=len(endpoints)), endpoints


@dataclass(frozen=True, slots=True)
class FeedDetection:
    """What one trial fetch of a URL the operator just typed found.

    ``parser`` is a **proposal**. The page shows the picker either way, pre-set
    to this, and stores whatever the operator left it on -- so a detection that
    is wrong costs one click, and a detection that finds nothing costs none of
    the operator's ability to add the feed anyway.
    """

    ok: bool
    detail: str = ""
    trials: tuple[ParserTrial, ...] = ()
    parser: str = ""

    @property
    def count(self) -> int:
        """How many addresses the proposed reader got out of the body."""

        return next(
            (trial.count for trial in self.trials if trial.parser == self.parser), 0
        )

    def as_document(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "detail": self.detail,
            "parser": self.parser,
            "count": self.count,
            "trials": [trial.as_document() for trial in self.trials],
        }


async def detect_feed(
    url: str,
    *,
    timeout: float = FEED_TIMEOUT_SECONDS,
    client: httpx.AsyncClient | None = None,
) -> FeedDetection:
    """Fetch ``url`` once and propose the reader that made most of it.

    **One outbound request, to a URL the operator typed into the form and
    pressed a button about.** That is the consent, and it is the same shape as
    the Test button's: nothing else here fetches, and the feed makes no further
    request until it is saved *and* enabled *and* somebody presses Fetch.

    Never raises, and a failure is still a useful answer: a URL that cannot be
    reached today is reported as unreachable and the operator can still add it,
    because a list that 404s this afternoon may be back tomorrow and a form
    that refuses to record it would be enforcing a guess about somebody else's
    uptime.

    TLS is ordinary and strict, the same as :func:`fetch_feed` -- this is a
    public list over HTTPS and there is no reason for one to need a certificate
    this machine does not already trust.
    """

    owned = client is None
    session = client or httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers={"user-agent": FEED_USER_AGENT},
    )
    try:
        response = await session.get(url)
        if response.status_code >= 400:
            return FeedDetection(
                ok=False, detail=f"The URL answered {response.status_code}."
            )
        body = response.text
        if len(body.encode("utf-8", "ignore")) > FEED_MAX_BYTES:
            body = body[: FEED_MAX_BYTES // 2]
    except Exception as exc:
        return FeedDetection(
            ok=False, detail=f"Could not read the URL: {type(exc).__name__}: {exc}"
        )
    finally:
        if owned:
            await session.aclose()

    trials = detect_parser(body)
    proposal = proposed_parser(trials)
    if not proposal:
        return FeedDetection(
            ok=True,
            detail=(
                "The URL answered, but none of the formats MCC reads "
                "recognised it. Pick one anyway if you know what this list is "
                "-- nothing is fetched again until you switch the feed on."
            ),
            trials=trials,
        )
    shape = parser_shape(proposal)
    count = next(trial.count for trial in trials if trial.parser == proposal)
    return FeedDetection(
        ok=True,
        detail=(
            f"{shape} A trial read found {count} address{'' if count == 1 else 'es'}."
        ),
        trials=trials,
        parser=proposal,
    )


def rank(merged: Iterable[_Merged]) -> list[_Merged]:
    """Best first.

    In order, and the order is the argument:

    1. **How many feeds agree.** The only term that is evidence about the
       address rather than a claim copied off one publisher.
    2. **HTTPS-capable**, which is the feed-side reading of the one property
       this product needs at all.
    3. **Elite**, then anonymous. It says what the destination sees, not
       whether the tunnel is honest -- so it ranks below both of the above.
    4. **Observed uptime**, then **latency**. Other people's measurements, and
       the last word goes to the checker rather than to these.
    """

    def key(item: _Merged) -> tuple[int, int, int, float, int]:
        endpoint = item.endpoint
        anonymity = {"elite": 2, "anonymous": 1}.get(endpoint.anonymity, 0)
        return (
            -len(item.sources),
            0 if endpoint.https_ok else 1,
            -anonymity,
            -(endpoint.uptime_pct or 0.0),
            endpoint.latency_ms if endpoint.latency_ms is not None else 10**6,
        )

    return sorted(merged, key=key)


def merge(
    harvest: Sequence[tuple[str, tuple[FeedEndpoint, ...]]],
) -> list[_Merged]:
    """Fold every feed's answer into one address-per-row table."""

    table: dict[str, _Merged] = {}
    for feed_id, endpoints in harvest:
        for endpoint in endpoints:
            existing = table.get(endpoint.address)
            if existing is None:
                table[endpoint.address] = _Merged(endpoint, [feed_id])
            else:
                existing.absorb(endpoint, feed_id)
    return list(table.values())


def rank_merged(
    harvest: Sequence[tuple[str, tuple[FeedEndpoint, ...]]],
) -> list[_Merged]:
    """Merge every feed's answer and put the best first. One line, one place.

    Exists so that the pass that only reads and the pass that reads and tests
    cannot disagree about what "best first" means: there is one merge and one
    ranking, and both callers ask this for them.
    """

    return rank(merge(harvest))


async def harvest_feeds(
    *,
    timeout: float = FEED_TIMEOUT_SECONDS,
    store: ProxyChains | None = None,
    on_feed: Callable[[], None] | None = None,
) -> tuple[list[FeedResult], list[tuple[str, tuple[FeedEndpoint, ...]]], int]:
    """Read every enabled feed once. Results, harvest, and how many were read.

    One client for the whole pass and one yield per feed, so a pass over a
    dozen slow lists cannot sit in front of a request. ``on_feed`` is called
    after each one, which is how a fetch shows "3 of 7 lists read" while it is
    still reading them.

    An install with no enabled feeds returns ``([], [], 0)`` and makes no
    outbound request at all -- which is every fresh install, because MCC ships
    no feeds of its own.
    """

    table = load_proxy_chains() if store is None else store
    feeds = enabled_feeds(table)
    if not feeds:
        return [], [], 0

    harvest: list[tuple[str, tuple[FeedEndpoint, ...]]] = []
    results: list[FeedResult] = []
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers={"user-agent": FEED_USER_AGENT},
    ) as client:
        for feed in feeds:
            result, endpoints = await fetch_feed(feed, timeout=timeout, client=client)
            results.append(result)
            if endpoints:
                harvest.append((feed.id, endpoints))
            if on_feed is not None:
                on_feed()
            # One yield per feed, so a pass cannot sit in front of a request
            # even when every feed the operator added is slow.
            await asyncio.sleep(0)
    return results, harvest, len(feeds)


def as_candidate_endpoint(item: _Merged, at: str) -> ProxyEndpoint:
    """One merged address as the catalogue row a candidate is stored as."""

    return _as_endpoint(item, at)


def _as_endpoint(item: _Merged, at: str) -> ProxyEndpoint:
    endpoint = item.endpoint
    return ProxyEndpoint(
        url=endpoint.url,
        label=mask_proxy_label(endpoint.url),
        added_at=at,
        source=SOURCE_FEED,
        source_count=len(item.sources),
        sources=tuple(item.sources),
        feed=ProxyFeedFacts(
            protocol=endpoint.protocol,
            country=endpoint.country,
            anonymity=endpoint.anonymity,
            https_ok=endpoint.https_ok,
            latency_ms=endpoint.latency_ms,
            uptime_pct=endpoint.uptime_pct,
            last_checked=endpoint.last_checked,
            asn=endpoint.asn,
        ),
    )


def candidate_id(address: str) -> str:
    """The id one ``ip:port`` is always filed under.

    Derived from the address rather than minted, so the same endpoint keeps the
    same id across passes -- which is what lets a candidate an operator already
    tested keep its verdict, including a ``TLS intercepted`` refusal, instead
    of being re-offered as a fresh unknown row on the next refresh.
    """

    digest = hashlib.sha256(address.encode("utf-8")).hexdigest()
    return f"px_{digest[:10]}"


def enabled_feeds(store: ProxyChains) -> tuple[ProxyFeed, ...]:
    """The feeds this install has been told to read, in the operator's order.

    Empty on a fresh install, and empty is the point: MCC ships no feed of its
    own, so "no outbound request the operator did not choose" is a property of
    the code -- there is nothing here to choose *from* until somebody adds one.

    A feed whose reader this install no longer ships is skipped rather than
    fetched: there would be nothing to do with the body.
    """

    return tuple(
        feed.as_proxy_feed() for feed in store.feeds if feed.enabled and feed.readable
    )


async def ingest(
    *,
    timeout: float = FEED_TIMEOUT_SECONDS,
    persist: bool = True,
    store: ProxyChains | None = None,
    limit: int = 0,
) -> IngestRun:
    """One pass: read every enabled feed, merge, rank, store the candidates.

    **This pass stores addresses nothing has tested**, which is why nothing in
    the product calls it any more:
    :func:`~my_claude_code.application.proxy_fetch.run_fetch_pass` is what the
    Fetch button and the scheduled refresh go through, and it keeps only
    addresses that passed the checker. This one survives for callers that want
    the reading half on its own -- and for the tests that pin what the reading
    half does -- and it is not reachable from the page.

    ``limit`` is a ceiling on how many of the ranked addresses are stored, in
    rank order. ``0``, the default, is no ceiling: chains have been unlimited
    since 7.19.0 and the sixty this used to impose was a number the tool chose
    rather than one the operator did.

    The store is re-read before the write rather than held across the fetches,
    the way ``check_endpoints`` does it and for the same reason: a pass takes
    seconds and an operator editing a chain in the meantime must not lose the
    edit to a result about a different table.
    """

    at = _now()
    results, harvest, feed_count = await harvest_feeds(timeout=timeout, store=store)
    if not feed_count:
        return IngestRun(at=at)

    merged = rank_merged(harvest)
    corroborated = sum(1 for item in merged if len(item.sources) > 1)
    run = IngestRun(
        at=at,
        results=tuple(results),
        offered=len(merged),
        corroborated=corroborated,
    )

    if persist:
        fresh = load_proxy_chains()
        kept = merged[:limit] if limit > 0 else merged
        offered = [
            (candidate_id(item.endpoint.address), _as_endpoint(item, at))
            for item in kept
        ]
        save_proxy_chains(fresh.with_candidates(offered))

    logger.info(
        "PROXY FEEDS: {} of {} feed(s) answered; {} address(es) on offer, {} "
        "listed by more than one",
        run.reached,
        feed_count,
        run.offered,
        run.corroborated,
    )
    return run


def feed_payload(store: ProxyChains) -> list[dict[str, object]]:
    """Every feed the operator has added, and what MCC makes of each.

    The URL travels back in full, unlike a proxy URL: a feed URL is a public
    list the operator typed themselves and they have to be able to see and edit
    it, where a proxy URL can carry ``user:pass`` and is masked everywhere.

    ``readable`` is the honest reading of a row whose parser this install does
    not ship -- a hand-edited file, or a reader retired in a later release. It
    renders as a row asking for a format rather than as a feed that silently
    stopped offering anything.
    """

    return [
        {
            "id": feed.id,
            "name": feed.name,
            "url": feed.url,
            "parser": feed.parser,
            "parser_shape": parser_shape(feed.parser),
            "readable": feed.readable,
            "observed": feed.observed,
            "tls_strict": feed.tls_strict,
            "enabled": feed.enabled,
        }
        for feed in store.feeds
    ]


def known_feed_name(store: ProxyChains, feed_id: str) -> str:
    """The display name of the feed that supplied an address, or its id.

    Falling back to the id matters more than it used to: a candidate outlives
    the feed that offered it -- removing a feed deliberately keeps the
    addresses it supplied -- so this is routinely asked about a feed that is no
    longer in the store, and "px_… came from databay" is a better answer than
    an empty cell.
    """

    feed = store.feed(feed_id)
    return feed.name if feed is not None else feed_id


__all__ = [
    "FEED_TIMEOUT_SECONDS",
    "FEED_USER_AGENT",
    "FeedDetection",
    "FeedResult",
    "IngestRun",
    "as_candidate_endpoint",
    "candidate_id",
    "detect_feed",
    "enabled_feeds",
    "feed_payload",
    "fetch_feed",
    "harvest_feeds",
    "ingest",
    "known_feed_name",
    "merge",
    "rank",
    "rank_merged",
]
