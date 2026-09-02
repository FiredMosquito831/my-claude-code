"""The inbound product surfaces this proxy serves, and how each reports errors.

Three routes now accept a chat request -- ``/v1/messages`` (Anthropic),
``/v1/responses`` (OpenAI Responses) and ``/v1/chat/completions`` (OpenAI Chat
Completions) -- and two of the three want their errors in the OpenAI envelope.
Before this module that question was answered by comparing ``request.url.path``
against one literal in two separate places, which is exactly the shape of thing
that gets extended in one place and not the other: the observable symptom would
have been an OpenAI SDK client receiving an Anthropic error body and reporting
"unknown error" for a perfectly well-described 400.

Path constants live here rather than in the route module so that handlers,
routes and the app-level exception handlers can all agree without importing
each other.
"""

from typing import Literal

MESSAGES_ENDPOINT = "/v1/messages"
RESPONSES_ENDPOINT = "/v1/responses"
CHAT_COMPLETIONS_ENDPOINT = "/v1/chat/completions"

WireApi = Literal["messages", "responses", "chat_completions"]

_WIRE_API_BY_PATH: dict[str, WireApi] = {
    RESPONSES_ENDPOINT: "responses",
    CHAT_COMPLETIONS_ENDPOINT: "chat_completions",
}

#: The surfaces whose clients parse ``{"error": {...}}``. Membership -- not the
#: path -- is what every error boundary should ask about.
_OPENAI_SHAPED: frozenset[WireApi] = frozenset({"responses", "chat_completions"})


def wire_api_for_path(path: str) -> WireApi:
    """Return the product surface a request path belongs to.

    Anything unrecognised is the Anthropic surface, which is what the
    app-level handlers have always defaulted to: an admin or probe route that
    somehow reaches an error boundary is better served an Anthropic body than
    no body at all.
    """
    return _WIRE_API_BY_PATH.get(path, "messages")


def is_openai_shaped(wire_api: WireApi) -> bool:
    """Whether this surface reports failure in the OpenAI error envelope."""
    return wire_api in _OPENAI_SHAPED
