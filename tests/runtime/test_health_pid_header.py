"""``x-mcc-pid`` on every answer the ASGI gate gives to ``GET /health`` (7.70.0).

Four answers, four states: starting (503 + ``x-mcc-starting``), ready (200),
busy (200 + ``x-mcc-busy``) and shutting down (503 + ``x-mcc-shutdown``). Each
carries the pid of the process that answered, and none of the bodies change:
the healthy one is still exactly ``{"status":"healthy"}``.
"""

import json
import os
from collections.abc import MutableMapping
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from my_claude_code.core.loop_health import (
    BUSY_MARKER_HEADER,
    BUSY_MARKER_VALUE,
    LoopHealth,
)
from my_claude_code.core.server_pid import SERVER_PID_HEADER, pid_from_headers
from my_claude_code.core.startup_state import (
    STARTING_MARKER_HEADER,
    STARTING_MARKER_VALUE,
    startup_state,
)
from my_claude_code.core.stop_deadline import (
    SHUTDOWN_MARKER_HEADER,
    SHUTDOWN_MARKER_VALUE,
    stop_deadline,
)
from my_claude_code.runtime.application import ApplicationRuntime
from my_claude_code.runtime.asgi import RuntimeASGIApp
from my_claude_code.runtime.loop_heartbeat import reset_health_answer


class _Recorder:
    def __init__(self) -> None:
        self.status: int | None = None
        self.headers: dict[str, str] = {}
        self.raw_headers: list[tuple[bytes, bytes]] = []
        self.body = b""

    async def __call__(self, message: MutableMapping[str, Any]) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.raw_headers = list(message["headers"])
            self.headers = {
                key.decode("ascii").lower(): value.decode("ascii")
                for key, value in message["headers"]
            }
        elif message["type"] == "http.response.body":
            self.body += message["body"]


async def _never_called(scope, receive, send):  # pragma: no cover - a guard
    raise AssertionError("the gate let /health through to the router")


@pytest.fixture(autouse=True)
def _clean_state():
    startup_state().begin()
    stop_deadline().clear()
    reset_health_answer()
    yield
    startup_state().begin()
    stop_deadline().clear()
    reset_health_answer()


async def _get_health() -> _Recorder:
    app = RuntimeASGIApp(
        _never_called, cast(ApplicationRuntime, MagicMock(spec=ApplicationRuntime))
    )
    recorder = _Recorder()

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    await app({"type": "http", "path": "/health", "method": "GET"}, receive, recorder)
    return recorder


def _assert_this_process(recorder: _Recorder) -> None:
    assert recorder.headers[SERVER_PID_HEADER] == str(os.getpid())
    assert pid_from_headers(recorder.headers) == os.getpid()
    # Exactly one, so a reader never has to choose.
    names = [name for name, _ in recorder.raw_headers]
    assert names.count(SERVER_PID_HEADER.encode("ascii")) == 1


@pytest.mark.asyncio
async def test_a_starting_server_names_its_pid() -> None:
    recorder = await _get_health()

    assert recorder.status == 503
    assert recorder.headers[STARTING_MARKER_HEADER] == STARTING_MARKER_VALUE
    _assert_this_process(recorder)


@pytest.mark.asyncio
async def test_a_ready_server_names_its_pid_and_its_body_is_unchanged() -> None:
    startup_state().mark_ready()

    recorder = await _get_health()

    assert recorder.status == 200
    assert recorder.body == b'{"status":"healthy"}'
    assert BUSY_MARKER_HEADER not in recorder.headers
    _assert_this_process(recorder)


@pytest.mark.asyncio
async def test_a_busy_server_names_its_pid(monkeypatch) -> None:
    from my_claude_code.core import loop_health as module

    record = LoopHealth()
    record.configure(interval_seconds=0.1, busy_lag_seconds=0.2)
    monkeypatch.setattr(module, "_LOOP_HEALTH", record)
    startup_state().mark_ready()

    with record.working("a bulk add"):
        record.beat(2.0)
        recorder = await _get_health()

    assert recorder.status == 200
    assert recorder.headers[BUSY_MARKER_HEADER] == BUSY_MARKER_VALUE
    assert json.loads(recorder.body)["status"] == "healthy"
    _assert_this_process(recorder)


@pytest.mark.asyncio
async def test_a_draining_server_names_its_pid() -> None:
    startup_state().mark_ready()
    stop_deadline().request(5.0)

    recorder = await _get_health()

    assert recorder.status == 503
    assert recorder.headers[SHUTDOWN_MARKER_HEADER] == SHUTDOWN_MARKER_VALUE
    assert recorder.headers["connection"] == "close"
    _assert_this_process(recorder)


@pytest.mark.asyncio
async def test_a_stop_during_a_start_still_names_its_pid_once() -> None:
    """Both gates apply; the drain wins, and the pid is still there, once."""

    stop_deadline().request(5.0)

    recorder = await _get_health()

    assert recorder.headers[SHUTDOWN_MARKER_HEADER] == SHUTDOWN_MARKER_VALUE
    _assert_this_process(recorder)


def test_a_missing_or_bad_pid_is_none() -> None:
    assert pid_from_headers({}) is None
    assert pid_from_headers({SERVER_PID_HEADER: "abc"}) is None
    assert pid_from_headers({SERVER_PID_HEADER: "-3"}) is None
    assert pid_from_headers({"X-MCC-PID": " 77 "}) == 77
