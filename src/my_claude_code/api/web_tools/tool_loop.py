"""Shared bounded multi-door web_search and web_fetch execution loop."""

import json
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlparse

from my_claude_code.api.wire_surfaces import WireApi
from my_claude_code.application.errors import InvalidRequestError
from my_claude_code.application.execution import ProviderExecutor
from my_claude_code.application.routing import ModelRouter, RoutedMessagesPlan
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic import (
    ContentBlockText,
    ContentBlockToolResult,
    ContentBlockToolUse,
    Message,
    MessagesRequest,
    Tool,
    aggregate_anthropic_sse_to_message,
)
from my_claude_code.core.anthropic.server_tool_sse import (
    SERVER_TOOL_USE,
    WEB_FETCH_TOOL_ERROR,
    WEB_FETCH_TOOL_RESULT,
    WEB_SEARCH_TOOL_RESULT,
    WEB_SEARCH_TOOL_RESULT_ERROR,
)
from my_claude_code.core.anthropic.streaming import format_sse_event
from my_claude_code.core.failures import ExecutionFailure, FailureKind
from my_claude_code.websearch.registry import websearch_telemetry_context

from ..request_capture import RequestCapture
from . import outbound, streaming
from .constants import _MAX_FETCH_CHARS, _MAX_SEARCH_RESULTS
from .egress import (
    WebFetchEgressPolicy,
    WebFetchEgressViolation,
    web_fetch_allowed_scheme_set,
)
from .request import (
    WebSearchToolOptions,
    is_anthropic_server_tool_definition,
)

SYNTHETIC_WEB_SEARCH_TOOL_NAME = "__mcc_web_search"
SYNTHETIC_WEB_FETCH_TOOL_NAME = "__mcc_web_fetch"
SYNTHETIC_WEB_TOOL_NAMES = frozenset(
    {SYNTHETIC_WEB_SEARCH_TOOL_NAME, SYNTHETIC_WEB_FETCH_TOOL_NAME}
)

WEB_SEARCH_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "minLength": 1,
            "description": "Search query string.",
        },
    },
    "required": ["query"],
    "additionalProperties": False,
}

WEB_FETCH_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "url": {
            "type": "string",
            "minLength": 1,
            "description": "HTTP or HTTPS URL to fetch and extract readable text from.",
        },
    },
    "required": ["url"],
    "additionalProperties": False,
}

_SYNTHETIC_SCHEMAS: dict[str, tuple[str, dict[str, Any]]] = {
    SYNTHETIC_WEB_SEARCH_TOOL_NAME: ("query", WEB_SEARCH_TOOL_SCHEMA),
    SYNTHETIC_WEB_FETCH_TOOL_NAME: ("url", WEB_FETCH_TOOL_SCHEMA),
}

_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_BARE_URL_RE = re.compile(r"https?://[^\s<>\"'\)\]]+")


@dataclass(frozen=True, slots=True)
class WebToolLoopConfig:
    """Normalized policy and budget parameters for one multi-door loop run."""

    enable_search: bool = True
    enable_fetch: bool = False
    search_options: WebSearchToolOptions = field(default_factory=WebSearchToolOptions)
    fetch_options: WebSearchToolOptions = field(default_factory=WebSearchToolOptions)
    search_external_web_access: bool = True
    fetch_external_web_access: bool = True
    search_max_results: int = _MAX_SEARCH_RESULTS
    fetch_max_chars: int = _MAX_FETCH_CHARS
    user_location: dict[str, Any] | None = None
    max_steps: int = 5
    allow_private_override: bool | None = None
    initial_force_search: bool = False
    initial_force_fetch: bool = False
    search_allowed_disjoint: bool = False
    fetch_allowed_disjoint: bool = False


@dataclass(frozen=True, slots=True)
class GatheredSource:
    """Verified external source returned by search or fetch during this loop."""

    url: str
    title: str
    snippet: str = ""
    published: str = ""
    source_kind: Literal["search", "fetch"] = "search"


@dataclass(frozen=True, slots=True)
class WebToolCallRecord:
    """Protocol-neutral record of one synthetic search or fetch execution."""

    srv_id: str
    tool_kind: Literal["web_search", "web_fetch"]
    action: dict[str, Any]
    status: Literal["completed", "failed"]
    error_code: str | None = None
    results: tuple[dict[str, str], ...] = ()
    fetched_doc: dict[str, Any] | None = None


@dataclass(slots=True)
class WebToolLoopResult:
    """Complete outcome of executing one multi-door web search/fetch loop."""

    model: str
    stop_reason: str | None
    stop_sequence: str | None
    all_client_blocks: list[dict[str, Any]]
    final_step_blocks: list[dict[str, Any]]
    web_calls: list[WebToolCallRecord]
    gathered_sources: list[GatheredSource]
    grounded_citations: list[dict[str, Any]]
    usage: dict[str, Any]
    steps_executed: int
    loop_limit_hit: bool = False


ModelStepCallable = Callable[[MessagesRequest, int], Awaitable[dict[str, Any]]]


def synthetic_web_search_tool() -> Tool:
    """Build the synthetic search tool appended to the end of ``tools[]``."""
    return Tool(
        name=SYNTHETIC_WEB_SEARCH_TOOL_NAME,
        description=(
            "Search the web for current or factual information and return matching titles, "
            "URLs, and excerpts."
        ),
        input_schema=deepcopy(WEB_SEARCH_TOOL_SCHEMA),
    )


def synthetic_web_fetch_tool() -> Tool:
    """Build the synthetic fetch tool appended to the end of ``tools[]``."""
    return Tool(
        name=SYNTHETIC_WEB_FETCH_TOOL_NAME,
        description=(
            "Fetch a specific web page URL and extract its main readable text content."
        ),
        input_schema=deepcopy(WEB_FETCH_TOOL_SCHEMA),
    )


def ensure_no_synthetic_tool_name_collision(tools: Sequence[Tool] | None) -> None:
    """Reject caller tools that declare MCC's reserved synthetic tool names."""
    for tool in tools or ():
        if is_anthropic_server_tool_definition(tool):
            continue
        name = (tool.name or "").strip()
        if name in SYNTHETIC_WEB_TOOL_NAMES:
            raise InvalidRequestError(
                f"Tool name {name!r} is reserved by MCC's web search tool loop; rename your custom tool."
            )


def validate_synthetic_tool_input(
    tool_name: str,
    raw_input: Any,
) -> tuple[dict[str, str] | None, str | None]:
    """Validate synthetic tool call arguments and include the full schema on error."""
    spec = _SYNTHETIC_SCHEMAS.get(tool_name)
    if spec is None:
        return None, f"Unknown synthetic tool {tool_name!r}."
    required_key, schema = spec
    public_name = (
        "web_search" if tool_name == SYNTHETIC_WEB_SEARCH_TOOL_NAME else "web_fetch"
    )
    schema_text = json.dumps(schema, sort_keys=True)

    if not isinstance(raw_input, dict):
        return None, (
            f"Invalid arguments for {public_name}: expected a JSON object, got "
            f"{type(raw_input).__name__}. Expected input schema: {schema_text}"
        )

    unexpected = sorted(key for key in raw_input if key != required_key)
    if unexpected:
        return None, (
            f"Invalid arguments for {public_name}: unexpected parameter(s) "
            f"{', '.join(unexpected)}. Expected input schema: {schema_text}"
        )

    value = raw_input.get(required_key)
    if not isinstance(value, str) or not value.strip():
        return None, (
            f"Invalid arguments for {public_name}: '{required_key}' must be a non-empty string. "
            f"Expected input schema: {schema_text}"
        )

    return {required_key: value.strip()}, None


def _normalize_url_for_match(url: str) -> str:
    stripped = url.strip()
    if not stripped:
        return ""
    parsed = urlparse(stripped)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        return stripped
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    query = f"?{parsed.query}" if parsed.query else ""
    frag = f"#{parsed.fragment}" if parsed.fragment else ""
    return f"{scheme}://{netloc}{path}{query}{frag}"


def extract_grounded_citations(
    text: str,
    gathered_sources: Sequence[GatheredSource],
) -> list[dict[str, Any]]:
    """Extract OpenAI Responses flat ``url_citation`` annotations grounded in gathered results.

    Never invents or emits URLs that did not come from ``gathered_sources``.
    """
    if not text or not gathered_sources:
        return []

    sources_by_url: dict[str, GatheredSource] = {}
    for source in gathered_sources:
        norm = _normalize_url_for_match(source.url)
        if not norm:
            continue
        existing = sources_by_url.get(norm)
        if existing is None or (source.title and not existing.title):
            sources_by_url[norm] = source

    if not sources_by_url:
        return []

    spans: list[tuple[int, int, GatheredSource]] = []
    occupied: list[tuple[int, int]] = []

    def _overlaps(start: int, end: int) -> bool:
        return any(not (end <= s or start >= e) for s, e in occupied)

    for match in _MARKDOWN_LINK_RE.finditer(text):
        raw_url = match.group(2)
        source = sources_by_url.get(_normalize_url_for_match(raw_url))
        if source is None:
            continue
        start, end = match.span()
        occupied.append((start, end))
        spans.append((start, end, source))

    for match in _BARE_URL_RE.finditer(text):
        start, end = match.span()
        if _overlaps(start, end):
            continue
        raw_url = match.group(0)
        candidates = [raw_url]
        trimmed = raw_url.rstrip(".,;:!?\"'")
        if trimmed != raw_url:
            candidates.append(trimmed)
        matched_source: GatheredSource | None = None
        matched_len = len(raw_url)
        for cand in candidates:
            found = sources_by_url.get(_normalize_url_for_match(cand))
            if found is not None:
                matched_source = found
                matched_len = len(cand)
                break
        if matched_source is None:
            continue
        span_end = start + matched_len
        occupied.append((start, span_end))
        spans.append((start, span_end, matched_source))

    spans.sort(key=lambda item: item[0])
    return [
        {
            "type": "url_citation",
            "url": source.url,
            "title": source.title,
            "start_index": start,
            "end_index": end,
        }
        for start, end, source in spans
    ]


def build_anthropic_citations(
    annotations: Sequence[dict[str, Any]],
    gathered_sources: Sequence[GatheredSource],
) -> list[dict[str, Any]]:
    """Build grounded Anthropic ``web_search_result_location`` citations for a text block."""
    if not annotations:
        return []
    snippets_by_url: dict[str, str] = {}
    for src in gathered_sources:
        norm = _normalize_url_for_match(src.url)
        if norm and src.snippet and norm not in snippets_by_url:
            snippets_by_url[norm] = src.snippet

    seen_urls: set[str] = set()
    citations: list[dict[str, Any]] = []
    for ann in annotations:
        url = str(ann.get("url") or "")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        norm = _normalize_url_for_match(url)
        cited_text = snippets_by_url.get(norm) or str(ann.get("title") or url)
        citations.append(
            {
                "type": "web_search_result_location",
                "url": url,
                "title": str(ann.get("title") or url),
                "cited_text": cited_text[:500],
            }
        )
    return citations


def _block_type(block: Any) -> str:
    if isinstance(block, dict):
        return str(block.get("type") or "")
    return str(getattr(block, "type", "") or "")


def _summarize_server_tool_block(block: Any) -> str:
    btype = _block_type(block)
    if isinstance(block, dict):
        name = block.get("name")
        inp = block.get("input")
        content = block.get("content")
    else:
        name = getattr(block, "name", None)
        inp = getattr(block, "input", None)
        content = getattr(block, "content", None)

    if btype == SERVER_TOOL_USE:
        return f"[Prior {name or 'web_tool'} call: {json.dumps(inp or {})}]"
    if btype == WEB_SEARCH_TOOL_RESULT and isinstance(content, list):
        titles = [
            f"{item.get('title', '')} ({item.get('url', '')})"
            for item in content[:5]
            if isinstance(item, dict) and item.get("url")
        ]
        if titles:
            return "[Prior search results: " + "; ".join(titles) + "]"
    if btype == WEB_FETCH_TOOL_RESULT and isinstance(content, dict):
        url = str(content.get("url") or "")
        if url:
            return f"[Prior fetched URL: {url}]"
    return f"[Prior {btype}]"


def sanitize_messages_for_loop(messages: Sequence[Message]) -> list[Message]:
    """Convert historical Anthropic server-tool blocks into neutral text for loop safety."""
    sanitized: list[Message] = []
    server_types = {SERVER_TOOL_USE, WEB_SEARCH_TOOL_RESULT, WEB_FETCH_TOOL_RESULT}
    for msg in messages:
        if not isinstance(msg.content, list):
            sanitized.append(msg)
            continue
        if not any(_block_type(block) in server_types for block in msg.content):
            sanitized.append(msg)
            continue
        new_blocks: list[Any] = []
        summary_lines: list[str] = []
        for block in msg.content:
            btype = _block_type(block)
            if btype in server_types:
                summary_lines.append(_summarize_server_tool_block(block))
            else:
                new_blocks.append(block)
        if summary_lines:
            # Insert summary after any leading thinking/redacted_thinking blocks.
            insert_idx = 0
            while insert_idx < len(new_blocks) and _block_type(
                new_blocks[insert_idx]
            ) in (
                "thinking",
                "redacted_thinking",
            ):
                insert_idx += 1
            new_blocks.insert(
                insert_idx, ContentBlockText(type="text", text="\n".join(summary_lines))
            )
        sanitized.append(msg.model_copy(update={"content": new_blocks}))
    return sanitized


def _build_webfetch_egress_policy(
    settings: Settings,
    *,
    allow_private_override: bool | None,
) -> WebFetchEgressPolicy:
    """Apply P1 tightening-only egress rules for loop web_fetch calls."""
    configured = bool(settings.web_fetch_allow_private_networks)
    effective_private = (
        configured
        if allow_private_override is None
        else (configured and allow_private_override)
    )
    return WebFetchEgressPolicy(
        allow_private_network_targets=effective_private,
        allowed_schemes=web_fetch_allowed_scheme_set(
            settings.web_fetch_allowed_schemes
        ),
    )


def _host_allowed_for_domains(
    url: str,
    *,
    allowed_domains: tuple[str, ...],
    blocked_domains: tuple[str, ...],
    disjoint: bool = False,
) -> bool:
    if disjoint:
        return False
    parsed = urlparse(url)
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        return False
    for blocked in blocked_domains:
        if host == blocked or host.endswith("." + blocked):
            return False
    if allowed_domains:
        return any(
            host == allowed or host.endswith("." + allowed)
            for allowed in allowed_domains
        )
    return True


def _filter_search_items(
    items: list[dict[str, str]],
    *,
    allowed_domains: tuple[str, ...],
    blocked_domains: tuple[str, ...],
    disjoint: bool,
) -> list[dict[str, str]]:
    if disjoint:
        return []
    if not allowed_domains and not blocked_domains:
        return items
    return [
        item
        for item in items
        if _host_allowed_for_domains(
            item.get("url", ""),
            allowed_domains=allowed_domains,
            blocked_domains=blocked_domains,
            disjoint=disjoint,
        )
    ]


def _block_to_model_input(block: dict[str, Any]) -> Any:
    """Preserve non-server Anthropic blocks cleanly when sending assistant turns back."""
    btype = block.get("type")
    if btype == "tool_use":
        return ContentBlockToolUse(
            type="tool_use",
            id=str(block.get("id") or ""),
            name=str(block.get("name") or ""),
            input=dict(block.get("input") or {}),
        )
    if btype == "text":
        return ContentBlockText(type="text", text=str(block.get("text") or ""))
    return deepcopy(block)


async def _execute_synthetic_call(
    *,
    tool_name: str,
    raw_id: str,
    raw_input: Any,
    config: WebToolLoopConfig,
    settings: Settings,
    usage_counts: dict[str, int],
    gathered_sources: list[GatheredSource],
) -> tuple[WebToolCallRecord, dict[str, Any], dict[str, Any], ContentBlockToolResult]:
    """Execute one synthetic ``__mcc_web_search`` or ``__mcc_web_fetch`` call."""
    srv_id = f"srvtoolu_{uuid.uuid4().hex[:24]}"
    is_search = tool_name == SYNTHETIC_WEB_SEARCH_TOOL_NAME
    tool_kind: Literal["web_search", "web_fetch"] = (
        "web_search" if is_search else "web_fetch"
    )
    public_name = tool_kind
    result_block_type = WEB_SEARCH_TOOL_RESULT if is_search else WEB_FETCH_TOOL_RESULT
    error_payload_type = (
        WEB_SEARCH_TOOL_RESULT_ERROR if is_search else WEB_FETCH_TOOL_ERROR
    )

    def _error_tuple(
        *,
        action: dict[str, Any],
        error_code: str,
        llm_message: str,
    ) -> tuple[
        WebToolCallRecord, dict[str, Any], dict[str, Any], ContentBlockToolResult
    ]:
        srv_block = {
            "type": SERVER_TOOL_USE,
            "id": srv_id,
            "name": public_name,
            "input": action,
        }
        res_block = {
            "type": result_block_type,
            "tool_use_id": srv_id,
            "content": {"type": error_payload_type, "error_code": error_code},
        }
        record = WebToolCallRecord(
            srv_id=srv_id,
            tool_kind=tool_kind,
            action=action,
            status="failed",
            error_code=error_code,
        )
        llm_result = ContentBlockToolResult.model_validate(
            {
                "type": "tool_result",
                "tool_use_id": raw_id,
                "content": llm_message,
                "is_error": True,
            }
        )
        return record, srv_block, res_block, llm_result

    # 1. Budget enforcement (max_uses).
    max_uses = (
        config.search_options.max_uses if is_search else config.fetch_options.max_uses
    )
    used = usage_counts[tool_kind]
    if max_uses is not None and used >= max_uses:
        action = dict(raw_input) if isinstance(raw_input, dict) else {}
        return _error_tuple(
            action=action,
            error_code="max_uses_exceeded",
            llm_message=f"{public_name} budget exceeded (max_uses={max_uses}). Answer from already gathered results.",
        )

    # 2. Schema validation + schema-on-error (M11 self-correction).
    validated, schema_err = validate_synthetic_tool_input(tool_name, raw_input)
    if validated is None:
        action = dict(raw_input) if isinstance(raw_input, dict) else {}
        return _error_tuple(
            action=action,
            error_code="invalid_tool_input",
            llm_message=schema_err or "Invalid tool arguments.",
        )

    usage_counts[tool_kind] += 1

    # 3. External web access guard.
    external_allowed = (
        config.search_external_web_access
        if is_search
        else config.fetch_external_web_access
    )
    if not external_allowed:
        action = (
            {"type": "search", "query": validated["query"]}
            if is_search
            else {"type": "open_page", "url": validated["url"]}
        )
        return _error_tuple(
            action=action,
            error_code="unavailable",
            llm_message=f"External web access is disabled for {public_name} (external_web_access=false).",
        )

    if is_search:
        query = validated["query"]
        if (
            not settings.enable_web_server_tools
            or settings.web_search_provider == "disabled"
        ):
            return _error_tuple(
                action={"type": "search", "query": query},
                error_code="unavailable",
                llm_message="Web search is disabled on this MCC server.",
            )
        if config.search_allowed_disjoint:
            items: list[dict[str, str]] = []
            response = None
        else:
            try:
                with websearch_telemetry_context(
                    door="tool_loop", tool_kind="web_search"
                ):
                    (
                        response,
                        raw_items,
                        _meta,
                    ) = await outbound._run_web_search_response(
                        query,
                        settings,
                        max_results=config.search_max_results,
                        allowed_domains=config.search_options.allowed_domains,
                        blocked_domains=config.search_options.blocked_domains,
                    )
                items = _filter_search_items(
                    raw_items,
                    allowed_domains=config.search_options.allowed_domains,
                    blocked_domains=config.search_options.blocked_domains,
                    disjoint=config.search_allowed_disjoint,
                )
            except Exception as error:
                outbound._log_web_tool_failure("web_search", error)
                code = streaming._web_search_error_code(error)
                summary = outbound._web_tool_client_error_summary(
                    "web_search",
                    error,
                    verbose=bool(settings.log_api_error_tracebacks),
                )
                return _error_tuple(
                    action={"type": "search", "query": query},
                    error_code=code,
                    llm_message=f"web_search failed ({code}): {summary}",
                )

        if response is not None and response.answer:
            items_with_answer = list(items)
        else:
            items_with_answer = items
        for item in items_with_answer:
            url = str(item.get("url") or "").strip()
            if url:
                gathered_sources.append(
                    GatheredSource(
                        url=url,
                        title=str(item.get("title") or url),
                        snippet=str(item.get("snippet") or item.get("text") or ""),
                        published=str(item.get("published") or ""),
                        source_kind="search",
                    )
                )

        srv_block = {
            "type": SERVER_TOOL_USE,
            "id": srv_id,
            "name": "web_search",
            "input": {"query": query},
        }
        result_blocks = [
            streaming._web_search_result_block(item) for item in items_with_answer
        ]
        res_block = {
            "type": WEB_SEARCH_TOOL_RESULT,
            "tool_use_id": srv_id,
            "content": result_blocks,
        }
        digest = streaming.format_search_digest(query, items_with_answer, settings)
        llm_result = ContentBlockToolResult(
            type="tool_result",
            tool_use_id=raw_id,
            content=digest,
        )
        record = WebToolCallRecord(
            srv_id=srv_id,
            tool_kind="web_search",
            action={"type": "search", "query": query},
            status="completed",
            results=tuple(items_with_answer),
        )
        return record, srv_block, res_block, llm_result

    # web_fetch branch
    target_url = validated["url"]
    if not settings.enable_web_server_tools:
        return _error_tuple(
            action={"type": "open_page", "url": target_url},
            error_code="unavailable",
            llm_message="Web fetch is disabled on this MCC server (ENABLE_WEB_SERVER_TOOLS=false).",
        )
    if not _host_allowed_for_domains(
        target_url,
        allowed_domains=config.fetch_options.allowed_domains,
        blocked_domains=config.fetch_options.blocked_domains,
        disjoint=config.fetch_allowed_disjoint,
    ):
        return _error_tuple(
            action={"type": "open_page", "url": target_url},
            error_code="url_not_allowed",
            llm_message=f"URL {target_url!r} is not permitted by tool allowed_domains/blocked_domains policy.",
        )

    egress = _build_webfetch_egress_policy(
        settings, allow_private_override=config.allow_private_override
    )
    max_chars = config.fetch_max_chars
    try:
        fetched, _extractor, _truncated = await outbound._run_web_fetch_detail(
            target_url,
            egress,
            max_chars=max_chars,
        )
    except WebFetchEgressViolation as error:
        outbound._log_web_tool_failure("web_fetch", error, fetch_url=target_url)
        return _error_tuple(
            action={"type": "open_page", "url": target_url},
            error_code="url_not_allowed",
            llm_message=f"web_fetch blocked by egress policy: {error}",
        )
    except Exception as error:
        outbound._log_web_tool_failure("web_fetch", error, fetch_url=target_url)
        summary = outbound._web_tool_client_error_summary(
            "web_fetch",
            error,
            verbose=bool(settings.log_api_error_tracebacks),
        )
        return _error_tuple(
            action={"type": "open_page", "url": target_url},
            error_code="unavailable",
            llm_message=f"web_fetch failed: {summary}",
        )

    fetched_url = str(fetched.get("url") or target_url)
    if not _host_allowed_for_domains(
        fetched_url,
        allowed_domains=config.fetch_options.allowed_domains,
        blocked_domains=config.fetch_options.blocked_domains,
        disjoint=config.fetch_allowed_disjoint,
    ):
        return _error_tuple(
            action={"type": "open_page", "url": fetched_url},
            error_code="url_not_allowed",
            llm_message=f"Redirected URL {fetched_url!r} is not permitted by tool allowed_domains/blocked_domains policy.",
        )
    fetched_title = str(fetched.get("title") or fetched_url)
    fetched_data = str(fetched.get("data") or "")
    gathered_sources.append(
        GatheredSource(
            url=fetched_url,
            title=fetched_title,
            snippet=fetched_data[:500],
            source_kind="fetch",
        )
    )
    fetch_result_content = {
        "type": "web_fetch_result",
        "url": fetched_url,
        "content": {
            "type": "document",
            "source": {
                "type": "text",
                "media_type": str(fetched.get("media_type") or "text/plain"),
                "data": fetched_data,
            },
            "title": fetched_title,
            "citations": {"enabled": True},
        },
        "retrieved_at": datetime.now(UTC).isoformat(),
    }
    srv_block = {
        "type": SERVER_TOOL_USE,
        "id": srv_id,
        "name": "web_fetch",
        "input": {"url": target_url},
    }
    res_block = {
        "type": WEB_FETCH_TOOL_RESULT,
        "tool_use_id": srv_id,
        "content": fetch_result_content,
    }
    llm_result = ContentBlockToolResult(
        type="tool_result",
        tool_use_id=raw_id,
        content=f"URL: {fetched_url}\nTitle: {fetched_title}\n\n{fetched_data[:max_chars]}",
    )
    record = WebToolCallRecord(
        srv_id=srv_id,
        tool_kind="web_fetch",
        action={"type": "open_page", "url": fetched_url},
        status="completed",
        fetched_doc=fetch_result_content,
    )
    return record, srv_block, res_block, llm_result


def _split_caller_tools(tools: Sequence[Tool] | None) -> list[Tool]:
    """Return non-server custom tools in original order (cache-prefix stability)."""
    return [t for t in (tools or ()) if not is_anthropic_server_tool_definition(t)]


def _build_step_tools(
    caller_tools: list[Tool],
    config: WebToolLoopConfig,
    *,
    include_synthetic: bool,
) -> list[Tool] | None:
    combined = list(caller_tools)
    if include_synthetic:
        if config.enable_search:
            combined.append(synthetic_web_search_tool())
        if config.enable_fetch:
            combined.append(synthetic_web_fetch_tool())
    return combined or None


def _step_tool_choice(
    base_request: MessagesRequest,
    config: WebToolLoopConfig,
    *,
    step_index: int,
    summarizing: bool,
) -> Any:
    if summarizing:
        return None
    if step_index == 0:
        if config.initial_force_search and config.enable_search:
            return {"type": "tool", "name": SYNTHETIC_WEB_SEARCH_TOOL_NAME}
        if config.initial_force_fetch and config.enable_fetch:
            return {"type": "tool", "name": SYNTHETIC_WEB_FETCH_TOOL_NAME}
        tc = base_request.tool_choice
        if isinstance(tc, dict):
            tc_type = str(tc.get("type") or "").strip().lower()
            tc_name = (
                str(tc.get("name") or "").strip() if tc.get("name") is not None else ""
            )
            if tc_type == "web_search" or (
                tc_type == "tool" and tc_name == "web_search"
            ):
                return {"type": "tool", "name": SYNTHETIC_WEB_SEARCH_TOOL_NAME}
            if tc_type == "web_fetch" or (tc_type == "tool" and tc_name == "web_fetch"):
                return {"type": "tool", "name": SYNTHETIC_WEB_FETCH_TOOL_NAME}
        return tc
    return None


def _accumulate_usage(totals: dict[str, int], msg: dict[str, Any]) -> None:
    usage = msg.get("usage")
    if not isinstance(usage, dict):
        return
    for key in (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    ):
        val = usage.get(key)
        if isinstance(val, int) and not isinstance(val, bool) and val > 0:
            totals[key] += val


def _extract_final_text(blocks: Sequence[dict[str, Any]]) -> str:
    return "".join(
        str(block.get("text") or "") for block in blocks if block.get("type") == "text"
    )


def _attach_citations_to_text_blocks(
    blocks: list[dict[str, Any]],
    gathered_sources: Sequence[GatheredSource],
) -> list[dict[str, Any]]:
    final_text = _extract_final_text(blocks)
    annotations = extract_grounded_citations(final_text, gathered_sources)
    if not annotations:
        return annotations
    anthropic_citations = build_anthropic_citations(annotations, gathered_sources)
    if anthropic_citations:
        for block in reversed(blocks):
            if block.get("type") == "text" and str(block.get("text") or "").strip():
                block["citations"] = anthropic_citations
                break
    return annotations


async def execute_web_tool_loop(
    base_request: MessagesRequest,
    config: WebToolLoopConfig,
    settings: Settings,
    call_model_step: ModelStepCallable,
) -> WebToolLoopResult:
    """Execute a bounded model + local web_search/web_fetch loop above ProviderExecutor."""
    ensure_no_synthetic_tool_name_collision(base_request.tools)

    caller_tools = _split_caller_tools(base_request.tools)
    working_messages = sanitize_messages_for_loop(base_request.messages)
    max_steps = max(1, int(config.max_steps))

    client_blocks: list[dict[str, Any]] = []
    final_step_blocks: list[dict[str, Any]] = []
    web_calls: list[WebToolCallRecord] = []
    gathered_sources: list[GatheredSource] = []
    usage_totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    tool_use_counts = {"web_search": 0, "web_fetch": 0}
    last_model = base_request.model
    stop_reason: str | None = "end_turn"
    stop_sequence: str | None = None
    steps_executed = 0
    loop_limit_hit = False
    pending_summarization = False

    for step_index in range(max_steps):
        step_tools = _build_step_tools(caller_tools, config, include_synthetic=True)
        step_choice = _step_tool_choice(
            base_request, config, step_index=step_index, summarizing=False
        )
        step_request = base_request.model_copy(
            update={
                "messages": list(working_messages),
                "tools": step_tools,
                "tool_choice": step_choice,
                "stream": True,
            }
        )
        msg = await call_model_step(step_request, step_index)
        steps_executed += 1
        last_model = str(msg.get("model") or last_model)
        _accumulate_usage(usage_totals, msg)
        stop_reason = msg.get("stop_reason") or "end_turn"
        stop_sequence = msg.get("stop_sequence")
        raw_content = list(msg.get("content") or [])

        synthetic_calls = [
            block
            for block in raw_content
            if block.get("type") == "tool_use"
            and block.get("name") in SYNTHETIC_WEB_TOOL_NAMES
        ]
        client_tool_calls = [
            block
            for block in raw_content
            if block.get("type") == "tool_use"
            and block.get("name") not in SYNTHETIC_WEB_TOOL_NAMES
        ]

        if not synthetic_calls:
            final_step_blocks = [deepcopy(b) for b in raw_content]
            client_blocks.extend(deepcopy(b) for b in raw_content)
            pending_summarization = False
            break

        # Execute synthetic web tool calls cleanly while keeping block order.
        step_llm_results: list[ContentBlockToolResult] = []
        step_non_synthetic_blocks: list[dict[str, Any]] = []
        for block in raw_content:
            btype = block.get("type")
            bname = block.get("name")
            if btype == "tool_use" and bname in SYNTHETIC_WEB_TOOL_NAMES:
                raw_id = str(block.get("id") or f"toolu_{uuid.uuid4().hex[:16]}")
                (
                    record,
                    srv_block,
                    res_block,
                    llm_result,
                ) = await _execute_synthetic_call(
                    tool_name=str(bname),
                    raw_id=raw_id,
                    raw_input=block.get("input"),
                    config=config,
                    settings=settings,
                    usage_counts=tool_use_counts,
                    gathered_sources=gathered_sources,
                )
                web_calls.append(record)
                client_blocks.append(srv_block)
                client_blocks.append(res_block)
                step_llm_results.append(llm_result)
            else:
                copied = deepcopy(block)
                client_blocks.append(copied)
                step_non_synthetic_blocks.append(copied)

        if client_tool_calls:
            # Mixed client + web turn: never drop caller's client tool_use; return immediately.
            final_step_blocks = step_non_synthetic_blocks
            stop_reason = "tool_use"
            pending_summarization = False
            break

        # Append assistant turn + tool_result turn to loop history for next step.
        working_messages.append(
            Message(
                role="assistant",
                content=[_block_to_model_input(b) for b in raw_content],
            )
        )
        working_messages.append(Message(role="user", content=list(step_llm_results)))
        pending_summarization = step_index == max_steps - 1
        if pending_summarization:
            loop_limit_hit = True

    if pending_summarization:
        summary_tools = _build_step_tools(caller_tools, config, include_synthetic=False)
        summary_request = base_request.model_copy(
            update={
                "messages": list(working_messages),
                "tools": summary_tools,
                "tool_choice": None,
                "stream": True,
            }
        )
        msg = await call_model_step(summary_request, steps_executed)
        steps_executed += 1
        last_model = str(msg.get("model") or last_model)
        _accumulate_usage(usage_totals, msg)
        stop_reason = msg.get("stop_reason") or "end_turn"
        stop_sequence = msg.get("stop_sequence")
        raw_content = [
            deepcopy(b)
            for b in list(msg.get("content") or [])
            if not (
                b.get("type") == "tool_use"
                and b.get("name") in SYNTHETIC_WEB_TOOL_NAMES
            )
        ]
        final_step_blocks = raw_content
        client_blocks.extend(deepcopy(b) for b in raw_content)

    # Guarantee raw synthetic tool names never appear anywhere in client blocks.
    assert not any(
        b.get("type") == "tool_use" and b.get("name") in SYNTHETIC_WEB_TOOL_NAMES
        for b in client_blocks
    )

    grounded_annotations = _attach_citations_to_text_blocks(
        final_step_blocks, gathered_sources
    )
    if grounded_annotations:
        _attach_citations_to_text_blocks(client_blocks, gathered_sources)

    server_usage: dict[str, int] = {}
    if tool_use_counts["web_search"] > 0:
        server_usage["web_search_requests"] = tool_use_counts["web_search"]
    if tool_use_counts["web_fetch"] > 0:
        server_usage["web_fetch_requests"] = tool_use_counts["web_fetch"]

    usage: dict[str, Any] = {**usage_totals}
    if server_usage:
        usage["server_tool_use"] = server_usage

    return WebToolLoopResult(
        model=last_model,
        stop_reason=stop_reason,
        stop_sequence=stop_sequence,
        all_client_blocks=client_blocks,
        final_step_blocks=final_step_blocks,
        web_calls=web_calls,
        gathered_sources=gathered_sources,
        grounded_citations=grounded_annotations,
        usage=usage,
        steps_executed=steps_executed,
        loop_limit_hit=loop_limit_hit,
    )


async def run_routed_web_tool_loop(
    base_request: MessagesRequest,
    config: WebToolLoopConfig,
    settings: Settings,
    *,
    model_router: ModelRouter,
    provider_executor: ProviderExecutor,
    wire_api: WireApi,
    request_id: str,
    raw_log_label: str,
    raw_log_payload: object | None = None,
    capture: RequestCapture | None = None,
    harness: str | None = None,
    plan_postprocessor: Callable[[RoutedMessagesPlan], RoutedMessagesPlan]
    | None = None,
) -> WebToolLoopResult:
    """Route and execute the web tool loop through ``ModelRouter`` + ``ProviderExecutor``."""

    async def _call_step(
        step_request: MessagesRequest, step_index: int
    ) -> dict[str, Any]:
        plan = model_router.resolve_messages_plan(step_request, harness=harness)
        if plan_postprocessor is not None:
            plan = plan_postprocessor(plan)
        if capture is not None and step_index == 0:
            capture.set_plan(plan)

        def _on_attempt_result(attempt_record: Any) -> None:
            if capture is None:
                return
            if step_index == 0:
                capture.record_attempt_result(attempt_record)
            else:
                capture.record_attempt_result(
                    replace(
                        attempt_record,
                        attempt=step_index * 100 + attempt_record.attempt,
                    )
                )

        step_label = (
            raw_log_label if step_index == 0 else f"{raw_log_label}_STEP_{step_index}"
        )
        step_payload = raw_log_payload if step_index == 0 else step_request.model_dump()
        streamed = provider_executor.stream(
            plan,
            wire_api=wire_api,
            raw_log_label=step_label,
            raw_log_payload=step_payload,
            request_id=request_id,
            on_attempt=capture.set_routing if capture is not None else None,
            on_attempt_result=_on_attempt_result if capture is not None else None,
        )
        message, stream_error = await aggregate_anthropic_sse_to_message(streamed)
        if stream_error is not None:
            err_msg = str(
                stream_error.get("message") or "Upstream error during web tool loop"
            )
            raise ExecutionFailure(
                kind=FailureKind.UPSTREAM,
                status_code=502,
                message=err_msg,
                retryable=False,
            )
        return message

    return await execute_web_tool_loop(base_request, config, settings, _call_step)


def anthropic_message_payload_from_loop(
    result: WebToolLoopResult,
    *,
    response_model: str,
    message_id: str | None = None,
) -> dict[str, Any]:
    """Render ``WebToolLoopResult`` as a complete non-streaming Anthropic Message dict."""
    return {
        "id": message_id or f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": response_model,
        "content": result.all_client_blocks,
        "stop_reason": result.stop_reason or "end_turn",
        "stop_sequence": result.stop_sequence,
        "usage": result.usage,
    }


async def iter_anthropic_sse_from_loop(
    result: WebToolLoopResult,
    *,
    response_model: str,
    message_id: str | None = None,
) -> AsyncIterator[str]:
    """Emit authentic Anthropic SSE events for a completed ``WebToolLoopResult``."""
    msg_id = message_id or f"msg_{uuid.uuid4().hex[:24]}"
    input_tokens = int(result.usage.get("input_tokens") or 0)
    output_tokens = int(result.usage.get("output_tokens") or 0)

    yield format_sse_event(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "model": response_model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": 0,
                    "cache_creation_input_tokens": int(
                        result.usage.get("cache_creation_input_tokens") or 0
                    ),
                    "cache_read_input_tokens": int(
                        result.usage.get("cache_read_input_tokens") or 0
                    ),
                },
            },
        },
    )

    for index, block in enumerate(result.all_client_blocks):
        btype = block.get("type")
        if btype == SERVER_TOOL_USE:
            yield format_sse_event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {
                        "type": SERVER_TOOL_USE,
                        "id": block["id"],
                        "name": block["name"],
                        "input": {},
                    },
                },
            )
            yield format_sse_event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": json.dumps(block.get("input") or {}),
                    },
                },
            )
            yield format_sse_event(
                "content_block_stop",
                {"type": "content_block_stop", "index": index},
            )
        elif btype in (WEB_SEARCH_TOOL_RESULT, WEB_FETCH_TOOL_RESULT):
            yield format_sse_event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": block,
                },
            )
            yield format_sse_event(
                "content_block_stop",
                {"type": "content_block_stop", "index": index},
            )
        elif btype == "tool_use":
            yield format_sse_event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {
                        "type": "tool_use",
                        "id": block["id"],
                        "name": block["name"],
                        "input": {},
                    },
                },
            )
            yield format_sse_event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": json.dumps(block.get("input") or {}),
                    },
                },
            )
            yield format_sse_event(
                "content_block_stop",
                {"type": "content_block_stop", "index": index},
            )
        elif btype == "thinking":
            yield format_sse_event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {"type": "thinking", "thinking": ""},
                },
            )
            thinking_text = str(block.get("thinking") or "")
            if thinking_text:
                yield format_sse_event(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {"type": "thinking_delta", "thinking": thinking_text},
                    },
                )
            if sig := block.get("signature"):
                yield format_sse_event(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {"type": "signature_delta", "signature": str(sig)},
                    },
                )
            yield format_sse_event(
                "content_block_stop",
                {"type": "content_block_stop", "index": index},
            )
        elif btype == "text":
            start_block: dict[str, Any] = {"type": "text", "text": ""}
            if citations := block.get("citations"):
                start_block["citations"] = citations
            yield format_sse_event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": start_block,
                },
            )
            text_val = str(block.get("text") or "")
            if text_val:
                yield format_sse_event(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {"type": "text_delta", "text": text_val},
                    },
                )
            yield format_sse_event(
                "content_block_stop",
                {"type": "content_block_stop", "index": index},
            )
        else:
            yield format_sse_event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": block,
                },
            )
            yield format_sse_event(
                "content_block_stop",
                {"type": "content_block_stop", "index": index},
            )

    delta_usage: dict[str, Any] = {"output_tokens": output_tokens}
    if server_usage := result.usage.get("server_tool_use"):
        delta_usage["server_tool_use"] = server_usage
    yield format_sse_event(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {
                "stop_reason": result.stop_reason or "end_turn",
                "stop_sequence": result.stop_sequence,
            },
            "usage": delta_usage,
        },
    )
    yield format_sse_event("message_stop", {"type": "message_stop"})
