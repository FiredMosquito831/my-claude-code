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

**TLS-strict first.** Databay publishes an ``ssl=strict`` filter -- verified,
see :mod:`~my_claude_code.config.proxy_feeds` -- which selects for exactly the
property this product needs: a tunnel that carries HTTPS without breaking the
destination's certificate validation. Where a feed offers that, the feed URL
asks for it, and an address any feed marked HTTPS-capable outranks one none
did. It is the feed-side analogue of the checker, and it is a preference, not a
verdict: only the checker actually finds out.
"""

import asyncio
import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

import httpx
from loguru import logger

from my_claude_code.config.credentials import mask_proxy_label
from my_claude_code.config.proxy_chains import (
    MAX_CANDIDATES,
    SOURCE_FEED,
    ProxyChains,
    ProxyEndpoint,
    ProxyFeedFacts,
    load_proxy_chains,
    save_proxy_chains,
)
from my_claude_code.config.proxy_feeds import (
    CATALOGUE,
    FEED_MAX_BYTES,
    FEEDS_BY_ID,
    FeedEndpoint,
    ProxyFeed,
)

#: How long one feed has to answer. Short on purpose: seven feeds at sixty
#: seconds each would be a seven-minute button, and a public list that cannot
#: answer in fifteen seconds is not the one to build a chain on.
FEED_TIMEOUT_SECONDS = 15.0

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
    """The feeds this install has been told to read, in catalogue order.

    Empty on a fresh install, and empty is the point: it is what makes "no
    outbound request the operator did not choose" a property of the code rather
    than a promise in a docstring.
    """

    chosen = set(store.feeds)
    return tuple(feed for feed in CATALOGUE if feed.id in chosen)


async def ingest(
    *,
    timeout: float = FEED_TIMEOUT_SECONDS,
    persist: bool = True,
    store: ProxyChains | None = None,
) -> IngestRun:
    """One pass: read every enabled feed, merge, rank, store the candidates.

    The store is re-read before the write rather than held across the fetches,
    the way ``check_endpoints`` does it and for the same reason: a pass takes
    seconds and an operator editing a chain in the meantime must not lose the
    edit to a result about a different table.
    """

    table = load_proxy_chains() if store is None else store
    feeds = enabled_feeds(table)
    at = _now()
    if not feeds:
        return IngestRun(at=at)

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
            # One yield per feed, so a pass over seven of them cannot sit in
            # front of a request even when every one of them is slow.
            await asyncio.sleep(0)

    merged = rank(merge(harvest))
    corroborated = sum(1 for item in merged if len(item.sources) > 1)
    run = IngestRun(
        at=at,
        results=tuple(results),
        offered=len(merged),
        corroborated=corroborated,
    )

    if persist:
        fresh = load_proxy_chains()
        offered = [
            (candidate_id(item.endpoint.address), _as_endpoint(item, at))
            for item in merged[:MAX_CANDIDATES]
        ]
        save_proxy_chains(fresh.with_candidates(offered))

    logger.info(
        "PROXY FEEDS: {} of {} feed(s) answered; {} address(es) on offer, {} "
        "listed by more than one",
        run.reached,
        len(feeds),
        run.offered,
        run.corroborated,
    )
    return run


def feed_catalogue_payload(store: ProxyChains) -> list[dict[str, object]]:
    """Every feed this install ships, and whether it has been switched on."""

    chosen = set(store.feeds)
    return [
        {
            "id": feed.id,
            "name": feed.name,
            "homepage": feed.homepage,
            "observed": feed.observed,
            "tls_strict": feed.tls_strict,
            "enabled": feed.id in chosen,
        }
        for feed in CATALOGUE
    ]


def known_feed_name(feed_id: str) -> str:
    feed = FEEDS_BY_ID.get(feed_id)
    return feed.name if feed is not None else feed_id


__all__ = [
    "FEED_TIMEOUT_SECONDS",
    "FEED_USER_AGENT",
    "FeedResult",
    "IngestRun",
    "candidate_id",
    "enabled_feeds",
    "feed_catalogue_payload",
    "fetch_feed",
    "ingest",
    "known_feed_name",
    "merge",
    "rank",
]
