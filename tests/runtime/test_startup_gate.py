"""The gate that answers ``starting`` until the application is ready.

The whole point of binding the listener before doing the work is that a program
looking at the port can now tell "coming up" from "nothing here". These tests
pin the three things that makes true: the refusal is distinguishable, the drain
still wins over it, and the stage table is emitted.
"""

import json
from typing import cast
from unittest.mock import MagicMock

import pytest

from my_claude_code.core.startup_state import (
    STARTING_MARKER_HEADER,
    STARTING_MARKER_VALUE,
    StartupState,
    startup_state,
)
from my_claude_code.core.stop_deadline import (
    SHUTDOWN_MARKER_HEADER,
    SHUTDOWN_MARKER_VALUE,
    stop_deadline,
)
from my_claude_code.runtime.application import ApplicationRuntime
from my_claude_code.runtime.asgi import RuntimeASGIApp


class _Recorder:
    """Collects one ASGI response."""

    def __init__(self) -> None:
        self.status: int | None = None
        self.headers: dict[str, str] = {}
        self.body = b""

    async def __call__(self, message: dict) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.headers = {
                key.decode("ascii").lower(): value.decode("ascii")
                for key, value in message["headers"]
            }
        elif message["type"] == "http.response.body":
            self.body += message["body"]


async def _never_called(scope, receive, send):  # pragma: no cover - a guard
    raise AssertionError("the gate let a request through")


def _runtime() -> ApplicationRuntime:
    """A runtime the gate never reaches: no request gets past it in these tests."""

    return cast(ApplicationRuntime, MagicMock(spec=ApplicationRuntime))


@pytest.fixture(autouse=True)
def _clean_state():
    startup_state().begin()
    stop_deadline().clear()
    yield
    startup_state().begin()
    stop_deadline().clear()


async def _get_health(app: RuntimeASGIApp) -> _Recorder:
    recorder = _Recorder()

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    await app({"type": "http", "path": "/health", "method": "GET"}, receive, recorder)
    return recorder


@pytest.mark.asyncio
async def test_every_route_answers_starting_until_the_application_is_ready(
    monkeypatch,
) -> None:
    app = RuntimeASGIApp(_never_called, _runtime())
    state = startup_state()
    state.mark("catalogue")

    recorder = await _get_health(app)

    assert recorder.status == 503
    assert recorder.headers[STARTING_MARKER_HEADER] == STARTING_MARKER_VALUE
    # Distinct from the drain's marker: one means "wait, it is coming", the
    # other "wait, it is going", and a caller that cannot tell them apart
    # cannot decide whether to start a replacement.
    assert SHUTDOWN_MARKER_HEADER not in recorder.headers
    payload = json.loads(recorder.body)
    assert payload["status"] == "starting"
    assert payload["stage"] == "catalogue"
    assert isinstance(payload["elapsed_ms"], int)


@pytest.mark.asyncio
async def test_a_ready_application_is_not_gated() -> None:
    seen: list[str] = []

    async def app_inner(scope, receive, send):
        seen.append(scope["path"])

    app = RuntimeASGIApp(app_inner, _runtime())
    startup_state().mark_ready()

    await _get_health(app)

    assert seen == ["/health"]


@pytest.mark.asyncio
async def test_the_drain_gate_wins_over_the_startup_gate() -> None:
    """A stop requested during a slow start must answer "going", not "coming".

    This is the one ordering that matters between the two gates. A caller told
    "starting" would wait for a server that is on its way out, which is exactly
    the wait the desktop app used to sit in.
    """

    app = RuntimeASGIApp(_never_called, _runtime())
    startup_state().mark("configured-models")
    stop_deadline().request(5.0)

    recorder = await _get_health(app)

    assert recorder.status == 503
    assert recorder.headers[SHUTDOWN_MARKER_HEADER] == SHUTDOWN_MARKER_VALUE
    assert STARTING_MARKER_HEADER not in recorder.headers


def test_every_stage_logs_one_startup_line(caplog) -> None:
    """The stage table exists so a regression is visible in the log a user sends.

    Startup cost roughly tripled between 6.41.2 and 6.58.4 with nobody
    noticing, because nothing in the log measured it.
    """

    lines: list[str] = []
    state = StartupState()

    from loguru import logger

    sink_id = logger.add(lambda message: lines.append(message.record["message"]))
    try:
        state.mark("learned-facts")
        state.mark_ready()
    finally:
        logger.remove(sink_id)

    assert any(line.startswith("STARTUP: learned-facts +") for line in lines)
    assert any(line.startswith("STARTUP: ready +") for line in lines)
    assert lines[-1].endswith("ms")


def test_the_clock_survives_the_first_begin_and_restarts_on_a_reload() -> None:
    """The interpreter and the imports are part of how long a cold start took.

    A table that began after them would hide the largest row in it -- three
    seconds of import on this machine. A reload, which pays for none of that
    again, does start from now.
    """

    state = StartupState()
    first = state.elapsed_ms
    state.begin()
    assert state.elapsed_ms >= first

    state.mark_ready()
    state.begin()
    assert not state.ready
    assert state.elapsed_ms < 1000
