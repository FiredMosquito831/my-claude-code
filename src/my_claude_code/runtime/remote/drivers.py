"""Shared protocol and callback types for structured Remote Control drivers."""

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

AUTO_EDIT_TOOL_NAMES = frozenset(
    {
        "Edit",
        "Write",
        "MultiEdit",
        "NotebookEdit",
        "FileChange",
        "edit",
        "write",
        "patch",
        "multiedit",
        "apply_patch",
    }
)

EventEmitCallback = Callable[[str, dict[str, Any]], Awaitable[None]]
ApprovalCreateCallback = Callable[
    [str, str, str, dict[str, Any]], Awaitable[dict[str, Any]]
]
StateUpdateCallback = Callable[..., Awaitable[None]]


class StructuredDriver(Protocol):
    """Uniform lifecycle and interaction contract for Tier 1A/1B structured drivers."""

    driver_tier: str

    @property
    def pid(self) -> int | None: ...

    async def start(self) -> None: ...

    async def send_user_message(self, text: str) -> None: ...

    async def resolve_approval(
        self,
        approval_id: str,
        *,
        decision: str,
        updated_input: Mapping[str, Any] | None = None,
        answers: Mapping[str, Any] | None = None,
        message: str = "",
    ) -> None: ...

    async def set_permission_mode(self, mode: str) -> None: ...

    async def set_model(self, model: str) -> None: ...

    async def interrupt(self) -> None: ...

    async def fork_session(self, *, title: str = "") -> dict[str, Any] | None: ...

    async def close(self) -> None: ...
