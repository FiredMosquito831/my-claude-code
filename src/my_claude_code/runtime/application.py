"""Single owner for application startup, shutdown, and runtime operations."""

import asyncio
import inspect
import logging
import os
import traceback
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from loguru import logger

import my_claude_code.cli.managed as cli_managed
import my_claude_code.messaging.session as messaging_session
import my_claude_code.messaging.workflow as messaging_workflow_module
from my_claude_code.application.errors import ApplicationUnavailableError
from my_claude_code.application.model_metadata import ProviderModelRefreshResult
from my_claude_code.application.ports import StopResult
from my_claude_code.config.admin.manifest import update_affects_providers
from my_claude_code.config.admin.persistence import (
    PreparedAdminUpdate,
    commit_prepared_admin_update,
    prepare_admin_update,
)
from my_claude_code.config.admin.status import provider_config_status
from my_claude_code.config.admin.values import load_value_state
from my_claude_code.config.env_files import (
    ANTHROPIC_AUTH_TOKEN_ENV,
    process_env_key_is_effective,
)
from my_claude_code.config.model_refs import parse_provider_type
from my_claude_code.config.paths import messaging_state_dir_path
from my_claude_code.config.provider_registry import get_provider_registry
from my_claude_code.config.server_urls import local_admin_url, local_proxy_root_url
from my_claude_code.config.settings import Settings, get_settings
from my_claude_code.core.diagnostics import redact_sensitive_error_text
from my_claude_code.core.request_log import reset_request_log_stores
from my_claude_code.messaging.platforms import factory as messaging_platform_factory
from my_claude_code.messaging.platforms.factory import MessagingPlatformOptions
from my_claude_code.messaging.platforms.ports import (
    MessagingPlatformComponents,
    MessagingRuntime,
)
from my_claude_code.messaging.voice import Transcriber
from my_claude_code.providers.runtime.discovery import cache_enriched_model_infos
from my_claude_code.providers.runtime.reasoning_probe import (
    ReasoningProbeOutcome,
    probe_reasoning_dialect,
)

from .provider_manager import ProviderRuntimeManager

RestartCallback = Callable[[], Awaitable[None] | None]


async def best_effort(
    name: str,
    awaitable: Awaitable[Any],
    *,
    log_verbose_errors: bool = False,
) -> bool:
    """Run one cleanup step and report whether it completed.

    The lifecycle owner intentionally applies no generic timeout here. Cancelling
    an arbitrary cleanup at a deadline can abandon a half-closed SDK, thread, or
    provider resource; resource-specific cleanup or the process supervisor owns
    any force-termination deadline.
    """
    try:
        await awaitable
    except Exception as exc:
        if log_verbose_errors:
            logger.warning(
                "Shutdown step failed: {}: {}: {}",
                name,
                type(exc).__name__,
                exc,
            )
        else:
            logger.warning(
                "Shutdown step failed: {}: exc_type={}",
                name,
                type(exc).__name__,
            )
        return False
    return True


def warn_if_process_auth_token(settings: Settings) -> None:
    """Warn when server auth was implicitly inherited from the shell."""
    model_config = getattr(settings, "model_config", Settings.model_config)
    if process_env_key_is_effective(model_config, ANTHROPIC_AUTH_TOKEN_ENV):
        logger.warning(
            "ANTHROPIC_AUTH_TOKEN is set in the process environment but not in "
            "a configured .env file. The proxy will require that token. Add "
            "ANTHROPIC_AUTH_TOKEN= to .env to disable proxy auth, or set the "
            "same token in .env to make server auth explicit."
        )


def startup_failure_message(settings: Settings, exc: Exception) -> str:
    """Return the existing concise ASGI startup failure message."""
    if isinstance(exc, ApplicationUnavailableError):
        return exc.message.strip() or "Server startup failed."
    if settings.log_api_error_tracebacks:
        return f"{type(exc).__name__}: {exc}"
    return f"Server startup failed: exc_type={type(exc).__name__}"


class ApplicationRuntime:
    """Own every process-lifetime resource used by one server instance."""

    def __init__(
        self,
        provider_manager: ProviderRuntimeManager,
        *,
        transcriber: Transcriber | None,
        restart_callback: RestartCallback | None = None,
        process_restart_callback: RestartCallback | None = None,
    ) -> None:
        self.provider_manager = provider_manager
        self._transcriber = transcriber
        self._restart_callback = restart_callback
        self._process_restart_callback = process_restart_callback
        self._config_lock = asyncio.Lock()
        self._pending_fields: list[str] = []
        self._messaging_runtime: MessagingRuntime | None = None
        self._messaging_workflow: messaging_workflow_module.MessagingWorkflow | None = (
            None
        )
        self._cli_manager: cli_managed.ManagedClaudeSessionManager | None = None
        self._started = False
        self._closed = False
        self._provider_manager_closed = False
        self._close_lock = asyncio.Lock()

    @property
    def settings(self) -> Settings:
        return self.provider_manager.current_settings()

    @property
    def is_closed(self) -> bool:
        """Whether this runtime released its complete ownership graph."""
        return self._closed

    async def start(self) -> None:
        if self._started:
            return
        logger.info("Starting Claude Code Proxy...")
        try:
            warn_if_process_auth_token(self.settings)
            await self._validate_configured_models_best_effort()
            self.provider_manager.start_model_list_refresh()
            await self._start_messaging_if_configured()
            logging.getLogger("uvicorn.error").info(
                "Admin UI: %s (local-only)",
                local_admin_url(self.settings),
            )
            self._started = True
        except asyncio.CancelledError:
            await self.close()
            raise
        except Exception as exc:
            logger.error(
                "Startup failed:\n{}",
                startup_failure_message(self.settings, exc),
            )
            await self.close()
            raise

    async def close(self) -> bool:
        async with self._close_lock:
            if self._closed:
                return True
            logger.info("Shutdown requested, cleaning up...")
            self._closed = await self._close_owned_resources()
            if self._closed:
                self._started = False
                logger.info("Server shut down cleanly")
            else:
                logger.warning(
                    "Server shutdown incomplete; owned resources remain for retry"
                )
            return self._closed

    async def apply_admin_config(
        self,
        updates: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Apply one validated config update without splitting runtime ownership."""
        async with self._config_lock:
            return await self._apply_admin_config_locked(updates)

    async def apply_admin_config_with(
        self,
        build: Callable[[Settings], Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Apply an update computed *inside* the config lock.

        A caller that reads the current settings, derives a replacement value
        and then calls :meth:`apply_admin_config` has already lost: two such
        callers read the same base and each write a full replacement derived
        from it, so the second commit silently drops the first one's edit. The
        atomic ``os.replace`` behind the write does not help -- the staleness is
        baked into the values before the file is ever touched.

        ``build`` receives the settings as they are at commit time, under the
        same lock, so a read-modify-write is one indivisible step.
        """
        async with self._config_lock:
            return await self._apply_admin_config_locked(build(self.settings))

    async def _apply_admin_config_locked(
        self,
        updates: Mapping[str, Any],
    ) -> dict[str, Any]:
        prepared = prepare_admin_update(updates)
        if not prepared.valid:
            return prepared.applied_response()
        assert prepared.settings is not None

        if prepared.pending_fields:
            result = self._commit_admin_update(prepared)
            restart = self._restart_metadata(
                prepared.pending_fields,
                prepared.settings,
            )
            result["restart"] = restart
            self._pending_fields = (
                [] if restart["automatic"] else list(prepared.pending_fields)
            )
            return result

        result: dict[str, Any] = {}

        def commit() -> None:
            result.update(self._commit_admin_update(prepared))

        # A routing-only write -- a pause is the whole of it today -- cannot
        # change a provider client or its catalogue, so it must not pay for a
        # rebuild of every provider and a full /models sweep. The generation
        # swap itself is kept: it costs under a millisecond and it is what
        # makes the new paused set visible to the very next plan.
        await self.provider_manager.replace(
            prepared.settings,
            commit=commit,
            reason="admin_apply",
            background_refresh=update_affects_providers(updates),
        )
        self._pending_fields = []
        result["restart"] = self._restart_metadata((), prepared.settings)
        return result

    def cached_model_ids(self) -> dict[str, frozenset[str]]:
        """Return cached discovered model ids per provider for admin display."""
        return self.provider_manager.cached_model_ids()

    async def reload_providers(
        self, reason: str, *, refresh_provider_id: str | None = None
    ) -> dict[str, Any]:
        """Republish the provider generation after a non-Settings mutation.

        Custom provider registry entries live outside Settings; the mutation is
        already persisted by the caller, so the commit boundary is a no-op and
        only the provider runtime needs a fresh generation.

        ``refresh_provider_id`` names the one provider the caller just changed.
        It replaces the generation's blanket background sweep with a single
        scoped, awaited discovery, and returns what that discovery found -- so
        the caller reports the catalogue, not a second independent probe.
        """
        async with self._config_lock:
            await self.provider_manager.replace(
                self.settings,
                commit=lambda: None,
                reason=reason,
                background_refresh=refresh_provider_id is None,
            )
            if refresh_provider_id is None:
                return {}
            result = await self.provider_manager.refresh_provider_models(
                refresh_provider_id
            )
            failure = result.failure_for(refresh_provider_id)
            if failure is not None:
                return {
                    "provider_id": refresh_provider_id,
                    "ok": False,
                    "model_count": 0,
                    "error_type": failure.error_type,
                    "message": failure.message,
                }
            cached = self.provider_manager.cached_model_ids()
            return {
                "provider_id": refresh_provider_id,
                "ok": True,
                "model_count": len(cached.get(refresh_provider_id, frozenset())),
            }

    def admin_status(self) -> dict[str, Any]:
        settings = self.settings
        return {
            "status": "running",
            "host": settings.host,
            "port": settings.port,
            "model": settings.model,
            "provider": parse_provider_type(settings.model),
            "pending_fields": list(self._pending_fields),
            "provider_status": provider_config_status(load_value_state()),
            "cached_models": {
                provider_id: sorted(model_ids)
                for provider_id, model_ids in self.provider_manager.cached_model_ids().items()
            },
        }

    async def test_provider(self, provider_id: str) -> dict[str, Any]:
        lease = await self.provider_manager.acquire()
        try:
            provider = lease.resolve_provider(provider_id)
            infos = await provider.list_model_infos()
        except Exception as exc:
            # The class name alone reads as "application error" to the person
            # who pressed the button, while the message it was raised with
            # already says what to do ("AZURE_OPENAI_API_KEY is not set. Get a
            # key at ..."). Send both, redacted the same way logged errors are.
            return {
                "provider_id": provider_id,
                "ok": False,
                "error_type": type(exc).__name__,
                "message": redact_sensitive_error_text(str(exc).strip()),
            }
        finally:
            await lease.release()
        cached = await cache_enriched_model_infos(
            provider_id, infos, self.provider_manager.cache_model_infos
        )
        return {
            "provider_id": provider_id,
            "ok": True,
            "models": sorted(info.model_id for info in cached),
        }

    async def probe_custom_provider_dialect(self, provider_id: str) -> dict[str, Any]:
        """Learn one custom host's effort vocabulary and store what it said.

        Runs against a model the catalogue already discovered, so the probe
        never invents a model id, and stores the answer on the registry entry
        where the factory reads it. An ``unknown`` outcome is stored too: the
        card should be able to say "asked, and the host answered 401" rather
        than looking as though nobody ever asked.
        """
        registry = get_provider_registry()
        entry = registry.get(provider_id)
        if entry is None:
            return {
                "provider_id": provider_id,
                "status": "unknown",
                "detail": "unknown provider",
            }
        models = sorted(self.cached_model_ids().get(provider_id, frozenset()))
        key = entry.api_keys[0] if entry.api_keys else ""
        outcome: ReasoningProbeOutcome = await probe_reasoning_dialect(
            entry.base_url,
            key,
            models[0] if models else "",
            proxy=entry.proxy,
        )
        registry.update(
            provider_id,
            reasoning_effort_enum=(
                list(outcome.effort_enum) if outcome.status == "learned" else None
            ),
            reasoning_field_ignored=outcome.field_ignored,
            reasoning_probe_status=outcome.status,
            reasoning_probed_at=outcome.probed_at,
        )
        # The vocabulary is read when a provider is *built*, so the generation
        # has to be replaced before the next request can spell the new word.
        # Republish only -- explicitly no discovery sweep. A create already
        # queried this host's ``/models`` exactly once (A1.3), and a probe that
        # quietly made it twice would put that invariant back the way it was.
        async with self._config_lock:
            await self.provider_manager.replace(
                self.settings,
                commit=lambda: None,
                reason="reasoning_dialect_probe",
                background_refresh=False,
            )
        payload = outcome.as_payload()
        payload["provider_id"] = provider_id
        payload["model"] = models[0] if models else ""
        return payload

    async def refresh_models(self) -> ProviderModelRefreshResult:
        return await self.provider_manager.refresh_model_list_cache()

    async def request_restart(self) -> None:
        callback = self._restart_callback
        if callback is None:
            return
        result = callback()
        if inspect.isawaitable(result):
            await result

    async def request_process_restart(self) -> None:
        """Close this process and relaunch the installed server executable.

        Distinct from :meth:`request_restart`, which rebuilds the ASGI app in
        the current interpreter for configuration changes. A package update has
        replaced code on disk, so only a new process can import it.
        """
        callback = self._process_restart_callback
        if callback is None:
            return
        result = callback()
        if inspect.isawaitable(result):
            await result

    async def stop_all(self) -> StopResult | None:
        if self._messaging_workflow is not None:
            outcome = await self._messaging_workflow.stop_all_tasks()
            return StopResult(cancelled_count=outcome.cancelled_count)
        if self._cli_manager is not None:
            await self._cli_manager.stop_all()
            return StopResult(source="cli_manager")
        return None

    def _commit_admin_update(
        self,
        prepared: PreparedAdminUpdate,
    ) -> dict[str, Any]:
        result = commit_prepared_admin_update(prepared)
        get_settings.cache_clear()
        return result

    def _restart_metadata(
        self,
        fields: tuple[str, ...],
        settings: Settings,
    ) -> dict[str, Any]:
        automatic = bool(fields and self._restart_callback is not None)
        return {
            "required": bool(fields),
            "automatic": automatic,
            "admin_url": local_admin_url(settings) if automatic else None,
            "fields": list(fields),
        }

    async def _validate_configured_models_best_effort(self) -> None:
        try:
            await self.provider_manager.validate_configured_models()
        except ApplicationUnavailableError as exc:
            logger.warning(
                "Configured provider model validation failed during startup; "
                "server will continue and requests will fail at provider resolution "
                "when config is incomplete. {}",
                exc.message,
            )

    async def _start_messaging_if_configured(self) -> None:
        try:
            components = messaging_platform_factory.create_messaging_components(
                self.settings.messaging_platform,
                self._messaging_options(),
            )
            if components is not None:
                await self._start_messaging_workflow(components)
        except ImportError as exc:
            cleaned = await self._cleanup_messaging()
            if self.settings.log_api_error_tracebacks:
                logger.warning("Messaging module import error: {}", exc)
            else:
                logger.warning(
                    "Messaging module import error: exc_type={}",
                    type(exc).__name__,
                )
            if not cleaned:
                raise RuntimeError("Messaging startup cleanup incomplete") from exc
        except Exception as exc:
            cleaned = await self._cleanup_messaging()
            if self.settings.log_api_error_tracebacks:
                logger.error("Failed to start messaging platform: {}", exc)
                logger.error(traceback.format_exc())
            else:
                logger.error(
                    "Failed to start messaging platform: exc_type={}",
                    type(exc).__name__,
                )
            if not cleaned:
                raise RuntimeError("Messaging startup cleanup incomplete") from exc

    def _messaging_options(self) -> MessagingPlatformOptions:
        settings = self.settings
        return MessagingPlatformOptions(
            telegram_bot_token=settings.telegram_bot_token,
            allowed_telegram_user_id=settings.allowed_telegram_user_id,
            telegram_proxy_url=settings.telegram_proxy_url,
            discord_bot_token=settings.discord_bot_token,
            allowed_discord_channels=settings.allowed_discord_channels,
            transcriber=self._transcriber,
            messaging_rate_limit=settings.messaging_rate_limit,
            messaging_rate_window=settings.messaging_rate_window,
            log_raw_messaging_content=settings.log_raw_messaging_content,
            log_messaging_error_details=settings.log_messaging_error_details,
            log_api_error_tracebacks=settings.log_api_error_tracebacks,
        )

    async def _start_messaging_workflow(
        self,
        components: MessagingPlatformComponents,
    ) -> None:
        settings = self.settings
        self._messaging_runtime = components.runtime
        workspace = (
            os.path.abspath(settings.allowed_dir)
            if settings.allowed_dir
            else os.getcwd()
        )
        os.makedirs(workspace, exist_ok=True)
        data_path = os.path.abspath(messaging_state_dir_path())
        os.makedirs(data_path, exist_ok=True)
        allowed_dirs = [workspace] if settings.allowed_dir else []

        self._cli_manager = cli_managed.ManagedClaudeSessionManager(
            workspace_path=workspace,
            proxy_root_url=local_proxy_root_url(settings),
            allowed_dirs=allowed_dirs,
            auth_token=settings.anthropic_auth_token,
            log_raw_cli_diagnostics=settings.log_raw_cli_diagnostics,
            log_messaging_error_details=settings.log_messaging_error_details,
        )
        session_store = messaging_session.SessionStore(
            storage_path=os.path.join(data_path, "sessions.json"),
            managed_message_cap=settings.max_message_log_entries_per_chat,
        )
        workflow = messaging_workflow_module.MessagingWorkflow(
            platform_name=components.name,
            outbound=components.outbound,
            voice_cancellation=components.voice_cancellation,
            cli_manager=self._cli_manager,
            session_store=session_store,
            debug_platform_edits=settings.debug_platform_edits,
            debug_subagent_stack=settings.debug_subagent_stack,
            log_raw_cli_diagnostics=settings.log_raw_cli_diagnostics,
            log_messaging_error_details=settings.log_messaging_error_details,
        )
        self._messaging_workflow = workflow
        workflow.restore()
        components.runtime.on_message(workflow.handle_message)
        await components.runtime.start()
        await workflow.repair_restored_statuses()
        if components.startup_notice is not None:
            await workflow.publish_startup_notice(components.startup_notice)
        logger.info("{} platform started with messaging workflow", components.name)

    async def _close_owned_resources(self) -> bool:
        await best_effort(
            "request_log.flush",
            asyncio.to_thread(reset_request_log_stores),
            log_verbose_errors=self.settings.log_api_error_tracebacks,
        )
        if not await self._cleanup_messaging():
            return False
        if not await self._cleanup_transcriber():
            return False
        if self._provider_manager_closed:
            return True
        verbose = self.settings.log_api_error_tracebacks
        self._provider_manager_closed = await best_effort(
            "provider_manager.close",
            self.provider_manager.close(),
            log_verbose_errors=verbose,
        )
        return self._provider_manager_closed

    async def _cleanup_messaging(self) -> bool:
        verbose = self.settings.log_api_error_tracebacks
        workflow = self._messaging_workflow
        runtime = self._messaging_runtime
        cli_manager = self._cli_manager

        if runtime is not None:
            quiesced = await best_effort(
                "messaging_runtime.quiesce",
                runtime.quiesce(),
                log_verbose_errors=verbose,
            )
            if not quiesced:
                # Delivery must remain available until ingress is known stopped.
                # Retaining the graph lets the next close retry this exact gate.
                return False

        if workflow is not None:
            closed = await best_effort(
                "messaging_workflow.close",
                workflow.close(),
                log_verbose_errors=verbose,
            )
            if not closed:
                # Active workflow tasks may still need delivery, transcription,
                # CLI sessions, and providers while a later close retries drain.
                return False
            if self._messaging_workflow is workflow:
                self._messaging_workflow = None
            if self._cli_manager is cli_manager:
                self._cli_manager = None
        elif cli_manager is not None:
            drained = await best_effort(
                "cli_manager.stop_all",
                cli_manager.stop_all(),
                log_verbose_errors=verbose,
            )
            if not drained:
                return False
            if self._cli_manager is cli_manager:
                self._cli_manager = None

        if runtime is not None:
            closed = await best_effort(
                "messaging_runtime.close",
                runtime.close(),
                log_verbose_errors=verbose,
            )
            if not closed:
                return False
            if self._messaging_runtime is runtime:
                self._messaging_runtime = None
        return True

    async def _cleanup_transcriber(self) -> bool:
        transcriber = self._transcriber
        if transcriber is None:
            return True
        closed = await best_effort(
            "transcriber.close",
            transcriber.close(),
            log_verbose_errors=self.settings.log_api_error_tracebacks,
        )
        if closed and self._transcriber is transcriber:
            self._transcriber = None
        return closed
