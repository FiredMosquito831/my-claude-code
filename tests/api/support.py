"""Explicit test composition for the API adapter."""

from collections.abc import AsyncIterator, Iterable, MutableMapping
from pathlib import Path

from fastapi import FastAPI

from my_claude_code.api.app import create_app
from my_claude_code.api.ports import ApiServices
from my_claude_code.config.provider_registry import ProviderRegistry
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.reasoning import DEFAULT_REASONING_POLICY, ReasoningPolicy
from my_claude_code.providers.base import BaseProvider, ProviderConfig
from my_claude_code.providers.runtime import ProviderRuntime
from my_claude_code.runtime.application import ApplicationRuntime, RestartCallback
from my_claude_code.runtime.provider_manager import ProviderRuntimeManager


class ModelListingProviderDouble(BaseProvider):
    """Answer ``/models`` and count how many times the runtime asked.

    The count is the whole point: a create used to hit a brand-new upstream
    twice in the same second -- the generation's background sweep and the
    route's own probe -- and one of the two came back 403.
    """

    def __init__(
        self,
        model_ids: Iterable[str],
        *,
        error: BaseException | None = None,
        failures: int = 0,
    ) -> None:
        super().__init__(
            ProviderConfig(api_key="test-key", base_url="https://custom.test/v1")
        )
        self.model_ids = frozenset(model_ids)
        self.calls = 0
        self._error = error
        self._failures = failures

    def preflight_stream(
        self,
        request: MessagesRequest,
        *,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> None:
        return None

    def stream_response(
        self,
        request: MessagesRequest,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> AsyncIterator[str]:
        raise NotImplementedError("This double only answers the model list")

    async def cleanup(self) -> None:
        return None

    async def list_model_ids(self) -> frozenset[str]:
        self.calls += 1
        if self._error is not None and self.calls <= self._failures:
            raise self._error
        return self.model_ids


def create_custom_provider_app(
    monkeypatch,
    tmp_path: Path,
    providers: MutableMapping[str, BaseProvider],
) -> tuple[FastAPI, ProviderRegistry]:
    """Build an app whose custom-provider registry and runtime are both real.

    Only the upstream HTTP client is doubled, so the create route, the hot
    reload, discovery, the model cache and ``/v1/models`` are all the shipping
    code. Route-level tests that stub the runtime cannot see the defect this
    covers, because the defect lives between them.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FCC_ENV_FILE", raising=False)
    registry = ProviderRegistry(tmp_path / "custom_providers.json")
    monkeypatch.setattr("my_claude_code.config.provider_registry._registry", registry)
    return create_test_app(Settings(), providers=providers), registry


def create_test_app(
    settings: Settings | None = None,
    *,
    providers: MutableMapping[str, BaseProvider] | None = None,
    restart_callback: RestartCallback | None = None,
    process_restart_callback: RestartCallback | None = None,
) -> FastAPI:
    """Build an API app with explicit in-memory runtime services."""
    settings = settings or Settings()
    if providers is None:
        manager = ProviderRuntimeManager(settings)
    else:
        manager = ProviderRuntimeManager(
            settings,
            runtime_factory=lambda snapshot: ProviderRuntime(
                snapshot,
                dict(providers),
            ),
        )
    runtime = ApplicationRuntime(
        manager,
        transcriber=None,
        restart_callback=restart_callback,
        process_restart_callback=process_restart_callback,
    )
    return create_app(
        ApiServices(
            requests=manager,
            admin=runtime,
            tasks=runtime,
        )
    )


def runtime_for_app(app: FastAPI) -> ApplicationRuntime:
    """Return the runtime supplied by :func:`create_test_app`."""
    runtime = app.state.services.admin
    if not isinstance(runtime, ApplicationRuntime):
        raise TypeError("Test app does not use ApplicationRuntime")
    return runtime


def provider_manager_for_app(app: FastAPI) -> ProviderRuntimeManager:
    """Return the provider manager supplied by :func:`create_test_app`."""
    manager = app.state.services.requests
    if not isinstance(manager, ProviderRuntimeManager):
        raise TypeError("Test app does not use ProviderRuntimeManager")
    return manager
