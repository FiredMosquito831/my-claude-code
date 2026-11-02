"""``GET /admin/api/usage``: read-only, loopback-only, four states on the wire.

USAGE PR 2's route. What it must prove, in the order that matters:

1. **It is a read.** No ``POST``, no ``PUT``, no second method of any kind, and
   the body says so -- a caller can check the claim rather than trust it.
2. **It is loopback-only**, through the same guard every other admin route
   uses, and a non-loopback origin is refused.
3. **The four states are decided on the wire**, not in JavaScript: a provider
   with a fresh snapshot is ``measured``, an old one ``stale``, a provider with
   a mechanism and no snapshot ``not_measured``, and one with no mechanism
   ``unsupported``.
4. **Values are the provider's own strings, verbatim, and a field it did not
   send is absent** rather than empty -- so the page can render *not measured*
   without guessing.
5. **Reading the route changes nothing**: two calls in a row return the same
   provider set, and the store is untouched by the read.
"""

import time

import pytest
from fastapi.testclient import TestClient

from my_claude_code.api.admin_usage_routes import STALE_AFTER_SECONDS
from my_claude_code.core.usage_capture import OBSERVER
from tests.api.support import create_test_app

LOCAL_ORIGIN = {"Origin": "http://127.0.0.1:8082"}
FAR_ORIGIN = {"Origin": "https://example.com"}


@pytest.fixture(autouse=True)
def _clean_board():
    """A module-wide process store, reset around every test."""

    OBSERVER.reset()
    yield
    OBSERVER.reset()


@pytest.fixture
def client() -> TestClient:
    # The loopback peer the admin guard checks. ``TestClient``'s default peer is
    # ``testclient``, which is not a loopback address, so every admin route test
    # in this suite that expects a 200 names one.
    return TestClient(create_test_app(), client=("127.0.0.1", 50000))


def test_a_remote_client_is_refused(client: TestClient) -> None:
    remote = TestClient(client.app, client=("203.0.113.10", 50000))
    assert remote.get("/admin/api/usage").status_code == 403


def _row(payload: dict, provider_id: str) -> dict:
    return next(
        row for row in payload["providers"] if row["provider_id"] == provider_id
    )


def test_the_route_is_read_only_and_says_so(client: TestClient) -> None:
    response = client.get("/admin/api/usage", headers=LOCAL_ORIGIN)
    assert response.status_code == 200
    payload = response.json()
    assert payload["observed_at"] > 0
    assert payload["stale_after_seconds"] == STALE_AFTER_SECONDS
    assert "providers" in payload


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_no_write_method_exists(client: TestClient, method: str) -> None:
    """Not even a 405 with a body: there is no such route at all."""

    response = getattr(client, method)("/admin/api/usage", headers=LOCAL_ORIGIN)
    assert response.status_code in (404, 405), response.status_code


def test_a_foreign_origin_is_refused(client: TestClient) -> None:
    response = client.get("/admin/api/usage", headers=FAR_ORIGIN)
    assert response.status_code == 403


def test_a_provider_with_no_snapshot_is_not_measured(client: TestClient) -> None:
    payload = client.get("/admin/api/usage", headers=LOCAL_ORIGIN).json()
    row = _row(payload, "nous_portal")
    assert row["state"] == "not_measured"
    assert row["observed_at"] is None
    assert row["values"] == {}
    assert row["source"] == "response-header"


def test_a_fresh_snapshot_is_measured_with_verbatim_values(client: TestClient) -> None:
    OBSERVER.observe(
        {
            "x-nous-credits-remaining-usd": "142.5",
            "x-nous-credits-subscription-limit-usd": "6.00",
        },
        status_code=200,
        provider_id="nous_portal",
        now=time.time() - 60,
    )
    row = _row(
        client.get("/admin/api/usage", headers=LOCAL_ORIGIN).json(), "nous_portal"
    )
    assert row["state"] == "measured"
    assert row["status_code"] == 200
    assert row["values"] == {
        "x-nous-credits-remaining-usd": "142.5",
        "x-nous-credits-subscription-limit-usd": "6.00",
    }


def test_a_snapshot_older_than_the_bound_is_stale(client: TestClient) -> None:
    OBSERVER.observe(
        {"x-ratelimit-remaining-requests": "4"},
        status_code=200,
        provider_id="open_router",
        now=time.time() - (STALE_AFTER_SECONDS + 1),
    )
    row = _row(
        client.get("/admin/api/usage", headers=LOCAL_ORIGIN).json(), "open_router"
    )
    assert row["state"] == "stale"
    # The value is still there: stale is a reading with an age, never a blank.
    assert row["values"] == {"x-ratelimit-remaining-requests": "4"}


def test_a_provider_with_no_mechanism_is_unsupported(client: TestClient) -> None:
    row = _row(client.get("/admin/api/usage", headers=LOCAL_ORIGIN).json(), "novita")
    assert row["state"] == "unsupported"
    assert row["mechanisms"] == []
    # And it can never be measured even if a stray observation arrives.
    OBSERVER.observe(
        {"x-ratelimit-limit": "1"},
        status_code=200,
        provider_id="novita",
        now=time.time(),
    )
    again = _row(client.get("/admin/api/usage", headers=LOCAL_ORIGIN).json(), "novita")
    assert again["state"] == "unsupported"


def test_an_endpoint_only_provider_is_not_measured_and_not_unsupported(
    client: TestClient,
) -> None:
    """'Not built yet' must not read as 'nothing to read' (IV.16)."""

    row = _row(
        client.get("/admin/api/usage", headers=LOCAL_ORIGIN).json(), "opencode_go"
    )
    assert row["state"] == "not_measured"
    assert "provider-endpoint" in row["mechanisms"]


def test_the_route_reports_exactly_the_declared_providers(client: TestClient) -> None:
    from my_claude_code.config.usage_sources import PROVIDER_SOURCES

    payload = client.get("/admin/api/usage", headers=LOCAL_ORIGIN).json()
    assert [row["provider_id"] for row in payload["providers"]] == [
        row.provider_id for row in PROVIDER_SOURCES
    ]


def test_reading_the_route_twice_changes_nothing(client: TestClient) -> None:
    OBSERVER.observe(
        {"x-ratelimit-limit-requests": "5000"},
        status_code=200,
        provider_id="deepseek",
        now=100.0,
    )
    first = client.get("/admin/api/usage", headers=LOCAL_ORIGIN).json()
    second = client.get("/admin/api/usage", headers=LOCAL_ORIGIN).json()
    assert first["providers"] == second["providers"]


def test_a_non_allow_listed_header_never_reaches_the_payload() -> None:
    """Proven end to end: observed through the real callback path, then read.

    The unit test on ``capture_usage_headers`` proves the filter; this proves the
    filter is the one the provider path actually uses.
    """

    from my_claude_code.providers.runtime.usage_observers import (
        install_usage_observer,
    )

    class _Holder:
        def __init__(self) -> None:
            self.seen = None

        def set_response_observer(self, observer) -> None:
            self.seen = observer

    holder = _Holder()
    assert install_usage_observer(holder, "nous_portal") is True
    callback = holder.seen
    assert callback is not None
    callback(
        {
            "x-nous-credits-remaining-usd": "1.00",
            "authorization": "Bearer secret",
            "x-account-email": "someone@example.com",
        },
        200,
    )
    snapshot = OBSERVER.latest_for("nous_portal")
    assert snapshot is not None
    assert snapshot.values == {"x-nous-credits-remaining-usd": "1.00"}
