"""Block finalization for OpenAI Responses streams."""

import json
from collections.abc import Callable

from ..carrier import is_carrier
from ..errors import ResponsesConversionError
from ..items import (
    encrypted_reasoning_item,
    message_item,
    reasoning_item,
    web_search_call_item,
)
from ..tools import (
    custom_tool_input_text_from_arguments,
    normalized_function_call_arguments,
)
from . import event_builders as events
from .blocks import (
    BlockState,
    ReasoningBlockState,
    TextBlockState,
    ToolBlockState,
    WebSearchBlockState,
)
from .ledger import ResponsesOutputLedger

InvalidFunctionCallHandler = Callable[
    [ToolBlockState, ResponsesConversionError], list[str]
]


class ResponseBlockCompleter:
    """Finalize active Responses output blocks."""

    def __init__(
        self,
        ledger: ResponsesOutputLedger,
        *,
        on_invalid_function_call: InvalidFunctionCallHandler,
    ) -> None:
        self._ledger = ledger
        self._on_invalid_function_call = on_invalid_function_call

    def complete_block(self, state: BlockState) -> list[str]:
        if isinstance(state, TextBlockState):
            return self._complete_text_block(state)
        if isinstance(state, ReasoningBlockState):
            return self._complete_reasoning_block(state)
        if isinstance(state, WebSearchBlockState):
            return self._complete_web_search_block(state)
        return self._complete_tool_block(state)

    def _complete_text_block(self, state: TextBlockState) -> list[str]:
        text = "".join(state.text_parts)
        annotations = state.annotations or None
        item = message_item(state.item_id, text, "completed", annotations=annotations)
        self._ledger.commit_output(state.output_index, item)
        return [
            events.output_text_done(state.item_id, state.output_index, text),
            events.content_part_done(
                state.item_id,
                state.output_index,
                text,
                annotations=annotations,
            ),
            events.output_item_done(state.output_index, item),
        ]

    def _complete_reasoning_block(self, state: ReasoningBlockState) -> list[str]:
        if state.encrypted_content is None and state.signature_parts:
            assembled_signature = "".join(state.signature_parts)
            if is_carrier(assembled_signature):
                state.encrypted_content = assembled_signature
        item = _reasoning_output_item(state, status="completed")
        self._ledger.commit_output(state.output_index, item)
        chunks: list[str] = []
        text = "".join(state.text_parts)
        if text:
            self._ledger.add_reasoning_text(text)
        if state.emit_summary:
            if state.summary_part_added:
                chunks.extend(
                    [
                        events.reasoning_summary_text_done(
                            state.item_id, state.output_index, text
                        ),
                        events.reasoning_summary_part_done(
                            state.item_id, state.output_index, text
                        ),
                    ]
                )
        elif text:
            chunks.append(
                events.reasoning_text_done(state.item_id, state.output_index, text)
            )
        chunks.append(events.output_item_done(state.output_index, item))
        return chunks

    def _complete_tool_block(self, state: ToolBlockState) -> list[str]:
        if state.kind == "custom":
            return self._complete_custom_tool_block(state)
        raw_arguments = "".join(state.argument_parts) or "{}"
        try:
            arguments = normalized_function_call_arguments(raw_arguments)
        except ResponsesConversionError as exc:
            return self._on_invalid_function_call(state, exc)
        item = tool_item(state, status="completed", arguments=arguments)
        self._ledger.commit_output(state.output_index, item)
        chunks: list[str] = []
        if arguments and not state.streamed_arguments:
            chunks.append(
                events.function_call_arguments_delta(
                    state.item_id, state.output_index, arguments
                )
            )
        chunks.extend(
            [
                events.function_call_arguments_done(
                    state.item_id, state.output_index, arguments
                ),
                events.output_item_done(state.output_index, item),
            ]
        )
        return chunks

    def _complete_web_search_block(self, state: WebSearchBlockState) -> list[str]:
        raw = "".join(state.argument_parts).strip()
        action: dict[str, object] = {"type": "search"}
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    query = parsed.get("query")
                    if isinstance(query, str) and query:
                        action["query"] = query
            except json.JSONDecodeError:
                pass
        item = web_search_call_item(state.item_id, "completed", action)
        self._ledger.commit_output(state.output_index, item)
        return [
            events.web_search_call_searching(state.item_id, state.output_index),
            events.web_search_call_completed(state.item_id, state.output_index),
            events.output_item_done(state.output_index, item),
        ]

    def _complete_custom_tool_block(self, state: ToolBlockState) -> list[str]:
        input_text = custom_tool_input_text_from_arguments(
            "".join(state.argument_parts)
        )
        item = tool_item(state, status="completed", input_text=input_text)
        self._ledger.commit_output(state.output_index, item)
        chunks: list[str] = []
        if input_text:
            chunks.append(
                events.custom_tool_call_input_delta(
                    state.item_id, state.output_index, input_text
                )
            )
        chunks.extend(
            [
                events.custom_tool_call_input_done(
                    state.item_id, state.output_index, input_text
                ),
                events.output_item_done(state.output_index, item),
            ]
        )
        return chunks


def tool_item(
    state: ToolBlockState,
    *,
    status: str,
    arguments: str = "",
    input_text: str = "",
) -> dict[str, object]:
    if state.kind == "custom":
        item: dict[str, object] = {
            "id": state.item_id,
            "type": "custom_tool_call",
            "status": status,
            "call_id": state.call_id,
            "name": state.name,
            "input": input_text,
        }
    else:
        item = {
            "id": state.item_id,
            "type": "function_call",
            "status": status,
            "call_id": state.call_id,
            "name": state.name,
            "arguments": arguments,
        }
    if state.namespace:
        item["namespace"] = state.namespace
    return item


def reasoning_output_item(
    state: ReasoningBlockState, *, status: str
) -> dict[str, object]:
    text = "".join(state.text_parts)
    if state.encrypted_content is not None:
        return encrypted_reasoning_item(
            state.item_id,
            state.encrypted_content,
            status,
            summary_text=text or None,
        )
    return reasoning_item(state.item_id, text, status, as_summary=state.emit_summary)


def _reasoning_output_item(
    state: ReasoningBlockState, *, status: str
) -> dict[str, object]:
    return reasoning_output_item(state, status=status)
