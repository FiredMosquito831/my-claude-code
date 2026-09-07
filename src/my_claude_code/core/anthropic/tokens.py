"""Token estimation for Anthropic-compatible requests."""

import json

from loguru import logger

from my_claude_code.core.image_geometry import image_dimensions
from my_claude_code.core.token_encoder import cl100k_encoder

from .content import get_block_attr
from .image_tokens import ImageTokenFamily, image_tokens
from .models import Message, SystemContent, Tool
from .tool_result_media import split_tool_result_media

_DISALLOWED_SPECIAL: tuple[str, ...] = ()


def _image_tokens(block: object, family: str | ImageTokenFamily) -> int:
    """Cost of one image or document block, wherever it appears.

    Factored out so the top-level branch and the tool-result branch share the
    arithmetic instead of coinciding by luck. Before 6.49.0 they did not: the
    same 213 KB screenshot scored 99 tokens pasted and 204,216 tokens inside a
    tool result, because the second one was measured as JSON text. That number
    is what the dashboard shows, what the context-headroom arithmetic uses, and
    what a cancelled request records, so the disagreement was not cosmetic.

    Since 6.53.0 the arithmetic is the destination family's own published
    formula applied to the image's real pixel dimensions, which is what every
    provider actually bills on. The old ``len(base64) // 3000`` survives only
    as the unreadable-image fallback: a URL-referenced image, a truncated
    upload, a PDF sent as a document. The invented ``765`` for a block with no
    inlined data is gone -- it corresponded to no published number anywhere,
    and a block whose bytes we never saw is now charged the same 85-token floor
    every other unmeasurable image gets.
    """
    source = get_block_attr(block, "source")
    if isinstance(source, dict):
        data = source.get("data") or source.get("base64") or ""
        if data:
            size = image_dimensions(str(data))
            if size is not None:
                return image_tokens(size[0], size[1], family)
            return max(85, len(data) // 3000)
    return 85


def count_text_tokens(text: str) -> int:
    """Token count of a bare string, with no per-message framing added.

    ``get_token_count`` bills a fixed overhead per message, which is right for
    a request and wrong for a lone reply -- it scores the empty string at 4.
    """
    encoder = cl100k_encoder()
    if encoder is None:
        # Before 6.41.2 this module built the encoder at import, so a broken
        # tiktoken stopped the server outright. Counting is an estimate, so
        # fall back to the same 4-chars-per-token rule the OpenAI usage
        # estimator already uses rather than failing a live request.
        return max(1, len(text) // 4)
    return len(encoder.encode(text, disallowed_special=_DISALLOWED_SPECIAL))


def get_token_count(
    messages: list[Message],
    system: str | list[SystemContent] | None = None,
    tools: list[Tool] | None = None,
    image_token_family: str | ImageTokenFamily = ImageTokenFamily.UNKNOWN,
) -> int:
    """Estimate token count for a request.

    ``image_token_family`` is how the destination host bills a picture, as
    declared on its provider descriptor. It is positional-or-keyword and
    defaults to ``UNKNOWN`` -- which charges Anthropic's rate -- so every
    existing caller keeps working and every caller that knows the destination
    can say so. It is deliberately *not* derived from the request's ``detail``
    field: that field does not exist in the Anthropic protocol at all, and
    keying the estimate on it is the bug LiteLLM ships.
    """
    total_tokens = 0

    if system:
        if isinstance(system, str):
            total_tokens += count_text_tokens(system)
        elif isinstance(system, list):
            for block in system:
                text = get_block_attr(block, "text", "")
                if text:
                    total_tokens += count_text_tokens(str(text))
        total_tokens += 4

    for msg in messages:
        if isinstance(msg.content, str):
            total_tokens += count_text_tokens(msg.content)
        elif isinstance(msg.content, list):
            for block in msg.content:
                b_type = get_block_attr(block, "type") or None

                if b_type == "text":
                    text = get_block_attr(block, "text", "")
                    total_tokens += count_text_tokens(str(text))
                elif b_type == "thinking":
                    thinking = get_block_attr(block, "thinking", "")
                    total_tokens += count_text_tokens(str(thinking))
                elif b_type == "tool_use":
                    name = get_block_attr(block, "name", "")
                    inp = get_block_attr(block, "input", {})
                    block_id = get_block_attr(block, "id", "")
                    total_tokens += count_text_tokens(str(name))
                    total_tokens += count_text_tokens(json.dumps(inp))
                    total_tokens += count_text_tokens(str(block_id))
                    total_tokens += 15
                elif b_type in ("image", "document"):
                    total_tokens += _image_tokens(block, image_token_family)
                elif b_type == "tool_result":
                    raw = get_block_attr(block, "content", "")
                    content, media = split_tool_result_media(raw)
                    tool_use_id = get_block_attr(block, "tool_use_id", "")
                    if isinstance(content, str):
                        total_tokens += count_text_tokens(content)
                    else:
                        total_tokens += count_text_tokens(
                            json.dumps(content, default=str)
                        )
                    for item in media:
                        total_tokens += _image_tokens(item, image_token_family)
                    total_tokens += count_text_tokens(str(tool_use_id))
                    total_tokens += 8
                elif b_type in (
                    "server_tool_use",
                    "web_search_tool_result",
                    "web_fetch_tool_result",
                ):
                    if hasattr(block, "model_dump"):
                        blob: object = block.model_dump()
                    else:
                        blob = block
                    try:
                        total_tokens += count_text_tokens(
                            json.dumps(blob, default=str, ensure_ascii=False)
                        )
                    except (TypeError, ValueError, OverflowError) as e:
                        logger.debug(
                            "Block encode fallback b_type={} err={}", b_type, e
                        )
                        total_tokens += count_text_tokens(str(blob))
                    total_tokens += 12
                else:
                    logger.debug(
                        "Unexpected block type %r, falling back to json/str encoding",
                        b_type,
                    )
                    try:
                        total_tokens += count_text_tokens(json.dumps(block))
                    except TypeError, ValueError:
                        total_tokens += count_text_tokens(str(block))

    if tools:
        for tool in tools:
            tool_str = (
                tool.name + (tool.description or "") + json.dumps(tool.input_schema)
            )
            total_tokens += count_text_tokens(tool_str)

    total_tokens += len(messages) * 4
    if tools:
        total_tokens += len(tools) * 5

    return max(1, total_tokens)
