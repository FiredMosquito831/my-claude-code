"""Detect forced Anthropic web server tool requests and read their parameters."""

from dataclasses import dataclass
from typing import Any

from my_claude_code.application.errors import InvalidRequestError
from my_claude_code.core.anthropic import MessagesRequest, Tool

from .parsers import content_text

EXPECTED_ANTHROPIC_WEB_TOOLS_SHAPE = (
    'Expected Anthropic server tools: {"type": "web_search_20250305", "name": "web_search", '
    '"allowed_domains"?: string[], "blocked_domains"?: string[], "max_uses"?: int >= 1} '
    'or {"type": "web_fetch_20250910", "name": "web_fetch", "allowed_domains"?: string[], '
    '"blocked_domains"?: string[], "max_uses"?: int >= 1, "max_content_tokens"?: int >= 1}, '
    'with matching tool_choice {"type": "auto" | "any" | "none"} or '
    '{"type": "tool", "name": "<declared tool>"}.'
)


@dataclass(frozen=True, slots=True)
class WebSearchToolOptions:
    """Parameters the client declared on its ``web_search`` tool definition.

    Anthropic documents ``max_uses``, ``allowed_domains``, ``blocked_domains``
    and ``user_location`` on the tool itself rather than on the tool call, so
    they arrive as extra fields on :class:`Tool` (which allows extras).
    """

    allowed_domains: tuple[str, ...] = ()
    blocked_domains: tuple[str, ...] = ()
    max_uses: int | None = None


def _domain_tuple(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list | tuple):
        return ()
    return tuple(str(entry).strip() for entry in raw if str(entry).strip())


def web_search_tool_options(request: MessagesRequest) -> WebSearchToolOptions:
    """Read search parameters off the request's ``web_search`` tool definition.

    Anthropic rejects a request carrying both allow and block lists, so a
    caller sending both is honoured on the allow list alone rather than
    silently intersecting them.
    """

    for tool in request.tools or []:
        if tool.name != "web_search":
            continue
        extra = tool.model_extra or {}
        allowed = _domain_tuple(extra.get("allowed_domains"))
        blocked = () if allowed else _domain_tuple(extra.get("blocked_domains"))
        raw_max_uses = extra.get("max_uses")
        max_uses = raw_max_uses if isinstance(raw_max_uses, int) else None
        return WebSearchToolOptions(
            allowed_domains=allowed,
            blocked_domains=blocked,
            max_uses=max_uses,
        )
    return WebSearchToolOptions()


def forced_tool_turn_text(request: MessagesRequest) -> str:
    """Text for parsing forced server-tool inputs: latest user turn only (avoids stale history)."""
    if not request.messages:
        return ""

    for message in reversed(request.messages):
        if message.role == "user":
            return content_text(message.content)
    return ""


def selected_server_tool_name(request: MessagesRequest) -> str | None:
    """Return the one local server tool selected without ambiguity.

    Claude Code's WebSearch and WebFetch helpers send a dedicated request with
    one server-tool definition and ``tool_choice=auto``. Explicit tool choices
    remain supported. An auto request with multiple tools belongs to the
    upstream model and must not be intercepted here.
    """

    tool_choice = request.tool_choice
    if not isinstance(tool_choice, dict):
        return None
    if tool_choice.get("type") == "tool":
        name = tool_choice.get("name")
        return str(name) if name in {"web_search", "web_fetch"} else None
    tools = request.tools or []
    if tool_choice.get("type") != "auto" or len(tools) != 1:
        return None
    name = tools[0].name
    return name if name in {"web_search", "web_fetch"} else None


def has_tool_named(request: MessagesRequest, name: str) -> bool:
    return any(tool.name == name for tool in request.tools or [])


def is_web_server_tool_request(request: MessagesRequest) -> bool:
    """True when one local server tool is selected without ambiguity."""

    selected = selected_server_tool_name(request)
    if selected is None:
        return False
    return has_tool_named(request, selected)


def is_anthropic_server_tool_definition(tool: Tool) -> bool:
    """Whether ``tool`` refers to an Anthropic server tool (web_search / web_fetch family)."""
    name = (tool.name or "").strip()
    if name in ("web_search", "web_fetch"):
        return True
    typ = tool.type
    if isinstance(typ, str):
        return typ.startswith("web_search") or typ.startswith("web_fetch")
    return False


def has_listed_anthropic_server_tools(request: MessagesRequest) -> bool:
    """True when tools include web_search / web_fetch-style entries (listed, forced or not)."""
    return any(is_anthropic_server_tool_definition(t) for t in (request.tools or []))


def unsupported_server_tool_error(
    request: MessagesRequest, *, web_tools_enabled: bool
) -> str | None:
    """Return the user-facing error when the resolved provider cannot run server tools."""
    selected = selected_server_tool_name(request)
    if selected and not web_tools_enabled:
        return (
            f"Anthropic server tool {selected!r} is selected, but local web server tools are "
            "disabled (ENABLE_WEB_SERVER_TOOLS=false). Enable them or remove the server tool."
        )
    if not selected and has_listed_anthropic_server_tools(request):
        return (
            "MCC cannot pass ambiguous Anthropic server tools (web_search / web_fetch) "
            "to OpenAI Chat upstreams. Use a dedicated single-tool request with "
            "tool_choice=auto, force one tool, or remove these tools."
        )
    return None


def is_anthropic_web_search_tool(tool: Tool) -> bool:
    """Whether ``tool`` declares an Anthropic ``web_search`` server tool."""
    name = (tool.name or "").strip()
    typ = tool.type
    if isinstance(typ, str) and typ.startswith("web_search"):
        return True
    return name == "web_search" and typ in (None, "custom")


def is_anthropic_web_fetch_tool(tool: Tool) -> bool:
    """Whether ``tool`` declares an Anthropic ``web_fetch`` server tool."""
    name = (tool.name or "").strip()
    typ = tool.type
    if isinstance(typ, str) and typ.startswith("web_fetch"):
        return True
    return name == "web_fetch" and typ in (None, "custom")


def is_unsupported_non_web_server_tool(tool: Tool) -> bool:
    """Whether ``tool`` uses a non-custom server tool type outside web_search/web_fetch."""
    typ = tool.type
    if typ is None or typ == "custom":
        return False
    return not is_anthropic_server_tool_definition(tool)


def list_unsupported_non_web_server_tools(request: MessagesRequest) -> list[Tool]:
    """Return declared server tools whose type is neither custom nor web_search/web_fetch."""
    return [t for t in (request.tools or []) if is_unsupported_non_web_server_tool(t)]


def _normalize_domain_entry(raw: str) -> str:
    return raw.strip().lower().rstrip(".")


def _domain_permits(candidate: str, allowed_rule: str) -> bool:
    return candidate == allowed_rule or candidate.endswith("." + allowed_rule)


def intersect_allowed_domains(
    *domain_lists: tuple[str, ...],
) -> tuple[tuple[str, ...], bool]:
    """Intersect multiple domain allowlists using tightening-only semantics.

    Empty tuples mean no restriction from that layer. Returns ``(domains, disjoint)``
    where ``disjoint=True`` indicates two non-empty allowlists had zero overlap.
    """
    non_empty = [
        tuple(dict.fromkeys(_normalize_domain_entry(d) for d in layer if d.strip()))
        for layer in domain_lists
        if any(d.strip() for d in layer)
    ]
    if not non_empty:
        return (), False
    current = list(non_empty[0])
    for nxt in non_empty[1:]:
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


def merge_blocked_domains(*domain_lists: tuple[str, ...]) -> tuple[str, ...]:
    """Union domain blocklists in normalized form."""
    seen: list[str] = []
    for layer in domain_lists:
        for raw in layer:
            norm = _normalize_domain_entry(raw)
            if norm and norm not in seen:
                seen.append(norm)
    return tuple(seen)


def _validate_domain_list(
    raw: object, *, field_name: str, tool_label: str
) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise InvalidRequestError(
            f"Invalid '{field_name}' on {tool_label}: expected a list of non-empty domain strings. "
            f"{EXPECTED_ANTHROPIC_WEB_TOOLS_SHAPE}"
        )
    domains: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise InvalidRequestError(
                f"Invalid '{field_name}' on {tool_label}: expected a list of non-empty domain strings. "
                f"{EXPECTED_ANTHROPIC_WEB_TOOLS_SHAPE}"
            )
        domains.append(_normalize_domain_entry(item))
    return tuple(dict.fromkeys(domains))


def _validate_positive_int(
    raw: object, *, field_name: str, tool_label: str
) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise InvalidRequestError(
            f"Invalid '{field_name}' on {tool_label}: expected an integer >= 1. "
            f"{EXPECTED_ANTHROPIC_WEB_TOOLS_SHAPE}"
        )
    return raw


@dataclass(frozen=True, slots=True)
class AnthropicMultiToolSpec:
    """Validated options for opt-in multi-tool ``/v1/messages`` requests."""

    has_web_search: bool
    has_web_fetch: bool
    search_options: WebSearchToolOptions
    fetch_options: WebSearchToolOptions
    search_external_web_access: bool
    fetch_external_web_access: bool
    user_location: dict[str, Any] | None
    max_fetch_content_tokens: int | None
    force_search: bool
    force_fetch: bool
    search_allowed_disjoint: bool
    fetch_allowed_disjoint: bool


def validate_and_extract_multi_tool_spec(
    request: MessagesRequest,
) -> AnthropicMultiToolSpec:
    """Validate Anthropic multi-tool definitions + ``tool_choice`` and return normalized spec."""
    search_tools = [t for t in (request.tools or []) if is_anthropic_web_search_tool(t)]
    fetch_tools = [t for t in (request.tools or []) if is_anthropic_web_fetch_tool(t)]

    search_allowed_layers: list[tuple[str, ...]] = []
    search_blocked_layers: list[tuple[str, ...]] = []
    search_max_uses: int | None = None
    search_external = True
    user_location: dict[str, Any] | None = None

    for tool in search_tools:
        extra = tool.model_extra or {}
        label = f"tool {tool.name!r}"
        allowed = _validate_domain_list(
            extra.get("allowed_domains"), field_name="allowed_domains", tool_label=label
        )
        blocked = _validate_domain_list(
            extra.get("blocked_domains"), field_name="blocked_domains", tool_label=label
        )
        if allowed:
            search_allowed_layers.append(allowed)
        if blocked:
            search_blocked_layers.append(blocked)
        uses = _validate_positive_int(
            extra.get("max_uses"), field_name="max_uses", tool_label=label
        )
        if uses is not None:
            search_max_uses = (
                uses if search_max_uses is None else min(search_max_uses, uses)
            )
        ext = extra.get("external_web_access")
        if ext is not None:
            if not isinstance(ext, bool):
                raise InvalidRequestError(
                    f"Invalid 'external_web_access' on {label}: expected a boolean. "
                    f"{EXPECTED_ANTHROPIC_WEB_TOOLS_SHAPE}"
                )
            search_external = search_external and ext
        loc = extra.get("user_location")
        if loc is not None:
            if not isinstance(loc, dict):
                raise InvalidRequestError(
                    f"Invalid 'user_location' on {label}: expected an object. "
                    f"{EXPECTED_ANTHROPIC_WEB_TOOLS_SHAPE}"
                )
            user_location = dict(loc)

    fetch_allowed_layers: list[tuple[str, ...]] = []
    fetch_blocked_layers: list[tuple[str, ...]] = []
    fetch_max_uses: int | None = None
    fetch_external = True
    max_content_tokens: int | None = None

    for tool in fetch_tools:
        extra = tool.model_extra or {}
        label = f"tool {tool.name!r}"
        allowed = _validate_domain_list(
            extra.get("allowed_domains"), field_name="allowed_domains", tool_label=label
        )
        blocked = _validate_domain_list(
            extra.get("blocked_domains"), field_name="blocked_domains", tool_label=label
        )
        if allowed:
            fetch_allowed_layers.append(allowed)
        if blocked:
            fetch_blocked_layers.append(blocked)
        uses = _validate_positive_int(
            extra.get("max_uses"), field_name="max_uses", tool_label=label
        )
        if uses is not None:
            fetch_max_uses = (
                uses if fetch_max_uses is None else min(fetch_max_uses, uses)
            )
        tokens = _validate_positive_int(
            extra.get("max_content_tokens"),
            field_name="max_content_tokens",
            tool_label=label,
        )
        if tokens is not None:
            max_content_tokens = (
                tokens
                if max_content_tokens is None
                else min(max_content_tokens, tokens)
            )
        ext = extra.get("external_web_access")
        if ext is not None:
            if not isinstance(ext, bool):
                raise InvalidRequestError(
                    f"Invalid 'external_web_access' on {label}: expected a boolean. "
                    f"{EXPECTED_ANTHROPIC_WEB_TOOLS_SHAPE}"
                )
            fetch_external = fetch_external and ext

    force_search = False
    force_fetch = False
    tc = request.tool_choice
    if isinstance(tc, dict):
        tc_type = str(tc.get("type") or "").strip().lower()
        tc_name = (
            str(tc.get("name") or "").strip() if tc.get("name") is not None else ""
        )
        if tc_type == "web_search" or (tc_type == "tool" and tc_name == "web_search"):
            if not search_tools:
                raise InvalidRequestError(
                    "Invalid 'tool_choice': references 'web_search', but no web_search tool "
                    f"is declared in tools[]. {EXPECTED_ANTHROPIC_WEB_TOOLS_SHAPE}"
                )
            force_search = True
        elif tc_type == "web_fetch" or (tc_type == "tool" and tc_name == "web_fetch"):
            if not fetch_tools:
                raise InvalidRequestError(
                    "Invalid 'tool_choice': references 'web_fetch', but no web_fetch tool "
                    f"is declared in tools[]. {EXPECTED_ANTHROPIC_WEB_TOOLS_SHAPE}"
                )
            force_fetch = True

    merged_search_allowed, search_disjoint = intersect_allowed_domains(
        *search_allowed_layers
    )
    merged_fetch_allowed, fetch_disjoint = intersect_allowed_domains(
        *fetch_allowed_layers
    )

    return AnthropicMultiToolSpec(
        has_web_search=bool(search_tools),
        has_web_fetch=bool(fetch_tools),
        search_options=WebSearchToolOptions(
            allowed_domains=merged_search_allowed,
            blocked_domains=merge_blocked_domains(*search_blocked_layers),
            max_uses=search_max_uses,
        ),
        fetch_options=WebSearchToolOptions(
            allowed_domains=merged_fetch_allowed,
            blocked_domains=merge_blocked_domains(*fetch_blocked_layers),
            max_uses=fetch_max_uses,
        ),
        search_external_web_access=search_external,
        fetch_external_web_access=fetch_external,
        user_location=user_location,
        max_fetch_content_tokens=max_content_tokens,
        force_search=force_search,
        force_fetch=force_fetch,
        search_allowed_disjoint=search_disjoint,
        fetch_allowed_disjoint=fetch_disjoint,
    )
