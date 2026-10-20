"""Mid-conversation tool changes, folded into the request's ``tools``.

Claude Code 2.1.304 tells the model that its tools changed in the middle of a
conversation with a ``role: "system"`` message holding ``tool_addition`` and
``tool_removal`` blocks -- Anthropic's ``mid-conversation-tool-changes`` beta.
A resumed session always carries them: the original launch's additions are
replayed, and the new launch removes what it no longer has.

No provider MCC routes to receives those blocks as they are: the OpenAI-shaped
converters refuse a non-text system block, and the native Anthropic body maps
a system message to a user message, where the blocks mean nothing. So the
change is translated once, here, into the one thing every provider already
understands -- the tool list -- before routing:

* an addition carrying a ``tool_definition`` appends that definition to
  ``tools``; an addition carrying a ``tool_reference`` re-offers a tool the
  request declared in ``tools`` or an earlier addition defined;
* a removal takes every tool of that name off ``tools``;
* changes apply in message order, so add-then-remove ends removed and
  remove-then-add ends offered; a tool already offered under that name keeps
  its first definition, and a re-offered tool goes to the end of the list;
* the blocks themselves are taken out of the messages. A ``cache_control``
  breakpoint that sat on one moves to the nearest earlier block that can carry
  it, so a cache-minded client still caches the same prefix minus the block;
  a message the blocks leave empty is dropped.

A request without any of these blocks is returned as the very same object: the
fold is invisible to every request MCC accepted before it existed.

What is deliberately not translated: MCP references (``mcp_tool_reference``,
``mcp_toolset_reference``), which name tools of an MCP server that only
Anthropic's own connector can reach -- they are counted as unresolved and
dropped with their block, as is a reference to a name nothing defined, or a
definition that is not a valid tool. Message-level ``output_config`` and
``clear_at`` on these system messages are never parsed in the first place
(spec decision Q7: the request's own effort applies).
"""

from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from .content import get_block_attr, get_block_type
from .models import Message, MessagesRequest, TokenCountRequest, Tool

TOOL_ADDITION = "tool_addition"
TOOL_REMOVAL = "tool_removal"
TOOL_CHANGE_BLOCK_TYPES = frozenset({TOOL_ADDITION, TOOL_REMOVAL})

#: Blocks Anthropic refuses a ``cache_control`` on: a moved breakpoint skips
#: past them to the block before.
_UNCACHEABLE_BLOCK_TYPES = frozenset({"thinking", "redacted_thinking"})


@dataclass(frozen=True, slots=True)
class ToolChangeFold[R: (MessagesRequest, TokenCountRequest)]:
    """The folded request, and what folding it did."""

    request: R
    #: Tool names newly offered, in the order they were added.
    added: tuple[str, ...] = ()
    #: Tool names taken off the list, in the order they were removed.
    removed: tuple[str, ...] = ()
    #: Changes that named nothing MCC can offer or withdraw (MCP references,
    #: unknown names, definitions that are not a valid tool).
    unresolved: int = 0
    #: Breakpoints re-homed on an earlier block.
    breakpoints_moved: int = 0
    #: Breakpoints with no earlier block to go to.
    breakpoints_dropped: int = 0
    #: Messages the blocks left empty.
    messages_dropped: int = 0
    #: How many tool-change blocks were folded.
    blocks: int = 0

    @property
    def applied(self) -> bool:
        return self.blocks > 0


def carries_tool_changes(messages: list[Message]) -> bool:
    """Whether any message holds a ``tool_addition`` or ``tool_removal`` block."""
    for message in messages:
        content = message.content
        if isinstance(content, str):
            continue
        for block in content:
            if get_block_type(block) in TOOL_CHANGE_BLOCK_TYPES:
                return True
    return False


def fold_tool_changes[R: (MessagesRequest, TokenCountRequest)](
    request: R,
) -> ToolChangeFold[R]:
    """Fold every tool change of ``request`` into its ``tools``; see the module."""
    if not carries_tool_changes(request.messages):
        return ToolChangeFold(request=request)

    ledger = _ToolLedger(request.tools)
    messages: list[Message] = []
    moved = dropped_breakpoints = dropped_messages = blocks = 0
    for message in request.messages:
        content = message.content
        if isinstance(content, str) or not any(
            get_block_type(block) in TOOL_CHANGE_BLOCK_TYPES for block in content
        ):
            messages.append(message)
            continue
        kept: list[Any] = []
        for block in content:
            block_type = get_block_type(block)
            if block_type not in TOOL_CHANGE_BLOCK_TYPES:
                kept.append(block)
                continue
            blocks += 1
            ledger.apply(block_type, get_block_attr(block, "tool"))
            breakpoint_ = get_block_attr(block, "cache_control")
            if breakpoint_ is None:
                continue
            if _rehome_breakpoint(breakpoint_, kept, messages):
                moved += 1
            else:
                dropped_breakpoints += 1
        if kept:
            messages.append(message.model_copy(update={"content": kept}))
        else:
            dropped_messages += 1

    folded = request.model_copy(update={"messages": messages, "tools": ledger.tools()})
    return ToolChangeFold(
        request=folded,
        added=tuple(ledger.added),
        removed=tuple(ledger.removed),
        unresolved=ledger.unresolved,
        breakpoints_moved=moved,
        breakpoints_dropped=dropped_breakpoints,
        messages_dropped=dropped_messages,
        blocks=blocks,
    )


class _ToolLedger:
    """The tool list as the conversation changes it, in order."""

    def __init__(self, tools: list[Tool] | None) -> None:
        self._declared_none = tools is None
        self._offered: list[Tool] = list(tools or ())
        # Every definition a reference may name: the request's own tools, then
        # each inline definition (the latest one wins, so a tool re-added with
        # a changed schema is re-offered with the schema it was last given).
        self._known: dict[str, Tool] = {}
        for tool in self._offered:
            self._known.setdefault(tool.name, tool)
        self.added: list[str] = []
        self.removed: list[str] = []
        self.unresolved = 0

    def tools(self) -> list[Tool] | None:
        if self._declared_none and not self._offered:
            return None
        return list(self._offered)

    def apply(self, block_type: str | None, tool: Any) -> None:
        if not isinstance(tool, dict):
            self.unresolved += 1
            return
        kind = tool.get("type")
        if block_type == TOOL_ADDITION and kind == "tool_definition":
            self._add_definition(tool.get("definition"))
        elif kind == "tool_reference" and isinstance(tool.get("name"), str):
            name = tool["name"]
            if block_type == TOOL_ADDITION:
                self._re_offer(name)
            else:
                self._remove(name)
        else:
            self.unresolved += 1

    def _offers(self, name: str) -> bool:
        return any(tool.name == name for tool in self._offered)

    def _add_definition(self, definition: Any) -> None:
        if not isinstance(definition, dict):
            self.unresolved += 1
            return
        try:
            tool = Tool.model_validate(definition)
        except ValidationError:
            self.unresolved += 1
            return
        self._known[tool.name] = tool
        if self._offers(tool.name):
            return
        self._offered.append(tool)
        self.added.append(tool.name)

    def _re_offer(self, name: str) -> None:
        if self._offers(name):
            return
        tool = self._known.get(name)
        if tool is None:
            self.unresolved += 1
            return
        self._offered.append(tool)
        self.added.append(name)

    def _remove(self, name: str) -> None:
        if not self._offers(name):
            self.unresolved += 1
            return
        self._offered = [tool for tool in self._offered if tool.name != name]
        self.removed.append(name)


def _rehome_breakpoint(
    breakpoint_: Any, kept: list[Any], messages: list[Message]
) -> bool:
    """Put ``breakpoint_`` on the nearest earlier block that can carry one.

    Searches the blocks this message keeps first, then the messages already
    folded, newest first. A block that already has a breakpoint keeps its own
    (the two mark the same prefix). Copies rather than mutates, so the request
    the client sent is left as it arrived.
    """
    index = _cacheable_index(kept)
    if index is not None:
        kept[index] = _with_breakpoint(kept[index], breakpoint_)
        return True
    for position in range(len(messages) - 1, -1, -1):
        content = messages[position].content
        if isinstance(content, str):
            continue
        index = _cacheable_index(content)
        if index is None:
            continue
        blocks = list(content)
        blocks[index] = _with_breakpoint(blocks[index], breakpoint_)
        messages[position] = messages[position].model_copy(update={"content": blocks})
        return True
    return False


def _cacheable_index(blocks: list[Any]) -> int | None:
    for index in range(len(blocks) - 1, -1, -1):
        if get_block_type(blocks[index]) not in _UNCACHEABLE_BLOCK_TYPES:
            return index
    return None


def _with_breakpoint(block: Any, breakpoint_: Any) -> Any:
    if get_block_attr(block, "cache_control") is not None:
        return block
    if isinstance(block, dict):
        return {**block, "cache_control": breakpoint_}
    return block.model_copy(update={"cache_control": breakpoint_})


__all__ = [
    "TOOL_ADDITION",
    "TOOL_CHANGE_BLOCK_TYPES",
    "TOOL_REMOVAL",
    "ToolChangeFold",
    "carries_tool_changes",
    "fold_tool_changes",
]
