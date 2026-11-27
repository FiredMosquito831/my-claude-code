"""ChatGPT OAuth Codex Responses ``image_generation`` tool as a media upstream.

ChatGPT OAuth has no ``/v1/images/generations`` or ``/v1/images/edits``. It
serves image generation and editing through ``POST /codex/responses`` with
``stream: True``, ``store: False``, and ``tools: [{"type": "image_generation", ...}]``,
streaming SSE frames carrying ``image_generation_call`` items with base64
``result`` payloads.

Pure and synchronous: callers run base64 / JSON / SSE parsing off the event
loop through ``asyncio.to_thread``. Imports nothing outside the stdlib so
both ``application`` and ``providers`` can use it cleanly.
"""

import base64
import binascii
import json
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

#: Optional ``image_generation`` tool parameters documented on Responses API.
CHATGPT_OAUTH_IMAGE_TOOL_OPTIONS: tuple[str, ...] = (
    "size",
    "quality",
    "background",
    "output_format",
    "moderation",
)


class ChatGPTOAuthMediaAnswerError(ValueError):
    """The Codex Responses answer holds nothing this image request can serve.

    The message completes "<provider> answered ...", e.g. "without an image".
    """


@dataclass(frozen=True, slots=True)
class ChatGPTOAuthImageAnswer:
    """A Codex Responses image answer translated to OpenAI Images JSON."""

    body: bytes
    content_type: str = "application/json"
    usage: dict[str, Any] | None = None
    text: str | None = None


def chatgpt_oauth_image_request(
    prompt: str,
    model: str,
    *,
    instructions: str,
    image_urls: Sequence[str] = (),
    tool_options: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Build a Codex ``/codex/responses`` JSON payload requesting image output.

    ``instructions`` is passed in rather than imported from ``application`` so
    ``core`` stays dependency-free (``tests/contracts/test_import_boundaries.py``).
    """

    tool: dict[str, Any] = {"type": "image_generation"}
    if tool_options:
        for key in CHATGPT_OAUTH_IMAGE_TOOL_OPTIONS:
            val = tool_options.get(key)
            if isinstance(val, str) and val.strip():
                tool[key] = val.strip()

    content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
    content.extend(
        {"type": "input_image", "image_url": url.strip()}
        for url in image_urls
        if isinstance(url, str) and url.strip()
    )

    return {
        "model": model,
        "store": False,
        "stream": True,
        "instructions": instructions,
        "tools": [tool],
        "tool_choice": {"type": "image_generation"},
        "input": [{"role": "user", "content": content}],
    }


def _mapping(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {str(key): item for key, item in value.items()}


def _usage(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    block = _mapping(payload.get("usage"))
    if block is None:
        return None
    usage = dict(block)
    in_tok = block.get("input_tokens")
    out_tok = block.get("output_tokens")
    if (
        isinstance(in_tok, int)
        and not isinstance(in_tok, bool)
        and isinstance(out_tok, int)
        and not isinstance(out_tok, bool)
    ):
        total = block.get("total_tokens")
        if not isinstance(total, int) or isinstance(total, bool):
            usage["total_tokens"] = in_tok + out_tok
    return usage


def _extract_message_text(item: Mapping[str, Any]) -> str:
    content = item.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        part_map = _mapping(part)
        if part_map is None:
            continue
        text = part_map.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
    return "".join(parts)


def _error_message(event: Mapping[str, Any]) -> str:
    err = event.get("error")
    if isinstance(err, Mapping):
        msg = err.get("message")
        if isinstance(msg, str) and msg.strip():
            return msg.strip()
        code = err.get("code")
        if isinstance(code, str) and code.strip():
            return code.strip()
    elif isinstance(err, str) and err.strip():
        return err.strip()
    msg = event.get("message")
    if isinstance(msg, str) and msg.strip():
        return msg.strip()
    return ""


def _clean_b64(raw_b64: Any) -> str | None:
    if not isinstance(raw_b64, str):
        return None
    cleaned = re.sub(r"\s+", "", raw_b64)
    if not cleaned:
        return None
    try:
        decoded = base64.b64decode(cleaned, validate=True)
    except binascii.Error, ValueError:
        return None
    if not decoded:
        return None
    return cleaned


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


def parse_chatgpt_oauth_image_answer(
    raw: bytes, *, created: int | None = None
) -> ChatGPTOAuthImageAnswer:
    """Parse Codex ``/codex/responses`` SSE events into OpenAI Images JSON.

    Extracts ``image_generation_call`` items from ``response.output_item.done``
    or terminal ``response.completed`` / ``response.done`` ``output[]`` carrying
    non-empty base64 ``result`` payloads, plus terminal ``usage``. Raises
    :class:`ChatGPTOAuthMediaAnswerError` on upstream ``error`` / ``failed``
    or when no generated image was returned.
    """

    events = _iter_sse_events(raw)
    items_by_id: dict[str, dict[str, Any]] = {}
    item_order: list[str] = []
    text_parts: list[str] = []
    usage: dict[str, Any] | None = None
    response_created: int | None = None
    terminal_status: str | None = None

    def _record_image_item(item: dict[str, Any], fallback_key: str) -> None:
        raw_id = item.get("id")
        key = (
            raw_id.strip()
            if isinstance(raw_id, str) and raw_id.strip()
            else fallback_key
        )
        if key not in items_by_id:
            item_order.append(key)
            items_by_id[key] = dict(item)
        else:
            merged = {
                **items_by_id[key],
                **{k: v for k, v in item.items() if v is not None and v != ""},
            }
            items_by_id[key] = merged

    for event in events:
        event_type = str(event.get("type") or "")
        if event_type == "error":
            detail = _error_message(event)
            raise ChatGPTOAuthMediaAnswerError(
                f"with an upstream error: {detail}"
                if detail
                else "with an upstream error"
            )
        if event_type == "response.output_item.done":
            item = _mapping(event.get("item"))
            if item is None:
                continue
            item_type = item.get("type")
            if item_type == "image_generation_call":
                _record_image_item(item, f"__done_{len(item_order)}")
            elif item_type == "message":
                msg_text = _extract_message_text(item)
                if msg_text:
                    text_parts.append(msg_text)
            continue
        if event_type == "response.output_text.done":
            done_text = event.get("text")
            if isinstance(done_text, str) and done_text and not text_parts:
                text_parts.append(done_text)
            continue
        if event_type in (
            "response.completed",
            "response.done",
            "response.failed",
            "response.incomplete",
        ):
            resp = _mapping(event.get("response")) or event
            status_val = resp.get("status")
            status_lower = (
                status_val.strip().lower() if isinstance(status_val, str) else ""
            )
            if event_type == "response.failed" or status_lower == "failed":
                detail = _error_message(resp) or _error_message(event)
                raise ChatGPTOAuthMediaAnswerError(
                    f"with a failed response: {detail}"
                    if detail
                    else "with a failed response"
                )
            if event_type == "response.incomplete" or status_lower == "incomplete":
                terminal_status = "incomplete"
            raw_created = resp.get("created_at")
            if isinstance(raw_created, int) and not isinstance(raw_created, bool):
                response_created = raw_created
            parsed_usage = _usage(resp)
            if parsed_usage is not None:
                usage = parsed_usage
            output = resp.get("output")
            if isinstance(output, list):
                for idx, raw_item in enumerate(output):
                    item = _mapping(raw_item)
                    if item is None:
                        continue
                    item_type = item.get("type")
                    if item_type == "image_generation_call":
                        _record_image_item(item, f"__out_{idx}")
                    elif item_type == "message" and not text_parts:
                        msg_text = _extract_message_text(item)
                        if msg_text:
                            text_parts.append(msg_text)

    data_items: list[dict[str, Any]] = []
    top_meta: dict[str, str] = {}
    for key in item_order:
        item = items_by_id[key]
        if str(item.get("status") or "").strip().lower() == "failed":
            continue
        cleaned_b64 = _clean_b64(item.get("result"))
        if cleaned_b64 is None:
            continue
        entry: dict[str, Any] = {"b64_json": cleaned_b64}
        revised = item.get("revised_prompt")
        if isinstance(revised, str) and revised.strip():
            entry["revised_prompt"] = revised.strip()
        data_items.append(entry)
        for meta_key in ("size", "quality", "background", "output_format"):
            meta_val = item.get(meta_key)
            if (
                meta_key not in top_meta
                and isinstance(meta_val, str)
                and meta_val.strip()
            ):
                top_meta[meta_key] = meta_val.strip()

    text = "\n".join(part for part in text_parts if part).strip() or None
    if not data_items:
        if terminal_status == "incomplete":
            raise ChatGPTOAuthMediaAnswerError("ended incomplete without an image")
        if text:
            snippet = text[:200]
            raise ChatGPTOAuthMediaAnswerError(f"without an image ({snippet})")
        raise ChatGPTOAuthMediaAnswerError("without an image")

    effective_created = (
        created
        if created is not None
        else (response_created if response_created is not None else int(time.time()))
    )
    out: dict[str, Any] = {"created": effective_created, "data": data_items}
    out.update(top_meta)
    if usage is not None:
        out["usage"] = usage
    return ChatGPTOAuthImageAnswer(
        body=json.dumps(out, separators=(",", ":")).encode("utf-8"),
        usage=usage,
        text=text,
    )
