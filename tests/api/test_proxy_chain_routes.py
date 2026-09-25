"""The two routes behind the Proxying page.

A chain is an ordered list of egress addresses with a per-entry pause, which
is not expressible as a flat env-key-to-string map, so it writes a JSON
document rather than settings keys -- the same argument the harness tier
routes make, and the same reason it cannot go through
``/admin/api/config/apply``.

The sharp edge these tests exist for is the one the store cannot close on its
own: a proxy URL may carry a password, and this is the surface that would leak
it.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from my_claude_code.config.settings import Settings
from my_claude_code.core.failures import FailureKind
from tests.api.support import create_test_app

SECRET_URL = "socks5h://alice:hunter2@203.0.113.7:1080"
SECOND_URL = "http://198.51.100.9:8080"


@pytest.fixture(autouse=True)
def _isolate_chain_store(monkeypatch, tmp_path: Path):
    """Never the real config directory: these routes write a file."""

    from my_claude_code.config import proxy_chains

    path = tmp_path / "proxy_chains.json"
    monkeypatch.setattr(proxy_chains, "proxy_chains_path", lambda: path)
    proxy_chains.reset_proxy_chains_cache()
    yield path
    proxy_chains.reset_proxy_chains_cache()


def _settings(proxy: str = "") -> Settings:
    # One configured provider is enough for every assertion here, and keeping
    # the install small keeps the payload readable when one fails. The proxy is
    # passed by its env alias because that is the only name the field accepts.
    return Settings.model_validate(
        {
            "model": "nvidia_nim/primary",
            "nvidia_nim_api_key": "nim-key",
            "NVIDIA_NIM_PROXY": proxy,
        }
    )


def _client(settings: Settings | None = None) -> TestClient:
    # Loopback, as every admin route requires: the check is on the client host
    # and TestClient's default is not one.
    return TestClient(
        create_test_app(settings or _settings()), client=("127.0.0.1", 50000)
    )


def _provider(payload: dict, provider_id: str = "nvidia_nim") -> dict:
    match = [
        entry for entry in payload["providers"] if entry["provider_id"] == provider_id
    ]
    assert match, f"{provider_id} is not on the page: " + ", ".join(
        entry["provider_id"] for entry in payload["providers"]
    )
    return match[0]


def test_the_page_lists_only_configured_providers() -> None:
    """Fifty-six cards for the four providers an operator uses is not a page.

    And a chain on a provider with no credential could never route anything,
    so it is not a chain, it is a text box that lies.
    """

    payload = _client().get("/admin/api/proxy-chains").json()
    listed = {entry["provider_id"] for entry in payload["providers"]}

    assert "nvidia_nim" in listed
    assert "anthropic" not in listed
    assert "open_router" not in listed


def test_the_vocabulary_offers_the_four_rotation_policies_and_no_fifth() -> None:
    payload = _client().get("/admin/api/proxy-chains").json()

    assert [policy["id"] for policy in payload["vocabulary"]["policies"]] == [
        "single",
        "round_robin",
        "least_used",
        "failover",
    ]
    assert payload["vocabulary"]["default_policy"] == "failover"
    # The one thing four names do not say on their own, and the point of the
    # feature for a provider metered by address.
    help_text = {
        policy["id"]: policy["help"] for policy in payload["vocabulary"]["policies"]
    }
    assert "multiplies" in help_text["round_robin"]
    assert "until it fails" in help_text["failover"]


def test_every_kind_is_offered_and_exactly_two_are_refused() -> None:
    """The page offers the whole vocabulary, not a hand-kept subset of it.

    Counted against the enum rather than against a literal: a kind added to
    ``FailureKind`` and not reaching this page is a trigger an operator cannot
    choose, and a number in a test is exactly what stops saying so.
    """

    payload = _client().get("/admin/api/proxy-chains").json()
    kinds = payload["vocabulary"]["kinds"]

    assert len(kinds) == len(FailureKind)
    refused = {kind["id"] for kind in kinds if kind["state"] == "refused"}
    recommended = [kind["id"] for kind in kinds if kind["state"] == "recommended"]
    assert refused == {"authentication", "permission"}
    assert recommended == ["quota", "rate_limit", "timeout"]
    assert all(kind["reason"] for kind in kinds if kind["state"] == "refused")


def test_the_get_never_returns_a_proxy_password() -> None:
    """The whole response body is searched, not just the field it should be in.

    A leak that reaches a label, a title or a debug field is the same leak.
    """

    client = _client()
    client.put(
        "/admin/api/proxy-chains",
        json={
            "provider": "nvidia_nim",
            "enabled": True,
            "entries": [{"url": SECRET_URL}],
        },
    ).raise_for_status()

    response = client.get("/admin/api/proxy-chains")
    body = response.text

    assert "hunter2" not in body
    assert "alice" not in body
    entry = _provider(response.json())["chain"]["entries"][0]
    assert entry["label"] == "203.0.113.7:1080"
    assert entry["scheme"] == "socks5h"
    assert entry["proxy"].startswith("px_")


def test_an_unedited_entry_travels_back_by_id_and_keeps_its_url() -> None:
    """Referencing by id *is* the "unchanged" sentinel.

    The page never holds the URL, so there is no code path that needs the
    password on the client -- and therefore none that can leak it.
    """

    client = _client()
    first = client.put(
        "/admin/api/proxy-chains",
        json={"provider": "nvidia_nim", "entries": [{"url": SECRET_URL}]},
    ).json()
    proxy_id = _provider(first)["chain"]["entries"][0]["proxy"]

    second = client.put(
        "/admin/api/proxy-chains",
        json={
            "provider": "nvidia_nim",
            "enabled": True,
            "entries": [{"proxy": proxy_id}, {"direct": True}],
        },
    ).json()

    chain = _provider(second)["chain"]
    assert chain["enabled"] is True
    assert [entry["proxy"] for entry in chain["entries"]] == [proxy_id, ""]
    assert chain["entries"][1]["direct"] is True

    from my_claude_code.config.proxy_chains import load_proxy_chains

    endpoint = load_proxy_chains().endpoint(proxy_id)
    assert endpoint is not None
    assert endpoint.url == SECRET_URL


def test_the_env_proxy_becomes_entry_one_without_ever_being_sent_to_the_page() -> None:
    """The upgrade story, and the reason it needs a server-side sentinel.

    "Start from your existing proxy" cannot be a URL the page posts back: the
    page has never been told that URL, deliberately, and telling it so that it
    could echo it would undo the whole point. So the entry says `inherit` and
    the server resolves it from the settings it already holds. The `.env` key
    keeps its value either way.
    """

    client = _client(_settings(proxy=SECRET_URL))

    payload = client.put(
        "/admin/api/proxy-chains",
        json={
            "provider": "nvidia_nim",
            "enabled": True,
            "entries": [{"inherit": True}, {"direct": True}],
        },
    ).json()

    chain = _provider(payload)["chain"]
    assert chain["entries"][0]["label"] == "203.0.113.7:1080"
    assert chain["entries"][0]["scheme"] == "socks5h"

    from my_claude_code.config.proxy_chains import load_proxy_chains

    stored = load_proxy_chains()
    endpoint = stored.endpoint(chain["entries"][0]["proxy"])
    assert endpoint is not None
    assert endpoint.url == SECRET_URL


def test_inheriting_a_proxy_a_provider_does_not_have_is_refused() -> None:
    response = _client().put(
        "/admin/api/proxy-chains",
        json={"provider": "nvidia_nim", "entries": [{"inherit": True}]},
    )

    assert response.status_code == 422
    assert "has none" in response.json()["detail"]


def test_a_chain_is_not_capped_unless_the_operator_capped_it() -> None:
    """Thirteen entries are accepted on a shipped install, and three hundred.

    Replaces ``test_a_chain_longer_than_the_cap_is_refused``, which asserted
    that a fourteenth entry was refused with "at most 12". That ceiling was
    about construction cost -- a client, a rate limiter and a recovery ladder
    per rung per credential, all built eagerly -- and 7.19.0 builds a rung's
    leaf on its first use. What is left is ``PROXY_CHAIN_MAX_ENTRIES``, which
    ships as 0 (no limit) and is the operator's to set.
    """

    response = _client().put(
        "/admin/api/proxy-chains",
        json={
            "provider": "nvidia_nim",
            "entries": [{"url": f"http://198.51.100.9:{9000 + i}"} for i in range(300)],
        },
    )

    assert response.status_code == 200
    chain = _provider(response.json(), "nvidia_nim")["chain"]
    assert len(chain["entries"]) == 300


def test_a_chain_longer_than_the_operators_own_cap_is_refused() -> None:
    """A ceiling the operator set is enforced with a message, not a truncation.

    The number in the message is theirs, and the message says where it came
    from -- an operator who finds a chain refused has to be able to find the
    setting that refused it.
    """

    settings = Settings.model_validate(
        {
            "model": "nvidia_nim/primary",
            "nvidia_nim_api_key": "nim-key",
            "PROXY_CHAIN_MAX_ENTRIES": 12,
        }
    )
    response = _client(settings).put(
        "/admin/api/proxy-chains",
        json={
            "provider": "nvidia_nim",
            "entries": [{"url": SECOND_URL}] * 13,
        },
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "at most 12" in detail
    assert "PROXY_CHAIN_MAX_ENTRIES" in detail


def test_a_destructive_trigger_is_refused_with_its_reason() -> None:
    """Rotating on a 401 burns the whole chain and benches every proxy in it.

    The store drops the word silently because it is also reached by a file a
    human edited; the API is a contract with a page that cannot send it, so
    anything that does is a caller worth telling.
    """

    response = _client().put(
        "/admin/api/proxy-chains",
        json={
            "provider": "nvidia_nim",
            "on": ["quota", "authentication"],
            "entries": [{"url": SECOND_URL}],
        },
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "authentication" in detail
    assert "burns the whole chain" in detail


def test_an_unusable_proxy_url_names_the_entry_that_is_wrong() -> None:
    response = _client().put(
        "/admin/api/proxy-chains",
        json={
            "provider": "nvidia_nim",
            "entries": [{"url": SECOND_URL}, {"url": "203.0.113.7:1080"}],
        },
    )

    assert response.status_code == 422
    assert "Entry 2" in response.json()["detail"]


def test_the_switch_bound_is_refused_outside_the_one_to_five_range() -> None:
    for bound in (0, 6):
        response = _client().put(
            "/admin/api/proxy-chains",
            json={
                "provider": "nvidia_nim",
                "max_switches": bound,
                "entries": [{"url": SECOND_URL}],
            },
        )
        assert response.status_code == 422, bound
        assert "between 1 and 5" in response.json()["detail"]


def test_a_provider_with_no_credential_can_not_be_given_a_chain() -> None:
    response = _client().put(
        "/admin/api/proxy-chains",
        json={"provider": "anthropic", "entries": [{"url": SECOND_URL}]},
    )

    assert response.status_code == 404
    assert "configured provider" in response.json()["detail"]


def test_removing_a_chain_puts_the_provider_back_on_its_env_proxy() -> None:
    """Removal is a third state, not an empty chain.

    "This provider uses ``<PROVIDER>_PROXY`` exactly as it always has" and
    "this provider has a chain that is switched off" are different answers and
    the card has to be able to render both.
    """

    client = _client(_settings(proxy="http://198.51.100.9:8080"))
    client.put(
        "/admin/api/proxy-chains",
        json={"provider": "nvidia_nim", "entries": [{"url": SECOND_URL}]},
    ).raise_for_status()
    assert _provider(client.get("/admin/api/proxy-chains").json())["chain"]

    payload = client.put(
        "/admin/api/proxy-chains", json={"provider": "nvidia_nim", "remove": True}
    ).json()
    provider = _provider(payload)

    assert provider["chain"] is None
    assert provider["inherited_label"] == "198.51.100.9:8080"
    assert provider["env_var"] == "NVIDIA_NIM_PROXY"


def test_the_env_proxy_is_reported_as_a_label_and_never_as_a_url() -> None:
    """A static ``<PROVIDER>_PROXY`` can carry a password too."""

    client = _client(_settings(proxy=SECRET_URL))

    response = client.get("/admin/api/proxy-chains")

    assert "hunter2" not in response.text
    provider = _provider(response.json())
    assert provider["inherited_label"] == "203.0.113.7:1080"
    assert provider["inherited_scheme"] == "socks5h"


# ``test_saving_a_chain_does_not_republish_the_provider_generation`` lived here
# and was removed when ingestion shipped. It pinned the *first* release's
# deliberate choice not to republish, which the release that added the runtime
# seam reversed on purpose -- and ``test_saving_a_chain_republishes_the_provider
# _generation`` below has asserted the opposite ever since. It survived that
# release only because it was written as a grep for the string "replace(" over
# the module's source rather than as an assertion about behaviour, and it
# finally failed on an unrelated ``dataclasses.replace`` import. A source grep
# standing in for a behavioural claim is worth deleting rather than narrowing:
# the behaviour it meant to describe is already pinned, correctly, ten lines
# down.


def test_the_routes_are_loopback_only() -> None:
    off_box = TestClient(create_test_app(_settings()), client=("10.0.0.9", 50000))

    assert off_box.get("/admin/api/proxy-chains").status_code == 403
    assert (
        off_box.put(
            "/admin/api/proxy-chains", json={"provider": "nvidia_nim"}
        ).status_code
        == 403
    )


def test_saving_a_chain_republishes_the_provider_generation(monkeypatch) -> None:
    """A chain that is stored but not published is a chain that does nothing.

    A proxy is read once, in a provider's constructor, and baked into a
    long-lived client; a chain is read in the same place. So the write has to
    rebuild the generation, or a saved chain would sit inert until the next
    restart -- which is exactly what the release that shipped this page did on
    purpose, because there was no runtime to tell.
    """

    from my_claude_code.runtime.application import ApplicationRuntime

    seen: list[str] = []
    swept: list[bool] = []

    async def _reload(
        self,
        reason: str,
        *,
        refresh_provider_id: str | None = None,
        sweep: bool = True,
    ):
        seen.append(reason)
        swept.append(sweep)
        return {}

    monkeypatch.setattr(ApplicationRuntime, "reload_providers", _reload)

    client = _client()
    response = client.put(
        "/admin/api/proxy-chains",
        json={
            "provider": "nvidia_nim",
            "enabled": True,
            "policy": "round_robin",
            "scope": "provider",
            "max_switches": 2,
            "on": ["quota", "rate_limit"],
            "entries": [{"url": SECOND_URL}, {"direct": True}],
        },
    )

    assert response.status_code == 200
    assert seen == ["proxy_chains"]

    # And removing one, which is just as much a change to what routes.
    client.put(
        "/admin/api/proxy-chains", json={"provider": "nvidia_nim", "remove": True}
    )
    assert seen == ["proxy_chains", "proxy_chains"]
    # 7.27.0: the generation is still replaced -- that is what makes the new
    # chain the one that routes -- but neither save asks for the blanket
    # /models sweep of every configured provider that used to come with it.
    assert swept == [False, False]


def test_every_entry_reports_the_health_the_pools_measured(monkeypatch) -> None:
    """Live per-entry health, out of the ledger the running pools write to.

    A registry rather than a walk of the live provider tree: the answer
    outlives the generation replace a chain edit performs, and nothing on the
    request path has to be reachable from an admin request. An address no
    request has gone through says so, which is what the card renders as "not
    checked yet".
    """

    from my_claude_code.core.proxy_rotation import PROXY_HEALTH, reset_proxy_health

    reset_proxy_health()
    client = _client()
    client.put(
        "/admin/api/proxy-chains",
        json={
            "provider": "nvidia_nim",
            "enabled": True,
            "policy": "failover",
            "scope": "provider",
            "max_switches": 2,
            "on": ["quota"],
            "entries": [{"url": SECOND_URL}, {"direct": True}],
        },
    )

    try:
        PROXY_HEALTH.note_acquired("nvidia_nim", "198.51.100.9:8080")
        PROXY_HEALTH.note_success("nvidia_nim", "198.51.100.9:8080")

        entries = _provider(client.get("/admin/api/proxy-chains").json())["chain"][
            "entries"
        ]
        assert entries[0]["health"]["state"] == "healthy"
        assert entries[0]["health"]["checked"] is True
        assert entries[1]["health"]["state"] == "unknown"
        assert entries[1]["health"]["checked"] is False
    finally:
        reset_proxy_health()


def test_candidates_carry_measured_speed_and_rate() -> None:
    """MCC's own measurement travels beside the feed's number, never as it.

    7.54.0: ``speed`` is the ledger's score for the provider the candidate
    was checked against; ``feed_latency_ms`` is what the feed published.
    A chain entry carries ``speed`` for its own chain's provider.
    """

    from my_claude_code.application.proxy_ingest import candidate_id
    from my_claude_code.config.proxy_chains import (
        ProxyChain,
        ProxyChainEntry,
        ProxyChains,
        ProxyEndpoint,
        ProxyFeedFacts,
        save_proxy_chains,
    )
    from my_claude_code.core.proxy_speed import KIND_CHECK, PROXY_SPEED, SpeedSample

    label = "203.0.113.7:1080"
    proxy_id = candidate_id(label)
    store = ProxyChains().with_candidates(
        [
            (
                proxy_id,
                ProxyEndpoint(
                    url="socks5h://203.0.113.7:1080",
                    label=label,
                    source="feed",
                    feed=ProxyFeedFacts(latency_ms=42),
                    checked_for="nvidia_nim",
                ),
            )
        ]
    )
    store, typed_id = store.add_endpoint(SECOND_URL)
    store = store.with_chain(
        "nvidia_nim",
        ProxyChain(enabled=True, entries=(ProxyChainEntry(proxy=typed_id),)),
    )
    save_proxy_chains(store)
    import time

    now = time.time()
    for ok in (True, True, True, False):
        PROXY_SPEED.note(
            label,
            "nvidia_nim",
            SpeedSample(
                kind=KIND_CHECK,
                at=now,
                ok=ok,
                connect_ms=400,
                tunnel_ms=300,
                tls_ms=500,
            ),
        )
    PROXY_SPEED.note(
        "198.51.100.9:8080",
        "nvidia_nim",
        SpeedSample(kind=KIND_CHECK, at=now, ok=True, connect_ms=100, tunnel_ms=50),
    )

    payload = _client().get("/admin/api/proxy-chains").json()
    row = next(item for item in payload["candidates"] if item["proxy"] == proxy_id)

    assert row["feed_latency_ms"] == 42
    assert row["latency_ms"] == 42
    speed = row["speed"]
    assert speed["setup_ms"] == 1200
    assert (speed["successes"], speed["samples"]) == (3, 4)
    assert speed["success_rate"] == round(4 / 6, 4)
    assert speed["live_samples"] == 0
    assert speed["ttft_factor"] == 1.0
    # F is the connect timeout the operator runs with (10 s shipped).
    assert speed["rank_key"] == round(1200 + (2 / 6) / (4 / 6) * 10000, 1)
    # 3 of 4 is neither flaky (r >= 0.5) nor working (r < 0.8): no label.
    assert speed["state"] == ""

    entry = _provider(payload)["chain"]["entries"][0]
    assert entry["speed"]["setup_ms"] == 150
    assert entry["speed"]["samples"] == 1
    assert entry["speed"]["state"] == "working"
