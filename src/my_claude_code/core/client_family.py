"""Which API dialect the *inbound* request was written in.

7.114.0. A gateway that fronts several APIs can serve one model on more than
one of them, and when it can, the honest first door depends on what the caller
speaks: a Claude Code request arrives as Anthropic Messages and should be
answered from the Messages door where the model has one, while a Codex or
OpenAI-SDK request arrives in one of OpenAI's two dialects and should be
answered from Responses. Nothing about the *model* changes; only which of its
known doors is asked first.

The family is a fact about the door the request came in through, which every
handler already knows (``api/handlers/messages.py`` passes
``wire_api="messages"`` and so on), and it is carried to the surface resolver
in the same kind of per-request slot the proxy label and the recovery rung
already use (``core/upstream_ladder.py:456-471``): widening
``BaseProvider.stream_response`` -- a signature every provider shares and no
provider would read -- would be a much larger change than the feature needs.

Three values, and :data:`UNKNOWN` is the safe one:

* every path with no request in flight -- the Models page, the admin
  endpoints, a capability probe, a startup sweep -- reads ``UNKNOWN``, and
  ``UNKNOWN`` prefers nothing, so those paths keep the order they always had;
* a dialect this module does not name (``gemini``) is ``UNKNOWN`` for the same
  reason: a Gemini-format request says nothing about which of *this* gateway's
  doors its model answers on, and guessing on its behalf would move a door
  nobody asked to move.

The slot is installed in one place, ``application/execution.py``, around the
provider stream it is about to consume, so it is set before any provider code
runs and reset when the stream ends. A stream that is never iterated leaves
nothing behind.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from enum import StrEnum


class ClientFamily(StrEnum):
    """The inbound dialect, as far as the surface resolver cares."""

    #: ``POST /v1/messages`` -- Anthropic's Messages protocol.
    ANTHROPIC = "anthropic"
    #: ``POST /v1/chat/completions`` or ``POST /v1/responses`` -- either of
    #: OpenAI's two dialects. They share one preference because they share one
    #: client population: an OpenAI-format caller is best served from the
    #: Responses door where a model has one, and both handlers know they are
    #: that caller.
    OPENAI = "openai"
    #: No request in flight, or a dialect that says nothing about a surface.
    UNKNOWN = "unknown"


#: The per-request slot. ``UNKNOWN`` is the default rather than ``None`` so a
#: caller that never installs a scope reads the safe answer, not an error.
_CLIENT_FAMILY: ContextVar[ClientFamily] = ContextVar(
    "mcc_client_family", default=ClientFamily.UNKNOWN
)

#: The wire APIs that name a family. Anything else -- ``gemini`` today -- is
#: :data:`ClientFamily.UNKNOWN`.
_CLIENT_FAMILY_BY_WIRE_API: dict[str, ClientFamily] = {
    "messages": ClientFamily.ANTHROPIC,
    "responses": ClientFamily.OPENAI,
    "chat_completions": ClientFamily.OPENAI,
}


@contextmanager
def client_family_scope(wire_api: str) -> Iterator[None]:
    """Answer :func:`current_client_family` with ``wire_api``'s family here."""

    token = _CLIENT_FAMILY.set(
        _CLIENT_FAMILY_BY_WIRE_API.get(wire_api, ClientFamily.UNKNOWN)
    )
    try:
        yield
    finally:
        _CLIENT_FAMILY.reset(token)


def current_client_family() -> ClientFamily:
    """The family of the request in flight, or ``UNKNOWN`` when none is."""

    return _CLIENT_FAMILY.get()


def reset_client_family() -> None:
    """Return the slot to ``UNKNOWN``, discarding any scope in flight.

    For the test suite, which must not inherit a family from a test that left
    a stream half-consumed: the slot is per-context and a task that ended
    mid-stream can outlive the scope that would have reset it.
    """

    _CLIENT_FAMILY.set(ClientFamily.UNKNOWN)
