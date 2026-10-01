"""The supervisor's half of a lost listener: stop like any stop, then exit 75.

7.69.2. The listener guard (``runtime/listener_guard.py``) finds the socket
closed and calls back; the supervisor turns that into the ordinary terminal
stop -- the shared stop deadline, new requests refused, the hard-exit watchdog
armed -- and, once the generation has drained, ends the process with
``LISTENER_LOST_EXIT_CODE`` rather than 0, so whoever started it knows to start
another. These run on every platform; the drain itself, over a real socket on a
real proactor loop, is ``tests/runtime/test_listener_lost_drain.py``.
"""

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from my_claude_code.cli import commands
from my_claude_code.config.constants import LISTENER_LOST_EXIT_CODE
from my_claude_code.config.settings import Settings
from my_claude_code.core.request_log import server_bind_address
from my_claude_code.core.stop_deadline import HARD_EXIT_STATUS, stop_deadline


def _settings() -> Settings:
    return Settings.model_construct(
        host="127.0.0.1",
        port=8082,
        anthropic_auth_token="freecc",
        model="nvidia_nim/test-model",
        open_admin_browser=False,
        server_graceful_shutdown_seconds=3.0,
        server_port_takeover="never",
    )


def _supervise(run: Callable[[Any, dict[str, Any]], None]) -> dict[str, Any]:
    """One supervised generation; ``run`` plays uvicorn's ``Server.run``."""

    settings = _settings()
    get_settings = MagicMock(return_value=settings)
    get_settings.cache_clear = MagicMock()
    seen: dict[str, Any] = {"armed": 0}
    bound = object()

    def build_asgi_app(_settings: Settings, **kwargs: Any) -> SimpleNamespace:
        seen["callback"] = kwargs["listener_lost_callback"]
        seen["socket"] = kwargs["listening_socket"]
        seen["socket_before_bind"] = kwargs["listening_socket"]()
        return SimpleNamespace(runtime=SimpleNamespace(is_closed=True))

    class FakeServer:
        def __init__(self, config: Any) -> None:
            self.config = config
            self.should_exit = False

        def run(self, sockets: Any = None) -> None:
            seen["sockets"] = sockets
            run(self, seen)

    def count_arm(_self: Any, **_kwargs: Any) -> None:
        seen["armed"] += 1

    with (
        patch.object(type(stop_deadline()), "arm_hard_exit", count_arm),
        patch.object(commands, "get_settings", get_settings),
        patch.object(
            commands.uvicorn,
            "Config",
            side_effect=lambda app, **kw: SimpleNamespace(
                app=app, kwargs=kw, bind_socket=lambda: None
            ),
        ),
        patch.object(commands.uvicorn, "Server", side_effect=FakeServer),
        patch.object(commands, "_bind_listening_socket", return_value=bound),
        patch.object(commands, "build_asgi_app", side_effect=build_asgi_app),
        patch.object(commands, "_schedule_open_admin_browser"),
        patch.object(commands, "_survey_other_servers"),
        patch.object(commands, "kill_all_best_effort"),
        patch.object(commands, "probe_port_available", return_value=True),
        patch.object(commands, "wait_for_port_free", return_value=True),
    ):
        try:
            seen["result"] = commands._run_supervised_server(
                settings, open_admin_browser=False
            )
        except SystemExit as exc:
            seen["exit"] = exc.code
    seen["bound"] = bound
    return seen


def test_a_lost_listener_drains_like_any_stop_and_exits_75() -> None:
    def run(server: Any, seen: dict[str, Any]) -> None:
        # The guard sees the socket the supervisor bound...
        seen["socket_during_run"] = seen["socket"]()
        # ...finds it closed, and calls back from the loop.
        seen["callback"]()
        seen["should_exit"] = server.should_exit
        seen["requested"] = stop_deadline().requested
        seen["budget"] = stop_deadline().budget

    seen = _supervise(run)

    assert seen["socket_before_bind"] is None
    assert seen["socket_during_run"] is seen["bound"]
    assert seen["sockets"] == [seen["bound"]]
    assert seen["should_exit"] is True
    assert seen["requested"] is True
    assert seen["budget"] == 3.0
    # A terminal stop: the watchdog that guarantees the process leaves is armed.
    assert seen["armed"] == 1
    assert seen["exit"] == LISTENER_LOST_EXIT_CODE == 75
    assert "result" not in seen
    # Nothing is left claiming the port or the stop for a next generation.
    assert server_bind_address() is None
    assert stop_deadline().requested is False


def test_an_ordinary_stop_still_returns_stop_and_exits_zero() -> None:
    def run(server: Any, seen: dict[str, Any]) -> None:
        server.should_exit = True

    seen = _supervise(run)

    assert "exit" not in seen
    assert seen["result"] is commands.ServerExitAction.STOP


def test_the_exit_code_is_distinct_from_every_other_exit() -> None:
    """0 a clean stop; 1 refused to start or failed to bind; 3 the hard exit."""

    assert LISTENER_LOST_EXIT_CODE not in {0, 1, HARD_EXIT_STATUS}
