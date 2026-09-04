"""ASGI lifespan adapter for the application runtime owner."""

import asyncio
import json
from typing import Any

from loguru import logger
from starlette.types import ASGIApp, Receive, Scope, Send

from my_claude_code.core.stop_deadline import (
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
]


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

    def __init__(self, app: ASGIApp, runtime: ApplicationRuntime) -> None:
        self.app = app
        self.runtime = runtime

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
        await self.app(scope, receive, send)

    async def _lifespan(self, receive: Receive, send: Send) -> None:
        started = False
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                try:
                    await self.runtime.start()
                except Exception as exc:
                    await send(
                        {
                            "type": "lifespan.startup.failed",
                            "message": startup_failure_message(
                                self.runtime.settings,
                                exc,
                            ),
                        }
                    )
                    return
                started = True
                await send({"type": "lifespan.startup.complete"})
                continue

            if message["type"] == "lifespan.shutdown":
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
