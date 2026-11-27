"""Pure ChatGPT OAuth Codex Responses web search request & SSE parser.

ChatGPT OAuth has no standalone search REST endpoint. When opted in via
``CHATGPT_OAUTH_WEBSEARCH_ENABLED=true``, standalone ``/v1/websearch`` and
``WEB_SEARCH_CHAIN`` hops targeting ``chatgpt_oauth`` send
``POST {base_url}/codex/responses`` with ``store=False``, ``stream=True``,
``tools=[{"type": "web_search", ...}]``, and ``tool_choice={"type": "web_search"}``,
then extract ``url_citation`` annotations, ``web_search_call`` sources, and the
assistant answer text into normalized :class:`WebSearchResultItem` entries.

Stdlib-only and synchronous so both ``websearch`` and ``core`` can use it
without transport or circular dependencies.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from my_claude_code.core.websearch.models import WebSearchResultItem

CODEX_BASE_INSTRUCTIONS = (
    "You are Codex, a coding agent. Help the user understand, modify, test, "
    "and review code in their workspace. Follow the user's instructions, use "
    "tools when needed, and communicate concise progress and verification."
)

CHATGPT_OAUTH_DEFAULT_BASE = "https://chatgpt.com/backend-api"
CHATGPT_OAUTH_WEBSEARCH_DEFAULT_MODEL = "gpt-5.4"
CHATGPT_OAUTH_ZSTD_THRESHOLD_BYTES = 786_432
CHATGPT_OAUTH_ZSTD_LEVEL = 3

_SNIPPET_MAX_CHARS = 320
_SNIPPET_CONTEXT_BEFORE = 80
_SNIPPET_CONTEXT_AFTER = 180


class ChatGPTOAuthWebSearchAnswerError(ValueError):
    """Raised when a Codex Responses SSE stream reports an upstream failure."""

    def __init__(
        self,
        message: str,
        *,
        error_kind: str = "upstream",
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.error_kind = error_kind
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class ChatGPTOAuthWebSearchAnswer:
    """Normalized search results and answer parsed from Codex Responses SSE."""

    results: tuple[WebSearchResultItem, ...]
    answer: str | None = None
    request_id: str | None = None


def normalize_chatgpt_oauth_base_url(raw_base_url: str | None) -> str:
    """Normalize Codex OAuth base URL, stripping any trailing ``/codex``."""

    base_url = (raw_base_url or CHATGPT_OAUTH_DEFAULT_BASE).strip().rstrip("/")
    if base_url.endswith("/codex"):
        base_url = base_url[: -len("/codex")]
    return base_url or CHATGPT_OAUTH_DEFAULT_BASE


def build_chatgpt_oauth_websearch_payload(
    query: str,
    *,
    model: str = CHATGPT_OAUTH_WEBSEARCH_DEFAULT_MODEL,
    instructions: str = CODEX_BASE_INSTRUCTIONS,
    allowed_domains: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Build a Codex ``/codex/responses`` JSON payload for ``web_search``."""

    tool: dict[str, Any] = {"type": "web_search"}
    cleaned_domains = [
        domain.strip()
        for domain in allowed_domains
        if isinstance(domain, str) and domain.strip()
    ]
    if cleaned_domains:
        tool["filters"] = {"allowed_domains": cleaned_domains}

    effective_model = model.strip() if isinstance(model, str) else ""
    if not effective_model:
        effective_model = CHATGPT_OAUTH_WEBSEARCH_DEFAULT_MODEL

    return {
        "model": effective_model,
        "instructions": instructions,
        "store": False,
        "stream": True,
        "tools": [tool],
        "tool_choice": {"type": "web_search"},
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": query}],
            }
        ],
    }


def _mapping(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {str(key): item for key, item in value.items()}


def _clean_str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _iter_sse_events(raw: bytes) -> list[dict[str, Any]]:
    text = raw.decode("utf-8", errors="replace")
    events: list[dict[str, Any]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or not line.startswith("data:"):
            continue
        payload = line[5:].lstrip(" ").strip()
        if not payload:
            continue
        if payload == "[DONE]":
            break
        try:
            parsed = json.loads(payload)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            events.append({str(k): v for k, v in parsed.items()})
    return events


def _classify_sse_error(container: Mapping[str, Any]) -> tuple[str, str]:
    err = container.get("error")
    err_map = _mapping(err)
    code = _clean_str(err_map.get("code")) if err_map else ""
    msg = (
        _clean_str(err_map.get("message"))
        if err_map
        else (_clean_str(err) or _clean_str(container.get("message")))
    )
    detail = msg or code or "upstream stream error"
    lower = f"{code} {detail}".lower()
    if any(
        token in lower
        for token in ("rate_limit", "rate limit", "too_many_requests", "too many")
    ):
        return "rate_limit", detail
    if any(
        token in lower
        for token in ("insufficient_quota", "quota", "usage_limit", "usage limit")
    ):
        return "quota", detail
    if any(
        token in lower
        for token in ("unauthorized", "authentication", "invalid_api_key")
    ):
        return "auth", detail
    return "upstream", detail


def _citation_snippet(part_text: str, ann: Mapping[str, Any]) -> str:
    explicit = _clean_str(ann.get("snippet")) or _clean_str(ann.get("cited_text"))
    if explicit:
        return explicit[:_SNIPPET_MAX_CHARS]
    if not part_text:
        return ""
    start = ann.get("start_index")
    end = ann.get("end_index")
    if (
        isinstance(start, int)
        and not isinstance(start, bool)
        and isinstance(end, int)
        and not isinstance(end, bool)
        and 0 <= start <= end <= len(part_text)
    ):
        lo = max(0, start - _SNIPPET_CONTEXT_BEFORE)
        hi = min(len(part_text), end + _SNIPPET_CONTEXT_AFTER)
        excerpt = " ".join(part_text[lo:hi].split())
        if len(excerpt) > _SNIPPET_MAX_CHARS:
            return excerpt[:_SNIPPET_MAX_CHARS].rstrip()
        return excerpt
    return ""


def _domain_matches(host: str, blocked: tuple[str, ...]) -> bool:
    if not host or not blocked:
        return False
    norm_host = host.lower().lstrip(".")
    for raw_rule in blocked:
        rule = raw_rule.strip().lower().lstrip(".")
        if not rule:
            continue
        if norm_host == rule or norm_host.endswith(f".{rule}"):
            return True
    return False


def _is_blocked_url(url: str, blocked_domains: tuple[str, ...]) -> bool:
    if not blocked_domains:
        return False
    try:
        host = (urlsplit(url).hostname or "").strip().lower()
    except ValueError:
        return False
    return _domain_matches(host, blocked_domains)


def parse_chatgpt_oauth_websearch_sse(
    raw: bytes,
    *,
    query: str,
    max_results: int = 10,
    blocked_domains: tuple[str, ...] = (),
) -> ChatGPTOAuthWebSearchAnswer:
    """Parse Codex ``/codex/responses`` SSE events into normalized search results."""

    del query  # kept for symmetric caller context / diagnostics
    events = _iter_sse_events(raw)
    if not events:
        raise ChatGPTOAuthWebSearchAnswerError(
            "empty or unparseable SSE response from Codex Responses"
        )

    items_by_url: dict[str, WebSearchResultItem] = {}
    url_order: list[str] = []
    delta_parts: list[str] = []
    done_text_parts: list[str] = []
    message_text_parts: list[str] = []
    request_id: str | None = None

    def _upsert(url: str, *, title: str = "", snippet: str = "") -> None:
        cleaned_url = url.strip()
        if not cleaned_url:
            return
        if _is_blocked_url(cleaned_url, blocked_domains):
            return
        cleaned_title = title.strip()
        cleaned_snippet = snippet.strip()
        existing = items_by_url.get(cleaned_url)
        if existing is None:
            url_order.append(cleaned_url)
            items_by_url[cleaned_url] = WebSearchResultItem(
                title=cleaned_title or cleaned_url,
                url=cleaned_url,
                snippet=cleaned_snippet,
                content=None,
                published=None,
            )
            return
        updated_title = existing.title
        if existing.title == existing.url and cleaned_title:
            updated_title = cleaned_title
        updated_snippet = existing.snippet or cleaned_snippet
        if updated_title != existing.title or updated_snippet != existing.snippet:
            items_by_url[cleaned_url] = WebSearchResultItem(
                title=updated_title,
                url=cleaned_url,
                snippet=updated_snippet,
                content=existing.content,
                published=existing.published,
            )

    def _collect_message_item(item: Mapping[str, Any]) -> str:
        content = item.get("content")
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return ""
        parts: list[str] = []
        for raw_part in content:
            part = _mapping(raw_part)
            if part is None:
                continue
            raw_text = part.get("text")
            part_text = raw_text if isinstance(raw_text, str) else ""
            if part_text:
                parts.append(part_text)
            annotations = part.get("annotations")
            if isinstance(annotations, list):
                for raw_ann in annotations:
                    ann = _mapping(raw_ann)
                    if ann is None or ann.get("type") != "url_citation":
                        continue
                    url = _clean_str(ann.get("url"))
                    if not url:
                        continue
                    _upsert(
                        url,
                        title=_clean_str(ann.get("title")),
                        snippet=_citation_snippet(part_text, ann),
                    )
        return "".join(parts)

    def _collect_search_call_item(item: Mapping[str, Any]) -> None:
        action = _mapping(item.get("action"))
        if action is None:
            return
        action_url = _clean_str(action.get("url"))
        if action_url:
            _upsert(action_url, title=_clean_str(action.get("title")))
        sources = action.get("sources")
        if isinstance(sources, list):
            for raw_src in sources:
                src = _mapping(raw_src)
                if src is None:
                    continue
                src_url = _clean_str(src.get("url"))
                if not src_url:
                    continue
                _upsert(
                    src_url,
                    title=_clean_str(src.get("title")),
                    snippet=_clean_str(src.get("snippet"))
                    or _clean_str(src.get("description")),
                )

    for event in events:
        event_type = str(event.get("type") or "")
        if event_type == "error":
            kind, detail = _classify_sse_error(event)
            raise ChatGPTOAuthWebSearchAnswerError(detail, error_kind=kind)
        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str) and delta:
                delta_parts.append(delta)
            continue
        if event_type == "response.output_text.done":
            done_text = event.get("text")
            if isinstance(done_text, str) and done_text:
                done_text_parts.append(done_text)
            continue
        if event_type == "response.output_text.annotation.added":
            ann = _mapping(event.get("annotation"))
            if ann is not None and ann.get("type") == "url_citation":
                url = _clean_str(ann.get("url"))
                if url:
                    _upsert(
                        url,
                        title=_clean_str(ann.get("title")),
                        snippet=_citation_snippet("", ann),
                    )
            continue
        if event_type == "response.output_item.done":
            item = _mapping(event.get("item"))
            if item is None:
                continue
            item_type = item.get("type")
            if item_type == "web_search_call":
                _collect_search_call_item(item)
            elif item_type == "message":
                text = _collect_message_item(item)
                if text:
                    message_text_parts.append(text)
            continue
        if event_type in (
            "response.completed",
            "response.done",
            "response.failed",
        ):
            resp = _mapping(event.get("response")) or event
            resp_id = _clean_str(resp.get("id"))
            if resp_id:
                request_id = resp_id
            status_val = _clean_str(resp.get("status")).lower()
            if event_type == "response.failed" or status_val == "failed":
                kind, detail = _classify_sse_error(resp)
                raise ChatGPTOAuthWebSearchAnswerError(detail, error_kind=kind)
            output = resp.get("output")
            if isinstance(output, list):
                for raw_item in output:
                    item = _mapping(raw_item)
                    if item is None:
                        continue
                    item_type = item.get("type")
                    if item_type == "web_search_call":
                        _collect_search_call_item(item)
                    elif item_type == "message":
                        text = _collect_message_item(item)
                        if text and not message_text_parts:
                            message_text_parts.append(text)

    if message_text_parts:
        answer_text = "\n".join(part for part in message_text_parts if part).strip()
    elif done_text_parts:
        answer_text = "\n".join(part for part in done_text_parts if part).strip()
    else:
        answer_text = "".join(delta_parts).strip()

    results = tuple(items_by_url[url] for url in url_order[:max_results])
    return ChatGPTOAuthWebSearchAnswer(
        results=results,
        answer=answer_text or None,
        request_id=request_id,
    )
