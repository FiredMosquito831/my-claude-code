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
from dataclasses import dataclass
from typing import Any

from .errors import GeminiConversionError

#: Search-family hosted tool keys handled by MCC's local web search loop when enabled.
GEMINI_SEARCH_TOOL_KEYS: tuple[str, ...] = (
    "googleSearch",
    "google_search",
    "googleSearchRetrieval",
    "google_search_retrieval",
)

#: URL-context hosted tool keys handled by MCC's local web fetch loop when enabled.
GEMINI_URL_CONTEXT_TOOL_KEYS: tuple[str, ...] = (
    "urlContext",
    "url_context",
)

GEMINI_WEB_TOOL_KEYS: frozenset[str] = frozenset(
    (*GEMINI_SEARCH_TOOL_KEYS, *GEMINI_URL_CONTEXT_TOOL_KEYS)
)

#: Non-web Google-hosted tool keys still dropped and traced even when web search is enabled.
NON_WEB_HOSTED_TOOL_KEYS: tuple[str, ...] = (
    "codeExecution",
    "code_execution",
    "computerUse",
    "computer_use",
    "googleMaps",
    "google_maps",
)

#: Tool-block keys that name a Google-hosted tool this proxy cannot run on the legacy path.
#: Dropped rather than refused, and named in the trace event so a user can see
#: why the model never searched.
HOSTED_TOOL_KEYS: tuple[str, ...] = (
    *GEMINI_SEARCH_TOOL_KEYS,
    *GEMINI_URL_CONTEXT_TOOL_KEYS,
    *NON_WEB_HOSTED_TOOL_KEYS,
)

EXPECTED_GEMINI_WEB_TOOLS_SCHEMA = (
    'Expected Gemini web tools in tools[]: {"googleSearch": {"excludeDomains"?: string[]}} '
    '(or "google_search" / "googleSearchRetrieval" with optional {"dynamicRetrievalConfig": '
    '{"mode"?: "MODE_DYNAMIC" | "MODE_UNSPECIFIED", "dynamicThreshold"?: number}}) '
    'and/or {"urlContext": {}} (or "url_context").'
)

_FUNCTION_DECLARATION_KEYS = ("functionDeclarations", "function_declarations")
_SCHEMA_KEYS = ("parametersJsonSchema", "parameters_json_schema", "parameters")
_VALID_DYNAMIC_MODES = frozenset({"MODE_DYNAMIC", "MODE_UNSPECIFIED"})


@dataclass(frozen=True, slots=True)
class GeminiWebSearchSpec:
    """Validated Gemini ``googleSearch`` and/or ``urlContext`` configuration."""

    enable_search: bool = False
    enable_fetch: bool = False
    blocked_domains: tuple[str, ...] = ()
    dynamic_threshold: float | None = None


def _norm_domain(raw: str) -> str:
    return raw.strip().lower().rstrip(".")


def _validate_search_block(
    key: str, raw_cfg: Any
) -> tuple[tuple[str, ...], float | None]:
    if raw_cfg is None:
        return (), None
    if not isinstance(raw_cfg, Mapping):
        raise GeminiConversionError(
            f"Invalid {key!r} tool configuration: expected an object, got "
            f"{type(raw_cfg).__name__}. {EXPECTED_GEMINI_WEB_TOOLS_SCHEMA}",
            field="tools",
        )

    blocked: tuple[str, ...] = ()
    if "excludeDomains" in raw_cfg or "exclude_domains" in raw_cfg:
        raw_exclude = (
            raw_cfg["excludeDomains"]
            if "excludeDomains" in raw_cfg
            else raw_cfg.get("exclude_domains")
        )
        if raw_exclude is not None:
            if not isinstance(raw_exclude, list) or any(
                not isinstance(d, str) or not d.strip() for d in raw_exclude
            ):
                raise GeminiConversionError(
                    f"Invalid '{key}.excludeDomains': expected a list of non-empty domain strings. "
                    f"{EXPECTED_GEMINI_WEB_TOOLS_SCHEMA}",
                    field="tools",
                )
            blocked = tuple(
                dict.fromkeys(_norm_domain(d) for d in raw_exclude if d.strip())
            )

    dynamic_threshold: float | None = None
    if "dynamicRetrievalConfig" in raw_cfg or "dynamic_retrieval_config" in raw_cfg:
        raw_dyn = (
            raw_cfg["dynamicRetrievalConfig"]
            if "dynamicRetrievalConfig" in raw_cfg
            else raw_cfg.get("dynamic_retrieval_config")
        )
        if raw_dyn is not None:
            if not isinstance(raw_dyn, Mapping):
                raise GeminiConversionError(
                    f"Invalid '{key}.dynamicRetrievalConfig': expected an object, got "
                    f"{type(raw_dyn).__name__}. {EXPECTED_GEMINI_WEB_TOOLS_SCHEMA}",
                    field="tools",
                )
            if "mode" in raw_dyn and raw_dyn["mode"] is not None:
                mode = raw_dyn["mode"]
                if (
                    not isinstance(mode, str)
                    or mode.strip().upper() not in _VALID_DYNAMIC_MODES
                ):
                    raise GeminiConversionError(
                        f"Invalid '{key}.dynamicRetrievalConfig.mode': expected 'MODE_DYNAMIC' "
                        f"or 'MODE_UNSPECIFIED'. {EXPECTED_GEMINI_WEB_TOOLS_SCHEMA}",
                        field="tools",
                    )
            if "dynamicThreshold" in raw_dyn or "dynamic_threshold" in raw_dyn:
                raw_thresh = (
                    raw_dyn["dynamicThreshold"]
                    if "dynamicThreshold" in raw_dyn
                    else raw_dyn.get("dynamic_threshold")
                )
                if raw_thresh is not None:
                    if (
                        isinstance(raw_thresh, bool)
                        or not isinstance(raw_thresh, (int, float))
                        or not (0.0 <= float(raw_thresh) <= 1.0)
                    ):
                        raise GeminiConversionError(
                            f"Invalid '{key}.dynamicRetrievalConfig.dynamicThreshold': expected a "
                            f"number between 0.0 and 1.0. {EXPECTED_GEMINI_WEB_TOOLS_SCHEMA}",
                            field="tools",
                        )
                    dynamic_threshold = float(raw_thresh)

    if "timeRangeFilter" in raw_cfg or "time_range_filter" in raw_cfg:
        raw_tr = (
            raw_cfg["timeRangeFilter"]
            if "timeRangeFilter" in raw_cfg
            else raw_cfg.get("time_range_filter")
        )
        if raw_tr is not None and not isinstance(raw_tr, Mapping):
            raise GeminiConversionError(
                f"Invalid '{key}.timeRangeFilter': expected an object, got "
                f"{type(raw_tr).__name__}. {EXPECTED_GEMINI_WEB_TOOLS_SCHEMA}",
                field="tools",
            )

    return blocked, dynamic_threshold


def _validate_url_context_block(key: str, raw_cfg: Any) -> None:
    if raw_cfg is not None and not isinstance(raw_cfg, Mapping):
        raise GeminiConversionError(
            f"Invalid {key!r} tool configuration: expected an object, got "
            f"{type(raw_cfg).__name__}. {EXPECTED_GEMINI_WEB_TOOLS_SCHEMA}",
            field="tools",
        )


def _tool_config_forbids_tools(tool_config: Any) -> bool:
    if tool_config is None:
        return False
    if isinstance(tool_config, Mapping):
        fcc = tool_config.get("functionCallingConfig") or tool_config.get(
            "function_calling_config"
        )
    else:
        fcc = getattr(tool_config, "function_calling_config", None)
    if fcc is None:
        return False
    mode = fcc.get("mode") if isinstance(fcc, Mapping) else getattr(fcc, "mode", None)
    return isinstance(mode, str) and mode.strip().upper() == "NONE"


def extract_gemini_web_search_spec(
    tools: Any,
    tool_config: Any = None,
    *,
    allow_web_search: bool,
) -> GeminiWebSearchSpec | None:
    """Validate and extract Gemini ``googleSearch``/``urlContext`` spec when enabled."""
    if not allow_web_search or tools is None:
        return None
    if not isinstance(tools, list):
        raise GeminiConversionError(
            "tools must be a list of tool blocks", field="tools"
        )

    enable_search = False
    enable_fetch = False
    blocked_domains: list[str] = []
    dynamic_threshold: float | None = None

    for block in tools:
        if not isinstance(block, Mapping):
            raise GeminiConversionError(
                f"Unsupported tools entry: {type(block).__name__}", field="tools"
            )
        for key in GEMINI_SEARCH_TOOL_KEYS:
            if key in block:
                enable_search = True
                blocked, thresh = _validate_search_block(key, block[key])
                for dom in blocked:
                    if dom not in blocked_domains:
                        blocked_domains.append(dom)
                if thresh is not None:
                    dynamic_threshold = thresh
        for key in GEMINI_URL_CONTEXT_TOOL_KEYS:
            if key in block:
                enable_fetch = True
                _validate_url_context_block(key, block[key])

    if not enable_search and not enable_fetch:
        return None
    if _tool_config_forbids_tools(tool_config):
        return None

    return GeminiWebSearchSpec(
        enable_search=enable_search,
        enable_fetch=enable_fetch,
        blocked_domains=tuple(blocked_domains),
        dynamic_threshold=dynamic_threshold,
    )


def convert_tools(
    value: Any,
    *,
    allow_web_search: bool = False,
) -> tuple[list[dict[str, Any]] | None, list[str]]:
    """Convert Gemini ``tools`` into Anthropic tool definitions.

    Returns ``(tools, dropped)``. ``dropped`` names every hosted tool block
    that was discarded, so the handler can trace exactly what the model was
    not given. When ``allow_web_search=True``, supported ``googleSearch*`` and
    ``urlContext*`` declarations are validated instead of being added to
    ``dropped``.
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
        if allow_web_search:
            for key in GEMINI_SEARCH_TOOL_KEYS:
                if key in block:
                    _validate_search_block(key, block[key])
            for key in GEMINI_URL_CONTEXT_TOOL_KEYS:
                if key in block:
                    _validate_url_context_block(key, block[key])
            dropped.extend(key for key in NON_WEB_HOSTED_TOOL_KEYS if key in block)
        else:
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
