"""The inbound product surfaces this proxy serves, and how each reports errors.

Four routes now accept a chat request -- ``/v1/messages`` (Anthropic),
``/v1/responses`` (OpenAI Responses), ``/v1/chat/completions`` (OpenAI Chat
Completions) and ``/v1beta/models/{model}:generateContent`` (Google Gemini) --
and each family wants its errors in an envelope of its own.
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

#: The Gemini surface is a *prefix* rather than one path: the model id sits in
#: the path and carries slashes -- ``/v1beta/models/anthropic/openrouter/gpt-5
#: :generateContent`` -- so membership is decided by prefix where the other
#: three are decided by an exact match.
GEMINI_ENDPOINT_PREFIX = "/v1beta/models"
#: A Veo job's own paths (7.65.0): ``GET /v1beta/operations/{id}`` polls a
#: ``:predictLongRunning`` job and ``GET /v1beta/files/{id}:download`` fetches
#: its video -- what ``google-genai`` builds from the operation's ``name`` and
#: the video's ``uri``. Google-shaped like the models paths.
GEMINI_OPERATIONS_PREFIX = "/v1beta/operations"
GEMINI_FILES_PREFIX = "/v1beta/files"
# Media endpoints (7.60.0) answer in the OpenAI error envelope, the shape
# every OpenAI SDK client reads.
IMAGES_GENERATIONS_ENDPOINT = "/v1/images/generations"
IMAGES_EDITS_ENDPOINT = "/v1/images/edits"
AUDIO_SPEECH_ENDPOINT = "/v1/audio/speech"
AUDIO_TRANSCRIPTIONS_ENDPOINT = "/v1/audio/transcriptions"
AUDIO_TRANSLATIONS_ENDPOINT = "/v1/audio/translations"
VIDEOS_ENDPOINT = "/v1/videos"

WireApi = Literal["messages", "responses", "chat_completions", "gemini"]

_WIRE_API_BY_PATH: dict[str, WireApi] = {
    RESPONSES_ENDPOINT: "responses",
    CHAT_COMPLETIONS_ENDPOINT: "chat_completions",
    IMAGES_GENERATIONS_ENDPOINT: "chat_completions",
    IMAGES_EDITS_ENDPOINT: "chat_completions",
    AUDIO_SPEECH_ENDPOINT: "chat_completions",
    AUDIO_TRANSCRIPTIONS_ENDPOINT: "chat_completions",
    AUDIO_TRANSLATIONS_ENDPOINT: "chat_completions",
    VIDEOS_ENDPOINT: "chat_completions",
}

#: The surfaces whose clients parse OpenAI's ``{"error": {...}}``. Membership
#: -- not the path -- is what every error boundary should ask about. Google's
#: envelope is also called ``error`` and is *not* the same object: it carries
#: ``code`` and ``status`` where OpenAI's carries ``type`` and ``param``, so it
#: has a set of its own rather than sharing this one.
_OPENAI_SHAPED: frozenset[WireApi] = frozenset({"responses", "chat_completions"})
_GEMINI_SHAPED: frozenset[WireApi] = frozenset({"gemini"})


def wire_api_for_path(path: str) -> WireApi:
    """Return the product surface a request path belongs to.

    Anything unrecognised is the Anthropic surface, which is what the
    app-level handlers have always defaulted to: an admin or probe route that
    somehow reaches an error boundary is better served an Anthropic body than
    no body at all.
    """
    if path.startswith(
        (GEMINI_ENDPOINT_PREFIX, GEMINI_OPERATIONS_PREFIX, GEMINI_FILES_PREFIX)
    ):
        return "gemini"
    if path.startswith(VIDEOS_ENDPOINT + "/"):
        # A video job's own paths (``/v1/videos/{id}``, ``.../content``) are
        # OpenAI-shaped like the create; their clients parse OpenAI errors.
        return "chat_completions"
    return _WIRE_API_BY_PATH.get(path, "messages")


def is_openai_shaped(wire_api: WireApi) -> bool:
    """Whether this surface reports failure in the OpenAI error envelope."""
    return wire_api in _OPENAI_SHAPED


def is_gemini_shaped(wire_api: WireApi) -> bool:
    """Whether this surface reports failure in the Google error envelope."""
    return wire_api in _GEMINI_SHAPED
