"""Request finalisation runs on a worker thread, and records the same row.

``_finalize`` used to run a PIL decode and thumbnail per picture, a full
tiktoken pass over the prompt and the pricing ladder on the event loop, after
the client's last token. None of it can change what the request answered, but
all of it stalled every OTHER request in flight: the in-process heartbeat
caught 371 ms and 1,108 ms gaps inside this one method.

Two things have to stay true: the row is byte-for-byte what it was
(invariant 2), and the work really is off the loop.
"""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import fields
from typing import Any, Literal

import pytest

from my_claude_code.api import request_capture as capture_module
from my_claude_code.api.request_capture import RequestCapture
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.request_log import RequestLogStore, RequestRecord

_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmM"
    "IQAAAABJRU5ErkJggg=="
)


@pytest.fixture
def store(tmp_path) -> Any:
    store = RequestLogStore(tmp_path / "requests.db")
    yield store
    store.close()


def _request() -> MessagesRequest:
    return MessagesRequest.model_validate(
        {
            "model": "claude-sonnet-4-5",
            "max_tokens": 100,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": _PNG,
                            },
                        },
                        {"type": "text", "text": "what is in this picture"},
                    ],
                }
            ],
        }
    )


def _capture(store: RequestLogStore) -> RequestCapture:
    from my_claude_code.core.anthropic import request_image_inputs

    request = _request()
    capture = RequestCapture(
        store,
        request_id="req_offloop",
        endpoint="/v1/messages",
        protocol="anthropic",
        stream=True,
        requested_model="claude-sonnet-4-5",
        input_text="what is in this picture",
        params={"max_tokens": 100},
        images=tuple(request_image_inputs(request)),
        capture_images_pixels=4096,
        request=request,
    )
    # Set directly rather than through ``set_routing``, which wants a whole
    # routed request: what finalisation reads is these two fields.
    capture._record.provider = "nvidia_nim"
    capture._record.resolved_model = "acme-1"
    return capture


def _enqueued(store: RequestLogStore, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    rows: list[Any] = []
    monkeypatch.setattr(store, "enqueue", rows.append)
    return rows


def _comparable(record: RequestRecord) -> dict[str, Any]:
    """Everything on the row except the two clocks that must differ."""

    skip = {"ts_epoch", "duration_ms"}
    return {
        field.name: getattr(record, field.name)
        for field in fields(record)
        if field.name not in skip
    }


@pytest.mark.asyncio
async def test_finalize_record_fields_unchanged(
    store: RequestLogStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The off-loop path enqueues exactly the row the on-loop path did."""

    rows = _enqueued(store, monkeypatch)

    on_loop = _capture(store)
    on_loop._finalize("success")

    off_loop = _capture(store)
    await off_loop._finalize_off_loop("success")

    assert len(rows) == 2
    assert _comparable(rows[0]) == _comparable(rows[1])
    # And the fields the spec names are actually populated, or the comparison
    # above would be comparing two rows of Nones.
    assert rows[1].input_image_count == 1
    assert rows[1].images is not None
    assert rows[1].est_tokens_in is not None
    assert rows[1].est_image_tokens is not None


@pytest.mark.asyncio
async def test_a_slow_finalize_does_not_stall_the_loop(
    store: RequestLogStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The proof: a deliberately slow finalize, and a heartbeat that survives.

    ``capture_images`` is made to take 400 ms of blocking CPU -- the shape of
    a real PIL pass over several screenshots. On the event loop that is a
    400 ms freeze for every other request in the process. On a worker thread
    it is not.
    """

    _enqueued(store, monkeypatch)

    def slow_capture_images(*args: Any, **kwargs: Any) -> tuple[Any, ...]:
        time.sleep(0.4)
        return ()

    monkeypatch.setattr(capture_module, "capture_images", slow_capture_images)

    gaps: list[float] = []
    stop = asyncio.Event()

    async def heartbeat() -> None:
        last = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            gaps.append((now - last - 0.01) * 1000.0)
            last = now

    beat = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.05)
    await _capture(store)._finalize_off_loop("success")
    stop.set()
    await beat

    assert max(gaps) < 150.0, f"loop held for {max(gaps):.0f}ms"


@pytest.mark.asyncio
async def test_the_streaming_path_uses_the_off_loop_form(
    store: RequestLogStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not just available -- actually what ``_observe`` calls."""

    rows = _enqueued(store, monkeypatch)
    seen: list[str] = []
    real = RequestCapture._finalize_off_loop

    async def counting(
        self: RequestCapture, status: Literal["success", "error", "cancelled"]
    ) -> None:
        seen.append(status)
        await real(self, status)

    monkeypatch.setattr(RequestCapture, "_finalize_off_loop", counting)

    async def body() -> AsyncIterator[str]:
        for event, data in (
            (
                "message_start",
                {"type": "message_start", "message": {"usage": {"input_tokens": 7}}},
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "hi"},
                },
            ),
            ("message_stop", {"type": "message_stop"}),
        ):
            yield f"event: {event}\ndata: {json.dumps(data)}\n\n"

    capture = _capture(store)
    async for _chunk in capture.wrap(body()):
        pass

    assert seen == ["success"]
    assert len(rows) == 1
    assert rows[0].status == "success"


@pytest.mark.asyncio
async def test_an_image_that_cannot_be_decoded_is_not_a_failed_request(
    store: RequestLogStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant 2: arithmetic about a request never fails the request."""

    rows = _enqueued(store, monkeypatch)

    def exploding(*args: Any, **kwargs: Any) -> tuple[Any, ...]:
        raise ValueError("not an image")

    monkeypatch.setattr(capture_module, "capture_images", exploding)

    await _capture(store)._finalize_off_loop("success")

    assert len(rows) == 1
    assert rows[0].status == "success"
    assert not rows[0].images
