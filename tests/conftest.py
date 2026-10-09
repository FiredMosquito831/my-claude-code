import asyncio
import contextlib
import logging
import os
import threading
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from loguru import logger as loguru_logger

from my_claude_code.config import logging_config
from my_claude_code.config.logging_config import InterceptHandler
from my_claude_code.config.settings import Settings
from tests.providers.support import passthrough_rate_limiter

# Any test whose subject is the Windows autostart registration takes the
# ``fake_winreg`` fixture. It lives in one place rather than beside its tests so
# that both files driving ``set_start_at_login`` share the same fake;
# ``tests/cli/test_desktop_rtk.py`` not having one is what deleted the
# developer's real ``Run`` value, twice.
from tests.support.fake_winreg import fake_winreg  # noqa: F401  (shared fixture)

# The hermeticity guard. Importing its fixtures and hook implementations here is
# what installs them for the whole suite: ``isolate_the_machine`` gives every
# test its own HOME/APPDATA, ``hermetic_marker_gates`` projects the opt-in
# markers into the process-wide interceptors, ``pytest_configure`` installs
# those interceptors, ``pytest_runtest_setup`` opens the opt-in gates before any
# fixture of any scope is built, and ``pytest_runtest_teardown`` fails a test
# that resolved the developer's real config directory. ``isolate_the_machine`` is bound before
# any fixture defined below, on purpose: autouse fixtures run in collection
# order and every one of those resolves a path that must already be redirected.
# See ``tests/support/hermetic.py`` and ``tests/README.md``.
from tests.support.hermetic import (  # noqa: F401  (re-exported fixtures/hooks)
    hermetic_marker_gates,
    isolate_the_machine,
    pytest_configure,
    pytest_runtest_setup,
    pytest_runtest_teardown,
)
from tests.support.hermetic import under_real_config_dir as _under_real_home

# Set mock environment BEFORE any imports that use Settings
os.environ.setdefault("NVIDIA_NIM_API_KEY", "test_key")
os.environ.setdefault("MODEL", "nvidia_nim/test-model")
os.environ["PTB_TIMEDELTA"] = "1"
# Ensure tests don't pick up a server API key from the repo .env
# (tests expect endpoints to be unauthenticated by default)
os.environ["ANTHROPIC_AUTH_TOKEN"] = ""

Settings.model_config = {**Settings.model_config, "env_file": None}


@pytest.fixture(autouse=True)
def _isolate_from_dotenv(monkeypatch):
    """Prevent Pydantic BaseSettings from reading the .env file during tests."""
    monkeypatch.setattr(
        Settings, "model_config", {**Settings.model_config, "env_file": None}
    )


@pytest.fixture(autouse=True)
def _reset_stop_deadline():
    """No test may inherit another test's "the server is stopping" state.

    The stop deadline is process-wide on purpose -- the supervisor sets it once
    and the ASGI gate, the provider drain and the response cleanup all read the
    same clock -- so a test that requests a stop would otherwise make every
    later test in the same worker refuse its own provider leases.
    """
    from my_claude_code.core.stop_deadline import stop_deadline

    stop_deadline().clear()
    yield
    stop_deadline().clear()


@pytest.fixture(autouse=True)
def _reset_seen_settings_files():
    """7.69.7: the settings-save reader remembers which files held settings.

    A file it has seen holding settings that is suddenly missing or empty is
    retried and refused rather than read as a fresh install, so no test may
    inherit another test's memory of a path.
    """
    from my_claude_code.config.admin.env_io import forget_seen_settings_files

    forget_seen_settings_files()
    yield
    forget_seen_settings_files()


@pytest.fixture(autouse=True)
def _reset_listener_state():
    """7.69.2: two process-wide facts about the listening socket.

    The composition root replaces ``IocpProactor.accept`` on Windows, class-wide,
    and the listener guard publishes whether the socket is open for the session
    row. No test may inherit either: a test of CPython's own accept loop must
    see CPython's, and a session row must not report another test's listener.
    """
    from my_claude_code.core.request_log import set_server_listening
    from my_claude_code.runtime.windows_accept import uninstall_keep_accepting

    uninstall_keep_accepting()
    set_server_listening(None)
    yield
    uninstall_keep_accepting()
    set_server_listening(None)


@pytest.fixture(autouse=True)
def _reset_old_server_cleanup_world():
    """7.72.0: a server's own start stops old servers of its port and folder.

    That start-time cleanup reads the machine through one process-wide seam
    (``cli.rescue.set_cleanup_world_factory``). No test may reach the real
    process table, socket table or stop path through it: every test gets an
    inert machine -- nothing listed, nothing alive -- whose stop raises a
    ``HermeticityViolation``; a test of the cleanup supplies its own machine.
    """
    from my_claude_code.cli import rescue
    from tests.support.token_host_block import HermeticityViolation

    def refuse(chain: Any) -> bool:
        raise HermeticityViolation(
            f"a test reached the real old-server stop path for {chain!r}"
        )

    def inert(_request_log_path: Path) -> rescue.CleanupWorld:
        return rescue.CleanupWorld(
            listening=lambda: frozenset(),
            processes=lambda: [],
            sessions=lambda: [],
            alive=lambda _pid: False,
            stop_chain=refuse,
            info=lambda _message: None,
            warning=lambda _message: None,
            console=lambda _message: None,
            stopping=lambda: False,
        )

    rescue.set_cleanup_world_factory(inert)
    yield
    rescue.set_cleanup_world_factory(None)


@pytest.fixture(autouse=True)
def _isolate_oauth_ownership_state():
    """7.69.1: the shared-credential module keeps two process-wide flags.

    The one-time migration is switched OFF by default -- a test that is about
    the migration turns it back on with ``reset_migration_flag()`` -- and the
    once-per-token decision cache starts empty, so no test inherits another's
    "already said that".
    """
    from my_claude_code.providers.anthropic_oauth import shared as oauth_shared

    oauth_shared._MIGRATION_DONE = True
    oauth_shared.reset_once_cache()
    yield
    oauth_shared._MIGRATION_DONE = True
    oauth_shared.reset_once_cache()


@pytest.fixture(autouse=True)
def _reset_request_task_registry():
    """No test may inherit another test's in-flight requests.

    The registry the stuck-request watchdog reads is process-wide for the same
    reason the stop deadline is: one dictionary is read by a background task,
    by an admin route and by whichever task is serving a request, and none of
    them has a runtime in hand. A capture built and never finalized -- which a
    great many unit tests do deliberately -- would otherwise be counted as
    in-flight by every later test in the same worker.
    """

    from my_claude_code.core import request_tasks

    request_tasks.reset()
    yield
    request_tasks.reset()


@pytest.fixture(autouse=True)
def _reset_rejected_client_tracker():
    """No test may inherit another test's rejected-401-client memory.

    ``RejectedClientTracker`` is process-wide for the same reason
    ``core.request_tasks`` is above: a test that rejects a client with a bad
    proxy token must not have that client already "seen" because an earlier
    test in the same worker rejected one that happens to hash the same way,
    and must not leave its own clients behind for the next test either.
    """

    from my_claude_code.api.rejected_client_log import rejected_client_tracker

    rejected_client_tracker().reset()
    yield
    rejected_client_tracker().reset()


@pytest.fixture(autouse=True)
def _isolate_opencode_client_version():
    """No test may inherit another test's reading of the machine.

    The OpenCode version behind the outbound user-agent is cached process-wide
    for an hour, because it is read off disk and does not change between two
    requests. A test that pins it would otherwise pin it for whatever ran next.
    """
    from my_claude_code.providers.openai_chat import opencode_identity

    opencode_identity.reset_version_cache()
    yield
    opencode_identity.reset_version_cache()


@pytest.fixture(autouse=True)
def _isolate_learned_facts():
    """No test may inherit another test's learned facts.

    The store is a process-wide singleton on purpose -- a provider instance is
    rebuilt on every config apply while what the host said about itself is not
    -- and it hands the *same* memory to every provider built with the same
    catalogue id. That is exactly the behaviour the feature wants and exactly
    the behaviour that makes two tests constructing "the same" provider share
    a table of refusals.
    """
    from my_claude_code.providers.chatgpt_oauth.provider import WITHHELD_MODEL_IDS
    from my_claude_code.providers.recovery import store as learned_store

    learned_store.reset_learned_fact_store()
    WITHHELD_MODEL_IDS.clear()
    WITHHELD_MODEL_IDS.sink = None
    yield
    learned_store.reset_learned_fact_store()
    WITHHELD_MODEL_IDS.clear()
    WITHHELD_MODEL_IDS.sink = None


@pytest.fixture(autouse=True)
def _no_first_start_side_effects(monkeypatch):
    """``serve()`` initialises the config home; no unit test may really do it.

    Since 6.65.0 the first thing ``cli.commands.serve`` does is create the
    config directory, move a legacy ``~/.fcc`` into place and write a
    ``.env``. Every ``serve()`` unit test would therefore migrate its
    hermetic home -- and the liveness probe inside the migration knocks on
    the port the ``Settings`` default names, which on a developer machine
    is their own running server. ``tests/cli/test_first_start.py`` exercises
    the real function directly, against a home of its own.
    """

    from my_claude_code.cli import commands

    monkeypatch.setattr(commands, "ensure_config_home_or_exit", lambda: "")


@pytest.fixture(autouse=True)
def _isolate_server_log(monkeypatch, tmp_path):
    """Keep ``logs/server.log`` out of any config directory a test resolves.

    Since 6.65.0 the 6.30.0 refusal appends itself to the server log before
    the composition root exists, so a unit test of the startup guard writes
    a file where previously it wrote none. ``LOG_FILE`` is the same override
    the composition root already honours, and pointing it at ``tmp_path``
    keeps every such write inside the test that made it.
    """

    monkeypatch.setenv("LOG_FILE", str(tmp_path / "server.log"))


@pytest.fixture(autouse=True)
def _isolate_request_log(monkeypatch, tmp_path):
    """Keep request-log writes out of the real ~/.fcc directory during tests."""
    from my_claude_code.core import request_log

    request_log.set_request_log_path(tmp_path / "requests.db")
    # The historical cost backfill runs only when a pricer is registered, and
    # the registration is process-wide. A test that registers one must not
    # leave the next test's store quietly rewriting its rows -- the house rule
    # for a singleton is that a test never inherits another test's copy of it.
    request_log.set_cost_backfill_pricer(None)
    yield
    request_log.set_request_log_path(None)
    request_log.set_cost_backfill_pricer(None)
    request_log.reset_request_log_stores()


@pytest.fixture(autouse=True)
def _reset_derived_refresh_state():
    """Forget any in-flight derived-payload refresh between tests.

    ``application.derived_payloads`` keeps one process-wide set of the entries
    currently being recomputed, so that a page polling three times does not
    start three twelve-second recomputations of the same answer. It is a
    singleton, and the house rule for a singleton is that a test never inherits
    another test's copy of it. The minute-long in-memory Analytics answers
    (7.69.4) are the same kind of singleton and are forgotten the same way.
    """
    from my_claude_code.application import derived_payloads

    with derived_payloads._refresh_lock:
        derived_payloads._refreshing.clear()
    derived_payloads.recent_analytics().clear()
    yield
    with derived_payloads._refresh_lock:
        derived_payloads._refreshing.clear()
    derived_payloads.recent_analytics().clear()


@pytest.fixture(autouse=True)
def _isolate_websearch_analytics(monkeypatch, tmp_path):
    """Keep the web-search analytics database out of the real config directory.

    ``WebSearchAnalytics`` defaults to ``<config dir>/logs/websearch.db`` and
    creates that directory on construction. The request log has been isolated
    since forever; this store never was, so on a machine with no redirected
    HOME the suite wrote a real ``websearch.db`` into the developer's config
    directory -- and on a machine with neither config directory it *created*
    one, which then wins resolution over the legacy home for good.
    """
    from my_claude_code.websearch import analytics

    real_default = analytics.default_websearch_db_path

    def redirect_only_if_it_would_hit_the_real_home() -> Path:
        # Evaluated lazily, so a test that patches ``Path.home`` itself (and
        # then asserts on ``default_websearch_db_path()``) still sees its own
        # answer; only an unredirected resolution is diverted.
        path = real_default()
        return tmp_path / "websearch.db" if _under_real_home(path) else path

    monkeypatch.setattr(
        analytics,
        "default_websearch_db_path",
        redirect_only_if_it_would_hit_the_real_home,
    )
    analytics.reset_analytics_state()
    yield
    analytics.reset_analytics_state()


@pytest.fixture(autouse=True)
def _isolate_messaging_state_dir(monkeypatch, tmp_path):
    """Keep ``<config dir>/agent_workspace`` out of the real config directory.

    ``ApplicationRuntime._start_messaging_workflow`` calls ``os.makedirs`` on
    it, so a test that composes the runtime created a real directory tree --
    including, on a machine with no config directory yet, the ``~/.mcc`` that
    then outranks a legacy ``~/.fcc`` for good. Redirected lazily so a test that
    points the config dir somewhere itself still gets its own answer.
    """
    from my_claude_code.runtime import application

    real_default = application.messaging_state_dir_path

    def redirect_only_if_it_would_hit_the_real_home() -> Path:
        path = real_default()
        return tmp_path / "agent_workspace" if _under_real_home(path) else path

    monkeypatch.setattr(
        application,
        "messaging_state_dir_path",
        redirect_only_if_it_would_hit_the_real_home,
    )
    yield


@pytest.fixture(autouse=True)
def _isolate_harness_tiers(monkeypatch, tmp_path):
    """No test may read the developer's own per-agent tier overrides.

    The file is read on the request path, so a real one on the machine running
    the suite would silently move which model a tier resolves to and make a
    routing test pass or fail for a reason that is not in the repository.
    """
    from my_claude_code.config import harness_tiers

    monkeypatch.setattr(
        harness_tiers, "harness_tiers_path", lambda: tmp_path / "harness_tiers.json"
    )
    harness_tiers.reset_harness_tiers_cache()
    yield
    harness_tiers.reset_harness_tiers_cache()


@pytest.fixture(autouse=True)
def _reset_proxy_chain_not_routing():
    """No test may inherit another's "Saved -- not routing yet" (7.78.8).

    The Proxying page remembers, per provider, a chain save whose provider
    rebuild failed, until a rebuild of that provider succeeds. It is process
    state on purpose -- the card must keep saying it across page loads -- so a
    test that makes a rebuild fail would otherwise leave every later test's
    card saying it too.
    """
    from my_claude_code.api import admin_proxy_routes

    admin_proxy_routes._NOT_ROUTING.clear()
    yield
    admin_proxy_routes._NOT_ROUTING.clear()


@pytest.fixture(autouse=True)
def _isolate_desktop_shell(monkeypatch, tmp_path):
    """No test may download the desktop shell, or install one at the developer.

    ``ShellWindow.create()`` is the first link of the ``auto`` window chain and
    it *fetches* -- a real 1.5 MB archive, from the real GitHub release, on the
    first call. Any test that builds a window with no preference would therefore
    reach the network, take a second to do it, and fail on an offline CI runner.
    Both halves are closed here: the install directory is redirected under
    ``tmp_path``, and the shell is switched off, so the chain behaves as it did
    before 6.44.0 unless a test deliberately turns it on.
    """
    from my_claude_code.config import desktop_shell

    monkeypatch.setenv(
        desktop_shell.DESKTOP_SHELL_DIR_ENV, str(tmp_path / "desktop-shell-bin")
    )
    monkeypatch.setenv(desktop_shell.DESKTOP_SHELL_ENABLED_ENV, "off")


@pytest.fixture(autouse=True)
def _isolate_client_fingerprint():
    """The mirrored client fingerprint must not survive from one test to the next.

    ``install_fingerprint`` writes a ContextVar that the Anthropic subscription
    provider reads to reproduce the caller's own headers upstream. Anything that
    builds a request capture sets it as a side effect, and under xdist the next
    test on that worker inherits it -- which is how a fixture user-agent from an
    unrelated capture test made ``test_oauth_headers_are_the_claude_code_set``
    fail on a particular shard order and pass on every other. The leak was always
    there; it only became reachable when a second suite started capturing. Clear
    it around every test rather than asking each new one to remember.
    """
    from my_claude_code.core.client_fingerprint import install_fingerprint

    install_fingerprint(None)
    yield
    install_fingerprint(None)


@pytest.fixture(autouse=True)
def _isolate_route_health():
    """Benches must not survive from one test into the next.

    The registry is deliberately shared across requests -- three consecutive
    failures cannot be observed by three registries that each start empty --
    which makes it process state, and process state leaks between tests.
    """
    from my_claude_code.application import execution

    execution.reset_route_health_registries()
    yield
    execution.reset_route_health_registries()


@pytest.fixture(autouse=True)
def _isolate_media_books():
    """Media keeps its own route, key and proxy books (7.60.0); reset them too."""
    from my_claude_code.application.media import executor as media_executor
    from my_claude_code.providers.media import proxy_pool, registry

    media_executor.reset_media_route_health_registries()
    proxy_pool.reset_media_proxy_books()
    registry.MEDIA_REGISTRY.forget()
    yield
    media_executor.reset_media_route_health_registries()
    proxy_pool.reset_media_proxy_books()
    registry.MEDIA_REGISTRY.forget()


@pytest.fixture(autouse=True)
def _isolate_exit_memory():
    """What a ticked chain remembers about its exits (7.81.0) is process-wide.

    On purpose -- it has to outlive a provider rebuild -- which makes it process
    state, and a spent exit one test remembered would be skipped in the next.
    """
    from my_claude_code.core.proxy_exit_memory import EXIT_MEMORY, MEDIA_EXIT_MEMORY

    EXIT_MEMORY.clear()
    MEDIA_EXIT_MEMORY.clear()
    yield
    EXIT_MEMORY.clear()
    MEDIA_EXIT_MEMORY.clear()


@pytest.fixture(autouse=True)
def _reset_config_dir_cache():
    """Forget the cached config-directory decision before each test.

    ``config_dir_resolution()`` caches the resolved directory for the life of
    the process; tests that redirect ``HOME``/``USERPROFILE`` to a ``tmp_path``
    would otherwise keep using a directory resolved by an earlier test.
    """
    from my_claude_code.config import paths

    paths.reset_config_dir_cache()
    yield
    paths.reset_config_dir_cache()


@pytest.fixture(autouse=True)
def _reset_image_geometry_cache():
    """Forget memoised image dimensions between tests.

    ``core.image_geometry`` memoises width/height per image for the life of the
    process, because Claude Code re-sends the same screenshot on every turn of
    a conversation and re-parsing its header every time is pure cost. That memo
    is a process-wide singleton, so a test that asserts on the hit/miss
    counters -- or one that reuses a fixture image another test already
    measured -- would otherwise read the previous test's state.
    """
    from my_claude_code.core import image_geometry

    image_geometry.reset_image_geometry_cache()
    yield
    image_geometry.reset_image_geometry_cache()


@pytest.fixture(autouse=True)
def _reset_image_downscale_cache():
    """Forget memoised image shrinks, and their byte bound, between tests.

    ``core.anthropic.image_downscale`` keeps each finished shrink for the life
    of the process, so the second rung of a fallback chain -- and the next turn
    of a conversation -- reuses the first one's bytes instead of resizing the
    same screenshot again. That memo and the bound it has learned are a
    process-wide singleton: a test asserting on its counters, or on how much
    work a chain did, would otherwise read an earlier test's entries.
    """
    from my_claude_code.core.anthropic import image_downscale

    image_downscale.reset_image_downscale_cache()
    yield
    image_downscale.reset_image_downscale_cache()


@pytest.fixture(autouse=True)
def _reset_process_settings_cache():
    """Forget the process-wide ``Settings`` between tests.

    ``get_settings()`` is memoized for the life of the process and cleared
    when a configuration is applied. Since 6.62.0 the web-tools path reads it
    instead of building a fresh ``Settings`` per request, so a test that sets
    an environment variable and then drives a web search would otherwise be
    answered from whatever the previous test's environment produced.
    """
    from my_claude_code.config.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_models_dev_payload_cache():
    """Forget the parsed models.dev payload between tests.

    Since 6.62.0 the 4.9 MB cache file is parsed once per on-disk generation
    and every index shape is built from that one parse. The memo is keyed by
    ``(path, mtime_ns, size)``, but two tests can write different payloads to
    the same ``tmp_path`` within one filesystem timestamp tick, so the memo is
    dropped between tests rather than trusted to notice.
    """
    from my_claude_code.providers.runtime import models_dev

    models_dev.reset_models_dev_payload_cache()
    yield
    models_dev.reset_models_dev_payload_cache()


@pytest.fixture(autouse=True)
def _reset_normalize_candidates_cache():
    """Forget memoised model-id match keys between tests.

    ``core.model_ids.normalize_candidates`` is an ``lru_cache`` over a pure
    function, so nothing it returns can go stale -- but a test that asserts on
    its ``cache_info`` needs to start from zero.
    """
    from my_claude_code.core import model_ids

    model_ids.normalize_candidates.cache_clear()
    yield
    model_ids.normalize_candidates.cache_clear()


@pytest.fixture(autouse=True)
def _reset_credential_digests():
    """Forget the configured-credential digests between tests.

    ``core.wire_capture`` holds sha256 digests of the credentials the running
    generation was configured with, so a value equal to one of them is redacted
    under any key. The set is process-wide and is installed whenever a provider
    generation is published, so a test that publishes one would otherwise leave
    the next test redacting strings it never configured.
    """
    from my_claude_code.core import wire_capture

    wire_capture.reset_credential_digests()
    yield
    wire_capture.reset_credential_digests()


@pytest.fixture(autouse=True)
def _isolate_proxy_fetch_status(monkeypatch, tmp_path):
    """No test may write the real config directory's fetch-status file.

    A fetch job became durable in 7.22.1 -- it has to be, or a server restarted
    mid-sweep reports ``running`` for ever about a job nothing is doing -- and
    that means ``start_fetch`` writes a document beside the operator's own
    stores. Every test that starts one would otherwise write the developer's,
    and inherit whatever the last one left.
    """
    from my_claude_code.application import proxy_fetch

    path = tmp_path / "fcc-config" / "proxy_fetch_status.json"
    monkeypatch.setattr(proxy_fetch, "proxy_fetch_status_path", lambda: path)
    proxy_fetch.reset_fetch_job()
    yield path
    proxy_fetch.reset_fetch_job()


@pytest.fixture(autouse=True)
def _isolate_proxy_speed(monkeypatch, tmp_path):
    """No test may write the real speed ledger, or see another test's samples.

    The ledger is process-wide (7.54.0) and every check, fetch and logged
    request feeds it, so without this one test's measurements would rank the
    next test's addresses -- and a flush would write the developer's file.
    """
    from my_claude_code.application import proxy_speed_store
    from my_claude_code.core.proxy_speed import PROXY_SPEED

    path = tmp_path / "fcc-config" / "proxy_speed.json"
    monkeypatch.setattr(proxy_speed_store, "proxy_speed_path", lambda: path)
    PROXY_SPEED.reset()
    yield path
    PROXY_SPEED.reset()


@pytest.fixture(autouse=True)
def _isolate_provider_registry(monkeypatch, tmp_path):
    """Keep custom provider registry state out of the real ~/.fcc directory."""
    from my_claude_code.config import provider_registry
    from my_claude_code.providers.runtime import models_dev

    config_dir = tmp_path / "fcc-config"
    monkeypatch.setattr(provider_registry, "config_dir_path", lambda: config_dir)
    monkeypatch.setattr(models_dev, "config_dir_path", lambda: config_dir)
    provider_registry.reset_provider_registry()
    yield
    provider_registry.reset_provider_registry()


@pytest.fixture
def provider_config():
    from my_claude_code.providers.base import ProviderConfig

    return ProviderConfig(
        api_key="test_key",
        base_url="https://test.api.nvidia.com/v1",
        rate_limit=10,
        rate_window=60,
    )


@pytest.fixture
def nim_provider(provider_config):
    from my_claude_code.config.nim import NimSettings
    from my_claude_code.providers.nvidia_nim import NvidiaNimProvider

    return NvidiaNimProvider(
        provider_config,
        nim_settings=NimSettings(),
        rate_limiter=passthrough_rate_limiter(),
    )


@pytest.fixture
def open_router_provider(provider_config):
    from my_claude_code.providers.open_router import OpenRouterProvider

    return OpenRouterProvider(provider_config, rate_limiter=passthrough_rate_limiter())


@pytest.fixture
def lmstudio_provider(provider_config):
    from my_claude_code.providers.base import ProviderConfig
    from my_claude_code.providers.lmstudio import LMStudioProvider

    lmstudio_config = ProviderConfig(
        api_key="lm-studio",
        base_url="http://localhost:1234/v1",
        rate_limit=provider_config.rate_limit,
        rate_window=provider_config.rate_window,
    )
    return LMStudioProvider(lmstudio_config, rate_limiter=passthrough_rate_limiter())


@pytest.fixture
def llamacpp_provider(provider_config):
    from my_claude_code.providers.base import ProviderConfig
    from my_claude_code.providers.openai_chat import create_openai_chat_provider

    llamacpp_config = ProviderConfig(
        api_key="llamacpp",
        base_url="http://localhost:8080/v1",
        rate_limit=10,
        rate_window=60,
    )
    return create_openai_chat_provider(
        "llamacpp",
        llamacpp_config,
        passthrough_rate_limiter(),
    )


@pytest.fixture
def mock_cli_session():
    from my_claude_code.messaging.managed_protocols import (
        ManagedClaudeSessionProtocol,
    )

    session = MagicMock(spec=ManagedClaudeSessionProtocol)
    session.start_task = MagicMock()  # This will return an async generator
    session.is_busy = False
    return session


@pytest.fixture
def mock_cli_manager():
    from my_claude_code.messaging.managed_protocols import (
        ManagedClaudeSessionManagerProtocol,
    )

    manager = MagicMock(spec=ManagedClaudeSessionManagerProtocol)
    manager.get_or_create_session = AsyncMock()
    manager.register_real_session_id = AsyncMock(return_value=True)
    manager.stop_all = AsyncMock()
    manager.remove_session = AsyncMock(return_value=True)
    manager.get_stats = MagicMock(return_value={"active_sessions": 0})
    return manager


@pytest.fixture
def mock_platform():
    from my_claude_code.messaging.platforms.ports import OutboundMessenger

    platform = MagicMock(spec=OutboundMessenger)
    platform.send_message = AsyncMock(return_value="msg_123")
    platform.edit_message = AsyncMock()
    platform.delete_message = AsyncMock()
    platform.queue_send_message = AsyncMock(return_value="msg_123")
    platform.queue_edit_message = AsyncMock()
    platform.queue_delete_messages = AsyncMock()
    platform.cancel_pending_voice = AsyncMock(return_value=None)
    platform.cancel_all_pending_voices = AsyncMock(return_value=())
    platform.cancel_pending_voices_in_scope = AsyncMock(return_value=())

    def _fire_and_forget(task):
        if asyncio.iscoroutine(task):
            # Create a task to avoid "coroutine was never awaited" warning
            return asyncio.create_task(task)
        return None

    platform.fire_and_forget = MagicMock(side_effect=_fire_and_forget)
    return platform


@pytest.fixture
def mock_session_store():
    from my_claude_code.messaging.session import SessionStore

    store = MagicMock(spec=SessionStore)
    store.save_tree = MagicMock()
    store.get_tree = MagicMock(return_value=None)
    store.register_node = MagicMock()
    store.record_message_id = MagicMock()
    store.get_tracked_message_ids_for_chat = MagicMock(return_value=[])
    store.forget_tracked_message_ids = MagicMock()
    store.clear_scope = MagicMock()
    return store


@pytest.fixture
def incoming_message_factory():
    _valid_keys = frozenset(
        {
            "text",
            "chat_id",
            "user_id",
            "message_id",
            "platform",
            "reply_to_message_id",
            "message_thread_id",
            "username",
            "timestamp",
            "raw_event",
            "status_message_id",
        }
    )

    def _create(**kwargs):
        from my_claude_code.messaging.models import IncomingMessage

        defaults: dict[str, Any] = {
            "text": "hello",
            "chat_id": "chat_1",
            "user_id": "user_1",
            "message_id": "msg_1",
            "platform": "telegram",
        }
        defaults.update(kwargs)
        if "timestamp" in defaults and isinstance(defaults["timestamp"], str):
            from datetime import datetime

            defaults["timestamp"] = datetime.fromisoformat(defaults["timestamp"])
        filtered = {k: v for k, v in defaults.items() if k in _valid_keys}
        return IncomingMessage(**filtered)

    return _create


#: ``logging_config``'s module state, which ``configure_logging`` keeps between
#: calls to decide whether it is configuring the process or adjusting it.
_LOGGING_CONFIG_GLOBALS = (
    "_configured",
    "_current_path",
    "_current_level",
    "_current_verbose",
    "_sink_id",
    "_active_sink",
)


class _ProcessLogging:
    """What ``configure_logging`` takes over, as it was before a test ran.

    ``configure_logging`` is the server's composition-root call. It owns the
    process's logging from then on: every loguru sink removed, an
    ``InterceptHandler`` as the root logger's only handler, the root logger at
    DEBUG, the noisy third-party loggers re-levelled, and a file sink with its
    own writer thread. Nothing undoes that, because a server never needs it
    undone -- so a test that runs it (``tests/core/test_trace.py``, the
    logging-config tests, and ``build_asgi_app`` in
    ``tests/api/test_app_lifespan_and_errors.py``) handed all of it to every
    later test in the same xdist worker, which is to say to most of the
    suite: on CI those tests ran in the job's first two minutes, on every
    worker.

    That inherited state is half of the deadlock that hung the pytest job at
    99% (see ``_propagate_loguru_to_caplog``), and it also sent every
    library's DEBUG record through loguru into a file in a finished test's
    directory for the rest of the run. Noted before the test's first phase --
    see ``pytest_runtest_protocol`` below -- so the snapshot holds the
    session's own handlers and none of pytest's per-phase capture handlers;
    put back by ``_restore_process_logging``.
    """

    def __init__(self) -> None:
        root = logging.getLogger()
        self.root_handlers = list(root.handlers)
        self.root_level = root.level
        self.third_party_levels = {
            name: logging.getLogger(name).level
            for name in logging_config._THIRD_PARTY_LOGGERS
        }
        self.module_state = {
            name: getattr(logging_config, name) for name in _LOGGING_CONFIG_GLOBALS
        }

    def restore(self) -> None:
        sink_id = logging_config._sink_id
        if sink_id is not None and sink_id != self.module_state["_sink_id"]:
            # Stops the sink's writer thread and closes its file. Loguru ids
            # are never reused, so a different id is a sink this test added.
            with contextlib.suppress(ValueError):  # the test removed it itself
                loguru_logger.remove(sink_id)
        root = logging.getLogger()
        # pytest's capture handlers for the phase now running (teardown) stay:
        # pytest takes them off again on its way out of the phase.
        in_progress = [
            handler
            for handler in root.handlers
            if handler not in self.root_handlers
            and not isinstance(handler, InterceptHandler)
        ]
        wanted = [*self.root_handlers, *in_progress]
        if root.handlers != wanted:
            root.handlers = wanted
        if root.level != self.root_level:
            root.setLevel(self.root_level)
        for name, level in self.third_party_levels.items():
            if logging.getLogger(name).level != level:
                logging.getLogger(name).setLevel(level)
        for name, value in self.module_state.items():
            setattr(logging_config, name, value)


_LOGGING_AT_START = pytest.StashKey[_ProcessLogging]()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item: pytest.Item) -> Generator[None, object, object]:
    """Note the process's logging before any phase of the test touches it."""
    item.stash[_LOGGING_AT_START] = _ProcessLogging()
    return (yield)


@pytest.fixture(autouse=True)
def _restore_process_logging(request: pytest.FixtureRequest):
    """No test may inherit another test's ``configure_logging``.

    Restored in teardown rather than after the protocol so that it runs inside
    the window ``faulthandler_timeout`` watches: removing a file sink joins its
    writer thread. Defined immediately before ``_propagate_loguru_to_caplog``
    so it is torn down immediately after it.
    """

    yield
    started_with = request.node.stash.get(_LOGGING_AT_START, None)
    if started_with is not None:
        started_with.restore()


def _hand_to_stdlib_handlers(
    py_logger: logging.Logger, record: logging.LogRecord
) -> None:
    """``Logger.handle`` for a record that came from loguru, minus loguru.

    The one handler skipped is ``InterceptHandler``: it would hand the record
    straight back to loguru, and it does so holding its own stdlib lock -- the
    opposite order to this bridge, which runs inside a loguru sink while loguru
    holds that sink's lock. See ``_propagate_loguru_to_caplog``.
    """

    if py_logger.disabled:
        return
    filtered = py_logger.filter(record)
    if not filtered:
        return
    if isinstance(filtered, logging.LogRecord):
        record = filtered
    node: logging.Logger | None = py_logger
    while node is not None:
        for handler in node.handlers:
            if isinstance(handler, InterceptHandler):
                continue
            if record.levelno >= handler.level:
                handler.handle(record)
        node = node.parent if node.propagate else None


@pytest.fixture(autouse=True)
def _propagate_loguru_to_caplog():
    """Route loguru logs to stdlib logging so pytest caplog captures them.

    The bridge must never write back into loguru. It runs inside a loguru sink,
    holding that sink's lock, and ``InterceptHandler`` -- which
    ``configure_logging`` makes the root logger's handler -- takes its own lock
    and then calls loguru, which needs the bridge's lock. With a thread logging
    through loguru and another through stdlib at the same moment, each held
    the lock the other wanted, forever:

    * thread 1: ``logger.debug`` -> this bridge (holds the bridge sink's
      lock) -> ``logging.Handler.handle`` waits for InterceptHandler's lock;
    * thread 2: Pillow's ``logging.debug`` -> InterceptHandler (holds its
      lock) -> ``logger.log`` waits for the bridge sink's lock.

    That is the pytest job that stopped at 99% and was cancelled by its
    15-minute limit eleven times between 2026-10-01 and 2026-10-02, each time
    inside ``test_threads_sharing_the_memo_get_the_uncached_bytes``. A record
    that came from loguru has no business going back into loguru, so the
    bridge delivers it to every stdlib handler except that one -- which also
    ends the "Logging error in Loguru Handler" traceback loguru printed for
    every record while the bridge re-entered itself.
    """

    class _PropagateHandler:
        def write(self, message):
            record = message.record
            level = min(record["level"].no, logging.CRITICAL)
            py_logger = logging.getLogger(record["name"])
            if not py_logger.isEnabledFor(level):
                return
            _hand_to_stdlib_handlers(
                py_logger,
                py_logger.makeRecord(
                    py_logger.name,
                    level,
                    record["file"].path,
                    record["line"],
                    record["message"],
                    (),
                    None,
                    record["function"],
                ),
            )

    handler_id = loguru_logger.add(_PropagateHandler(), format="{message}")
    yield
    with contextlib.suppress(ValueError):
        loguru_logger.remove(
            handler_id
        )  # Handler already removed (e.g. by configure_logging)


@pytest.fixture(scope="session", autouse=True)
def _no_fcc_threads_leak_at_session_end():
    """Fail loudly if any FCC-owned background thread survives the whole run.

    The request-log and web-search stores each start a daemon writer thread.
    Daemon threads cannot prevent interpreter exit, but a store that is never
    closed keeps its queue, sqlite connection and any held records alive for the
    rest of the process -- which under xdist accumulates per-worker and reads as
    a memory leak with "processes that won't die". Every test is responsible
    for closing what it constructs (the autouse request-log fixture already
    resets the shared registry); this guard makes forgetting an error at the
    session boundary instead of a 64 GB surprise hours into a run.
    """
    from my_claude_code.core import request_log
    from my_claude_code.websearch import analytics

    request_log.reset_request_log_stores()
    analytics.reset_analytics_state()
    yield
    request_log.reset_request_log_stores()
    analytics.reset_analytics_state()

    fcc_writer_threads = [
        thread.name
        for thread in threading.enumerate()
        if thread is not threading.current_thread()
        and (
            thread.name.startswith("fcc-request-log-writer")
            or thread.name.startswith("websearch-log-writer")
            or thread.name.startswith("chatgpt-oauth-callback")
            or thread.name.startswith("fcc-open-admin-browser")
        )
    ]
    assert not fcc_writer_threads, (
        "FCC background threads leaked past the session: "
        f"{fcc_writer_threads}. Close every store/thread a test creates."
    )
