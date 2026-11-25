"""Translate Anthropic SSE streams into OpenAI Responses SSE streams."""

import asyncio
import sys
from collections.abc import AsyncIterable, AsyncIterator, Callable, Mapping
from typing import Any

from my_claude_code.core.diagnostics import safe_exception_message
from my_claude_code.core.failures import ExecutionFailure, find_execution_failure
from my_claude_code.core.openai_common.drain import drain_after_terminal
from my_claude_code.core.trace import close_stream_input

from .anthropic_sse import iter_sse_events
from .models import OpenAIResponsesRequest
from .streaming import ResponsesStreamAssembler

PostStartTerminalFailureObserver = Callable[[BaseException], None]


async def iter_responses_sse_from_anthropic(
    chunks: AsyncIterable[Any],
    request: OpenAIResponsesRequest,
    *,
    on_post_start_terminal_failure: PostStartTerminalFailureObserver | None = None,
) -> AsyncIterator[str]:
    """Yield Responses SSE events translated from an Anthropic SSE stream."""
    assembler = ResponsesStreamAssembler(request)
    emitted_any_chunk = False
    events = iter_sse_events(chunks)
    try:
        async for event in events:
            for chunk in assembler.process_anthropic_event(event):
                yield chunk
                emitted_any_chunk = True
            if assembler.terminal:
                # Read the rest, translate none of it: the executor records
                # the attempt and route health only once its stream is
                # exhausted, and a close here reported it as interrupted.
                await drain_after_terminal(events, owner="openai_responses.stream")
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
        _observe_post_start_terminal_failure(on_post_start_terminal_failure, exc)
        for chunk in assembler.fail_execution(exc):
            yield chunk
    except BaseExceptionGroup as exc:
        if not emitted_any_chunk:
            raise
        failure = find_execution_failure(exc)
        if failure is not None:
            _observe_post_start_terminal_failure(
                on_post_start_terminal_failure, failure
            )
            for chunk in assembler.fail_execution(failure):
                yield chunk
        else:
            _observe_post_start_terminal_failure(on_post_start_terminal_failure, exc)
            for chunk in assembler.fail_response(_unexpected_error_data(exc)):
                yield chunk
    except Exception as exc:
        if not emitted_any_chunk:
            raise
        _observe_post_start_terminal_failure(on_post_start_terminal_failure, exc)
        for chunk in assembler.fail_response(_unexpected_error_data(exc)):
            yield chunk
    finally:
        await close_stream_input(
            events,
            owner="openai_responses.stream",
            source="core",
            preserved_error=sys.exception(),
        )


def _observe_post_start_terminal_failure(
    observer: PostStartTerminalFailureObserver | None,
    exc: BaseException,
) -> None:
    if observer is not None:
        observer(exc)


def _unexpected_error_data(exc: BaseException) -> dict[str, dict[str, str]]:
    return {
        "error": {
            "type": "api_error",
            "message": safe_exception_message(exc),
        }
    }


async def complete_response_from_anthropic_sse(
    chunks: AsyncIterable[Any],
    request: OpenAIResponsesRequest,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Assemble one non-streaming Responses payload from an Anthropic SSE stream."""
    assembler = ResponsesStreamAssembler(request)
    raw_error: dict[str, Any] | None = None
    events = iter_sse_events(chunks)
    try:
        async for event in events:
            if event.event == "error" and not assembler.terminal:
                err_obj = event.data.get("error")
                raw_error = (
                    dict(err_obj)
                    if isinstance(err_obj, Mapping)
                    else {"type": "api_error", "message": str(event.data)}
                )
            assembler.process_anthropic_event(event)
            if assembler.terminal:
                await drain_after_terminal(events, owner="openai_responses.stream")
                break
        if not assembler.terminal:
            assembler.finish_if_needed()
    finally:
        await close_stream_input(
            events,
            owner="openai_responses.stream",
            source="core",
            preserved_error=sys.exception(),
        )
    response_payload = assembler.final_response or assembler.response_payload(
        status="completed"
    )
    return response_payload, raw_error
