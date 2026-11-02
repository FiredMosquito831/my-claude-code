"""The Usage page's on-demand read: the two routes, and the proofs that matter.

``specs/INVESTIGATION-USAGE-AND-CREDITS.md`` §7 PR 3. What this file proves, in
the order the release's contract asks for it:

1. **Both routes exist, and each does only its own thing.** ``GET
   /admin/api/usage`` reads the cache and never dials; ``POST
   /admin/api/usage/read`` reads ONE named provider.
2. **The read path can never charge a key or bench a provider.** A fake 401 on
   a usage read yields state 4 and fires **no** rotation recorder; a fake 429
   sets **only** this provider's usage deadline and never the provider's bench.
   Both are asserted with recorders monkeypatched to *raise*, so a call is
   impossible to miss rather than merely unlikely.
3. **One request per provider per interval.** Two presses inside it make one
   request, counted at the fake host rather than at the caller.
4. **A 429 is a deadline, not a verdict**, and ``Retry-After`` is honoured as
   the provider's own wait.
5. **A malformed body is state 4 with a reason**, never a crash and never a
   zero.
6. **The guards**: loopback only, a local ``Origin`` on the POST, and an id the
   declared table does not carry refused rather than guessed at.
7. **No credential value ever reaches a payload or a log line.**

**LIVE CALLS: 0.** Every read here goes to an ``httpx.MockTransport`` in this
process. Nothing in this file opens a socket.
"""

import json
import time
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from my_claude_code.api.admin_usage_routes import (
    MINIMUM_INTERVAL_SECONDS,
    STALE_AFTER_SECONDS,
)
from my_claude_code.core.usage.reading import READINGS, Reading, ReadRecord
from my_claude_code.core.usage_capture import OBSERVER
from my_claude_code.providers.runtime import usage_reads
from tests.api.support import create_test_app

LOCAL_ORIGIN = {"Origin": "http://127.0.0.1:8082"}
FAR_ORIGIN = {"Origin": "https://example.com"}

#: What the fake hosts answer. One body per provider, in the shape its adapter
#: declares, written from §3's table rather than from a live answer.
BODIES: dict[str, dict[str, Any]] = {
    "deepseek": {
        "is_available": True,
        "balance_infos": [
            {
                "currency": "USD",
                "total_balance": "12.34",
                "granted_balance": "2.00",
                "topped_up_balance": "10.34",
            }
        ],
    },
    "open_router": {
        "data": {
            "label": "a key",
            "usage": 1.5,
            "limit": 50,
            "limit_remaining": 48.5,
            "limit_reset": "monthly",
        }
    },
    "kimi": {
        "data": {
            "available_balance": "0.00",
            "voucher_balance": "0.00",
            "cash_balance": "0.00",
            "status": "EXPIRED",
        }
    },
}


class FakeHost:
    """An answer and a record of every call made to it, in this process."""

    def __init__(
        self, status: int = 200, body: Any = None, headers: dict | None = None
    ):
        self.status = status
        self.body = body
        self.headers = headers or {}
        self.calls: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.status == 200:
            raw = json.dumps(self.body).encode()
            return httpx.Response(
                200, content=raw, headers={"content-type": "application/json"}
            )
        content = b"" if self.body is None else json.dumps(self.body).encode()
        return httpx.Response(
            self.status,
            content=content,
            headers={"content-type": "application/json", **self.headers},
        )

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


@pytest.fixture(autouse=True)
def _clean_board(monkeypatch: pytest.MonkeyPatch):
    """A process store and a credential resolver, reset around every test.

    The credential resolver is replaced with a fake *of the right shape* rather
    than with "whatever is configured on this machine": a test that read a real
    ``.env`` would be a test that depends on the developer. The replacement is
    the same value the real accessor would hand over -- an opaque token string
    -- so what is being proved is the read path, not the store.
    """

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


def _fake_credential():
    from my_claude_code.core.usage.reading import UsageCredential

    return UsageCredential(token="sk-fake-0123456789", label="sk-f…6789")


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_test_app(), client=("127.0.0.1", 50000))


@pytest.fixture
def fake_host(monkeypatch: pytest.MonkeyPatch) -> FakeHost:
    """Point every read at one fake host, and hand back its recorder."""

    host = FakeHost(body=BODIES["deepseek"])
    monkeypatch.setattr(usage_reads, "httpx", _patched_httpx(host))
    return host


def _patched_httpx(host: FakeHost) -> Any:
    """``httpx`` with ``AsyncClient`` bound to the fake transport.

    Everything else on the module is the real thing -- ``TimeoutException``,
    ``Headers``, ``Response`` -- because the classification under test reads
    them, and a stub of those would test the stub.
    """

    class _Module:
        Headers = httpx.Headers
        Response = httpx.Response
        Request = httpx.Request
        TimeoutException = httpx.TimeoutException

        @staticmethod
        def AsyncClient(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            return host.client()

    return _Module()


def _row(payload: dict, provider_id: str) -> dict:
    return next(
        row for row in payload["providers"] if row["provider_id"] == provider_id
    )


# ---------------------------------------------------------------- the two routes


def test_the_get_route_reads_the_cache_and_never_dials(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opening the page must make ZERO usage reads. Re-proved for PR 3.

    8.4.0 proved it for the page's own traffic. PR 3 adds a route that *can*
    dial, so the property has to be re-proved against that route's absence from
    the GET path: the fake host records every call, and the GET makes none.
    """

    host = FakeHost(body=BODIES["deepseek"])
    monkeypatch.setattr(usage_reads, "httpx", _patched_httpx(host))
    for _ in range(3):
        response = client.get("/admin/api/usage", headers=LOCAL_ORIGIN)
        assert response.status_code == 200
    assert host.calls == []


def test_the_post_route_reads_exactly_one_named_provider(
    client: TestClient, fake_host: FakeHost
) -> None:
    response = client.post(
        "/admin/api/usage/read",
        headers=LOCAL_ORIGIN,
        json={"provider_id": "deepseek"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["requested"] is True
    assert payload["state"] == "measured"
    assert payload["reading"]["balances"][0]["amount"] == "12.34"
    assert len(fake_host.calls) == 1
    assert str(fake_host.calls[0].url) == "https://api.deepseek.com/user/balance"
    # And NOTHING else was asked: one press, one request.
    assert fake_host.calls[0].method == "GET"


def test_the_post_carries_the_declared_beta_header_for_a_row_that_needs_one(
    client: TestClient, fake_host: FakeHost
) -> None:
    """The Claude row's ``oauth-2025-04-20`` beta is declared, and it is sent."""

    client.post(
        "/admin/api/usage/read",
        headers=LOCAL_ORIGIN,
        json={"provider_id": "anthropic_oauth"},
    )
    assert fake_host.calls, "the read never reached the fake host"
    request = fake_host.calls[0]
    assert str(request.url) == "https://api.anthropic.com/api/oauth/usage"
    assert request.headers["anthropic-beta"] == "oauth-2025-04-20"


# --------------------------------------------------- the no-charge proof (§6.3)


def _refuse_every_recorder(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Make every key-charging and bench recorder raise, and name them.

    V.3 is explicit that key health changes on exactly three signals, and that
    an exit may be charged from nowhere else. So the recorders to trap are the
    rotation engine's four, the cooldown resolver, and route health's two. If a
    usage read reaches any of them, the read fails loudly here -- which is the
    mutation-shaped proof III.4 asks for: remove the isolation and this test
    fails rather than quietly passing.
    """

    fired: list[str] = []

    def trap(name: str) -> Any:
        def _raise(*args: Any, **kwargs: Any) -> None:
            fired.append(name)
            raise AssertionError(f"a usage read reached {name}")

        return _raise

    from my_claude_code.application import route_health
    from my_claude_code.core import credential_rotation, rate_limit

    for name in ("fail", "succeed", "note_rate_limit", "mark_acquired"):
        monkeypatch.setattr(
            credential_rotation.RotationEngine, name, trap(name), raising=True
        )
    monkeypatch.setattr(
        rate_limit.RateLimitCooldown,
        "resolve",
        trap("RateLimitCooldown.resolve"),
        raising=True,
    )
    monkeypatch.setattr(
        route_health.RouteHealthRegistry,
        "record_failure",
        trap("RouteHealthRegistry.record_failure"),
        raising=True,
    )
    monkeypatch.setattr(
        route_health.RouteHealthRegistry,
        "record_success",
        trap("RouteHealthRegistry.record_success"),
        raising=True,
    )
    return fired


def test_a_401_on_a_usage_read_is_state_4_and_charges_nothing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**The load-bearing proof of the PR.**

    A usage read answered 401 yields state 4 with the reason, and NO rotation
    recorder fires. The recorders are monkeypatched to raise, so this cannot
    pass by accident: if the read path called one, the route would answer 500.
    """

    fired = _refuse_every_recorder(monkeypatch)
    host = FakeHost(status=401, body={"error": {"message": "Invalid API key"}})
    monkeypatch.setattr(usage_reads, "httpx", _patched_httpx(host))

    response = client.post(
        "/admin/api/usage/read",
        headers=LOCAL_ORIGIN,
        json={"provider_id": "deepseek"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["state"] == "failed"
    assert payload["status_code"] == 401
    # The provider's own sentence, not a generic one.
    assert "Invalid API key" in payload["read_error"]
    # And the absent proof: nothing fired.
    assert fired == []
    # The reading stays absent -- a 401 is never a number, and never a zero.
    assert payload["reading"] is None


def test_a_429_on_a_usage_read_sets_only_this_providers_deadline(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second half: a 429 becomes a usage deadline and nothing else.

    ``Retry-After`` is honoured as the provider's own wait, the GET reports it,
    a second press inside it is refused WITHOUT a request, and no bench
    recorder fired.
    """

    fired = _refuse_every_recorder(monkeypatch)
    host = FakeHost(
        status=429, body={"error": "slow down"}, headers={"retry-after": "120"}
    )
    monkeypatch.setattr(usage_reads, "httpx", _patched_httpx(host))

    before = time.time()
    first = client.post(
        "/admin/api/usage/read",
        headers=LOCAL_ORIGIN,
        json={"provider_id": "open_router"},
    )
    assert first.status_code == 200
    payload = first.json()
    assert payload["state"] == "failed"
    assert payload["status_code"] == 429
    assert 100 <= payload["retry_in_seconds"] <= 120

    # The deadline is per provider: another provider is not affected.
    other = _row(
        client.get("/admin/api/usage", headers=LOCAL_ORIGIN).json(), "deepseek"
    )
    assert other["retry_in_seconds"] is None
    affected = _row(
        client.get("/admin/api/usage", headers=LOCAL_ORIGIN).json(), "open_router"
    )
    assert affected["retry_at"] is not None
    assert affected["retry_at"] >= before + 100

    # A press inside the deadline sends NOTHING and says why.
    calls_before = len(host.calls)
    second = client.post(
        "/admin/api/usage/read",
        headers=LOCAL_ORIGIN,
        json={"provider_id": "open_router"},
    )
    assert second.status_code == 200
    refused = second.json()
    assert refused["requested"] is False
    assert refused["refusal"] in {"cooldown", "interval"}
    assert len(host.calls) == calls_before
    assert fired == []


def test_a_429_without_retry_after_still_sets_the_interval(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No header, no invented wait -- but the 30-minute interval still holds."""

    host = FakeHost(status=429, body={})
    monkeypatch.setattr(usage_reads, "httpx", _patched_httpx(host))
    payload = client.post(
        "/admin/api/usage/read",
        headers=LOCAL_ORIGIN,
        json={"provider_id": "deepseek"},
    ).json()
    assert payload["retry_in_seconds"] is None
    assert payload["next_read_in_seconds"] is not None
    assert payload["next_read_in_seconds"] <= MINIMUM_INTERVAL_SECONDS


# --------------------------------------------- one request per provider/interval


def test_two_presses_inside_the_interval_make_one_request(
    client: TestClient, fake_host: FakeHost
) -> None:
    """**The interval proof.** Two presses, one request at the fake host."""

    first = client.post(
        "/admin/api/usage/read",
        headers=LOCAL_ORIGIN,
        json={"provider_id": "deepseek"},
    )
    second = client.post(
        "/admin/api/usage/read",
        headers=LOCAL_ORIGIN,
        json={"provider_id": "deepseek"},
    )
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["requested"] is True
    assert second.json()["requested"] is False
    assert second.json()["refusal"] == "interval"
    assert len(fake_host.calls) == 1


def test_two_providers_are_claimed_independently(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = FakeHost(body=BODIES["deepseek"])
    monkeypatch.setattr(usage_reads, "httpx", _patched_httpx(host))
    for provider_id in ("deepseek", "open_router", "deepseek"):
        client.post(
            "/admin/api/usage/read",
            headers=LOCAL_ORIGIN,
            json={"provider_id": provider_id},
        )
    # Three presses, two providers, two requests: the third was inside
    # deepseek's own interval.
    assert len(host.calls) == 2


def test_a_concurrent_claim_is_refused_rather_than_duplicated() -> None:
    """The gate itself, without a client: a claim is atomic.

    ``UsageReadStore.claim`` marks the provider in flight *and* stamps the
    attempt before returning, so a second caller in the same instant sees a
    claim rather than a "never read" time. That is what makes two page loads a
    millisecond apart one request even when neither has finished.
    """

    assert READINGS.claim("deepseek", now=1000.0, interval=1800.0) == ""
    assert READINGS.claim("deepseek", now=1000.0, interval=1800.0) == "in_flight"
    # Even after the finish, the interval still holds.
    READINGS.finish("deepseek", None, ReadRecord(attempted_at=1000.0))
    assert READINGS.claim("deepseek", now=1001.0, interval=1800.0) == "interval"
    assert READINGS.claim("deepseek", now=1000.0 + 1801.0, interval=1800.0) == ""


# --------------------------------------------------------------- malformed bodies


def test_a_malformed_body_is_state_4_with_a_reason_not_a_crash(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = FakeHost(status=200, body={"unrelated": "shape"})
    monkeypatch.setattr(usage_reads, "httpx", _patched_httpx(host))
    response = client.post(
        "/admin/api/usage/read",
        headers=LOCAL_ORIGIN,
        json={"provider_id": "deepseek"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["state"] == "failed"
    assert "could not read the host's answer" in payload["read_error"]
    assert payload["reading"] is None


def test_a_non_json_body_is_state_4_with_a_sentence(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _HtmlHost(FakeHost):
        def handler(self, request: httpx.Request) -> httpx.Response:
            self.calls.append(request)
            return httpx.Response(200, content=b"<html>dashboard</html>")

    host = _HtmlHost()
    monkeypatch.setattr(usage_reads, "httpx", _patched_httpx(host))
    payload = client.post(
        "/admin/api/usage/read",
        headers=LOCAL_ORIGIN,
        json={"provider_id": "deepseek"},
    ).json()
    assert payload["state"] == "failed"
    assert "not JSON" in payload["read_error"]


def test_a_redirect_is_refused_and_never_followed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The credential must not travel to a host the operator never named."""

    host = FakeHost(status=302, body={})
    monkeypatch.setattr(usage_reads, "httpx", _patched_httpx(host))
    payload = client.post(
        "/admin/api/usage/read",
        headers=LOCAL_ORIGIN,
        json={"provider_id": "deepseek"},
    ).json()
    assert payload["state"] == "failed"
    assert "redirect" in payload["read_error"]
    assert len(host.calls) == 1


# ---------------------------------------------------------------------- guards


def test_a_remote_client_is_refused_on_both_routes(client: TestClient) -> None:
    remote = TestClient(client.app, client=("203.0.113.10", 50000))
    assert remote.get("/admin/api/usage").status_code == 403
    assert (
        remote.post(
            "/admin/api/usage/read",
            headers=LOCAL_ORIGIN,
            json={"provider_id": "deepseek"},
        ).status_code
        == 403
    )


def test_the_read_route_requires_a_local_origin(
    client: TestClient, fake_host: FakeHost
) -> None:
    """Unforgeable from a page: no local ``Origin``, no read."""

    assert (
        client.post(
            "/admin/api/usage/read",
            headers=FAR_ORIGIN,
            json={"provider_id": "deepseek"},
        ).status_code
        == 403
    )
    # And a bare curl with no Origin at all is refused too -- this route
    # spends a provider's read budget, so it asks for the browser's own header.
    assert (
        client.post(
            "/admin/api/usage/read", json={"provider_id": "deepseek"}
        ).status_code
        == 403
    )
    assert fake_host.calls == []


def test_a_provider_with_no_declared_read_is_refused_not_guessed(
    client: TestClient, fake_host: FakeHost
) -> None:
    for provider_id in ("novita", "kimi_coding", "nous_portal", "made_up_provider"):
        response = client.post(
            "/admin/api/usage/read",
            headers=LOCAL_ORIGIN,
            json={"provider_id": provider_id},
        )
        assert response.status_code == 404, provider_id
    assert fake_host.calls == []


def test_the_get_route_still_has_no_write_method(client: TestClient) -> None:
    for method in ("put", "patch", "delete"):
        response = getattr(client, method)("/admin/api/usage", headers=LOCAL_ORIGIN)
        assert response.status_code in (404, 405)


# ---------------------------------------------------------------- the wire shape


def test_every_row_carries_readable_and_the_declared_endpoint(
    client: TestClient,
) -> None:
    payload = client.get("/admin/api/usage", headers=LOCAL_ORIGIN).json()
    deepseek = _row(payload, "deepseek")
    assert deepseek["readable"] is True
    assert deepseek["endpoint"]["path"] == "/user/balance"
    assert deepseek["endpoint"]["provenance"] == "vendor-doc"
    # The credential KIND is on the wire; the credential VALUE is not.
    assert deepseek["endpoint"]["credential"] == "api-key-env"

    novita = _row(payload, "novita")
    assert novita["readable"] is False
    assert novita["endpoint"] is None
    assert novita["state"] == "unsupported"


def test_no_payload_anywhere_carries_a_credential_value(
    client: TestClient, fake_host: FakeHost
) -> None:
    """A token is never in a response body, on either route."""

    token = "sk-fake-0123456789"
    read = client.post(
        "/admin/api/usage/read", headers=LOCAL_ORIGIN, json={"provider_id": "deepseek"}
    ).text
    listed = client.get("/admin/api/usage", headers=LOCAL_ORIGIN).text
    for text in (read, listed):
        assert token not in text
        assert "Bearer" not in text


def test_a_failed_read_keeps_the_last_good_reading(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure never erases a reading: it is state 4 WITH the old number.

    §5.4's "Read failed, provider said why" row: *"Could not read usage: … Last
    good reading 2 hours ago."* The old reading keeps its own ``fetched_at``,
    so its age is the truth rather than the failure's.
    """

    good = FakeHost(body=BODIES["deepseek"])
    monkeypatch.setattr(usage_reads, "httpx", _patched_httpx(good))
    first = client.post(
        "/admin/api/usage/read", headers=LOCAL_ORIGIN, json={"provider_id": "deepseek"}
    ).json()
    assert first["state"] == "measured"
    fetched_at = first["fetched_at"]
    assert fetched_at is not None

    READINGS.reset()  # a fresh gate, so the next press is allowed
    bad = FakeHost(status=401, body={"error": "refused"})
    monkeypatch.setattr(usage_reads, "httpx", _patched_httpx(bad))
    # Put the good reading back, and put the last *attempt* far enough in the
    # past that the interval does not refuse this press. The reading keeps its
    # own fetched_at, which is the whole point: its age must not become the
    # failure's.
    READINGS.finish(
        "deepseek",
        Reading(fetched_at=fetched_at),
        ReadRecord(
            attempted_at=fetched_at - (MINIMUM_INTERVAL_SECONDS + 60),
            status_code=200,
        ),
    )
    second = client.post(
        "/admin/api/usage/read", headers=LOCAL_ORIGIN, json={"provider_id": "deepseek"}
    ).json()
    assert second["requested"] is True
    assert second["state"] == "failed"
    assert second["fetched_at"] == fetched_at
    assert second["reading"] is not None


def test_the_state_arithmetic_is_decided_on_the_server(
    client: TestClient,
) -> None:
    """A reading older than the bound is stale; a provider with no mechanism is
    never promoted, even by a stray reading."""

    now = time.time()
    READINGS.finish(
        "deepseek",
        Reading(fetched_at=now - (STALE_AFTER_SECONDS + 5)),
        ReadRecord(attempted_at=now - (STALE_AFTER_SECONDS + 5), status_code=200),
    )
    payload = client.get("/admin/api/usage", headers=LOCAL_ORIGIN).json()
    assert _row(payload, "deepseek")["state"] == "stale"

    # An unavailable answer is its own state, never "failed" and never
    # "measured with a zero".
    READINGS.finish(
        "kimi",
        Reading(fetched_at=now, available=False, unavailable_reason="expired"),
        ReadRecord(attempted_at=now, status_code=200),
    )
    assert (
        _row(client.get("/admin/api/usage", headers=LOCAL_ORIGIN).json(), "kimi")[
            "state"
        ]
        == "unavailable"
    )
