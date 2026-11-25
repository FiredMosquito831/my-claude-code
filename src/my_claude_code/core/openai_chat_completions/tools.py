"""Tool conversion helpers for the OpenAI Chat Completions adapter."""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .errors import ChatCompletionsConversionError

SUPPORTED_CHAT_WEB_SEARCH_TYPES: frozenset[str] = frozenset(
    {
        "web_search",
        "web_search_2025_08_26",
        "web_search_preview",
        "web_search_preview_2025_03_11",
    }
)
EXPECTED_CHAT_WEB_SEARCH_SCHEMA = (
    'Expected Chat Completions web search via top-level "web_search_options": '
    '{"search_context_size"?: "low" | "medium" | "high", '
    '"user_location"?: {"type": "approximate", "approximate": {"country"?: string, '
    '"city"?: string, "region"?: string, "timezone"?: string}}, '
    '"filters"?: {"allowed_domains": string[]}} '
    'or in tools[]: {"type": "web_search" | "web_search_2025_08_26" | '
    '"web_search_preview" | "web_search_preview_2025_03_11", '
    '"filters"?: {"allowed_domains": string[]}, "external_web_access"?: boolean, '
    '"search_context_size"?: "low" | "medium" | "high", "user_location"?: object}, '
    'with tool_choice "auto" or {"type": "web_search"}.'
)
_VALID_SEARCH_CONTEXT_SIZES = frozenset({"low", "medium", "high"})


@dataclass(frozen=True, slots=True)
class ChatWebSearchSpec:
    """Validated Chat Completions ``web_search_options`` / ``web_search*`` config."""

    allowed_domains: tuple[str, ...] = ()
    external_web_access: bool = True
    search_context_size: str | None = None
    user_location: dict[str, Any] | None = None
    forced: bool = False
    allowed_disjoint: bool = False


def _norm_domain(raw: str) -> str:
    return raw.strip().lower().rstrip(".")


def _domain_permits(candidate: str, allowed_rule: str) -> bool:
    return candidate == allowed_rule or candidate.endswith("." + allowed_rule)


def _intersect_domain_layers(
    layers: list[tuple[str, ...]],
) -> tuple[tuple[str, ...], bool]:
    if not layers:
        return (), False
    current = list(dict.fromkeys(_norm_domain(d) for d in layers[0] if d.strip()))
    if not current:
        return (), False
    for layer in layers[1:]:
        nxt = list(dict.fromkeys(_norm_domain(d) for d in layer if d.strip()))
        if not nxt:
            continue
        narrowed: list[str] = []
        for left in current:
            for right in nxt:
                if _domain_permits(left, right) and left not in narrowed:
                    narrowed.append(left)
                elif _domain_permits(right, left) and right not in narrowed:
                    narrowed.append(right)
        if not narrowed:
            return (), True
        current = narrowed
    return tuple(current), False


def _validate_domain_list(raw: Any, *, field_path: str, param: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or any(
        not isinstance(d, str) or not d.strip() for d in raw
    ):
        raise ChatCompletionsConversionError(
            f"Invalid {field_path!r}: expected a list of non-empty domain strings. "
            f"{EXPECTED_CHAT_WEB_SEARCH_SCHEMA}",
            param=param,
        )
    return tuple(dict.fromkeys(_norm_domain(d) for d in raw if d.strip()))


def _validate_user_location(
    raw_loc: Any, *, label: str, param: str
) -> dict[str, Any] | None:
    if raw_loc is None:
        return None
    if not isinstance(raw_loc, Mapping):
        raise ChatCompletionsConversionError(
            f"Invalid 'user_location' on {label}: expected a JSON object. "
            f"{EXPECTED_CHAT_WEB_SEARCH_SCHEMA}",
            param=param,
        )
    loc_type = raw_loc.get("type")
    if loc_type is not None and (
        not isinstance(loc_type, str) or loc_type.strip().lower() != "approximate"
    ):
        raise ChatCompletionsConversionError(
            f"Invalid 'user_location.type' on {label}: expected 'approximate'. "
            f"{EXPECTED_CHAT_WEB_SEARCH_SCHEMA}",
            param=param,
        )
    approx = raw_loc.get("approximate")
    if approx is not None:
        if not isinstance(approx, Mapping):
            raise ChatCompletionsConversionError(
                f"Invalid 'user_location.approximate' on {label}: expected an object. "
                f"{EXPECTED_CHAT_WEB_SEARCH_SCHEMA}",
                param=param,
            )
        for key in ("country", "city", "region", "timezone"):
            val = approx.get(key)
            if val is not None and not isinstance(val, str):
                raise ChatCompletionsConversionError(
                    f"Invalid 'user_location.approximate.{key}' on {label}: expected a string. "
                    f"{EXPECTED_CHAT_WEB_SEARCH_SCHEMA}",
                    param=param,
                )
    return {str(k): v for k, v in raw_loc.items()}


def _validate_chat_search_config(
    raw: Mapping[str, Any], *, label: str, param: str
) -> dict[str, Any]:
    domain_layers: list[tuple[str, ...]] = []
    filters = raw.get("filters")
    if filters is not None:
        if not isinstance(filters, Mapping):
            raise ChatCompletionsConversionError(
                f"Invalid 'filters' on {label}: expected an object. "
                f"{EXPECTED_CHAT_WEB_SEARCH_SCHEMA}",
                param=param,
            )
        raw_filter_domains = filters.get("allowed_domains")
        if raw_filter_domains is not None:
            domain_layers.append(
                _validate_domain_list(
                    raw_filter_domains,
                    field_path=f"{label}.filters.allowed_domains",
                    param=param,
                )
            )
    raw_top_domains = raw.get("allowed_domains")
    if raw_top_domains is not None:
        domain_layers.append(
            _validate_domain_list(
                raw_top_domains,
                field_path=f"{label}.allowed_domains",
                param=param,
            )
        )
    allowed_domains, allowed_disjoint = _intersect_domain_layers(
        [layer for layer in domain_layers if layer]
    )

    external_web_access = True
    raw_external = raw.get("external_web_access")
    if raw_external is not None:
        if not isinstance(raw_external, bool):
            raise ChatCompletionsConversionError(
                f"Invalid 'external_web_access' on {label}: expected a boolean. "
                f"{EXPECTED_CHAT_WEB_SEARCH_SCHEMA}",
                param=param,
            )
        external_web_access = raw_external

    search_context_size: str | None = None
    raw_size = raw.get("search_context_size")
    if raw_size is not None:
        if (
            not isinstance(raw_size, str)
            or raw_size.strip().lower() not in _VALID_SEARCH_CONTEXT_SIZES
        ):
            raise ChatCompletionsConversionError(
                f"Invalid 'search_context_size' on {label}: expected 'low', 'medium', or 'high'. "
                f"{EXPECTED_CHAT_WEB_SEARCH_SCHEMA}",
                param=param,
            )
        search_context_size = raw_size.strip().lower()

    user_location = _validate_user_location(
        raw.get("user_location"), label=label, param=param
    )

    return {
        "allowed_domains": allowed_domains,
        "allowed_disjoint": allowed_disjoint,
        "external_web_access": external_web_access,
        "search_context_size": search_context_size,
        "user_location": user_location,
    }


def extract_chat_web_search_spec(
    tools: Any,
    tool_choice: Any,
    web_search_options: Any = None,
    *,
    allow_web_search: bool,
) -> ChatWebSearchSpec | None:
    """Validate and extract Chat Completions web search config when enabled."""
    if not allow_web_search:
        return None

    entries: list[dict[str, Any]] = []
    if web_search_options is not None:
        if not isinstance(web_search_options, Mapping):
            raise ChatCompletionsConversionError(
                f"Invalid 'web_search_options': expected a JSON object, got "
                f"{type(web_search_options).__name__}. {EXPECTED_CHAT_WEB_SEARCH_SCHEMA}",
                param="web_search_options",
            )
        entries.append(
            _validate_chat_search_config(
                web_search_options,
                label="web_search_options",
                param="web_search_options",
            )
        )

    tool_list = tools if isinstance(tools, list) else []
    for tool in tool_list:
        if (
            isinstance(tool, Mapping)
            and tool.get("type") in SUPPORTED_CHAT_WEB_SEARCH_TYPES
        ):
            tool_type = str(tool.get("type") or "web_search")
            entries.append(
                _validate_chat_search_config(
                    tool,
                    label=f"{tool_type!r} tool",
                    param="tools",
                )
            )

    forced = False
    if (
        isinstance(tool_choice, Mapping)
        and tool_choice.get("type") in SUPPORTED_CHAT_WEB_SEARCH_TYPES
    ):
        if not entries:
            raise ChatCompletionsConversionError(
                f"Invalid tool_choice {tool_choice!r}: web search must be declared via "
                f"web_search_options or tools[]. {EXPECTED_CHAT_WEB_SEARCH_SCHEMA}",
                param="tool_choice",
            )
        forced = True

    if not entries:
        return None
    if tool_choice_forbids_tools(tool_choice):
        return None

    disjoint = any(entry["allowed_disjoint"] for entry in entries)
    domain_layers = [
        entry["allowed_domains"] for entry in entries if entry["allowed_domains"]
    ]
    if disjoint:
        allowed_domains: tuple[str, ...] = ()
    else:
        allowed_domains, disjoint = _intersect_domain_layers(domain_layers)
    external_web_access = all(entry["external_web_access"] for entry in entries)
    search_context_size = next(
        (
            entry["search_context_size"]
            for entry in reversed(entries)
            if entry["search_context_size"]
        ),
        None,
    )
    user_location = next(
        (
            entry["user_location"]
            for entry in reversed(entries)
            if entry["user_location"] is not None
        ),
        None,
    )
    return ChatWebSearchSpec(
        allowed_domains=allowed_domains,
        external_web_access=external_web_access,
        search_context_size=search_context_size,
        user_location=user_location,
        forced=forced,
        allowed_disjoint=disjoint,
    )


def convert_tools(
    value: Any,
    *,
    allow_web_search: bool = False,
) -> list[dict[str, Any]] | None:
    """Convert ``tools`` into Anthropic tool definitions.

    Chat Completions has ``{"type": "function", "function": {...}}`` tools, and
    when ``allow_web_search=True`` also validates hosted ``web_search*`` tools
    handled by MCC's local tool loop instead of rejecting them.
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
        if allow_web_search and tool_type in SUPPORTED_CHAT_WEB_SEARCH_TYPES:
            _validate_chat_search_config(
                tool,
                label=f"{tool_type!r} tool",
                param="tools",
            )
            continue
        if tool_type != "function":
            raise ChatCompletionsConversionError(
                f"Unsupported tool type: {tool_type!r}. "
                "MCC accepts function tools on /v1/chat/completions.",
                param="tools",
            )
        tools.append(_convert_function_tool(tool))
    return tools


def convert_tool_choice(
    value: Any,
    *,
    parallel_tool_calls: bool | None,
    allow_web_search: bool = False,
) -> Any:
    """Convert ``tool_choice`` plus ``parallel_tool_calls`` into Anthropic form.

    The two are one field upstream: Anthropic expresses "one tool call at a
    time" as ``disable_parallel_tool_use`` *inside* ``tool_choice``, so a
    client that sends ``parallel_tool_calls: false`` and no ``tool_choice``
    still needs a ``tool_choice`` object built for it.
    """
    choice = _base_tool_choice(value, allow_web_search=allow_web_search)
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


def _base_tool_choice(
    value: Any, *, allow_web_search: bool = False
) -> dict[str, Any] | None:
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
        if allow_web_search and choice_type in SUPPORTED_CHAT_WEB_SEARCH_TYPES:
            return {"type": "web_search"}
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
    strict = source.get("strict")
    if isinstance(strict, bool):
        converted["strict"] = strict
    return converted
