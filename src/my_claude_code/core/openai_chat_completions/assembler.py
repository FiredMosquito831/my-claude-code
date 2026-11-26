"""Anthropic SSE to OpenAI Chat Completions chunk assembly."""

import json
import time
from collections.abc import Mapping
from typing import Any

from my_claude_code.core.failures import ExecutionFailure
from my_claude_code.core.openai_common import (
    AnthropicSseEvent,
    openai_error_from_anthropic_error,
    openai_error_from_failure,
)
from my_claude_code.core.trace import trace_event

from .events import DONE_FRAME, finish_reason_for, format_chat_sse_data
from .ids import new_tool_call_id
from .models import OpenAIChatCompletionRequest
from .usage import ChatUsageLedger


class ChatCompletionsStreamAssembler:
    """Assemble ``chat.completion.chunk`` frames from Anthropic SSE events.

    Anthropic indexes content blocks and OpenAI indexes *tool calls only*, so
    the two numbering schemes are not the same and must not be conflated: a
    reply whose first block is text and whose second and third are tool calls
    is tool-call index 0 and 1 to a client, while Anthropic calls them blocks 1
    and 2. The map from one to the other is the whole reason this class holds
    state rather than translating event by event.
    """

    def __init__(self, request: OpenAIChatCompletionRequest) -> None:
        self._model = request.model
        self._include_usage = request.wants_usage
        self._completion_id = ""
        self._created = int(time.time())
        self._usage = ChatUsageLedger()
        self._tool_call_index: dict[int, int] = {}
        self._pending_seeded_tools: dict[int, dict[str, Any]] = {}
        self._next_tool_call_index = 0
        self._emitted_tool_calls = False
        self._role_emitted = False
        self._stop_reason: Any = None
        self.terminal = False

    def bind_completion_id(self, completion_id: str) -> None:
        """Adopt the id the handler minted, so the log and the wire agree."""
        self._completion_id = completion_id

    def process_anthropic_event(self, event: AnthropicSseEvent) -> list[str]:
        if self.terminal:
            return []
        if event.event == "message_start":
            self._usage.record_message_start(event.data)
            return self._ensure_role_chunk()
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
            return self.fail(openai_error_from_anthropic_error(event.data))
        return []

    def finish_if_needed(self) -> list[str]:
        if self.terminal:
            return []
        return self.complete()

    def complete(self) -> list[str]:
        """Emit the finish chunk, the optional usage chunk, and ``[DONE]``."""
        if self.terminal:
            return []
        self.terminal = True
        chunks = self._ensure_role_chunk()
        for _index, pending in sorted(
            self._pending_seeded_tools.items(),
            key=lambda item: int(item[1]["call_index"]),
        ):
            chunks.append(
                self._tool_call_opener_chunk(
                    pending["call_index"],
                    pending["id"],
                    pending["name"],
                    pending["arguments"],
                )
            )
        self._pending_seeded_tools.clear()
        chunks.append(
            self._chunk(
                {},
                finish_reason=finish_reason_for(
                    self._stop_reason, emitted_tool_calls=self._emitted_tool_calls
                ),
            )
        )
        chunks.extend(self._usage_chunks())
        chunks.append(DONE_FRAME)
        return chunks

    def fail_execution(self, failure: ExecutionFailure) -> list[str]:
        """Finish a started stream with a canonical execution failure."""
        return self.fail(openai_error_from_failure(failure))

    def fail(self, error: dict[str, Any]) -> list[str]:
        """Finish a started stream with an OpenAI error frame then ``[DONE]``.

        An error object inside the SSE body is what the OpenAI SDKs look for
        on a stream that has already returned 200: their stream readers raise
        on it. A bare ``[DONE]`` would instead read as a successful, empty
        answer, which is the worst of the available lies.
        """
        if self.terminal:
            return []
        self.terminal = True
        return [format_chat_sse_data({"error": error}), DONE_FRAME]

    def _ensure_role_chunk(self) -> list[str]:
        if self._role_emitted:
            return []
        self._role_emitted = True
        return [self._chunk({"role": "assistant", "content": ""})]

    def _handle_block_start(self, data: Mapping[str, Any]) -> list[str]:
        block = data.get("content_block")
        if not isinstance(block, Mapping):
            return []
        index = _event_index(data)
        block_type = block.get("type")
        if block_type == "text":
            chunks = self._ensure_role_chunk()
            if text := _text(block.get("text")):
                chunks.append(self._chunk({"content": text}))
            return chunks
        if block_type == "thinking":
            chunks = self._ensure_role_chunk()
            if text := _text(block.get("thinking")):
                chunks.append(self._reasoning_chunk(text))
            return chunks
        if block_type == "tool_use" and index is not None:
            return self._start_tool_call(index, block)
        if block_type == "server_tool_use":
            trace_event(
                stage="chat_completions",
                event="chat_completions.output.unsupported_block_ignored",
                source="openai_chat_completions",
                block_type="server_tool_use",
                tool_name=_text(block.get("name")),
            )
            return []
        return []

    def _handle_block_delta(self, data: Mapping[str, Any]) -> list[str]:
        delta = data.get("delta")
        if not isinstance(delta, Mapping):
            return []
        delta_type = delta.get("type")
        if delta_type == "text_delta":
            text = _text(delta.get("text"))
            if not text:
                return []
            return [*self._ensure_role_chunk(), self._chunk({"content": text})]
        if delta_type == "thinking_delta":
            text = _text(delta.get("thinking"))
            if not text:
                return []
            return [*self._ensure_role_chunk(), self._reasoning_chunk(text)]
        if delta_type == "input_json_delta":
            index = _event_index(data)
            if index is None or index not in self._tool_call_index:
                return []
            fragment = _text(delta.get("partial_json"))
            if not fragment:
                return []
            chunks: list[str] = []
            pending = self._pending_seeded_tools.pop(index, None)
            if pending is not None:
                chunks.extend(self._ensure_role_chunk())
                chunks.append(
                    self._tool_call_opener_chunk(
                        pending["call_index"],
                        pending["id"],
                        pending["name"],
                        "",
                    )
                )
            chunks.append(
                self._chunk(
                    {
                        "tool_calls": [
                            {
                                "index": self._tool_call_index[index],
                                "function": {"arguments": fragment},
                            }
                        ]
                    }
                )
            )
            return chunks
        return []

    def _handle_block_stop(self, data: Mapping[str, Any]) -> list[str]:
        index = _event_index(data)
        if index is None:
            return []
        pending = self._pending_seeded_tools.pop(index, None)
        if pending is None:
            return []
        return [
            *self._ensure_role_chunk(),
            self._tool_call_opener_chunk(
                pending["call_index"],
                pending["id"],
                pending["name"],
                pending["arguments"],
            ),
        ]

    def _start_tool_call(self, index: int, block: Mapping[str, Any]) -> list[str]:
        chunks = self._ensure_role_chunk()
        call_index = self._next_tool_call_index
        self._next_tool_call_index += 1
        self._tool_call_index[index] = call_index
        self._emitted_tool_calls = True
        tool_id = _text(block.get("id")) or new_tool_call_id()
        name = _text(block.get("name"))
        initial_input = block.get("input")
        if isinstance(initial_input, Mapping) and initial_input:
            # Hold seeded arguments until block stop so subsequent input_json_delta
            # chunks replace the fallback instead of concatenating onto it.
            self._pending_seeded_tools[index] = {
                "call_index": call_index,
                "id": tool_id,
                "name": name,
                "arguments": json.dumps(initial_input, separators=(",", ":")),
            }
            return chunks
        chunks.append(self._tool_call_opener_chunk(call_index, tool_id, name, ""))
        return chunks

    def _tool_call_opener_chunk(
        self, call_index: int, tool_id: str, name: str, arguments: str
    ) -> str:
        return self._chunk(
            {
                "tool_calls": [
                    {
                        "index": call_index,
                        "id": tool_id,
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": arguments,
                        },
                    }
                ]
            }
        )

    def _reasoning_chunk(self, text: str) -> str:
        self._usage.add_reasoning_text(text)
        # ``reasoning_content`` is the field every OpenAI-compatible client
        # that shows thinking actually reads; ``reasoning`` is carried beside
        # it because a second family of clients reads that one instead, and
        # the duplication costs one key.
        return self._chunk({"reasoning_content": text, "reasoning": text})

    def _usage_chunks(self) -> list[str]:
        if not self._include_usage or not self._usage.has_counts():
            return []
        payload = self._envelope()
        # The usage chunk carries no choice: that is what tells a client this
        # frame is the accounting and not another token.
        payload["choices"] = []
        payload["usage"] = self._usage.payload()
        return [format_chat_sse_data(payload)]

    def _chunk(self, delta: dict[str, Any], *, finish_reason: str | None = None) -> str:
        payload = self._envelope()
        payload["choices"] = [
            {"index": 0, "delta": delta, "finish_reason": finish_reason}
        ]
        return format_chat_sse_data(payload)

    def _envelope(self) -> dict[str, Any]:
        return {
            "id": self._completion_id,
            "object": "chat.completion.chunk",
            "created": self._created,
            "model": self._model,
        }


def _event_index(data: Mapping[str, Any]) -> int | None:
    value = data.get("index")
    return value if isinstance(value, int) else None


def _text(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)
