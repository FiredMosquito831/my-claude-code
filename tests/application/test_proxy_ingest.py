"""Ingestion: merge, count, rank, and never put a stranger in a chain.

The three properties this file exists to pin, in order of how much they would
cost to get wrong:

1. **A pass over no enabled feeds makes no outbound request at all.** That is
   the whole consent story and it is a property of the code, not a promise.
2. **An ingested address is a candidate, not a chain member.** Nothing in a
   chain changes when a pass runs.
3. ``source_count`` **counts feeds, not rows.** It is the one quality signal
   that is evidence rather than a claim copied off one publisher, and a count
   that double-counted one feed would be worse than no count.
"""

import json

import httpx
import pytest

from my_claude_code.application import proxy_ingest
from my_claude_code.config.proxy_chains import (
    TLS_INTERCEPTED,
    ProxyChain,
    ProxyChainEntry,
    ProxyChains,
    ProxyCheckRecord,
    ProxyEndpoint,
    load_proxy_chains,
    save_proxy_chains,
)
from my_claude_code.config.proxy_feeds import CustomFeed, FeedEndpoint, ProxyFeed

SHARED = "203.0.113.7"

DATABAY_URL = "https://databay.example/list"


def endpoint(ip: str = SHARED, port: int = 1080, **rest) -> FeedEndpoint:
    return FeedEndpoint(protocol="socks5", ip=ip, port=port, **rest)


def a_feed() -> ProxyFeed:
    """One feed an operator added, as ``fetch_feed`` is handed it.

    MCC ships no catalogue to borrow an entry from, so a test that wants
    something fetchable says so itself -- which is also exactly what
    :meth:`CustomFeed.as_proxy_feed` hands the fetcher in production.
    """

    return ProxyFeed(
        id="databay",
        name="Databay",
        url=DATABAY_URL,
        parser="databay",
        homepage="",
    )


@pytest.fixture
def store_path(tmp_path, monkeypatch):
    path = tmp_path / "proxy_chains.json"
    monkeypatch.setattr(
        "my_claude_code.config.proxy_chains.proxy_chains_path", lambda: path
    )
    from my_claude_code.config.proxy_chains import reset_proxy_chains_cache

    reset_proxy_chains_cache()
    yield path
    reset_proxy_chains_cache()


def test_a_pass_with_no_enabled_feeds_makes_no_request(store_path, monkeypatch):
    """The consent story, as a property of the code.

    A fresh install has no feed selected, so ``ingest`` must not so much as
    construct a client. Anything that reached the network here would be an
    outbound request the operator never asked for.
    """

    def explode(*args, **kwargs):  # pragma: no cover - the assertion is that
        raise AssertionError("ingest contacted the network with no feeds enabled")

    monkeypatch.setattr(httpx, "AsyncClient", explode)
    save_proxy_chains(ProxyChains())

    import asyncio

    run = asyncio.run(proxy_ingest.ingest())
    assert run.results == ()
    assert run.offered == 0


def test_source_count_counts_feeds_not_rows():
    """Three feeds listing one address is 3; one feed listing it twice is 1."""

    merged = proxy_ingest.merge(
        [
            ("proxyscrape", (endpoint(), endpoint(ip="198.51.100.1"))),
            ("hproxy", (endpoint(),)),
            ("databay", (endpoint(), endpoint())),
        ]
    )
    by_address = {item.endpoint.address: item for item in merged}
    assert sorted(by_address[f"{SHARED}:1080"].sources) == [
        "databay",
        "hproxy",
        "proxyscrape",
    ]
    assert by_address["198.51.100.1:1080"].sources == ["proxyscrape"]


def test_merging_fills_in_facts_rather_than_overwriting_them():
    """Neither feed is authoritative about an address they both list."""

    merged = proxy_ingest.merge(
        [
            ("proxyscrape", (endpoint(country="ES"),)),
            ("geonode", (endpoint(anonymity="elite", uptime_pct=94.5),)),
        ]
    )
    facts = merged[0].endpoint
    assert facts.country == "ES"
    assert facts.anonymity == "elite"
    assert facts.uptime_pct == 94.5


def test_agreement_outranks_every_other_signal():
    """Two feeds agreeing beats one feed's better-looking numbers.

    Deliberate: latency and uptime are somebody else's measurements of their
    own list, where "two independent projects saw this machine" is evidence
    about the machine.
    """

    ranked = proxy_ingest.rank(
        proxy_ingest.merge(
            [
                (
                    "proxyscrape",
                    (
                        endpoint(
                            ip="198.51.100.9",
                            https_ok=True,
                            anonymity="elite",
                            latency_ms=5,
                            uptime_pct=100.0,
                        ),
                        endpoint(ip="198.51.100.2", latency_ms=900),
                    ),
                ),
                ("hproxy", (endpoint(ip="198.51.100.2", latency_ms=900),)),
            ]
        )
    )
    assert [item.endpoint.ip for item in ranked] == ["198.51.100.2", "198.51.100.9"]


def test_tls_strict_capability_outranks_anonymity_and_latency():
    """The feed-side analogue of the checker, ranked where it belongs.

    Below agreement, because one publisher saying "this tunnels HTTPS" is
    still one publisher; above elite, because elite describes what the
    destination sees and says nothing about whether the tunnel is honest.
    """

    ranked = proxy_ingest.rank(
        proxy_ingest.merge(
            [
                (
                    "databay",
                    (
                        endpoint(ip="198.51.100.3", https_ok=True, latency_ms=900),
                        endpoint(
                            ip="198.51.100.4",
                            https_ok=False,
                            anonymity="elite",
                            latency_ms=5,
                        ),
                    ),
                )
            ]
        )
    )
    assert [item.endpoint.ip for item in ranked] == ["198.51.100.3", "198.51.100.4"]


def test_a_candidate_id_is_stable_for_one_address():
    """So a refusal survives the next refresh instead of being re-offered."""

    first = proxy_ingest.candidate_id("203.0.113.7:1080")
    assert first == proxy_ingest.candidate_id("203.0.113.7:1080")
    assert first != proxy_ingest.candidate_id("203.0.113.7:1081")
    assert first.startswith("px_")


def test_ingested_addresses_land_in_candidates_and_touch_no_chain(store_path):
    """The safety property: a pass changes what is on offer, not what routes."""

    chosen = ProxyEndpoint(
        url="socks5h://198.51.100.50:1080", label="198.51.100.50:1080"
    )
    save_proxy_chains(
        ProxyChains(
            proxies={"px_chosen": chosen},
            chains={
                "opencode": ProxyChain(
                    enabled=True, entries=(ProxyChainEntry(proxy="px_chosen"),)
                )
            },
        )
    )
    offered = [
        (
            proxy_ingest.candidate_id("203.0.113.7:1080"),
            ProxyEndpoint(
                url="socks5h://203.0.113.7:1080", source="feed", source_count=3
            ),
        )
    ]
    save_proxy_chains(load_proxy_chains().with_candidates(offered))

    after = load_proxy_chains()
    # The chain is byte-for-byte what it was.
    assert after.chains["opencode"].entries == (ProxyChainEntry(proxy="px_chosen"),)
    assert after.endpoint("px_chosen") is not None
    # And the new address is on offer, in no chain.
    assert len(after.candidates) == 1
    assert after.candidates[0] not in after.chains["opencode"].proxy_ids()


def test_a_refused_candidate_stays_refused_across_a_refresh(store_path):
    """The one piece of candidate state that is a security control.

    An address the checker found terminating TLS must not come back on the
    next pass as a fresh unknown row with an enabled button beside it.
    """

    proxy_id = proxy_ingest.candidate_id("203.0.113.7:1080")
    fresh = ProxyEndpoint(url="socks5h://203.0.113.7:1080", source="feed")
    save_proxy_chains(ProxyChains().with_candidates([(proxy_id, fresh)]))
    save_proxy_chains(
        load_proxy_chains().with_check(
            proxy_id, ProxyCheckRecord(at="now", ok=False, tls=TLS_INTERCEPTED)
        )
    )
    marked = load_proxy_chains().endpoint(proxy_id)
    assert marked is not None
    assert marked.refused is True

    # Same address, offered again by a later pass.
    save_proxy_chains(load_proxy_chains().with_candidates([(proxy_id, fresh)]))
    again = load_proxy_chains().endpoint(proxy_id)
    assert again is not None
    assert again.refused is True


def _client(handler) -> httpx.AsyncClient:
    """A real client over a stub transport.

    A hand-rolled object with a ``get`` would do less work here and would also
    not be an ``httpx.AsyncClient``, which is what this function is actually
    handed in production. Going through ``MockTransport`` keeps the redirect
    handling, the header merging and the exception types real.
    """

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_a_feed_that_errors_is_reported_not_raised():
    """One bad feed must not cost the pass the other six."""

    feed = a_feed()

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    async with _client(refuse) as client:
        result, endpoints = await proxy_ingest.fetch_feed(feed, client=client)
    assert result.ok is False
    assert endpoints == ()
    assert "ConnectError" in result.detail


@pytest.mark.asyncio
async def test_a_feed_that_answers_with_a_status_is_reported():
    feed = a_feed()

    async with _client(lambda request: httpx.Response(404, text="nope")) as client:
        result, endpoints = await proxy_ingest.fetch_feed(feed, client=client)
    assert result.ok is False
    assert "404" in result.detail
    assert endpoints == ()


@pytest.mark.asyncio
async def test_a_feed_that_changed_shape_says_so_rather_than_looking_healthy():
    """ "Answered, but nothing parsed" is a different problem from "down"."""

    feed = a_feed()
    body = json.dumps({"rows": []})

    async with _client(lambda request: httpx.Response(200, text=body)) as client:
        result, _ = await proxy_ingest.fetch_feed(feed, client=client)
    assert result.ok is False
    assert "changed shape" in result.detail


def _stored_feed(
    *,
    feed_id: str = "databay",
    name: str = "Databay",
    url: str = DATABAY_URL,
    parser: str = "databay",
    enabled: bool = True,
) -> CustomFeed:
    """One stored feed, in the shape the operator's own store holds it."""

    return CustomFeed(id=feed_id, name=name, url=url, parser=parser, enabled=enabled)


def test_a_feed_switched_off_is_not_read():
    """The switch is the consent, and it is the only thing that grants it."""

    store = ProxyChains(feeds=(_stored_feed(enabled=False),))
    assert proxy_ingest.enabled_feeds(store) == ()


def test_a_feed_this_install_has_no_reader_for_is_not_fetched():
    """Switched on, but nothing here could do anything with the body.

    A row whose parser is blank is one whose reader this install does not ship
    -- a hand-edited file, or a reader retired in a later release. Fetching it
    would be an outbound request that could not possibly produce an address.
    """

    store = ProxyChains(feeds=(_stored_feed(parser="", enabled=True),))
    assert proxy_ingest.enabled_feeds(store) == ()


def test_an_enabled_readable_feed_is_handed_over_as_something_fetchable():
    """The one case that does reach the network, and what it is handed."""

    store = ProxyChains(feeds=(_stored_feed(),))
    feeds = proxy_ingest.enabled_feeds(store)
    assert [feed.url for feed in feeds] == [DATABAY_URL]
    assert feeds[0].parser == "databay"
