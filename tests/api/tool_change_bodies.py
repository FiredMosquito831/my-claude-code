"""Invented request bodies in the shape of a resumed Claude Code 2.1.304 session.

The shapes follow the census in ``specs/INVESTIGATION-RESUMED-SESSION-422.md``
§1.1 and §1.6 (block types, keys, positions, lengths); every word of text is
invented here and carries :data:`MARKER`, so a test can prove that no response
ever repeats the conversation back. Shared by the door tests and by the
scratch-server proofs of the same release.
"""

from typing import Any

#: Appears in every text of the conversation and nowhere else.
MARKER = "INVENTED-RESUME422-MARKER"
CLAUDE_CODE_UA = "claude-cli/2.1.304 (external, cli)"
#: The tools the new launch declares in ``tools``.
LAUNCH_TOOLS = ("Bash", "Read", "Edit", "Grep")
#: Defined inline by the original session's first system message.
ADDED_TOOLS = ("mcp__alpha__one", "mcp__alpha__two", "mcp__alpha__three")
#: Withdrawn by the resume's system message.
REMOVED_TOOLS = ("mcp__alpha__two", "mcp__alpha__three", "Grep")
#: What the model should be offered after the fold, in order.
FOLDED_TOOLS = ("Bash", "Read", "Edit", "mcp__alpha__one")
HOOK_TEXT_CHARS = 30_000


def _schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }


def tool(name: str) -> dict[str, Any]:
    return {"name": name, "description": f"{MARKER} {name}", "input_schema": _schema()}


def hook_text(label: str, chars: int = HOOK_TEXT_CHARS) -> str:
    unit = f"{label} {MARKER} invented hook output line. "
    return (unit * (chars // len(unit) + 1))[:chars]


def resumed_session_body(model: str = "claude-fable-5-1") -> dict[str, Any]:
    """A resumed session: the original start's additions, the resume's removals.

    The resume's system message is the last message and its breakpoint sits on
    its last ``tool_removal`` block (``messages/N/content/3`` in the census).
    """
    additions = [
        {
            "type": "tool_addition",
            "tool": {"type": "tool_definition", "definition": tool(name)},
        }
        for name in ADDED_TOOLS
    ]
    removals: list[dict[str, Any]] = [
        {"type": "tool_removal", "tool": {"type": "tool_reference", "name": name}}
        for name in REMOVED_TOOLS
    ]
    removals[-1]["cache_control"] = {"type": "ephemeral"}
    return {
        "model": model,
        "max_tokens": 1024,
        "stream": True,
        "system": [
            {"type": "text", "text": f"{MARKER} invented identity"},
            {
                "type": "text",
                "text": f"{MARKER} invented instructions",
                "cache_control": {"type": "ephemeral"},
            },
        ],
        "tools": [tool(name) for name in LAUNCH_TOOLS],
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": f"{MARKER} first ask"}],
            },
            {
                "role": "system",
                "content": [
                    {
                        "type": "text",
                        "text": "SessionStart:startup hook success: "
                        + hook_text("start"),
                    },
                    *additions,
                ],
            },
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": f"{MARKER} invented plan"},
                    {
                        "type": "tool_use",
                        "id": "toolu_inv_01",
                        "name": "mcp__alpha__one",
                        "input": {"query": MARKER},
                        "caller": {"type": "direct"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_inv_01",
                        "content": f"{MARKER} invented result",
                    }
                ],
            },
            {
                "role": "assistant",
                "content": [{"type": "text", "text": f"{MARKER} invented answer"}],
            },
            {
                "role": "user",
                "content": [{"type": "text", "text": f"{MARKER} resumed ask"}],
            },
            {
                "role": "system",
                "output_config": {"effort": "low"},
                "content": [
                    {
                        "type": "text",
                        "text": "SessionStart:resume hook success: "
                        + hook_text("resume"),
                    },
                    *removals,
                ],
            },
        ],
    }


def plain_body(model: str = "claude-fable-5-1") -> dict[str, Any]:
    """The conversation the fold should turn :func:`resumed_session_body` into.

    The tool changes already applied to ``tools``, no tool-change block left,
    and the resume's breakpoint on the block before the removals -- a body
    every MCC release accepts, so it can go to the code before the fold too.
    """
    body = resumed_session_body(model)
    for message in body["messages"]:
        content = message["content"]
        if isinstance(content, list):
            message["content"] = [
                block
                for block in content
                if block["type"] not in ("tool_addition", "tool_removal")
            ]
        message.pop("output_config", None)
    body["messages"][-1]["content"][-1]["cache_control"] = {"type": "ephemeral"}
    body["tools"] = [tool(name) for name in FOLDED_TOOLS]
    return body


def unknown_block_body(model: str = "claude-fable-5-1") -> dict[str, Any]:
    """A body with one block type MCC does not know (Q5: one of the 14)."""
    body = plain_body(model)
    body["messages"][0]["content"].append(
        {"type": "compaction", "content": f"{MARKER} invented summary"}
    )
    return body


def missing_field_body(model: str = "claude-fable-5-1") -> dict[str, Any]:
    """A body with no ``messages`` at all, every other field intact."""
    body = plain_body(model)
    body["note"] = hook_text("note", 4_000)
    del body["messages"]
    return body
