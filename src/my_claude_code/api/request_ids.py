"""Ingress-owned HTTP request correlation."""

import uuid

from fastapi import Request, Response
from loguru import logger
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from my_claude_code.config.logging_config import inbound_capture_enabled
from my_claude_code.core.trace import extract_claude_session_id_from_headers

from . import inbound_body

REQUEST_ID_HEADER = "request-id"
OPENAI_REQUEST_ID_HEADER = "x-request-id"
_REQUEST_ID_STATE_ATTRIBUTE = "fcc_request_id"
_OPENAI_REQUEST_ID_PATHS = frozenset(
    {"/v1/chat/completions", "/v1/responses", "/v1/models"}
)


class RequestCorrelationMiddleware:
    """Own one request id and logging context for the full ASGI response."""

    def __init__(self, app: ASGIApp, *, capture_inbound: bool | None = None) -> None:
        self._app = app
        #: 8.8.0: whether the client's own body is recorded under
        #: ``INBOUND_PAYLOAD``. ``None`` -- how the application installs it --
        #: means "ask the live setting on every request", which is what makes a
        #: Save reach a running server without a restart. A test that wants it
        #: pinned passes the value instead.
        self._capture_inbound = capture_inbound

    def set_capture_inbound(self, enabled: bool) -> None:
        """Pin the flag for this process (a test's, or a future admin hook)."""

        self._capture_inbound = bool(enabled)

    def _inbound_enabled(self) -> bool:
        pinned = self._capture_inbound
        return inbound_capture_enabled() if pinned is None else pinned

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        request_id = new_request_id()
        state = scope.setdefault("state", {})
        state[_REQUEST_ID_STATE_ATTRIBUTE] = request_id
        method = scope.get("method", "")
        path = scope.get("path", "")
        request_headers = Headers(scope=scope)
        claude_sid = extract_claude_session_id_from_headers(request_headers)

        async def send_with_correlation(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = dict(message)
                raw_headers = list(message.get("headers", ()))
                _set_request_id_headers(
                    MutableHeaders(raw=raw_headers),
                    request_id=request_id,
                    path=path,
                )
                message["headers"] = raw_headers
            await send(message)

        with logger.contextualize(
            http_method=method,
            http_path=path,
            claude_session_id=claude_sid,
            request_id=request_id,
        ):
            # 8.8.0: the client's own body, recorded beside the outbound one.
            # ``wrap_receive`` returns ``receive`` itself when the setting is
            # off or the path is not a model door, so this line costs one
            # comparison and the app below reads exactly what arrived.
            await self._app(
                scope,
                inbound_body.wrap_receive(
                    receive,
                    path,
                    request_id=request_id,
                    enabled=self._inbound_enabled(),
                ),
                send_with_correlation,
            )


def new_request_id() -> str:
    """Return a new opaque FCC request identifier."""
    return f"req_{uuid.uuid4().hex}"


def set_request_id(request: Request, request_id: str) -> None:
    """Attach the ingress correlation identifier to request state."""
    setattr(request.state, _REQUEST_ID_STATE_ATTRIBUTE, request_id)


def get_request_id(request: Request) -> str:
    """Return the ingress correlation identifier, creating a fallback if needed."""
    request_id = getattr(request.state, _REQUEST_ID_STATE_ATTRIBUTE, None)
    if isinstance(request_id, str) and request_id:
        return request_id
    request_id = new_request_id()
    set_request_id(request, request_id)
    return request_id


def attach_request_id_headers(
    response: Response, *, request_id: str, path: str
) -> None:
    """Attach correlation when an outer server-error boundary bypasses middleware."""
    _set_request_id_headers(response.headers, request_id=request_id, path=path)


def _set_request_id_headers(
    headers: MutableHeaders,
    *,
    request_id: str,
    path: str,
) -> None:
    headers[REQUEST_ID_HEADER] = request_id
    if path in _OPENAI_REQUEST_ID_PATHS:
        headers[OPENAI_REQUEST_ID_HEADER] = request_id
