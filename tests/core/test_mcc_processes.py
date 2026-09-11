"""What counts as an MCC server process, and what must never count as one.

Every case here is taken from a real process table read off the machine that
produced this feature on 2026-09-11 (766 processes, 15 of them ``mcc-claude``).
The command lines are verbatim, because the defect this guards against is
entirely about their shape: on Windows every MCC command runs an interpreter
out of a uv tool environment whose directory is called ``my-claude-code``, so
the naive substring test that answers "is this MCC" for a port holder answers
"server" for a coding agent the user is typing into.
"""

import pytest

from my_claude_code.core.mcc_processes import (
    ProcessFacts,
    _listening_pids_from,
    mcc_server_chains,
)

_TOOL_PYTHON = r"C:\Users\x\AppData\Roaming\uv\tools\my-claude-code\Scripts\python.exe"
_MANAGED_PYTHON = (
    r"C:\Users\x\AppData\Roaming\uv\python"
    r"\cpython-3.14.0-windows-x86_64-none\python.exe"
)


def _server_chain_facts() -> list[ProcessFacts]:
    """The three processes one ``mcc-server`` launch is, on Windows."""

    command = f'"{_TOOL_PYTHON}" "C:\\Users\\x\\.local\\bin\\mcc-server.exe"'
    return [
        ProcessFacts(
            pid=6764,
            parent_pid=47740,
            image="mcc-server.exe",
            executable=r"C:\Users\x\.local\bin\mcc-server.exe",
            command='"mcc-server"',
            started_at=1000.0,
        ),
        ProcessFacts(
            pid=36856,
            parent_pid=6764,
            image="python.exe",
            executable=_TOOL_PYTHON,
            command=command,
            started_at=1000.0,
        ),
        ProcessFacts(
            pid=63484,
            parent_pid=36856,
            image="python.exe",
            executable=_MANAGED_PYTHON,
            command=command,
            started_at=1000.0,
        ),
    ]


@pytest.mark.parametrize(
    ("image", "executable", "command"),
    [
        # The uv trampoline. Its whole command line is the bare name.
        ("mcc-server.exe", r"C:\Users\x\.local\bin\mcc-server.exe", '"mcc-server"'),
        # The interpreter the trampoline execs.
        (
            "python.exe",
            _TOOL_PYTHON,
            f'"{_TOOL_PYTHON}" "C:\\Users\\x\\.local\\bin\\mcc-server.exe"',
        ),
        # POSIX shapes.
        (
            "mcc-server",
            "/home/x/.local/bin/mcc-server",
            "/home/x/.local/bin/mcc-server",
        ),
        (
            "python3",
            "/usr/bin/python3",
            "/usr/bin/python3 -m my_claude_code.cli.entrypoints",
        ),
        # The three other console scripts that map to entrypoints:serve.
        ("fcc-server.exe", r"C:\x\fcc-server.exe", '"fcc-server"'),
        ("my-claude-code.exe", r"C:\x\my-claude-code.exe", '"my-claude-code"'),
        ("free-claude-code", "/usr/local/bin/free-claude-code", "free-claude-code"),
    ],
)
def test_a_server_is_recognised_by_the_entrypoint_it_runs(
    image, executable, command
) -> None:
    facts = ProcessFacts(pid=1, image=image, executable=executable, command=command)
    assert facts.is_mcc_server


@pytest.mark.parametrize(
    ("image", "executable", "command"),
    [
        # THE case this module exists for. Verbatim from the real machine: a
        # coding agent the user is sitting in front of, whose command line
        # contains "my-claude-code" only because that is the name of the uv
        # tool environment directory every MCC command runs out of.
        (
            "python.exe",
            _TOOL_PYTHON,
            f'"{_TOOL_PYTHON}" "C:\\Users\\x\\.local\\bin\\mcc-claude.exe"',
        ),
        (
            "python.exe",
            _MANAGED_PYTHON,
            f'"{_TOOL_PYTHON}" "C:\\Users\\x\\.local\\bin\\mcc-claude.exe" --resume abc',
        ),
        # The launcher trampoline itself.
        ("mcc-claude.exe", r"C:\Users\x\.local\bin\mcc-claude.exe", '"mcc-claude"'),
        # The desktop shell is not a server either; it only starts one.
        ("mcc-desktop.exe", r"C:\Users\x\.local\bin\mcc-desktop.exe", '"mcc-desktop"'),
        # Other people's programs.
        ("python.exe", r"C:\Python\python.exe", "python -m http.server 8082"),
        ("node.exe", r"C:\node\node.exe", "node server.js"),
        # Claude Code's own binary, which lives in the same bin directory.
        ("claude.exe", r"C:\Users\x\.local\bin\claude.exe", '"claude"'),
        # Nothing readable at all is never "ours": unknown must not license
        # action.
        (None, None, None),
    ],
)
def test_a_non_server_is_never_mistaken_for_one(image, executable, command) -> None:
    facts = ProcessFacts(pid=1, image=image, executable=executable, command=command)
    assert not facts.is_mcc_server


def test_one_launch_is_one_chain_not_three_processes() -> None:
    chains = mcc_server_chains(_server_chain_facts())
    assert len(chains) == 1
    assert chains[0].pids == (6764, 36856, 63484)
    assert chains[0].root.pid == 6764
    assert chains[0].leaf.pid == 63484


def test_a_launcher_sitting_beside_a_server_is_not_part_of_its_chain() -> None:
    """A coding agent must never be swept up by the launch next to it."""

    facts = _server_chain_facts()
    facts.append(
        ProcessFacts(
            pid=59780,
            parent_pid=47740,
            image="mcc-claude.exe",
            executable=r"C:\Users\x\.local\bin\mcc-claude.exe",
            command='"mcc-claude"',
            started_at=1000.0,
        )
    )
    chains = mcc_server_chains(facts)
    assert len(chains) == 1
    assert 59780 not in chains[0].pids


def test_a_console_host_windows_attaches_is_not_part_of_the_chain() -> None:
    """Measured: Windows parents a ``conhost.exe`` onto the uv trampoline.

    It is not ours, it holds nothing of ours open, and -- because a chain is
    the list of pids the opt-in sweep would signal -- it must not be on one.
    """

    facts = _server_chain_facts()
    facts.append(
        ProcessFacts(
            pid=43064,
            parent_pid=6764,
            image="conhost.exe",
            executable=r"C:\Windows\system32\conhost.exe",
            command=r"\??\C:\Windows\system32\conhost.exe 0x4",
            started_at=1000.0,
        )
    )
    chains = mcc_server_chains(facts)
    assert chains[0].pids == (6764, 36856, 63484)
    assert r"C:\Windows\system32\conhost.exe" not in chains[0].holds()


def test_two_launches_are_two_chains() -> None:
    facts = _server_chain_facts()
    for pid, parent in ((16884, 47740), (38720, 16884), (37728, 38720)):
        template = next(
            item for item in _server_chain_facts() if item.parent_pid is not None
        )
        facts.append(
            ProcessFacts(
                pid=pid,
                parent_pid=parent,
                image="mcc-server.exe" if parent == 47740 else "python.exe",
                executable=r"C:\Users\x\.local\bin\mcc-server.exe"
                if parent == 47740
                else _TOOL_PYTHON,
                command=template.command,
                started_at=2000.0,
            )
        )
    chains = mcc_server_chains(facts)
    assert sorted(chain.root.pid for chain in chains) == [6764, 16884]


def test_the_chain_names_the_files_an_install_cannot_replace() -> None:
    holds = mcc_server_chains(_server_chain_facts())[0].holds()
    assert r"C:\Users\x\.local\bin\mcc-server.exe" in holds
    assert _TOOL_PYTHON in holds


def test_listening_pids_are_read_from_either_tool() -> None:
    netstat = (
        "  Proto  Local Address    Foreign Address   State           PID\n"
        "  TCP    0.0.0.0:8082     0.0.0.0:0         LISTENING       35280\n"
        "  TCP    127.0.0.1:24810  1.2.3.4:443       ESTABLISHED     63484\n"
    )
    assert _listening_pids_from(netstat) == frozenset({35280})

    ss_output = (
        "State  Recv-Q Send-Q Local Address:Port  Peer Address:Port Process\n"
        'LISTEN 0      2048   0.0.0.0:8082        0.0.0.0:*  users:(("python",pid=4242,fd=9))\n'
    )
    assert _listening_pids_from(ss_output) == frozenset({4242})


def test_an_established_connection_is_not_a_listening_socket() -> None:
    """The two live servers on the reporting machine had only these.

    Reading an outbound connection as "listening" would have made them look
    like they were serving, which is a different wrong answer from the one
    this feature guards against but a wrong answer all the same.
    """

    established = (
        "  TCP    192.168.1.145:24810   166.117.181.48:443   ESTABLISHED     63484\n"
        "  TCP    127.0.0.1:10477       127.0.0.1:10476      ESTABLISHED     63484\n"
    )
    assert _listening_pids_from(established) == frozenset()
