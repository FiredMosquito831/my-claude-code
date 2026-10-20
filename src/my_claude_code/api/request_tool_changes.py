"""Apply a request's mid-conversation tool changes at the Messages doors.

The fold itself is pure protocol work and lives in
:mod:`my_claude_code.core.anthropic.tool_changes`; this is the one place both
Anthropic doors (``/v1/messages`` and ``/v1/messages/count_tokens``) call it,
so the message and its token count always agree on the tool list, and the one
place a fold that did something leaves a trace row saying what.
"""

from my_claude_code.core.anthropic import (
    MessagesRequest,
    TokenCountRequest,
    fold_tool_changes,
)
from my_claude_code.core.trace import trace_event


def fold_request_tool_changes[R: (MessagesRequest, TokenCountRequest)](
    request: R, *, request_id: str
) -> R:
    """Return ``request`` with its tool changes folded into ``tools``.

    The same object when it carries none, which is every request MCC accepted
    before Claude Code 2.1.304.
    """
    fold = fold_tool_changes(request)
    if not fold.applied:
        return request
    trace_event(
        stage="request",
        event="my_claude_code.api.tool_changes_folded",
        source="api",
        request_id=request_id,
        model=request.model,
        blocks=fold.blocks,
        added=list(fold.added),
        removed=list(fold.removed),
        unresolved=fold.unresolved,
        breakpoints_moved=fold.breakpoints_moved,
        breakpoints_dropped=fold.breakpoints_dropped,
        messages_dropped=fold.messages_dropped,
        tools_after=len(fold.request.tools or ()),
    )
    return fold.request
