"""Tool conversion helpers for the OpenAI Chat Completions adapter."""

import json
from collections.abc import Mapping
from typing import Any

from .errors import ChatCompletionsConversionError


def convert_tools(value: Any) -> list[dict[str, Any]] | None:
    """Convert ``tools`` into Anthropic tool definitions.

    Chat Completions has exactly one tool shape -- ``{"type": "function",
    "function": {...}}`` -- and, unlike Responses, no namespaces, so the
    Anthropic name is the client's name unchanged and every ``tool_call`` can
    be matched back by that name alone.
    """
    if value is None:
        return None
    if not isinstance(value, list):
        raise ChatCompletionsConversionError(
            "tools must be a list of function tool objects", param="tools"
        )

    tools: list[dict[str, Any]] = []
    for tool in value:
        if not isinstance(tool, dict):
            raise ChatCompletionsConversionError(
                f"Unsupported tool entry: {type(tool).__name__}", param="tools"
            )
        tool_type = tool.get("type", "function")
        if tool_type != "function":
            raise ChatCompletionsConversionError(
                f"Unsupported tool type: {tool_type!r}. "
                "MCC accepts function tools on /v1/chat/completions.",
                param="tools",
            )
        tools.append(_convert_function_tool(tool))
    return tools


def convert_tool_choice(value: Any, *, parallel_tool_calls: bool | None) -> Any:
    """Convert ``tool_choice`` plus ``parallel_tool_calls`` into Anthropic form.

    The two are one field upstream: Anthropic expresses "one tool call at a
    time" as ``disable_parallel_tool_use`` *inside* ``tool_choice``, so a
    client that sends ``parallel_tool_calls: false`` and no ``tool_choice``
    still needs a ``tool_choice`` object built for it.
    """
    choice = _base_tool_choice(value)
    if parallel_tool_calls is False:
        if choice is None:
            choice = {"type": "auto"}
        choice = {**choice, "disable_parallel_tool_use": True}
    return choice


def tool_choice_forbids_tools(value: Any) -> bool:
    """Whether the client asked for no tool to be callable at all."""
    return value == "none" or (
        isinstance(value, Mapping) and value.get("type") == "none"
    )


def parse_arguments(value: Any, *, tool_name: str) -> dict[str, Any]:
    """Decode a ``tool_calls[].function.arguments`` string into tool input."""
    if value is None or value == "":
        return {}
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        raise ChatCompletionsConversionError(
            f"tool_calls arguments for {tool_name!r} must be a JSON string",
            param="messages",
        )
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ChatCompletionsConversionError(
            f"tool_calls arguments for {tool_name!r} are invalid JSON: {exc.msg}",
            param="messages",
        ) from exc
    if not isinstance(parsed, dict):
        raise ChatCompletionsConversionError(
            f"tool_calls arguments for {tool_name!r} must decode to an object",
            param="messages",
        )
    return parsed


def _base_tool_choice(value: Any) -> dict[str, Any] | None:
    if value is None or value == "auto":
        return None
    if value == "none":
        return None
    if value == "required":
        return {"type": "any"}
    if isinstance(value, Mapping):
        choice_type = value.get("type")
        if choice_type == "none":
            return None
        if choice_type == "function":
            function = value.get("function")
            source = function if isinstance(function, Mapping) else value
            name = source.get("name")
            if not isinstance(name, str) or not name:
                raise ChatCompletionsConversionError(
                    "tool_choice.function.name must be a non-empty string",
                    param="tool_choice",
                )
            return {"type": "tool", "name": name}
        if choice_type in {"auto", "any", "required"}:
            return {"type": "any" if choice_type == "required" else choice_type}
    raise ChatCompletionsConversionError(
        f"Unsupported tool_choice: {value!r}", param="tool_choice"
    )


def _convert_function_tool(tool: Mapping[str, Any]) -> dict[str, Any]:
    function = tool.get("function")
    source = function if isinstance(function, Mapping) else tool
    name = source.get("name")
    if not isinstance(name, str) or not name:
        raise ChatCompletionsConversionError(
            "tools[].function.name must be a non-empty string", param="tools"
        )
    schema = source.get("parameters")
    if schema is None:
        schema = {"type": "object", "properties": {}}
    if not isinstance(schema, dict):
        raise ChatCompletionsConversionError(
            f"tools[].function.parameters for {name!r} must be an object",
            param="tools",
        )
    converted: dict[str, Any] = {"name": name, "input_schema": schema}
    description = source.get("description")
    if isinstance(description, str) and description:
        converted["description"] = description
    return converted
