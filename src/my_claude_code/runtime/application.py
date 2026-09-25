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
from my_claude_code.application.proxy_check import (
    arm_refusals_from_store,
    check_targets,
)
from my_claude_code.application.proxy_fetch import recover_fetch_job
from my_claude_code.application.proxy_health_store import (
    arm_health_from_store,
    install_listener,
    remove_listener,
)
from my_claude_code.application.proxy_speed_store import load_speed
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
from my_claude_code.config.logging_config import set_third_party_verbosity
from my_claude_code.config.model_refs import parse_provider_type
from my_claude_code.config.paths import messaging_state_dir_path
from my_claude_code.config.provider_registry import get_provider_registry
from my_claude_code.config.proxy_chains import (
    current_proxy_chains,
    migrate_proxy_feeds,
)
from my_claude_code.config.server_urls import local_admin_url, local_proxy_root_url
from my_claude_code.config.settings import Settings, get_settings, parse_lockout_tiers
from my_claude_code.core import request_tasks
from my_claude_code.core.diagnostics import redact_sensitive_error_text
from my_claude_code.core.loop_health import loop_health
from my_claude_code.core.proxy_rotation import configure_proxy_rotation
from my_claude_code.core.request_log import (
    reset_request_log_stores,
    retune_request_log_store,
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
from my_claude_code.providers.runtime.opencode_credentials import (
    probe_credential,
    probe_fallback_credential,
)
from my_claude_code.providers.runtime.reasoning_probe import (
    ReasoningProbeOutcome,
    probe_reasoning_dialect,
)
from my_claude_code.websearch.analytics import retune_shared_store

from .discovery_timer import ProviderDiscoveryTimer, resolve_refresh_interval
from .loop_heartbeat import LoopHeartbeat
from .provider_manager import ProviderRuntimeManager
from .proxy_check_timer import ProxyCheckTimer, ProxyHealthTimer
from .proxy_feed_timer import ProxyFeedTimer
from .stall_watchdog import StallWatchdog

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


def _config_gesture(updates: Mapping[str, Any]) -> str:
    """What to call this settings write in a busy ``/health`` answer.

    One short sentence an operator can act on, built from the keys being
    written and never from their values -- a settings update can carry an API
    key, and the busy answer is served to anything that can reach the port.
    """

    keys = [str(key) for key in updates]
    if not keys:
        return "a settings change is being applied"
    if len(keys) == 1:
        return f"the setting {keys[0]} is being applied"
    return f"{len(keys)} settings are being applied"


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
        # The loop-lag monitor. One sleep and one subtraction, ten times a
        # second, and the only thing in this process that can tell a server
        # that is busy from a server that is gone.
        self._loop_heartbeat: LoopHeartbeat | None = None
        # The stuck-request watchdog. One sleep and a tuple comparison per
        # in-flight request, and the only thing in this process that can say
        # which await a request that has gone quiet is parked on. It observes
        # and never intervenes -- see runtime/stall_watchdog.py.
        self._stall_watchdog: StallWatchdog | None = None
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
        # The optional proxy checker. Off unless the operator asked for it, and
        # entirely out of band: it writes the store and the two ledgers, and it
        # constructs no provider and republishes no generation.
        self._proxy_check_timer = ProxyCheckTimer(
            self._proxy_check_targets,
            lambda: self.settings.proxy_check_interval_minutes,
            lambda: self.settings.proxy_check_enabled,
            lambda: self.settings.proxy_check_exit_ip_url,
        )
        # The optional feed refresh. Off unless the operator asked for it, and
        # a pass over zero enabled feeds is a pass that makes no request at
        # all -- which is what every install has until somebody ticks a feed.
        # It writes the candidate list and nothing else: no chain, no provider,
        # no generation.
        self._proxy_feed_timer = ProxyFeedTimer(
            lambda: self.settings.proxy_feed_refresh_minutes,
            lambda: self.settings.proxy_feed_refresh_enabled,
            # The live Settings, because a pass has to pick a destination to
            # test against and that depends on which providers are configured
            # right now. A pass with nowhere to test does nothing at all --
            # storing addresses it could not measure is exactly what 7.21.0
            # removed from the button, and a timer is the last place to keep it.
            settings=lambda: self.settings,
        )
        # The health re-prober, and the writer that makes a bench survive a
        # restart. On by default, unlike the two above, because it is not a new
        # conversation: it re-tests only addresses that have already failed on
        # this operator's own traffic, only in chains they switched on, and
        # only against provider hosts they already route to. Its other half --
        # writing the bench into the store -- runs whether probing is on or
        # off, because a file write is not an outbound request.
        self._proxy_health_timer = ProxyHealthTimer(
            lambda: self.settings,
            lambda: self.settings.proxy_health_reprobe_enabled,
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
            # First of everything, and deliberately: the instrument that
            # measures how long the rest of this start holds the loop has to be
            # running before the rest of this start runs. It is one task, one
            # sleep and one subtraction; it cannot fail and it waits for
            # nothing.
            self._start_loop_heartbeat()
            # Beside the beat, and for the same reason: an instrument that is
            # started late cannot describe what happened before it. It watches
            # requests, and a request can arrive the instant the listener is up.
            self._start_stall_watchdog()
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
            # Before the first request, and cheap: one file read that re-arms
            # the refusals a previous run measured. The interception ledger is
            # process-lifetime state and the store is durable, so without this
            # a restart would quietly re-admit every address already found
            # terminating TLS.
            # First, and before either store is read: the engine in ``core``
            # is handed the operator's ladder and cooldown pair, exactly as
            # 7.22.0 hands the credential pool its ``RateLimitCooldown``.
            # ``core`` may not import ``config``, so the policy is built here,
            # where ``Settings`` is, and passed in. Ordering matters -- the
            # re-arm below clamps each stored bench to the ladder's own
            # window, so the ladder has to be the new one by then.
            configure_proxy_rotation(
                cooldown_seconds=self.settings.proxy_cooldown_seconds,
                cooldown_max_seconds=self.settings.proxy_cooldown_max_seconds,
                reachability_tiers=parse_lockout_tiers(
                    self.settings.proxy_reachability_tiers
                ),
            )
            state.mark("proxy-refusals")
            await asyncio.to_thread(arm_refusals_from_store)
            # And the other half of the same argument. A reachability bench is
            # process-lifetime state too, and since 7.19.0 an address that
            # failed comes back only after a check that PASSES -- so a restart
            # that forgot the benches would put every dead proxy back into
            # rotation and charge the operator a connect timeout each to
            # rediscover them.
            await asyncio.to_thread(arm_health_from_store)
            # The speed ledger (7.54.0): measurements, not verdicts, so a
            # missing or damaged file is simply an empty history.
            await asyncio.to_thread(load_speed)
            # And the third: a feed fetch is memory, so one that was running
            # when this process's predecessor stopped would otherwise be
            # reported as still running for ever. This reads the record it left
            # and reports it as interrupted -- with its counters, and with the
            # addresses it had already found, which the sweep writes as it goes
            # rather than only at the end.
            await asyncio.to_thread(recover_fetch_job)
            install_listener()
            self._proxy_check_timer.start()
            self._proxy_health_timer.start()
            # Before the feed timer reads the store, and before the Proxying
            # page can be opened: an install upgrading from 7.17.1 still names
            # its feeds by the ids of the seven this product used to ship, and
            # this converts them to the custom entries that mean the same
            # thing. One write, once -- a store already converted comes back
            # with nothing to do. See ``config.proxy_feed_legacy``.
            state.mark("proxy-feeds")
            await asyncio.to_thread(migrate_proxy_feeds)
            self._proxy_feed_timer.start()
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
        # Named, not moved. Measured on a 52-provider scratch config with the
        # reporting machine's 995 visibility patterns: a one-key pause costs
        # 2.1 s and a resume 2.3 s, all of it a ``Settings`` rebuild and a
        # 51 KB managed-file render on the loop. 7.27.0 does not change that --
        # the 2026-09-11 measurements rejected ``asyncio.to_thread`` for it,
        # because the work holds the GIL and a worker thread stalls the loop
        # just as hard -- so the honest thing this release can do is let
        # ``/health`` say which gesture the loop is inside. The fix is a
        # follow-up with its own release.
        with loop_health().working(_config_gesture(updates)):
            return await self._apply_admin_config_prepared(updates)

    async def _apply_admin_config_prepared(
        self,
        updates: Mapping[str, Any],
    ) -> dict[str, Any]:
        prepared = prepare_admin_update(
            updates, messaging_running=self._messaging_workflow is not None
        )
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
        previous = self.settings

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
        await self._apply_live_settings(previous, prepared.settings)
        self._pending_fields = []
        result["restart"] = self._restart_metadata((), prepared.settings)
        return result

    async def _apply_live_settings(self, previous: Settings, current: Settings) -> None:
        """Move what this runtime built at start onto a saved configuration.

        The generation swap above is what makes most fields hot: a provider,
        its pool and its limiter are built from the new settings, and every
        request handler is built per request from its lease. What it cannot
        reach is what ``start`` built once and kept -- the loop-lag monitor,
        the stall watchdog, the three settings-driven loops, the proxy
        engine's ladder and the two shared log stores. Each is moved here, in
        place, and only when a value it reads actually changed.

        Nothing here interrupts a request. The monitors and loops are
        observers; a loop asleep is re-armed, a loop mid-pass is left to finish
        it; the stores keep their writer and every queued record; and a proxy
        bench already running keeps its record, re-armed by the engine's own
        ladder rule rather than cleared.
        """

        def changed(*attrs: str) -> bool:
            return any(getattr(previous, a) != getattr(current, a) for a in attrs)

        retune_request_log_store(current)
        retune_shared_store(current)
        set_third_party_verbosity(bool(current.log_raw_api_payloads))
        if changed(
            "proxy_cooldown_seconds",
            "proxy_cooldown_max_seconds",
            "proxy_reachability_tiers",
        ):
            # The engine's own entry point, called again: the ladder re-arms
            # benches already on the books by its own rule (``set_tiers``) and
            # the cooldown pair is updated in place, which is the object every
            # engine already reads. ``core/proxy_rotation.py`` is unchanged.
            configure_proxy_rotation(
                cooldown_seconds=current.proxy_cooldown_seconds,
                cooldown_max_seconds=current.proxy_cooldown_max_seconds,
                reachability_tiers=parse_lockout_tiers(
                    current.proxy_reachability_tiers
                ),
            )
        if not self._started:
            # ``start`` reads every one of these itself.
            return
        if changed("health_heartbeat_interval_ms", "health_busy_lag_ms"):
            interval = self._configure_loop_health(current)
            if self._loop_heartbeat is not None:
                self._loop_heartbeat.set_interval(interval)
        if changed(
            "request_watchdog_enabled",
            "request_watchdog_interval_seconds",
            "request_inflight_enabled",
        ):
            await self._rearm_stall_watchdog(current)
        if changed("model_discovery_refresh_seconds"):
            self._discovery_timer.rearm()
        if changed("proxy_check_enabled", "proxy_check_interval_minutes"):
            self._proxy_check_timer.rearm()
        if changed("proxy_feed_refresh_enabled", "proxy_feed_refresh_minutes"):
            self._proxy_feed_timer.rearm()

    @staticmethod
    def _configure_loop_health(settings: Settings) -> float:
        """Push the two loop-health numbers into the record; return the beat."""

        interval = max(10, int(settings.health_heartbeat_interval_ms)) / 1000.0
        loop_health().configure(
            interval_seconds=interval,
            busy_lag_seconds=max(0, int(settings.health_busy_lag_ms)) / 1000.0,
        )
        return interval

    async def _rearm_stall_watchdog(self, settings: Settings) -> None:
        """Start, stop or re-time the watchdog to match a saved configuration."""

        watchdog = self._stall_watchdog
        if settings.request_watchdog_enabled and watchdog is None:
            self._start_stall_watchdog()
            return
        request_tasks.configure(
            enabled=bool(settings.request_watchdog_enabled),
            inflight=bool(getattr(settings, "request_inflight_enabled", True)),
        )
        if watchdog is None:
            return
        if not settings.request_watchdog_enabled:
            self._stall_watchdog = None
            await watchdog.close()
            return
        watchdog.set_interval(settings.request_watchdog_interval_seconds)

    def cached_model_ids(self) -> dict[str, frozenset[str]]:
        """Return cached discovered model ids per provider for admin display."""
        return self.provider_manager.cached_model_ids()

    async def reload_providers(
        self,
        reason: str,
        *,
        refresh_provider_id: str | None = None,
        sweep: bool = True,
        rebuild_provider_ids: frozenset[str] | None = None,
    ) -> dict[str, Any]:
        """Republish the provider generation after a non-Settings mutation.

        Custom provider registry entries live outside Settings; the mutation is
        already persisted by the caller, so the commit boundary is a no-op and
        only the provider runtime needs a fresh generation.

        ``refresh_provider_id`` names the one provider the caller just changed.
        It replaces the generation's blanket background sweep with a single
        scoped, awaited discovery, and returns what that discovery found -- so
        the caller reports the catalogue, not a second independent probe.

        ``sweep=False`` says the opposite thing about the same sweep: the
        mutation did not change any provider's *catalogue*, so nothing needs
        re-querying at all. A proxy chain is the case it exists for -- a chain
        edit changes the address a provider dials from, not the models that
        provider has -- and until 7.27.0 every chain save fired the blanket
        ``/models`` sweep of *every* configured provider, measured on the
        reporting machine at 1.6-9.7 s of held event loop per call and fired
        three or more times by one bulk add. The generation is still replaced,
        so the new chain is what routes the instant the save returns; only the
        sweep it never needed is gone.

        ``rebuild_provider_ids`` names the providers whose build-time inputs
        the mutation changed (7.55.0). ``None`` -- every caller but the proxy
        chain routes -- rebuilds every provider, as it always did. A set keeps
        every other provider's already-built object, pool and benches included;
        see ``ProviderRuntimeManager.replace``.
        """
        async with self._config_lock:
            await self.provider_manager.replace(
                self.settings,
                commit=lambda: None,
                reason=reason,
                background_refresh=sweep and refresh_provider_id is None,
                rebuild_provider_ids=rebuild_provider_ids,
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
            # What the long gestures actually cost the event loop, newest
            # first. ``/health`` says "busy, because <gesture>" while a hold is
            # happening; this says how late the loop was for each of the last
            # gestures once they are over, so the follow-up table in the perf
            # specs is reproducible from the dashboard rather than only from a
            # harness. Additive: every existing key above is untouched.
            "loop_health": self.loop_health_status(),
        }

    def loop_health_status(self) -> dict[str, Any]:
        """What the last gestures cost the loop, newest first.

        ``/health`` says "busy, because <gesture>" while a hold is happening;
        this says how late the loop was for each of the last gestures once they
        are over, so the follow-up tables in the perf specs are reproducible
        from the dashboard rather than only from a harness.
        """

        record = loop_health()
        return {
            "busy_lag_ms": round(record.busy_lag_seconds * 1000.0),
            "gestures": [
                gesture.as_body_fields()
                for gesture in reversed(record.recent_gestures())
            ],
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
            # Per model, not per press: a probe of a free Zen model is exactly
            # the request its free tier meters, so under
            # ``OPENCODE_FREE_TIER_CREDENTIAL=public`` it is spent out of the
            # shared bucket. Identity for every other provider and every paid
            # model, so nothing else here moves.
            outcomes = await probe_model_capabilities(
                base_url,
                probe_credential(provider_id, model, api_key),
                model,
                probes=probes,
                proxy=proxy,
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
        credential = probe_fallback_credential(
            provider_id, provider_credential(descriptor, self.settings)
        )
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
        # Deliberately not named again here: ``refresh_model_list_cache``
        # already declares "every provider's model list is being refreshed" for
        # exactly this call, and a second frame around it put the same sweep in
        # the Server responsiveness readout twice under two names.
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

    def _proxy_check_targets(self) -> dict[str, str]:
        """Which addresses the background checker should measure, and against what.

        Read from the store on every tick rather than captured once: a chain
        edited while the server runs takes effect at the next sweep, and an
        address removed from the last chain that named it stops being asked
        about at all.
        """

        return check_targets(self.settings, current_proxy_chains())

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

    def _start_loop_heartbeat(self) -> None:
        """Adopt the operator's two numbers and start the beat.

        Called at the top of ``start``. The record itself is process-wide (it
        is read by the ASGI gate, which has no runtime in hand), so the numbers
        are pushed into it here rather than read from ``Settings`` per answer --
        ``core`` may not import ``config``, and a health answer may not resolve
        configuration.
        """

        interval = self._configure_loop_health(self.settings)
        heartbeat = LoopHeartbeat(interval_seconds=interval)
        heartbeat.start()
        self._loop_heartbeat = heartbeat

    def _start_stall_watchdog(self) -> None:
        """Adopt the operator's four numbers and start watching.

        The registry in ``core`` is told whether to track requests at all from
        here, for the same reason the loop-health record is: ``core`` may not
        import ``config``, and a request path may not resolve configuration.
        The registry has two readers -- this watchdog and the in-flight view --
        and it tracks requests while either is switched on. Switching both off
        empties it, so a server the operator told not to watch holds nothing.
        """

        settings = self.settings
        request_tasks.configure(
            enabled=bool(settings.request_watchdog_enabled),
            inflight=bool(getattr(settings, "request_inflight_enabled", True)),
        )
        if not settings.request_watchdog_enabled:
            return
        watchdog = StallWatchdog(
            stall_seconds=lambda: float(self.settings.request_watchdog_stall_seconds),
            interval_seconds=settings.request_watchdog_interval_seconds,
            log_max_bytes=lambda: (
                int(self.settings.request_watchdog_log_max_mb) * 1024 * 1024
            ),
            retain_files=lambda: int(self.settings.server_log_retain_files),
        )
        watchdog.start()
        self._stall_watchdog = watchdog

    async def _close_owned_resources(self) -> bool:
        heartbeat = self._loop_heartbeat
        self._loop_heartbeat = None
        if heartbeat is not None:
            await heartbeat.close()
        # Before anything that could take time: it is one cancel and one await
        # of a task that is asleep, so it always settles well inside the stop
        # deadline, and a watchdog still sweeping a registry being torn down
        # would report a shutdown as a stall.
        watchdog = self._stall_watchdog
        self._stall_watchdog = None
        if watchdog is not None:
            await watchdog.close()
        request_tasks.reset()
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
        await self._proxy_check_timer.close()
        await self._proxy_feed_timer.close()
        await self._proxy_health_timer.close()
        remove_listener()
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
