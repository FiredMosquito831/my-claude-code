"""Anthropic SSE to Gemini ``GenerateContentResponse`` chunk assembly."""

import json
from collections.abc import Mapping
from typing import Any

from my_claude_code.core.failures import ExecutionFailure
from my_claude_code.core.openai_common import AnthropicSseEvent

from .errors import gemini_error_from_failure
from .events import finish_reason_for, format_gemini_sse_data
from .usage import GeminiUsageLedger


class GeminiStreamAssembler:
    """Assemble Gemini stream chunks from Anthropic SSE events.

    One structural difference from both OpenAI adapters drives the whole
    class: **Gemini does not stream function arguments.** Anthropic sends a
    ``tool_use`` block start and then a run of ``input_json_delta`` fragments,
    while a Gemini ``functionCall`` part is whole or absent -- there is no
    partial form in the protocol and every client ``JSON.parse``s ``args`` on
    arrival. So a tool call is buffered until its ``content_block_stop`` and
    emitted once, complete. Two interleaved calls therefore appear in the order
    they *finished*, which is the order Anthropic closes them in.

    Thought parts are the second difference. They are emitted only when the
    client sent ``thinkingConfig.includeThoughts: true``: Gemini's own default
    is false, and a client that did not ask renders a ``thought`` part it did
    not expect as ordinary answer text.
    """

    def __init__(self, model: str, *, include_thoughts: bool) -> None:
        self._model = model
        self._include_thoughts = include_thoughts
        self._response_id = ""
        self._usage = GeminiUsageLedger()
        self._pending_tools: dict[int, dict[str, Any]] = {}
        self._stop_reason: Any = None
        self.terminal = False

    def bind_response_id(self, response_id: str) -> None:
        """Adopt the id the handler minted, so the log and the wire agree."""

        self._response_id = response_id

    def process_anthropic_event(self, event: AnthropicSseEvent) -> list[str]:
        if self.terminal:
            return []
        if event.event == "message_start":
            self._usage.record_message_start(event.data)
            return []
        if event.event == "content_block_start":
            return self._handle_block_start(event.data)
        if event.event == "content_block_delta":
            return self._handle_block_delta(event.data)
        if event.event == "content_block_stop":
            return self._handle_block_stop(event.data)
        if event.event == "message_delta":
            self._usage.record_usage_delta(event.data)
            delta = event.data.get("delta")
            if isinstance(delta, Mapping) and delta.get("stop_reason") is not None:
                self._stop_reason = delta.get("stop_reason")
            return []
        if event.event == "message_stop":
            return self.complete()
        if event.event == "error":
            return self.fail(_error_object(event.data))
        return []

    def finish_if_needed(self) -> list[str]:
        if self.terminal:
            return []
        return self.complete()

    def complete(self) -> list[str]:
        """Emit any unclosed tool call, then the finish chunk."""

        if self.terminal:
            return []
        self.terminal = True
        chunks = self._flush_pending_tools()
        chunks.append(
            self._chunk([], finish_reason=finish_reason_for(self._stop_reason))
        )
        return chunks

    def fail_execution(self, failure: ExecutionFailure) -> list[str]:
        """Finish a started stream with a canonical execution failure."""

        return self.fail(gemini_error_from_failure(failure))

    def fail(self, error: dict[str, Any]) -> list[str]:
        """Finish a started stream with a Gemini error frame.

        Google publishes no in-band error convention for ``alt=sse``, so the
        frame carries both halves of the truth: the ``error`` envelope a client
        that looks for one will read, and a candidate whose ``finishReason`` is
        ``OTHER`` so that a client which only reads candidates sees an abnormal
        end rather than a clean ``STOP``. A stream that simply stopped would
        read as a successful, empty answer, which is the worst of the available
        lies.
        """

        if self.terminal:
            return []
        self.terminal = True
        payload = self._envelope()
        payload["candidates"] = [
            {
                "content": {"role": "model", "parts": []},
                "index": 0,
                "finishReason": "OTHER",
            }
        ]
        payload["error"] = error
        return [format_gemini_sse_data(payload)]

    def _handle_block_start(self, data: Mapping[str, Any]) -> list[str]:
        block = data.get("content_block")
        if not isinstance(block, Mapping):
            return []
        index = _event_index(data)
        block_type = block.get("type")
        if block_type == "text":
            if text := _text(block.get("text")):
                return [self._chunk([{"text": text}])]
            return []
        if block_type == "thinking":
            return self._thought_chunks(_text(block.get("thinking")))
        if block_type == "tool_use" and index is not None:
            self._pending_tools[index] = {
                "name": _text(block.get("name")),
                "arguments": "",
            }
        return []

    def _handle_block_delta(self, data: Mapping[str, Any]) -> list[str]:
        delta = data.get("delta")
        if not isinstance(delta, Mapping):
            return []
        delta_type = delta.get("type")
        if delta_type == "text_delta":
            text = _text(delta.get("text"))
            return [self._chunk([{"text": text}])] if text else []
        if delta_type == "thinking_delta":
            return self._thought_chunks(_text(delta.get("thinking")))
        if delta_type == "input_json_delta":
            index = _event_index(data)
            pending = self._pending_tools.get(index) if index is not None else None
            if pending is not None:
                pending["arguments"] += _text(delta.get("partial_json"))
        return []

    def _handle_block_stop(self, data: Mapping[str, Any]) -> list[str]:
        index = _event_index(data)
        if index is None:
            return []
        pending = self._pending_tools.pop(index, None)
        if pending is None:
            return []
        return [self._chunk([_function_call_part(pending)])]

    def _flush_pending_tools(self) -> list[str]:
        """Emit any tool call whose block never closed.

        A provider that drops the connection after the last argument fragment
        would otherwise leave the client with a turn that called nothing, which
        looks like the model declining rather than the stream ending early.
        """

        chunks = [
            self._chunk([_function_call_part(pending)])
            for _, pending in sorted(self._pending_tools.items())
        ]
        self._pending_tools.clear()
        return chunks

    def _thought_chunks(self, text: str) -> list[str]:
        if not text:
            return []
        self._usage.add_thought_text(text)
        if not self._include_thoughts:
            return []
        return [self._chunk([{"text": text, "thought": True}])]

    def _chunk(
        self, parts: list[dict[str, Any]], *, finish_reason: str | None = None
    ) -> str:
        payload = self._envelope()
        candidate: dict[str, Any] = {
            "content": {"role": "model", "parts": parts},
            "index": 0,
        }
        if finish_reason is not None:
            candidate["finishReason"] = finish_reason
        payload["candidates"] = [candidate]
        return format_gemini_sse_data(payload)

    def _envelope(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "responseId": self._response_id,
            "modelVersion": self._model,
        }
        if self._usage.has_counts():
            # Google repeats ``usageMetadata`` on every chunk of a stream and
            # clients read the last one; repeating it keeps a client that
            # closes the stream early from having no counts at all.
            payload["usageMetadata"] = self._usage.payload()
        return payload


def _function_call_part(pending: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "functionCall": {
            "name": str(pending.get("name", "")),
            "args": _parse_arguments(pending.get("arguments")),
        }
    }


def _parse_arguments(value: Any) -> dict[str, Any]:
    """Decode buffered ``input_json_delta`` text into a Gemini ``args`` object.

    A truncated stream leaves invalid JSON. An empty object is the only safe
    answer -- a client that would have ``JSON.parse``d a fragment throws, and a
    string in ``args`` is not the type the protocol declares.
    """

    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _error_object(data: Mapping[str, Any]) -> dict[str, Any]:
    error = data.get("error")
    if not isinstance(error, Mapping):
        error = {"type": "api_error", "message": str(data)}
    return {
        "code": 500,
        "message": str(error.get("message", "")),
        "status": "INTERNAL",
    }


def _event_index(data: Mapping[str, Any]) -> int | None:
    value = data.get("index")
    return value if isinstance(value, int) else None


def _text(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)
