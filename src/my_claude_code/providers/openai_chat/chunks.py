"""MCC's own OpenAI-chat stream chunk shape, and the stream that yields it.

The OpenAI SDK decodes every SSE frame of every streamed reply into a full
``ChatCompletionChunk`` pydantic model: one ``construct`` per chunk, plus one
per choice, per delta, per tool call, per tool-call function and per usage
block. A 200-token reply is 200 frames, so a single request pays several
hundred model constructions -- measured at 24 850 ``construct`` calls per 40
requests, about 45 ms of event-loop CPU per request, on a shape MCC reads six
fields out of.

Nothing here re-implements SSE. The SDK's framing and decoding stay exactly
where they are -- :meth:`adopt_chat_stream` takes the decoder and the open
``httpx`` response off the SDK's own stream object -- and the only thing
replaced is the step that turns one already-``json.loads``-ed frame into an
object. What comes out is a plain Python object with the same attribute
surface the SDK model had:

* the same declared fields, with the same ``None`` defaults, so
  ``delta.content`` and ``tool_call.function.arguments`` read identically;
* the same ``model_extra`` / ``__pydantic_extra__`` mapping of *undeclared*
  keys, which is where every host-specific field MCC reads lives --
  ``reasoning``, ``reasoning_content``, ``reasoning_details``,
  ``extra_content``, ``cost``, ``cost_details``, ``is_byok``,
  ``prompt_cache_hit_tokens`` -- and
* attribute access that falls through to those extras, so
  ``getattr(delta, "reasoning")`` on a host that sends ``reasoning`` finds it
  exactly as pydantic's own ``__getattr__`` did.

The declared field sets below are copied from ``openai`` 2.54.0's
``ChatCompletionChunk`` tree deliberately: which keys land in ``model_extra``
is part of the observable shape, so a field the SDK declares must stay
declared here, not silently become an extra.
"""

from collections.abc import AsyncIterator, Mapping
from typing import Any

#: Shared empty extras. Never mutated: ``model_extra`` is read-only to every
#: consumer in this repo, and handing out one object keeps the common case
#: (a chunk with no undeclared keys) allocation-free.
_NO_EXTRA: dict[str, Any] = {}

_CHUNK_FIELDS = frozenset(
    {
        "id",
        "choices",
        "created",
        "model",
        "object",
        "moderation",
        "service_tier",
        "system_fingerprint",
        "usage",
    }
)
_CHOICE_FIELDS = frozenset({"delta", "finish_reason", "index", "logprobs"})
_DELTA_FIELDS = frozenset({"content", "function_call", "refusal", "role", "tool_calls"})
_TOOL_CALL_FIELDS = frozenset({"index", "id", "function", "type"})
_FUNCTION_FIELDS = frozenset({"arguments", "name"})
_USAGE_FIELDS = frozenset(
    {
        "completion_tokens",
        "prompt_tokens",
        "total_tokens",
        "completion_tokens_details",
        "prompt_tokens_details",
    }
)


class _WithExtras:
    """Attribute access that falls through to a host's undeclared keys."""

    __slots__ = ("_extra",)

    _extra: dict[str, Any]

    def __getattr__(self, name: str) -> Any:
        # Only reached when the name is not one of the declared slots, which
        # is exactly when pydantic would have consulted ``__pydantic_extra__``.
        # Missing names raise, rather than answering ``None``, so
        # ``getattr(x, name, default)`` and ``hasattr`` keep their meaning.
        if name == "_extra":
            # Asked before the constructor filled it in; answering by reading
            # it would recurse instead of raising.
            raise AttributeError(name)
        try:
            return self._extra[name]
        except KeyError:
            raise AttributeError(
                f"{type(self).__name__!r} object has no attribute {name!r}"
            ) from None

    @property
    def model_extra(self) -> dict[str, Any]:
        """The undeclared keys this frame carried, under pydantic's name."""
        return self._extra

    @property
    def __pydantic_extra__(self) -> dict[str, Any]:
        """The same mapping, under the name pydantic stores it as.

        ``tool_call_extra_content`` reads this one directly as its last rung.
        """
        return self._extra


def _extras(data: Mapping[Any, Any], declared: frozenset[str]) -> dict[str, Any]:
    if declared.issuperset(data):
        return _NO_EXTRA
    return {key: value for key, value in data.items() if key not in declared}


class MccToolCallFunction(_WithExtras):
    """``choices[].delta.tool_calls[].function``."""

    __slots__ = ("arguments", "name")

    def __init__(self, data: Mapping[Any, Any]) -> None:
        self.arguments: Any = data.get("arguments")
        self.name: Any = data.get("name")
        self._extra = _extras(data, _FUNCTION_FIELDS)


class MccToolCall(_WithExtras):
    """One ``choices[].delta.tool_calls[]`` entry."""

    __slots__ = ("function", "id", "index", "type")

    def __init__(self, data: Mapping[Any, Any]) -> None:
        self.index: Any = data.get("index")
        self.id: Any = data.get("id")
        function = data.get("function")
        self.function: Any = (
            MccToolCallFunction(function) if isinstance(function, Mapping) else function
        )
        self.type: Any = data.get("type")
        self._extra = _extras(data, _TOOL_CALL_FIELDS)


class MccDelta(_WithExtras):
    """``choices[].delta`` -- the only part of a chunk read on every frame."""

    __slots__ = ("content", "function_call", "refusal", "role", "tool_calls")

    def __init__(self, data: Mapping[Any, Any]) -> None:
        self.content: Any = data.get("content")
        self.function_call: Any = data.get("function_call")
        self.refusal: Any = data.get("refusal")
        self.role: Any = data.get("role")
        tool_calls = data.get("tool_calls")
        self.tool_calls: Any = (
            [
                MccToolCall(entry) if isinstance(entry, Mapping) else entry
                for entry in tool_calls
            ]
            if isinstance(tool_calls, list)
            else tool_calls
        )
        self._extra = _extras(data, _DELTA_FIELDS)


class MccChoice(_WithExtras):
    """One ``choices[]`` entry."""

    __slots__ = ("delta", "finish_reason", "index", "logprobs")

    def __init__(self, data: Mapping[Any, Any]) -> None:
        delta = data.get("delta")
        self.delta: Any = MccDelta(delta) if isinstance(delta, Mapping) else delta
        self.finish_reason: Any = data.get("finish_reason")
        self.index: Any = data.get("index")
        self.logprobs: Any = data.get("logprobs")
        self._extra = _extras(data, _CHOICE_FIELDS)


class MccUsage(_WithExtras):
    """A final usage block.

    ``prompt_tokens_details`` and ``completion_tokens_details`` stay plain
    mappings: every reader of them in this repo (``usage_int``,
    ``prompt_tokens_details``, ``reported_cost._lookup``) already takes the
    ``Mapping`` branch first, so building objects for them would buy nothing.
    """

    __slots__ = (
        "completion_tokens",
        "completion_tokens_details",
        "prompt_tokens",
        "prompt_tokens_details",
        "total_tokens",
    )

    #: What ``wire_capture._public_attrs`` reads to record *which* usage keys a
    #: host answered with. The SDK model exposes its declared fields here, so
    #: this exposes the same names and the recorded shape does not move.
    model_fields: Mapping[str, None] = {
        "completion_tokens": None,
        "prompt_tokens": None,
        "total_tokens": None,
        "completion_tokens_details": None,
        "prompt_tokens_details": None,
    }

    def __init__(self, data: Mapping[Any, Any]) -> None:
        self.completion_tokens: Any = data.get("completion_tokens")
        self.prompt_tokens: Any = data.get("prompt_tokens")
        self.total_tokens: Any = data.get("total_tokens")
        self.completion_tokens_details: Any = data.get("completion_tokens_details")
        self.prompt_tokens_details: Any = data.get("prompt_tokens_details")
        self._extra = _extras(data, _USAGE_FIELDS)


class MccChunk(_WithExtras):
    """One streamed ``chat.completion.chunk``."""

    __slots__ = (
        "choices",
        "created",
        "id",
        "model",
        "moderation",
        "object",
        "service_tier",
        "system_fingerprint",
        "usage",
    )

    def __init__(self, data: Mapping[Any, Any]) -> None:
        self.id: Any = data.get("id")
        choices = data.get("choices")
        # A frame with no ``choices`` key at all keeps ``None``, which is what
        # the SDK stores too: ``field_get_default`` turns a missing required
        # field into ``None``. Every reader here asks ``if not chunk.choices``,
        # so the usage-only final frame some hosts send without a ``choices``
        # key goes on being read as carrying no choices.
        self.choices: Any = (
            [
                MccChoice(entry) if isinstance(entry, Mapping) else entry
                for entry in choices
            ]
            if isinstance(choices, list)
            else choices
        )
        self.created: Any = data.get("created")
        self.model: Any = data.get("model")
        self.object: Any = data.get("object")
        self.moderation: Any = data.get("moderation")
        self.service_tier: Any = data.get("service_tier")
        self.system_fingerprint: Any = data.get("system_fingerprint")
        usage = data.get("usage")
        self.usage: Any = MccUsage(usage) if isinstance(usage, Mapping) else usage
        self._extra = _extras(data, _CHUNK_FIELDS)


def build_chunk(data: object) -> Any:
    """Build one chunk from a decoded frame, or pass a non-object through.

    The SDK's ``construct_type`` returns a value it cannot coerce unchanged;
    a frame whose JSON is not an object is handed on the same way here.
    """
    if isinstance(data, Mapping):
        return MccChunk(data)
    return data


class MccChatChunkStream:
    """An OpenAI-chat SSE stream that yields :class:`MccChunk`.

    A transcription of ``openai._streaming.AsyncStream.__stream__`` with one
    line changed: where the SDK calls ``client._process_response_data`` to
    build a pydantic model, this calls :func:`build_chunk`. Everything else --
    the decoder, ``[DONE]``, the ``{"error": ...}`` frame that becomes an
    ``APIError``, closing the response in a ``finally`` -- is the SDK's own
    behaviour, kept deliberately identical.

    The Assistants ``thread.*`` event branch is not carried over: it cannot
    occur on ``/chat/completions``, and a stream that did emit one would take
    the ordinary branch, which is what the SDK does for every other event.

    Like ``AsyncStream`` this exposes ``close`` and no ``aclose``, so
    ``maybe_await_aclose`` treats it exactly as it treats the SDK's stream and
    the response is still closed by the generator's own ``finally``.
    """

    __slots__ = ("_decoder", "_iterator", "response")

    def __init__(self, *, response: Any, decoder: Any) -> None:
        self.response = response
        self._decoder = decoder
        self._iterator = self.__stream__()

    async def __anext__(self) -> Any:
        return await self._iterator.__anext__()

    async def __aiter__(self) -> AsyncIterator[Any]:
        async for item in self._iterator:
            yield item

    async def __stream__(self) -> AsyncIterator[Any]:
        response = self.response
        try:
            async for sse in self._decoder.aiter_bytes(response.aiter_bytes()):
                if sse.data.startswith("[DONE]"):
                    break
                data = sse.json()
                if isinstance(data, Mapping) and data.get("error"):
                    raise _stream_api_error(data, response)
                yield build_chunk(data)
        finally:
            await response.aclose()

    async def __aenter__(self) -> MccChatChunkStream:
        return self

    async def __aexit__(self, exc_type: object, exc: object, exc_tb: object) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the response and release the connection."""
        await self.response.aclose()


def _stream_api_error(data: Mapping[Any, Any], response: Any) -> Exception:
    """Build the SDK's own error for an ``{"error": ...}`` frame."""
    # Imported here, not at module scope: this module is reachable from the
    # provider package, which must stay importable without paying the ~2 s
    # ``openai`` import before the server can answer ``/health``.
    from openai import APIError

    error = data.get("error")
    message = None
    if isinstance(error, Mapping):
        message = error.get("message")
    if not message or not isinstance(message, str):
        message = "An error occurred during streaming"
    return APIError(message=message, request=response.request, body=error)


def adopt_chat_stream(stream: Any) -> Any:
    """Re-read an SDK chat stream as MCC chunks, or leave it alone.

    Anything that is not an ``openai.AsyncStream`` -- every test double, and
    the async generator Mistral's normalizer wraps around one -- is returned
    untouched, so the only streams that change shape are the ones the SDK
    itself opened.

    The SDK stream's own iterator has not been started at this point (it is
    created in ``AsyncStream.__init__`` and nothing has awaited it yet), so
    taking its response over is not a partial read; no bytes have been
    consumed.
    """
    from openai import AsyncStream

    if not isinstance(stream, AsyncStream):
        return stream
    return MccChatChunkStream(response=stream.response, decoder=stream._decoder)
