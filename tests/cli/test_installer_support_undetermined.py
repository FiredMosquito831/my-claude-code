"""``mcc-server --stop-holder`` never calls an undetermined lookup "nothing".

The 2026-10-21 incident (7.103.1): on a loaded machine ``netstat -ano``
overran its timeout, the port lookup came back empty, and ``--stop-holder``
answered ``{"port_free": true, "message": "Nothing was listening on that
port."}`` while the old server kept listening. 7.103.0's installer then moved
the live environment out from under it.

From 7.111.3 the answer is "could not determine who holds port N (netstat
timed out after 5 s)": nothing is stopped, ``port_free`` is false, and the
exit code is 0 with a document -- the same as every other "cannot act"
answer (a stranger on the port). These tests drive the real ladder in
``cli.port_diagnostics`` through ``identify_port_holder``, with only
``subprocess.run`` faked.
"""

import json
import subprocess

import pytest

from my_claude_code.cli import installer_support, port_diagnostics

PORT = 18887


@pytest.fixture
def no_signals(monkeypatch):
    """Fail loudly if anything on this path signals a process."""

    def refuse(*args, **kwargs):  # pragma: no cover - the assertion is the point
        raise AssertionError("nothing may be signalled on this path")

    monkeypatch.setattr(installer_support, "stop_process", refuse)
    monkeypatch.setattr(installer_support, "stop_chain", refuse)
    monkeypatch.setattr(installer_support, "scan_processes", list)


def _netstat(monkeypatch, behaviour) -> None:
    """Windows: no ss; netstat times out, or answers ``behaviour``."""

    monkeypatch.setattr(port_diagnostics, "_PLATFORM", "win32")

    def run(command, *args, **kwargs):
        if command[0] == "netstat":
            if behaviour == "timeout":
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            return type("R", (), {"returncode": 0, "stdout": behaviour, "stderr": ""})()
        raise FileNotFoundError(2, "No such file or directory", command[0])

    monkeypatch.setattr(subprocess, "run", run)


NO_LISTENER = (
    "  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING       1160\n"
)

UNDETERMINED = (
    f"could not determine who holds port {PORT} "
    "(ss was not found; netstat timed out after 5 s)"
)


def test_a_timed_out_lookup_stops_nothing_and_claims_nothing(
    monkeypatch, no_signals
) -> None:
    _netstat(monkeypatch, "timeout")

    document = installer_support.stop_document("127.0.0.1", PORT)

    assert document["stopped"] is False
    assert document["port_free"] is False
    assert document["message"] == (
        f"Could not determine who holds port {PORT} (ss was not found; netstat "
        "timed out after 5 s), so nothing was stopped."
    )
    assert "Nothing was listening" not in json.dumps(document)
    assert document["holder"]["pid"] is None
    assert document["holder"]["is_mcc_server"] is False
    assert document["holder"]["undetermined"] is True
    assert document["holder"]["reason"] == UNDETERMINED
    assert document["holder_description"] == UNDETERMINED


def test_the_command_line_answers_with_exit_zero_as_for_any_cannot_act(
    monkeypatch, no_signals, capsys
) -> None:
    _netstat(monkeypatch, "timeout")

    status = installer_support.run_installer_support(["--stop-holder", str(PORT)])

    assert status == 0
    document = json.loads(capsys.readouterr().out)
    assert document["stopped"] is False
    assert document["port_free"] is False
    assert document["message"].startswith(
        f"Could not determine who holds port {PORT} ("
    )


def test_the_shell_form_says_undetermined_and_not_free(
    monkeypatch, no_signals, capsys
) -> None:
    _netstat(monkeypatch, "timeout")

    status = installer_support.run_installer_support(
        ["--stop-holder", str(PORT), "--format", "shell"]
    )

    assert status == 0
    lines = capsys.readouterr().out.splitlines()
    assert "MCC_HOLDER_PID=0" in lines
    assert "MCC_HOLDER_UNDETERMINED=1" in lines
    assert "MCC_PORT_FREE=0" in lines
    assert "MCC_STOPPED=0" in lines
    assert f"MCC_HOLDER_DESCRIPTION={UNDETERMINED}" in lines


def test_the_read_only_report_says_it_could_not_tell(monkeypatch, no_signals) -> None:
    _netstat(monkeypatch, "timeout")

    document = installer_support.report_document("127.0.0.1", PORT)

    assert document["holder"]["pid"] is None
    assert document["holder"]["undetermined"] is True
    assert document["holder_description"] == UNDETERMINED


def test_an_undetermined_port_is_not_free(monkeypatch) -> None:
    _netstat(monkeypatch, "timeout")

    assert installer_support.port_is_free("127.0.0.1", PORT) is False


def test_a_real_empty_port_still_reads_exactly_as_before(
    monkeypatch, no_signals
) -> None:
    """Control: netstat answered and lists no listener -- the old document."""

    _netstat(monkeypatch, NO_LISTENER)

    document = installer_support.stop_document("127.0.0.1", PORT)

    assert document["port_free"] is True
    assert document["message"] == "Nothing was listening on that port."
    assert document["holder"] == {
        "pid": None,
        "image": None,
        "command": None,
        "is_mcc_server": False,
        "reason": "nothing is listening on that port",
        "chain_pids": [],
    }
    assert document["holder_description"] == "nothing is listening on that port"
    assert installer_support.port_is_free("127.0.0.1", PORT) is True
    assert "MCC_HOLDER_UNDETERMINED=1" not in installer_support.shell_lines(document)


def test_a_stop_followed_by_an_undetermined_release_is_not_reported_free(
    monkeypatch,
) -> None:
    """After our server was stopped, a port lookup that cannot tell is not free."""

    from my_claude_code.cli.port_takeover import ProcessIdentity
    from my_claude_code.core.mcc_processes import ProcessFacts

    server = ProcessFacts(
        pid=300,
        parent_pid=None,
        image="python.exe",
        executable=r"C:\uv\tools\my-claude-code\Scripts\python.exe",
        command=r'"C:\uv\tools\my-claude-code\Scripts\python.exe" "C:\bin\mcc-server.exe"',
    )
    monkeypatch.setattr(installer_support, "scan_processes", lambda: [server])
    monkeypatch.setattr(installer_support, "stop_chain", lambda chain, **k: True)
    monkeypatch.setattr(installer_support, "PORT_RELEASE_SECONDS", 0.0)
    # The holder on the first ask; the release check after the stop goes
    # through the real port_is_free and the real ladder, netstat timing out.
    monkeypatch.setattr(
        installer_support,
        "identify_port_holder",
        _first_then(
            ProcessIdentity(pid=300, image="python.exe", command=None),
            monkeypatch,
        ),
    )

    document = installer_support.stop_document("127.0.0.1", PORT)

    assert document["stopped"] is True
    assert document["port_free"] is False


def _first_then(identity, monkeypatch):
    """The holder on the first ask; afterwards the real ladder with netstat timing out."""

    calls = {"n": 0}

    def identify(host, port, *, timeout=5.0):
        calls["n"] += 1
        if calls["n"] == 1:
            return identity
        _netstat(monkeypatch, "timeout")
        return port_diagnostics.diagnose_port_owner(host, port, timeout=timeout)

    return identify
