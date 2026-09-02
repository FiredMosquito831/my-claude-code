"""The server command applies the open-proxy refusal before it binds a socket."""

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
    ):
        action = commands._run_supervised_server(
            _settings(host="0.0.0.0", token="freecc"), open_admin_browser=False
        )

    assert server.run.called
    assert action is commands.ServerExitAction.STOP
