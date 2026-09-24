"""``GET /admin/api/requests/in-flight``: loopback-only, memory-only, bounded.

The endpoint is polled every few seconds by the dashboard, so the properties
that matter are the ones that keep it cheap and harmless: it never touches the
request log database, never walks a stack, answers the same disabled shape
``pulse`` does, bounds what it returns and says when it cut something.
"""

import asyncio
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from my_claude_code.config.settings import Settings
from my_claude_code.core import request_tasks
from my_claude_code.core.request_tasks import (
    PHASE_ATTEMPT,
    PHASE_AWAITING_FIRST_BYTE,
    RequestProgress,
)
from tests.api.support import create_test_app, runtime_for_app

ROUTE = "/admin/api/requests/in-flight"


def _client(app) -> TestClient:
    return TestClient(app, client=("127.0.0.1", 50000))


def _register(request_id: str) -> request_tasks.RequestTaskEntry:
    entry = request_tasks.register(
        request_id=request_id,
        endpoint="/v1/messages",
        protocol="anthropic",
        stream=True,
        harness="claude",
        requested_model="mcc/best",
        progress=lambda: RequestProgress(
            phase=PHASE_AWAITING_FIRST_BYTE,
            attempt_mono=0.0,
            attempt_index=0,
            provider="opencode",
            model_ref="opencode/big-pickle",
            key_label="sk-8...Kofx",
        ),
    )
    assert entry is not None
    return entry


def test_an_idle_server_answers_with_nothing_in_flight() -> None:
    with _client(create_test_app()) as client:
        response = client.get(ROUTE)
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["total"] == 0
    assert body["rows"] == []
    assert body["truncated"] is False
    assert isinstance(body["now_mono"], float)
    assert "reaped" in body


def test_inflight_endpoint_requires_loopback() -> None:
    with TestClient(create_test_app(), client=("10.1.2.3", 50000)) as client:
        response = client.get(ROUTE)
    assert response.status_code in (403, 404)


def test_the_rows_are_the_registry_oldest_first() -> None:
    app = create_test_app()
    with _client(app) as client:
        for index in range(3):
            entry = _register(f"req_{index}")
            entry.started_at_mono -= 10 - index
        body = client.get(ROUTE).json()
    assert body["total"] == 3
    assert [row["id"] for row in body["rows"]] == ["req_0", "req_1", "req_2"]
    row = body["rows"][0]
    assert row["phase"] == PHASE_ATTEMPT
    assert row["provider"] == "opencode"
    assert row["key_label"] == "sk-8...Kofx"
    assert row["elapsed_ms"] >= 10_000


def test_the_limit_is_honoured_and_the_cut_declared() -> None:
    app = create_test_app()
    with _client(app) as client:
        for index in range(8):
            _register(f"req_{index}")
        body = client.get(f"{ROUTE}?limit=3").json()
        fallback = client.get(f"{ROUTE}?limit=nope").json()
    assert body["total"] == 8
    assert body["shown"] == 3
    assert len(body["rows"]) == 3
    assert body["truncated"] is True
    assert fallback["shown"] == 8


async def _adopt(settings: Settings):
    """Run the lifespan step that adopts the two switches, as the server does.

    ``create_test_app`` composes the API without starting the runtime, so the
    step is driven directly -- the same method ``ApplicationRuntime.start``
    calls -- and the watchdog task it may start is closed again.
    """

    app = create_test_app(settings)
    runtime = runtime_for_app(app)
    runtime._start_stall_watchdog()
    watchdog = runtime._stall_watchdog
    if watchdog is not None:
        await watchdog.close()
    return app


@pytest.mark.asyncio
async def test_the_switch_off_answers_the_pulse_shape_and_the_watchdog_keeps_working() -> (
    None
):
    settings = Settings()
    settings.request_inflight_enabled = False
    app = await _adopt(settings)
    # The view is off; the watchdog's registry (on by default) is not.
    assert request_tasks.inflight_enabled() is False
    assert request_tasks.enabled() is True
    _register("req_hidden")
    with _client(app) as client:
        assert client.get(ROUTE).json() == {"enabled": False}
        assert client.get("/admin/api/tasks/stacks").json()["in_flight"] == 1


@pytest.mark.asyncio
async def test_the_view_alone_keeps_the_registry_on() -> None:
    settings = Settings()
    settings.request_watchdog_enabled = False
    app = await _adopt(settings)
    assert request_tasks.inflight_enabled() is True
    _register("req_seen")
    with _client(app) as client:
        assert client.get(ROUTE).json()["total"] == 1


@pytest.mark.asyncio
async def test_both_switches_off_leaves_nothing_registered() -> None:
    settings = Settings()
    settings.request_inflight_enabled = False
    settings.request_watchdog_enabled = False
    app = await _adopt(settings)
    assert request_tasks.enabled() is False
    assert (
        request_tasks.register(
            request_id="req_off",
            endpoint="/v1/messages",
            protocol="anthropic",
            stream=True,
            harness=None,
            requested_model=None,
            progress=None,
        )
        is None
    )
    with _client(app) as client:
        assert client.get(ROUTE).json() == {"enabled": False}


def test_inflight_endpoint_makes_no_db_query() -> None:
    """Patch every door to the request log; none may be opened."""

    app = create_test_app()
    with _client(app) as client:
        for index in range(5):
            _register(f"req_{index}")
        with (
            patch(
                "my_claude_code.api.admin_routes._request_log_store_or_none",
                side_effect=AssertionError("in-flight touched the request log"),
            ) as store_door,
            patch(
                "my_claude_code.core.request_log.RequestLogStore._connect",
                side_effect=AssertionError("in-flight opened a connection"),
            ) as connection_door,
            patch(
                "my_claude_code.core.stuck_requests.describe_task",
                side_effect=AssertionError("in-flight walked a stack"),
            ) as stack_walk,
        ):
            response = client.get(ROUTE)
    assert response.status_code == 200
    assert response.json()["total"] == 5
    assert store_door.call_count == 0
    assert connection_door.call_count == 0
    assert stack_walk.call_count == 0


def test_pulse_carries_the_count_and_keeps_its_change_keys() -> None:
    app = create_test_app()
    with _client(app) as client:
        before = client.get("/admin/api/requests/pulse").json()
        _register("req_pulse")
        _register("req_pulse_2")
        after = client.get("/admin/api/requests/pulse").json()
    if before.get("enabled") is False:
        pytest.skip("request log disabled in this composition")
    assert before["in_flight"] == 0
    assert after["in_flight"] == 2
    # The page decides "did anything change" from these two only, so an
    # in-flight count moving can never trigger a table refresh on its own.
    assert after["total"] == before["total"]
    assert after["last_ts"] == before["last_ts"]


@pytest.mark.asyncio
async def test_a_hundred_parked_requests_answer_in_one_bounded_read() -> None:
    """The shape the endpoint has to survive: many requests, all parked.

    No stack walk and no await: one lock, one reap, one loop. Asserted by
    counting reads rather than milliseconds, for the reason
    ``tests/api/test_task_stacks_route.py`` gives -- a wall-clock bound
    measures the runner. The live numbers are in the PR.
    """

    reads = 0
    release = asyncio.Event()
    ready = asyncio.Event()
    started = 0

    def reader() -> RequestProgress:
        nonlocal reads
        reads += 1
        return RequestProgress(phase=PHASE_AWAITING_FIRST_BYTE)

    async def serve(index: int) -> None:
        nonlocal started
        request_tasks.register(
            request_id=f"req_parked_{index}",
            endpoint="/v1/messages",
            protocol="anthropic",
            stream=True,
            harness="opencode",
            requested_model="mcc/best",
            progress=reader,
        )
        started += 1
        if started == 100:
            ready.set()
        await release.wait()

    tasks = [asyncio.create_task(serve(index)) for index in range(100)]
    await ready.wait()
    try:
        report = request_tasks.inflight_report(limit=50)
        assert report["total"] == 100
        assert report["shown"] == 50
        assert report["truncated"] is True
        # Exactly one progress read per row shown; the other fifty are counted,
        # never described.
        assert reads == 50
        ages = [row["elapsed_ms"] for row in report["rows"]]
        assert ages == sorted(ages, reverse=True)
    finally:
        release.set()
        await asyncio.gather(*tasks)
    # Every serving task has finished: the next read reaps all hundred.
    after = request_tasks.inflight_report()
    assert after["total"] == 0
