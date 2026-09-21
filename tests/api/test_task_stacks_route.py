"""``GET /admin/api/tasks/stacks``: bounded, loopback-only, frames only.

The on-demand half of the watchdog. What it has to answer is the question the
09-16 investigation could not: not "is something stuck" but "what is it stuck
*on*", and, for nine requests at once, whether they are stuck on the same
thing.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from my_claude_code.core import request_tasks
from my_claude_code.core.request_tasks import PHASE_AWAITING_FIRST_BYTE, RequestProgress
from tests.api.support import create_test_app

SECRET = "sk-route-SECRET-5555555555"


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
            attempt_index=0,
            provider="opencode",
            model_ref="opencode/muse-spark-1.3-contributor-free",
            key_label="sk-8...Kofx",
            proxy_label="173.249.24.121:1080",
        ),
    )
    assert entry is not None
    return entry


def test_an_idle_server_answers_with_nothing_in_flight() -> None:
    app = create_test_app()
    with _client(app) as client:
        response = client.get("/admin/api/tasks/stacks")
    assert response.status_code == 200
    body = response.json()
    assert body["in_flight"] == 0
    assert body["requests"] == []
    assert body["stuck"] == 0
    assert body["watchdog_enabled"] is True
    assert "tasks_by_deepest_frame" in body


def test_a_non_loopback_caller_is_refused() -> None:
    app = create_test_app()
    with TestClient(app, client=("10.1.2.3", 50000)) as client:
        response = client.get("/admin/api/tasks/stacks")
    assert response.status_code in (403, 404)


def test_the_caps_are_honoured_and_truncation_is_declared() -> None:
    app = create_test_app()
    for index in range(8):
        _register(f"req_{index}")
    with _client(app) as client:
        body = client.get("/admin/api/tasks/stacks?limit=3&tasks=2&frames=2").json()
    assert body["in_flight"] == 8
    assert body["shown"] == 3
    assert body["truncated"] is True
    assert body["tasks_by_deepest_frame"]["examined"] <= 2
    for row in body["requests"]:
        for task in row["tasks"]:
            assert len(task["frames"]) <= 2


def test_a_nonsense_cap_falls_back_to_the_default() -> None:
    app = create_test_app()
    with _client(app) as client:
        body = client.get("/admin/api/tasks/stacks?limit=nope&frames=-9").json()
    assert body["shown"] == 0


@pytest.mark.asyncio
async def test_nine_parked_requests_group_under_one_frame() -> None:
    """The view that would have settled 09-16 in a single look."""

    app = create_test_app()
    lock = asyncio.Lock()
    release = asyncio.Event()
    ready = asyncio.Event()
    started = 0

    async def serve(index: int, api_key: str) -> None:
        nonlocal started
        held_locally = api_key
        assert held_locally
        _register(f"req_parked_{index}")
        started += 1
        if started == 9:
            ready.set()
        async with lock:
            await release.wait()

    async with lock:
        tasks = [asyncio.create_task(serve(i, SECRET)) for i in range(9)]
        await ready.wait()
        for _ in range(8):
            await asyncio.sleep(0)
        from my_claude_code.core.stuck_requests import stuck_report

        report = await stuck_report(stall_seconds=0.0)
        release.set()
    await asyncio.gather(*tasks, return_exceptions=True)

    assert report["in_flight"] == 9
    groups = report["tasks_by_deepest_frame"]["groups"]
    by_frame = {row["frame"]: row["tasks"] for row in groups}
    acquiring = {
        frame: count for frame, count in by_frame.items() if "acquire" in frame
    }
    assert sum(acquiring.values()) == 9, groups
    assert len(acquiring) == 1, acquiring
    blob = repr(report)
    assert SECRET not in blob
    assert "sk-route" not in blob
    del app


def test_the_answer_carries_no_secret_and_no_home_path() -> None:
    app = create_test_app()
    _register("req_private")
    with _client(app) as client:
        text = client.get("/admin/api/tasks/stacks").text
    assert SECRET not in text
    assert "Users\\\\" not in text
    assert "/home/" not in text


@pytest.mark.asyncio
async def test_building_the_report_never_holds_the_loop() -> None:
    """The measurement that chose the batch size, asserted without a clock.

    Building a stack is synchronous: 0.66 ms per request on the scratch rig,
    so a hundred in-flight requests is ~130 ms of walking, and answering that
    in one slice would hold this server's single event loop for a quarter of
    the busy threshold 7.27.0 publishes. The build therefore yields every
    ``YIELD_EVERY`` requests and every ``YIELD_EVERY`` tasks.

    Asserted by counting turns rather than milliseconds: a task that does
    nothing but ``await asyncio.sleep(0)`` gets exactly one turn per yield the
    build makes, so the count IS the number of yields. A wall-clock bound here
    would measure the CI runner instead, which is what the first version of
    this test did and why it failed there and not locally.
    """

    from my_claude_code.core.async_stacks import YIELD_EVERY
    from my_claude_code.core.stuck_requests import stuck_report

    release = asyncio.Event()
    ready = asyncio.Event()
    started = 0

    async def nested(level: int):
        if level == 0:
            await release.wait()
            yield 1
            return
        async for item in nested(level - 1):
            yield item

    async def serve(index: int) -> None:
        nonlocal started
        _register(f"req_cost_{index}")
        started += 1
        if started == 100:
            ready.set()
        async for _ in nested(12):
            pass

    tasks = [asyncio.create_task(serve(i)) for i in range(100)]
    await ready.wait()
    for _ in range(12):
        await asyncio.sleep(0)

    turns = 0
    spinning = True

    async def ticker() -> None:
        nonlocal turns
        while spinning:
            await asyncio.sleep(0)
            turns += 1

    beat = asyncio.create_task(ticker())
    try:
        await asyncio.sleep(0)
        before = turns
        report = await stuck_report(stall_seconds=0.0, request_limit=200)
        gained = turns - before
        spinning = False
        await asyncio.gather(beat, return_exceptions=True)
        assert report["in_flight"] == 100
        # One hundred requests at ten per slice is ten yields on its own, and
        # the all-task summary adds about as many again. Anything that stopped
        # yielding would land at one or two.
        assert gained >= 100 // YIELD_EVERY, gained
    finally:
        spinning = False
        beat.cancel()
        release.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, beat, return_exceptions=True)
