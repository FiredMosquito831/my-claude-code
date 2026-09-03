import asyncio
import contextlib
import logging
import os
import threading
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from my_claude_code.config.settings import Settings
from tests.providers.support import passthrough_rate_limiter

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
def _isolate_request_log(monkeypatch, tmp_path):
    """Keep request-log writes out of the real ~/.fcc directory during tests."""
    from my_claude_code.core import request_log

    monkeypatch.setattr(
        request_log,
        "default_request_log_path",
        lambda: tmp_path / "requests.db",
    )
    yield
    request_log.reset_request_log_stores()


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
