"""Free-text searches on their own pool, never on the default executor (7.91.1).

On 2026-10-09 a saved all-time search held six default-executor workers for
twenty-five minutes; the Requests table, lifetime and every other dashboard
read queued behind it, and a stop overran its budget. These tests check the
routes the way the server runs them:

* with the default executor full, every search route still answers -- none of
  them needs it -- and every body read happens on a search worker;
* with both search workers busy scanning, a finished completion's stream still
  closes at once (7.90.1's regression, with real scans instead of parked
  threads);
* a stop with searches running finishes inside its budget, and a request still
  waiting is told the server is stopping;
* a search answer is still kept for the minute the page relies on.
"""

import asyncio
import json
import random
import threading
import time
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

from my_claude_code.api import admin_routes
from my_claude_code.api.request_capture import RequestCapture
from my_claude_code.api.search_pool import close_search_pool, run_on_search_pool
from my_claude_code.application.search_jobs import SearchStopped, search_jobs
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.request_log import RequestLogStore, RequestRecord
from my_claude_code.core.stop_deadline import SHUTDOWN_MARKER_HEADER, stop_deadline
from my_claude_code.runtime.application import ApplicationRuntime
from my_claude_code.runtime.provider_manager import ProviderRuntimeManager
from tests.api.support import create_test_app
from tests.support.search_log import build_search_log, make_record

pytestmark = pytest.mark.local_serial

SEARCH_ROUTES = (
    "/admin/api/requests?limit=5",
    "/admin/api/requests/count",
    "/admin/api/requests/stats",
    "/admin/api/requests/cost",
    "/admin/api/requests/ttft",
    "/admin/api/requests/no-answer",
    "/admin/api/requests/origin",
    "/admin/api/requests/pulse",
)


class Reads:
    """Counts (and can slow) the body predicate, and says where it ran."""

    def __init__(self, store: RequestLogStore, delay: float = 0.0) -> None:
        self.threads: set[str] = set()
        self.delay = delay
        self._real = store._bodies_match

    def __call__(self, *args: Any) -> int:
        self.threads.add(threading.current_thread().name)
        if self.delay:
            time.sleep(self.delay)
        return self._real(*args)


@pytest.fixture
def store(tmp_path) -> Iterator[RequestLogStore]:
    built, _times = build_search_log(tmp_path / "requests.db", rows=120)
    yield built
    built.close()


@pytest.fixture
def client_for(monkeypatch):
    """An HTTP client on the running loop, the routes reading ``store``."""

    @asynccontextmanager
    async def make(store: RequestLogStore) -> AsyncIterator[httpx.AsyncClient]:
        monkeypatch.setattr(
            admin_routes, "_request_log_store_or_none", lambda settings: store
        )
        transport = httpx.ASGITransport(
            app=create_test_app(), client=("127.0.0.1", 50000)
        )
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1:8082"
        ) as client:
            yield client

    return make


@asynccontextmanager
async def _default_executor_full() -> AsyncIterator[None]:
    """Every default-executor worker parked, the way the old scans held it."""

    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="scan")
    loop.set_default_executor(executor)
    release = threading.Event()
    parked = [loop.run_in_executor(None, release.wait) for _ in range(8)]
    probe = asyncio.ensure_future(asyncio.to_thread(lambda: None))
    try:
        # The premise, checked: a fresh to_thread really cannot get a worker.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(probe), 0.3)
        yield
    finally:
        release.set()
        await asyncio.gather(*parked, probe)
        executor.shutdown(wait=True)


@pytest.mark.asyncio
async def test_every_search_route_answers_while_the_default_executor_is_full(
    store, client_for, monkeypatch
) -> None:
    expected = store.count_requests(q="x", local="hide")
    reads = Reads(store)
    monkeypatch.setattr(store, "_bodies_match", reads)
    async with client_for(store) as client, _default_executor_full():
        responses = await asyncio.wait_for(
            asyncio.gather(
                *(
                    client.get(path, params={"q": "x", "local": "hide"})
                    for path in SEARCH_ROUTES
                )
            ),
            10.0,
        )

    assert [response.status_code for response in responses] == [200] * len(
        SEARCH_ROUTES
    )
    assert responses[1].json()["total"] == expected
    assert reads.threads and all(
        name.startswith("mcc-search") for name in reads.threads
    )


def _capture(store: RequestLogStore) -> RequestCapture:
    request = MessagesRequest.model_validate(
        {
            "model": "claude-sonnet-4-5",
            "max_tokens": 32,
            "messages": [{"role": "user", "content": "ping"}],
        }
    )
    capture = RequestCapture(
        store,
        request_id="req_search_pool",
        endpoint="/v1/messages",
        protocol="anthropic",
        stream=True,
        requested_model="claude-sonnet-4-5",
        input_text="ping",
        params={"max_tokens": 32},
        request=request,
    )
    capture._record.provider = "nvidia_nim"
    capture._record.resolved_model = "acme-1"
    return capture


async def _body() -> AsyncIterator[str]:
    for event, data in (
        (
            "message_start",
            {"type": "message_start", "message": {"usage": {"input_tokens": 5}}},
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "pong"},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ):
        yield f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def _never() -> bool:
    return False


def _scan(store: RequestLogStore) -> asyncio.Future[Any]:
    filters = {"q": "x", "local": None, "since": None, "until": None}
    return asyncio.ensure_future(
        search_jobs().answer(
            store,
            filters,
            lambda matched: store.count_requests(matched=matched, q="x"),
            run=run_on_search_pool,
            disconnected=_never,
        )
    )


@pytest.mark.asyncio
async def test_a_stream_closes_while_scans_fill_the_search_pool(
    tmp_path, monkeypatch
) -> None:
    """7.90.1's regression with real searches: two passes on two logs."""

    first, _ = build_search_log(tmp_path / "a.db", rows=120)
    second, _ = build_search_log(tmp_path / "b.db", rows=120, seed=8)
    for one in (first, second):
        monkeypatch.setattr(one, "_bodies_match", Reads(one, delay=0.05))
    scans = [_scan(first), _scan(second)]
    await asyncio.sleep(0.3)
    assert len(search_jobs().running()) == 2
    rows: list[RequestRecord] = []
    finisher = RequestLogStore(tmp_path / "c.db")
    monkeypatch.setattr(finisher, "enqueue", rows.append)

    async def read_to_the_end() -> list[str]:
        return [chunk async for chunk in _capture(finisher).wrap(_body())]

    started = time.perf_counter()
    chunks = await asyncio.wait_for(read_to_the_end(), 2.0)
    closed_after = time.perf_counter() - started
    still_scanning = len(search_jobs().running())
    await close_search_pool()
    for scan in scans:
        # Stopped by the close, and told so.
        with pytest.raises(SearchStopped):
            await scan
    finisher.close()
    first.close()
    second.close()

    assert closed_after < 1.0
    assert still_scanning == 2
    assert len(chunks) == 3
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_a_stop_with_searches_running_finishes_within_its_budget(
    store, monkeypatch
) -> None:
    monkeypatch.setattr(store, "_bodies_match", Reads(store, delay=0.05))
    scan = _scan(store)
    await asyncio.sleep(0.3)
    assert search_jobs().running()
    manager = ProviderRuntimeManager(
        Settings().model_copy(update={"model": "nvidia_nim/model"})
    )
    runtime = ApplicationRuntime(manager, transcriber=None)
    budget = stop_deadline().request(5.0)
    started = time.perf_counter()
    try:
        closed = await asyncio.wait_for(
            runtime.close(), stop_deadline().teardown_remaining()
        )
        took = time.perf_counter() - started
    finally:
        stop_deadline().clear()
    # Told the server is stopping.
    with pytest.raises(SearchStopped):
        await scan

    assert closed is True
    assert took < budget
    assert took < 2.5
    assert not search_jobs().running()
    # The scanning thread came back at once: both workers answer now.
    assert await asyncio.wait_for(
        asyncio.gather(run_on_search_pool(lambda: 1), run_on_search_pool(lambda: 2)),
        0.5,
    ) == [1, 2]
    await close_search_pool()


@pytest.mark.asyncio
async def test_a_request_still_waiting_is_told_the_server_is_stopping(
    store, client_for, monkeypatch
) -> None:
    monkeypatch.setattr(store, "_bodies_match", Reads(store, delay=0.05))
    async with client_for(store) as client:
        pending = asyncio.ensure_future(
            client.get("/admin/api/requests/count", params={"q": "x"})
        )
        await asyncio.sleep(0.3)
        stop_deadline().request(5.0)
        try:
            response = await asyncio.wait_for(pending, 2.0)
        finally:
            stop_deadline().clear()
    await close_search_pool()

    assert response.status_code == 503
    assert response.headers.get(SHUTDOWN_MARKER_HEADER) == "1"


@pytest.mark.asyncio
async def test_a_search_answer_is_kept_for_the_minute_like_any_other(
    store, client_for, monkeypatch
) -> None:
    calls = {"stats": 0}
    real = RequestLogStore.stats

    def counting(self: RequestLogStore, *args: Any, **kwargs: Any) -> dict[str, Any]:
        calls["stats"] += 1
        return real(self, *args, **kwargs)

    monkeypatch.setattr(RequestLogStore, "stats", counting)
    async with client_for(store) as client:
        first = await client.get("/admin/api/requests/stats", params={"q": "x"})
        second = await client.get("/admin/api/requests/stats", params={"q": "x"})

    assert first.status_code == second.status_code == 200
    assert calls["stats"] == 1
    assert first.json() == second.json()
    assert first.json()["total"] == store.count_requests(q="x")


PROGRESS_ROUTE = "/admin/api/requests/search-progress"


@pytest.mark.asyncio
async def test_the_progress_route_reads_a_running_search_without_waiting_for_it(
    store, client_for, monkeypatch
) -> None:
    """7.91.2: the page's progress line, from the search its count waits for.

    The term is found nowhere, so the count waits for every row; the route
    answers at once all the while, with the rows read growing towards the
    rows in the window, and starts nothing when nothing is running.
    """

    total = store.count_requests(local="hide")
    reads = Reads(store, delay=0.01)
    monkeypatch.setattr(store, "_bodies_match", reads)
    params = {"q": "zqxjvkw", "local": "hide"}
    async with client_for(store) as client:
        before = await client.get(PROGRESS_ROUTE, params=params)
        no_search = await client.get(PROGRESS_ROUTE, params={"local": "hide"})
        assert not search_jobs().running()
        count = asyncio.ensure_future(
            client.get("/admin/api/requests/count", params=params)
        )
        seen: list[dict[str, Any]] = []
        slowest = 0.0
        while not count.done():
            await asyncio.sleep(0.1)
            asked = time.perf_counter()
            answer = await client.get(PROGRESS_ROUTE, params=params)
            slowest = max(slowest, time.perf_counter() - asked)
            assert answer.status_code == 200
            seen.append(answer.json())
        counted = await count
        final = (await client.get(PROGRESS_ROUTE, params=params)).json()

    assert before.json() == {"enabled": True, "searching": False}
    assert no_search.json() == {"enabled": True, "searching": False}
    assert counted.json() == {"enabled": True, "total": 0}
    running = [s for s in seen if s["searching"] and not s["finished"]]
    assert len(running) >= 3, seen
    assert all(s["total"] == total and 0 <= s["read"] < total for s in running)
    assert [s["read"] for s in running] == sorted(s["read"] for s in running)
    assert final["searching"] is True and final["finished"] is True
    assert final["read"] == final["total"] == total
    assert final["matched"] == 0
    assert isinstance(final["searched_back_to"], float)
    # Never waits for the pass: each answer is a read of the search's state.
    assert slowest < 0.5
    assert reads.threads and all(
        name.startswith("mcc-search") for name in reads.threads
    )


class Calls:
    """Counts the body predicate, the expensive part of a pass."""

    def __init__(self, store: RequestLogStore) -> None:
        self.calls = 0
        self._lock = threading.Lock()
        self._real = store._bodies_match

    def __call__(self, *args: Any) -> int:
        with self._lock:
            self.calls += 1
        return self._real(*args)


# What the page asks for in one load with a search, since 7.91.2: the seven
# answers, the progress line, and (auto-refresh) the pulse.
PAGE_LOAD_ROUTES = (
    "/admin/api/requests?limit=25&offset=0",
    "/admin/api/requests/count",
    "/admin/api/requests/stats",
    "/admin/api/requests/cost",
    "/admin/api/requests/ttft",
    "/admin/api/requests/no-answer",
    "/admin/api/requests/origin",
    PROGRESS_ROUTE,
)


@pytest.mark.asyncio
async def test_auto_refresh_after_a_search_reads_only_the_new_rows(
    tmp_path, client_for, monkeypatch
) -> None:
    """Q12: auto-refresh stays on during a search and checks only new rows.

    The page's own sequence after the pass: the pulse, then a full reload of
    every answer (the totals are due after a minute). With rows written since
    the pass, the bodies read are those rows, once each, and nothing else.
    """

    path = tmp_path / "requests.db"
    store, times = build_search_log(path, rows=120)
    params = {"q": "x", "local": "hide"}
    calls = Calls(store)
    monkeypatch.setattr(store, "_bodies_match", calls)
    async with client_for(store) as client:
        first = await asyncio.gather(
            *(client.get(route, params=params) for route in PAGE_LOAD_ROUTES)
        )
        assert [response.status_code for response in first] == [200] * len(
            PAGE_LOAD_ROUTES
        )
        one_pass = calls.calls
        writer = RequestLogStore(path, max_rows=0)
        rng = random.Random(5)
        for index in range(4):
            record = make_record(rng, 900 + index, max(times) + 10 + index)
            record.input_text = f"a newer prompt with x in it {index}"
            record.output_text = ""
            record.thinking_text = None
            record.tool_calls = None
            writer.enqueue(record)
        writer.close()
        calls.calls = 0
        pulse = await client.get("/admin/api/requests/pulse", params=params)
        reload = await asyncio.gather(
            *(client.get(route, params=params) for route in PAGE_LOAD_ROUTES)
        )
        after_reload = calls.calls

    monkeypatch.undo()
    assert one_pass > 50
    assert pulse.status_code == 200
    assert [response.status_code for response in reload] == [200] * len(
        PAGE_LOAD_ROUTES
    )
    # The four new rows, read once each: not another pass.
    assert after_reload == 4
    assert reload[1].json()["total"] == store.count_requests(**params)
    assert pulse.json()["total"] == store.count_requests(**params)
    store.close()
