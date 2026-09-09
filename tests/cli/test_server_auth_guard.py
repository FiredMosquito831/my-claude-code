"""The server command applies the open-proxy refusal before it binds a socket."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from my_claude_code.cli import commands
from my_claude_code.config.settings import Settings


def _settings(*, host: str, token: str) -> Settings:
    return Settings().model_copy(update={"host": host, "anthropic_auth_token": token})


def test_a_reachable_bind_without_a_token_exits_before_building_the_app(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with (
        patch.object(commands, "build_asgi_app") as build_asgi_app,
        patch.object(commands, "probe_port_available") as probe,
        pytest.raises(SystemExit) as excinfo,
    ):
        commands._run_supervised_server(
            _settings(host="0.0.0.0", token=""), open_admin_browser=False
        )

    assert excinfo.value.code == 1
    # Nothing was constructed and no port was touched: the refusal is the
    # first thing that happens, not a late abort after a half-built runtime.
    assert not build_asgi_app.called
    assert not probe.called
    message = capsys.readouterr().err
    assert "ANTHROPIC_AUTH_TOKEN" in message
    assert "HOST" in message


def test_a_loopback_bind_without_a_token_still_starts() -> None:
    server = MagicMock()
    with (
        patch.object(commands, "build_asgi_app"),
        patch.object(commands, "probe_port_available", return_value=True),
        patch.object(commands.uvicorn, "Server", return_value=server),
        patch.object(commands.uvicorn, "Config"),
        # The supervisor binds its own listening socket since 6.59.0; a unit
        # test of the auth guard must not open one.
        patch.object(commands, "_bind_listening_socket", return_value=None),
    ):
        action = commands._run_supervised_server(
            _settings(host="127.0.0.1", token=""), open_admin_browser=False
        )

    assert server.run.called
    assert action is commands.ServerExitAction.STOP


def test_a_reachable_bind_with_a_token_still_starts() -> None:
    server = MagicMock()
    with (
        patch.object(commands, "build_asgi_app"),
        patch.object(commands, "probe_port_available", return_value=True),
        patch.object(commands.uvicorn, "Server", return_value=server),
        patch.object(commands.uvicorn, "Config"),
        # The supervisor binds its own listening socket since 6.59.0; a unit
        # test of the auth guard must not open one.
        patch.object(commands, "_bind_listening_socket", return_value=None),
    ):
        action = commands._run_supervised_server(
            _settings(host="0.0.0.0", token="freecc"), open_admin_browser=False
        )

    assert server.run.called
    assert action is commands.ServerExitAction.STOP


def test_the_refusal_reaches_the_server_log(tmp_path: Path, monkeypatch) -> None:
    """A refused start must leave the reason in a file, not only on a console.

    Until 6.65.0 the file sink was configured inside ``build_asgi_app``, which
    runs *after* this guard -- so the one start a user most needs an explanation
    for produced an empty config directory, and the desktop app's error page
    named a ``server_log`` that did not exist.
    """
    log_path = tmp_path / "logs" / "server.log"
    monkeypatch.setenv("LOG_FILE", str(log_path))

    with (
        patch.object(commands, "build_asgi_app") as build_asgi_app,
        pytest.raises(SystemExit),
    ):
        commands._run_supervised_server(
            _settings(host="0.0.0.0", token=""), open_admin_browser=False
        )

    assert not build_asgi_app.called
    assert log_path.is_file()
    written = log_path.read_text(encoding="utf-8", errors="replace")
    assert "Refusing to start" in written
    assert "ANTHROPIC_AUTH_TOKEN" in written
