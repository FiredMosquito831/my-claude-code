"""Tests for installed CLI entrypoints, commands, and launchers."""

import json
import os
import subprocess
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, patch
from urllib.error import URLError
from urllib.request import Request

import pytest

from my_claude_code.config.constants import CATALOGUE_FETCH_TIMEOUT_SECONDS
from my_claude_code.config.harnesses import harness_spec
from my_claude_code.config.settings import Settings
from my_claude_code.core.client_fingerprint import HARNESS_HEADER


def _launcher_settings(
    *,
    port: int = 8082,
    token: str = "freecc",
    open_admin_browser: bool = True,
) -> Settings:
    return Settings.model_construct(
        host="0.0.0.0",
        port=port,
        anthropic_auth_token=token,
        model="nvidia_nim/test-model",
        open_admin_browser=open_admin_browser,
    )


def _run_init(tmp_home: Path) -> tuple[str, Path]:
    """Run init() with home directory redirected to tmp_home. Returns (printed output, env_file path)."""
    from my_claude_code.cli.commands import init

    env_file = tmp_home / ".mcc" / ".env"
    printed: list[str] = []

    with (
        patch("pathlib.Path.home", return_value=tmp_home),
        patch(
            "builtins.print",
            side_effect=lambda *a: printed.append(" ".join(str(x) for x in a)),
        ),
    ):
        init()

    return "\n".join(printed), env_file


class _JsonResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def __enter__(self) -> _JsonResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


def test_init_creates_env_file(tmp_path: Path) -> None:
    """init() creates .env from the bundled template when it doesn't exist yet."""
    output, env_file = _run_init(tmp_path)

    assert env_file.exists()
    assert env_file.stat().st_size > 0
    assert str(env_file) in output


def test_init_copies_template_content(tmp_path: Path) -> None:
    """init() writes the canonical root env.example content, not an empty file."""
    template = (Path(__file__).resolve().parents[2] / ".env.example").read_text(
        encoding="utf-8"
    )
    _, env_file = _run_init(tmp_path)

    assert env_file.read_text("utf-8") == template


def test_init_migrates_home_checkout_env_before_template(tmp_path: Path) -> None:
    """init() preserves users who kept config in ~/free-claude-code/.env."""
    legacy_env = tmp_path / "free-claude-code" / ".env"
    legacy_env.parent.mkdir(parents=True)
    legacy_env.write_text("MODEL=deepseek/deepseek-chat\n", encoding="utf-8")

    output, env_file = _run_init(tmp_path)

    assert env_file.read_text("utf-8") == "MODEL=deepseek/deepseek-chat\n"
    assert f"Config migrated from {legacy_env}" in output


def test_init_migrates_legacy_xdg_env_before_template(tmp_path: Path) -> None:
    """init() preserves users who kept config in ~/.config/free-claude-code/.env."""
    legacy_env = tmp_path / ".config" / "free-claude-code" / ".env"
    legacy_env.parent.mkdir(parents=True)
    legacy_env.write_text("MODEL=open_router/free-model\n", encoding="utf-8")

    output, env_file = _run_init(tmp_path)

    assert env_file.read_text("utf-8") == "MODEL=open_router/free-model\n"
    assert f"Config migrated from {legacy_env}" in output


def test_legacy_env_migration_does_not_overwrite_managed_env(
    tmp_path: Path,
) -> None:
    """Legacy migration never overwrites an existing ~/.mcc/.env."""
    from my_claude_code.cli.commands import _migrate_legacy_env_if_missing

    managed_env = tmp_path / ".mcc" / ".env"
    managed_env.parent.mkdir(parents=True)
    managed_env.write_text("MODEL=nvidia_nim/current\n", encoding="utf-8")
    legacy_env = tmp_path / "free-claude-code" / ".env"
    legacy_env.parent.mkdir(parents=True)
    legacy_env.write_text("MODEL=deepseek/legacy\n", encoding="utf-8")

    with patch("pathlib.Path.home", return_value=tmp_path):
        migrated_from = _migrate_legacy_env_if_missing()

    assert migrated_from is None
    assert managed_env.read_text("utf-8") == "MODEL=nvidia_nim/current\n"


def test_env_template_loader_uses_root_template_in_source_checkout() -> None:
    """Source checkout fallback uses the root .env.example as the single source."""
    from my_claude_code.config.env_template import load_env_template

    template = (Path(__file__).resolve().parents[2] / ".env.example").read_text(
        encoding="utf-8"
    )

    assert load_env_template() == template


def test_init_creates_parent_directories(tmp_path: Path) -> None:
    """init() creates ~/.mcc/ even if it doesn't exist."""
    config_dir = tmp_path / ".mcc"
    assert not config_dir.exists()

    _run_init(tmp_path)

    assert config_dir.is_dir()


def test_init_skips_if_env_already_exists(tmp_path: Path) -> None:
    """init() does not overwrite an existing .env and prints a warning."""
    # Create it first
    _run_init(tmp_path)

    env_file = tmp_path / ".mcc" / ".env"
    env_file.write_text("existing content", encoding="utf-8")

    output, _ = _run_init(tmp_path)

    assert env_file.read_text("utf-8") == "existing content"
    assert "already exists" in output


def test_init_prints_next_step_hint(tmp_path: Path) -> None:
    """init() tells the user to run fcc-server after editing .env."""
    output, _ = _run_init(tmp_path)

    assert "fcc-server" in output


def test_cli_scripts_are_registered() -> None:
    pyproject = tomllib.loads(
        (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(
            encoding="utf-8"
        )
    )

    scripts = pyproject["project"]["scripts"]
    assert scripts["fcc-server"] == "my_claude_code.cli.entrypoints:serve"
    assert scripts["free-claude-code"] == "my_claude_code.cli.entrypoints:serve"
    assert scripts["fcc-claude"] == "my_claude_code.cli.launchers.claude:launch"
    assert (
        scripts["fcc-claude-old"] == "my_claude_code.cli.launchers.claude:launch_legacy"
    )
    assert scripts["fcc-codex"] == "my_claude_code.cli.launchers.codex:launch"
    assert scripts["fcc-pi"] == "my_claude_code.cli.launchers.pi:launch"
    assert scripts["mcc-help"] == "my_claude_code.cli.entrypoints:help_command"
    assert scripts["fcc-help"] == "my_claude_code.cli.entrypoints:help_command"
    assert scripts["mcc-rtk"] == "my_claude_code.cli.entrypoints:rtk"

    gui_scripts = pyproject["project"]["gui-scripts"]
    assert gui_scripts["mcc-desktop"] == "my_claude_code.cli.desktop_entrypoint:launch"
    assert gui_scripts["fcc-desktop"] == "my_claude_code.cli.desktop_entrypoint:launch"


def test_help_command_documents_every_mcc_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from my_claude_code.cli import entrypoints

    entrypoints.help_command(())
    out = capsys.readouterr().out

    # Every native command appears with a purpose line.
    for command in (
        "mcc-server",
        "my-claude-code",
        "mcc-claude",
        "mcc-claude-old",
        "mcc-codex",
        "mcc-pi",
        "mcc-init",
        "mcc-chatgpt-oauth-login",
        "mcc-compact-log",
        "mcc-rtk",
        "mcc-desktop",
        "mcc-help",
    ):
        assert command in out
    # The legacy family is acknowledged as aliases, not advertised first.
    assert "fcc-*" in out
    assert "aliases" in out
    # The install-while-running / restart note is present.
    assert "restart" in out


@pytest.mark.parametrize("entrypoint_name", ["serve", "init"])
@pytest.mark.parametrize(
    "argv",
    [("--version",), ("--version", "--help"), ("--help", "--version")],
)
def test_fcc_owned_entrypoints_report_version_without_side_effects(
    entrypoint_name: str,
    argv: tuple[str, ...],
    capsys: pytest.CaptureFixture[str],
) -> None:
    from my_claude_code.cli import entrypoints

    with patch.object(entrypoints, "package_version", return_value="9.8.7"):
        getattr(entrypoints, entrypoint_name)(argv)

    assert capsys.readouterr() == ("free-claude-code 9.8.7\n", "")


@pytest.mark.parametrize("entrypoint_name", ["serve", "init"])
def test_version_entrypoints_do_not_import_command_runtime(
    entrypoint_name: str,
) -> None:
    script = "\n".join(
        (
            "import json",
            "import sys",
            f"from my_claude_code.cli.entrypoints import {entrypoint_name}",
            f"{entrypoint_name}(['--version'])",
            "forbidden = ('uvicorn', 'fastapi', 'openai', "
            "'my_claude_code.cli.commands', "
            "'my_claude_code.runtime.bootstrap')",
            "print(json.dumps([name for name in forbidden if name in sys.modules]))",
        )
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout.splitlines()[-1]) == []


@pytest.mark.parametrize("entrypoint_name", ["serve", "init"])
def test_non_version_entrypoints_delegate_to_command_implementation(
    entrypoint_name: str,
) -> None:
    from my_claude_code.cli import commands, entrypoints

    with patch.object(commands, entrypoint_name) as command:
        getattr(entrypoints, entrypoint_name)(())

    command.assert_called_once_with()


def test_schedule_open_admin_browser_opens_when_health_ready() -> None:
    """Opening /admin runs after /health preflight succeeds."""
    from my_claude_code.cli import commands
    from my_claude_code.config.server_urls import local_admin_url

    settings = _launcher_settings(port=31337)
    opened_urls: list[str] = []

    class ImmediateThread:
        def __init__(self, target=None, **_kwargs: object) -> None:
            self._target = target

        def start(self) -> None:
            assert self._target is not None
            self._target()

    with (
        patch.object(commands.threading, "Thread", ImmediateThread),
        patch.object(commands, "preflight_proxy", return_value=None),
        patch.object(
            commands.webbrowser,
            "open",
            side_effect=lambda url: opened_urls.append(url),
        ),
        patch.object(commands.time, "sleep"),
    ):
        commands._schedule_open_admin_browser(settings)

    assert opened_urls == [local_admin_url(settings)]


def test_serve_skips_admin_browser_when_setting_is_disabled() -> None:
    from my_claude_code.cli import commands

    settings = _launcher_settings(open_admin_browser=False)
    get_settings = MagicMock(return_value=settings)
    get_settings.cache_clear = MagicMock()

    with (
        patch.object(commands, "get_settings", get_settings),
        patch.object(
            commands,
            "_run_supervised_server",
            return_value=commands.ServerExitAction.STOP,
        ) as run_server,
        patch.object(commands, "kill_all_best_effort"),
    ):
        commands.serve()

    run_server.assert_called_once_with(settings, open_admin_browser=False)


def test_serve_supervisor_restarts_when_app_requests_restart() -> None:
    from my_claude_code.cli import commands

    settings = _launcher_settings()
    get_settings = MagicMock(side_effect=[settings, settings])
    get_settings.cache_clear = MagicMock()
    servers: list[object] = []
    restart_callbacks: list[Callable[[], None]] = []

    apps: list[SimpleNamespace] = []

    def build_asgi_app(
        _settings: Settings,
        restart_callback: Callable[[], None],
        process_restart_callback: Callable[[], None],
    ):
        assert callable(process_restart_callback)
        restart_callbacks.append(restart_callback)
        app = SimpleNamespace(runtime=SimpleNamespace(is_closed=False))
        apps.append(app)
        return app

    class FakeServer:
        def __init__(self, config):
            self.config = config
            self.should_exit = False
            servers.append(self)

        def run(self):
            if len(servers) == 1:
                restart_callbacks[-1]()
                assert self.should_exit is True
                self.config.app.runtime.is_closed = True

    def fake_config(app, **kwargs):
        return SimpleNamespace(app=app, kwargs=kwargs)

    with (
        patch.object(commands, "get_settings", get_settings),
        patch.object(commands.uvicorn, "Config", side_effect=fake_config),
        patch.object(commands.uvicorn, "Server", side_effect=FakeServer),
        patch.object(commands, "build_asgi_app", side_effect=build_asgi_app),
        patch.object(commands, "_schedule_open_admin_browser") as schedule_open_admin,
        patch.object(commands, "kill_all_best_effort") as kill_all,
        patch.object(commands, "probe_port_available", return_value=True),
        patch.object(commands, "wait_for_port_free", return_value=True),
    ):
        commands.serve()

    assert len(servers) == 2
    schedule_open_admin.assert_called_once_with(settings)
    get_settings.cache_clear.assert_called_once()
    kill_all.assert_called_once()


def test_serve_supervisor_replaces_process_after_update() -> None:
    from my_claude_code.cli import commands

    settings = _launcher_settings()
    get_settings = MagicMock(return_value=settings)
    get_settings.cache_clear = MagicMock()
    process_callbacks: list[Callable[[], None]] = []

    def build_asgi_app(
        _settings: Settings,
        restart_callback: Callable[[], None],
        process_restart_callback: Callable[[], None],
    ):
        assert callable(restart_callback)
        process_callbacks.append(process_restart_callback)
        return SimpleNamespace(runtime=SimpleNamespace(is_closed=False))

    class FakeServer:
        def __init__(self, config):
            self.config = config
            self.should_exit = False

        def run(self):
            process_callbacks[-1]()
            assert self.should_exit is True
            self.config.app.runtime.is_closed = True

    def fake_config(app, **kwargs):
        return SimpleNamespace(app=app, kwargs=kwargs)

    with (
        patch.object(commands, "get_settings", get_settings),
        patch.object(commands.uvicorn, "Config", side_effect=fake_config),
        patch.object(commands.uvicorn, "Server", side_effect=FakeServer),
        patch.object(commands, "build_asgi_app", side_effect=build_asgi_app),
        patch.object(commands, "_schedule_open_admin_browser"),
        patch.object(commands, "_replace_server_process") as replace_process,
        patch.object(commands, "kill_all_best_effort") as kill_all,
        patch.object(commands, "probe_port_available", return_value=True),
        patch.object(commands, "wait_for_port_free", return_value=True),
    ):
        commands.serve()

    replace_process.assert_called_once()
    get_settings.cache_clear.assert_not_called()
    kill_all.assert_called_once()


def test_process_replacement_flushes_logs_and_execs_stable_launcher() -> None:
    from my_claude_code.cli import commands

    with (
        patch.object(commands, "_WINDOWS", False),
        patch.object(commands, "external_upgrade_helper_pending", return_value=False),
        patch.object(commands, "_server_launcher", return_value="/stable/fcc-server"),
        patch.object(commands.logger, "complete") as complete,
        patch.object(commands, "kill_all_best_effort") as kill_all,
        patch.object(commands, "wait_for_port_free", return_value=True),
        patch.object(commands.os, "execv") as execv,
        patch.object(commands.sys, "argv", ["fcc-server", "--example"]),
    ):
        commands._replace_server_process(_launcher_settings())

    complete.assert_called_once()
    kill_all.assert_called_once()
    execv.assert_called_once_with(
        "/stable/fcc-server", ["/stable/fcc-server", "--example"]
    )


def test_windows_process_replacement_exits_for_the_external_helper() -> None:
    from my_claude_code.cli import commands

    with (
        patch.object(commands, "_WINDOWS", True),
        patch.object(commands.logger, "complete") as complete,
        patch.object(commands, "kill_all_best_effort") as kill_all,
        patch.object(commands.os, "execv") as execv,
    ):
        commands._replace_server_process(_launcher_settings())

    complete.assert_called_once()
    kill_all.assert_called_once()
    execv.assert_not_called()


def test_wsl_drvfs_process_replacement_exits_for_the_external_helper() -> None:
    from my_claude_code.cli import commands

    with (
        patch.object(commands, "_WINDOWS", False),
        patch.object(commands, "external_upgrade_helper_pending", return_value=True),
        patch.object(commands.logger, "complete") as complete,
        patch.object(commands, "kill_all_best_effort") as kill_all,
        patch.object(commands.os, "execv") as execv,
    ):
        commands._replace_server_process(_launcher_settings())

    complete.assert_called_once()
    kill_all.assert_called_once()
    execv.assert_not_called()


def test_serve_supervisor_refuses_restart_after_incomplete_shutdown() -> None:
    """An incomplete close must not silently become a plain stop.

    A config-driven RELOAD keeps the serve loop alive even when the runtime is
    still draining; the fresh generation retries the close. The mock closes on
    the retry so serve() terminates rather than looping forever.
    """
    from my_claude_code.cli import commands

    settings = _launcher_settings()
    get_settings = MagicMock(return_value=settings)
    get_settings.cache_clear = MagicMock()
    servers: list[object] = []
    restart_callbacks: list[Callable[[], None]] = []

    def build_asgi_app(
        _settings: Settings,
        restart_callback: Callable[[], None],
        process_restart_callback: Callable[[], None],
    ):
        assert callable(process_restart_callback)
        restart_callbacks.append(restart_callback)
        # The first generation's close is incomplete; the retry generation
        # finally reports closed so serve() terminates after one reload.
        return SimpleNamespace(
            runtime=SimpleNamespace(
                is_closed=bool(restart_callbacks) and len(restart_callbacks) > 1
            )
        )

    class FakeServer:
        def __init__(self, config):
            self.config = config
            self.should_exit = False
            servers.append(self)

        def run(self):
            if len(servers) == 1:
                restart_callbacks[-1]()
                assert self.should_exit is True

    def fake_config(app, **kwargs):
        return SimpleNamespace(app=app, kwargs=kwargs)

    with (
        patch.object(commands, "get_settings", get_settings),
        patch.object(commands.uvicorn, "Config", side_effect=fake_config),
        patch.object(commands.uvicorn, "Server", side_effect=FakeServer),
        patch.object(commands, "build_asgi_app", side_effect=build_asgi_app),
        patch.object(commands, "_schedule_open_admin_browser"),
        patch.object(commands, "kill_all_best_effort") as kill_all,
        patch.object(commands, "probe_port_available", return_value=True),
        patch.object(commands, "wait_for_port_free", return_value=True),
    ):
        commands.serve()

    assert len(servers) == 2
    get_settings.cache_clear.assert_called_once()
    kill_all.assert_called_once()


def test_serve_migrates_legacy_env_before_loading_settings(tmp_path: Path) -> None:
    from my_claude_code.cli import commands

    legacy_env = tmp_path / "free-claude-code" / ".env"
    legacy_env.parent.mkdir(parents=True)
    legacy_env.write_text("MODEL=deepseek/deepseek-chat\n", encoding="utf-8")
    settings = _launcher_settings()
    get_settings = MagicMock(return_value=settings)
    get_settings.cache_clear = MagicMock()

    with (
        patch("pathlib.Path.home", return_value=tmp_path),
        patch.object(commands, "get_settings", get_settings),
        patch.object(
            commands,
            "_run_supervised_server",
            return_value=commands.ServerExitAction.STOP,
        ),
        patch.object(commands, "kill_all_best_effort"),
    ):
        commands.serve()

    assert (tmp_path / ".mcc" / ".env").read_text("utf-8") == (
        "MODEL=deepseek/deepseek-chat\n"
    )
    get_settings.assert_called_once_with()


def test_serve_migrates_hf_token_before_loading_settings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from my_claude_code.cli import commands

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text("HF_TOKEN=legacy-hf\n", encoding="utf-8")
    settings = _launcher_settings()
    get_settings = MagicMock(return_value=settings)
    get_settings.cache_clear = MagicMock()
    monkeypatch.chdir(repo)

    with (
        patch("pathlib.Path.home", return_value=tmp_path),
        patch.object(commands, "get_settings", get_settings),
        patch.object(
            commands,
            "_run_supervised_server",
            return_value=commands.ServerExitAction.STOP,
        ),
        patch.object(commands, "kill_all_best_effort"),
        patch.object(commands, "explicit_env_file_migration_warning"),
    ):
        commands.serve()

    assert (repo / ".env").read_text(encoding="utf-8") == (
        "HUGGINGFACE_API_KEY=legacy-hf\n"
    )
    get_settings.assert_called_once_with()


def test_config_env_key_migration_warns_for_explicit_env_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from my_claude_code.cli import commands

    explicit = tmp_path / "custom.env"
    explicit.write_text("HF_TOKEN=legacy-hf\n", encoding="utf-8")

    with patch.dict(commands.os.environ, {"FCC_ENV_FILE": str(explicit)}):
        migrated = commands._migrate_config_env_keys()

    assert migrated == ()
    assert "HF_TOKEN" in capsys.readouterr().err
    assert explicit.read_text(encoding="utf-8") == "HF_TOKEN=legacy-hf\n"


def test_serve_handles_keyboard_interrupt_without_traceback() -> None:
    from my_claude_code.cli import commands

    settings = _launcher_settings()
    get_settings = MagicMock(return_value=settings)
    get_settings.cache_clear = MagicMock()

    with (
        patch.object(commands, "get_settings", get_settings),
        patch.object(
            commands,
            "_run_supervised_server",
            side_effect=KeyboardInterrupt,
        ),
        patch.object(commands, "kill_all_best_effort") as kill_all,
    ):
        commands.serve()

    get_settings.cache_clear.assert_not_called()
    kill_all.assert_called_once()


def test_claude_child_env_targets_current_proxy_config() -> None:
    from my_claude_code.cli.claude_env import build_claude_proxy_env

    env = build_claude_proxy_env(
        proxy_root_url="http://127.0.0.1:9090",
        auth_token=" proxy-token ",
        base_env={
            "PATH": "keep",
            "ANTHROPIC_API_URL": "https://api.anthropic.com/v1",
            "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
            "ANTHROPIC_AUTH_TOKEN": "old-token",
            "ANTHROPIC_API_KEY": "official-key",
            "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "0",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "0",
            "DISABLE_FEEDBACK_COMMAND": "0",
            "DISABLE_ERROR_REPORTING": "0",
            "DISABLE_TELEMETRY": "0",
        },
    )

    assert env["PATH"] == "keep"
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9090"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "proxy-token"
    assert env["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"] == "1"
    assert env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "190000"
    assert env["DISABLE_AUTOUPDATER"] == "1"
    assert env["DISABLE_FEEDBACK_COMMAND"] == "1"
    assert env["DISABLE_ERROR_REPORTING"] == "1"
    assert env["DISABLE_TELEMETRY"] == "1"
    assert "ANTHROPIC_API_URL" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC" not in env


def test_claude_child_env_uses_sentinel_for_blank_configured_auth_token() -> None:
    from my_claude_code.cli.claude_env import build_claude_proxy_env

    env = build_claude_proxy_env(
        proxy_root_url="http://127.0.0.1:8082",
        auth_token="",
        base_env={
            "ANTHROPIC_AUTH_TOKEN": "inherited-token",
            "ANTHROPIC_API_KEY": "official-key",
        },
    )

    assert env["ANTHROPIC_AUTH_TOKEN"] == "fcc-no-auth"
    assert "ANTHROPIC_API_KEY" not in env


def test_claude_minimal_child_env_sets_only_proxy_variables() -> None:
    from my_claude_code.cli.claude_env import build_minimal_claude_proxy_env

    base_env = {
        "PATH": "keep",
        "ANTHROPIC_API_KEY": "official-key",
        "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
        "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "0",
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "5000",
    }

    env = build_minimal_claude_proxy_env(
        proxy_root_url="http://127.0.0.1:9090",
        auth_token=" proxy-token ",
        base_env=base_env,
    )

    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9090"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "proxy-token"
    # Web server tools default off on the client, mirroring the proxy setting.
    assert "ENABLE_WEB_SERVER_TOOLS" not in env
    assert set(env) - set(base_env) == {
        "ANTHROPIC_AUTH_TOKEN",
        # MCC's own attribution header. It is a launcher-set variable like the
        # two above, not a variable of the user's this builder preserves.
        "ANTHROPIC_CUSTOM_HEADERS",
    }
    assert env["ANTHROPIC_CUSTOM_HEADERS"] == f"{HARNESS_HEADER}: claude"
    assert env["PATH"] == "keep"
    assert env["ANTHROPIC_API_KEY"] == "official-key"
    assert env["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"] == "0"
    assert env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "5000"


def test_claude_minimal_child_env_web_server_tools_flag_adds_only_that_var() -> None:
    """`enable_web_server_tools=True` adds exactly the web-tools key."""
    from my_claude_code.cli.claude_env import build_minimal_claude_proxy_env

    base_env = {"PATH": "keep"}
    proxy_args = {
        "proxy_root_url": "http://127.0.0.1:9090",
        "auth_token": "proxy-token",
        "base_env": base_env,
    }

    without_web_tools = build_minimal_claude_proxy_env(
        proxy_root_url=proxy_args["proxy_root_url"],
        auth_token=proxy_args["auth_token"],
        base_env=proxy_args["base_env"],
        enable_web_server_tools=False,
    )
    with_web_tools = build_minimal_claude_proxy_env(
        proxy_root_url=proxy_args["proxy_root_url"],
        auth_token=proxy_args["auth_token"],
        base_env=proxy_args["base_env"],
        enable_web_server_tools=True,
    )

    assert "ENABLE_WEB_SERVER_TOOLS" not in without_web_tools
    assert set(with_web_tools) - set(without_web_tools) == {"ENABLE_WEB_SERVER_TOOLS"}
    assert with_web_tools["ENABLE_WEB_SERVER_TOOLS"] == "true"
    for key in without_web_tools:
        assert with_web_tools[key] == without_web_tools[key]


def test_claude_minimal_child_env_uses_sentinel_for_blank_configured_auth_token() -> (
    None
):
    from my_claude_code.cli.claude_env import build_minimal_claude_proxy_env

    env = build_minimal_claude_proxy_env(
        proxy_root_url="http://127.0.0.1:8082",
        auth_token="",
        base_env={"ANTHROPIC_AUTH_TOKEN": "inherited-token"},
    )

    assert env["ANTHROPIC_AUTH_TOKEN"] == "fcc-no-auth"


def test_claude_minimal_child_env_discovery_flag_adds_only_the_discovery_var() -> None:
    """`enable_model_discovery=True` adds exactly one key relative to `False`."""
    from my_claude_code.cli.claude_env import build_minimal_claude_proxy_env

    base_env = {"PATH": "keep"}
    proxy_args = {
        "proxy_root_url": "http://127.0.0.1:9090",
        "auth_token": "proxy-token",
        "base_env": base_env,
    }

    without_discovery = build_minimal_claude_proxy_env(
        proxy_root_url=proxy_args["proxy_root_url"],
        auth_token=proxy_args["auth_token"],
        base_env=proxy_args["base_env"],
        enable_model_discovery=False,
    )
    with_discovery = build_minimal_claude_proxy_env(
        proxy_root_url=proxy_args["proxy_root_url"],
        auth_token=proxy_args["auth_token"],
        base_env=proxy_args["base_env"],
        enable_model_discovery=True,
    )

    assert "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY" not in without_discovery
    assert set(with_discovery) - set(without_discovery) == {
        "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"
    }
    assert with_discovery["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"] == "1"
    for key in without_discovery:
        assert with_discovery[key] == without_discovery[key]


def test_claude_minimal_child_env_defaults_to_no_discovery_key() -> None:
    """The default omits the discovery key entirely, not just falsy."""
    from my_claude_code.cli.claude_env import build_minimal_claude_proxy_env

    env = build_minimal_claude_proxy_env(
        proxy_root_url="http://127.0.0.1:9090",
        auth_token="proxy-token",
        base_env={},
    )

    assert "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY" not in env


@pytest.mark.parametrize(
    ("argv", "expected_found", "expected_remaining"),
    [
        ([], False, []),
        (["-p", "hi"], False, ["-p", "hi"]),
        (["--discover-models"], True, []),
        (["--discover-models", "-p", "hi"], True, ["-p", "hi"]),
        (["-p", "hi", "--discover-models"], True, ["-p", "hi"]),
        (
            ["--discover-models", "--discover-models", "-p", "hi"],
            True,
            ["-p", "hi"],
        ),
        (
            ["--discover-models", "--", "--discover-models"],
            True,
            ["--", "--discover-models"],
        ),
        (
            ["--", "--discover-models"],
            False,
            ["--", "--discover-models"],
        ),
        (
            ["-p", "explain --discover-models"],
            False,
            ["-p", "explain --discover-models"],
        ),
        (["--", "-p", "hi"], False, ["--", "-p", "hi"]),
    ],
)
def test_split_discover_models_flag_edge_cases(
    argv: list[str],
    expected_found: bool,
    expected_remaining: list[str],
) -> None:
    from my_claude_code.cli.launchers.claude import _split_discover_models_flag

    found, remaining = _split_discover_models_flag(argv)

    assert found is expected_found
    assert remaining == expected_remaining


def test_launch_claude_uses_minimal_env_and_passes_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from my_claude_code.cli.launchers.claude import launch

    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "old-token")
    monkeypatch.setenv("KEEP_ME", "yes")
    settings = _launcher_settings(port=9191, token="proxy-token")
    inherited_env = dict(os.environ)

    with (
        patch(
            "my_claude_code.cli.launchers.claude.get_settings", return_value=settings
        ),
        patch("my_claude_code.cli.launchers.claude.preflight_proxy", return_value=None),
        patch(
            "my_claude_code.cli.launchers.common.shutil.which",
            return_value="resolved-claude.cmd",
        ),
        patch("my_claude_code.cli.launchers.common.subprocess.Popen") as popen,
        patch("my_claude_code.cli.launchers.common.register_pid") as register_pid,
        patch("my_claude_code.cli.launchers.common.unregister_pid") as unregister_pid,
        pytest.raises(SystemExit) as exc_info,
    ):
        process = popen.return_value
        process.pid = 12345
        process.wait.return_value = 7
        launch(["--model", "sonnet"])

    assert exc_info.value.code == 7
    popen.assert_called_once()
    assert popen.call_args.args[0] == ["resolved-claude.cmd", "--model", "sonnet"]
    child_env = popen.call_args.kwargs["env"]
    assert child_env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9191"
    assert child_env["ANTHROPIC_AUTH_TOKEN"] == "proxy-token"
    assert child_env["ENABLE_WEB_SERVER_TOOLS"] == "true"
    assert child_env["KEEP_ME"] == "yes"
    # Only the two proxy variables plus the forced web-server-tools flag differ
    # from the inherited environment; everything else — including anything
    # already set by the caller's shell — is left exactly as it was.
    changed = {
        key
        for key in set(inherited_env) | set(child_env)
        if inherited_env.get(key) != child_env.get(key)
    }
    assert changed == {
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_CUSTOM_HEADERS",
        "ENABLE_WEB_SERVER_TOOLS",
    }
    assert child_env["ANTHROPIC_CUSTOM_HEADERS"] == f"{HARNESS_HEADER}: claude"
    register_pid.assert_called_once_with(12345)
    unregister_pid.assert_called_once_with(12345)


def test_launch_claude_omits_web_tools_when_proxy_setting_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The client web-tools flag mirrors the proxy setting: off -> no env var."""
    from my_claude_code.cli.launchers.claude import launch

    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "old-token")
    settings = _launcher_settings(port=9191, token="proxy-token")
    settings = settings.model_copy(update={"enable_web_server_tools": False}, deep=True)
    inherited_env = dict(os.environ)

    with (
        patch(
            "my_claude_code.cli.launchers.claude.get_settings", return_value=settings
        ),
        patch("my_claude_code.cli.launchers.claude.preflight_proxy", return_value=None),
        patch(
            "my_claude_code.cli.launchers.common.shutil.which",
            return_value="resolved-claude.cmd",
        ),
        patch("my_claude_code.cli.launchers.common.subprocess.Popen") as popen,
        patch("my_claude_code.cli.launchers.common.register_pid"),
        patch("my_claude_code.cli.launchers.common.unregister_pid"),
        pytest.raises(SystemExit),
    ):
        process = popen.return_value
        process.pid = 12345
        process.wait.return_value = 0
        launch(["--model", "sonnet"])

    child_env = popen.call_args.kwargs["env"]
    assert "ENABLE_WEB_SERVER_TOOLS" not in child_env
    changed = {
        key
        for key in set(inherited_env) | set(child_env)
        if inherited_env.get(key) != child_env.get(key)
    }
    assert changed == {
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_CUSTOM_HEADERS",
    }


def test_launch_claude_discover_models_flag_strips_flag_and_enables_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--discover-models` is not forwarded to Claude, but sets the env var."""
    from my_claude_code.cli.launchers import claude as claude_launcher

    settings = _launcher_settings(port=9191, token="proxy-token")
    calls: list[dict[str, object]] = []

    def fake_run_client_process(**kwargs: object) -> None:
        calls.append(kwargs)
        raise SystemExit(0)

    with (
        patch.object(claude_launcher, "get_settings", return_value=settings),
        patch.object(claude_launcher, "preflight_proxy", return_value=None),
        patch.object(
            claude_launcher,
            "resolve_harness_binary",
            return_value="resolved-claude.cmd",
        ),
        patch.object(
            claude_launcher, "run_client_process", side_effect=fake_run_client_process
        ),
        pytest.raises(SystemExit) as exc_info,
    ):
        claude_launcher.launch(["--discover-models", "-p", "hi"])

    assert exc_info.value.code == 0
    assert len(calls) == 1
    assert calls[0]["command"] == ["resolved-claude.cmd", "-p", "hi"]
    env = cast(dict[str, str], calls[0]["env"])
    assert env["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"] == "1"


def test_launch_claude_without_flag_forwards_args_and_skips_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the flag, args pass through unchanged and no env var is set."""
    from my_claude_code.cli.launchers import claude as claude_launcher

    settings = _launcher_settings(port=9191, token="proxy-token")
    calls: list[dict[str, object]] = []

    def fake_run_client_process(**kwargs: object) -> None:
        calls.append(kwargs)
        raise SystemExit(0)

    with (
        patch.object(claude_launcher, "get_settings", return_value=settings),
        patch.object(claude_launcher, "preflight_proxy", return_value=None),
        patch.object(
            claude_launcher,
            "resolve_harness_binary",
            return_value="resolved-claude.cmd",
        ),
        patch.object(
            claude_launcher, "run_client_process", side_effect=fake_run_client_process
        ),
        pytest.raises(SystemExit) as exc_info,
    ):
        claude_launcher.launch(["-p", "hi"])

    assert exc_info.value.code == 0
    assert len(calls) == 1
    assert calls[0]["command"] == ["resolved-claude.cmd", "-p", "hi"]
    env = cast(dict[str, str], calls[0]["env"])
    assert "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY" not in env


def test_launch_claude_legacy_passes_args_and_full_child_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from my_claude_code.cli.launchers.claude import launch_legacy

    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "old-token")
    monkeypatch.setenv("KEEP_ME", "yes")
    settings = _launcher_settings(port=9191, token="proxy-token")

    with (
        patch(
            "my_claude_code.cli.launchers.claude.get_settings", return_value=settings
        ),
        patch("my_claude_code.cli.launchers.claude.preflight_proxy", return_value=None),
        patch(
            "my_claude_code.cli.launchers.common.shutil.which",
            return_value="resolved-claude.cmd",
        ),
        patch("my_claude_code.cli.launchers.common.subprocess.Popen") as popen,
        patch("my_claude_code.cli.launchers.common.register_pid") as register_pid,
        patch("my_claude_code.cli.launchers.common.unregister_pid") as unregister_pid,
        pytest.raises(SystemExit) as exc_info,
    ):
        process = popen.return_value
        process.pid = 12345
        process.wait.return_value = 7
        launch_legacy(["--model", "sonnet"])

    assert exc_info.value.code == 7
    popen.assert_called_once()
    assert popen.call_args.args[0] == ["resolved-claude.cmd", "--model", "sonnet"]
    child_env = popen.call_args.kwargs["env"]
    assert child_env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9191"
    assert child_env["ANTHROPIC_AUTH_TOKEN"] == "proxy-token"
    assert child_env["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"] == "1"
    assert child_env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "190000"
    assert child_env["DISABLE_AUTOUPDATER"] == "1"
    assert child_env["DISABLE_FEEDBACK_COMMAND"] == "1"
    assert child_env["DISABLE_ERROR_REPORTING"] == "1"
    assert child_env["DISABLE_TELEMETRY"] == "1"
    assert "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC" not in child_env
    assert child_env["KEEP_ME"] == "yes"
    register_pid.assert_called_once_with(12345)
    unregister_pid.assert_called_once_with(12345)


def test_launch_codex_passes_responses_config_and_child_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from my_claude_code.cli.launchers.codex import launch

    monkeypatch.setenv("OPENAI_API_KEY", "official-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("CODEX_HOME", "keep-home")
    monkeypatch.setenv("CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "Codex Desktop")
    monkeypatch.setenv("CODEX_PERMISSION_PROFILE", "danger-full-access")
    monkeypatch.setenv("CODEX_SHELL", "1")
    monkeypatch.setenv("CODEX_THREAD_ID", "parent-thread")
    settings = _launcher_settings(port=9191, token="proxy-token")
    catalog_path = tmp_path / "codex-model-catalog.json"
    requests: list[Request] = []

    def fake_urlopen(request: Request, *, timeout: float) -> _JsonResponse:
        requests.append(request)
        # This route's own budget, never the /health preflight's 1.5s. The two
        # sharing one constant is the whole of the defect fixed in 6.36.1.
        assert timeout == CATALOGUE_FETCH_TIMEOUT_SECONDS
        return _JsonResponse(
            {
                "models": [],
                "catalogues": {
                    "codex": {
                        "format": "codex",
                        "filename": "codex-model-catalog.json",
                        "document": {
                            "models": [
                                {
                                    "slug": "nvidia_nim/provider-model",
                                    "display_name": "nvidia_nim/provider-model",
                                    "context_window": 262144,
                                }
                            ],
                            "_mcc_defaulted": {
                                "nvidia_nim/provider-model": ["input_modalities"]
                            },
                        },
                        "defaulted": {
                            "nvidia_nim/provider-model": ["input_modalities"]
                        },
                    }
                },
            }
        )

    with (
        patch("my_claude_code.cli.launchers.codex.get_settings", return_value=settings),
        patch("my_claude_code.cli.launchers.codex.preflight_proxy", return_value=None),
        patch(
            "my_claude_code.cli.launchers.common.shutil.which",
            return_value="resolved-codex.cmd",
        ),
        patch(
            "my_claude_code.cli.launchers.codex.codex_model_catalog_path",
            return_value=catalog_path,
        ),
        patch(
            "my_claude_code.cli.harnesses.catalogue_client.urlopen",
            side_effect=fake_urlopen,
        ),
        patch("my_claude_code.cli.launchers.common.subprocess.Popen") as popen,
        patch("my_claude_code.cli.launchers.common.register_pid") as register_pid,
        patch("my_claude_code.cli.launchers.common.unregister_pid") as unregister_pid,
        pytest.raises(SystemExit) as exc_info,
    ):
        process = popen.return_value
        process.pid = 12345
        process.wait.return_value = 0
        launch(["exec", "hello"])

    assert exc_info.value.code == 0
    command = popen.call_args.args[0]
    assert command[0] == "resolved-codex.cmd"
    assert 'model_provider="fcc"' in command
    assert 'model_providers.fcc.base_url="http://127.0.0.1:9191/v1"' in command
    assert 'model_providers.fcc.wire_api="responses"' in command
    assert f"model_catalog_json={json.dumps(str(catalog_path))}" in command
    assert command[-2:] == ["exec", "hello"]
    assert len(requests) == 1
    request = requests[0]
    assert request.full_url == "http://127.0.0.1:9191/admin/api/catalogue-models"
    headers = {key.lower(): value for key, value in request.header_items()}
    assert headers["authorization"] == "Bearer proxy-token"
    assert "x-api-key" not in headers
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    assert [model["slug"] for model in catalog["models"]] == [
        "nvidia_nim/provider-model"
    ]
    # The window is the server's answer for that model, not a launcher literal.
    assert catalog["models"][0]["context_window"] == 262144
    error_output = capsys.readouterr().err
    # One line, not one per model: the per-model detail is behind
    # MCC_CATALOGUE_VERBOSE=1 and has two other homes.
    assert "carry a value Codex supplied because no provider published one" in (
        error_output
    )
    assert "MCC_CATALOGUE_VERBOSE=1" in error_output
    child_env = popen.call_args.kwargs["env"]
    assert child_env["FCC_CODEX_API_KEY"] == "proxy-token"
    assert child_env["CODEX_HOME"] == "keep-home"
    assert "CODEX_INTERNAL_ORIGINATOR_OVERRIDE" not in child_env
    assert "CODEX_PERMISSION_PROFILE" not in child_env
    assert "CODEX_SHELL" not in child_env
    assert "CODEX_THREAD_ID" not in child_env
    assert "OPENAI_API_KEY" not in child_env
    assert "OPENAI_BASE_URL" not in child_env
    register_pid.assert_called_once_with(12345)
    unregister_pid.assert_called_once_with(12345)


def test_launch_codex_catalog_failure_warns_and_continues(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    from my_claude_code.cli.launchers.codex import launch

    settings = _launcher_settings(port=9191, token="proxy-token")

    with (
        patch("my_claude_code.cli.launchers.codex.get_settings", return_value=settings),
        patch("my_claude_code.cli.launchers.codex.preflight_proxy", return_value=None),
        patch(
            "my_claude_code.cli.launchers.common.shutil.which",
            return_value="resolved-codex.cmd",
        ),
        patch(
            "my_claude_code.cli.launchers.codex.codex_model_catalog_path",
            return_value=tmp_path / "codex-model-catalog.json",
        ),
        patch(
            "my_claude_code.cli.harnesses.catalogue_client.urlopen",
            side_effect=URLError("boom"),
        ),
        patch("my_claude_code.cli.launchers.common.subprocess.Popen") as popen,
        patch("my_claude_code.cli.launchers.common.register_pid"),
        patch("my_claude_code.cli.launchers.common.unregister_pid"),
        pytest.raises(SystemExit) as exc_info,
    ):
        process = popen.return_value
        process.pid = 12345
        process.wait.return_value = 0
        launch(["exec", "hello"])

    assert exc_info.value.code == 0
    command = popen.call_args.args[0]
    assert not any("model_catalog_json=" in arg for arg in command)
    captured = capsys.readouterr()
    # The warning names the file it wanted, the request it tried and what to do
    # about it -- the old one said only that something failed.
    assert "no Codex model list at" in captured.err
    assert "codex-model-catalog.json" in captured.err
    assert "/admin/api/catalogue-models" in captured.err
    assert "CATALOGUE_FETCH_TIMEOUT_SECONDS" in captured.err
    assert "Start the server with mcc-server" in captured.err
    assert "Launching without the model picker catalog." in captured.err


def test_pi_launcher_builds_scoped_session_command_and_proxy_env(
    tmp_path: Path,
) -> None:
    from my_claude_code.cli.launchers.pi import (
        build_pi_launcher_command,
        build_pi_launcher_env,
    )

    extension = tmp_path / "pi_extension.ts"
    env = build_pi_launcher_env(
        proxy_root_url="http://127.0.0.1:9191/",
        auth_token=" proxy-token ",
        base_env={
            "PATH": "keep",
            "ANTHROPIC_API_KEY": "native-pi-credential",
            "FCC_PI_API_KEY": "stale-key",
            "FCC_PI_BASE_URL": "https://stale.invalid",
        },
    )

    assert build_pi_launcher_command(
        binary_path="resolved-pi.cmd",
        extension_path=extension,
        argv=["--print", "hello"],
    ) == [
        "resolved-pi.cmd",
        "-e",
        str(extension),
        "--models",
        "free-claude-code/**",
        "--print",
        "hello",
    ]
    assert env == {
        "PATH": "keep",
        "ANTHROPIC_API_KEY": "native-pi-credential",
        "FCC_PI_BASE_URL": "http://127.0.0.1:9191",
        "FCC_PI_API_KEY": "proxy-token",
    }


def test_pi_launcher_uses_no_auth_sentinel_for_blank_token() -> None:
    from my_claude_code.cli.launchers.pi import build_pi_launcher_env

    env = build_pi_launcher_env(
        proxy_root_url="http://127.0.0.1:8082",
        auth_token="",
        base_env={},
    )

    assert env["FCC_PI_API_KEY"] == "fcc-no-auth"


def test_launch_pi_registers_bundled_extension_for_sessions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from my_claude_code.cli.launchers.pi import launch

    monkeypatch.setenv("KEEP_ME", "yes")
    monkeypatch.setenv("FCC_PI_API_KEY", "stale-key")
    extension = tmp_path / "pi_extension.ts"
    extension.write_text("export default () => {};", encoding="utf-8")
    settings = _launcher_settings(port=9191, token="proxy-token")

    with (
        patch("my_claude_code.cli.launchers.pi.get_settings", return_value=settings),
        patch("my_claude_code.cli.launchers.pi.preflight_proxy", return_value=None),
        patch(
            "my_claude_code.cli.launchers.pi.pi_extension_path",
            return_value=extension,
        ),
        patch(
            "my_claude_code.cli.launchers.common.shutil.which",
            return_value="resolved-pi.cmd",
        ),
        patch(
            "my_claude_code.cli.launchers.pi.pi_binary_is_compatible",
            return_value=True,
        ),
        patch("my_claude_code.cli.launchers.common.subprocess.Popen") as popen,
        patch("my_claude_code.cli.launchers.common.register_pid"),
        patch("my_claude_code.cli.launchers.common.unregister_pid"),
        pytest.raises(SystemExit) as exc_info,
    ):
        process = popen.return_value
        process.pid = 12345
        process.wait.return_value = 0
        launch(["--print", "hello"])

    assert exc_info.value.code == 0
    assert popen.call_args.args[0] == [
        "resolved-pi.cmd",
        "-e",
        str(extension),
        "--models",
        "free-claude-code/**",
        "--print",
        "hello",
    ]
    child_env = popen.call_args.kwargs["env"]
    assert child_env["FCC_PI_BASE_URL"] == "http://127.0.0.1:9191"
    assert child_env["FCC_PI_API_KEY"] == "proxy-token"
    assert child_env["KEEP_ME"] == "yes"


@pytest.mark.parametrize(
    "argv",
    [
        ["--help"],
        ["--version"],
        ["config", "set", "theme", "dark"],
        ["install", "npm:example"],
        ["list"],
        ["remove", "npm:example"],
        ["uninstall", "npm:example"],
        ["update"],
    ],
)
def test_launch_pi_passes_management_commands_through_without_proxy(
    argv: list[str],
) -> None:
    from my_claude_code.cli.launchers.pi import launch

    with (
        patch("my_claude_code.cli.launchers.pi.get_settings") as get_settings,
        patch("my_claude_code.cli.launchers.pi.preflight_proxy") as preflight,
        patch(
            "my_claude_code.cli.launchers.common.shutil.which",
            return_value="resolved-pi",
        ),
        patch(
            "my_claude_code.cli.launchers.pi.pi_binary_is_compatible",
            return_value=True,
        ),
        patch("my_claude_code.cli.launchers.common.subprocess.Popen") as popen,
        patch("my_claude_code.cli.launchers.common.register_pid"),
        patch("my_claude_code.cli.launchers.common.unregister_pid"),
        pytest.raises(SystemExit) as exc_info,
    ):
        process = popen.return_value
        process.pid = 12345
        process.wait.return_value = 0
        launch(argv)

    assert exc_info.value.code == 0
    assert popen.call_args.args[0] == ["resolved-pi", *argv]
    get_settings.assert_not_called()
    preflight.assert_not_called()


def test_launch_pi_fails_closed_when_bundled_extension_is_missing(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    from my_claude_code.cli.launchers.pi import launch

    settings = _launcher_settings(port=9191)
    with (
        patch("my_claude_code.cli.launchers.pi.get_settings", return_value=settings),
        patch("my_claude_code.cli.launchers.pi.preflight_proxy", return_value=None),
        patch(
            "my_claude_code.cli.launchers.pi.pi_extension_path",
            return_value=tmp_path / "missing.ts",
        ),
        patch(
            "my_claude_code.cli.launchers.common.shutil.which",
            return_value="resolved-pi",
        ),
        patch(
            "my_claude_code.cli.launchers.pi.pi_binary_is_compatible",
            return_value=True,
        ),
        patch("my_claude_code.cli.launchers.common.subprocess.Popen") as popen,
        pytest.raises(SystemExit) as exc_info,
    ):
        launch([])

    assert exc_info.value.code == 1
    popen.assert_not_called()
    assert "bundled Pi extension is missing" in capsys.readouterr().err


def test_pi_install_hints_use_official_platform_installers() -> None:
    from my_claude_code.cli.launchers.pi import pi_install_hint

    assert "https://pi.dev/install.ps1" in pi_install_hint("win32")
    assert "https://pi.dev/install.sh" in pi_install_hint("darwin")


@pytest.mark.parametrize(
    ("help_output", "return_code", "expected"),
    [
        ("--extension <path>\n--models <patterns>\n", 0, True),
        ("--models <patterns>\n", 0, False),
        ("--extension <path>\n", 0, False),
        ("--extension <path>\n--models <patterns>\n", 1, False),
    ],
)
def test_pi_binary_compatibility_requires_both_launcher_capabilities(
    help_output: str,
    return_code: int,
    expected: bool,
) -> None:
    from my_claude_code.cli.launchers.pi import pi_binary_is_compatible

    with patch(
        "my_claude_code.cli.launchers.pi.subprocess.run",
        return_value=SimpleNamespace(returncode=return_code, stdout=help_output),
    ):
        assert pi_binary_is_compatible("resolved-pi") is expected


def test_launch_pi_rejects_unrelated_pi_binary(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from my_claude_code.cli.launchers.pi import launch

    with (
        patch(
            "my_claude_code.cli.launchers.common.shutil.which",
            return_value="unrelated-pi",
        ),
        patch(
            "my_claude_code.cli.launchers.pi.pi_binary_is_compatible",
            return_value=False,
        ),
        patch("my_claude_code.cli.launchers.pi.get_settings") as get_settings,
        patch("my_claude_code.cli.launchers.common.subprocess.Popen") as popen,
        pytest.raises(SystemExit) as exc_info,
    ):
        launch([])

    assert exc_info.value.code == 126
    get_settings.assert_not_called()
    popen.assert_not_called()
    captured = capsys.readouterr()
    assert "not a compatible Pi Coding Agent" in captured.err
    assert "https://pi.dev/install." in captured.err


def test_launch_claude_keyboard_interrupt_kills_child_tree() -> None:
    from my_claude_code.cli.launchers.claude import launch

    settings = _launcher_settings(port=9191, token="proxy-token")

    with (
        patch(
            "my_claude_code.cli.launchers.claude.get_settings", return_value=settings
        ),
        patch("my_claude_code.cli.launchers.claude.preflight_proxy", return_value=None),
        patch(
            "my_claude_code.cli.launchers.common.shutil.which",
            return_value="resolved-claude.cmd",
        ),
        patch("my_claude_code.cli.launchers.common.subprocess.Popen") as popen,
        patch("my_claude_code.cli.launchers.common.register_pid"),
        patch(
            "my_claude_code.cli.launchers.common.kill_pid_tree_best_effort"
        ) as kill_tree,
        patch("my_claude_code.cli.launchers.common.unregister_pid") as unregister_pid,
        pytest.raises(KeyboardInterrupt),
    ):
        process = popen.return_value
        process.pid = 12345
        process.wait.side_effect = [KeyboardInterrupt, 0]

        launch([])

    kill_tree.assert_called_once_with(12345)
    unregister_pid.assert_called_once_with(12345)


def test_launch_claude_exits_when_command_cannot_be_resolved(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from my_claude_code.cli.launchers.claude import launch

    settings = _launcher_settings()
    with (
        patch(
            "my_claude_code.cli.launchers.claude.get_settings", return_value=settings
        ),
        patch("my_claude_code.cli.launchers.claude.preflight_proxy", return_value=None),
        patch("my_claude_code.cli.launchers.common.shutil.which", return_value=None),
        patch("my_claude_code.cli.launchers.common.subprocess.Popen") as popen,
        pytest.raises(SystemExit) as exc_info,
    ):
        launch([])

    assert exc_info.value.code == 127
    popen.assert_not_called()
    captured = capsys.readouterr()
    assert "Could not find Claude Code command: claude" in captured.err
    assert "npm install -g @anthropic-ai/claude-code" in captured.err


def test_launch_claude_unreachable_proxy_exits_with_hint(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from my_claude_code.cli.launchers.claude import launch

    settings = _launcher_settings(port=9393)
    with (
        patch(
            "my_claude_code.cli.launchers.claude.get_settings", return_value=settings
        ),
        patch(
            "my_claude_code.cli.launchers.claude.preflight_proxy",
            return_value="connection refused",
        ),
        patch("my_claude_code.cli.launchers.common.subprocess.Popen") as popen,
        pytest.raises(SystemExit) as exc_info,
    ):
        launch([])

    assert exc_info.value.code == 1
    popen.assert_not_called()
    captured = capsys.readouterr()
    assert "http://127.0.0.1:9393" in captured.err
    assert "fcc-server" in captured.err


def test_compact_log_entrypoint_is_registered_and_reports_version() -> None:
    """The command has to exist under the name the docs tell people to run."""
    import tomllib

    from my_claude_code.cli import entrypoints

    root = Path(__file__).resolve().parents[2]
    manifest = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = manifest["project"]["scripts"]
    assert scripts["fcc-compact-log"] == ("my_claude_code.cli.entrypoints:compact_log")
    assert callable(entrypoints.compact_log)


def test_compact_log_refuses_when_there_is_no_log(tmp_path, monkeypatch, capsys):
    from my_claude_code.cli import commands

    monkeypatch.setattr(
        "my_claude_code.core.request_log.default_request_log_path",
        lambda: tmp_path / "absent.db",
    )
    with pytest.raises(SystemExit) as exit_info:
        commands.compact_log()
    assert exit_info.value.code == 1
    assert "No request log" in capsys.readouterr().err


def _fake_config(app, **kwargs):
    return SimpleNamespace(app=app, kwargs=kwargs)


def test_graceful_shutdown_budget_comes_from_settings() -> None:
    """The supervisor hands the configured (not hard-coded) budget to uvicorn."""
    from my_claude_code.cli import commands

    settings = Settings.model_construct(
        host="0.0.0.0",
        port=8082,
        anthropic_auth_token="freecc",
        model="nvidia_nim/test-model",
        open_admin_browser=False,
        server_graceful_shutdown_seconds=300,
    )
    get_settings = MagicMock(return_value=settings)
    get_settings.cache_clear = MagicMock()
    captured: dict = {}

    def build_asgi_app(_settings, restart_callback, process_restart_callback):
        return SimpleNamespace(runtime=SimpleNamespace(is_closed=True))

    class FakeServer:
        def __init__(self, config):
            self.config = config
            self.should_exit = False

        def run(self):
            self.config.app.runtime.is_closed = True

    with (
        patch.object(commands, "get_settings", get_settings),
        patch.object(
            commands.uvicorn,
            "Config",
            side_effect=lambda app, **kw: (
                captured.update(kw) or _fake_config(app, **kw)
            ),
        ),
        patch.object(commands.uvicorn, "Server", side_effect=FakeServer),
        patch.object(commands, "build_asgi_app", side_effect=build_asgi_app),
        patch.object(commands, "_schedule_open_admin_browser"),
        patch.object(commands, "kill_all_best_effort"),
        patch.object(commands, "probe_port_available", return_value=True),
        patch.object(commands, "wait_for_port_free", return_value=True),
    ):
        commands.serve()

    assert captured["timeout_graceful_shutdown"] == 300


def test_graceful_shutdown_budget_tracks_a_configured_override() -> None:
    """A non-default configured value reaches uvicorn, not the old constant."""
    from my_claude_code.cli import commands

    settings = Settings.model_construct(
        host="0.0.0.0",
        port=8082,
        anthropic_auth_token="freecc",
        model="nvidia_nim/test-model",
        open_admin_browser=False,
        server_graceful_shutdown_seconds=45,
    )
    get_settings = MagicMock(return_value=settings)
    get_settings.cache_clear = MagicMock()
    captured: dict = {}

    def build_asgi_app(_settings, restart_callback, process_restart_callback):
        return SimpleNamespace(runtime=SimpleNamespace(is_closed=True))

    class FakeServer:
        def __init__(self, config):
            self.config = config
            self.should_exit = False

        def run(self):
            self.config.app.runtime.is_closed = True

    with (
        patch.object(commands, "get_settings", get_settings),
        patch.object(
            commands.uvicorn,
            "Config",
            side_effect=lambda app, **kw: (
                captured.update(kw) or _fake_config(app, **kw)
            ),
        ),
        patch.object(commands.uvicorn, "Server", side_effect=FakeServer),
        patch.object(commands, "build_asgi_app", side_effect=build_asgi_app),
        patch.object(commands, "_schedule_open_admin_browser"),
        patch.object(commands, "kill_all_best_effort"),
        patch.object(commands, "probe_port_available", return_value=True),
        patch.object(commands, "wait_for_port_free", return_value=True),
    ):
        commands.serve()

    assert captured["timeout_graceful_shutdown"] == 45


def test_process_replace_is_not_downgraded_by_a_later_reload() -> None:
    """A REPLACE_PROCESS in flight must outrank a config-driven RELOAD."""
    from my_claude_code.cli import commands

    settings = _launcher_settings()
    get_settings = MagicMock(return_value=settings)
    get_settings.cache_clear = MagicMock()
    process_callbacks: list[Callable[[], None]] = []
    restart_callbacks: list[Callable[[], None]] = []

    def build_asgi_app(
        _settings: Settings,
        restart_callback: Callable[[], None],
        process_restart_callback: Callable[[], None],
    ):
        process_callbacks.append(process_restart_callback)
        restart_callbacks.append(restart_callback)
        return SimpleNamespace(runtime=SimpleNamespace(is_closed=False))

    class FakeServer:
        def __init__(self, config):
            self.config = config
            self.should_exit = False

        def run(self):
            # Self-update requests a process replace; then a config change
            # arrives and requests a reload before the runtime fully closes.
            process_callbacks[-1]()
            restart_callbacks[-1]()
            assert self.should_exit is True
            self.config.app.runtime.is_closed = True

    with (
        patch.object(commands, "get_settings", get_settings),
        patch.object(commands.uvicorn, "Config", side_effect=_fake_config),
        patch.object(commands.uvicorn, "Server", side_effect=FakeServer),
        patch.object(commands, "build_asgi_app", side_effect=build_asgi_app),
        patch.object(commands, "_schedule_open_admin_browser"),
        patch.object(commands, "_replace_server_process") as replace_process,
        patch.object(commands, "kill_all_best_effort") as kill_all,
        patch.object(commands, "probe_port_available", return_value=True),
        patch.object(commands, "wait_for_port_free", return_value=True),
    ):
        commands.serve()

    replace_process.assert_called_once()
    kill_all.assert_called_once()


def test_process_replace_is_refused_when_runtime_did_not_close() -> None:
    """An incomplete close must not silently become a plain stop."""
    from my_claude_code.cli import commands

    settings = _launcher_settings()
    get_settings = MagicMock(return_value=settings)
    get_settings.cache_clear = MagicMock()
    process_callbacks: list[Callable[[], None]] = []

    def build_asgi_app(
        _settings: Settings,
        restart_callback: Callable[[], None],
        process_restart_callback: Callable[[], None],
    ):
        process_callbacks.append(process_restart_callback)
        return SimpleNamespace(runtime=SimpleNamespace(is_closed=False))

    class FakeServer:
        def __init__(self, config):
            self.config = config
            self.should_exit = False

        def run(self):
            process_callbacks[-1]()
            assert self.should_exit is True
            # The runtime never reaches is_closed: a real in-flight drain.

    errors: list[str] = []

    # The runtime never closes, so the supervisor must refuse the process
    # replacement and surface a clear error rather than silently stopping.
    with (
        pytest.raises(SystemExit),
        patch.object(commands, "get_settings", get_settings),
        patch.object(commands.uvicorn, "Config", side_effect=_fake_config),
        patch.object(commands.uvicorn, "Server", side_effect=FakeServer),
        patch.object(commands, "build_asgi_app", side_effect=build_asgi_app),
        patch.object(commands, "_schedule_open_admin_browser"),
        patch.object(commands, "_replace_server_process") as replace_process,
        patch.object(commands, "kill_all_best_effort") as kill_all,
        patch.object(commands, "probe_port_available", return_value=True),
        patch.object(commands, "wait_for_port_free", return_value=True),
        patch.object(
            commands.logger,
            "error",
            side_effect=lambda *a, **k: errors.append(str(a[0])),
        ),
    ):
        commands.serve()

    replace_process.assert_not_called()
    kill_all.assert_called_once()
    assert any("refused" in message for message in errors)


def test_config_reload_not_degraded_to_stop_when_runtime_still_closing() -> None:
    """A config-driven RELOAD must not become a process exit when the runtime
    is still draining (the 'server crashed after I applied a setting' bug)."""
    from my_claude_code.cli import commands

    settings = _launcher_settings()
    get_settings = MagicMock(return_value=settings)
    get_settings.cache_clear = MagicMock()
    restart_callbacks: list[Callable[[], None]] = []
    runs = 0

    def build_asgi_app(
        _settings: Settings,
        restart_callback: Callable[[], None],
        process_restart_callback: Callable[[], None],
    ):
        restart_callbacks.append(restart_callback)
        # The runtime never reports closed: a real in-flight drain (e.g. the
        # request-log writer flushing a large DB during a config apply).
        return SimpleNamespace(runtime=SimpleNamespace(is_closed=False))

    class FakeServer:
        def __init__(self, config):
            self.config = config
            self.should_exit = False

        def run(self):
            nonlocal runs
            runs += 1
            if runs == 1:
                # First generation: a config change requests a reload.
                restart_callbacks[-1]()
                assert self.should_exit is True

    with (
        patch.object(commands, "get_settings", get_settings),
        patch.object(commands.uvicorn, "Config", side_effect=_fake_config),
        patch.object(commands.uvicorn, "Server", side_effect=FakeServer),
        patch.object(commands, "build_asgi_app", side_effect=build_asgi_app),
        patch.object(commands, "_schedule_open_admin_browser"),
        patch.object(commands, "kill_all_best_effort") as kill_all,
        patch.object(commands, "probe_port_available", return_value=True),
        patch.object(commands, "wait_for_port_free", return_value=True),
    ):
        commands.serve()

    # The reload must cause the serve loop to run a second generation (the
    # fresh app), not exit the process. Before the fix it returned STOP and
    # killed the server (kill_all via serve's finally).
    assert runs == 2
    assert len(restart_callbacks) == 2
    kill_all.assert_called_once()


def test_process_replacement_logs_recovery_command_when_execv_fails() -> None:
    """When execv cannot launch the new image, the recovery command is logged."""
    from my_claude_code.cli import commands

    with (
        patch.object(commands, "_WINDOWS", False),
        patch.object(commands, "external_upgrade_helper_pending", return_value=False),
        patch.object(commands, "_server_launcher", return_value="/stable/fcc-server"),
        patch.object(commands.logger, "complete"),
        patch.object(commands, "kill_all_best_effort"),
        patch.object(commands, "wait_for_port_free", return_value=True),
        patch.object(
            commands.os, "execv", side_effect=OSError(13, "Permission denied")
        ),
        patch.object(commands.sys, "argv", ["fcc-server", "--example"]),
        patch.object(commands.logger, "error") as error,
    ):
        commands._replace_server_process(_launcher_settings())

    error.assert_called_once()
    recovery = " ".join(str(arg) for arg in error.call_args.args)
    assert "/stable/fcc-server --example" in recovery


def test_bind_failure_surfaces_the_port_owner() -> None:
    """uvicorn's SystemExit(1) on a held port is diagnosed, not a bare crash."""
    from my_claude_code.cli import commands
    from my_claude_code.cli.port_diagnostics import PortOwner

    settings = _launcher_settings(port=8099)
    get_settings = MagicMock(return_value=settings)
    get_settings.cache_clear = MagicMock()

    def build_asgi_app(
        _settings: Settings,
        restart_callback: Callable[[], None],
        process_restart_callback: Callable[[], None],
    ):
        return SimpleNamespace(runtime=SimpleNamespace(is_closed=True))

    class FakeServer:
        def __init__(self, config):
            self.config = config
            self.should_exit = False

        def run(self):
            raise SystemExit(1)

    errors: list = []

    # The port is genuinely unavailable here: the probe is forced False and the
    # waiter gives up, so the failure is diagnosed as a held port with an owner.
    with (
        pytest.raises(SystemExit),
        patch.object(commands, "get_settings", get_settings),
        patch.object(commands.uvicorn, "Config", side_effect=_fake_config),
        patch.object(commands.uvicorn, "Server", side_effect=FakeServer),
        patch.object(commands, "build_asgi_app", side_effect=build_asgi_app),
        patch.object(commands, "_schedule_open_admin_browser"),
        patch.object(commands, "probe_port_available", return_value=False),
        patch.object(commands, "wait_for_port_free", return_value=False),
        patch.object(
            commands,
            "diagnose_port_owner",
            return_value=PortOwner(pid=4242, name="intruder", command=None),
        ),
        patch.object(commands, "kill_all_best_effort"),
        patch.object(
            commands.logger, "error", side_effect=lambda *a, **k: errors.append((a, k))
        ),
    ):
        commands.serve()

    # The owner PID is reported as a structured field, not buried in text.
    assert any(kw.get("pid") == 4242 for _args, kw in errors)
    assert any("Cannot bind" in str(args[0]) for args, _kw in errors)


# -------------------------------------------------------- harness attribution


def test_claude_custom_headers_keep_what_the_user_set() -> None:
    """The variable is the user's; MCC owns one line in it, not the file.

    ``ANTHROPIC_CUSTOM_HEADERS`` is a documented Claude Code variable a user
    may already be using for a corporate gateway token. Overwriting it to gain
    a diagnostic label would break their session; appending costs nothing, and
    the later line wins on a duplicate name.
    """
    from my_claude_code.cli.claude_env import build_minimal_claude_proxy_env

    env = build_minimal_claude_proxy_env(
        proxy_root_url="http://127.0.0.1:9090",
        auth_token="proxy-token",
        base_env={"ANTHROPIC_CUSTOM_HEADERS": "X-Corp-Tenant: acme"},
    )

    assert env["ANTHROPIC_CUSTOM_HEADERS"] == (
        f"X-Corp-Tenant: acme\n{HARNESS_HEADER}: claude"
    )


def test_claude_custom_headers_are_not_appended_twice() -> None:
    """A launch inside a session MCC already configured adds no second line."""
    from my_claude_code.cli.claude_env import build_minimal_claude_proxy_env

    already = f"{HARNESS_HEADER}: claude"

    env = build_minimal_claude_proxy_env(
        proxy_root_url="http://127.0.0.1:9090",
        auth_token="proxy-token",
        base_env={"ANTHROPIC_CUSTOM_HEADERS": already},
    )

    assert env["ANTHROPIC_CUSTOM_HEADERS"] == already


def test_the_full_claude_env_declares_the_harness_too() -> None:
    """Both builders, not just the minimal one: both start the same binary."""
    from my_claude_code.cli.claude_env import build_claude_proxy_env

    env = build_claude_proxy_env(
        proxy_root_url="http://127.0.0.1:9090",
        auth_token="proxy-token",
        base_env={"PATH": "keep"},
    )

    assert env["ANTHROPIC_CUSTOM_HEADERS"] == f"{HARNESS_HEADER}: claude"


def test_the_pi_extension_declares_the_same_header_and_id_python_does() -> None:
    """The one constant this repo cannot share, checked instead of trusted.

    ``pi_extension.ts`` is TypeScript and the definitions are Python, so the
    header name and the harness id are restated in it. This test is what stops
    the two copies drifting: rename either side and it fails here rather than
    silently attributing every Pi session to nothing.
    """
    from my_claude_code.cli.launchers.pi import pi_extension_path

    source = pi_extension_path().read_text(encoding="utf-8")

    assert f'const HARNESS_HEADER = "{HARNESS_HEADER}";' in source
    assert f'const HARNESS_ID = "{harness_spec("pi").id}";' in source
    # And it is actually handed to Pi, not merely declared.
    assert "headers: { [HARNESS_HEADER]: HARNESS_ID }," in source
    # No version companion: MCC never probes a harness for its version.
    assert "x-mcc-harness-version" not in source
