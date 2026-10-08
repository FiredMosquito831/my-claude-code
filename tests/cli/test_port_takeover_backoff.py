"""The takeover table, row by row, through the real supervisor (7.70.0).

User decision R5: a start that finds a live My Claude Code server ANSWERING on
its port backs off with a clear message instead of killing it; the kill is
kept only for a holder that does not answer. ``SERVER_PORT_TAKEOVER=always``
now means "replace a holder that is not answering"; ``mcc-only`` keeps its
"only My Claude Code's own processes" limit with the same back-off; ``never``
is untouched and never even asks.

Every row drives ``cli.commands._run_supervised_server`` -- the code every
start runs (a hand start, the desktop host's spawn, the installer's restart) --
with the operating system faked: what the port answers, whether it is
bindable, and who the process lookup says holds it. The "kills nothing" rows
replace the kill function itself with one that fails the test if it is called.
"""

from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from my_claude_code.cli import commands, port_takeover
from my_claude_code.cli.port_diagnostics import PortOwner
from my_claude_code.cli.port_takeover import ProcessIdentity
from my_claude_code.config.constants import PORT_ALREADY_SERVED_EXIT_CODE
from my_claude_code.config.settings import Settings
from my_claude_code.core import mcc_processes, port_holder_answer
from my_claude_code.core.loop_health import BUSY_MARKER_HEADER, BUSY_MARKER_VALUE
from my_claude_code.core.server_pid import SERVER_PID_HEADER
from my_claude_code.core.startup_state import (
    STARTING_MARKER_HEADER,
    STARTING_MARKER_VALUE,
)
from my_claude_code.core.stop_deadline import (
    SHUTDOWN_MARKER_HEADER,
    SHUTDOWN_MARKER_VALUE,
)

PORT = 18999
HOLDER_PID = 4242

HEALTHY = (200, {SERVER_PID_HEADER: str(HOLDER_PID)}, b'{"status":"healthy"}')
OLD_HEALTHY = (200, {"content-type": "application/json"}, b'{"status":"healthy"}')
BUSY = (
    200,
    {BUSY_MARKER_HEADER: BUSY_MARKER_VALUE, SERVER_PID_HEADER: str(HOLDER_PID)},
    b'{"status":"healthy","busy":true}',
)
STARTING = (
    503,
    {STARTING_MARKER_HEADER: STARTING_MARKER_VALUE, SERVER_PID_HEADER: str(HOLDER_PID)},
    b'{"status":"starting"}',
)
DRAINING = (
    503,
    {SHUTDOWN_MARKER_HEADER: SHUTDOWN_MARKER_VALUE, SERVER_PID_HEADER: str(HOLDER_PID)},
    b"{}",
)
STRANGER_PAGE = (200, {"content-type": "text/html"}, b"<html>not us</html>")
SILENT = None

MCC = ProcessIdentity(
    pid=HOLDER_PID, image="python.exe", command="python -m mcc_server"
)
STRANGER = ProcessIdentity(pid=99, image="nginx.exe", command="nginx")


@dataclass
class World:
    """The faked operating system one start runs against."""

    replies: list
    identity: ProcessIdentity | None
    frees_on_drain: bool = False
    frees_after_kill: bool = True
    #: The holder exits while the first try is waiting for its answer.
    leaves_when_asked: bool = False
    free: bool = False
    asked: int = 0
    draining_seen: bool = False
    killed: list[int] = field(default_factory=list)
    bound: bool = False
    #: The port comes free inside a wait of the whole bind budget (7.71.0).
    frees_on_long_wait: bool = False
    #: Every ``wait_for_port_free`` timeout the start used.
    waits: list[float] = field(default_factory=list)


def _settings(policy: str) -> Settings:
    return Settings.model_construct(
        host="127.0.0.1",
        port=PORT,
        anthropic_auth_token="test-token",
        open_admin_browser=False,
        server_port_takeover=policy,
        server_graceful_shutdown_seconds=20.0,
        desktop_health_probe_timeouts="5,10,15",
        desktop_health_failure_threshold=3,
        desktop_health_probe_timeout=1.5,
    )


def _never_kill(pid: int) -> bool:
    pytest.fail(
        f"the kill function was called for pid {pid}; this row must kill nothing"
    )


def _start(monkeypatch, world: World, policy: str, *, may_kill: bool):
    """Run one supervised start against ``world``; return its exit action."""

    def ask(_root_url: str, _timeout: float):
        world.asked += 1
        reply = world.replies.pop(0) if world.replies else SILENT
        if reply is DRAINING:
            world.draining_seen = True
        if world.leaves_when_asked:
            world.free = True
        return reply

    def wait_for_port_free(_host, _port, *, timeout=5.0, interval=0.2):
        del interval
        world.waits.append(timeout)
        if world.draining_seen and world.frees_on_drain and timeout >= 20.0:
            world.free = True
        if world.frees_on_long_wait and timeout >= 20.0:
            world.free = True
        return world.free

    def kill(pid: int) -> bool:
        world.killed.append(pid)
        world.free = world.frees_after_kill
        return True

    def bind(_settings):
        world.bound = True

    monkeypatch.setattr(port_holder_answer, "ask_health", ask)
    monkeypatch.setattr(commands, "probe_port_available", lambda *a, **k: world.free)
    monkeypatch.setattr(commands, "wait_for_port_free", wait_for_port_free)
    monkeypatch.setattr(
        commands,
        "diagnose_port_owner",
        lambda *a, **k: PortOwner(pid=HOLDER_PID, name="python.exe", command=None),
    )
    monkeypatch.setattr(
        port_takeover, "identify_port_holder", lambda *a, **k: world.identity
    )
    monkeypatch.setattr(port_takeover, "_kill", kill if may_kill else _never_kill)
    monkeypatch.setattr(port_takeover, "wait_for_port_free", lambda *a, **k: world.free)
    # Belt and braces: the escalation the kill uses must not run either.
    monkeypatch.setattr(port_takeover, "stop_process", _never_kill)
    monkeypatch.setattr(mcc_processes, "stop_process", _never_kill)

    monkeypatch.setattr(
        commands,
        "build_asgi_app",
        lambda *_a, **_k: SimpleNamespace(runtime=SimpleNamespace(is_closed=True)),
    )
    monkeypatch.setattr(
        commands.uvicorn,
        "Config",
        lambda app, **kw: SimpleNamespace(app=app, kwargs=kw),
    )
    server = MagicMock()
    server.run = MagicMock(return_value=None)
    monkeypatch.setattr(commands.uvicorn, "Server", lambda _config: server)
    monkeypatch.setattr(commands, "_bind_listening_socket", bind)
    monkeypatch.setattr(commands, "_schedule_open_admin_browser", lambda _s: None)
    monkeypatch.setattr(commands, "_survey_other_servers", lambda _s: None)
    monkeypatch.setattr(commands, "set_server_bind_address", lambda *_a: None)
    return commands._run_supervised_server(_settings(policy), open_admin_browser=False)


# ----------------------------------------------------------- the back-off rows


@pytest.mark.parametrize("policy", ["always", "mcc-only"])
@pytest.mark.parametrize(
    "reply",
    [HEALTHY, OLD_HEALTHY, BUSY, STARTING],
    ids=["healthy", "healthy-7.69", "busy", "starting"],
)
def test_an_answering_mcc_holder_is_never_killed_and_the_start_exits(
    monkeypatch, capsys, policy, reply
) -> None:
    world = World(replies=[reply], identity=MCC)

    with pytest.raises(SystemExit) as exited:
        _start(monkeypatch, world, policy, may_kill=False)

    assert exited.value.code == PORT_ALREADY_SERVED_EXIT_CODE == 1
    assert world.killed == []
    assert world.bound is False
    assert world.asked == 1
    lines = [line for line in capsys.readouterr().err.splitlines() if line.strip()]
    assert len(lines) == 1, lines
    assert (
        f"Port {PORT} is already served by My Claude Code (pid {HOLDER_PID}" in lines[0]
    )
    assert "nothing was stopped" in lines[0]
    assert lines[0].startswith("[")


def test_the_back_off_is_logged_as_a_warning(monkeypatch, capsys) -> None:
    from loguru import logger

    records: list[str] = []
    sink = logger.add(lambda message: records.append(str(message)), level="WARNING")
    try:
        with pytest.raises(SystemExit):
            _start(
                monkeypatch,
                World(replies=[HEALTHY], identity=MCC),
                "always",
                may_kill=False,
            )
    finally:
        logger.remove(sink)
    capsys.readouterr()
    assert any("already served by My Claude Code" in record for record in records)


# ------------------------------------------------------------- the drain rows


def test_a_draining_holder_is_waited_out_and_never_killed(monkeypatch, capsys) -> None:
    world = World(replies=[DRAINING], identity=MCC, frees_on_drain=True)

    action = _start(monkeypatch, world, "always", may_kill=False)

    assert action is commands.ServerExitAction.STOP
    assert world.bound is True
    assert world.killed == []
    assert world.asked == 1
    err = capsys.readouterr().err
    assert "shutting down. Waiting up to 24 s" in err


def test_a_draining_holder_that_goes_silent_past_its_budget_is_handled_as_today(
    monkeypatch, capsys
) -> None:
    world = World(replies=[DRAINING, SILENT, SILENT, SILENT], identity=MCC)

    _start(monkeypatch, world, "always", may_kill=True)

    assert world.killed == [HOLDER_PID]
    assert world.bound is True
    capsys.readouterr()


def test_a_draining_holder_still_answering_after_its_budget_is_backed_off_from(
    monkeypatch, capsys
) -> None:
    world = World(replies=[DRAINING, HEALTHY], identity=MCC)

    with pytest.raises(SystemExit) as exited:
        _start(monkeypatch, world, "always", may_kill=False)

    assert exited.value.code == 1
    assert world.killed == []
    capsys.readouterr()


# ------------------------------------------------- silent holders: unchanged rows


@pytest.mark.parametrize(
    ("policy", "identity", "killed"),
    [
        ("always", MCC, [HOLDER_PID]),
        ("always", STRANGER, [99]),
        ("mcc-only", MCC, [HOLDER_PID]),
    ],
    ids=["always-mcc", "always-stranger", "mcc-only-mcc"],
)
def test_a_silent_holder_is_replaced_exactly_as_before(
    monkeypatch, capsys, policy, identity, killed
) -> None:
    world = World(replies=[SILENT, SILENT, SILENT], identity=identity)

    action = _start(monkeypatch, world, policy, may_kill=True)

    assert action is commands.ServerExitAction.STOP
    assert world.killed == killed
    assert world.bound is True
    # The whole ladder was asked first: three tries, no answer.
    assert world.asked == 3
    capsys.readouterr()


def test_a_silent_stranger_under_mcc_only_is_refused_and_left_alone(
    monkeypatch, capsys
) -> None:
    world = World(replies=[SILENT, SILENT, SILENT], identity=STRANGER)

    with pytest.raises(SystemExit) as exited:
        _start(monkeypatch, world, "mcc-only", may_kill=False)

    assert exited.value.code == 1
    assert world.killed == []
    assert world.bound is False
    capsys.readouterr()


@pytest.mark.parametrize(
    ("policy", "killed"), [("always", [99])], ids=["always-stranger-page"]
)
def test_a_stranger_that_answers_is_handled_as_before(
    monkeypatch, capsys, policy, killed
) -> None:
    world = World(replies=[STRANGER_PAGE], identity=STRANGER)

    _start(monkeypatch, world, policy, may_kill=True)

    assert world.killed == killed
    assert world.asked == 1
    capsys.readouterr()


def test_a_stranger_that_answers_under_mcc_only_is_refused(monkeypatch, capsys) -> None:
    world = World(replies=[STRANGER_PAGE], identity=STRANGER)

    with pytest.raises(SystemExit) as exited:
        _start(monkeypatch, world, "mcc-only", may_kill=False)

    assert exited.value.code == 1
    assert world.killed == []
    capsys.readouterr()


# ------------------------------------------------------------------- never


@pytest.mark.parametrize(
    ("reply", "identity"),
    [(HEALTHY, MCC), (SILENT, MCC), (STRANGER_PAGE, STRANGER)],
    ids=["answering-mcc", "silent-mcc", "stranger"],
)
def test_never_is_untouched_it_asks_nothing_and_stops_nothing(
    monkeypatch, capsys, reply, identity
) -> None:
    world = World(replies=[reply], identity=identity)

    with pytest.raises(SystemExit) as exited:
        _start(monkeypatch, world, "never", may_kill=False)

    assert exited.value.code == 1
    assert world.killed == []
    assert world.asked == 0
    assert "already served" not in capsys.readouterr().err


# --------------------------------------------------------------- the free rows


def test_a_holder_that_leaves_while_asked_is_never_taken_from(
    monkeypatch, capsys
) -> None:
    world = World(replies=[SILENT], identity=MCC, leaves_when_asked=True)

    action = _start(monkeypatch, world, "always", may_kill=False)

    assert action is commands.ServerExitAction.STOP
    assert world.bound is True
    assert world.killed == []
    assert world.asked == 1
    capsys.readouterr()


# ------------------------------------------------- one row against a real holder


@pytest.mark.local_serial
def test_a_real_answering_holder_on_loopback_survives_a_start(
    monkeypatch, capsys
) -> None:
    """No fakes on the wire: a real HTTP holder, the real probe, the real exit."""

    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from my_claude_code.cli.launchers.common import preflight_result

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = b'{"status":"healthy"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header(SERVER_PID_HEADER, str(HOLDER_PID))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    holder = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    holder.daemon_threads = True
    thread = threading.Thread(target=holder.serve_forever, daemon=True)
    thread.start()
    port = int(holder.server_address[1])
    monkeypatch.setattr(port_takeover, "_kill", _never_kill)
    monkeypatch.setattr(port_takeover, "stop_process", _never_kill)
    monkeypatch.setattr(mcc_processes, "stop_process", _never_kill)
    settings = Settings.model_construct(
        host="127.0.0.1",
        port=port,
        server_port_takeover="always",
        server_graceful_shutdown_seconds=20.0,
        desktop_health_probe_timeouts="5,10,15",
        desktop_health_failure_threshold=3,
        desktop_health_probe_timeout=1.5,
    )
    try:
        with pytest.raises(SystemExit) as exited:
            commands._port_freed_after_asking_the_holder(settings)
        assert exited.value.code == 1
        # Still there, still answering.
        assert preflight_result(f"http://127.0.0.1:{port}").ok
    finally:
        holder.shutdown()
        holder.server_close()
        thread.join(timeout=5)
    assert f"(pid {HOLDER_PID}; it answered /health in" in capsys.readouterr().err


# ------------------------------------------- --no-port-takeover (7.71.0, desktop)


@pytest.mark.parametrize("policy", ["always", "mcc-only", "never"])
@pytest.mark.parametrize(
    ("reply", "identity"),
    [(HEALTHY, MCC), (SILENT, MCC), (STRANGER_PAGE, STRANGER), (SILENT, None)],
    ids=["answering-mcc", "silent-mcc", "stranger", "unidentified"],
)
def test_no_port_takeover_stops_nothing_and_asks_nothing_whatever_the_policy(
    monkeypatch, capsys, policy, reply, identity
) -> None:
    # Every server the desktop app starts carries the flag (rescue spec 2.3,
    # layer 3): even a race in which something grabs the port between the
    # rescue and the bind cannot make it stop anything.
    monkeypatch.setattr(commands, "_port_takeover_allowed", False)
    monkeypatch.setattr(
        port_takeover,
        "take_port",
        lambda *a, **k: pytest.fail("take_port ran under --no-port-takeover"),
    )
    monkeypatch.setattr(
        commands,
        "take_port",
        lambda *a, **k: pytest.fail("take_port ran under --no-port-takeover"),
    )
    world = World(replies=[reply], identity=identity)

    with pytest.raises(SystemExit) as exited:
        _start(monkeypatch, world, policy, may_kill=False)

    assert exited.value.code == 1
    assert world.killed == []
    assert world.asked == 0
    assert world.bound is False
    err = capsys.readouterr().err
    assert "--no-port-takeover" in err
    assert f"pid {HOLDER_PID}" in err


def test_no_port_takeover_binds_when_the_port_comes_free_in_the_wait(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(commands, "_port_takeover_allowed", False)
    world = World(replies=[], identity=MCC, frees_on_long_wait=True)

    action = _start(monkeypatch, world, "always", may_kill=False)

    assert action is commands.ServerExitAction.STOP
    assert world.bound is True
    assert world.killed == []
    assert world.asked == 0
    # The patient wait the bind budget allows, not the 2 s grace.
    assert world.waits == [20.0]
    capsys.readouterr()


def test_the_flag_reaches_the_supervisor_and_is_never_left_behind(monkeypatch) -> None:
    from my_claude_code.cli import entrypoints

    seen: list[bool] = []
    monkeypatch.setattr(entrypoints, "_bootstrap_config_paths", lambda: None)
    monkeypatch.setattr(
        commands, "_serve", lambda: seen.append(commands._takeover_allowed())
    )

    entrypoints.serve(["--no-port-takeover"])
    entrypoints.serve([])

    assert seen == [False, True]
    # ...and nothing of the first start outlives it.
    assert commands._takeover_allowed() is True
