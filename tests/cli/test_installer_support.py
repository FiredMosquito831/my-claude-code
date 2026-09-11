"""What the installer asks the product before it stops or starts anything.

From 6.73.0 ``install.ps1 -Restart`` / ``install.sh --restart`` restarts the
server they just installed, and the two questions that decide whether anything
is stopped are answered here rather than in PowerShell or ``sh``:
*who holds the configured port*, and *is it one of ours*.

The rule these tests pin is the user's, stated on 2026-09-11 in two parts:

* **"Don't kill currently running servers -- I have servers running with agents
  on WAIT."** Nothing is stopped but the one server bound to the port of the
  configuration directory the install is for. Every other My Claude Code server
  on the machine is listed and left running.
* A **foreign** holder of that port is never signalled, by any path (invariant
  1, 6.59.0's ``SERVER_PORT_TAKEOVER`` contract). Neither is a holder this
  module could not identify: "cannot tell" is always "not ours".
"""

import json

import pytest

from my_claude_code.cli import installer_support
from my_claude_code.cli.port_takeover import ProcessIdentity
from my_claude_code.core.mcc_processes import ProcessFacts


def _server(pid: int, parent: int | None = None) -> ProcessFacts:
    """A process the structural scanner calls one of our servers."""

    return ProcessFacts(
        pid=pid,
        parent_pid=parent,
        image="python.exe",
        executable=r"C:\uv\tools\my-claude-code\Scripts\python.exe",
        command=r'"C:\uv\tools\my-claude-code\Scripts\python.exe" "C:\bin\mcc-server.exe"',
    )


def _launcher(pid: int, parent: int | None = None) -> ProcessFacts:
    """A coding-agent launcher out of the SAME tool environment.

    The uv tool environment is a directory literally named ``my-claude-code``,
    so this process's command line contains the product's name twice. A
    substring test says "server" about a coding agent the user is sitting in
    front of, which is exactly why the structural test exists.
    """

    return ProcessFacts(
        pid=pid,
        parent_pid=parent,
        image="python.exe",
        executable=r"C:\uv\tools\my-claude-code\Scripts\python.exe",
        command=r'"C:\uv\tools\my-claude-code\Scripts\python.exe" "C:\bin\mcc-claude.exe"',
    )


def _stranger(pid: int) -> ProcessFacts:
    return ProcessFacts(
        pid=pid,
        parent_pid=1,
        image="python.exe",
        executable=r"C:\Python313\python.exe",
        command=r'"C:\Python313\python.exe" -m http.server 8199',
    )


@pytest.fixture
def no_signals(monkeypatch):
    """Fail loudly if anything in this module signals a process."""

    def refuse(*args, **kwargs):  # pragma: no cover - the assertion is the point
        raise AssertionError("nothing may be signalled on this path")

    monkeypatch.setattr(installer_support, "stop_process", refuse)
    monkeypatch.setattr(installer_support, "stop_chain", refuse)


def _holder(monkeypatch, identity: ProcessIdentity | None) -> None:
    monkeypatch.setattr(
        installer_support, "identify_port_holder", lambda *a, **k: identity
    )


def test_an_empty_port_is_reported_as_empty_and_nothing_is_signalled(
    monkeypatch, no_signals
) -> None:
    _holder(monkeypatch, None)
    monkeypatch.setattr(installer_support, "scan_processes", list)

    document = installer_support.report_document("127.0.0.1", 8199)

    assert document["holder"]["pid"] is None
    assert document["holder"]["is_mcc_server"] is False
    assert document["other_servers"] == []


def test_a_foreign_holder_is_named_and_never_stopped(monkeypatch, no_signals) -> None:
    """Invariant 1. The one rule that has no exceptions on any path."""

    _holder(
        monkeypatch,
        ProcessIdentity(pid=4242, image="python.exe", command="python -m http.server"),
    )
    monkeypatch.setattr(
        installer_support, "scan_processes", lambda *a, **k: [_stranger(4242)]
    )

    document = installer_support.stop_document("127.0.0.1", 8199)

    assert document["holder"]["is_mcc_server"] is False
    assert document["stopped"] is False
    assert "not a My Claude Code server" in document["message"]


def test_a_launcher_from_our_own_tool_environment_is_not_a_server(
    monkeypatch, no_signals
) -> None:
    """The 6.72.2 lesson, asked the other way round.

    ``mcc-claude``'s command line contains ``my-claude-code`` because the uv
    tool environment is a directory of that name. A substring rule stops the
    user's coding agent; the structural rule does not.
    """

    _holder(
        monkeypatch,
        ProcessIdentity(
            pid=77,
            image="python.exe",
            command=r"C:\uv\tools\my-claude-code\Scripts\python.exe mcc-claude.exe",
        ),
    )
    monkeypatch.setattr(
        installer_support, "scan_processes", lambda *a, **k: [_launcher(77)]
    )

    verdict = installer_support.classify_holder("127.0.0.1", 8199)

    assert verdict.is_mcc_server is False
    assert "not a My Claude Code server" in verdict.reason


def test_an_unreadable_holder_is_not_ours(monkeypatch, no_signals) -> None:
    """ "Cannot tell" is never "ours". A process we failed to read is a stranger."""

    _holder(monkeypatch, ProcessIdentity(pid=999, image=None, command=None))
    monkeypatch.setattr(
        installer_support, "scan_processes", lambda *a, **k: [_server(1)]
    )

    verdict = installer_support.classify_holder("127.0.0.1", 8199)

    assert verdict.is_mcc_server is False
    assert "not in the process scan" in verdict.reason


def test_our_server_is_the_only_thing_that_may_be_stopped(monkeypatch) -> None:
    """And the chain stopped is exactly that ONE launch, innermost included."""

    stopped: list[tuple[int, ...]] = []

    _holder(
        monkeypatch,
        ProcessIdentity(pid=300, image="python.exe", command="mcc-server"),
    )
    # Two launches. One holds the port; the other is the user's, with agents
    # waiting on it, and it must come back untouched in `other_servers`.
    table = [
        _server(100),
        _server(200, parent=100),
        _server(300, parent=200),
        _server(900),
        _server(901, parent=900),
    ]
    monkeypatch.setattr(installer_support, "scan_processes", lambda *a, **k: table)
    monkeypatch.setattr(
        installer_support,
        "stop_chain",
        lambda chain, **kwargs: stopped.append(chain.pids) or True,
    )
    monkeypatch.setattr(installer_support, "_wait_for_release", lambda *a, **k: True)

    document = installer_support.stop_document("127.0.0.1", 8199)

    assert document["stopped"] is True
    assert document["port_free"] is True
    assert stopped == [(100, 200, 300)], stopped
    others = [tuple(item["pids"]) for item in document["other_servers"]]
    assert others == [(900, 901)], others


def test_the_stop_budget_is_the_servers_own(monkeypatch) -> None:
    """Never a number of the installer's own: the operator configured it."""

    assert installer_support.default_stop_budget(20.0) == pytest.approx(24.0)
    assert installer_support.default_stop_budget(5.0) == pytest.approx(9.0)


def test_the_shell_form_carries_every_answer_on_one_line_each() -> None:
    """``install.sh`` has no JSON parser, and must never ``eval`` what it reads."""

    document = {
        "host": "127.0.0.1",
        "port": 8199,
        "holder": {
            "pid": 42,
            "is_mcc_server": True,
            "reason": "the port holder is a My Claude Code\nserver",
        },
        "holder_description": "mcc-server.exe (pid 42)",
        "other_servers": [{"describe": "mcc-server.exe (pid 900) -> pid 901"}],
        "stopped": True,
        "port_free": True,
        "message": "Stopped it.",
    }

    lines = installer_support.shell_lines(document)

    assert "MCC_HOLDER_PID=42" in lines
    assert "MCC_HOLDER_IS_SERVER=1" in lines
    assert "MCC_PORT_FREE=1" in lines
    assert "MCC_OTHER_SERVERS=1" in lines
    assert "MCC_OTHER_SERVER_1=mcc-server.exe (pid 900) -> pid 901" in lines
    # One value, one line, whatever was in it.
    assert all(line.count("\n") == 0 for line in lines)
    assert any(
        line.startswith("MCC_HOLDER_REASON=the port holder is") for line in lines
    )


def test_the_command_line_answers_and_exits_zero(monkeypatch, capsys) -> None:
    """Exit 0 with a document is the contract; non-zero means "I do not know"."""

    monkeypatch.setattr(
        installer_support,
        "report_document",
        lambda host, port, **kwargs: {"host": host, "port": port, "holder": {}},
    )

    assert installer_support.run_installer_support(["--report-holder", "8199"]) == 0
    assert json.loads(capsys.readouterr().out)["port"] == 8199

    assert installer_support.run_installer_support(["--version"]) is None


def test_a_missing_port_is_refused_rather_than_guessed(monkeypatch, capsys) -> None:
    assert installer_support.run_installer_support(["--report-holder"]) == 2
    assert "needs a port number" in capsys.readouterr().err
