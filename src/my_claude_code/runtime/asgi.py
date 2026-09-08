"""ASGI lifespan adapter for the application runtime owner."""

import asyncio
import json
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from loguru import logger
from starlette.types import ASGIApp, Receive, Scope, Send

from my_claude_code.core.startup_state import (
    STARTING_MARKER_HEADER,
    STARTING_MARKER_VALUE,
    STARTING_RETRY_AFTER_SECONDS,
    startup_state,
)
from my_claude_code.core.stop_deadline import (
    SHUTDOWN_MARKER_HEADER,
    SHUTDOWN_MARKER_VALUE,
    SHUTDOWN_RETRY_AFTER_SECONDS,
    stop_deadline,
)

from .application import ApplicationRuntime, startup_failure_message

# What a request that arrives during the drain is told. A plain 503 rather than
# a new wire frame: the harness already knows how to retry one, and inventing a
# shutdown-specific protocol message would be a contract every client has to
# learn. ``connection: close`` is the load-bearing header -- without it the
# refusal travels back down a keep-alive connection that the client will
# happily reuse for the next request, which is how a closing server kept
# serving new work for the whole drain window before 6.41.0.
_SHUTTING_DOWN_BODY = json.dumps(
    {
        "error": {
            "type": "service_unavailable",
            "message": (
                "My Claude Code is shutting down and is not accepting new "
                "requests. Retry in a few seconds."
            ),
        }
    }
).encode("utf-8")
_SHUTTING_DOWN_HEADERS = [
    (b"content-type", b"application/json"),
    (b"content-length", str(len(_SHUTTING_DOWN_BODY)).encode("ascii")),
    (b"connection", b"close"),
    (b"retry-after", str(SHUTDOWN_RETRY_AFTER_SECONDS).encode("ascii")),
    # The signature a program reads. Without it, ``probe_server_presence``
    # cannot tell MCC mid-drain from a stranger on the port, and a desktop
    # window launched during a restart accuses MCC's own process of being a
    # port conflict. A bare 503 is not enough: any reverse proxy, any other
    # service, any unrelated application can answer 503 on that port.
    (
        SHUTDOWN_MARKER_HEADER.encode("ascii"),
        SHUTDOWN_MARKER_VALUE.encode("ascii"),
    ),
]


def _starting_payload() -> tuple[bytes, list[tuple[bytes, bytes]]]:
    """The body and headers of a "still coming up" refusal.

    Built per request rather than once at import because two of its three
    fields change every millisecond. It is small, and it is only ever built
    while the server has nothing else to do.
    """

    state = startup_state()
    body = json.dumps(
        {
            "status": "starting",
            "stage": state.stage,
            "elapsed_ms": state.elapsed_ms,
            "error": {
                "type": "service_unavailable",
                "message": (
                    "My Claude Code is still starting and is not accepting "
                    "requests yet. Retry in a moment."
                ),
            },
        }
    ).encode("utf-8")
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
        (b"retry-after", str(STARTING_RETRY_AFTER_SECONDS).encode("ascii")),
        # The signature a program reads, and the whole reason the listener
        # binds before the work now happens: without it a starting server is a
        # free port, and a free port is an invitation to start a second one.
        (
            STARTING_MARKER_HEADER.encode("ascii"),
            STARTING_MARKER_VALUE.encode("ascii"),
        ),
    ]
    return body, headers


async def _refuse_while_starting(send: Send) -> None:
    """Answer one HTTP request with the 503 that names the current stage."""

    body, headers = _starting_payload()
    await send({"type": "http.response.start", "status": 503, "headers": headers})
    await send({"type": "http.response.body", "body": body})


#: How long ``_wait_until_serving`` waits for the supervisor to report an
#: accepting listener before giving up and starting the application regardless.
SERVING_WAIT_SECONDS = 10.0

#: How often it asks. Small: this is the delay added to every start.
SERVING_POLL_SECONDS = 0.01


async def _refuse_during_shutdown(send: Send) -> None:
    """Answer one HTTP request with 503 + ``connection: close``."""

    await send(
        {
            "type": "http.response.start",
            "status": 503,
            "headers": list(_SHUTTING_DOWN_HEADERS),
        }
    )
    await send({"type": "http.response.body", "body": _SHUTTING_DOWN_BODY})


class RuntimeASGIApp:
    """Delegate HTTP to FastAPI and lifespan to `ApplicationRuntime`."""

    def __init__(
        self,
        app: ASGIApp,
        runtime: ApplicationRuntime,
        startup_failed_callback: Callable[[], None] | None = None,
        serving_predicate: Callable[[], bool] | None = None,
    ) -> None:
        self.app = app
        self.runtime = runtime
        # Called when the background startup raises. The supervisor owns the
        # decision to end the process; this class only reports.
        self._startup_failed_callback = startup_failed_callback
        # "Is the server actually accepting connections yet?", asked of the
        # supervisor that owns the uvicorn Server. See ``_run_startup``.
        self._serving_predicate = serving_predicate
        self._startup_task: asyncio.Task[None] | None = None

    @property
    def startup_task(self) -> asyncio.Task[None] | None:
        """The background startup, once the lifespan has scheduled it.

        Exposed because "the listener is up but the application is not" is now
        a real state with a real handle, and a test that wants to assert on
        what startup did has to be able to wait for it without sleeping.
        """

        return self._startup_task

    def __getattr__(self, name: str) -> Any:
        return getattr(self.app, name)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
            return
        # The shutdown gate. uvicorn stops ACCEPTING at the first instant of a
        # stop, but a client holding an established keep-alive connection could
        # still pipeline new requests down it for the whole drain -- each one
        # taking a fresh provider lease, each lease re-clearing ``drained``, and
        # so pushing the finish line out indefinitely. Refusing here, at the
        # outermost ASGI layer, is what makes the drain a drain.
        #
        # Health and admin routes are refused the same way on purpose: a server
        # that is going away should say so with one answer, not two. The
        # dashboard's reconnect loop treats a failed /admin/api/version as the
        # expected mid-handoff disconnect and keeps polling until the new
        # process answers, so uniform refusal costs it nothing.
        if stop_deadline().requested:
            if scope["type"] == "http":
                await _refuse_during_shutdown(send)
                return
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1012})
                return
        # The startup gate, checked strictly AFTER the drain gate. A process
        # that is both starting and stopping -- a stop requested during a slow
        # start, which is exactly what a restart-during-startup is -- must
        # answer "going", not "coming": a caller told "coming" would wait for a
        # server that is on its way out. Ordering these two ifs is the whole of
        # that rule.
        if not startup_state().ready:
            if scope["type"] == "http":
                await _refuse_while_starting(send)
                return
            if scope["type"] == "websocket":
                # 1013 "try again later", the counterpart of the drain's 1012
                # "service restart".
                await send({"type": "websocket.close", "code": 1013})
                return
        await self.app(scope, receive, send)

    async def _run_startup(self) -> None:
        """Run the heavy startup behind an already-bound listener.

        Everything that used to happen *before* uvicorn created its socket --
        learned facts, configured-model validation, the catalogue sweep, the
        rediscovery timer, messaging -- happens here instead, while the port is
        already answering ``starting``. Nothing is skipped and nothing is
        reordered relative to its neighbours; only the listener moved.
        """

        state = startup_state()
        await self._wait_until_serving()
        try:
            await self.runtime.start()
        except asyncio.CancelledError:
            # A stop arrived mid-start. ``ApplicationRuntime.start`` has
            # already closed what it opened; the shutdown branch below owns
            # the rest.
            state.mark_failed()
            raise
        except Exception as exc:
            # ``ApplicationRuntime.start`` has already logged and closed. This
            # is the part uvicorn used to do for us: a startup that fails must
            # end the process rather than leave a listener answering
            # ``starting`` forever.
            logger.error(
                "Startup failed after the listener was bound; stopping.\n{}",
                startup_failure_message(self.runtime.settings, exc),
            )
            state.mark_failed()
            if self._startup_failed_callback is not None:
                self._startup_failed_callback()
            return
        state.mark_ready()

    async def _wait_until_serving(self) -> None:
        """Hold the heavy startup until uvicorn is accepting connections.

        The lifespan is answered before the socket exists, so this task and
        uvicorn's own ``startup()`` continuation are scheduled together and
        this one runs first. Startup is not purely cooperative -- provider
        construction and the configured-model probe each block the loop for up
        to a second at a time -- so a task that begins immediately can starve
        the very ``create_server`` call it was moved in front of, and the port
        would still look free for the whole start. Waiting for the answer the
        supervisor already has is what makes "bind first" true rather than
        merely intended.

        Bounded, and silent when it times out: a slow bind is still better
        served by starting the application than by refusing to.
        """

        predicate = self._serving_predicate
        if predicate is None:
            return
        deadline = asyncio.get_running_loop().time() + SERVING_WAIT_SECONDS
        while not predicate():
            if asyncio.get_running_loop().time() >= deadline:
                logger.warning(
                    "The listener was not reported as serving within "
                    "{seconds:.0f}s; starting the application anyway.",
                    seconds=SERVING_WAIT_SECONDS,
                )
                return
            await asyncio.sleep(SERVING_POLL_SECONDS)

    async def _await_startup_task(self) -> None:
        """Stop the background startup before tearing the runtime down.

        Closing a runtime whose ``start()`` is still running would race two
        owners over the same provider generation. The cancellation is bounded
        by the shared stop deadline like every other teardown stage.
        """

        task = self._startup_task
        if task is None or task.done():
            return
        task.cancel()
        deadline = stop_deadline()
        timeout = deadline.teardown_remaining() if deadline.requested else None
        with suppress(asyncio.CancelledError, TimeoutError, Exception):
            await asyncio.wait_for(asyncio.shield(task), timeout)

    async def _lifespan(self, receive: Receive, send: Send) -> None:
        started = False
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                # The bind-first switch, and it is this small: uvicorn creates
                # its listening socket the instant this message is answered,
                # so answering it *before* the work rather than after is what
                # turns twenty seconds of "free port" into twenty seconds of
                # "starting". The work itself is unchanged -- it is now a task
                # this object owns instead of a call this coroutine awaits.
                self._startup_task = asyncio.create_task(self._run_startup())
                started = True
                await send({"type": "lifespan.startup.complete"})
                continue

            if message["type"] == "lifespan.shutdown":
                await self._await_startup_task()
                if started:
                    try:
                        closed = await self._close_runtime()
                    except TimeoutError:
                        # The teardown ran past the shared stop deadline. Report
                        # the shutdown as failed rather than waiting: uvicorn
                        # returns control to the supervisor, which then owns the
                        # decision (refuse a REPLACE_PROCESS, keep a RELOAD up).
                        logger.error(
                            "Shutdown did not finish within the graceful "
                            "shutdown budget; closing anyway."
                        )
                        await send({"type": "lifespan.shutdown.failed", "message": ""})
                        return
                    except Exception as exc:
                        logger.error(
                            "Shutdown failed: exc_type={}",
                            type(exc).__name__,
                        )
                        await send({"type": "lifespan.shutdown.failed", "message": ""})
                        return
                    if not closed:
                        await send({"type": "lifespan.shutdown.failed", "message": ""})
                        return
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def _close_runtime(self) -> bool:
        """Close the runtime, bounded by the shared stop deadline.

        ``ApplicationRuntime.close`` deliberately applies no generic timeout of
        its own -- cancelling an arbitrary cleanup at a deadline can abandon a
        half-closed SDK or provider resource, and the owner would rather hand
        that decision to the process supervisor. Before 6.41.0 the supervisor
        had no way to take it, because it never regained control. This is the
        backstop that gives it back: the resource-specific bounds inside
        ``ProviderRuntimeManager.close`` and the response cleanup do the real
        work, and this only fires when one of them is not enough.
        """

        deadline = stop_deadline()
        if not deadline.requested:
            return await self.runtime.close()
        return await asyncio.wait_for(
            self.runtime.close(), deadline.teardown_remaining()
        )
