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
            "/admin/api/proxy-chains/candidates/add",
            json={"provider": "nvidia_nim", "proxy": CANDIDATE_ID},
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
    assert payload["checked"][CANDIDATE_ID]["tls"] == TLS_STRICT


def test_an_intercepting_candidate_is_refused_and_stays_a_candidate(
    monkeypatch,
) -> None:
    """The security control, reached through the feed door.

    The refusal has to hold here exactly as it does for a typed address: this
    is the write that would put a machine reading the plaintext in front of an
    API key.
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
        "/admin/api/proxy-chains/candidates/add",
        json={"provider": "nvidia_nim", "proxy": CANDIDATE_ID},
    )

    assert response.status_code == 422
    assert "certificate validation" in response.json()["detail"]
    store = load_proxy_chains()
    assert store.chain("nvidia_nim") is None
    assert store.candidates == (CANDIDATE_ID,)


def test_a_candidate_that_is_no_longer_on_offer_is_refused() -> None:
    response = _client().post(
        "/admin/api/proxy-chains/candidates/add",
        json={"provider": "nvidia_nim", "proxy": "px_gone"},
    )
    assert response.status_code == 422
    assert "not on offer" in response.json()["detail"]


def test_adding_to_an_unconfigured_provider_is_refused() -> None:
    _offer_one()
    response = _client().post(
        "/admin/api/proxy-chains/candidates/add",
        json={"provider": "not_a_provider", "proxy": CANDIDATE_ID},
    )
    assert response.status_code == 404
