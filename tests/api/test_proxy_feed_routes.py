"""The ingestion routes: feed selection, a fetch, and a checked promotion.

The two rules this surface must not break, and both are about the moment a
stranger's address gets near a credential:

* **Selecting a feed fetches nothing.** It records consent. The fetch is a
  separate press, and on a fresh install neither has happened.
* **A candidate is tested before it can be added, never after.** An address
  whose tunnel breaks certificate validation is refused at exactly the write
  that would put it in front of an API key -- the same refusal a typed address
  gets, reached by a different door.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from my_claude_code.application.proxy_check import ProxyCheckOutcome
from my_claude_code.application.proxy_ingest import IngestRun, candidate_id
from my_claude_code.config.proxy_chains import (
    TLS_INTERCEPTED,
    TLS_STRICT,
    ProxyChains,
    ProxyCheckRecord,
    ProxyEndpoint,
    load_proxy_chains,
    save_proxy_chains,
)
from my_claude_code.config.settings import Settings
from tests.api.support import create_test_app

CANDIDATE_URL = "socks5h://203.0.113.7:1080"
CANDIDATE_ID = candidate_id("203.0.113.7:1080")


@pytest.fixture(autouse=True)
def _isolate_chain_store(monkeypatch, tmp_path: Path):
    from my_claude_code.config import proxy_chains

    path = tmp_path / "proxy_chains.json"
    monkeypatch.setattr(proxy_chains, "proxy_chains_path", lambda: path)
    proxy_chains.reset_proxy_chains_cache()
    yield path
    proxy_chains.reset_proxy_chains_cache()


def _client() -> TestClient:
    settings = Settings.model_validate(
        {"model": "nvidia_nim/primary", "nvidia_nim_api_key": "nim-key"}
    )
    return TestClient(create_test_app(settings), client=("127.0.0.1", 50000))


def _offer_one() -> None:
    save_proxy_chains(
        ProxyChains().with_candidates(
            [
                (
                    CANDIDATE_ID,
                    ProxyEndpoint(
                        url=CANDIDATE_URL,
                        label="203.0.113.7:1080",
                        source="feed",
                        source_count=3,
                        sources=("proxyscrape", "hproxy", "databay"),
                    ),
                )
            ]
        )
    )


def test_a_fresh_install_has_no_feed_switched_on() -> None:
    """The shipped answer, and the reason nothing is ever fetched by itself."""

    payload = _client().get("/admin/api/proxy-chains").json()
    assert payload["feeds"], "the catalogue should still be offered"
    assert not any(feed["enabled"] for feed in payload["feeds"])
    assert payload["candidates"] == []
    assert payload["vocabulary"]["refresh"]["enabled"] is False


def test_selecting_a_feed_records_consent_and_fetches_nothing(monkeypatch) -> None:
    def explode(*args, **kwargs):  # pragma: no cover - the assertion is that
        raise AssertionError("selecting a feed must not contact it")

    monkeypatch.setattr("my_claude_code.api.admin_proxy_routes.ingest", explode)
    payload = (
        _client()
        .put("/admin/api/proxy-chains/feeds", json={"feeds": ["databay"]})
        .json()
    )
    enabled = [feed["id"] for feed in payload["feeds"] if feed["enabled"]]
    assert enabled == ["databay"]
    assert payload["candidates"] == []
    assert load_proxy_chains().feeds == ("databay",)


def test_an_unknown_feed_name_is_refused_loudly() -> None:
    """The store drops one silently; the API is a contract with the page."""

    response = _client().put(
        "/admin/api/proxy-chains/feeds", json={"feeds": ["not-a-feed"]}
    )
    assert response.status_code == 422
    assert "not-a-feed" in response.json()["detail"]


def test_fetching_with_no_feed_selected_is_refused_rather_than_silent() -> None:
    response = _client().post("/admin/api/proxy-chains/ingest")
    assert response.status_code == 422
    assert "contacts none of them" in response.json()["detail"]


def test_a_fetch_reports_which_feeds_answered(monkeypatch) -> None:
    from my_claude_code.application.proxy_ingest import FeedResult

    async def fake_ingest(**kwargs):
        _offer_one()
        return IngestRun(
            at="2026-09-15T00:00:00Z",
            results=(
                FeedResult("databay", "Databay (TLS-strict)", ok=True, count=40),
                FeedResult("geonode", "Geonode", ok=False, detail="answered 503"),
            ),
            offered=1,
            corroborated=1,
        )

    monkeypatch.setattr("my_claude_code.api.admin_proxy_routes.ingest", fake_ingest)
    client = _client()
    client.put("/admin/api/proxy-chains/feeds", json={"feeds": ["databay", "geonode"]})
    payload = client.post("/admin/api/proxy-chains/ingest").json()

    assert payload["ingest"]["corroborated"] == 1
    names = {entry["id"]: entry["ok"] for entry in payload["ingest"]["feeds"]}
    assert names == {"databay": True, "geonode": False}


def test_a_candidate_names_the_feeds_that_agreed_and_never_its_url() -> None:
    """``source_count`` is a score; the names are the answer.

    An operator deciding whether to put a stranger's machine in front of a
    credential should be able to see which projects saw it, on the row.
    """

    _offer_one()
    payload = _client().get("/admin/api/proxy-chains").json()
    row = payload["candidates"][0]
    assert row["source_count"] == 3
    assert [item["name"] for item in row["sources"]] == [
        "ProxyScrape",
        "HProxy",
        "Databay (TLS-strict)",
    ]
    assert row["label"] == "203.0.113.7:1080"
    assert "203.0.113.7:1080" not in str(row.get("url", ""))
    assert "url" not in row
    assert CANDIDATE_URL not in payload["candidates"][0].values()


def test_an_ingested_address_is_not_in_any_chain() -> None:
    """The line this whole release is not allowed to cross."""

    _offer_one()
    payload = _client().get("/admin/api/proxy-chains").json()
    for provider in payload["providers"]:
        assert provider["chain"] is None
    assert load_proxy_chains().chain("nvidia_nim") is None


def test_adding_a_candidate_checks_it_first(monkeypatch) -> None:
    calls: list[tuple] = []

    async def fake_check(ids, destinations, **kwargs):
        calls.append((tuple(ids), dict(destinations)))
        return {
            CANDIDATE_ID: ProxyCheckOutcome(
                label="203.0.113.7:1080",
                record=ProxyCheckRecord(
                    at="now", ok=True, latency_ms=120, tls=TLS_STRICT
                ),
            )
        }

    monkeypatch.setattr(
        "my_claude_code.api.admin_proxy_routes.check_endpoints", fake_check
    )
    _offer_one()
    payload = (
        _client()
        .post(
            "/admin/api/proxy-chains/candidates/bulk",
            json={
                "action": "add",
                "provider": "nvidia_nim",
                "proxies": [CANDIDATE_ID],
            },
        )
        .json()
    )

    assert calls and calls[0][0] == (CANDIDATE_ID,)
    # Against that provider's own host, never a third-party echo service.
    assert calls[0][1][CANDIDATE_ID].startswith("https://")
    store = load_proxy_chains()
    chain = store.chain("nvidia_nim")
    assert chain is not None
    assert chain.proxy_ids() == (CANDIDATE_ID,)
    # Added, but not armed: adding an address is not the same act as turning
    # the chain on.
    assert chain.enabled is False
    assert store.candidates == ()
    assert payload["bulk"]["counts"]["added"] == 1
    assert payload["bulk"]["results"][0]["outcome"] == "added"


def test_an_intercepting_candidate_is_refused_and_stays_a_candidate(
    monkeypatch,
) -> None:
    """The security control, reached through the feed door.

    The refusal has to hold here exactly as it does for a typed address: this
    is the write that would put a machine reading the plaintext in front of an
    API key.

    It is reported per address rather than as a failed request, because in a
    bulk add a refusal beside eleven successes is the ordinary outcome and a
    422 would throw the other eleven away.
    """

    async def fake_check(ids, destinations, **kwargs):
        return {
            CANDIDATE_ID: ProxyCheckOutcome(
                label="203.0.113.7:1080",
                record=ProxyCheckRecord(
                    at="now",
                    ok=False,
                    tls=TLS_INTERCEPTED,
                    detail="breaks certificate validation",
                ),
            )
        }

    monkeypatch.setattr(
        "my_claude_code.api.admin_proxy_routes.check_endpoints", fake_check
    )
    _offer_one()
    response = _client().post(
        "/admin/api/proxy-chains/candidates/bulk",
        json={"action": "add", "provider": "nvidia_nim", "proxies": [CANDIDATE_ID]},
    )

    assert response.status_code == 200
    bulk = response.json()["bulk"]
    assert bulk["counts"]["refused"] == 1
    assert bulk["counts"]["added"] == 0
    assert "certificate validation" in bulk["results"][0]["detail"]
    store = load_proxy_chains()
    assert store.chain("nvidia_nim") is None
    assert store.candidates == (CANDIDATE_ID,)


def test_typing_an_address_a_feed_also_offered_stops_it_being_on_offer() -> None:
    """On offer and chosen are different states; one address is not both.

    The catalogue files an address by id, so typing one a feed had already
    listed reuses that row rather than duplicating it -- and without this the
    same machine would sit in a chain and still be advertised as something
    nobody had picked.
    """

    _offer_one()
    client = _client()
    payload = client.put(
        "/admin/api/proxy-chains",
        json={
            "provider": "nvidia_nim",
            "enabled": False,
            "entries": [{"url": CANDIDATE_URL}],
        },
    ).json()

    assert payload["candidates"] == []
    store = load_proxy_chains()
    assert store.candidates == ()
    chain = store.chain("nvidia_nim")
    assert chain is not None and chain.proxy_ids() == (CANDIDATE_ID,)


def test_a_candidate_that_is_no_longer_on_offer_is_reported_not_added() -> None:
    """A stale selection names an address a refetch has already dropped."""

    response = _client().post(
        "/admin/api/proxy-chains/candidates/bulk",
        json={"action": "add", "provider": "nvidia_nim", "proxies": ["px_gone"]},
    )
    assert response.status_code == 200
    bulk = response.json()["bulk"]
    assert bulk["counts"]["gone"] == 1
    assert load_proxy_chains().chain("nvidia_nim") is None


def test_adding_to_an_unconfigured_provider_is_refused() -> None:
    _offer_one()
    response = _client().post(
        "/admin/api/proxy-chains/candidates/bulk",
        json={"action": "add", "provider": "not_a_provider", "proxies": [CANDIDATE_ID]},
    )
    assert response.status_code == 404


def test_one_address_is_the_bulk_route_with_one_element() -> None:
    """There is no second, per-row write path, and that is the point.

    6.24.0 exists because the Models page kept one: the single-row path and the
    bulk path diverged, and the single-row one silently skipped the counters.
    """

    client = _client()
    assert (
        client.post(
            "/admin/api/proxy-chains/candidates/add",
            json={"provider": "nvidia_nim", "proxy": CANDIDATE_ID},
        ).status_code
        == 404
    )


def test_discarding_offers_touches_no_chain_and_can_be_undone(monkeypatch) -> None:
    """Discarding says "stop showing me this", not "remove it from a chain"."""

    _offer_one()
    client = _client()
    discarded = client.post(
        "/admin/api/proxy-chains/candidates/bulk",
        json={"action": "discard", "proxies": [CANDIDATE_ID]},
    ).json()

    assert discarded["bulk"]["counts"]["discarded"] == 1
    assert load_proxy_chains().candidates == ()

    token = discarded["bulk"]["undo_token"]
    assert token
    restored = client.post(
        "/admin/api/proxy-chains/candidates/undo", json={"token": token}
    )
    assert restored.status_code == 200
    assert load_proxy_chains().candidates == (CANDIDATE_ID,)


def test_a_batch_is_one_write_and_reports_every_address(monkeypatch) -> None:
    """Three addresses, one save, three outcomes, and the cap respected.

    The per-address version of this read the whole document, derived a
    replacement and wrote it once per address -- the read-modify-write race
    6.7.0 found on the Models page in exactly this shape.
    """

    ids = [candidate_id(f"203.0.113.{n}:1080") for n in (11, 12, 13)]
    save_proxy_chains(
        ProxyChains().with_candidates(
            [
                (
                    proxy_id,
                    ProxyEndpoint(
                        url=f"socks5h://203.0.113.{n}:1080",
                        label=f"203.0.113.{n}:1080",
                        source="feed",
                        source_count=1,
                        sources=("proxyscrape",),
                    ),
                )
                for proxy_id, n in zip(ids, (11, 12, 13), strict=True)
            ]
        )
    )

    saves: list[int] = []
    real_save = save_proxy_chains

    def counting_save(chains, path=None):
        saves.append(1)
        return real_save(chains, path)

    async def fake_check(ids_asked, destinations, **kwargs):
        # The middle one is a machine reading the plaintext; the last one is
        # simply down. Both are ordinary things in a public list.
        out = {}
        for index, proxy_id in enumerate(ids_asked):
            out[proxy_id] = ProxyCheckOutcome(
                label=f"label-{index}",
                record=ProxyCheckRecord(
                    at="now",
                    ok=index == 0,
                    latency_ms=99 if index == 0 else None,
                    tls=TLS_INTERCEPTED if index == 1 else TLS_STRICT,
                    detail="" if index == 0 else "no answer",
                ),
            )
        return out

    monkeypatch.setattr(
        "my_claude_code.api.admin_proxy_routes.check_endpoints", fake_check
    )
    monkeypatch.setattr(
        "my_claude_code.api.admin_proxy_routes.save_proxy_chains", counting_save
    )

    payload = (
        _client()
        .post(
            "/admin/api/proxy-chains/candidates/bulk",
            json={"action": "add", "provider": "nvidia_nim", "proxies": ids},
        )
        .json()
    )

    counts = payload["bulk"]["counts"]
    assert counts["added"] == 1
    assert counts["refused"] == 1
    assert counts["benched"] == 1
    # One save for the batch, not one per address.
    assert saves == [1]
    chain = load_proxy_chains().chain("nvidia_nim")
    assert chain is not None
    assert chain.proxy_ids() == (ids[0], ids[2])


def test_a_subscription_login_needs_its_acknowledgement_by_this_door_too(
    monkeypatch,
) -> None:
    """The gate the PUT holds, held on the bulk add as well.

    A subscription login uses the operator's personal account rather than a
    metered key, and moving it between source addresses is likelier to be
    flagged there than anywhere else. A chain gains entries through two doors;
    both ask.
    """

    # The only provider this fixture configures, read as a subscription login
    # for the length of this test: the gate is about what a provider IS, and
    # standing up a second one here would test the fixture rather than it.
    monkeypatch.setattr(
        "my_claude_code.api.admin_proxy_routes.OAUTH_PROVIDER_IDS",
        frozenset({"nvidia_nim"}),
    )
    _offer_one()

    response = _client().post(
        "/admin/api/proxy-chains/candidates/bulk",
        json={"action": "add", "provider": "nvidia_nim", "proxies": [CANDIDATE_ID]},
    )

    assert response.status_code == 422
    assert "personal subscription" in response.json()["detail"]
    assert load_proxy_chains().chain("nvidia_nim") is None
    # And it stays on offer rather than vanishing into a chain that refused it.
    assert load_proxy_chains().candidates == (CANDIDATE_ID,)
