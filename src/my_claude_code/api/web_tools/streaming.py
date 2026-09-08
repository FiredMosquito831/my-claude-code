"""SSE streaming for local web_search / web_fetch server tool results."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from my_claude_code.config.settings import Settings, get_settings
from my_claude_code.core.anthropic import MessagesRequest
from my_claude_code.core.anthropic.server_tool_sse import (
    SERVER_TOOL_USE,
    WEB_FETCH_TOOL_ERROR,
    WEB_FETCH_TOOL_RESULT,
    WEB_SEARCH_TOOL_RESULT,
    WEB_SEARCH_TOOL_RESULT_ERROR,
)
from my_claude_code.core.anthropic.streaming import format_sse_event
from my_claude_code.websearch.errors import (
    WebSearchInvalidRequestError,
    WebSearchQuotaError,
    WebSearchRateLimitError,
)

from . import outbound
from .constants import _MAX_FETCH_CHARS
from .egress import WebFetchEgressPolicy
from .parsers import extract_query, extract_url
from .request import (
    forced_tool_turn_text,
    has_tool_named,
    selected_server_tool_name,
    web_search_tool_options,
)


class _MaxUsesExceeded(Exception):
    """The client's ``max_uses`` budget leaves no room for this search."""


def _web_search_error_code(error: BaseException) -> str:
    """Map a failure to one of Anthropic's documented ``error_code`` values.

    Collapsing everything to ``unavailable`` loses the distinction a client
    acts on: ``too_many_requests`` is worth retrying later, an invalid input
    never is. Anything we cannot classify confidently stays ``unavailable``.
    """

    if isinstance(error, _MaxUsesExceeded):
        return "max_uses_exceeded"
    if isinstance(error, WebSearchRateLimitError | WebSearchQuotaError):
        return "too_many_requests"
    if isinstance(error, WebSearchInvalidRequestError):
        return "invalid_tool_input"
    return "unavailable"


_MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def _format_page_age(published: str) -> str:
    """Human date for Anthropic's ``page_age`` field (e.g. "July 22, 2026").

    Non-ISO values (e.g. Serper's "Jan 2, 2026") pass through unchanged.
    """

    try:
        parsed = datetime.fromisoformat(published.replace("Z", "+00:00"))
    except ValueError:
        return published
    return f"{_MONTH_NAMES[parsed.month - 1]} {parsed.day}, {parsed.year}"


def _web_search_result_block(result: dict[str, str]) -> dict[str, str]:
    block = {
        "type": "web_search_result",
        "title": result["title"],
        "url": result["url"],
    }
    if published := result.get("published", ""):
        block["page_age"] = _format_page_age(published)
    return block


def _search_summary(
    query: str, results: list[dict[str, str]], settings: Settings
) -> str:
    """Rich per-result digest: optional provider answer lead, then numbered
    title (date) + url + excerpt capped at ``websearch_digest_chars``.

    Sparse results still render with title and URL; provider metadata is optional.
    """

    if not results:
        return f"No web search results found for: {query}"
    lines = [f"Search results for: {query}"]
    provider = results[0].get("provider", "")
    if provider:
        lines.append(f"Source provider: {provider}")
    answer = results[0].get("answer", "") if settings.websearch_digest_answer else ""
    if answer:
        lines.append(answer)
    for index, result in enumerate(results, start=1):
        header = f"{index}. {result['title']}"
        if published := result.get("published", ""):
            header += f" ({_format_page_age(published)})"
        block = f"{header}\n{result['url']}"
        # Extracted page text beats the snippet when the provider returned it:
        # it is what the operator opted in (and paid) for. It gets its own,
        # larger cap so enabling content is not silently trimmed to snippet
        # length.
        content = result.get("content", "")
        if content:
            excerpt = content[: settings.websearch_digest_content_chars]
        else:
            excerpt = result.get("snippet", "")[: settings.websearch_digest_chars]
        if excerpt:
            block += f"\n{excerpt}"
        lines.append(block)
    return "\n\n".join(lines)


async def stream_web_server_tool_response(
    request: MessagesRequest,
    input_tokens: int,
    *,
    web_fetch_egress: WebFetchEgressPolicy,
    verbose_client_errors: bool = False,
) -> AsyncIterator[str]:
    """Stream a minimal Anthropic-shaped turn for a selected local server tool.

    When `ENABLE_WEB_SERVER_TOOLS` is on, this is a proxy-side execution path — not a full
    hosted Anthropic citation or encrypted-content pipeline.
    """
    tool_name = selected_server_tool_name(request)
    if tool_name is None or not has_tool_named(request, tool_name):
        return

    text = forced_tool_turn_text(request)
    message_id = f"msg_{uuid.uuid4()}"
    tool_id = f"srvtoolu_{uuid.uuid4().hex}"
    usage_key = (
        "web_search_requests" if tool_name == "web_search" else "web_fetch_requests"
    )
    tool_input = (
        {"query": extract_query(text)}
        if tool_name == "web_search"
        else {"url": extract_url(text)}
    )
    _result_block_for_tool = {
        "web_search": WEB_SEARCH_TOOL_RESULT,
        "web_fetch": WEB_FETCH_TOOL_RESULT,
    }
    _error_payload_type_for_tool = {
        "web_search": WEB_SEARCH_TOOL_RESULT_ERROR,
        "web_fetch": WEB_FETCH_TOOL_ERROR,
    }

    yield format_sse_event(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": request.model,
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": input_tokens, "output_tokens": 1},
            },
        },
    )
    yield format_sse_event(
        "content_block_start",
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {
                "type": SERVER_TOOL_USE,
                "id": tool_id,
                "name": tool_name,
                "input": tool_input,
            },
        },
    )
    yield format_sse_event(
        "content_block_stop", {"type": "content_block_stop", "index": 0}
    )

    try:
        if tool_name == "web_search":
            query = str(tool_input["query"])
            # The process-wide one, like every other call site. Building a
            # fresh ``Settings`` here read and validated the whole environment
            # on the event loop for every web search -- 46.5 ms on a
            # 384-key configuration, paid while other requests waited.
            settings = get_settings()
            search_options = web_search_tool_options(request)
            if search_options.max_uses is not None and search_options.max_uses < 1:
                raise _MaxUsesExceeded(
                    f"web_search max_uses is {search_options.max_uses}"
                )
            results = await outbound._run_web_search(
                query,
                settings,
                allowed_domains=search_options.allowed_domains,
                blocked_domains=search_options.blocked_domains,
            )
            result_content: Any = [
                _web_search_result_block(result) for result in results
            ]
            summary = _search_summary(query, results, settings)
            result_block_type = WEB_SEARCH_TOOL_RESULT
        else:
            fetched = await outbound._run_web_fetch(
                str(tool_input["url"]), web_fetch_egress
            )
            result_content = {
                "type": "web_fetch_result",
                "url": fetched["url"],
                "content": {
                    "type": "document",
                    "source": {
                        "type": "text",
                        "media_type": fetched["media_type"],
                        "data": fetched["data"],
                    },
                    "title": fetched["title"],
                    "citations": {"enabled": True},
                },
                "retrieved_at": datetime.now(UTC).isoformat(),
            }
            summary = fetched["data"][:_MAX_FETCH_CHARS]
            result_block_type = WEB_FETCH_TOOL_RESULT
    except Exception as error:
        fetch_url = str(tool_input["url"]) if tool_name == "web_fetch" else None
        outbound._log_web_tool_failure(tool_name, error, fetch_url=fetch_url)
        result_block_type = _result_block_for_tool[tool_name]
        result_content = {
            "type": _error_payload_type_for_tool[tool_name],
            "error_code": (
                _web_search_error_code(error)
                if tool_name == "web_search"
                else "unavailable"
            ),
        }
        summary = outbound._web_tool_client_error_summary(
            tool_name, error, verbose=verbose_client_errors
        )

    output_tokens = max(1, len(summary) // 4)

    yield format_sse_event(
        "content_block_start",
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {
                "type": result_block_type,
                "tool_use_id": tool_id,
                "content": result_content,
            },
        },
    )
    yield format_sse_event(
        "content_block_stop", {"type": "content_block_stop", "index": 1}
    )
    # Model-facing summary: stream as normal text deltas (CLI/transcript code reads `text_delta`,
    # not eager `text` on `content_block_start`).
    yield format_sse_event(
        "content_block_start",
        {
            "type": "content_block_start",
            "index": 2,
            "content_block": {"type": "text", "text": ""},
        },
    )
    yield format_sse_event(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": 2,
            "delta": {"type": "text_delta", "text": summary},
        },
    )
    yield format_sse_event(
        "content_block_stop", {"type": "content_block_stop", "index": 2}
    )
    yield format_sse_event(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "server_tool_use": {usage_key: 1},
            },
        },
    )
    yield format_sse_event("message_stop", {"type": "message_stop"})
