"""The stop is bounded end to end, and a new request cannot extend it.

Every test here pins one row of the shutdown contract introduced in 6.41.0.
The bug they exist for: with all five per-request deadlines at 0 (the shipped
default since 6.16.0), a request against a silent upstream made a stop take
forever -- uvicorn cancelled the request task at its bound, the shielded
cleanup in ``api.response_streams`` absorbed the cancellation, the provider
lease was never released, and the lifespan waited on ``drained`` with no
timeout at all. Reproduced on a scratch server before the fix: a 5s bound
overran to +39.56s and was still climbing.

The bounds are deliberately tiny here (1-2s) so the suite pays milliseconds for
behaviour measured in minutes.
"""

import asyncio
import time
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.responses import ContentStream
from starlette.types import Message, Scope

from my_claude_code.api.response_streams import ManagedStreamingResponse
from my_claude_code.application.errors import ApplicationUnavailableError
from my_claude_code.config.constants import (
    SERVER_GRACEFUL_SHUTDOWN_SECONDS_DEFAULT,
)
from my_claude_code.config.settings import Settings
from my_claude_code.core import stop_deadline as stop_deadline_module
from my_claude_code.core.stop_deadline import (
    MAX_STOP_BUDGET_SECONDS,
    MIN_STOP_BUDGET_SECONDS,
    STOP_TEARDOWN_MARGIN_SECONDS,
    StopDeadline,
    clamp_stop_budget,
    stop_deadline,
)
from my_claude_code.providers.base import BaseProvider
from my_claude_code.providers.runtime import ProviderRuntime
from my_claude_code.runtime.asgi import RuntimeASGIApp
from my_claude_code.runtime.provider_manager import ProviderRuntimeManager

# The bound each test hands the supervisor. Small, because the point is the
# *shape* of the wait, not its size.
BOUND = 1.0
# What a caller is allowed to observe on top of the bound: the fixed teardown
# margin plus scheduling slack for a loaded CI machine.
SLACK = 4.0


class _FakeRuntime(ProviderRuntime):
    """A provider runtime whose cleanup is instant and observable."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.cleanup_calls = 0
        self.provider = MagicMock()
        self.provider.list_model_infos = AsyncMock(return_value=frozenset())

    def is_cached(self, provider_id: str) -> bool:
        return False

    def resolve_provider(self, provider_id: str) -> BaseProvider:
        return cast(BaseProvider, self.provider)

    async def cleanup(self) -> None:
        self.cleanup_calls += 1


def _settings(**overrides: object) -> Settings:
    return Settings().model_copy(update={"model": "nvidia_nim/one", **overrides})


def _manager() -> ProviderRuntimeManager:
    return ProviderRuntimeManager(_settings(), runtime_factory=_FakeRuntime)


async def _drive_lifespan(app: RuntimeASGIApp) -> str:
    """Run one startup + shutdown through the ASGI lifespan protocol."""

    messages: list[Message] = [
        {"type": "lifespan.startup"},
        {"type": "lifespan.shutdown"},
    ]
    sent: list[Message] = []

    async def receive() -> Message:
        return messages.pop(0)

    async def send(message: Message) -> None:
        sent.append(message)

    await app(cast(Scope, {"type": "lifespan"}), receive, send)
    return str(sent[-1]["type"])


async def _receive() -> Message:
    return {"type": "http.request"}


def _http_scope(path: str) -> Scope:
    return cast(Scope, {"type": "http", "path": path})


# --------------------------------------------------------------------------
# T1 -- the headline: a stop with a hung upstream still ends.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_with_a_hung_upstream_exits_within_the_graceful_bound() -> None:
    """The whole chain that used to hang, driven end to end, now finishes.

    The shape of the real failure, reproduced with the real objects: a
    streaming response holding a provider lease, whose body close never returns
    (the silent upstream), and whose request task is then cancelled the way
    uvicorn cancels it at its own bound. Before 6.41.0 every one of the three
    waits after that point was unbounded and the process never exited.
    """

    manager = _manager()
    lease = await manager.acquire()

    never_returns = asyncio.Event()

    class _HungBody:
        def __aiter__(self) -> _HungBody:
            return self

        async def __anext__(self) -> bytes:
            raise StopAsyncIteration

        async def aclose(self) -> None:
            # The upstream socket is open and silent, and with every
            # per-request deadline at 0 nothing ends it.
            await never_returns.wait()

    response = ManagedStreamingResponse(cast(ContentStream, _HungBody()))
    response.bind_release(lease.release)

    async def hold_the_response() -> None:
        await response.aclose()

    request_task = asyncio.create_task(hold_the_response())
    await asyncio.sleep(0)

    started = time.monotonic()
    stop_deadline().request(BOUND)
    # uvicorn cancels the surviving request tasks at its own bound and does not
    # wait for them; that cancellation is what the cleanup used to absorb.
    request_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await request_task
    # The lease the abandoned response held is released, so the drain can end.
    await asyncio.wait_for(manager.close(), BOUND + SLACK)
    elapsed = time.monotonic() - started

    assert elapsed <= BOUND + STOP_TEARDOWN_MARGIN_SECONDS + SLACK
    never_returns.set()


# --------------------------------------------------------------------------
# T2 -- a new request during the drain is refused and does not extend it.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_new_request_during_the_drain_is_refused_and_does_not_extend_it() -> (
    None
):
    """503 + ``connection: close``, and ``drained`` is not re-cleared.

    uvicorn refuses new TCP *connections* the instant a stop begins, but a
    client holding an established keep-alive connection could still pipeline
    new requests down it for the whole drain -- each taking a lease, each lease
    re-clearing ``drained``, so the finish line moved every time. That is the
    "it also allows new ones" half of the report.
    """

    manager = _manager()
    runtime = MagicMock()
    runtime.close = AsyncMock(return_value=True)
    served: list[str] = []

    async def inner_app(scope, receive, send) -> None:
        served.append(str(scope["path"]))

    app = RuntimeASGIApp(inner_app, runtime)

    generation = manager._current
    assert generation.drained.is_set()

    stop_deadline().request(BOUND)

    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    await app(_http_scope("/v1/messages"), _receive, send)

    assert served == [], "a request during the drain must not reach the app"
    start = sent[0]
    assert start["status"] == 503
    headers = dict(start["headers"])
    assert headers[b"connection"] == b"close"
    assert b"retry-after" in headers

    # And the provider runtime refuses the lease too, for the paths that do not
    # arrive over HTTP at all (messaging, a managed CLI session).
    with pytest.raises(ApplicationUnavailableError, match="shutting down"):
        await manager.acquire()
    assert generation.drained.is_set(), "a refused request must not re-clear drained"


@pytest.mark.asyncio
async def test_health_and_admin_are_refused_the_same_way_during_a_stop() -> None:
    """One answer for a server that is going away, not two.

    The dashboard's reconnect loop treats a failed ``/admin/api/version`` as the
    expected mid-handoff disconnect and keeps polling until the new process
    answers, so uniform refusal costs it nothing and keeps the rule simple.
    """

    runtime = MagicMock()
    served: list[str] = []

    async def inner_app(scope, receive, send) -> None:
        served.append(str(scope["path"]))

    app = RuntimeASGIApp(inner_app, runtime)
    stop_deadline().request(BOUND)

    for path in ("/health", "/admin/api/version"):
        sent: list[Message] = []

        async def send(message: Message, sink: list[Message] = sent) -> None:
            sink.append(message)

        await app(_http_scope(path), _receive, send)
        assert sent[0]["status"] == 503

    assert served == []


@pytest.mark.asyncio
async def test_requests_are_served_normally_until_a_stop_is_requested() -> None:
    """The gate is closed only by a stop; nothing changes for a running server."""

    runtime = MagicMock()
    served: list[str] = []

    async def inner_app(scope, receive, send) -> None:
        served.append(str(scope["path"]))

    app = RuntimeASGIApp(inner_app, runtime)

    async def send(message: Message) -> None:
        raise AssertionError("the gate must not answer while the server is up")

    await app(_http_scope("/v1/messages"), _receive, send)
    assert served == ["/v1/messages"]


# --------------------------------------------------------------------------
# T3 -- the shielded cleanup no longer absorbs cancellation forever.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_response_cleanup_cancellation_is_not_swallowed_past_the_bound() -> None:
    """``_wait_for_cleanup`` gives up at the stop deadline.

    The shield and the re-awaiting loop are correct while the server is
    running: a caller cancelled mid-cleanup must not leave the body iterator
    half-closed. What was missing was a way out, and its absence is what turned
    uvicorn's force-cancel into a no-op.
    """

    never_returns = asyncio.Event()

    class _HungBody:
        def __aiter__(self) -> _HungBody:
            return self

        async def __anext__(self) -> bytes:
            raise StopAsyncIteration

        async def aclose(self) -> None:
            await never_returns.wait()

    response = ManagedStreamingResponse(cast(ContentStream, _HungBody()))
    stop_deadline().request(BOUND)

    started = time.monotonic()
    await asyncio.wait_for(response.aclose(), BOUND + SLACK)
    elapsed = time.monotonic() - started

    assert elapsed <= BOUND + STOP_TEARDOWN_MARGIN_SECONDS + SLACK
    never_returns.set()


@pytest.mark.asyncio
async def test_response_cleanup_still_waits_when_no_stop_was_requested() -> None:
    """No stop, no bound: an ordinary cancelled request still cleans up fully."""

    finished = asyncio.Event()

    class _SlowBody:
        def __aiter__(self) -> _SlowBody:
            return self

        async def __anext__(self) -> bytes:
            raise StopAsyncIteration

        async def aclose(self) -> None:
            await asyncio.sleep(0.05)
            finished.set()

    response = ManagedStreamingResponse(cast(ContentStream, _SlowBody()))
    await response.aclose()
    assert finished.is_set()


# --------------------------------------------------------------------------
# T4 -- the provider drain is bounded.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_manager_close_bounds_the_drain_wait() -> None:
    """A lease that is never released must not block ``close()`` forever.

    ``drained`` is set only when ``active_leases`` reaches 0, and the wait on it
    had no timeout at all. This is the line the 98-minute hang in the rotated
    logs was sitting on.
    """

    manager = _manager()
    await manager.acquire()  # never released

    stop_deadline().request(BOUND)
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="failed to close"):
        await asyncio.wait_for(manager.close(), BOUND + SLACK)
    elapsed = time.monotonic() - started

    assert elapsed <= BOUND + STOP_TEARDOWN_MARGIN_SECONDS + SLACK
    # The generation is deliberately left unclosed: the supervisor, not this
    # layer, decides what an overrun means (C7 refuses, C8 keeps serving).
    assert manager._closed is False


@pytest.mark.asyncio
async def test_provider_manager_close_still_drains_fully_without_a_stop() -> None:
    """The bound is a property of a stop, not a new timeout on every close."""

    manager = _manager()
    lease = await manager.acquire()
    close_task = asyncio.create_task(manager.close())
    await asyncio.sleep(0.05)

    assert not close_task.done(), "close must wait for a live lease"
    await lease.release()
    await asyncio.wait_for(close_task, SLACK)
    assert manager._closed is True


# --------------------------------------------------------------------------
# T5 -- the lifespan is bounded even when the runtime close hangs.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lifespan_shutdown_is_bounded_even_when_runtime_close_hangs() -> None:
    """``lifespan.shutdown.failed`` beats waiting forever.

    ``ApplicationRuntime.close`` deliberately applies no generic timeout: it
    would rather hand force-termination to "the process supervisor". Before
    6.41.0 the supervisor could never take that decision, because control never
    came back to it. Answering the lifespan is what gives it back.
    """

    never_returns = asyncio.Event()

    async def hanging_close() -> bool:
        await never_returns.wait()
        return True

    runtime = MagicMock()
    runtime.start = AsyncMock(return_value=None)
    runtime.close = hanging_close

    app = RuntimeASGIApp(AsyncMock(), runtime)
    stop_deadline().request(BOUND)

    started = time.monotonic()
    final = await asyncio.wait_for(_drive_lifespan(app), BOUND + SLACK)
    elapsed = time.monotonic() - started

    assert final == "lifespan.shutdown.failed"
    assert elapsed <= BOUND + STOP_TEARDOWN_MARGIN_SECONDS + SLACK
    never_returns.set()


@pytest.mark.asyncio
async def test_lifespan_shutdown_is_unbounded_when_no_stop_was_requested() -> None:
    """A clean close is never cut short; the bound belongs to the stop."""

    runtime = MagicMock()
    runtime.start = AsyncMock(return_value=None)
    runtime.close = AsyncMock(return_value=True)

    app = RuntimeASGIApp(AsyncMock(), runtime)
    assert await _drive_lifespan(app) == "lifespan.shutdown.complete"


# --------------------------------------------------------------------------
# T10 -- zero per-request deadlines do not unbound the stop (C11).
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_zero_per_request_deadlines_do_not_unbound_the_stop() -> None:
    """The 6.16.0 decision stands, and no longer costs the process its exit.

    Every fallback deadline at 0 still means MCC never ends a silent upstream
    on its own. It must no longer mean the server cannot be stopped.
    """

    settings = _settings(
        fallback_first_token_timeout=0.0,
        fallback_total_timeout=0.0,
        fallback_stall_timeout=0.0,
        fallback_reasoning_answer_timeout=0.0,
        server_graceful_shutdown_seconds=BOUND,
    )
    assert settings.fallback_total_timeout == 0.0

    manager = ProviderRuntimeManager(settings, runtime_factory=_FakeRuntime)
    await manager.acquire()  # a request with no deadline of its own

    stop_deadline().request(settings.server_graceful_shutdown_seconds)
    started = time.monotonic()
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(manager.close(), BOUND + SLACK)

    assert time.monotonic() - started <= BOUND + STOP_TEARDOWN_MARGIN_SECONDS + SLACK


# --------------------------------------------------------------------------
# The deadline object itself.
# --------------------------------------------------------------------------


def test_the_deadline_is_computed_once_and_shared_by_every_stage() -> None:
    """A second stop request cannot move the finish line."""

    deadline = StopDeadline()
    assert deadline.requested is False
    assert deadline.request(5.0) == 5.0
    first = deadline.teardown_remaining()
    assert deadline.request(600.0) == 5.0, "a later request must not extend the stop"
    assert deadline.teardown_remaining() <= first
    assert deadline.budget == 5.0

    deadline.clear()
    assert deadline.requested is False
    assert deadline.budget is None


def test_a_teardown_stage_never_gets_a_zero_or_negative_budget() -> None:
    """``wait_for(0)`` fails before the awaitable is scheduled -- close nothing."""

    deadline = StopDeadline()
    deadline.request(MIN_STOP_BUDGET_SECONDS)
    time.sleep(0.01)
    assert deadline.teardown_remaining() > 0.0
    assert deadline.remaining() >= 0.0


def test_the_budget_is_clamped_to_the_documented_range() -> None:
    """The same 1s..600s range the Settings limit publishes."""

    assert clamp_stop_budget(0.0) == MIN_STOP_BUDGET_SECONDS
    assert clamp_stop_budget(-5.0) == MIN_STOP_BUDGET_SECONDS
    assert clamp_stop_budget(10_000.0) == MAX_STOP_BUDGET_SECONDS
    assert clamp_stop_budget(float("nan")) == MIN_STOP_BUDGET_SECONDS
    assert clamp_stop_budget(cast(float, "not a number")) == MIN_STOP_BUDGET_SECONDS
    assert clamp_stop_budget(20.0) == 20.0


def test_the_watchdog_ends_a_process_that_ignored_every_bound(monkeypatch) -> None:
    """The last line of defence, and the reason a tray stop can be trusted."""

    exits: list[int] = []
    monkeypatch.setattr(stop_deadline_module.os, "_exit", exits.append)

    deadline = StopDeadline()
    deadline.request(MIN_STOP_BUDGET_SECONDS)
    monkeypatch.setattr(stop_deadline_module, "STOP_TEARDOWN_MARGIN_SECONDS", 0.0)
    deadline._hard_deadline = time.monotonic() + 0.05
    deadline.arm_hard_exit()

    finish = time.monotonic() + 5.0
    while not exits and time.monotonic() < finish:
        time.sleep(0.02)
    assert exits == [stop_deadline_module.HARD_EXIT_STATUS]


def test_the_watchdog_stands_down_when_the_ordered_stop_wins(monkeypatch) -> None:
    """A RELOAD, and every stop that finished in time, must survive."""

    exits: list[int] = []
    monkeypatch.setattr(stop_deadline_module.os, "_exit", exits.append)

    deadline = StopDeadline()
    deadline.request(MIN_STOP_BUDGET_SECONDS)
    deadline._hard_deadline = time.monotonic() + 0.05
    deadline.arm_hard_exit()
    deadline.disarm_hard_exit()

    time.sleep(0.3)
    assert exits == []


def test_the_shipped_stop_budget_is_a_restart_an_operator_will_wait_for() -> None:
    """20s, not 300s: this number is now the time you wait for a restart."""

    assert SERVER_GRACEFUL_SHUTDOWN_SECONDS_DEFAULT == 20.0
    assert Settings().server_graceful_shutdown_seconds == 20.0
