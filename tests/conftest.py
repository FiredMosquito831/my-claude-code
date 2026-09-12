import asyncio
import contextlib
import logging
import os
import threading
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

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
    another test's copy of it.
    """
    from my_claude_code.application import derived_payloads

    with derived_payloads._refresh_lock:
        derived_payloads._refreshing.clear()
    yield
    with derived_payloads._refresh_lock:
        derived_payloads._refreshing.clear()


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


@pytest.fixture(autouse=True)
def _propagate_loguru_to_caplog():
    """Route loguru logs to stdlib logging so pytest caplog captures them."""
    from loguru import logger as loguru_logger

    class _PropagateHandler:
        def write(self, message):
            record = message.record
            level = record["level"].no
            stdlib_level = min(level, logging.CRITICAL)
            py_logger = logging.getLogger(record["name"])
            py_logger.log(stdlib_level, record["message"])

    handler_id = loguru_logger.add(_PropagateHandler(), format="{message}")
    yield
    with contextlib.suppress(ValueError):
        loguru_logger.remove(
            handler_id
        )  # Handler already removed (e.g. by test_logging_config)


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
