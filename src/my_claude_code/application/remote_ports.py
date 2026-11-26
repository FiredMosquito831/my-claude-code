"""Application-layer port and error contracts for Remote Control (/rc)."""

from collections.abc import AsyncIterator, Mapping
from typing import Any, Protocol


class RemoteControlError(Exception):
    """Base error raised by Remote Control operations."""


class RemoteDisabledError(RemoteControlError):
    """Raised when Remote Control is disabled (RC_ENABLED=false)."""


class RemoteForbiddenError(RemoteControlError):
    """Raised when a workspace path or permission mode violates policy."""


class RemoteNotFoundError(RemoteControlError):
    """Raised when a requested session, approval, or device does not exist."""


class RemoteLimitError(RemoteControlError):
    """Raised when active session limits or rate limits are exceeded."""


class RemoteConflictError(RemoteControlError):
    """Raised on invalid state transitions or duplicate concurrent actions."""


class RemoteSessionPort(Protocol):
    """Runtime capabilities consumed by `/rc/*` and `/admin/api/rc/*` routes."""

    def is_enabled(self) -> bool: ...

    def status_snapshot(self) -> dict[str, Any]: ...

    async def inspect_tunnels(self) -> dict[str, Any]: ...

    async def start_tunnel(self, mode: str | None = None) -> dict[str, Any]: ...

    async def stop_tunnel(self) -> dict[str, Any]: ...

    def generate_qr_svg(self, url: str) -> str: ...

    def list_devices(self) -> list[dict[str, Any]]: ...

    def register_device(
        self,
        *,
        device_id: str,
        label: str,
        user_agent: str,
        ip: str,
    ) -> dict[str, Any]: ...

    def touch_device(self, device_id: str, *, ip: str, user_agent: str) -> bool: ...

    def is_device_active(self, device_id: str) -> bool: ...

    def revoke_device(self, device_id: str) -> bool: ...

    def revoke_all_devices(self) -> int: ...

    def session_signing_secret(self) -> str: ...

    def list_allowed_dirs(self, path: str | None = None) -> dict[str, Any]: ...

    def list_harnesses(self) -> list[dict[str, Any]]: ...

    def list_discovered_sessions(
        self, *, harness: str | None = None
    ) -> list[dict[str, Any]]: ...

    def list_sessions(self) -> list[dict[str, Any]]: ...

    def get_session(self, session_id: str) -> dict[str, Any]: ...

    def get_events(
        self, session_id: str, *, since_seq: int = 0, limit: int = 500
    ) -> list[dict[str, Any]]: ...

    async def create_session(
        self,
        *,
        harness: str,
        cwd: str,
        title: str = "",
        prompt: str = "",
        permission_mode: str | None = None,
        model: str | None = None,
        create_worktree: bool = False,
        driver_mode: str = "auto",
        external_session_id: str | None = None,
        fork_session: bool = False,
        server_url: str | None = None,
        cols: int = 120,
        rows: int = 36,
        detached: bool = True,
    ) -> dict[str, Any]: ...

    async def register_external_sidecar(
        self,
        *,
        session_id: str,
        harness: str,
        cwd: str,
        host: str,
        port: int,
        secret: str,
        pid: int | None = None,
        sidecar_pid: int | None = None,
        title: str = "",
        permission_mode: str | None = None,
        model: str | None = None,
        origin: str = "cli_rc",
    ) -> dict[str, Any]: ...

    def list_sidecars(self) -> list[dict[str, Any]]: ...

    async def import_session(
        self,
        *,
        external_session_id: str,
        harness: str,
        cwd: str,
        title: str = "",
        permission_mode: str | None = None,
        model: str | None = None,
        fork_session: bool = False,
        create_worktree: bool = False,
        driver_mode: str = "auto",
        server_url: str | None = None,
    ) -> dict[str, Any]: ...

    async def ensure_pty(
        self, session_id: str, *, cols: int = 120, rows: int = 36
    ) -> dict[str, Any]: ...

    async def switch_to_pty(
        self, session_id: str, *, cols: int = 120, rows: int = 36
    ) -> dict[str, Any]: ...

    async def send_message(self, session_id: str, text: str) -> dict[str, Any]: ...

    async def resolve_approval(
        self,
        session_id: str,
        approval_id: str,
        *,
        decision: str,
        updated_input: Mapping[str, Any] | None = None,
        answers: Mapping[str, Any] | None = None,
        message: str = "",
    ) -> dict[str, Any]: ...

    async def set_permission_mode(
        self, session_id: str, mode: str
    ) -> dict[str, Any]: ...

    async def set_model(self, session_id: str, model: str) -> dict[str, Any]: ...

    async def get_session_telemetry(
        self, session_id: str, *, emit_event: bool = False
    ) -> dict[str, Any]: ...

    async def set_proxy_controls(
        self,
        session_id: str,
        *,
        model: str | None = None,
        reasoning_effort: str | None = None,
        preferred_surface_info: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def vapid_public_key(self) -> dict[str, Any]: ...

    def subscribe_push(
        self,
        *,
        endpoint: str,
        p256dh: str,
        auth: str,
        device_id: str,
    ) -> dict[str, Any]: ...

    def unsubscribe_push(self, endpoint: str) -> bool: ...

    async def send_test_push(self, device_id: str) -> dict[str, Any]: ...

    async def interrupt_session(self, session_id: str) -> dict[str, Any]: ...

    async def fork_session(
        self, session_id: str, *, title: str = ""
    ) -> dict[str, Any]: ...

    async def get_diff(self, session_id: str) -> dict[str, Any]: ...

    async def stop_session(self, session_id: str) -> dict[str, Any]: ...

    async def delete_session(self, session_id: str) -> bool: ...

    async def pty_input(self, session_id: str, data: bytes) -> None: ...

    async def pty_resize(self, session_id: str, cols: int, rows: int) -> None: ...

    def pty_scrollback(self, session_id: str) -> bytes: ...

    def subscribe_events(
        self, session_id: str, *, since_seq: int = 0
    ) -> AsyncIterator[dict[str, Any]]: ...

    def subscribe_pty(self, session_id: str) -> AsyncIterator[bytes]: ...

    def register_ws_closer(self, device_id: str, closer: Any) -> Any: ...

    async def close(self) -> None: ...
