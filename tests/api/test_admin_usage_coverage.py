"""The coverage rule and the read-all walk, proved on the route.

``specs/PR-USAGE-LAYOUTS-SPEC.md`` §2 and §5.3. What this file proves:

1. **Every configured provider gets a row, including custom ones** — the list
   is derived from configuration, never a hard-coded table.
2. **Each row is in exactly one of the eight states**, with a fixture that puts
   one provider in each: a custom provider matching nothing, one matching a
   models.dev host, one on a recognised gateway family, one with an expired
   plan, one failed, one not read yet, one needing a credential MCC does not
   hold, and one measured.
3. **`POST /admin/api/usage/read-all` walks only what the gate allows** — a
   provider inside its own deadline is refused rather than asked, one failure
   does not stop the walk, and a hidden tab stops it mid-way.
4. **The route reports the layout and never branches on it** (§3).

**LIVE CALLS: 0.** Every read goes to an ``httpx.MockTransport`` in this
process; the recognisers read the local models.dev cache on disk and a declared
table. Nothing here opens a socket.
"""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from my_claude_code.api.admin_usage_routes import (
    STATE_NEEDS_CREDENTIAL,
    STATE_POSSIBLE_ENDPOINT,
    USAGE_STATE_ORDER,
)
from my_claude_code.config.usage_sources import (
    GATEWAY_FAMILIES,
    families_for_base_url,
    source_for,
)
from my_claude_code.core.usage.reading import READINGS
from my_claude_code.core.usage_capture import OBSERVER
from my_claude_code.providers.runtime import usage_reads
from tests.api.support import create_test_app

LOCAL_ORIGIN = {"Origin": "http://127.0.0.1:8082"}

#: The eight states, as the page words them. The order is the list order.
EIGHT_STATES = {
    "measured",
    "stale",
    "failed",
    "unavailable",
    STATE_NEEDS_CREDENTIAL,
    STATE_POSSIBLE_ENDPOINT,
    "not_measured",
    "unsupported",
}


class FakeHost:
    """A fake upstream that records every call and answers one body."""

    def __init__(self, body: Any, headers: dict[str, str] | None = None) -> None:
        self.body = body
        self.headers = headers or {}
        self.calls: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        content = b"" if self.body is None else json.dumps(self.body).encode()
        return httpx.Response(
            self.status,
            content=content,
            headers={"content-type": "application/json", **self.headers},
        )

    status = 200

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


def _patched_httpx(host: FakeHost) -> Any:
    class _Module:
        Headers = httpx.Headers
        Response = httpx.Response
        Request = httpx.Request
        TimeoutException = httpx.TimeoutException

        @staticmethod
        def AsyncClient(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            return host.client()

    return _Module()


def _fake_credential() -> Any:
    from my_claude_code.core.usage.reading import UsageCredential

    return UsageCredential(token="sk-fake-0123456789", label="sk-f…6789")


@pytest.fixture(autouse=True)
def _clean_board(monkeypatch: pytest.MonkeyPatch):
    OBSERVER.reset()
    READINGS.reset()
    monkeypatch.setattr(
        usage_reads,
        "resolve_credential",
        lambda endpoint, settings: _fake_credential(),
    )
    yield
    OBSERVER.reset()
    READINGS.reset()


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_test_app(), client=("127.0.0.1", 50000))


@pytest.fixture
def fake_host(monkeypatch: pytest.MonkeyPatch) -> FakeHost:
    host = FakeHost(
        body={
            "is_available": True,
            "balance_infos": [
                {
                    "currency": "USD",
                    "total_balance": "12.34",
                    "granted_balance": "0.00",
                    "topped_up_balance": "12.34",
                }
            ],
        }
    )
    monkeypatch.setattr(usage_reads, "httpx", _patched_httpx(host))
    return host


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, int]]:
    """The installed providers, as the coverage walk sees them.

    Replaced wholesale rather than written to a real config: the rule under test
    is "every CONFIGURED provider appears", so the fixture is the configuration
    and the assertion is about what the route did with it. The names are the
    user's own install, including three custom providers and a provider that
    needs a credential.
    """

    rows = [
        ("anthropic_oauth", "Claude subscription", 1),
        ("chatgpt_oauth", "ChatGPT / Codex subscription", 1),
        ("commandcode", "Command Code", 1),
        ("open_router", "OpenRouter", 1),
        ("deepseek", "DeepSeek", 1),
        ("opencode_go", "OpenCode Go", 1),
        ("kimi", "Kimi (Moonshot)", 1),
        ("kimi_coding", "Kimi Coding", 1),
        ("nous_portal", "Nous Portal (HyperCharm)", 2),
        ("novita", "Novita AI", 1),
        ("cline", "Cline", 1),
        ("copilot", "GitHub Copilot", 1),
        ("custom_91aa", "aihubmix", 2),
        ("custom_77bb", "A local LiteLLM", 1),
        ("custom_55cc", "Somebody's own gateway", 3),
    ]
    monkeypatch.setattr(
        "my_claude_code.api.admin_usage_routes._configured_provider_ids",
        lambda: list(rows),
    )
    return rows


@pytest.fixture
def models_dev(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A small models.dev cache this test owns.

    The recogniser reads a FILE, so the file is the fixture: a test that read
    the developer's own cache would pass or fail depending on which providers
    they happen to have installed.
    """

    from my_claude_code.providers.runtime import models_dev as models_dev_module
    from tests.support.usage_models_dev_fixture import write_cache

    path = write_cache(tmp_path)
    monkeypatch.setattr(models_dev_module, "_api_hosts_cache", {})
    monkeypatch.setattr(models_dev_module, "models_dev_cache_path", lambda: path)
    return path


@pytest.fixture
def base_urls(monkeypatch: pytest.MonkeyPatch, models_dev: Path) -> dict[str, str]:
    """Where each custom provider points, and what the recognisers make of it."""

    urls = {
        "custom_91aa": "https://aihubmix.com/v1",
        "custom_77bb": "https://litellm.internal.example.com/v1",
        "custom_55cc": "https://gateway.somebody.example/v1",
    }
    monkeypatch.setattr(
        "my_claude_code.api.admin_usage_routes._base_url_for",
        lambda provider_id: urls.get(provider_id, ""),
    )
    return urls


def _coverage(payload: dict, provider_id: str) -> dict:
    return next(row for row in payload["coverage"] if row["provider_id"] == provider_id)


# ────────────────────────────────────────── the coverage rule, end to end ────


def test_every_configured_provider_gets_a_row_including_custom_ones(
    client: TestClient, configured: list[tuple[str, str, int]], base_urls: dict
) -> None:
    """§2: derived from configuration, never a hard-coded list."""

    payload = client.get("/admin/api/usage", headers=LOCAL_ORIGIN).json()
    listed = [row["provider_id"] for row in payload["coverage"]]
    assert listed == [row[0] for row in configured]
    # The three custom ones really are there, and none of them was dropped for
    # having no catalogue row.
    assert {"custom_91aa", "custom_77bb", "custom_55cc"} <= set(listed)


def test_each_row_is_in_exactly_one_of_the_eight_states(
    client: TestClient,
    configured: list[tuple[str, str, int]],
    base_urls: dict,
    fake_host: FakeHost,
) -> None:
    """The eight-state fixture: one provider per state, all in one answer."""

    # One read, so the measured row is measured rather than not-read-yet.
    client.post(
        "/admin/api/usage/read",
        headers=LOCAL_ORIGIN,
        json={"provider_id": "deepseek"},
    )
    payload = client.get("/admin/api/usage", headers=LOCAL_ORIGIN).json()
    states = {row["provider_id"]: row["state"] for row in payload["coverage"]}

    for provider_id, state in states.items():
        assert state in EIGHT_STATES, (provider_id, state)
        assert state in USAGE_STATE_ORDER, (provider_id, state)

    # MEASURED: the provider the read just answered.
    assert states["deepseek"] == "measured"
    # NOT PUBLISHED: a provider the table names with no mechanism, and a custom
    # host nothing recognises.
    assert states["novita"] == "unsupported"
    assert states["custom_55cc"] == "unsupported"
    # POSSIBLE ENDPOINT: the two custom hosts a recogniser claims.
    assert states["custom_91aa"] == STATE_POSSIBLE_ENDPOINT
    assert states["custom_77bb"] == STATE_POSSIBLE_ENDPOINT
    # NEEDS A CREDENTIAL: declared, route known, secret absent.
    assert states["copilot"] == STATE_NEEDS_CREDENTIAL
    # NOT READ YET: a declared mechanism that has carried nothing, and a row
    # with no adapter this release builds.
    assert states["nous_portal"] == "not_measured"
    assert states["kimi_coding"] == "not_measured"
    # A subscriber that has not been read this session is not read yet either.
    assert states["anthropic_oauth"] == "not_measured"


def test_a_recognised_custom_host_gets_words_and_never_a_number(
    client: TestClient,
    configured: list[tuple[str, str, int]],
    base_urls: dict,
) -> None:
    """§2's eighth state: WORDS ONLY, never a figure."""

    payload = client.get("/admin/api/usage", headers=LOCAL_ORIGIN).json()
    for provider_id in ("custom_91aa", "custom_77bb"):
        row = _coverage(payload, provider_id)
        assert row["stage"] == "discovery"
        assert row["recognised"], provider_id
        assert row["sentence"], provider_id
        assert row["reading"] is None, provider_id
        assert row["fetched_at"] is None, provider_id


def test_the_recognisers_name_what_they_recognised(
    client: TestClient,
    configured: list[tuple[str, str, int]],
    base_urls: dict,
) -> None:
    payload = client.get("/admin/api/usage", headers=LOCAL_ORIGIN).json()
    # models.dev's api host, from the local cache.
    assert "models.dev" in _coverage(payload, "custom_91aa")["recognised"]
    # A gateway family, from the declared table.
    assert "gateway family" in _coverage(payload, "custom_77bb")["recognised"]


def test_a_custom_provider_that_matches_nothing_is_not_published_and_still_listed(
    client: TestClient,
    configured: list[tuple[str, str, int]],
    base_urls: dict,
) -> None:
    """The honest fallback, and it is a ROW rather than a gap."""

    payload = client.get("/admin/api/usage", headers=LOCAL_ORIGIN).json()
    row = _coverage(payload, "custom_55cc")
    assert row["state"] == "unsupported"
    assert row["stage"] == "read"
    assert row["recognised"] == ""
    # …and its credential count is on it, so the page can say "3 credentials".
    assert row["credentials"] == 3


def test_a_route_needing_a_credential_is_never_reported_as_publishing_nothing(
    client: TestClient, configured: list[tuple[str, str, int]], base_urls: dict
) -> None:
    """§2's seventh state is a fact about the KEYRING, not about the provider."""

    row = _coverage(
        client.get("/admin/api/usage", headers=LOCAL_ORIGIN).json(), "copilot"
    )
    assert row["state"] == STATE_NEEDS_CREDENTIAL
    assert "Copilot OAuth token" in row["needs_credential"]
    assert row["state"] != "unsupported"


# ───────────────────────────────────────────────── the read-all walk ────────


def test_read_all_asks_every_readable_configured_provider(
    client: TestClient,
    configured: list[tuple[str, str, int]],
    base_urls: dict,
    fake_host,
) -> None:
    """The page-level Refresh now: one POST, N reads, each behind the gate."""

    response = client.post("/admin/api/usage/read-all", headers=LOCAL_ORIGIN, json={})
    assert response.status_code == 200
    payload = response.json()
    asked = {
        entry["provider_id"] for entry in payload["attempted"] if entry["requested"]
    }
    # Every provider whose row declares a read this release builds, and nothing
    # else: no subscription without an adapter, no custom provider, no provider
    # with no mechanism.
    assert asked == {
        "anthropic_oauth",
        "chatgpt_oauth",
        "commandcode",
        "open_router",
        "deepseek",
        "opencode_go",
        "kimi",
    }
    # One fake-host call per asked provider, and no more.
    assert len(fake_host.calls) == len(asked)
    # The answer carries the route's own two lists, so the page repaints from
    # the server rather than from what it hoped.
    assert {row["provider_id"] for row in payload["coverage"]} == {
        row[0] for row in configured
    }


def test_read_all_refuses_a_provider_inside_its_own_deadline(
    client: TestClient,
    configured: list[tuple[str, str, int]],
    base_urls: dict,
    fake_host,
) -> None:
    """The gate is the same one the single read uses, and it says why."""

    first = client.post("/admin/api/usage/read-all", headers=LOCAL_ORIGIN, json={})
    assert first.status_code == 200
    calls_after_first = len(fake_host.calls)

    second = client.post("/admin/api/usage/read-all", headers=LOCAL_ORIGIN, json={})
    assert second.status_code == 200
    payload = second.json()
    # Nothing was asked a second time inside the interval.
    assert len(fake_host.calls) == calls_after_first
    refusals = {
        entry["provider_id"]: entry["refusal"] for entry in payload["attempted"]
    }
    assert refusals, payload["attempted"]
    assert set(refusals.values()) == {"interval"}
    assert all("nothing was sent" in entry["message"] for entry in payload["attempted"])


def test_read_all_stops_the_walk_when_the_tab_goes_hidden(
    client: TestClient,
    configured: list[tuple[str, str, int]],
    base_urls: dict,
    fake_host,
) -> None:
    """The third safety behaviour, applied to a button."""

    response = client.post(
        "/admin/api/usage/read-all",
        headers={**LOCAL_ORIGIN, "X-MCC-Visible": "hidden"},
        json={},
    )
    assert response.status_code == 200
    payload = response.json()
    assert fake_host.calls == [], "a hidden tab read something"
    assert payload["attempted"], "the walk must say it stopped rather than vanish"
    assert payload["attempted"][0]["refusal"] == "hidden"


def test_read_all_requires_a_local_origin(client: TestClient) -> None:
    """The same guard as the single read: no Origin, no walk."""

    response = client.post("/admin/api/usage/read-all", json={})
    assert response.status_code == 403


def test_one_providers_failure_does_not_stop_the_walk(
    client: TestClient,
    configured: list[tuple[str, str, int]],
    base_urls: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing host costs that provider's own reading and nothing else."""

    class _Failing(FakeHost):
        status = 500

    host = _Failing(body={"error": "the host broke"})
    monkeypatch.setattr(usage_reads, "httpx", _patched_httpx(host))
    payload = client.post(
        "/admin/api/usage/read-all", headers=LOCAL_ORIGIN, json={}
    ).json()
    attempted = {entry["provider_id"]: entry for entry in payload["attempted"]}
    assert len(attempted) == 7, attempted
    for entry in attempted.values():
        assert entry["requested"] is True, entry
        assert entry["state"] == "failed", entry
        assert entry["read_error"], entry
    # The answer still carries every configured provider's coverage.
    assert len(payload["coverage"]) == len(configured)


# ────────────────────────────────────────────── the layout, and no branching ─


def test_the_route_reports_the_layout_word(client: TestClient) -> None:
    payload = client.get("/admin/api/usage", headers=LOCAL_ORIGIN).json()
    assert payload["layout"] in {
        "one_screen",
        "ledger_rows",
        "meter_board",
        "dial_cards",
        "master_detail",
    }


def test_an_unknown_layout_falls_back_on_the_wire(
    client: TestClient, monkeypatch
) -> None:
    """§3: unknown value -> one_screen (the settings layer logs the one line)."""

    payload = client.get("/admin/api/usage", headers=LOCAL_ORIGIN).json()
    assert payload["layout"] == "one_screen"


# ───────────────────────────────────────────────── the recogniser, as data ──


def test_the_gateway_family_table_is_three_rows() -> None:
    assert {family.id for family in GATEWAY_FAMILIES} == {
        "new-api",
        "litellm",
        "key-info",
    }
    for family in GATEWAY_FAMILIES:
        assert family.routes, family.id
        assert family.reads, family.id


def test_a_base_url_names_one_family_or_none() -> None:
    assert [f.id for f in families_for_base_url("https://litellm.corp/v1")] == [
        "litellm"
    ]
    assert [f.id for f in families_for_base_url("https://new-api.corp/v1")] == [
        "new-api"
    ]
    assert families_for_base_url("https://api.example.com/v1") == ()
    assert families_for_base_url("") == ()


def test_two_families_claiming_one_url_is_refused_rather_than_guessed() -> None:
    """The Q7 ambiguity refusal, at the recogniser."""

    both = families_for_base_url("https://litellm.corp/new-api/v1")
    assert len(both) == 2


def test_a_declared_row_with_a_mechanism_is_never_a_hypothesis() -> None:
    """A catalogue row the table knows is never `possible_endpoint`: it is a
    fact about a provider, and recognising a host is not one."""

    for provider_id in ("deepseek", "open_router", "commandcode"):
        row = source_for(provider_id)
        assert row.supported is True
        assert row.readable is True
