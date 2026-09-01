"""Runtime capabilities consumed by the HTTP API adapter."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from my_claude_code.application.model_metadata import ProviderModelRefreshResult
from my_claude_code.application.ports import RequestRuntimePort, TaskController
from my_claude_code.config.settings import Settings


class AdminRuntimePort(Protocol):
    """Runtime operations exposed by the local Admin API."""

    async def apply_admin_config(
        self, updates: Mapping[str, Any]
    ) -> dict[str, Any]: ...

    async def apply_admin_config_with(
        self, build: Callable[[Settings], Mapping[str, Any]]
    ) -> dict[str, Any]: ...

    def admin_status(self) -> dict[str, Any]: ...

    def cached_model_ids(self) -> dict[str, frozenset[str]]: ...

    async def reload_providers(
        self, reason: str, *, refresh_provider_id: str | None = None
    ) -> dict[str, Any]: ...

    async def test_provider(self, provider_id: str) -> dict[str, Any]: ...

    async def refresh_models(self) -> ProviderModelRefreshResult: ...

    async def request_restart(self) -> None: ...

    async def request_process_restart(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ApiServices:
    """Complete runtime boundary required to construct the API application."""

    requests: RequestRuntimePort
    admin: AdminRuntimePort
    tasks: TaskController
