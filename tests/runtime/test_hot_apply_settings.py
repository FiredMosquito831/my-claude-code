"""L9 (7.48.0): a saved field applies without a restart unless it physically needs one.

Until 7.48.0, 130 of the dashboard's fields were marked ``restart_required``,
and saving any one of them rebuilt the whole server in place -- which is what
changing a number on Limits & Resilience did for no reason the operator could
see. Only the bind (``HOST``/``PORT``), the auth token, the log file sink and
the four fields the supervisor reads once at start still need one; six more
need one only while a Telegram or Discord bot is running.

Every other field is proved here the same way, one field at a time: the value
goes through the real apply route -- ``POST /admin/api/config/apply`` into
``prepare_admin_update`` and ``_commit_admin_update`` -- and the code that
reads it is then observed using the new value, while the restart callback is
never called. The observation is made where the value is consumed:

* **rebuild** fields on the provider the next request resolves from the new
  generation (its ``ProviderConfig``, its credential pool, its proxy legs);
* **per-request** fields on what a request builds from its lease (the
  execution policy, the describe adapter, the request-log capture) or reads
  per use (the OpenCode identity, the health re-prober's switch);
* **re-armed** fields on the long-lived object ``start`` built once (the
  loop-lag monitor, the watchdog, the three settings-driven loops, the proxy
  engine's ladder, the two log stores, the third-party loggers).

Nothing here makes a network call: providers are constructed, never asked.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Callable
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio

import my_claude_code.api.request_capture as request_capture_module
import my_claude_code.config.logging_config as logging_config
import my_claude_code.config.settings as settings_module
import my_claude_code.runtime.application as application_module
from my_claude_code.api.handlers.messages import MessagesHandler
from my_claude_code.api.request_capture import build_capture
from my_claude_code.application.execution import route_execution_policy
from my_claude_code.config.admin.manifest import FIELD_BY_KEY
from my_claude_code.config.env_files import LazyEnvFiles
from my_claude_code.config.paths import managed_env_path
from my_claude_code.config.provider_catalog import PROVIDER_CATALOG
from my_claude_code.config.settings import (
    Settings,
    configured_opencode_client_ai_sdk_version,
    configured_opencode_client_identity,
    configured_opencode_client_runtime,
    configured_opencode_client_version,
    configured_opencode_free_tier_models,
)
from my_claude_code.core import request_tasks
from my_claude_code.core.anthropic import Message, MessagesRequest
from my_claude_code.core.loop_health import loop_health
from my_claude_code.core.proxy_rotation import (
    PROXY_REACHABILITY,
    PROXY_TUNING,
    configure_proxy_rotation,
)
from my_claude_code.core.request_log import (
    reset_request_log_stores,
    store_from_settings,
)
from my_claude_code.providers.runtime.config import (
    build_provider_config,
    credential_rotation_policy,
)
from my_claude_code.runtime.application import ApplicationRuntime
from my_claude_code.websearch import analytics as websearch_analytics
from tests.api.support import create_test_app, runtime_for_app

MODEL = "nvidia_nim/test-model"
POOL = "nvapi-hot-key-one-aaaa,nvapi-hot-key-two-bbbb"

# The fields this release made hot, grouped by how they reach their reader.
# The sets are asserted against the manifest below, so a field cannot be
# flipped without a proof here, and a proof cannot outlive its field.
ROTATION_KEYS = frozenset(
    f"{descriptor.credential_env}_ROTATION"
    for descriptor in PROVIDER_CATALOG.values()
    if descriptor.credential_env is not None
)

#: key -> (saved value, ProviderConfig attribute, value the provider holds)
PROVIDER_CONFIG_FIELDS: dict[str, tuple[str, str, object]] = {
    "PROVIDER_RETRY_ATTEMPTS": ("4", "retry_attempts", 4),
    "STREAM_EARLY_RETRY_ATTEMPTS": ("7", "early_retry_attempts", 7),
    "STREAM_MIDSTREAM_RECOVERY_ATTEMPTS": ("3", "midstream_recovery_attempts", 3),
    "PROVIDER_RETRY_BACKOFF_BASE_SECONDS": ("3", "retry_backoff_base_seconds", 3.0),
    "PROVIDER_RETRY_BACKOFF_MAX_SECONDS": ("9", "retry_backoff_max_seconds", 9.0),
    "PROVIDER_RETRY_BACKOFF_JITTER_SECONDS": (
        "1.5",
        "retry_backoff_jitter_seconds",
        1.5,
    ),
    "STREAM_COMMIT_HOLDBACK_SECONDS": ("2", "commit_holdback_seconds", 2.0),
    "STREAM_COMMIT_HOLDBACK_CHARS": ("64", "commit_holdback_chars", 64),
    "FALLBACK_ON_REASONING_ONLY": ("false", "fallback_on_reasoning_only", False),
    "RATE_LIMIT_COOLDOWN_SECONDS": ("90", "rate_limit_cooldown_seconds", 90.0),
    "RATE_LIMIT_COOLDOWN_MAX_SECONDS": (
        "1800",
        "rate_limit_cooldown_max_seconds",
        1800.0,
    ),
    "RATE_LIMIT_COOLDOWN_MODE": ("fixed", "rate_limit_cooldown_mode", "fixed"),
    "CREDENTIAL_LOCKOUT_TIERS": ("60,600", "lockout_tiers", (60.0, 600.0)),
    "CREDENTIAL_MODEL_BENCH_ESCALATION": (
        "4",
        "credential_model_bench_escalation",
        4,
    ),
    "RATE_LIMIT_ROUTES_AROUND_MODEL": ("false", "routes_around_model", False),
    "LOG_API_ERROR_TRACEBACKS": ("true", "log_api_error_tracebacks", True),
}

#: The four read where a proxied pool is built. key -> (value, reader)
PROXY_POOL_FIELDS: dict[str, tuple[str, Callable[[Any], object], object]] = {
    "PROXY_MAX_SWITCHES_PER_REQUEST": ("3", lambda leg: leg._max_switches, 3),
    "PROXY_MAX_OPEN_LEGS": ("7", lambda leg: leg._pool._max_open, 7),
    "PROXY_MAX_LIVE_FAILURES": ("9", lambda leg: leg._max_live_failures, 9),
    "PROXY_CONNECT_TIMEOUT_SECONDS": (
        "4",
        lambda leg: leg._pool._build(0)._config.http_connect_timeout,
        4.0,
    ),
}

#: Read off the generation's settings by the factory's own special builder.
FACTORY_SETTINGS_FIELDS: dict[str, tuple[str, str, object]] = {
    "ANTHROPIC_OAUTH_REQUIRE_CLAUDE_CODE": (
        "false",
        "anthropic_oauth_require_claude_code",
        False,
    ),
}

#: The executor's policy, built per request from the lease.
EXECUTION_POLICY_FIELDS: dict[str, tuple[str, str, object]] = {
    "FALLBACK_REASONING_ANSWER_TIMEOUT": ("240", "reasoning_answer_timeout", 240.0),
    "FALLBACK_END_CLEANLY_AFTER_COMMIT": ("false", "end_cleanly_after_commit", False),
    "FALLBACK_RESUME_AFTER_COMMIT": ("false", "resume_after_commit", False),
    "FALLBACK_COOLDOWN_STEP_OVER_FLOOR": ("11", "cooldown_step_over_floor", 11.0),
}

#: Read per request through ``get_settings``.
OPENCODE_FIELDS: dict[str, tuple[str, Callable[[], object], object]] = {
    "OPENCODE_CLIENT_IDENTITY": ("mcc", configured_opencode_client_identity, "mcc"),
    "OPENCODE_CLIENT_VERSION": (
        "1.99.1",
        configured_opencode_client_version,
        "1.99.1",
    ),
    "OPENCODE_CLIENT_AI_SDK_VERSION": (
        "9.9.9",
        configured_opencode_client_ai_sdk_version,
        "9.9.9",
    ),
    "OPENCODE_CLIENT_RUNTIME": (
        "bun/9.9.9",
        configured_opencode_client_runtime,
        "bun/9.9.9",
    ),
    "OPENCODE_FREE_TIER_MODELS": (
        "big-pickle,hot-free",
        configured_opencode_free_tier_models,
        ("big-pickle", "hot-free"),
    ),
}

#: ``build_capture`` hands these to the request's capture, per request.
CAPTURE_FIELDS: dict[str, tuple[str, str, object]] = {
    "REQUEST_LOG_CAPTURE_BODIES": ("false", "capture_bodies", False),
    "REQUEST_LOG_CAPTURE_FOLDER": ("false", "capture_folder", False),
    "REQUEST_LOG_CAPTURE_SESSION": ("false", "capture_session", False),
    "REQUEST_LOG_CAPTURE_IMAGES": ("false", "capture_images_pixels", 0),
    "REQUEST_LOG_IMAGE_MAX_PIXELS": ("256", "capture_images_pixels", 256),
    "REQUEST_LOG_WIRE_BODY_MAX_CHARS": ("4000", "wire_body_max_chars", 4000),
    "REQUEST_LOG_LADDER_BODY_MAX_CHARS": ("400", "ladder_body_max_chars", 400),
}

#: The shared request-log store, retuned in place. key -> (value, attr, held)
REQUEST_LOG_STORE_FIELDS: dict[str, tuple[str, str, object]] = {
    "REQUEST_LOG_MAX_ROWS": ("123456", "_max_rows", 123456),
    "REQUEST_LOG_TEXT_MAX_CHARS": ("5000", "_text_max_chars", 5000),
    "REQUEST_LOG_COMPRESSION_LEVEL": ("3", "_compression_level", 3),
    "REQUEST_LOG_QUEUE_MAX_SIZE": ("250", "_queue_max_size", 250),
    "REQUEST_LOG_COMPRESS_BODIES": ("false", "_compress_bodies", False),
}

WEBSEARCH_STORE_FIELDS: dict[str, tuple[str, str, object]] = {
    "WEBSEARCH_LOG_CAPTURE_CONTENT": ("false", "_capture_content", False),
    "WEBSEARCH_LOG_CONTENT_MAX_CHARS": ("4096", "_max_content_chars", 4096),
    "WEBSEARCH_LOG_MAX_ROWS": ("777", "_max_rows", 777),
}

#: Re-armed objects ``start`` built once; proved individually below.
REARMED_KEYS = frozenset(
    {
        "MODEL_DISCOVERY_REFRESH_SECONDS",
        "PROXY_CHECK_ENABLED",
        "PROXY_CHECK_INTERVAL_MINUTES",
        "PROXY_FEED_REFRESH_ENABLED",
        "PROXY_FEED_REFRESH_MINUTES",
        "HEALTH_HEARTBEAT_INTERVAL_MS",
        "HEALTH_BUSY_LAG_MS",
        "REQUEST_WATCHDOG_ENABLED",
        "REQUEST_WATCHDOG_INTERVAL_SECONDS",
        "REQUEST_INFLIGHT_ENABLED",
        "PROXY_COOLDOWN_SECONDS",
        "PROXY_COOLDOWN_MAX_SECONDS",
        "PROXY_REACHABILITY_TIERS",
        "LOG_RAW_API_PAYLOADS",
    }
)

#: Proved one by one in their own tests.
SINGLE_KEYS = frozenset(
    {
        "OPENAI_PROXY",
        "DESCRIBE_CONCURRENCY",
        "MODEL_PROBE_NEW_MODELS",
        "PROXY_CHAIN_MAX_ENTRIES",
        "PROXY_HEALTH_REPROBE_ENABLED",
        "REQUEST_LOG_ENABLED",
    }
)

#: Still restart-required, and why (the reason is in each field's help).
PROCESS_RESTART_KEYS = frozenset(
    {
        # the bind
        "HOST",
        "PORT",
        # handed to launched agents and the bot at start
        "ANTHROPIC_AUTH_TOKEN",
        # the server log's file sink
        "LOG_LEVEL",
        "SERVER_LOG_RETAIN_FILES",
        # read once by the supervisor in cli/commands.py (frozen for L9)
        "SERVER_GRACEFUL_SHUTDOWN_SECONDS",
        "SERVER_PORT_TAKEOVER",
        "SERVER_STALE_SERVER_ACTION",
        "SERVER_STALE_SESSION_SECONDS",
    }
)
MESSAGING_RESTART_KEYS = frozenset(
    {
        "DEBUG_PLATFORM_EDITS",
        "DEBUG_SUBAGENT_STACK",
        "LOG_RAW_MESSAGING_CONTENT",
        "LOG_MESSAGING_ERROR_DETAILS",
        "LOG_RAW_CLI_DIAGNOSTICS",
        "LOG_API_ERROR_TRACEBACKS",
    }
)

HOT_KEYS = (
    ROTATION_KEYS
    | frozenset(PROVIDER_CONFIG_FIELDS)
    | frozenset(PROXY_POOL_FIELDS)
    | frozenset(FACTORY_SETTINGS_FIELDS)
    | frozenset(EXECUTION_POLICY_FIELDS)
    | frozenset(OPENCODE_FIELDS)
    | frozenset(CAPTURE_FIELDS)
    | frozenset(REQUEST_LOG_STORE_FIELDS)
    | frozenset(WEBSEARCH_STORE_FIELDS)
    | REARMED_KEYS
    | SINGLE_KEYS
) - MESSAGING_RESTART_KEYS


# --------------------------------------------------------------------- setup


@pytest.fixture
def managed_env(monkeypatch) -> Any:
    """A managed ``.env`` the settings layer really reads, as it does in production.

    The suite switches dotenv reading off for every test; production reads the
    managed file, and two of the readers proved here (the credential rotation
    policy and ``get_settings``) read it rather than the snapshot. Putting the
    file back is what makes the proof about the path the server runs.
    """

    path = managed_env_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"MODEL={MODEL}\nNVIDIA_NIM_API_KEY={POOL}\n", encoding="utf-8")
    # Process env outranks the file and locks a field against the page; the
    # suite (and the isolated runner) export some of these.
    for key in (
        "MODEL",
        "NVIDIA_NIM_API_KEY",
        *HOT_KEYS,
        *PROCESS_RESTART_KEYS,
        *MESSAGING_RESTART_KEYS,
    ):
        monkeypatch.delenv(key, raising=False)
    # Production's own value: resolved at every read, so ``Settings()`` reads
    # the file the apply wrote even if the config directory is resolved again
    # in between (a fixed tuple here made the proof order-dependent on CI).
    #
    # ``tests/config/test_env_aliases.py`` reloads ``config.settings``, which
    # redefines ``Settings`` and ``get_settings`` in the module while
    # ``runtime.application`` keeps the function it imported. In production
    # they are one function, so the apply's ``cache_clear`` is the one the
    # per-request readers consult; the runtime is pointed back at the module's
    # current function here so a worker that ran that test first proves the
    # same thing.
    for cls in {Settings, settings_module.Settings}:
        monkeypatch.setattr(
            cls, "model_config", {**cls.model_config, "env_file": LazyEnvFiles()}
        )
    monkeypatch.setattr(
        application_module, "get_settings", settings_module.get_settings
    )
    settings_module.get_settings.cache_clear()
    yield path
    settings_module.get_settings.cache_clear()


class HotApp:
    """An app, a loop-local client, and the runtime behind them."""

    def __init__(self, app: Any, client: httpx.AsyncClient, restart: AsyncMock):
        self.app = app
        self.client = client
        self.restart = restart
        self.runtime: ApplicationRuntime = runtime_for_app(app)
        self.manager = self.runtime.provider_manager

    async def apply(self, values: dict[str, str]) -> dict[str, Any]:
        response = await self.client.post(
            "/admin/api/config/apply", json={"values": values}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["applied"] is True, body
        return body

    async def apply_hot(self, values: dict[str, str]) -> dict[str, Any]:
        """Apply, and insist it needed no restart of any kind."""

        before = self.manager.current_generation_id
        body = await self.apply(values)
        assert body["pending_fields"] == [], body
        assert body["restart"]["required"] is False, body
        assert body["restart"]["fields"] == []
        # Published: the next request leases the generation built from it.
        assert self.manager.current_generation_id == before + 1
        self.restart.assert_not_awaited()
        return body

    @contextlib.asynccontextmanager
    async def leased(self) -> AsyncIterator[Any]:
        """A request's lease on the current generation, always given back."""

        lease = await self.manager.acquire()
        try:
            yield lease
        finally:
            await lease.release()


@pytest_asyncio.fixture
async def hot(managed_env) -> AsyncIterator[HotApp]:
    restart = AsyncMock()
    app = create_test_app(Settings(), restart_callback=restart)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://mcc") as client:
        yield HotApp(app, client, restart)
    # Bounded: a lease a failed assertion left behind must fail the test, not
    # hang the worker waiting for a drain that will never come.
    await asyncio.wait_for(runtime_for_app(app).provider_manager.close(), 20)
    reset_request_log_stores()
    websearch_analytics.reset_analytics_state()


# ---------------------------------------------------------------- the ledger


def test_the_restart_list_is_exactly_the_fields_that_physically_need_one() -> None:
    """The manifest's restart set is the honest list, and nothing else."""

    restart = {key for key, field in FIELD_BY_KEY.items() if field.restart_required}
    assert restart == PROCESS_RESTART_KEYS | MESSAGING_RESTART_KEYS
    for key in MESSAGING_RESTART_KEYS:
        assert FIELD_BY_KEY[key].restart_scope == "messaging", key
    for key in PROCESS_RESTART_KEYS:
        assert FIELD_BY_KEY[key].restart_scope == "process", key
        # The reason is in the field's own help, where the operator reads it.
        assert "Requires a restart:" in FIELD_BY_KEY[key].description, key


def test_every_hot_field_has_a_proof_here() -> None:
    """116 fields made hot; each is in exactly one proof table above."""

    for key in HOT_KEYS:
        assert key in FIELD_BY_KEY, key
        assert FIELD_BY_KEY[key].restart_required is False, key
    assert len(HOT_KEYS) == 116
    assert len(ROTATION_KEYS) == 52
    for key in HOT_KEYS:
        assert "Requires restart" not in FIELD_BY_KEY[key].description, key


# ------------------------------------------------------- rebuilt on the save


@pytest.mark.asyncio
@pytest.mark.parametrize("key", sorted(PROVIDER_CONFIG_FIELDS))
async def test_a_pool_tuning_reaches_the_next_requests_provider(hot, key) -> None:
    value, attr, expected = PROVIDER_CONFIG_FIELDS[key]
    async with hot.leased() as in_flight:
        held = getattr(in_flight.resolve_provider("nvidia_nim")._config, attr)
        assert held != expected, "pick a value the provider does not already hold"

        await hot.apply_hot({key: value})

        async with hot.leased() as fresh:
            provider = fresh.resolve_provider("nvidia_nim")
            assert getattr(provider._config, attr) == expected
        # The request that was already running keeps what it started with.
        assert getattr(in_flight.resolve_provider("nvidia_nim")._config, attr) == held


@pytest.mark.asyncio
@pytest.mark.parametrize("key", sorted(ROTATION_KEYS))
async def test_a_rotation_policy_reaches_the_pool_builder(hot, key) -> None:
    """Every provider's policy, read from the managed file at pool build."""

    descriptor = next(
        d for d in PROVIDER_CATALOG.values() if f"{d.credential_env}_ROTATION" == key
    )
    # ``build_provider_config`` resolves the policy through exactly this call,
    # with the generation's settings, when the pool is built.
    async with hot.leased() as lease:
        assert credential_rotation_policy(descriptor, lease.settings) == "single"

    await hot.apply_hot({key: "least_used"})

    async with hot.leased() as fresh:
        assert credential_rotation_policy(descriptor, fresh.settings) == "least_used"


@pytest.mark.asyncio
async def test_a_rotation_policy_changes_the_live_pool_itself(hot) -> None:
    """End to end on one provider: the resolved pool rotates the new way."""

    async with hot.leased() as lease:
        assert lease.resolve_provider("nvidia_nim")._state.policy == "single"

    await hot.apply_hot({"NVIDIA_NIM_API_KEY_ROTATION": "round_robin"})

    async with hot.leased() as fresh:
        pool = fresh.resolve_provider("nvidia_nim")
        assert pool._state.policy == "round_robin"
        assert len(pool._providers) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("key", sorted(PROXY_POOL_FIELDS))
async def test_a_proxy_pool_limit_reaches_the_next_proxied_leg(hot, key) -> None:
    response = await hot.client.put(
        "/admin/api/proxy-chains",
        json={
            "provider": "nvidia_nim",
            "enabled": True,
            "policy": "failover",
            "scope": "provider",
            "max_switches": 5,
            "on": ["quota"],
            "entries": [
                {"url": "http://198.51.100.7:8080"},
                {"url": "http://198.51.100.8:8080"},
            ],
        },
    )
    assert response.status_code == 200, response.text
    value, read, expected = PROXY_POOL_FIELDS[key]

    await hot.apply_hot({key: value})

    async with hot.leased() as fresh:
        pool = fresh.resolve_provider("nvidia_nim")
        # Below the credential pool, one proxied fan-out per key.
        leg = pool._providers[0]
        assert type(leg).__name__ == "ProxyRotatingProvider"
        assert read(leg) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("key", sorted(FACTORY_SETTINGS_FIELDS))
async def test_a_factory_setting_is_on_the_snapshot_the_factory_builds_from(
    hot, key
) -> None:
    value, attr, expected = FACTORY_SETTINGS_FIELDS[key]
    await hot.apply_hot({key: value})
    async with hot.leased() as fresh:
        # ``create_provider(provider_id, generation.settings)`` -- this object.
        assert getattr(fresh.settings, attr) == expected


@pytest.mark.asyncio
async def test_the_chatgpt_proxy_reaches_its_provider_config(hot) -> None:
    descriptor = PROVIDER_CATALOG["openai"]
    await hot.apply_hot({"CHATGPT_OAUTH_ACCESS_TOKEN": "l9-chatgpt-token"})
    await hot.apply_hot({"OPENAI_PROXY": "http://198.51.100.9:3128"})
    async with hot.leased() as fresh:
        config = build_provider_config(descriptor, fresh.settings)
        assert config.proxy == "http://198.51.100.9:3128"


# ------------------------------------------------------------- per request


@pytest.mark.asyncio
@pytest.mark.parametrize("key", sorted(EXECUTION_POLICY_FIELDS))
async def test_an_execution_knob_reaches_the_next_requests_policy(hot, key) -> None:
    value, attr, expected = EXECUTION_POLICY_FIELDS[key]
    await hot.apply_hot({key: value})
    async with hot.leased() as fresh:
        # What every handler builds its executor from, per request.
        assert getattr(route_execution_policy(fresh.settings), attr) == expected
        handler = MessagesHandler(
            fresh.settings, provider_resolver=fresh.resolve_provider
        )
        assert getattr(handler._provider_executor._policy, attr) == expected


@pytest.mark.asyncio
async def test_describe_concurrency_reaches_the_next_requests_adapter(hot) -> None:
    await hot.apply_hot({"DESCRIBE_CONCURRENCY": "9"})
    async with hot.leased() as fresh:
        handler = MessagesHandler(
            fresh.settings, provider_resolver=fresh.resolve_provider
        )
        assert handler._describe_adapter._concurrency == 9


@pytest.mark.asyncio
@pytest.mark.parametrize("key", sorted(OPENCODE_FIELDS))
async def test_an_opencode_identity_field_is_read_by_the_next_request(hot, key) -> None:
    value, read, expected = OPENCODE_FIELDS[key]
    assert read() != expected
    await hot.apply_hot({key: value})
    assert read() == expected


@pytest.mark.asyncio
async def test_the_probe_switch_is_read_off_the_live_settings(hot) -> None:
    assert hot.runtime.settings.model_probe_new_models is False
    await hot.apply_hot({"MODEL_PROBE_NEW_MODELS": "true"})
    assert hot.runtime.settings.model_probe_new_models is True


@pytest.mark.asyncio
async def test_the_chain_length_cap_refuses_on_the_next_save(hot) -> None:
    await hot.apply_hot({"PROXY_CHAIN_MAX_ENTRIES": "1"})
    response = await hot.client.put(
        "/admin/api/proxy-chains",
        json={
            "provider": "nvidia_nim",
            "enabled": True,
            "policy": "failover",
            "scope": "provider",
            "max_switches": 1,
            "on": ["quota"],
            "entries": [
                {"url": "http://198.51.100.7:8080"},
                {"url": "http://198.51.100.8:8080"},
            ],
        },
    )
    assert response.status_code >= 400, response.text


@pytest.mark.asyncio
async def test_the_reprobe_switch_is_read_by_the_next_tick(hot) -> None:
    timer = hot.runtime._proxy_health_timer
    assert timer._enabled() is True
    await hot.apply_hot({"PROXY_HEALTH_REPROBE_ENABLED": "false"})
    assert timer._enabled() is False


@pytest.mark.asyncio
async def test_turning_the_request_log_off_stops_the_next_capture(hot) -> None:
    async with hot.leased() as lease:
        assert store_from_settings(lease.settings) is not None
    await hot.apply_hot({"REQUEST_LOG_ENABLED": "false"})
    async with hot.leased() as fresh:
        assert store_from_settings(fresh.settings) is None


def _messages_request() -> MessagesRequest:
    return MessagesRequest(
        model="claude-sonnet-4",
        max_tokens=16,
        messages=[Message(role="user", content="hello")],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("key", sorted(CAPTURE_FIELDS))
async def test_a_capture_field_reaches_the_next_requests_capture(
    hot, key, monkeypatch
) -> None:
    value, kwarg, expected = CAPTURE_FIELDS[key]
    seen: list[dict[str, Any]] = []

    class RecordingCapture:
        def __init__(self, store: object, **kwargs: Any) -> None:
            seen.append(kwargs)

    monkeypatch.setattr(request_capture_module, "RequestCapture", RecordingCapture)
    await hot.apply_hot({key: value})
    async with hot.leased() as fresh:
        build_capture(
            fresh.settings,
            _messages_request(),
            request_id="req-hot",
            endpoint="/v1/messages",
            protocol="anthropic",
        )
    assert seen[-1][kwarg] == expected


# ------------------------------------------------------- re-armed in place


@pytest.mark.asyncio
@pytest.mark.parametrize("key", sorted(REQUEST_LOG_STORE_FIELDS))
async def test_a_request_log_tuning_moves_the_open_store(hot, key) -> None:
    async with hot.leased() as lease:
        store = store_from_settings(lease.settings)
    assert store is not None
    value, attr, expected = REQUEST_LOG_STORE_FIELDS[key]
    assert getattr(store, attr) != expected

    await hot.apply_hot({key: value})

    # The compression switch is adopted by the writer thread between two
    # batches rather than flipped under one; the writer polls every 0.25 s.
    deadline = time.monotonic() + 5.0
    while getattr(store, attr) != expected and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert getattr(store, attr) == expected
    async with hot.leased() as fresh:
        # The same store, not a second writer on the same file.
        assert store_from_settings(fresh.settings) is store
    if key == "REQUEST_LOG_QUEUE_MAX_SIZE":
        assert store._queue.maxsize == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("key", sorted(WEBSEARCH_STORE_FIELDS))
async def test_a_websearch_log_tuning_moves_the_open_store(hot, key) -> None:
    store = websearch_analytics.get_shared_store()
    value, attr, expected = WEBSEARCH_STORE_FIELDS[key]
    assert getattr(store, attr) != expected
    await hot.apply_hot({key: value})
    assert getattr(store, attr) == expected
    assert websearch_analytics.get_shared_store() is store


@pytest.mark.asyncio
async def test_the_websearch_log_switch_is_no_longer_cached_for_life(hot) -> None:
    """Already marked hot before 7.48.0, but cached at first use; now it moves."""

    assert websearch_analytics._log_enabled() is True
    await hot.apply_hot({"WEBSEARCH_LOG_ENABLED": "false"})
    assert websearch_analytics._log_enabled() is False


@pytest_asyncio.fixture
async def started(hot) -> Any:
    """The long-lived pieces ``start`` builds, built the way ``start`` builds them."""

    runtime = hot.runtime
    runtime._start_loop_heartbeat()
    runtime._start_stall_watchdog()
    runtime._discovery_timer.start()
    runtime._proxy_check_timer.start()
    runtime._proxy_feed_timer.start()
    runtime._started = True
    return hot


async def _close_started(runtime: ApplicationRuntime) -> None:
    runtime._started = False
    for timer in (
        runtime._discovery_timer,
        runtime._proxy_check_timer,
        runtime._proxy_feed_timer,
    ):
        await timer.close()
    if runtime._stall_watchdog is not None:
        await runtime._stall_watchdog.close()
        runtime._stall_watchdog = None
    if runtime._loop_heartbeat is not None:
        await runtime._loop_heartbeat.close()
        runtime._loop_heartbeat = None
    loop_health().reset()


@pytest.mark.asyncio
async def test_the_discovery_interval_is_re_armed_on_save(started) -> None:
    timer = started.runtime._discovery_timer
    try:
        assert timer.running
        assert timer.next_refresh_at > time.time() + 3000  # the 3600 s default
        await started.apply_hot({"MODEL_DISCOVERY_REFRESH_SECONDS": "600"})
        await asyncio.sleep(0)
        assert timer.running
        assert time.time() + 590 < timer.next_refresh_at < time.time() + 610
        await started.apply_hot({"MODEL_DISCOVERY_REFRESH_SECONDS": "0"})
        await asyncio.sleep(0)
        assert not timer.running
    finally:
        await _close_started(started.runtime)


@pytest.mark.asyncio
async def test_the_proxy_checker_starts_and_retimes_on_save(started) -> None:
    timer = started.runtime._proxy_check_timer
    try:
        assert not timer.running  # off by default
        await started.apply_hot({"PROXY_CHECK_ENABLED": "true"})
        await asyncio.sleep(0)
        assert timer.running
        assert timer.next_check_at > time.time() + 29 * 60  # 30 min default
        await started.apply_hot({"PROXY_CHECK_INTERVAL_MINUTES": "10"})
        await asyncio.sleep(0)
        assert time.time() + 9 * 60 < timer.next_check_at < time.time() + 11 * 60
    finally:
        await _close_started(started.runtime)


@pytest.mark.asyncio
async def test_the_feed_refresh_starts_and_retimes_on_save(started) -> None:
    timer = started.runtime._proxy_feed_timer
    try:
        assert not timer.running  # off by default
        await started.apply_hot({"PROXY_FEED_REFRESH_ENABLED": "true"})
        await asyncio.sleep(0)
        assert timer.running
        assert timer.next_refresh_at > time.time() + 59 * 60  # 60 min default
        await started.apply_hot({"PROXY_FEED_REFRESH_MINUTES": "120"})
        await asyncio.sleep(0)
        assert time.time() + 119 * 60 < timer.next_refresh_at < time.time() + 121 * 60
    finally:
        await _close_started(started.runtime)


@pytest.mark.asyncio
async def test_the_loop_monitor_adopts_its_two_numbers_on_save(started) -> None:
    runtime = started.runtime
    try:
        heartbeat = runtime._loop_heartbeat
        assert heartbeat is not None
        assert heartbeat.interval_seconds == pytest.approx(0.1)
        await started.apply_hot({"HEALTH_HEARTBEAT_INTERVAL_MS": "250"})
        # The same task, beating at the new interval: nothing was restarted.
        assert runtime._loop_heartbeat is heartbeat
        assert heartbeat.interval_seconds == pytest.approx(0.25)
        assert loop_health().interval_seconds == pytest.approx(0.25)
        await started.apply_hot({"HEALTH_BUSY_LAG_MS": "1500"})
        assert loop_health().busy_lag_seconds == pytest.approx(1.5)
    finally:
        await _close_started(runtime)


@pytest.mark.asyncio
async def test_the_watchdog_retimes_stops_and_starts_on_save(started) -> None:
    runtime = started.runtime
    try:
        watchdog = runtime._stall_watchdog
        assert watchdog is not None
        await started.apply_hot({"REQUEST_WATCHDOG_INTERVAL_SECONDS": "7"})
        assert runtime._stall_watchdog is watchdog
        assert watchdog.interval_seconds == 7.0

        await started.apply_hot({"REQUEST_WATCHDOG_ENABLED": "false"})
        assert runtime._stall_watchdog is None
        assert watchdog.task is None
        # The in-flight view is still on, so the registry still tracks.
        assert request_tasks.enabled() is True

        await started.apply_hot({"REQUEST_INFLIGHT_ENABLED": "false"})
        assert request_tasks.inflight_enabled() is False
        assert request_tasks.enabled() is False

        await started.apply_hot({"REQUEST_WATCHDOG_ENABLED": "true"})
        assert runtime._stall_watchdog is not None
        assert runtime._stall_watchdog.interval_seconds == 7.0
        assert request_tasks.enabled() is True
    finally:
        await _close_started(runtime)


@pytest.fixture
def restore_proxy_engine() -> Any:
    yield
    configure_proxy_rotation()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "value", "read", "expected"),
    [
        (
            "PROXY_COOLDOWN_SECONDS",
            "45",
            lambda: PROXY_TUNING.rate_limit_seconds,
            45.0,
        ),
        (
            "PROXY_COOLDOWN_MAX_SECONDS",
            "900",
            lambda: PROXY_TUNING.rate_limit_max_seconds,
            900.0,
        ),
        (
            "PROXY_REACHABILITY_TIERS",
            "5,10,20",
            lambda: (PROXY_TUNING.lockout_tiers, PROXY_REACHABILITY._tiers),
            ((5.0, 10.0, 20.0), (5.0, 10.0, 20.0)),
        ),
    ],
)
async def test_the_proxy_engine_adopts_its_ladder_on_save(
    hot, restore_proxy_engine, key, value, read, expected
) -> None:
    """The engine's own configure entry point, re-called; nothing frozen edited."""

    assert read() != expected
    await hot.apply_hot({key: value})
    assert read() == expected


@pytest.mark.asyncio
async def test_raw_payload_logging_moves_the_loggers_and_the_executor(
    hot, monkeypatch
) -> None:
    monkeypatch.setattr(logging_config, "_configured", True)
    monkeypatch.setattr(logging_config, "_current_verbose", False)
    httpx_logger = logging.getLogger("httpx")
    monkeypatch.setattr(httpx_logger, "level", logging.WARNING)

    await hot.apply_hot({"LOG_RAW_API_PAYLOADS": "true"})

    assert httpx_logger.level == logging.NOTSET
    async with hot.leased() as fresh:
        handler = MessagesHandler(
            fresh.settings, provider_resolver=fresh.resolve_provider
        )
        assert handler._provider_executor._log_raw_payloads is True


# ------------------------------------------------ what still needs a restart


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("HOST", "127.0.0.2"),
        ("PORT", "18579"),
        ("ANTHROPIC_AUTH_TOKEN", "l9-token"),
        ("LOG_LEVEL", "DEBUG"),
        ("SERVER_LOG_RETAIN_FILES", "3"),
        ("SERVER_GRACEFUL_SHUTDOWN_SECONDS", "9"),
        ("SERVER_PORT_TAKEOVER", "never"),
        ("SERVER_STALE_SERVER_ACTION", "stop"),
        ("SERVER_STALE_SESSION_SECONDS", "120"),
    ],
)
async def test_a_process_field_still_restarts_and_is_named(hot, key, value) -> None:
    assert key in PROCESS_RESTART_KEYS
    before = hot.manager.current_generation_id
    body = await hot.apply({key: value})
    assert body["restart"]["required"] is True
    assert body["restart"]["fields"] == [key]
    # Committed, not hot-published: the restart is what applies it.
    assert hot.manager.current_generation_id == before


@pytest.mark.asyncio
@pytest.mark.parametrize("key", sorted(MESSAGING_RESTART_KEYS))
async def test_a_messaging_field_is_hot_while_no_bot_runs(hot, key) -> None:
    assert hot.runtime._messaging_workflow is None
    await hot.apply_hot({key: "true"})


@pytest.mark.asyncio
@pytest.mark.parametrize("key", sorted(MESSAGING_RESTART_KEYS))
async def test_a_messaging_field_restarts_while_a_bot_runs(hot, key) -> None:
    hot.runtime._messaging_workflow = object()  # a bot built with the old value
    try:
        body = await hot.apply({key: "true"})
    finally:
        hot.runtime._messaging_workflow = None
    assert body["restart"]["required"] is True
    assert body["restart"]["fields"] == [key]


# ------------------------------------------------ nothing in flight is cut


@pytest.mark.asyncio
async def test_a_hot_save_leaves_a_request_in_flight_on_its_own_generation(
    hot,
) -> None:
    async with hot.leased() as in_flight:
        old_provider = in_flight.resolve_provider("nvidia_nim")
        old_generation = in_flight.generation_id

        await hot.apply_hot(
            {
                "RATE_LIMIT_COOLDOWN_SECONDS": "91",
                "NVIDIA_NIM_API_KEY_ROTATION": "failover",
            }
        )

        # The running request still resolves the provider it started with,
        # and that provider has not been cleaned up under it.
        assert in_flight.generation_id == old_generation
        assert in_flight.resolve_provider("nvidia_nim") is old_provider
        assert old_provider._config.rate_limit_cooldown_seconds == 60.0
        async with hot.leased() as fresh:
            assert fresh.generation_id == old_generation + 1
            assert fresh.resolve_provider("nvidia_nim")._state.policy == "failover"


# ------------------------------------------------ the re-arm rule, directly


@pytest.mark.asyncio
async def test_a_re_arm_never_cuts_a_pass_that_is_running() -> None:
    """Asleep: cancelled and started again. Mid-pass: left to finish."""

    from my_claude_code.application.model_metadata import ProviderModelRefreshResult
    from my_claude_code.runtime.discovery_timer import ProviderDiscoveryTimer

    entered = asyncio.Event()
    release = asyncio.Event()
    finished: list[bool] = []

    async def sweep(_skipped: frozenset[str]) -> ProviderModelRefreshResult:
        entered.set()
        await release.wait()
        finished.append(True)
        return ProviderModelRefreshResult()

    async def no_wait(_seconds: float) -> None:
        await asyncio.sleep(0)

    timer = ProviderDiscoveryTimer(sweep, lambda: 3600.0, sleep=no_wait)
    assert timer.start()
    await asyncio.wait_for(entered.wait(), 5)
    running = timer._task
    assert timer.rearm() is True
    assert timer._task is running
    assert running is not None and not running.cancelled()
    release.set()
    await asyncio.wait_for(_until(lambda: bool(finished)), 5)
    # The pass that was running when the save landed ran to its end, on the
    # same task (the loop then carries on, which with no wait is at once).
    assert finished[0] is True
    assert timer._task is running and not running.cancelled()
    await timer.close()

    parked = asyncio.Event()

    async def park(_seconds: float) -> None:
        await parked.wait()

    idle = ProviderDiscoveryTimer(sweep, lambda: 3600.0, sleep=park)
    assert idle.start()
    await asyncio.sleep(0)
    sleeping = idle._task
    assert idle.rearm() is True
    assert idle._task is not sleeping
    await asyncio.sleep(0)
    assert sleeping is not None and sleeping.cancelled()
    await idle.close()


async def _until(predicate: Callable[[], bool]) -> None:
    while not predicate():
        await asyncio.sleep(0.01)
