"""The port takeover, and the classification it rests on.

The rule is the user's: when the server starts, whatever is holding its port
stops holding it. The subtlety is not the killing, it is the *identification* --
a starting MCC, a draining MCC and a wedged MCC all answer the port differently
and none of them answers "I am MCC", which is how the desktop window came to
tell the user that MCC's own python.exe "is not the MCC server".
"""

import pytest

from my_claude_code.cli import port_takeover
from my_claude_code.cli.port_takeover import (
    ProcessIdentity,
    TakeoverOutcome,
    take_port,
)
from my_claude_code.config.constants import (
    SERVER_PORT_TAKEOVER_CHOICES,
    SERVER_PORT_TAKEOVER_DEFAULT,
)
from my_claude_code.config.settings import Settings


def test_the_default_policy_is_to_take_the_port() -> None:
    assert SERVER_PORT_TAKEOVER_DEFAULT == "always"
    assert set(SERVER_PORT_TAKEOVER_CHOICES) == {"always", "mcc-only", "never"}


@pytest.mark.parametrize(
    ("image", "command"),
    [
        ("mcc-server.exe", None),
        ("MCC-Desktop.exe", None),
        ("my-claude-code.exe", None),
        ("fcc-server", None),
        ("python.exe", r"C:\Users\x\.local\share\uv\tools\my-claude-code\python.exe"),
        ("python.exe", "/usr/bin/python -m my_claude_code.cli.entrypoints"),
    ],
)
def test_an_mcc_holder_is_recognised_by_its_process(image, command) -> None:
    assert ProcessIdentity(pid=1234, image=image, command=command).is_mcc


@pytest.mark.parametrize(
    ("image", "command"),
    [
        ("node.exe", "node server.js"),
        ("python.exe", "python -m http.server 8082"),
        # "Cannot tell" is deliberately NOT "is MCC": ``mcc-only`` must never
        # kill something it merely failed to read.
        (None, None),
    ],
)
def test_a_stranger_is_not_mistaken_for_mcc(image, command) -> None:
    assert not ProcessIdentity(pid=1234, image=image, command=command).is_mcc


def test_never_stops_nothing_and_reports_the_holder(monkeypatch) -> None:
    holder = ProcessIdentity(pid=99, image="mcc-server.exe", command=None)
    killed: list[int] = []
    monkeypatch.setattr(port_takeover, "identify_port_holder", lambda *a, **k: holder)
    monkeypatch.setattr(port_takeover, "_kill", lambda pid: killed.append(pid))

    outcome = take_port("127.0.0.1", 8391, "never")

    assert outcome == TakeoverOutcome(free=False, identity=holder, action="refused")
    assert killed == []


def test_mcc_only_stops_our_own_process_and_leaves_a_stranger_alone(
    monkeypatch,
) -> None:
    killed: list[int] = []
    monkeypatch.setattr(port_takeover, "_kill", lambda pid: killed.append(pid))
    monkeypatch.setattr(port_takeover, "wait_for_port_free", lambda *a, **k: True)

    ours = ProcessIdentity(pid=11, image="mcc-server.exe", command=None)
    monkeypatch.setattr(port_takeover, "identify_port_holder", lambda *a, **k: ours)
    assert take_port("127.0.0.1", 8391, "mcc-only").free
    assert killed == [11]

    theirs = ProcessIdentity(pid=22, image="node.exe", command="node app.js")
    monkeypatch.setattr(port_takeover, "identify_port_holder", lambda *a, **k: theirs)
    outcome = take_port("127.0.0.1", 8391, "mcc-only")
    assert outcome.action == "refused"
    assert not outcome.free
    assert killed == [11]


def test_always_stops_a_stranger_and_says_so_loudly(monkeypatch, caplog) -> None:
    """Killing somebody else's process is the user's explicit instruction.

    The least this owes them is one line in the log naming what it killed.
    """

    theirs = ProcessIdentity(pid=22, image="node.exe", command="node app.js")
    killed: list[int] = []
    warnings: list[str] = []
    monkeypatch.setattr(port_takeover, "identify_port_holder", lambda *a, **k: theirs)
    monkeypatch.setattr(port_takeover, "_kill", lambda pid: killed.append(pid))
    monkeypatch.setattr(port_takeover, "wait_for_port_free", lambda *a, **k: True)

    from loguru import logger

    sink_id = logger.add(
        lambda message: warnings.append(message.record["message"]),
        level="WARNING",
    )
    try:
        outcome = take_port("127.0.0.1", 8391, "always")
    finally:
        logger.remove(sink_id)

    assert outcome.free
    assert killed == [22]
    loud = " ".join(warnings)
    assert "node.exe" in loud
    assert "22" in loud
    assert "NOT My Claude Code" in loud


def test_an_unidentifiable_holder_is_waited_out_rather_than_killed(
    monkeypatch,
) -> None:
    """A takeover with no pid to take it from has nothing to do.

    Either nothing holds the port -- a race with a generation that is a beat
    from releasing it -- or the holder could not be read. Both are "wait".
    """

    monkeypatch.setattr(port_takeover, "identify_port_holder", lambda *a, **k: None)
    monkeypatch.setattr(port_takeover, "wait_for_port_free", lambda *a, **k: True)
    killed: list[int] = []
    monkeypatch.setattr(port_takeover, "_kill", lambda pid: killed.append(pid))

    outcome = take_port("127.0.0.1", 8391, "always")

    assert outcome == TakeoverOutcome(free=True, identity=None, action="free")
    assert killed == []


def test_the_setting_refuses_an_unknown_policy(monkeypatch) -> None:
    """The one choice field that would rather not start than guess.

    Every other one clamps or warns, because a bad value there costs an answer.
    A bad value here would decide whether another program on this machine keeps
    running.
    """

    monkeypatch.setenv("SERVER_PORT_TAKEOVER", "kill-everything")
    with pytest.raises(ValueError, match="SERVER_PORT_TAKEOVER"):
        Settings()


def test_the_setting_normalises_case_and_spacing(monkeypatch) -> None:
    monkeypatch.setenv("SERVER_PORT_TAKEOVER", "  MCC-Only ")
    assert Settings().server_port_takeover == "mcc-only"
