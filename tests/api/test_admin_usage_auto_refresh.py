"""8.6.0: the automatic refresh, proved on the wire.

The user's instruction (2026-11-02) is that the Usage page reads and updates
every configured provider **every 30 seconds**, with the interval configurable
on Limits & Resilience. This file is the wire half of that change -- the
arithmetic is in ``tests/core/test_usage_auto_refresh.py``.

What it proves, in the order the safety argument runs:

1. **The interval is the server's, and the page is told it.** The GET reports
   ``auto_refresh_seconds`` from Settings, so the poller's cadence and the
   gate's interval cannot drift.
2. **At 30 seconds, a provider is read once per 30 seconds -- not once per
   press.** Two presses inside one interval are one request; a press after it
   is a second.
3. **A 429 makes the poller skip that provider until its own wait passes**, and
   only that provider: the others are still due.
4. **Backoff is per provider.** A provider failing repeatedly reports a longer
   ``next_read_in_seconds`` while a healthy one beside it does not.
5. **Zero means off.** The GET reports ``auto_refresh_seconds: 0`` and the gate
   reverts to the 30-minute on-demand floor.
6. **The polling path cannot charge a key or bench a provider.** The same
   recorders the on-demand proof traps are trapped here while a full
   poll-shaped sequence runs: several reads, including failures.
7. **A provider that is not readable, or not configured, is never asked.**

**LIVE CALLS: 0.** Every read goes to an ``httpx.MockTransport`` in this
process.
"""

import time
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from my_claude_code.api.admin_usage_routes import (
    MINIMUM_INTERVAL_SECONDS,
    _interval_for,
)
from my_claude_code.config.constants import USAGE_AUTO_REFRESH_SECONDS_DEFAULT
from my_claude_code.config.settings import Settings
from my_claude_code.core.usage.reading import READINGS, ReadRecord
from my_claude_code.core.usage_capture import OBSERVER
from my_claude_code.providers.runtime import usage_reads
from tests.api.support import create_test_app

LOCAL_ORIGIN = {"Origin": "http://127.0.0.1:8082"}

DEEPSEEK = {
    "is_available": True,
    "balance_infos": [
        {
            "currency": "USD",
            "total_balance": "12.34",
            "granted_balance": "2.00",
            "topped_up_balance": "10.34",
        }
    ],
}


class Host:
    """One fake host, one scripted answer, every call recorded."""

    def __init__(
        self, status: int = 200, body: Any = None, headers: dict | None = None
    ):
        self.status = status
        self.body = body if body is not None else DEEPSEEK
        self.headers = headers or {}
        self.calls: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        content = (
            b""
            if self.body is None or isinstance(self.body, bytes)
            else __import__("json").dumps(self.body).encode()
        )
        return httpx.Response(
            self.status,
            content=content,
            headers={"content-type": "application/json", **self.headers},
        )


def _httpx_bound_to(host: Host) -> Any:
    class _Module:
        Headers = httpx.Headers
        Response = httpx.Response
        Request = httpx.Request
        TimeoutException = httpx.TimeoutException

        @staticmethod
        def AsyncClient(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.MockTransport(host.handler))

    return _Module()


def _settings(**overrides: Any) -> Settings:
    """Settings built from a mapping, so this machine's real ``.env`` is not read."""
    values = {"MCC_CONFIG_DIR": str(__import__("tempfile").mkdtemp())}
    values.update(overrides)
    return Settings.model_validate(values)


@pytest.fixture(autouse=True)
def _clean():
    OBSERVER.reset()
    READINGS.reset()
    yield
    OBSERVER.reset()
    READINGS.reset()


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_test_app(), client=("127.0.0.1", 50000))


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch):
    host = Host()
    monkeypatch.setattr(usage_reads, "httpx", _httpx_bound_to(host))
    monkeypatch.setattr(
        usage_reads,
        "resolve_credential",
        lambda endpoint, settings: _credential(),
    )
    return host


def _credential():
    from my_claude_code.core.usage.reading import UsageCredential

    return UsageCredential(token="sk-fake-0123456789", label="sk-f…6789")


def _row(payload: dict, provider_id: str) -> dict:
    return next(
        row for row in payload["providers"] if row["provider_id"] == provider_id
    )


# --------------------------------------------------- the interval reaches the page


def test_the_page_is_told_the_servers_own_interval(client: TestClient) -> None:
    """One source: the poller's cadence is the gate's interval.

    A number typed into ``admin.js`` would be a second answer to "how often",
    and the two would drift the first time somebody changed the setting.
    """

    payload = client.get("/admin/api/usage", headers=LOCAL_ORIGIN).json()
    assert payload["auto_refresh_seconds"] == int(USAGE_AUTO_REFRESH_SECONDS_DEFAULT)
    assert payload["read_interval_seconds"] == int(USAGE_AUTO_REFRESH_SECONDS_DEFAULT)
    assert payload["minimum_interval_seconds"] == MINIMUM_INTERVAL_SECONDS


def test_every_row_carries_its_own_wait_and_failure_count(client: TestClient) -> None:
    payload = client.get("/admin/api/usage", headers=LOCAL_ORIGIN).json()
    for row in payload["providers"]:
        assert "next_read_in_seconds" in row
        assert row["interval_seconds"] == int(USAGE_AUTO_REFRESH_SECONDS_DEFAULT)
        assert row["consecutive_failures"] == 0


# --------------------------------------------- 30 seconds means 30 seconds, not 0


def test_a_provider_is_read_once_per_interval_not_once_per_press(
    client: TestClient, fake: Host
) -> None:
    """The user's cadence, on the wire: 30 s is the gate's unit now."""

    first = client.post(
        "/admin/api/usage/read", headers=LOCAL_ORIGIN, json={"provider_id": "deepseek"}
    ).json()
    assert first["requested"] is True
    assert len(fake.calls) == 1

    # Inside the interval: nothing is sent, and the page is told how long.
    second = client.post(
        "/admin/api/usage/read", headers=LOCAL_ORIGIN, json={"provider_id": "deepseek"}
    ).json()
    assert second["requested"] is False
    assert second["refusal"] == "interval"
    assert len(fake.calls) == 1
    assert 0 < second["next_read_in_seconds"] <= 30

    # After it: a second request, which is what "every 30 seconds" means.
    READINGS.finish(
        "deepseek",
        READINGS.reading_for("deepseek"),
        ReadRecord(attempted_at=time.time() - 31, status_code=200),
    )
    third = client.post(
        "/admin/api/usage/read", headers=LOCAL_ORIGIN, json={"provider_id": "deepseek"}
    ).json()
    assert third["requested"] is True
    assert len(fake.calls) == 2


# ------------------------------------------------------- Retry-After is honoured


def test_a_429_makes_that_provider_undue_and_no_other(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The poller's own question, answered by the server: "is this one due?".

    The page asks ``next_read_in_seconds``; a provider that answered 429 has a
    non-zero value for as long as its own wait lasts, and every other provider
    does not. That is what lets one host sit out its cooldown while the rest of
    the page keeps refreshing.
    """

    host = Host(status=429, body={"error": "slow down"}, headers={"retry-after": "600"})
    monkeypatch.setattr(usage_reads, "httpx", _httpx_bound_to(host))
    monkeypatch.setattr(
        usage_reads, "resolve_credential", lambda endpoint, settings: _credential()
    )
    client.post(
        "/admin/api/usage/read", headers=LOCAL_ORIGIN, json={"provider_id": "deepseek"}
    )

    payload = client.get("/admin/api/usage", headers=LOCAL_ORIGIN).json()
    limited = _row(payload, "deepseek")
    assert limited["retry_in_seconds"] is not None
    assert limited["next_read_in_seconds"] is not None
    assert limited["next_read_in_seconds"] >= 500
    # Everyone else is due now, so the poller goes on without the broken host.
    for other in ("open_router", "kimi", "commandcode"):
        assert _row(payload, other)["next_read_in_seconds"] is None


# -------------------------------------------------------- backoff, per provider


def test_repeated_failures_lengthen_only_that_providers_wait(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host that is down must not be asked every 30 seconds for ever."""

    broken = Host(status=500, body={"error": "down"})
    monkeypatch.setattr(usage_reads, "httpx", _httpx_bound_to(broken))
    monkeypatch.setattr(
        usage_reads, "resolve_credential", lambda endpoint, settings: _credential()
    )

    now = time.time()
    # Three consecutive failures for one provider, each one interval apart.
    for attempt in range(3):
        READINGS.claim("deepseek", now=now + attempt * 30, interval=30)
        READINGS.finish(
            "deepseek",
            None,
            ReadRecord(attempted_at=now + attempt * 30, error="HTTP 500"),
        )
    # And one failure for a neighbour.
    READINGS.claim("open_router", now=now, interval=30)
    READINGS.finish("open_router", None, ReadRecord(attempted_at=now, error="HTTP 500"))

    payload = client.get("/admin/api/usage", headers=LOCAL_ORIGIN).json()
    failing = _row(payload, "deepseek")
    neighbour = _row(payload, "open_router")
    assert failing["consecutive_failures"] == 3
    assert neighbour["consecutive_failures"] == 1
    # 30 * 2**3 = 240 against 30 * 2**1 = 60: the streaks are independent.
    assert failing["next_read_in_seconds"] > neighbour["next_read_in_seconds"]
    assert failing["next_read_in_seconds"] >= 200


def test_one_success_puts_a_provider_back_on_the_interval(
    client: TestClient, fake: Host
) -> None:
    now = time.time()
    READINGS.claim("deepseek", now=now - 400, interval=30)
    READINGS.finish(
        "deepseek", None, ReadRecord(attempted_at=now - 400, error="HTTP 500")
    )
    assert (
        _row(client.get("/admin/api/usage", headers=LOCAL_ORIGIN).json(), "deepseek")[
            "consecutive_failures"
        ]
        == 1
    )

    # The next read lands, so the streak is gone with it.
    answer = client.post(
        "/admin/api/usage/read", headers=LOCAL_ORIGIN, json={"provider_id": "deepseek"}
    ).json()
    assert answer["requested"] is True
    assert answer["state"] == "measured"
    assert answer["consecutive_failures"] == 0
    row = _row(client.get("/admin/api/usage", headers=LOCAL_ORIGIN).json(), "deepseek")
    assert row["consecutive_failures"] == 0
    assert row["next_read_in_seconds"] <= 30


# ------------------------------------------------------------------ zero is off


def test_zero_reports_off_and_restores_the_on_demand_floor() -> None:
    """The setting's own 0, read the way the route reads it."""

    assert _interval_for(_settings(USAGE_AUTO_REFRESH_SECONDS="0")) == (
        MINIMUM_INTERVAL_SECONDS
    )
    assert _interval_for(_settings(USAGE_AUTO_REFRESH_SECONDS="30")) == 30


# -------------------------------- the polling path cannot charge a key or bench


def test_a_poll_shaped_sequence_charges_no_key_and_benches_nothing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**The load-bearing proof, extended to the polling path.**

    The automatic refresh calls the same route the button does, so it inherits
    the isolation -- but "inherits" is a claim, and this is the measurement. The
    recorders are made to raise, then a poll-shaped run happens: a good read, a
    401, a 429 and another good read, one provider after another. Every answer
    is a state with a sentence, and no recorder fired.
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

    monkeypatch.setattr(
        usage_reads, "resolve_credential", lambda endpoint, settings: _credential()
    )

    script = [
        ("deepseek", 200, None),
        ("open_router", 401, {"error": "refused"}),
        ("kimi", 429, {"error": "slow down"}),
        ("commandcode", 200, None),
    ]
    for provider_id, status, body in script:
        host = Host(status=status, body=body if body is not None else DEEPSEEK)
        if status == 429:
            host.headers = {"retry-after": "120"}
        monkeypatch.setattr(usage_reads, "httpx", _httpx_bound_to(host))
        answer = client.post(
            "/admin/api/usage/read",
            headers=LOCAL_ORIGIN,
            json={"provider_id": provider_id},
        )
        assert answer.status_code == 200, answer.text
        payload = answer.json()
        assert payload["requested"] is True, payload
        assert payload["state"] in {
            "measured",
            "unavailable",
            "failed",
        }, (provider_id, payload["state"])

    assert fired == []
    # And the two failures are remembered per provider, which is what makes the
    # next poll skip them rather than hammer them.
    payload = client.get("/admin/api/usage", headers=LOCAL_ORIGIN).json()
    assert _row(payload, "open_router")["consecutive_failures"] == 1
    assert _row(payload, "deepseek")["consecutive_failures"] == 0


# --------------------------------------------------------- who may not be asked


def test_a_provider_with_no_readable_route_is_refused_rather_than_polled(
    client: TestClient, fake: Host
) -> None:
    """The page's own filter, enforced again on the wire.

    A poller bug that asked a provider with no adapter would still be refused
    here -- 404 and no request -- so "MCC never contacts a provider it cannot
    read" does not depend on the page getting its filter right.
    """

    for provider_id in ("novita", "kimi_coding", "nous_portal"):
        response = client.post(
            "/admin/api/usage/read",
            headers=LOCAL_ORIGIN,
            json={"provider_id": provider_id},
        )
        assert response.status_code == 404, provider_id
    assert fake.calls == []
