"""The three non-Messages adapters read their input to the end (7.56.2).

Each adapter used to ``return`` the moment it translated the terminal event.
That closed the executor while it was suspended after its last frame, so the
bookkeeping it runs once its provider stream is exhausted -- the attempt's
``succeeded`` outcome and ``route_health.record_success`` -- never ran: the
request row was stored with no attempt, and the model never got the success
that clears its failure history. ``/v1/messages`` drains its stream and always
recorded both.

What arrives after the terminal event is read and discarded, never
translated, and a failure raised there is traced, never reported: the client
already has a complete answer.
"""

import json
from collections.abc import AsyncIterator, Callable

import pytest

from my_claude_code.core.failures import ExecutionFailure, FailureKind
from my_claude_code.core.gemini_api.stream import iter_gemini_sse_from_anthropic
from my_claude_code.core.openai_chat_completions.models import (
    OpenAIChatCompletionRequest,
)
from my_claude_code.core.openai_chat_completions.stream import (
    iter_chat_sse_from_anthropic,
)
from my_claude_code.core.openai_responses.models import OpenAIResponsesRequest
from my_claude_code.core.openai_responses.stream import (
    iter_responses_sse_from_anthropic,
)


def _frame(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


ANSWER = [
    _frame(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "m",
                "content": [],
                "stop_reason": None,
                "usage": {"input_tokens": 3, "output_tokens": 0},
            },
        },
    ),
    _frame(
        "content_block_start",
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
    ),
    _frame(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "OK"},
        },
    ),
    _frame("content_block_stop", {"type": "content_block_stop", "index": 0}),
    _frame(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 1},
        },
    ),
    _frame("message_stop", {"type": "message_stop"}),
]

#: A frame that would put visible text on the client's screen if translated.
STRAY = _frame(
    "content_block_delta",
    {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "text_delta", "text": "LEAKED-AFTER-END"},
    },
)


class _Source:
    """An executor stand-in: its success bookkeeping runs after its last frame."""

    def __init__(self, tail: list[str] | None = None, *, fail: bool = False) -> None:
        self.tail = tail or []
        self.fail = fail
        self.succeeded = False
        self.closed_early = False
        self.tail_read = 0

    async def body(self) -> AsyncIterator[str]:
        try:
            for chunk in ANSWER:
                yield chunk
            for chunk in self.tail:
                self.tail_read += 1
                yield chunk
            if self.fail:
                raise ExecutionFailure(
                    kind=FailureKind.UPSTREAM,
                    status_code=502,
                    message="peer closed connection after the answer",
                    retryable=False,
                )
            self.succeeded = True
        except GeneratorExit:
            self.closed_early = True
            raise


def _responses(src: _Source, observed: list[BaseException]) -> AsyncIterator[str]:
    return iter_responses_sse_from_anthropic(
        src.body(),
        OpenAIResponsesRequest(model="m", input="hi"),
        on_post_start_terminal_failure=observed.append,
    )


def _chat(src: _Source, observed: list[BaseException]) -> AsyncIterator[str]:
    request = OpenAIChatCompletionRequest.model_validate(
        {"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": True}
    )
    return iter_chat_sse_from_anthropic(
        src.body(),
        request,
        completion_id="chatcmpl-1",
        on_post_start_terminal_failure=observed.append,
    )


def _gemini(src: _Source, observed: list[BaseException]) -> AsyncIterator[str]:
    return iter_gemini_sse_from_anthropic(
        src.body(),
        model="m",
        response_id="r1",
        include_thoughts=False,
        on_post_start_terminal_failure=observed.append,
    )


ADAPTERS: dict[str, Callable[[_Source, list[BaseException]], AsyncIterator[str]]] = {
    "responses": _responses,
    "chat": _chat,
    "gemini": _gemini,
}


async def _drive(stream: AsyncIterator[str]) -> list[str]:
    return [chunk async for chunk in stream]


@pytest.mark.parametrize("surface", sorted(ADAPTERS))
@pytest.mark.asyncio
async def test_the_adapter_reads_its_input_to_the_end(surface: str) -> None:
    src = _Source()
    observed: list[BaseException] = []
    out = await _drive(ADAPTERS[surface](src, observed))
    assert out
    assert src.succeeded, "the executor never reached its success bookkeeping"
    assert not src.closed_early
    assert observed == []


@pytest.mark.parametrize("surface", sorted(ADAPTERS))
@pytest.mark.asyncio
async def test_frames_after_the_terminal_event_are_read_but_never_translated(
    surface: str,
) -> None:
    baseline = await _drive(ADAPTERS[surface](_Source(), []))
    src = _Source(tail=[STRAY, STRAY])
    out = await _drive(ADAPTERS[surface](src, []))
    assert src.tail_read == 2
    assert src.succeeded
    assert not any("LEAKED-AFTER-END" in chunk for chunk in out)
    # Nothing at all is added: the client sees exactly what it saw before.
    assert len(out) == len(baseline)


@pytest.mark.parametrize("surface", sorted(ADAPTERS))
@pytest.mark.asyncio
async def test_a_failure_after_the_terminal_event_is_never_reported(
    surface: str,
) -> None:
    """No error frame after a complete answer, and no post-start observer call."""
    baseline = await _drive(ADAPTERS[surface](_Source(), []))
    src = _Source(tail=[STRAY], fail=True)
    observed: list[BaseException] = []
    out = await _drive(ADAPTERS[surface](src, observed))
    assert observed == []
    assert not any("LEAKED-AFTER-END" in chunk for chunk in out)
    # The same frames as a clean run, one for one: no error frame was added.
    assert [chunk.split("\n", 1)[0] for chunk in out] == [
        chunk.split("\n", 1)[0] for chunk in baseline
    ]
