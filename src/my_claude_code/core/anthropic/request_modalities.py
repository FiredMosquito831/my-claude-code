"""Modality inspection of Anthropic protocol requests."""

from dataclasses import dataclass

from .models import (
    ContentBlockDocument,
    ContentBlockImage,
    ContentBlockToolResult,
    MessagesRequest,
)
from .tool_result_media import MAX_MEDIA_NESTING, VISUAL_BLOCK_TYPES

# Both constants live in ``tool_result_media`` so that the walk which *finds*
# an image and the walk which *disposes* of it can never disagree about what
# counts as visual or how deep to look. Aliased rather than re-declared.
_VISUAL_BLOCK_TYPES = VISUAL_BLOCK_TYPES
_MAX_NESTING = MAX_MEDIA_NESTING


@dataclass(frozen=True, slots=True)
class ImageInput:
    """One visual block carried by a request, as sent by the client."""

    kind: str
    media_type: str | None
    # ``base64`` data when the client inlined the bytes, ``None`` for a block
    # that only references a URL or a Files API id -- countable, not readable.
    data: str | None = None
    url: str | None = None

    @property
    def approx_bytes(self) -> int | None:
        """Return the decoded size of inlined data, without decoding it."""
        if self.data is None:
            return None
        # base64 is 4 characters per 3 bytes, minus the padding.
        return len(self.data) // 4 * 3 - self.data.count("=")


def request_image_inputs(request: MessagesRequest) -> tuple[ImageInput, ...]:
    """Return every image or document block a request carries, in order.

    Blocks are found wherever they legally appear, which is *not* only at the
    top level of a message. An image returned by a tool -- reading a PNG, or an
    MCP screenshot -- arrives nested inside that ``tool_result`` block's own
    content, and reaches the model exactly like a pasted one. Scanning only
    top-level blocks made every tool-delivered image invisible to vision
    routing, so those requests were never diverted and never recorded.

    ``system`` is text-or-text-blocks in the Anthropic schema and needs no walk.
    """
    found: list[ImageInput] = []
    for message in request.messages:
        content = message.content
        if isinstance(content, list):
            for block in content:
                _collect_block(block, found, depth=0)
    return tuple(found)


def request_carries_image(request: MessagesRequest) -> bool:
    """Return whether the request carries anything the model must look at."""
    return bool(request_image_inputs(request))


def _collect_block(block: object, found: list[ImageInput], *, depth: int) -> None:
    """Append any visual block, descending into tool results."""
    if depth > _MAX_NESTING:
        return
    if isinstance(block, ContentBlockImage | ContentBlockDocument):
        found.append(_image_input(block.type, block.source))
        return
    if isinstance(block, ContentBlockToolResult):
        _collect_nested(block.content, found, depth=depth + 1)
        return
    # A tool result parsed from an untyped payload stays a plain mapping, and
    # nested blocks are never re-parsed into models, so the dict shape is the
    # common case rather than the exotic one.
    if isinstance(block, dict):
        block_type = block.get("type")
        if block_type in _VISUAL_BLOCK_TYPES:
            source = block.get("source")
            found.append(_image_input(str(block_type), source))
        elif block_type == "tool_result":
            _collect_nested(block.get("content"), found, depth=depth + 1)


def _collect_nested(content: object, found: list[ImageInput], *, depth: int) -> None:
    """Walk the content of a tool result, which may be a block or a list."""
    if isinstance(content, list):
        for nested in content:
            _collect_block(nested, found, depth=depth)
    elif isinstance(content, dict):
        _collect_block(content, found, depth=depth)


def _image_input(kind: str, source: object) -> ImageInput:
    """Read the parts of a block source worth keeping, tolerating any shape."""
    if not isinstance(source, dict):
        return ImageInput(kind=kind, media_type=None)
    media_type = source.get("media_type")
    data = source.get("data")
    url = source.get("url")
    return ImageInput(
        kind=kind,
        media_type=media_type if isinstance(media_type, str) else None,
        data=data if isinstance(data, str) else None,
        url=url if isinstance(url, str) else None,
    )
