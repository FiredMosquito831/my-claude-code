"""Single owner for application startup, shutdown, and runtime operations."""

import asyncio
import inspect
import logging
import os
import traceback
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from loguru import logger

import my_claude_code.cli.managed as cli_managed
import my_claude_code.messaging.session as messaging_session
import my_claude_code.messaging.workflow as messaging_workflow_module
from my_claude_code.api.request_pricing import backfill_pricer
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
from my_claude_code.core.request_log import (
    reset_request_log_stores,
    set_cost_backfill_pricer,
)
from my_claude_code.core.startup_state import startup_state
from my_claude_code.messaging.platforms import factory as messaging_platform_factory
from my_claude_code.messaging.platforms.factory import MessagingPlatformOptions
from my_claude_code.messaging.platforms.ports import (
    MessagingPlatformComponents,
    MessagingRuntime,
)
from my_claude_code.messaging.voice import Transcriber
from my_claude_code.providers.chatgpt_oauth.provider import (
    CHATGPT_OAUTH_PROVIDER_ID,
    WITHHELD_MODEL_IDS,
)
from my_claude_code.providers.recovery import (
    FACT_MODEL_WITHHELD,
    SOURCE_PROBE,
    LearnedFactStore,
    learned_fact_store,
)
from my_claude_code.providers.runtime.capability_probes import (
    ALL_PROBES,
    DEFAULT_PROBES,
    MAX_MODELS_PER_PROBE_RUN,
    PROBE_FACT_KINDS,
    probe_model_capabilities,
)
from my_claude_code.providers.runtime.config import provider_credential
from my_claude_code.providers.runtime.discovery import cache_enriched_model_infos
from my_claude_code.providers.runtime.identity_probe import (
    probe_client_identity,
)
from my_claude_code.providers.runtime.reasoning_probe import (
    ReasoningProbeOutcome,
    probe_reasoning_dialect,
)

from .discovery_timer import ProviderDiscoveryTimer, resolve_refresh_interval
from .provider_manager import ProviderRuntimeManager

RestartCallback = Callable[[], Awaitable[None] | None]

#: Heavy third-party modules this server always ends up importing, imported on
#: a worker thread at the top of startup.
#:
#: They are lazy imports everywhere else on purpose -- ``openai`` alone costs
#: about 3.5 seconds on the reporter's machine, and most ``mcc-*`` commands
#: never touch it. But the server always does, and before 6.59.0 it did so
#: from *inside the event loop*, while constructing its first provider. An
#: import is not a suspension point, so the freshly bound listener spent those
#: 3.5 seconds accepting connections and answering none of them -- a /health
#: probe with a 3-second timeout would have timed out against a server that had
#: already declared itself ready.
#:
#: A worker thread fixes it where moving the import earlier does not: an import
#: is mostly stat and read syscalls, and the GIL is released across every one
#: of them, so the event loop keeps its slices and the ``starting`` gate keeps
#: answering while the module loads.
PREWARM_MODULES = ("openai",)


def prewarm_heavy_imports() -> None:
    """Import :data:`PREWARM_MODULES` now. Best effort, never raises.

    If an import fails, the lazy import at the original call site still runs
    exactly as it did before -- this is an optimisation, not a dependency.
    """

    for name in PREWARM_MODULES:
        try:
            __import__(name)
        except Exception:
            logger.debug("Prewarming {name} failed; it imports lazily.", name=name)


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


def _withheld_sink(store: LearnedFactStore) -> Callable[[str], None]:
    """Write one newly withheld model id through to the durable store."""

    def remember(model_id: str) -> None:
        store.record(
            CHATGPT_OAUTH_PROVIDER_ID,
            model_id,
            FACT_MODEL_WITHHELD,
            True,
            evidence="the backend refused this model id by name",
        )

    return remember


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
        # The configured-model probe, which runs behind readiness. Held so the
        # shutdown can cancel it rather than leave a network call outliving the
        # provider generation it is using.
        self._validation_task: asyncio.Task[None] | None = None
        # The desktop-app pin check, which also runs behind readiness. Held
        # for the same reason: the shutdown cancels it rather than leaving a
        # file read outliving the runtime that started it.
        self._shell_pin_task: asyncio.Task[None] | None = None
        self._provider_manager_closed = False
        self._close_lock = asyncio.Lock()
        # The durable store of what every host has taught this proxy about
        # itself, and the loop that keeps the catalogues current. Both are
        # owned here rather than in ``runtime.asgi``, which owns lifespan only.
        self._learned_facts = learned_fact_store()
        self._discovery_timer = ProviderDiscoveryTimer(
            self.provider_manager.refresh_model_list_cache_periodic,
            lambda: self.settings.model_discovery_refresh_seconds,
        )

    @property
    def settings(self) -> Settings:
        return self.provider_manager.current_settings()

    @property
    def configured_model_validation_task(self) -> asyncio.Task[None] | None:
        """The configured-model probe that runs behind readiness, if started."""

        return self._validation_task

    @property
    def is_closed(self) -> bool:
        """Whether this runtime released its complete ownership graph."""
        return self._closed

    async def start(self) -> None:
        if self._started:
            return
        logger.info("Starting Claude Code Proxy...")
        state = startup_state()
        try:
            warn_if_process_auth_token(self.settings)
            # Before the first sweep and before the first request: a provider
            # built during the sweep asks the store for its memory, and a
            # memory handed out empty would re-pay every 400 this proxy has
            # already paid for.
            # First, and on a worker thread. See ``PREWARM_MODULES``.
            state.mark("prewarm")
            await asyncio.to_thread(prewarm_heavy_imports)
            state.mark("learned-facts")
            self._load_learned_facts()
            # The single most expensive stage on a real config -- it probes
            # every configured provider over the network. It used to run
            # before the listener existed, which is most of why a start looked
            # like a free port for twenty seconds.
            # Before the sweep, and on a worker thread: one file read that
            # makes the first Models page and the first /v1/models answerable
            # without waiting for twelve gateways. The sweep below runs behind
            # readiness exactly as it did, and overwrites what it learns.
            state.mark("stored-catalogue")
            await asyncio.to_thread(self.provider_manager.load_stored_catalogue)
            state.mark("catalogue")
            self.provider_manager.start_model_list_refresh()
            state.mark("rediscovery")
            self._discovery_timer.start()
            state.mark("messaging")
            await self._start_messaging_if_configured()
            # One read of the models.dev cache, on a worker thread, and that is
            # the whole of it: registering a pricer is what lets the request log
            # price the requests it logged before anything priced anything. The
            # walk itself runs on the log's own writer thread, in its idle time,
            # and a process that gets no pricer simply never backfills.
            state.mark("cost-backfill")
            await asyncio.to_thread(self._register_cost_backfill_pricer)
            # Off the readiness path, and the single largest thing on it: this
            # asks every configured provider, over the network, whether the
            # models named in the configuration exist. It is best-effort -- it
            # logs and returns, it never refuses to start -- so making the
            # server wait for it only ever delayed the first request by however
            # slow the slowest provider was that morning. Measured at 3.6s on a
            # scratch config and around 9s on the reporter's.
            state.mark("configured-models")
            self._validation_task = asyncio.create_task(
                self._validate_configured_models_best_effort()
            )
            logging.getLogger("uvicorn.error").info(
                "Admin UI: %s (local-only)",
                local_admin_url(self.settings),
            )
            # Behind readiness and off the loop: one receipt read on a worker
            # thread, so a stale desktop app is named in the log the moment the
            # server is up rather than only when somebody opens the dashboard.
            # BUG-0's symptom was silence -- the wheel updated itself fifteen
            # times while the window did not, and nothing anywhere said so.
            self._shell_pin_task = asyncio.create_task(self._report_stale_desktop_app())
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

    async def probe_provider_capabilities(
        self, provider_id: str, models: tuple[str, ...] = ()
    ) -> dict[str, Any]:
        """Measure what one provider's host actually does, and store it.

        Bounded on purpose. A gateway listing 400 models behind one button
        press is 400-1200 upstream requests, so at most
        :data:`MAX_MODELS_PER_PROBE_RUN` models are probed per press and the
        count is stated before it runs. Nothing here ever touches the request
        path: this is reached only from the provider card.

        A host that answers 401/402/403 before it validates a body has told us
        nothing about the model, so it is recorded as unprobeable with its
        status code and no verdict is guessed at.
        """

        target = self._probe_target(provider_id)
        if target is None:
            return {
                "provider_id": provider_id,
                "status": "unknown",
                "detail": "not configured",
                "results": [],
            }
        base_url, api_key, proxy = target
        known = sorted(self.cached_model_ids().get(provider_id, frozenset()))
        chosen = [model for model in models if model] or known
        chosen = chosen[:MAX_MODELS_PER_PROBE_RUN]
        probes = ALL_PROBES if self.settings.model_probe_new_models else DEFAULT_PROBES
        results: list[dict[str, Any]] = []
        unprobeable = ""
        for model in chosen:
            outcomes = await probe_model_capabilities(
                base_url, api_key, model, probes=probes, proxy=proxy
            )
            for outcome in outcomes:
                results.append(outcome.as_payload())
                if outcome.detail.startswith("unprobeable"):
                    unprobeable = outcome.detail
                if outcome.status != "learned":
                    continue
                kind = PROBE_FACT_KINDS.get(outcome.probe)
                if kind is None:
                    continue
                self._learned_facts.record(
                    provider_id,
                    model,
                    kind,
                    outcome.value,
                    source=SOURCE_PROBE,
                    evidence=outcome.detail,
                )
            if unprobeable:
                # Every further model would get the same non-answer from the
                # same credential and cost another request to find out.
                break
        # Whether this host acts on who is calling. Two requests, and only
        # for a provider whose profile declares an identity -- which is
        # what keeps the press the same size for the other nineteen.
        identity = (
            None
            if unprobeable or not chosen
            else await probe_client_identity(
                provider_id, base_url, api_key, chosen[0], proxy=proxy
            )
        )
        if identity is not None:
            results.append(identity.as_payload())
        self._learned_facts.flush()
        return {
            "provider_id": provider_id,
            "status": "unprobeable" if unprobeable else "probed",
            "detail": unprobeable,
            "models": chosen,
            "probes": list(probes),
            "results": results,
        }

    def _probe_target(self, provider_id: str) -> tuple[str, str, str | None] | None:
        """Resolve one provider's base URL and credential, registry-first.

        No per-provider branch: a custom provider carries its own base URL and
        keys on its registry entry, and every other provider is described by
        its catalogue descriptor plus the credential ``provider_credential``
        already reads out of settings for discovery.
        """

        registry = get_provider_registry()
        entry = registry.get(provider_id)
        if entry is not None:
            key = entry.api_keys[0] if entry.api_keys else ""
            if entry.base_url and key:
                return entry.base_url, key, entry.proxy
            return None
        descriptor = registry.all_descriptors().get(provider_id)
        if descriptor is None:
            return None
        base_url = descriptor.default_base_url or ""
        if descriptor.base_url_attr:
            configured = getattr(self.settings, descriptor.base_url_attr, "")
            if isinstance(configured, str) and configured.strip():
                base_url = configured.strip()
        credential = provider_credential(descriptor, self.settings)
        if not base_url or not credential:
            return None
        proxy = None
        if descriptor.proxy_attr:
            value = getattr(self.settings, descriptor.proxy_attr, None)
            proxy = value if isinstance(value, str) and value.strip() else None
        return base_url, credential, proxy

    def learned_facts_by_model(self) -> dict[str, list[dict[str, Any]]]:
        """Every stored fact, grouped by ``provider/model`` for the page."""

        now = datetime.now(UTC)
        grouped: dict[str, list[dict[str, Any]]] = {}
        for fact in self._learned_facts.all_facts():
            row = fact.as_row()
            row["age_seconds"] = fact.age_seconds(now)
            row["ttl_seconds"] = fact.ttl_seconds
            row["stale"] = fact.is_stale(now)
            row["retired"] = fact.retired
            row["detail"] = fact.detail
            grouped.setdefault(f"{fact.provider_id}/{fact.model_id}", []).append(row)
        return grouped

    def forget_learned_facts(
        self, provider_id: str, model_id: str = "", fact_kind: str = ""
    ) -> int:
        """Forget one row, one model, one provider, or everything."""

        if not provider_id:
            return self._learned_facts.forget_all()
        if not model_id:
            return self._learned_facts.forget_provider(provider_id)
        return self._learned_facts.forget(provider_id, model_id, fact_kind)

    def catalogue_refresh_status(self) -> dict[str, Any]:
        """When the background sweep last ran, and when it is next due."""

        interval = resolve_refresh_interval(
            self.settings.model_discovery_refresh_seconds
        )
        return {
            "enabled": interval > 0,
            "interval_seconds": interval,
            "configured_seconds": self.settings.model_discovery_refresh_seconds,
            "last_refreshed_at": self.provider_manager.last_catalogue_refresh_at,
            "next_refresh_at": self._discovery_timer.next_refresh_at,
            "running": self._discovery_timer.running,
            # Two different questions the readout has to keep apart: whether a
            # sweep is happening right now, and whether the catalogue on show
            # is one this process swept or one it read from disk. Without the
            # second, "last refreshed 40 min ago" reads as a sweep that never
            # happened in this process.
            "refreshing": self.provider_manager.refresh_in_flight,
            "from_stored_catalogue": self.provider_manager.catalogue_from_store,
        }

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

    def _register_cost_backfill_pricer(self) -> None:
        """Hand the request log the real pricing ladder, or nothing.

        ``core`` may not import the ladder and must not carry a second copy of
        it, so the composition root injects it -- the same shape as the
        request-log path. Nothing is registered when cost estimation is off, or
        when the models.dev catalogue is not on disk yet: the backfill records
        "nobody publishes a rate for this" permanently, and a cold cache is not
        grounds for recording it about the whole log.
        """
        if not bool(getattr(self.settings, "cost_estimation_enabled", True)):
            set_cost_backfill_pricer(None)
            return
        set_cost_backfill_pricer(
            backfill_pricer(
                litellm_enabled=bool(
                    getattr(self.settings, "cost_source_litellm_enabled", False)
                )
            )
        )

    async def _validate_configured_models_best_effort(self) -> None:
        """Probe every configured model, and never let the result stop a start.

        Since 6.59.0 this runs as a task behind readiness rather than in front
        of it, so an exception here has nowhere to propagate to: an unretrieved
        task exception would be a warning on the console at interpreter exit
        and nothing else. Everything is caught and named instead. That is not a
        widening of the contract -- the method was already "best effort", and
        the server has always continued past a failed probe -- it is what
        makes the contract true now that nobody is awaiting it.
        """

        try:
            await self.provider_manager.validate_configured_models()
        except asyncio.CancelledError:
            raise
        except ApplicationUnavailableError as exc:
            logger.warning(
                "Configured provider model validation failed during startup; "
                "server will continue and requests will fail at provider resolution "
                "when config is incomplete. {}",
                exc.message,
            )
        except Exception as exc:
            logger.warning(
                "Configured provider model validation raised during startup; "
                "the server is already serving and continues. exc_type={}",
                type(exc).__name__,
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

    def _load_learned_facts(self) -> None:
        """Read the durable store and seed the process-wide shapes from it.

        The withheld-model set is a module singleton rather than provider
        state, because a provider instance is rebuilt on every config apply
        while the backend's opinion of a model id is not; loading it is
        therefore a separate step from handing out a recovery memory.
        """

        self._learned_facts.enable_persistence()
        WITHHELD_MODEL_IDS.load(
            self._learned_facts.withheld_model_ids(CHATGPT_OAUTH_PROVIDER_ID)
        )
        WITHHELD_MODEL_IDS.sink = _withheld_sink(self._learned_facts)

    async def _report_stale_desktop_app(self) -> None:
        """Say once, in the log, when the installed desktop app is not the pin.

        Best effort in the strongest sense: it reads one JSON receipt on a
        worker thread and logs. It never downloads, never writes, and never
        raises into the startup path -- an unreadable receipt is not a reason a
        server does not start.

        The import is function-local because
        ``tests/contracts/test_desktop_shell_not_on_the_server_path.py`` pins
        that building the ASGI app has not imported the fetcher; running behind
        readiness is not building the app.
        """

        try:
            from my_claude_code.config.desktop_shell import (
                desktop_shell_update_report,
            )

            report = await asyncio.to_thread(desktop_shell_update_report)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("Desktop app pin check failed: {}", type(exc).__name__)
            return
        if not report.get("shell_update_available"):
            return
        logger.info(
            "The My Claude Code desktop app on this machine is {} and this "
            "release pins {}. It updates the next time you restart the app.",
            report.get("shell_installed_tag"),
            report.get("shell_pinned_tag"),
        )

    async def _close_owned_resources(self) -> bool:
        # Cancelled before the provider manager closes, so a probe in flight is
        # abandoned rather than racing the shutdown that asked for it. Same
        # reason as the discovery timer below, and for the same task shape.
        for task in (self._validation_task, self._shell_pin_task):
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await task
        # Cancelled before the provider manager closes, so a sweep in flight
        # is abandoned rather than racing the shutdown that asked for it.
        await self._discovery_timer.close()
        await best_effort(
            "learned_facts.flush",
            self._learned_facts.close(),
            log_verbose_errors=self.settings.log_api_error_tracebacks,
        )
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
