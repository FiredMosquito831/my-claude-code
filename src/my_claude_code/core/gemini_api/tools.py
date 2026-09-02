"""Tool conversion for the Gemini ingress adapter.

Gemini nests its declarations one level deeper than either OpenAI surface --
``tools`` is a list of *tool blocks*, each of which may carry a
``functionDeclarations`` array -- and mixes MCC-servable function tools with
Google-hosted ones (``googleSearch``, ``urlContext``, ``codeExecution``) inside
the same array. Rejecting a request over a hosted tool would make Gemini CLI
unusable the moment its user enables web search, so a hosted block is dropped
and the drop is reported to the caller, which traces it.
"""

from collections.abc import Mapping
from typing import Any

from .errors import GeminiConversionError

#: Tool-block keys that name a Google-hosted tool this proxy cannot run.
#: Dropped rather than refused, and named in the trace event so a user can see
#: why the model never searched.
HOSTED_TOOL_KEYS: tuple[str, ...] = (
    "googleSearch",
    "google_search",
    "googleSearchRetrieval",
    "google_search_retrieval",
    "urlContext",
    "url_context",
    "codeExecution",
    "code_execution",
    "computerUse",
    "computer_use",
    "googleMaps",
    "google_maps",
)

_FUNCTION_DECLARATION_KEYS = ("functionDeclarations", "function_declarations")
_SCHEMA_KEYS = ("parametersJsonSchema", "parameters_json_schema", "parameters")


def convert_tools(value: Any) -> tuple[list[dict[str, Any]] | None, list[str]]:
    """Convert Gemini ``tools`` into Anthropic tool definitions.

    Returns ``(tools, dropped)``. ``dropped`` names every hosted tool block
    that was discarded, so the handler can trace exactly what the model was
    not given.
    """

    if value is None:
        return None, []
    if not isinstance(value, list):
        raise GeminiConversionError(
            "tools must be a list of tool blocks", field="tools"
        )

    tools: list[dict[str, Any]] = []
    dropped: list[str] = []
    for block in value:
        if not isinstance(block, Mapping):
            raise GeminiConversionError(
                f"Unsupported tools entry: {type(block).__name__}", field="tools"
            )
        declarations = _declarations(block)
        if declarations is not None:
            tools.extend(_convert_declaration(entry) for entry in declarations)
        dropped.extend(key for key in HOSTED_TOOL_KEYS if key in block)
    return (tools or None), dropped


def convert_tool_config(tool_config: Any, *, tool_names: set[str]) -> tuple[Any, bool]:
    """Convert ``toolConfig.functionCallingConfig`` into Anthropic form.

    Returns ``(tool_choice, forbids_tools)``. Google's four modes are:

    ``AUTO``       the model decides -- Anthropic's default, so no key.
    ``ANY``        it must call a function; with exactly one entry in
                   ``allowedFunctionNames`` that is Anthropic's
                   ``{"type": "tool", "name": ...}``, and otherwise
                   ``{"type": "any"}``.
    ``NONE``       no function may be called: the tools are withheld entirely,
                   which is what ``tool_choice_forbids_tools`` expresses on the
                   Chat Completions surface too.
    ``VALIDATED``  the model decides but its call is schema-checked upstream.
                   MCC has no such validator, so it reads as ``AUTO`` -- the
                   honest translation, since the alternative is claiming a
                   guarantee nothing here enforces.

    ``allowedFunctionNames`` with more than one entry has no Anthropic
    equivalent, so it becomes ``{"type": "any"}`` and the restriction is lost.
    That is stated rather than hidden: the caller traces it.
    """

    if tool_config is None:
        return None, False
    config = getattr(tool_config, "function_calling_config", None)
    if config is None:
        return None, False
    mode = config.mode
    normalized = mode.strip().upper() if isinstance(mode, str) else ""
    allowed = [
        name
        for name in (config.allowed_function_names or ())
        if isinstance(name, str) and name
    ]

    if normalized == "NONE":
        return None, True
    if normalized == "ANY":
        named = [name for name in allowed if name in tool_names]
        if len(named) == 1:
            return {"type": "tool", "name": named[0]}, False
        return {"type": "any"}, False
    if normalized in {"", "AUTO", "MODE_UNSPECIFIED", "VALIDATED"}:
        return None, False
    raise GeminiConversionError(
        f"Unsupported functionCallingConfig.mode: {mode!r}. "
        "MCC accepts AUTO, ANY, NONE and VALIDATED.",
        field="toolConfig",
    )


def _declarations(block: Mapping[str, Any]) -> list[Any] | None:
    for key in _FUNCTION_DECLARATION_KEYS:
        if key not in block:
            continue
        value = block[key]
        if not isinstance(value, list):
            raise GeminiConversionError(f"tools[].{key} must be a list", field="tools")
        return value
    return None


def _convert_declaration(entry: Any) -> dict[str, Any]:
    if not isinstance(entry, Mapping):
        raise GeminiConversionError(
            f"Unsupported functionDeclarations entry: {type(entry).__name__}",
            field="tools",
        )
    name = entry.get("name")
    if not isinstance(name, str) or not name:
        raise GeminiConversionError(
            "functionDeclarations[].name must be a non-empty string", field="tools"
        )
    converted: dict[str, Any] = {"name": name, "input_schema": _schema(entry, name)}
    description = entry.get("description")
    if isinstance(description, str) and description:
        converted["description"] = description
    return converted


def _schema(entry: Mapping[str, Any], name: str) -> dict[str, Any]:
    """Return the declaration's parameter schema in JSON Schema form.

    ``parametersJsonSchema`` is already JSON Schema. ``parameters`` is
    Google's own ``Schema`` proto, whose JSON encoding is JSON Schema with
    ``type`` spelled in upper case (``"STRING"``, ``"OBJECT"``); lowering it is
    the whole difference between a tool an Anthropic upstream accepts and one
    it rejects with a schema error naming no field.
    """

    for key in _SCHEMA_KEYS:
        if key not in entry:
            continue
        schema = entry[key]
        if schema is None:
            break
        if not isinstance(schema, Mapping):
            raise GeminiConversionError(
                f"functionDeclarations[].{key} for {name!r} must be an object",
                field="tools",
            )
        return normalize_schema(schema)
    return {"type": "object", "properties": {}}


def normalize_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Lower-case every ``type`` in a Google ``Schema``-shaped mapping.

    Applied recursively through ``properties``, ``items`` and the three
    composition keywords. A schema that already uses lower case -- which is
    what ``parametersJsonSchema`` carries -- passes through unchanged, so this
    is safe to run over either spelling.
    """

    result: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "type" and isinstance(value, str):
            result[key] = value.lower()
        elif key == "properties" and isinstance(value, Mapping):
            result[key] = {
                property_name: normalize_schema(property_schema)
                if isinstance(property_schema, Mapping)
                else property_schema
                for property_name, property_schema in value.items()
            }
        elif key == "items" and isinstance(value, Mapping):
            result[key] = normalize_schema(value)
        elif key in {"anyOf", "any_of", "oneOf", "allOf"} and isinstance(value, list):
            result[key] = [
                normalize_schema(item) if isinstance(item, Mapping) else item
                for item in value
            ]
        else:
            result[key] = value
    return result
