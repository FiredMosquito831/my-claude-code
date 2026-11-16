"""Emit Gemini ``groundingMetadata``, ``urlContextMetadata``, JSON, and SSE from the web tool loop."""

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any
from urllib.parse import urlparse

from .events import finish_reason_for, format_gemini_sse_data
from .response import function_call_part, parts_from_anthropic_blocks
from .usage import GeminiUsageLedger


def collect_gemini_final_text(final_step_blocks: Sequence[Mapping[str, Any]]) -> str:
    """Concatenate final assistant text blocks emitted after the web tool loop."""
    return "".join(
        str(block.get("text") or "")
        for block in final_step_blocks
        if block.get("type") == "text"
    )


def _normalize_url(url: str) -> str:
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


def build_gemini_grounding_metadata(
    *,
    web_calls: Sequence[Any],
    gathered_sources: Sequence[Any],
    grounded_citations: Sequence[Mapping[str, Any]],
    full_text: str,
) -> dict[str, Any] | None:
    """Build honest ``groundingMetadata`` from executed search calls and verified citations.

    Per IV.16, never fabricates ``confidenceScores`` or ``searchEntryPoint`` HTML chips,
    and returns ``None`` when no search executed.
    """
    queries: list[str] = []
    seen_queries: set[str] = set()
    for call in web_calls:
        if getattr(call, "tool_kind", None) != "web_search":
            continue
        action = getattr(call, "action", None) or {}
        query = str(action.get("query") or "").strip()
        if query and query not in seen_queries:
            seen_queries.add(query)
            queries.append(query)

    search_sources = [
        src
        for src in gathered_sources
        if getattr(src, "source_kind", "search") == "search"
    ]
    if not queries and not search_sources:
        return None

    grounding_chunks: list[dict[str, Any]] = []
    chunk_index_by_url: dict[str, int] = {}

    def _add_chunk(url: str, title: str) -> int | None:
        norm = _normalize_url(url)
        if not norm:
            return None
        if norm in chunk_index_by_url:
            return chunk_index_by_url[norm]
        idx = len(grounding_chunks)
        chunk_index_by_url[norm] = idx
        grounding_chunks.append({"web": {"uri": url, "title": title or url}})
        return idx

    for src in search_sources:
        url = str(getattr(src, "url", "") or "").strip()
        title = str(getattr(src, "title", "") or url)
        if url:
            _add_chunk(url, title)

    source_titles: dict[str, tuple[str, str]] = {}
    for src in gathered_sources:
        url = str(getattr(src, "url", "") or "").strip()
        norm = _normalize_url(url)
        if norm and norm not in source_titles:
            source_titles[norm] = (url, str(getattr(src, "title", "") or url))

    grounding_supports: list[dict[str, Any]] = []
    for citation in grounded_citations:
        raw_url = str(citation.get("url") or "").strip()
        norm = _normalize_url(raw_url)
        if not norm:
            continue
        chunk_idx = chunk_index_by_url.get(norm)
        if chunk_idx is None and norm in source_titles:
            src_url, src_title = source_titles[norm]
            chunk_idx = _add_chunk(src_url, src_title)
        if chunk_idx is None:
            continue
        start = int(citation.get("start_index") or 0)
        end = int(citation.get("end_index") or 0)
        if not (0 <= start < end <= len(full_text)):
            continue
        seg_text = full_text[start:end]
        if not seg_text:
            continue
        grounding_supports.append(
            {
                "segment": {
                    "startIndex": start,
                    "endIndex": end,
                    "text": seg_text,
                },
                "groundingChunkIndices": [chunk_idx],
            }
        )

    metadata: dict[str, Any] = {}
    if queries:
        metadata["webSearchQueries"] = queries
    if grounding_chunks:
        metadata["groundingChunks"] = grounding_chunks
    if grounding_supports:
        metadata["groundingSupports"] = grounding_supports
    return metadata or None


def build_gemini_url_context_metadata(
    web_calls: Sequence[Any],
) -> dict[str, Any] | None:
    """Build ``urlContextMetadata`` from executed ``web_fetch`` calls, or ``None``."""
    entries: list[dict[str, str]] = []
    for call in web_calls:
        if getattr(call, "tool_kind", None) != "web_fetch":
            continue
        action = getattr(call, "action", None) or {}
        url = str(action.get("url") or "").strip()
        if not url:
            continue
        status = getattr(call, "status", "failed")
        error_code = getattr(call, "error_code", None)
        if status == "completed":
            retrieval_status = "URL_RETRIEVAL_STATUS_SUCCESS"
        elif error_code in ("ssrf_blocked", "url_not_allowed"):
            retrieval_status = "URL_RETRIEVAL_STATUS_UNSAFE"
        else:
            retrieval_status = "URL_RETRIEVAL_STATUS_ERROR"
        entries.append({"retrievedUrl": url, "urlRetrievalStatus": retrieval_status})
    if not entries:
        return None
    return {"urlMetadata": entries}


def _metadata_for_loop(
    *,
    web_calls: Sequence[Any],
    gathered_sources: Sequence[Any],
    grounded_citations: Sequence[Mapping[str, Any]],
    full_text: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    grounding = build_gemini_grounding_metadata(
        web_calls=web_calls,
        gathered_sources=gathered_sources,
        grounded_citations=grounded_citations,
        full_text=full_text,
    )
    url_context = build_gemini_url_context_metadata(web_calls)
    return grounding, url_context


def build_gemini_payload_from_loop(
    *,
    model: str,
    response_id: str,
    include_thoughts: bool,
    stop_reason: str | None,
    web_calls: Sequence[Any],
    gathered_sources: Sequence[Any],
    final_step_blocks: Sequence[Mapping[str, Any]],
    grounded_citations: Sequence[Mapping[str, Any]],
    loop_usage: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a complete non-streaming Gemini ``GenerateContentResponse`` from loop output."""
    usage = GeminiUsageLedger()
    parts = parts_from_anthropic_blocks(
        final_step_blocks,
        usage=usage,
        include_thoughts=include_thoughts,
    )
    usage.absorb(loop_usage)

    full_text = collect_gemini_final_text(final_step_blocks)
    grounding, url_context = _metadata_for_loop(
        web_calls=web_calls,
        gathered_sources=gathered_sources,
        grounded_citations=grounded_citations,
        full_text=full_text,
    )

    candidate: dict[str, Any] = {
        "content": {"role": "model", "parts": parts},
        "index": 0,
        "finishReason": finish_reason_for(stop_reason),
    }
    if grounding is not None:
        candidate["groundingMetadata"] = grounding
    if url_context is not None:
        candidate["urlContextMetadata"] = url_context

    return {
        "candidates": [candidate],
        "usageMetadata": usage.payload(),
        "modelVersion": model,
        "responseId": response_id,
    }


async def iter_gemini_sse_from_loop(
    *,
    model: str,
    response_id: str,
    include_thoughts: bool,
    stop_reason: str | None,
    web_calls: Sequence[Any],
    gathered_sources: Sequence[Any],
    final_step_blocks: Sequence[Mapping[str, Any]],
    grounded_citations: Sequence[Mapping[str, Any]],
    loop_usage: Mapping[str, Any],
) -> AsyncIterator[str]:
    """Yield Gemini ``?alt=sse`` frames for a completed web tool loop (never emits ``[DONE]``)."""
    usage = GeminiUsageLedger()
    usage.absorb(loop_usage)
    for block in final_step_blocks:
        if block.get("type") == "thinking":
            thought = str(block.get("thinking") or "")
            if thought:
                usage.add_thought_text(thought)

    def _chunk(
        parts: list[dict[str, Any]],
        *,
        finish_reason: str | None = None,
        grounding: dict[str, Any] | None = None,
        url_context: dict[str, Any] | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "responseId": response_id,
            "modelVersion": model,
        }
        if usage.has_counts():
            payload["usageMetadata"] = usage.payload()
        candidate: dict[str, Any] = {
            "content": {"role": "model", "parts": parts},
            "index": 0,
        }
        if finish_reason is not None:
            candidate["finishReason"] = finish_reason
        if grounding is not None:
            candidate["groundingMetadata"] = grounding
        if url_context is not None:
            candidate["urlContextMetadata"] = url_context
        payload["candidates"] = [candidate]
        return format_gemini_sse_data(payload)

    for block in final_step_blocks:
        btype = block.get("type")
        if btype == "thinking":
            thought = str(block.get("thinking") or "")
            if thought and include_thoughts:
                yield _chunk([{"text": thought, "thought": True}])
        elif btype == "text":
            text = str(block.get("text") or "")
            if text:
                yield _chunk([{"text": text}])
        elif btype == "tool_use":
            yield _chunk([function_call_part(block)])

    full_text = collect_gemini_final_text(final_step_blocks)
    grounding, url_context = _metadata_for_loop(
        web_calls=web_calls,
        gathered_sources=gathered_sources,
        grounded_citations=grounded_citations,
        full_text=full_text,
    )
    yield _chunk(
        [],
        finish_reason=finish_reason_for(stop_reason),
        grounding=grounding,
        url_context=url_context,
    )
