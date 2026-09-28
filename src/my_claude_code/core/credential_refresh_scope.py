"""Why a shared OAuth credential is being asked for, carried by context.

A credential MCC shares with Claude Code or Codex may be refreshed only when a
real client request needs it (``request``) or the operator pressed the card's
button (``operator``). Everything else -- the discovery timer, startup
validation, probes, model listing, the image-description side call -- is
``background`` and must never spend a shared refresh token.

The rule is **default-deny**: an unset scope is background. Exactly one place
grants ``request``: the OAuth provider's ``stream_response`` wraps its stream
in :func:`request_scoped_stream`. The describe side call runs inside
:func:`background_scope`, which is sticky: a nested ``stream_response`` inside
it cannot elevate itself, so an image description never refreshes a shared
credential even though it travels the same provider code as a request.

A ``ContextVar`` because the purpose has to reach ``auth.headers()`` deep
inside the shared Messages transport, whose bytes must not change. The
request scope is set and reset around **each** step of the stream rather than
once for its lifetime: an async generator runs in whichever context iterates
it, so a value set before a ``yield`` would leak into the consumer between
chunks.

Imports nothing from ``config/`` (``core`` is a leaf below it).
"""

import contextlib
from collections.abc import AsyncIterator, Iterator
from contextvars import ContextVar
from typing import Literal

RefreshPurpose = Literal["request", "operator", "background"]

#: Nothing set: treated as background (default-deny).
_UNSET = "unset"
_BACKGROUND = "background"
_REQUEST = "request"

_SCOPE: ContextVar[str] = ContextVar("mcc_credential_refresh_scope", default=_UNSET)


def current_purpose() -> RefreshPurpose:
    """The purpose of the credential use happening in this context."""

    value = _SCOPE.get()
    if value == _REQUEST:
        return "request"
    return "background"


@contextlib.contextmanager
def background_scope() -> Iterator[None]:
    """Mark everything inside as background, stickily.

    Used by the describe side call. Nothing nested inside can elevate itself
    to ``request``.
    """

    token = _SCOPE.set(_BACKGROUND)
    try:
        yield
    finally:
        _SCOPE.reset(token)


async def request_scoped_stream[T](inner: AsyncIterator[T]) -> AsyncIterator[T]:
    """Yield ``inner`` with each step run as a client ``request``.

    Inside an explicit :func:`background_scope` the scope is left alone. The
    inner stream is always closed when this one is, so an abandoned request
    still releases the upstream the way it did before.
    """

    iterator = aiter(inner)
    try:
        while True:
            token = _SCOPE.set(_REQUEST) if _SCOPE.get() == _UNSET else None
            try:
                chunk = await anext(iterator)
            except StopAsyncIteration:
                return
            finally:
                if token is not None:
                    _SCOPE.reset(token)
            yield chunk
    finally:
        closer = getattr(iterator, "aclose", None)
        if closer is not None:
            await closer()


def stream_purpose() -> RefreshPurpose:
    """The purpose a provider's ``stream_response`` claims for itself.

    ``request`` -- a client request is being served -- unless an explicit
    :func:`background_scope` is active (the describe side call). For a
    provider that resolves its credential synchronously inside
    ``stream_response`` (ChatGPT), this is the whole grant.
    """

    return "background" if _SCOPE.get() == _BACKGROUND else "request"


@contextlib.contextmanager
def claimed_scope(purpose: RefreshPurpose) -> Iterator[None]:
    """Run the body under ``purpose``, as :func:`stream_purpose` decided it.

    For code that resolves a credential synchronously, or hands the work to
    ``asyncio.to_thread`` (which copies this context into the thread), so the
    purpose reaches the credential layer without changing any call shape. A
    ``request`` claim never overrides an explicit background scope.
    """

    if purpose == "request" and _SCOPE.get() != _BACKGROUND:
        token = _SCOPE.set(_REQUEST)
    elif purpose == "background":
        token = _SCOPE.set(_BACKGROUND)
    else:
        token = None
    try:
        yield
    finally:
        if token is not None:
            _SCOPE.reset(token)
