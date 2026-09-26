"""A client that hangs up the moment it reads the terminal event keeps its row.

Codex CLI closes its connection as soon as it reads ``response.completed``.
Under uvicorn (ASGI spec 2.3) Starlette's ``StreamingResponse`` answers that
disconnect by cancelling the response task group -- and on every one of the
four inbound surfaces that cancellation landed while the request capture was
awaiting its offloaded finalize arithmetic. The capture had already latched
itself as finalized, so the answered request left no row, no rollup, no
totals and no attempts (6.62.0 to 7.56.0; every Codex success in that span).

``TestClient`` cannot hang up mid-stream, so this is a real uvicorn listener on
an ephemeral port, on its own thread, with a stub provider behind the real
handlers, and a raw httpx client that stops reading at the terminal event and
closes. The finalize arithmetic is held in its worker thread until the
server's request task has *provably* been cancelled inside that await, so the
window this file is about is always the one exercised -- not a race that a
fast machine happens to win.
"""

import asyncio
import socket
import threading
import time
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
import uvicorn

from my_claude_code.api.request_capture import RequestCapture
from my_claude_code.core.anthropic.streaming import format_sse_event
from my_claude_code.core.request_log import get_request_log_store
from tests.api.support import create_test_app

MODEL = "nvidia_nim/test-model"

SURFACES: dict[str, tuple[str, dict[str, Any], str]] = {
    "messages": (
        "/v1/messages",
        {
            "model": MODEL,
            "max_tokens": 32,
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        },
        "message_stop",
    ),
    "responses": (
        "/v1/responses",
        {"model": MODEL, "stream": True, "input": "hi"},
        "response.completed",
    ),
    "chat": (
        "/v1/chat/completions",
        {
            "model": MODEL,
            "max_tokens": 32,
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        },
        "[DONE]",
    ),
    "gemini": (
        f"/v1beta/models/{MODEL}:streamGenerateContent?alt=sse",
        {
            "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
            "generationConfig": {"maxOutputTokens": 32},
        },
        "finishReason",
    ),
}


def _answer() -> list[str]:
    return [
        format_sse_event(
            "message_start",
            {
                "type": "message_start",
                "message": {"usage": {"input_tokens": 3, "output_tokens": 0}},
            },
        ),
        format_sse_event(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        format_sse_event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "OK"},
            },
        ),
        format_sse_event(
            "content_block_stop", {"type": "content_block_stop", "index": 0}
        ),
        format_sse_event(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 1},
            },
        ),
        format_sse_event("message_stop", {"type": "message_stop"}),
    ]


class _StubProvider:
    """Answers "OK" through the real executor, handlers and adapters."""

    def __init__(self) -> None:
        self.preflight_stream = MagicMock()

    @property
    def credential_label(self) -> str | None:
        return None

    async def stream_response(self, request_data, **_kwargs):
        for chunk in _answer():
            yield chunk


class _HeldFinalize:
    """Hold the offloaded arithmetic until the request task is cancelled in it.

    ``_finalize_off_loop`` is wrapped to count a ``CancelledError`` raised out
    of its await (the client's hang-up, delivered by Starlette), and the worker
    thread waits for that count before it computes anything. Both fixed and
    unfixed code raise out of the same await, so the wait means the same thing
    either way.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.cancelled = threading.Event()
        self.commits = 0
        self._lock = threading.Lock()
        offload = RequestCapture._finalize_off_loop
        compute = RequestCapture._compute_finalize_fields
        commit = RequestCapture._commit_finalize
        held = self

        async def counted_offload(capture: RequestCapture, status: Any) -> None:
            try:
                await offload(capture, status)
            except asyncio.CancelledError:
                held.cancelled.set()
                raise

        def gated(capture: RequestCapture, record: Any) -> None:
            held.cancelled.wait(10)
            compute(capture, record)

        def counted_commit(capture: RequestCapture, record: Any) -> None:
            with held._lock:
                held.commits += 1
            commit(capture, record)

        monkeypatch.setattr(RequestCapture, "_finalize_off_loop", counted_offload)
        monkeypatch.setattr(RequestCapture, "_compute_finalize_fields", gated)
        monkeypatch.setattr(RequestCapture, "_commit_finalize", counted_commit)


class _Live:
    """One uvicorn server on an ephemeral port, on its own thread."""

    def __init__(self) -> None:
        self._socket = socket.socket()
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(16)
        self.port: int = self._socket.getsockname()[1]
        self._server = uvicorn.Server(
            uvicorn.Config(create_test_app(), log_level="warning", lifespan="on")
        )
        self._thread = threading.Thread(
            target=lambda: self._server.run(sockets=[self._socket]),
            name="mcc-hangup-test-server",
            daemon=True,
        )

    def __enter__(self) -> _Live:
        self._thread.start()
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if self._server.started:
                return self
            time.sleep(0.01)
        raise AssertionError("the test server never started")

    def __exit__(self, *_exc: object) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=30)


def _hang_up_after_terminal(port: int, path: str, body: dict, terminal: str) -> int:
    """Stream the answer, stop at the terminal event, close the connection."""
    headers = {"anthropic-version": "2023-06-01", "x-mcc-harness": "probe"}
    with (
        httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30) as client,
        client.stream("POST", path, json=body, headers=headers) as response,
    ):
        status = response.status_code
        for line in response.iter_lines():
            if terminal in line:
                break
    return status


@pytest.mark.parametrize("surface", sorted(SURFACES))
def test_a_client_that_hangs_up_on_the_terminal_event_keeps_its_row(
    surface: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, body, terminal = SURFACES[surface]
    held = _HeldFinalize(monkeypatch)
    target = (
        "my_claude_code.api.gemini_routes.resolve_provider"
        if surface == "gemini"
        else "my_claude_code.api.routes.resolve_provider"
    )
    with patch(target, return_value=_StubProvider()), _Live() as live:
        status = _hang_up_after_terminal(live.port, path, body, terminal)
        assert status == 200
        assert held.cancelled.wait(10), (
            "the hang-up never reached the finalize await; the test did not "
            "exercise the window it exists for"
        )
        deadline = time.monotonic() + 10
        while held.commits == 0 and time.monotonic() < deadline:
            time.sleep(0.01)

    store = get_request_log_store()
    assert store is not None
    store.close()
    rows, total = store.list_requests()
    assert total == 1, f"{surface}: the answered request left no row"
    row = rows[0]
    assert row["status"] == "success"
    assert row["endpoint"] == path.split("?")[0]
    assert row["tokens_out"] == 1
