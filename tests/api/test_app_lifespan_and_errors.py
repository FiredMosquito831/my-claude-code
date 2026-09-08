import asyncio
import logging
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import my_claude_code.runtime.asgi as asgi_module
from my_claude_code.application.errors import (
    ApplicationUnavailableError,
    InvalidRequestError,
)
from my_claude_code.application.model_metadata import ProviderModelInfo
from my_claude_code.config.settings import Settings
from my_claude_code.core.startup_state import startup_state
from my_claude_code.messaging.transcription import TranscriptionService
from my_claude_code.providers.nvidia_nim.client import NvidiaNimProvider
from my_claude_code.providers.nvidia_nim.voice import NvidiaNimTranscriber
from my_claude_code.runtime.application import (
    ApplicationRuntime,
    startup_failure_message,
    warn_if_process_auth_token,
)
from my_claude_code.runtime.asgi import RuntimeASGIApp
from my_claude_code.runtime.bootstrap import _create_transcriber, build_asgi_app
from my_claude_code.runtime.provider_manager import ProviderRuntimeManager
from tests.api.support import create_test_app


@pytest.fixture(autouse=True)
def _fresh_startup_state():
    """Reset the process-wide startup state around every test in this module.

    ``startup_state()`` is one object per process, exactly like
    ``stop_deadline()``: a test that drives a lifespan to readiness leaves it
    ready for whatever runs next in the same xdist worker, and an assertion
    about "not ready yet" then depends on test order.
    """

    startup_state().begin()
    yield
    startup_state().begin()


def _settings(**updates: object) -> Settings:
    return Settings().model_copy(update=updates)


@pytest.fixture(autouse=True)
def _redirect_fcc_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))


def test_warn_if_process_auth_token_logs_warning(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "process-token")
    monkeypatch.setitem(Settings.model_config, "env_file", ())

    with patch("my_claude_code.runtime.application.logger.warning") as warning:
        warn_if_process_auth_token(Settings.model_construct())

    warning.assert_called_once()
    assert "ANTHROPIC_AUTH_TOKEN" in warning.call_args.args[0]


def test_warn_if_process_auth_token_skips_explicit_dotenv_config(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("ANTHROPIC_AUTH_TOKEN=\n", encoding="utf-8")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "process-token")
    monkeypatch.setitem(Settings.model_config, "env_file", (env_file,))

    with patch("my_claude_code.runtime.application.logger.warning") as warning:
        warn_if_process_auth_token(Settings.model_construct())

    warning.assert_not_called()


@pytest.mark.asyncio
async def test_runtime_startup_logs_admin_url_without_printed_server_banner():
    settings = _settings(
        messaging_platform="none",
        host="127.0.0.1",
        port=9099,
    )
    manager = ProviderRuntimeManager(settings)
    runtime = ApplicationRuntime(manager, transcriber=None)
    uvicorn_logger = MagicMock()

    with (
        patch("builtins.print") as printed,
        patch.object(manager, "validate_configured_models", new=AsyncMock()),
        patch.object(manager, "start_model_list_refresh") as start_refresh,
        patch.object(manager, "close", new=AsyncMock()),
        patch(
            "my_claude_code.runtime.application.messaging_platform_factory.create_messaging_components",
            return_value=None,
        ),
        patch.object(logging, "getLogger", return_value=uvicorn_logger) as get_logger,
    ):
        await runtime.start()
        await runtime.close()

    printed.assert_not_called()
    start_refresh.assert_called_once()
    get_logger.assert_any_call("uvicorn.error")
    uvicorn_logger.info.assert_called_once_with(
        "Admin UI: %s (local-only)",
        "http://127.0.0.1:9099/admin",
    )


def test_create_app_application_error_handler_returns_anthropic_format():
    app = create_test_app(_settings(log_api_error_tracebacks=False))

    @app.get("/raise_application")
    async def _raise_application():
        raise InvalidRequestError("bad request")

    response = TestClient(app).get("/raise_application")

    assert response.status_code == 400
    body = response.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "invalid_request_error"
    assert body["request_id"] == response.headers["request-id"]
    assert "x-should-retry" not in response.headers


def test_application_error_handler_does_not_log_error_message():
    app = create_test_app(_settings(log_api_error_tracebacks=False))
    secret = "provider-upstream-secret-detail"

    @app.get("/raise_application_secret")
    async def _raise_application_secret():
        raise InvalidRequestError(secret)

    with patch("my_claude_code.api.app.logger.error") as log_error:
        response = TestClient(app).get("/raise_application_secret")

    assert response.status_code == 400
    blob = " ".join(
        str(value) for call in log_error.call_args_list for value in call.args
    )
    assert secret not in blob
    log_error.assert_not_called()


def test_create_app_general_exception_handler_returns_correlated_500():
    app = create_test_app(_settings(log_api_error_tracebacks=False))

    @app.get("/raise_general")
    async def _raise_general():
        raise RuntimeError("boom")

    response = TestClient(app, raise_server_exceptions=False).get("/raise_general")

    assert response.status_code == 500
    body = response.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "api_error"
    assert body["request_id"] == response.headers["request-id"]


def test_general_exception_default_log_excludes_exception_message():
    app = create_test_app(_settings(log_api_error_tracebacks=False))
    secret = "user-provided-secret-token-xyzzy"

    @app.get("/raise_secret")
    async def _raise_secret():
        raise ValueError(secret)

    with patch("my_claude_code.api.app.logger.error") as log_error:
        response = TestClient(app, raise_server_exceptions=False).get("/raise_secret")

    assert response.status_code == 500
    blob = " ".join(
        str(value) for call in log_error.call_args_list for value in call.args
    )
    assert secret not in blob
    assert "ValueError" in blob


@pytest.mark.asyncio
async def test_model_validation_failure_does_not_block_runtime_startup():
    settings = _settings(messaging_platform="none")
    manager = ProviderRuntimeManager(settings)
    runtime = ApplicationRuntime(manager, transcriber=None)
    validation = AsyncMock(side_effect=ApplicationUnavailableError("bad model"))

    with (
        patch.object(manager, "validate_configured_models", new=validation),
        patch.object(manager, "start_model_list_refresh") as start_refresh,
        patch.object(manager, "close", new=AsyncMock()),
        patch(
            "my_claude_code.runtime.application.messaging_platform_factory.create_messaging_components",
            return_value=None,
        ),
    ):
        await runtime.start()
        # Off the readiness path since 6.59.0: the probe asks every configured
        # provider over the network, and it is best-effort, so making the
        # server wait for it only ever delayed the first request.
        task = runtime.configured_model_validation_task
        assert task is not None
        await task
        await runtime.close()

    validation.assert_awaited_once()
    start_refresh.assert_called_once()


def test_startup_failure_message_preserves_existing_concise_contract():
    quiet = _settings(log_api_error_tracebacks=False)
    verbose = _settings(log_api_error_tracebacks=True)

    assert startup_failure_message(quiet, RuntimeError("secret")) == (
        "Server startup failed: exc_type=RuntimeError"
    )
    assert startup_failure_message(verbose, RuntimeError("visible")) == (
        "RuntimeError: visible"
    )
    assert (
        startup_failure_message(
            quiet,
            ApplicationUnavailableError("configured model is unavailable"),
        )
        == "configured model is unavailable"
    )


@pytest.mark.asyncio
async def test_runtime_asgi_app_starts_and_closes_owner_once():
    runtime = MagicMock(spec=ApplicationRuntime)
    runtime.settings = _settings()
    runtime.start = AsyncMock()
    runtime.close = AsyncMock(return_value=True)
    app = RuntimeASGIApp(AsyncMock(), runtime)
    sent: list[dict[str, str]] = []
    shutdown = asyncio.Event()

    async def receive():
        if not sent:
            return {"type": "lifespan.startup"}
        await shutdown.wait()
        return {"type": "lifespan.shutdown"}

    async def send(message):
        sent.append(message)

    lifespan = asyncio.create_task(app({"type": "lifespan"}, receive, send))
    # The startup is a task now, so it is waited for explicitly rather than
    # implicitly by the lifespan message that used to await it.
    await _finished_startup(app)
    shutdown.set()
    await asyncio.wait_for(lifespan, timeout=5)

    runtime.start.assert_awaited_once()
    runtime.close.assert_awaited_once()
    assert sent == [
        {"type": "lifespan.startup.complete"},
        {"type": "lifespan.shutdown.complete"},
    ]


@pytest.mark.asyncio
async def test_the_lifespan_answers_before_the_startup_work_runs() -> None:
    """The bind-first switch, asserted where it is made.

    uvicorn creates its listening socket the instant this message is answered,
    so answering it before the work rather than after it is the difference
    between twenty seconds of "free port" and twenty seconds of "starting".
    """

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_start() -> None:
        started.set()
        await release.wait()

    runtime = MagicMock(spec=ApplicationRuntime)
    runtime.settings = _settings()
    runtime.start = AsyncMock(side_effect=slow_start)
    runtime.close = AsyncMock(return_value=True)
    app = RuntimeASGIApp(AsyncMock(), runtime)
    sent: list[dict[str, str]] = []
    shutdown = asyncio.Event()

    async def receive():
        if not sent:
            return {"type": "lifespan.startup"}
        await shutdown.wait()
        return {"type": "lifespan.shutdown"}

    async def send(message):
        sent.append(message)

    lifespan = asyncio.create_task(app({"type": "lifespan"}, receive, send))
    await asyncio.wait_for(started.wait(), timeout=5)

    # Answered while ``runtime.start`` is still blocked, which is the point.
    assert sent == [{"type": "lifespan.startup.complete"}]
    assert not startup_state().ready

    release.set()
    shutdown.set()
    await asyncio.wait_for(lifespan, timeout=5)


@pytest.mark.asyncio
async def test_a_startup_that_fails_after_the_bind_asks_to_end_the_process() -> None:
    """uvicorn used to end the process for us. Now the supervisor must.

    A listener that stays up answering ``starting`` for ever after its startup
    raised is strictly worse than the old crash: nothing would ever start a
    working server on that port, because the port is not free.
    """

    runtime = MagicMock(spec=ApplicationRuntime)
    runtime.settings = _settings(log_api_error_tracebacks=False)
    runtime.start = AsyncMock(side_effect=RuntimeError("secret"))
    runtime.close = AsyncMock(return_value=True)
    failures: list[bool] = []
    app = RuntimeASGIApp(
        AsyncMock(), runtime, startup_failed_callback=lambda: failures.append(True)
    )
    sent: list[dict[str, str]] = []
    shutdown = asyncio.Event()

    async def receive():
        if not sent:
            return {"type": "lifespan.startup"}
        await shutdown.wait()
        return {"type": "lifespan.shutdown"}

    async def send(message):
        sent.append(message)

    lifespan = asyncio.create_task(app({"type": "lifespan"}, receive, send))
    await _finished_startup(app)
    shutdown.set()
    await asyncio.wait_for(lifespan, timeout=5)

    assert failures == [True]
    assert startup_state().failed
    # The startup itself is still reported as complete: the socket is bound and
    # the process is on its way out under the supervisor's own stop path.
    assert sent[0] == {"type": "lifespan.startup.complete"}


@pytest.mark.asyncio
async def test_the_startup_work_waits_until_the_listener_is_serving() -> None:
    """Bind-first is only true if the bind actually happens first.

    Startup is not purely cooperative -- provider construction and the openai
    import block the loop for seconds at a time -- so a task scheduled ahead of
    uvicorn's own ``create_server`` can starve the very call it was moved in
    front of, and the port stays free for the whole start anyway.
    """

    serving = False
    app = RuntimeASGIApp(
        AsyncMock(),
        _ready_runtime(),
        serving_predicate=lambda: serving,
    )
    sent: list[dict[str, str]] = []
    shutdown = asyncio.Event()

    async def receive():
        if not sent:
            return {"type": "lifespan.startup"}
        await shutdown.wait()
        return {"type": "lifespan.shutdown"}

    async def send(message):
        sent.append(message)

    lifespan = asyncio.create_task(app({"type": "lifespan"}, receive, send))
    await asyncio.sleep(0.05)
    assert not startup_state().ready

    serving = True
    for _ in range(100):
        if startup_state().ready:
            break
        await asyncio.sleep(0.01)
    assert startup_state().ready

    shutdown.set()
    await asyncio.wait_for(lifespan, timeout=5)


def _ready_runtime() -> MagicMock:
    runtime = MagicMock(spec=ApplicationRuntime)
    runtime.settings = _settings()
    runtime.start = AsyncMock()
    runtime.close = AsyncMock(return_value=True)
    return runtime


@pytest.mark.asyncio
async def test_runtime_asgi_app_reports_incomplete_owned_shutdown() -> None:
    runtime = MagicMock(spec=ApplicationRuntime)
    runtime.settings = _settings()
    runtime.start = AsyncMock()
    runtime.close = AsyncMock(return_value=False)
    app = RuntimeASGIApp(AsyncMock(), runtime)
    sent: list[dict[str, str]] = []
    shutdown = asyncio.Event()

    async def receive():
        if not sent:
            return {"type": "lifespan.startup"}
        await shutdown.wait()
        return {"type": "lifespan.shutdown"}

    async def send(message):
        sent.append(message)

    lifespan = asyncio.create_task(app({"type": "lifespan"}, receive, send))
    await _finished_startup(app)
    shutdown.set()
    await asyncio.wait_for(lifespan, timeout=5)

    assert sent == [
        {"type": "lifespan.startup.complete"},
        {"type": "lifespan.shutdown.failed", "message": ""},
    ]


@pytest.mark.asyncio
async def test_runtime_asgi_app_logs_a_concise_startup_failure(caplog):
    runtime = MagicMock(spec=ApplicationRuntime)
    runtime.settings = _settings(log_api_error_tracebacks=False)
    runtime.start = AsyncMock(side_effect=RuntimeError("secret"))
    runtime.close = AsyncMock()
    messages: list[str] = []
    app = RuntimeASGIApp(
        AsyncMock(), runtime, startup_failed_callback=lambda: messages.append("stop")
    )
    sent: list[dict[str, str]] = []
    shutdown = asyncio.Event()

    async def receive():
        if not sent:
            return {"type": "lifespan.startup"}
        await shutdown.wait()
        return {"type": "lifespan.shutdown"}

    async def send(message):
        sent.append(message)

    with patch.object(asgi_module.logger, "error") as error:
        lifespan = asyncio.create_task(app({"type": "lifespan"}, receive, send))
        await _finished_startup(app)
        shutdown.set()
        await asyncio.wait_for(lifespan, timeout=5)

    assert messages == ["stop"]
    logged = " ".join(str(call.args) for call in error.call_args_list)
    assert "exc_type=RuntimeError" in logged
    assert "secret" not in logged


def test_bootstrap_configures_default_log_and_publishes_only_services(tmp_path):
    log_path = tmp_path / "server.log"
    settings = _settings()

    with (
        patch(
            "my_claude_code.runtime.bootstrap.server_log_path",
            return_value=log_path,
        ),
        patch("my_claude_code.runtime.bootstrap.configure_logging") as configure,
    ):
        asgi_app = build_asgi_app(settings)

    configure.assert_called_once_with(
        Path(log_path),
        level=settings.log_level,
        verbose_third_party=settings.log_raw_api_payloads,
        retain_files=settings.server_log_retain_files,
    )
    api_app = cast(FastAPI, asgi_app.app)
    assert set(api_app.state._state) == {"services"}


def test_bootstrap_wires_the_harness_catalogue_fanout_publisher() -> None:
    publisher = MagicMock()

    with (
        patch("my_claude_code.runtime.bootstrap.configure_logging"),
        patch(
            "my_claude_code.runtime.bootstrap.HarnessCatalogueFanoutPublisher",
            return_value=publisher,
        ) as publisher_type,
    ):
        asgi_app = build_asgi_app(_settings())

    manager = asgi_app.runtime.provider_manager
    manager.cache_model_infos(
        "nvidia_nim",
        {ProviderModelInfo("published-model")},
    )

    publisher_type.assert_called_once_with()
    publisher.publish.assert_called_once_with(manager)


def test_bootstrap_honors_process_log_file_override(monkeypatch, tmp_path):
    log_path = tmp_path / "custom.log"
    monkeypatch.setenv("LOG_FILE", str(log_path))

    with patch("my_claude_code.runtime.bootstrap.configure_logging") as configure:
        build_asgi_app(_settings())

    assert configure.call_args.args[0] == log_path


def test_bootstrap_constructs_fresh_runtime_owned_transcribers() -> None:
    settings = _settings(voice_note_enabled=True, whisper_device="cpu")

    first = _create_transcriber(settings)
    second = _create_transcriber(settings)

    assert isinstance(first, TranscriptionService)
    assert isinstance(second, TranscriptionService)
    assert first is not second


@pytest.mark.asyncio
async def test_bootstrap_constructs_isolated_runtime_resource_graphs() -> None:
    settings = _settings(
        model="nvidia_nim/test-model",
        voice_note_enabled=True,
        whisper_device="cpu",
    )

    with patch("my_claude_code.runtime.bootstrap.configure_logging"):
        first = build_asgi_app(settings)
        second = build_asgi_app(settings)

    first_lease = await first.runtime.provider_manager.acquire()
    second_lease = await second.runtime.provider_manager.acquire()
    try:
        first_provider = first_lease.resolve_provider("nvidia_nim")
        second_provider = second_lease.resolve_provider("nvidia_nim")

        assert isinstance(first_provider, NvidiaNimProvider)
        assert isinstance(second_provider, NvidiaNimProvider)
        assert first_provider._rate_limiter is not second_provider._rate_limiter
        assert first.runtime._transcriber is not second.runtime._transcriber
    finally:
        await first_lease.release()
        await second_lease.release()
        await first.runtime.close()
        await second.runtime.close()


def test_bootstrap_selects_nvidia_transcriber_without_loading_riva() -> None:
    settings = _settings(
        voice_note_enabled=True,
        whisper_device="nvidia_nim",
        whisper_model="openai/whisper-large-v3",
        nvidia_nim_api_key="nvapi-test",
    )

    assert isinstance(_create_transcriber(settings), NvidiaNimTranscriber)


def test_bootstrap_disables_transcription_as_one_owned_resource() -> None:
    assert _create_transcriber(_settings(voice_note_enabled=False)) is None


async def _finished_startup(app: RuntimeASGIApp) -> None:
    """Wait for the background startup the lifespan scheduled."""

    for _ in range(500):
        task = app.startup_task
        if task is not None:
            await asyncio.wait([task], timeout=5)
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the lifespan never scheduled a startup task")
