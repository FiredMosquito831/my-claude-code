"""FastAPI streaming response wrappers for public API wire formats."""

import asyncio
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Mapping,
)
from dataclasses import dataclass
from typing import Any, Literal

from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
from starlette.responses import ContentStream
from starlette.types import Receive, Scope, Send

from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic import anthropic_error_type_for_failure
from my_claude_code.core.anthropic.streaming import (
    ANTHROPIC_SSE_RESPONSE_HEADERS,
    anthropic_terminal_error_frame,
    anthropic_terminal_failure_frame,
    format_sse_event,
)
from my_claude_code.core.async_iterators import try_close_async_iterator
from my_claude_code.core.diagnostics import safe_exception_message
from my_claude_code.core.failures import find_execution_failure
from my_claude_code.core.request_tasks import note_serving_task, note_stream_chunk
from my_claude_code.core.stop_deadline import stop_deadline
from my_claude_code.core.trace import close_stream_input, trace_event

TERMINAL_EXECUTION_ERROR_HEADERS = {"x-should-retry": "false"}

PreStartErrorResponse = Callable[[BaseException], Response]
TerminalFrameEmitter = Callable[[BaseException], str]
TerminalFailureObserver = Callable[[BaseException], None]
ReleaseResponseResource = Callable[[], Awaitable[None]]
WireApi = Literal["messages", "responses", "chat_completions", "gemini"]


class EmptyStreamError(RuntimeError):
    """Raised when a public stream ends before emitting any protocol chunk."""


#: The keepalive each wire dialect defines as meaningless. Anthropic's own
#: ``ping`` event, byte for byte what api.anthropic.com sends, on
#: ``/v1/messages``; an SSE comment line everywhere else, because the OpenAI
#: and Gemini adapters drop any Anthropic event they do not translate and a
#: comment is the one frame every compliant SSE parser is required to ignore.
ANTHROPIC_KEEPALIVE_FRAME = format_sse_event("ping", {"type": "ping"})
SSE_COMMENT_KEEPALIVE_FRAME = ": keepalive\n\n"


@dataclass(frozen=True, slots=True)
class StreamKeepalive:
    """When a silent streaming response gets a keepalive frame, and for how long.

    ``idle_seconds`` of silence before the first one, then one every
    ``interval_seconds`` while the silence lasts, and none once one stretch of
    silence has lasted ``max_seconds`` (``0`` = no cap). A real frame from the
    body resets the clock. Silence is measured from the moment the response is
    requested until the first frame, and from the last real frame after that --
    so a model that answers and then goes quiet is covered the same way as one
    that has not answered yet.
    """

    idle_seconds: float
    interval_seconds: float
    max_seconds: float

    @classmethod
    def from_settings(cls, settings: Settings) -> StreamKeepalive | None:
        """The configured policy, or ``None`` when keepalives are off."""

        if settings.stream_keepalive_idle_seconds <= 0:
            return None
        return cls(
            idle_seconds=settings.stream_keepalive_idle_seconds,
            interval_seconds=max(settings.stream_keepalive_interval_seconds, 1.0),
            max_seconds=max(settings.stream_keepalive_max_seconds, 0.0),
        )


class ManagedStreamingResponse(StreamingResponse):
    """Own body closure and one response-scoped runtime release callback."""

    def __init__(
        self,
        content: ContentStream,
        status_code: int = 200,
        headers: Mapping[str, str] | None = None,
        media_type: str | None = None,
        background: BackgroundTask | None = None,
    ) -> None:
        super().__init__(
            content,
            status_code=status_code,
            headers=headers,
            media_type=media_type,
            background=background,
        )
        self._release: ReleaseResponseResource | None = None
        self._cleanup_task: asyncio.Task[None] | None = None

    def bind_release(self, release: ReleaseResponseResource) -> None:
        """Bind the resource retained for this response before ASGI execution."""
        if self._release is not None:
            raise RuntimeError("A response resource release is already bound.")
        if self._cleanup_task is not None:
            raise RuntimeError("Cannot bind a resource after response cleanup started.")
        self._release = release

    async def aclose(self) -> None:
        """Close the body and release its runtime resource exactly once."""
        await self._close(preserved_error=None)

    async def _close(self, *, preserved_error: BaseException | None) -> None:
        task = self._cleanup_task
        if task is None:
            task = asyncio.create_task(
                self._cleanup(preserved_error=preserved_error),
                name="mcc-api-response-cleanup",
            )
            self._cleanup_task = task
        await _wait_for_cleanup(task)

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        preserved_error: BaseException | None = None
        try:
            await super().__call__(scope, receive, send)
        except BaseException as exc:
            preserved_error = exc
            raise
        finally:
            await self._close(preserved_error=preserved_error)

    async def _cleanup(self, *, preserved_error: BaseException | None) -> None:
        close_body = close_stream_input(
            self.body_iterator,
            owner="ManagedStreamingResponse",
            source="api",
            preserved_error=preserved_error,
        )
        budget = _cleanup_wait_budget()
        try:
            if budget is None:
                await close_body
            else:
                # Closing the body means closing the upstream response, and an
                # upstream that is open but silent -- with no read deadline in
                # force, which is the shipped default -- can hold that close
                # open indefinitely. Bounding it HERE rather than only around
                # the whole cleanup is what keeps the release below reachable:
                # the lease this response holds is the thing the provider drain
                # is waiting for, so abandoning the close without releasing it
                # would trade one unbounded wait for another.
                await asyncio.wait_for(close_body, budget)
        except TimeoutError:
            _trace_response_cleanup_failure("close_body", TimeoutError())
        except Exception as exc:
            _trace_response_cleanup_failure("close_body", exc)

        release = self._release
        if release is None:
            return
        try:
            await release()
        except Exception as exc:
            _trace_response_cleanup_failure("release_resource", exc)


async def _wait_for_cleanup(task: asyncio.Task[None]) -> None:
    """Wait through repeated caller cancellation, then restore cancellation.

    The shield and the re-awaiting loop are deliberate: a caller cancelled
    mid-cleanup must not leave the body iterator half-closed, so the cleanup is
    always allowed to finish. What was missing is a way out. With every
    per-request deadline at 0 (the 6.16.0 decision, and the shipped default) a
    cleanup blocked on a silent upstream never finished, so uvicorn's
    force-cancel at the graceful-shutdown bound was absorbed here and the lease
    this response holds was never released -- which is what made the provider
    drain, and therefore the whole process, hang forever.

    A stop that has been requested therefore bounds this wait against the shared
    stop deadline. Past it the cleanup task is cancelled and abandoned, the
    caller's cancellation is restored, and the stop proceeds. Nothing changes
    for a request cancelled while the server is running normally.
    """
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        budget = _cleanup_wait_budget()
        try:
            if budget is None:
                await asyncio.shield(task)
            else:
                await asyncio.wait_for(asyncio.shield(task), budget)
        except TimeoutError:
            _abandon_cleanup(task)
            break
        except asyncio.CancelledError as exc:
            cancellation = exc

    if not task.done():
        # Abandoned at the stop deadline. There is no result to read; restore
        # the caller's cancellation so uvicorn's force-cancel does what it was
        # for, and let the supervisor's own bound finish the stop.
        if cancellation is not None:
            raise cancellation
        return

    # Ordinary defensive failures are trace-only; cancellation remains control flow.
    try:
        task.result()
    except asyncio.CancelledError:
        if cancellation is not None:
            raise cancellation from None
        raise
    except Exception as exc:
        _trace_response_cleanup_failure("cleanup_task", exc)

    if cancellation is not None:
        raise cancellation


def _cleanup_wait_budget() -> float | None:
    """Seconds this cleanup may still take, or ``None`` while not stopping."""

    deadline = stop_deadline()
    if not deadline.requested:
        return None
    return deadline.teardown_remaining()


def _abandon_cleanup(task: asyncio.Task[None]) -> None:
    """Cancel a cleanup that outlived the stop deadline and stop waiting on it."""

    task.cancel()
    # The task outlives this await, so nothing would otherwise retrieve its
    # outcome and asyncio would log "exception was never retrieved" during an
    # already-noisy shutdown.
    task.add_done_callback(_consume_cleanup_outcome)
    trace_event(
        stage="egress",
        event="my_claude_code.api.response.cleanup_abandoned",
        source="api",
        operation="close_body",
    )


def _consume_cleanup_outcome(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    task.exception()


def _trace_response_cleanup_failure(operation: str, exc: BaseException) -> None:
    trace_event(
        stage="egress",
        event="my_claude_code.api.response.cleanup_failed",
        source="api",
        operation=operation,
        exc_type=type(exc).__name__,
    )


async def bind_response_lifetime(
    response: object,
    release: ReleaseResponseResource,
) -> object:
    """Retain a runtime resource until a response body is fully consumed."""
    if isinstance(response, ManagedStreamingResponse):
        response.bind_release(release)
        return response
    if isinstance(response, StreamingResponse):
        error = TypeError("Streaming API responses must use ManagedStreamingResponse.")
        try:
            await close_stream_input(
                response.body_iterator,
                owner="bind_response_lifetime",
                source="api",
                preserved_error=error,
            )
        finally:
            await release()
        raise error
    await release()
    return response


def terminal_execution_error_response(
    *, status_code: int, content: dict[str, Any]
) -> JSONResponse:
    """Return a final provider-execution error without enabling client retries."""
    return JSONResponse(
        status_code=status_code,
        content=content,
        headers=dict(TERMINAL_EXECUTION_ERROR_HEADERS),
    )


def trace_terminal_execution_error(
    *,
    wire_api: WireApi,
    request_id: str,
    status_code: int,
    error_type: str,
    error: BaseException | None = None,
) -> None:
    """Record one correlated terminal-execution decision at the HTTP boundary."""
    fields: dict[str, object] = {
        "stage": "egress",
        "event": "my_claude_code.api.response.terminal_execution_error",
        "source": "api",
        "wire_api": wire_api,
        "request_id": request_id,
        "status_code": status_code,
        "error_type": error_type,
        "client_should_retry": False,
    }
    failure = find_execution_failure(error) if error is not None else None
    if error is not None:
        fields["exc_type"] = type(failure or error).__name__
    if failure is not None:
        fields["failure_kind"] = failure.kind.value
        fields["provider_retryable"] = failure.retryable
    trace_event(**fields)


async def _first_chunk_streaming_response(
    body: AsyncIterator[str],
    *,
    headers: Mapping[str, str],
    pre_start_error_response: PreStartErrorResponse,
    terminal_frame: TerminalFrameEmitter | None,
    terminal_failure_observer: TerminalFailureObserver | None,
    keepalive: StreamKeepalive | None = None,
    keepalive_frame: str = SSE_COMMENT_KEEPALIVE_FRAME,
) -> Response:
    if keepalive is not None:
        return await _keepalive_streaming_response(
            body,
            headers=headers,
            pre_start_error_response=pre_start_error_response,
            terminal_frame=terminal_frame,
            terminal_failure_observer=terminal_failure_observer,
            keepalive=keepalive,
            keepalive_frame=keepalive_frame,
        )
    try:
        first_chunk = await anext(body)
    except StopAsyncIteration:
        error = EmptyStreamError("Stream ended before emitting a response.")
        await _close_pre_start_body(body, preserved_error=error)
        return pre_start_error_response(error)
    except GeneratorExit as exc:
        await _close_pre_start_body(body, preserved_error=exc)
        raise
    except asyncio.CancelledError as exc:
        await _close_pre_start_body(body, preserved_error=exc)
        raise
    except BaseExceptionGroup as exc:
        await _close_pre_start_body(body, preserved_error=exc)
        return pre_start_error_response(exc)
    except Exception as exc:
        await _close_pre_start_body(body, preserved_error=exc)
        return pre_start_error_response(exc)

    return ManagedStreamingResponse(
        _PrefetchedStream(
            first_chunk,
            body,
            terminal_frame=terminal_frame,
            terminal_failure_observer=terminal_failure_observer,
        ),
        media_type="text/event-stream",
        headers=dict(headers),
    )


async def _keepalive_streaming_response(
    body: AsyncIterator[str],
    *,
    headers: Mapping[str, str],
    pre_start_error_response: PreStartErrorResponse,
    terminal_frame: TerminalFrameEmitter | None,
    terminal_failure_observer: TerminalFailureObserver | None,
    keepalive: StreamKeepalive,
    keepalive_frame: str,
) -> Response:
    """The same prefetch, raced against the keepalive clock.

    A first frame inside ``idle_seconds`` builds exactly the response the
    function above would have built -- the held-back status line, the pre-start
    HTTP error path, the same bytes -- and only the tail is watched for
    mid-stream silence. Past ``idle_seconds`` the status line is committed with
    a keepalive as its first frame, and a failure from then on can only be
    reported inside the stream: Anthropic's own ``error`` event on
    ``/v1/messages``, and on the other surfaces the exact JSON error body the
    pre-start path would have answered with, as one ``data:`` frame -- which is
    what the OpenAI SDKs raise ``APIError`` from and what Google's clients read
    when a transport hid the status.
    """

    source = _KeepaliveSource(body, keepalive, keepalive_frame)
    try:
        first_chunk = await source.next()
    except StopAsyncIteration:
        error = EmptyStreamError("Stream ended before emitting a response.")
        await _close_pre_start_body(source, preserved_error=error)
        return pre_start_error_response(error)
    except GeneratorExit as exc:
        await _close_pre_start_body(source, preserved_error=exc)
        raise
    except asyncio.CancelledError as exc:
        await _close_pre_start_body(source, preserved_error=exc)
        raise
    except BaseExceptionGroup as exc:
        await _close_pre_start_body(source, preserved_error=exc)
        return pre_start_error_response(exc)
    except Exception as exc:
        await _close_pre_start_body(source, preserved_error=exc)
        return pre_start_error_response(exc)

    committed_by_keepalive = first_chunk is None
    if committed_by_keepalive:
        trace_event(
            stage="egress",
            event="my_claude_code.api.response.committed_by_keepalive",
            source="api",
            idle_seconds=keepalive.idle_seconds,
        )
    return ManagedStreamingResponse(
        _PrefetchedStream(
            keepalive_frame if first_chunk is None else first_chunk,
            body,
            terminal_frame=terminal_frame,
            terminal_failure_observer=terminal_failure_observer,
            source=source,
            first_is_keepalive=committed_by_keepalive,
            late_failure_frame=(
                None
                if terminal_frame is not None
                else _error_body_frame_emitter(pre_start_error_response)
            ),
        ),
        media_type="text/event-stream",
        headers=dict(headers),
    )


def _error_body_frame_emitter(
    pre_start_error_response: PreStartErrorResponse,
) -> TerminalFrameEmitter:
    """Carry the pre-start error body inside an already-committed stream."""

    def emit(exc: BaseException) -> str:
        response = pre_start_error_response(exc)
        text = bytes(response.body).decode("utf-8", errors="replace")
        lines = "".join(f"data: {line}\n" for line in text.splitlines() or [""])
        return f"{lines}\n"

    return emit


class _KeepaliveSource:
    """Hand out the body's frames one at a time, or ``None`` when a keepalive is due.

    The body is iterated by one pump task per response, and only on demand: the
    pump asks for the next frame when the reader asks for one, never ahead of
    it, so backpressure and ordering are what they are without keepalives. The
    reader waits on that frame with a timeout; the timeout, not the pump, is
    what produces a keepalive, so a keepalive can never reorder or duplicate a
    frame. The pump runs in the *caller's own* context object rather than a
    copy, so a context variable the body sets is seen by the code around it
    exactly as when the body was awaited inline, and it announces itself to the
    stuck-request registry once, because it -- not the reader -- is now the
    task parked on a silent upstream.
    """

    def __init__(
        self,
        body: AsyncIterator[str],
        policy: StreamKeepalive,
        frame: str,
    ) -> None:
        loop = asyncio.get_running_loop()
        now = loop.time()
        self._body = body
        self._policy = policy
        self.frame = frame
        self._silent_since = now
        self._next_due: float | None = now + policy.idle_seconds
        self._demand = asyncio.Event()
        self._pending: asyncio.Future[str] | None = None
        self._finished = False
        current = asyncio.current_task()
        self._task: asyncio.Task[None] = asyncio.create_task(
            self._pump(),
            name="mcc-api-stream-pump",
            context=current.get_context() if current is not None else None,
        )
        self._task.add_done_callback(self._pump_done)

    async def next(self) -> str | None:
        """The body's next frame, or ``None`` when a keepalive is due first."""

        loop = asyncio.get_running_loop()
        pending = self._pending
        if pending is None:
            if self._finished:
                raise StopAsyncIteration
            pending = loop.create_future()
            self._pending = pending
            self._demand.set()
        while True:
            due = self._next_due
            timeout = None if due is None else max(0.0, due - loop.time())
            done, _ = await asyncio.wait((pending,), timeout=timeout)
            if done:
                self._pending = None
                chunk = pending.result()
                now = loop.time()
                self._silent_since = now
                self._next_due = now + self._policy.idle_seconds
                return chunk
            now = loop.time()
            if due is None or now < due:
                continue
            cap = self._policy.max_seconds
            if cap > 0 and now - self._silent_since > cap:
                # Past the cap nothing more is written for this stretch of
                # silence; the client's own idle timer takes over exactly as if
                # keepalives were off. A real frame re-arms the clock.
                self._next_due = None
                trace_event(
                    stage="egress",
                    event="my_claude_code.api.response.keepalive_capped",
                    source="api",
                    max_seconds=cap,
                )
                continue
            self._next_due = now + self._policy.interval_seconds
            return None

    async def aclose(self) -> None:
        """Cancel the pump, wait for it, then close the body exactly once.

        An async generator cannot be closed while its ``__anext__`` is still
        running, so the pump is cancelled and *awaited* first -- through
        ``asyncio.wait``, so the caller's own cancellation still interrupts the
        wait, and bounded by the shared stop deadline, so a body that will not
        unwind cannot hold the shutdown drain.
        """

        settled = True
        if not self._task.done():
            self._task.cancel()
            settled = await _await_cancelled_task(self._task)
        self._discard_pending()
        if not settled:
            # The pump still owns the body's in-flight ``__anext__``; its own
            # cancellation unwinds the generator. Closing it here would raise.
            trace_event(
                stage="egress",
                event="my_claude_code.api.response.stream_pump_abandoned",
                source="api",
            )
            return
        close_error = await try_close_async_iterator(self._body)
        if close_error is not None:
            raise close_error

    async def _pump(self) -> None:
        note_serving_task()
        while True:
            await self._demand.wait()
            self._demand.clear()
            pending = self._pending
            try:
                chunk = await anext(self._body)
            except StopAsyncIteration:
                self._finished = True
                _settle(pending, error=StopAsyncIteration())
                return
            except asyncio.CancelledError:
                raise
            except (Exception, BaseExceptionGroup) as exc:
                self._finished = True
                _settle(pending, error=exc)
                return
            if pending is not None and not pending.done():
                pending.set_result(chunk)

    def _pump_done(self, task: asyncio.Task[None]) -> None:
        # The safety net: however the pump ended, a reader still waiting on it
        # is released rather than left parked on a future nobody will settle.
        self._finished = True
        pending = self._pending
        if pending is None or pending.done():
            return
        if task.cancelled():
            pending.cancel()
            return
        error = task.exception()
        _settle(pending, error=error if error is not None else StopAsyncIteration())

    def _discard_pending(self) -> None:
        pending = self._pending
        self._pending = None
        if pending is None:
            return
        if not pending.done():
            pending.cancel()
        elif not pending.cancelled():
            # Retrieved so asyncio does not log it as never retrieved; the
            # request capture has already recorded whatever it was.
            pending.exception()


def _settle(pending: asyncio.Future[str] | None, *, error: BaseException) -> None:
    if pending is not None and not pending.done():
        pending.set_exception(error)


async def _await_cancelled_task(task: asyncio.Task[None]) -> bool:
    """Wait for a cancelled task to finish; ``False`` if abandoned at the deadline."""

    try:
        await asyncio.wait((task,), timeout=_cleanup_wait_budget())
    finally:
        if not task.done():
            task.add_done_callback(_consume_cleanup_outcome)
    if not task.done():
        return False
    _consume_cleanup_outcome(task)
    return True


async def _close_pre_start_body(
    body: object,
    *,
    preserved_error: BaseException,
) -> None:
    task = asyncio.create_task(
        close_stream_input(
            body,
            owner="first_chunk_streaming_response",
            source="api",
            preserved_error=preserved_error,
        ),
        name="mcc-api-pre-start-stream-cleanup",
    )
    await _wait_for_cleanup(task)


class _PrefetchedStream(AsyncIterator[str]):
    """Replay one prefetched frame while retaining ownership of the tail."""

    def __init__(
        self,
        first_chunk: str,
        body: AsyncIterator[str],
        *,
        terminal_frame: TerminalFrameEmitter | None,
        terminal_failure_observer: TerminalFailureObserver | None,
        source: _KeepaliveSource | None = None,
        first_is_keepalive: bool = False,
        late_failure_frame: TerminalFrameEmitter | None = None,
    ) -> None:
        self._first_chunk: str | None = first_chunk
        self._body = body
        self._terminal_frame = terminal_frame
        self._terminal_failure_observer = terminal_failure_observer
        self._source = source
        # True only while a keepalive, not a real frame, is all the client has
        # been sent: a failure then is a pre-start failure reported late.
        self._awaiting_first_frame = first_is_keepalive
        self._late_failure_frame = late_failure_frame
        self._done = False
        self._closed = False

    def __aiter__(self) -> _PrefetchedStream:
        return self

    async def __anext__(self) -> str:
        if self._closed or self._done:
            raise StopAsyncIteration
        if self._first_chunk is not None:
            first_chunk = self._first_chunk
            self._first_chunk = None
            if self._awaiting_first_frame:
                note_serving_task()
            else:
                note_stream_chunk()
            return first_chunk
        source = self._source
        try:
            if source is None:
                chunk = await anext(self._body)
            else:
                next_chunk = await source.next()
                if next_chunk is None:
                    # Not a chunk the body produced, so it is not counted as
                    # progress: the stuck-request watchdog and the in-flight
                    # panel must still see a silent stream as silent. The task
                    # writing it is adopted, which is all the registry needs.
                    note_serving_task()
                    return source.frame
                chunk = next_chunk
        except StopAsyncIteration:
            if self._awaiting_first_frame:
                return self._failure_chunk(
                    EmptyStreamError("Stream ended before emitting a response.")
                )
            self._done = True
            raise
        except BaseExceptionGroup as exc:
            if self._awaiting_first_frame:
                return self._failure_chunk(exc)
            return self._terminal_chunk(find_execution_failure(exc) or exc)
        except Exception as exc:
            if self._awaiting_first_frame:
                return self._failure_chunk(exc)
            return self._terminal_chunk(exc)
        self._awaiting_first_frame = False
        # After the await, so it counts chunks that were actually produced, and
        # here rather than in ``RequestCapture._observe`` because that observer
        # is skipped entirely when the request log is off. This is also where
        # the task that iterates a streaming body announces itself: it is a
        # child of the handler's task, started by Starlette's
        # ``StreamingResponse``, and it -- not the handler -- holds the awaits
        # of a stream that has gone quiet. Two increments and a ``ContextVar``
        # read, and a single module-level flag away when the watchdog is off.
        note_stream_chunk()
        return chunk

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._done = True
        close_error = await try_close_async_iterator(
            self._body if self._source is None else self._source
        )
        if close_error is not None:
            raise close_error

    def _failure_chunk(self, exc: BaseException) -> str:
        """A failure before any real frame, on a stream a keepalive committed."""

        late_failure_frame = self._late_failure_frame
        if late_failure_frame is None:
            if isinstance(exc, BaseExceptionGroup):
                return self._terminal_chunk(find_execution_failure(exc) or exc)
            return self._terminal_chunk(exc)
        self._done = True
        return late_failure_frame(exc)

    def _terminal_chunk(self, exc: BaseException) -> str:
        terminal_frame = self._terminal_frame
        if terminal_frame is None:
            raise exc
        self._done = True
        if self._terminal_failure_observer is not None:
            self._terminal_failure_observer(exc)
        return terminal_frame(exc)


async def anthropic_sse_streaming_response(
    body: AsyncIterator[str],
    *,
    pre_start_error_response: PreStartErrorResponse,
    request_id: str,
    keepalive: StreamKeepalive | None = None,
) -> Response:
    """Return a streaming response for Anthropic-style SSE streams."""
    return await _first_chunk_streaming_response(
        body,
        headers=ANTHROPIC_SSE_RESPONSE_HEADERS,
        pre_start_error_response=pre_start_error_response,
        terminal_frame=_anthropic_terminal_frame,
        terminal_failure_observer=lambda exc: _trace_anthropic_terminal_failure(
            exc,
            request_id=request_id,
        ),
        keepalive=keepalive,
        keepalive_frame=ANTHROPIC_KEEPALIVE_FRAME,
    )


def _anthropic_terminal_frame(exc: BaseException) -> str:
    failure = find_execution_failure(exc)
    if failure is not None:
        return anthropic_terminal_failure_frame(failure)
    return anthropic_terminal_error_frame(safe_exception_message(exc))


def _trace_anthropic_terminal_failure(
    exc: BaseException,
    *,
    request_id: str,
) -> None:
    failure = find_execution_failure(exc)
    trace_terminal_execution_error(
        wire_api="messages",
        request_id=request_id,
        status_code=failure.status_code if failure is not None else 500,
        error_type=(
            anthropic_error_type_for_failure(failure)
            if failure is not None
            else "api_error"
        ),
        error=exc,
    )


async def openai_sse_streaming_response(
    body: AsyncIterator[str],
    *,
    headers: Mapping[str, str],
    pre_start_error_response: PreStartErrorResponse,
    keepalive: StreamKeepalive | None = None,
) -> Response:
    """Return a streaming response for an OpenAI-shaped or the Gemini SSE dialect.

    Neither OpenAI surface gets a terminal frame emitter: both adapters already
    own how their own stream ends after a post-start failure -- Responses with
    ``response.failed``, Chat Completions with an ``error`` object followed by
    ``[DONE]`` -- and a second, protocol-blind ending appended here would
    contradict the one they wrote.
    """
    return await _first_chunk_streaming_response(
        body,
        headers=headers,
        pre_start_error_response=pre_start_error_response,
        terminal_frame=None,
        terminal_failure_observer=None,
        keepalive=keepalive,
        keepalive_frame=SSE_COMMENT_KEEPALIVE_FRAME,
    )
