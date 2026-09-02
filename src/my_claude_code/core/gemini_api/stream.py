"""Translate Anthropic SSE streams into Gemini ``alt=sse`` streams."""

import asyncio
import sys
from collections.abc import AsyncIterable, AsyncIterator, Callable
from typing import Any

from my_claude_code.core.diagnostics import safe_exception_message
from my_claude_code.core.failures import ExecutionFailure, find_execution_failure
from my_claude_code.core.openai_common import iter_sse_events
from my_claude_code.core.trace import close_stream_input

from .assembler import GeminiStreamAssembler

PostStartTerminalFailureObserver = Callable[[BaseException], None]


async def iter_gemini_sse_from_anthropic(
    chunks: AsyncIterable[Any],
    *,
    model: str,
    response_id: str,
    include_thoughts: bool,
    on_post_start_terminal_failure: PostStartTerminalFailureObserver | None = None,
) -> AsyncIterator[str]:
    """Yield Gemini SSE frames translated from an Anthropic stream.

    A failure that arrives *before* the first frame is re-raised so the HTTP
    boundary can still answer with a real status code; once a frame has gone
    out the status line is spent, and the only honest report left is an error
    frame inside the stream.
    """

    assembler = GeminiStreamAssembler(model, include_thoughts=include_thoughts)
    assembler.bind_response_id(response_id)
    emitted_any_chunk = False
    events = iter_sse_events(chunks)
    try:
        async for event in events:
            for chunk in assembler.process_anthropic_event(event):
                yield chunk
                emitted_any_chunk = True
            if assembler.terminal:
                return
        for chunk in assembler.finish_if_needed():
            yield chunk
            emitted_any_chunk = True
    except GeneratorExit:
        raise
    except asyncio.CancelledError:
        raise
    except ExecutionFailure as exc:
        if not emitted_any_chunk:
            raise
        _observe(on_post_start_terminal_failure, exc)
        for chunk in assembler.fail_execution(exc):
            yield chunk
    except BaseExceptionGroup as exc:
        if not emitted_any_chunk:
            raise
        failure = find_execution_failure(exc)
        if failure is not None:
            _observe(on_post_start_terminal_failure, failure)
            for chunk in assembler.fail_execution(failure):
                yield chunk
        else:
            _observe(on_post_start_terminal_failure, exc)
            for chunk in assembler.fail(_unexpected_error(exc)):
                yield chunk
    except Exception as exc:
        if not emitted_any_chunk:
            raise
        _observe(on_post_start_terminal_failure, exc)
        for chunk in assembler.fail(_unexpected_error(exc)):
            yield chunk
    finally:
        await close_stream_input(
            events,
            owner="gemini_api.stream",
            source="core",
            preserved_error=sys.exception(),
        )


def _observe(
    observer: PostStartTerminalFailureObserver | None, exc: BaseException
) -> None:
    if observer is not None:
        observer(exc)


def _unexpected_error(exc: BaseException) -> dict[str, Any]:
    return {
        "code": 500,
        "message": safe_exception_message(exc),
        "status": "INTERNAL",
    }
