"""7.103.1: the start gate is a liveness gate, a rollback never moves an
environment a server is running out of, and /health alone is not success.

On 2026-10-19 at 02:08 a dashboard update (7.100.0 -> 7.101.0) started the new
server and waited for /health with a FIXED budget of 35 s
(DESKTOP_SERVER_START_TIMEOUT x RETRIES). The server needed ~49 s -- it was
alive, bound, and still opening a 3.3 GB request log. At 35 s the installer
rolled back: it moved the tool environment out from under the running server
(which kept importing, so CPython re-created a stub directory to write a .pyc
and the second move failed with "Cannot create a file when that file already
exists"), the server then reached readiness and its own sweep removed the copy
it was running from, and the run exited 0. /health stayed green all night while
every request failed with FileNotFoundError: the CA bundle was gone.

The user's decisions (2026-10-20 14:37):

* A1 the wait continues while the started pid is alive and the port is its
  own or nobody's; it ends at once on death or on an answer, at a ceiling of
  max(600, 10 x budget) otherwise, and the receipt says "still starting" every
  15 s;
* A2 at the ceiling the started server is stopped by its exact pid and its
  children, and only once it is gone and the port is free do directories move;
  if it cannot be stopped nothing moves and the run exits non-zero;
* A3 a rollback step that fails stops the rollback where it is, the state is
  written down, the run exits non-zero, and the next run's self-heal repairs;
  a rollback that finished is checked (Scripts/python.exe, pyvenv.cfg, uv does
  not call the tool malformed);
* A4 success also needs GET /admin/api/version to answer the version document.

Every case below extracts the REAL function bodies and RUNS them -- both
PowerShell editions, every POSIX shell this machine has -- against real
processes, real sockets and a real HTTP fixture. The static cases at the end
pin the order the main flows call them in; the full update runs are in
test_installer_self_heal.py.
"""

import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_PS1 = REPO_ROOT / "scripts" / "install.ps1"
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"

pytestmark = pytest.mark.local_serial

windows_only = pytest.mark.skipif(
    os.name != "nt", reason="the PowerShell installer is a Windows fact"
)
posix_only = pytest.mark.skipif(
    os.name == "nt",
    reason="process trees and zombies are read from /proc or ps; msys emulates neither faithfully",
)


# ------------------------------------------------------------------ helpers


def _ps_text() -> str:
    return INSTALL_PS1.read_text(encoding="utf-8").replace("\r\n", "\n")


def _sh_text() -> str:
    return INSTALL_SH.read_text(encoding="utf-8").replace("\r\n", "\n")


def _extract_ps(text: str, name: str) -> str:
    start = text.index(f"function {name} {{")
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise AssertionError(f"function {name} is not closed")


def _extract_sh(text: str, name: str) -> str:
    # Every function in install.sh closes with a "}" alone in column 0.
    start = text.index(f"\n{name}() {{") + 1
    end = text.index("\n}\n", start)
    return text[start : end + 2]


def _powershells() -> list[tuple[str, str]]:
    found = []
    for name, executable in (("ps51", "powershell"), ("pwsh7", "pwsh")):
        resolved = shutil.which(executable)
        if resolved:
            found.append((name, resolved))
    return found


EDITIONS = _powershells() or [("unavailable", "")]


def _shells() -> list[tuple[str, str]]:
    found = []
    for name in ("sh", "dash"):
        resolved = shutil.which(name)
        if resolved:
            found.append((name, resolved))
    return found


SHELLS = _shells() or [("unavailable", "")]


def _require(executable: str) -> None:
    if not executable:
        pytest.skip("no such shell on this machine")


PS_PREAMBLE = """Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$DryRun = $false
$PackageEnvDirName = "my-claude-code"
$script:StartedServerChild = $null
$script:StartGateOutcome = ""
$script:StartGateServerPid = 0
$script:RollbackRefused = $false
$script:RollbackState = ""
$script:StagedRolledBack = $false
function Write-InstallLog { param([string] $Text) Write-Host ("LOG: " + $Text) }
function Write-InstallProgress { param([string] $Stage, [string] $Message) Write-Host ("PROGRESS: " + $Stage + " " + $Message) }
"""


def _ps_harness(names: tuple[str, ...], body: str) -> str:
    text = _ps_text()
    bodies = "\n\n".join(_extract_ps(text, name) for name in names)
    return f"{PS_PREAMBLE}\n{bodies}\n\n{body}\n"


def _run_ps(
    executable: str, script: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            executable,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
        ],
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
        env=env,
    )


SH_PREAMBLE = """set -eu
STAGING_ENV_DIRNAME=".mcc-staging"
PREVIOUS_ENV_DIRNAME=".mcc-previous"
PACKAGE_ENV_DIRNAME="my-claude-code"
write_install_log() { printf 'LOG: %s\\n' "$*"; }
write_install_progress() { printf 'PROGRESS: %s %s\\n' "$1" "${2:-}"; }
run_started_server_pid=0
start_gate_outcome=""
start_gate_seconds=0
start_gate_holder_pid=""
start_gate_holder_code=""
start_gate_server_pid=""
rollback_refused=0
rollback_state=""
serve_message=""
serve_checked=1
"""


def _sh_harness(names: tuple[str, ...], body: str) -> str:
    text = _sh_text()
    bodies = "\n\n".join(_extract_sh(text, name) for name in names)
    return f"{SH_PREAMBLE}\n{bodies}\n\n{body}\n"


def _run_sh(shell: str, script: Path) -> subprocess.CompletedProcess[str]:
    # The shell's own userland first on PATH (msys `sort`/`ps`, not Windows').
    env = os.environ.copy()
    env["PATH"] = os.pathsep.join([str(Path(shell).parent), env.get("PATH", "")])
    return subprocess.run(
        [shell, str(script)],
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
        env=env,
    )


def _p(path: Path) -> str:
    return path.as_posix()


def _touch(path: Path, text: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@dataclass
class FakeHttp:
    """An HTTP server whose answer per path the test sets: (status, body, pid)."""

    port: int
    routes: dict[str, tuple[int, str, str]] = field(default_factory=dict)

    def url(self, path: str = "/health") -> str:
        return f"http://127.0.0.1:{self.port}{path}"


@pytest.fixture
def fake_http() -> Iterator[FakeHttp]:
    holder = FakeHttp(port=0)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            status, text, pid = holder.routes.get(
                self.path, (404, '{"detail":"Not Found"}', "")
            )
            body = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if pid:
                self.send_header("x-mcc-pid", pid)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    holder.port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield holder
    finally:
        server.shutdown()
        server.server_close()


SLEEPER = """import sys, time
time.sleep(float(sys.argv[1]))
"""

# A launcher with a child that holds a port: the shape of the
# .cmd -> trampoline -> python.exe chain the installer starts.
TREE = """import socket, subprocess, sys, time
if len(sys.argv) > 2 and sys.argv[2] == "child":
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", int(sys.argv[1])))
    listener.listen(5)
    time.sleep(120)
    sys.exit(0)
child = subprocess.Popen([sys.executable, __file__, sys.argv[1], "child"])
child.wait()
"""


@dataclass
class Spawned:
    processes: list[subprocess.Popen[bytes]] = field(default_factory=list)

    def start(self, script: Path, *args: str) -> subprocess.Popen[bytes]:
        process = subprocess.Popen(
            [sys.executable, str(script), *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.processes.append(process)
        return process


@pytest.fixture
def spawned(tmp_path: Path) -> Iterator[Spawned]:
    """Every process a case starts is stopped by its exact pid afterwards,
    children by parent id -- never by name."""
    holder = Spawned()
    yield holder
    for process in holder.processes:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                timeout=60,
                check=False,
            )
        else:
            process.kill()
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=10)


def _wait_listening(port: int, seconds: float = 20.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.5)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise AssertionError(f"nothing listened on {port}")


def _ps_child(pid: int) -> str:
    """A start descriptor as Start-RestartedServer builds one."""
    return (
        f"$child = [pscustomobject]@{{ Id = {pid}; StdOut = ''; StdErr = ''; "
        f"StartTime = (Get-Process -Id {pid}).StartTime.ToFileTimeUtc() }}\n"
    )


# ------------------------------------------------- the ceiling (A1), RUN


CEILING_FUNCTIONS = (
    "Get-MccConfigDir",
    "Get-MccEnvSetting",
    "Get-ServerStartTimeoutSeconds",
    "Get-ServerStartCeilingSeconds",
)


@windows_only
@pytest.mark.parametrize(("edition", "executable"), EDITIONS, ids=lambda v: v)
@pytest.mark.parametrize(
    ("timeout", "retries", "ceiling"),
    [("", "", 600), ("35", "1", 600), ("10", "1", 600), ("120", "2", 2400)],
    ids=["defaults", "the-incident-budget", "under-the-floor", "a-raised-budget"],
)
def test_ps_the_ceiling_is_ten_budgets_and_never_under_ten_minutes(
    edition: str,
    executable: str,
    timeout: str,
    retries: str,
    ceiling: int,
    tmp_path: Path,
) -> None:
    _require(executable)
    config = tmp_path / "config"
    config.mkdir()
    lines = []
    if timeout:
        lines.append(f"DESKTOP_SERVER_START_TIMEOUT={timeout}")
    if retries:
        lines.append(f"DESKTOP_SERVER_START_RETRIES={retries}")
    (config / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")
    script = tmp_path / "ceiling.ps1"
    script.write_text(
        _ps_harness(
            CEILING_FUNCTIONS,
            "Write-Host ('CEILING=' + (Get-ServerStartCeilingSeconds))",
        ),
        encoding="utf-8",
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("DESKTOP_SERVER_START")
    }
    env["MCC_CONFIG_DIR"] = str(config)
    completed = _run_ps(executable, script, env=env)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert f"CEILING={ceiling}" in completed.stdout, completed.stdout


@pytest.mark.parametrize(("name", "shell"), SHELLS, ids=lambda v: v)
@pytest.mark.parametrize(
    ("budget", "ceiling"), [(30, 600), (40, 600), (35, 600), (240, 2400)]
)
def test_sh_the_ceiling_is_ten_budgets_and_never_under_ten_minutes(
    name: str, shell: str, budget: int, ceiling: int, tmp_path: Path
) -> None:
    _require(shell)
    body = f"""
server_start_budget_seconds() {{ printf '{budget}'; }}
printf 'CEILING=%s\\n' "$(server_start_ceiling_seconds)"
"""
    script = tmp_path / "ceiling.sh"
    script.write_text(
        _sh_harness(("server_start_ceiling_seconds",), body),
        encoding="utf-8",
        newline="\n",
    )
    completed = _run_sh(shell, script)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert f"CEILING={ceiling}" in completed.stdout


# -------------------------------------------- the liveness gate (A1), RUN


GATE_FUNCTIONS = (
    "Get-StartedProcessStartTime",
    "Test-StartedProcessAlive",
    "Get-HeaderPid",
    "Get-HealthProbe",
    "Test-PortHasListener",
    "Test-AnswerIsFromStartedServer",
    "Wait-ForStartedServer",
)


def _gate_body(pid: int, url: str, port: int, ceiling: float) -> str:
    return (
        _ps_child(pid)
        + f"function Get-ServerStartCeilingSeconds {{ return {ceiling} }}\n"
        + f"$gate = Wait-ForStartedServer -Url '{url}' -Child $child -Port {port} -ReportEverySeconds 1 -Label 'The test server'\n"
        + "Write-Host ('OUTCOME=' + $gate.Outcome + ' SECONDS=' + $gate.Seconds + ' HOLDER=' + $gate.HolderPid + ' STATUS=' + $gate.HolderStatus)\n"
    )


def _outcome(stdout: str) -> dict[str, str]:
    match = re.search(
        r"OUTCOME=(\S+) SECONDS=([\d.]+) HOLDER=(\d+) STATUS=(\d+)", stdout
    )
    assert match, stdout
    return {
        "outcome": match.group(1),
        "seconds": match.group(2),
        "holder": match.group(3),
        "status": match.group(4),
    }


@windows_only
@pytest.mark.parametrize(("edition", "executable"), EDITIONS, ids=lambda v: v)
def test_ps_the_gate_ends_within_seconds_when_the_started_process_dies(
    edition: str, executable: str, tmp_path: Path, spawned: Spawned
) -> None:
    """A crash ends the wait at once -- not at a budget, not at the ceiling."""
    _require(executable)
    sleeper = tmp_path / "sleeper.py"
    sleeper.write_text(SLEEPER, encoding="utf-8")
    child = spawned.start(sleeper, "2")
    port = _free_port()
    script = tmp_path / "gate.ps1"
    script.write_text(
        _ps_harness(
            GATE_FUNCTIONS,
            _gate_body(child.pid, f"http://127.0.0.1:{port}/health", port, 120),
        ),
        encoding="utf-8",
    )
    completed = _run_ps(executable, script)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = _outcome(completed.stdout)
    assert result["outcome"] == "exited"
    assert float(result["seconds"]) < 8, result


@windows_only
@pytest.mark.parametrize(("edition", "executable"), EDITIONS, ids=lambda v: v)
def test_ps_a_live_silent_server_is_waited_for_until_the_ceiling_and_reported(
    edition: str, executable: str, tmp_path: Path, spawned: Spawned
) -> None:
    """Alive and not answering is STARTING -- the receipt says so -- until the
    ceiling, where it is hung."""
    _require(executable)
    sleeper = tmp_path / "sleeper.py"
    sleeper.write_text(SLEEPER, encoding="utf-8")
    child = spawned.start(sleeper, "60")
    port = _free_port()
    script = tmp_path / "gate.ps1"
    script.write_text(
        _ps_harness(
            GATE_FUNCTIONS,
            _gate_body(child.pid, f"http://127.0.0.1:{port}/health", port, 4),
        ),
        encoding="utf-8",
    )
    completed = _run_ps(executable, script)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = _outcome(completed.stdout)
    assert result["outcome"] == "ceiling"
    assert 4 <= float(result["seconds"]) < 12, result
    reports = re.findall(
        rf"LOG: The test server is still starting, \d+ s \(pid {child.pid}\)\.",
        completed.stdout,
    )
    assert len(reports) >= 2, completed.stdout
    assert "PROGRESS: starting The test server is still starting" in completed.stdout
    assert child.poll() is None, "the gate itself stops nothing"


@windows_only
@pytest.mark.parametrize(("edition", "executable"), EDITIONS, ids=lambda v: v)
def test_ps_an_answer_ends_the_wait_and_another_servers_503_is_port_taken(
    edition: str,
    executable: str,
    tmp_path: Path,
    spawned: Spawned,
    fake_http: FakeHttp,
) -> None:
    """200 ends the wait as `answered` (whose answer it is, the caller
    decides). A server that is not ours answering 503 -- starting, or shutting
    down -- twice in a row ends it as `port-taken`, never a rollback."""
    _require(executable)
    sleeper = tmp_path / "sleeper.py"
    sleeper.write_text(SLEEPER, encoding="utf-8")
    child = spawned.start(sleeper, "60")

    fake_http.routes["/health"] = (200, '{"status":"healthy"}', "")
    answered = tmp_path / "answered.ps1"
    answered.write_text(
        _ps_harness(
            GATE_FUNCTIONS,
            _gate_body(child.pid, fake_http.url(), fake_http.port, 30),
        ),
        encoding="utf-8",
    )
    first = _run_ps(executable, answered)
    assert first.returncode == 0, first.stdout + first.stderr
    assert _outcome(first.stdout)["outcome"] == "answered"

    # This test process is not under the sleeper: positively someone else.
    fake_http.routes["/health"] = (503, '{"status":"starting"}', str(os.getpid()))
    taken = tmp_path / "taken.ps1"
    taken.write_text(
        _ps_harness(
            GATE_FUNCTIONS,
            _gate_body(child.pid, fake_http.url(), fake_http.port, 30),
        ),
        encoding="utf-8",
    )
    second = _run_ps(executable, taken)
    assert second.returncode == 0, second.stdout + second.stderr
    result = _outcome(second.stdout)
    assert result["outcome"] == "port-taken"
    assert result["holder"] == str(os.getpid())
    assert result["status"] == "503"
    assert float(result["seconds"]) < 10, result


@pytest.mark.parametrize(("name", "shell"), SHELLS, ids=lambda v: v)
def test_sh_the_gate_ends_within_seconds_when_the_started_process_dies(
    name: str, shell: str, tmp_path: Path
) -> None:
    _require(shell)
    port = _free_port()
    body = f"""
sleep 2 &
gate_child=$!
wait_for_started_server 'http://127.0.0.1:{port}/health' "$gate_child" 'The test server' 120
printf 'OUTCOME=%s SECONDS=%s\\n' "$start_gate_outcome" "$start_gate_seconds"
"""
    script = tmp_path / "gate.sh"
    script.write_text(
        _sh_harness(
            (
                "now_seconds",
                "started_process_alive",
                "health_answer",
                "parent_pid_of",
                "answer_is_from_started_server",
                "wait_for_started_server",
            ),
            body,
        ),
        encoding="utf-8",
        newline="\n",
    )
    completed = _run_sh(shell, script)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    match = re.search(r"OUTCOME=(\S+) SECONDS=(\d+)", completed.stdout)
    assert match, completed.stdout
    assert match.group(1) == "exited"
    assert int(match.group(2)) <= 8


@pytest.mark.parametrize(("name", "shell"), SHELLS, ids=lambda v: v)
def test_sh_a_live_silent_server_is_waited_for_until_the_ceiling(
    name: str, shell: str, tmp_path: Path
) -> None:
    _require(shell)
    port = _free_port()
    body = f"""
sleep 30 &
gate_child=$!
wait_for_started_server 'http://127.0.0.1:{port}/health' "$gate_child" 'The test server' 3
printf 'OUTCOME=%s SECONDS=%s\\n' "$start_gate_outcome" "$start_gate_seconds"
kill "$gate_child" 2>/dev/null || true
"""
    script = tmp_path / "gate.sh"
    script.write_text(
        _sh_harness(
            (
                "now_seconds",
                "started_process_alive",
                "health_answer",
                "parent_pid_of",
                "answer_is_from_started_server",
                "wait_for_started_server",
            ),
            body,
        ),
        encoding="utf-8",
        newline="\n",
    )
    completed = _run_sh(shell, script)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    match = re.search(r"OUTCOME=(\S+) SECONDS=(\d+)", completed.stdout)
    assert match, completed.stdout
    assert match.group(1) == "ceiling"
    assert 3 <= int(match.group(2)) <= 10


@posix_only
@pytest.mark.parametrize(("name", "shell"), SHELLS, ids=lambda v: v)
def test_sh_a_child_that_exited_is_not_alive_although_kill_0_answers_for_it(
    name: str, shell: str, tmp_path: Path
) -> None:
    """The server is the installer's own child: once it exits it is a zombie
    until reaped, and `kill -0` still succeeds for it."""
    _require(shell)
    body = """
sleep 0 &
gate_child=$!
sleep 1
if kill -0 "$gate_child" 2>/dev/null; then echo KILL0=yes; else echo KILL0=no; fi
if started_process_alive "$gate_child"; then echo ALIVE=yes; else echo ALIVE=no; fi
"""
    script = tmp_path / "zombie.sh"
    script.write_text(
        _sh_harness(("started_process_alive",), body), encoding="utf-8", newline="\n"
    )
    completed = _run_sh(shell, script)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "ALIVE=no" in completed.stdout


# ---------------------------------------------- /health is not success (A4)


SERVE_CASES = [
    (200, '{"current":"9.9.9","latest":null}', True, True, "answered: version 9.9.9."),
    (
        500,
        '{"error":{"type":"api_error","message":"[Errno 2] No such file or directory"}}',
        False,
        True,
        "it answered HTTP 500",
    ),
    (403, '{"detail":"Admin UI is local-only"}', True, False, "was not checked"),
    (200, "not a version document", False, True, "without the version document"),
]


@windows_only
@pytest.mark.parametrize(("edition", "executable"), EDITIONS, ids=lambda v: v)
@pytest.mark.parametrize(
    ("status", "text", "ok", "checked", "says"),
    SERVE_CASES,
    ids=["version-json", "500-no-ca-bundle", "403-not-loopback", "200-not-json"],
)
def test_ps_can_serve_means_the_version_route_answers_the_version_document(
    edition: str,
    executable: str,
    status: int,
    text: str,
    ok: bool,
    checked: bool,
    says: str,
    tmp_path: Path,
    fake_http: FakeHttp,
) -> None:
    _require(executable)
    fake_http.routes["/admin/api/version"] = (status, text, "")
    script = tmp_path / "serve.ps1"
    script.write_text(
        _ps_harness(
            ("Test-ServerCanServe",),
            f"$v = Test-ServerCanServe -HealthUrl '{fake_http.url()}' -Attempts 2\n"
            "Write-Host ('OK=' + $v.Ok + ' CHECKED=' + $v.Checked)\n"
            "Write-Host ('MESSAGE=' + $v.Message)\n",
        ),
        encoding="utf-8",
    )
    completed = _run_ps(executable, script)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert f"OK={ok} CHECKED={checked}" in completed.stdout, completed.stdout
    assert says in completed.stdout


@pytest.mark.parametrize(("name", "shell"), SHELLS, ids=lambda v: v)
@pytest.mark.parametrize(
    ("status", "text", "ok", "checked", "says"),
    SERVE_CASES,
    ids=["version-json", "500-no-ca-bundle", "403-not-loopback", "200-not-json"],
)
def test_sh_can_serve_means_the_version_route_answers_the_version_document(
    name: str,
    shell: str,
    status: int,
    text: str,
    ok: bool,
    checked: bool,
    says: str,
    tmp_path: Path,
    fake_http: FakeHttp,
) -> None:
    _require(shell)
    if not shutil.which("curl"):
        pytest.skip("no curl")
    fake_http.routes["/admin/api/version"] = (status, text, "")
    body = f"""
if server_can_serve '{fake_http.url()}'; then serve_ok=1; else serve_ok=0; fi
printf 'OK=%s CHECKED=%s\\n' "$serve_ok" "$serve_checked"
printf 'MESSAGE=%s\\n' "$serve_message"
"""
    script = tmp_path / "serve.sh"
    script.write_text(
        _sh_harness(("server_can_serve",), body), encoding="utf-8", newline="\n"
    )
    completed = _run_sh(shell, script)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert f"OK={int(ok)} CHECKED={int(checked)}" in completed.stdout, completed.stdout
    assert says in completed.stdout


# ------------------------------------------ stop before anything moves (A2)


STOP_FUNCTIONS = (
    "Get-StartedProcessStartTime",
    "Test-AnswerIsFromStartedServer",
    "Get-StartedProcessTree",
    "Stop-ProcessByExactId",
    "Test-StartedMemberAlive",
    "Test-PortIsOccupied",
    "Test-PortHasListener",
    "Test-ServerPortFree",
    "Stop-StartedServer",
)


def _stop_body(pid: int, port: int, extra: str = "") -> str:
    return (
        _ps_child(pid)
        + extra
        + f"$address = [pscustomobject]@{{ BindHost = '127.0.0.1'; ReachableHost = '127.0.0.1'; Port = {port} }}\n"
        + "$stop = Stop-StartedServer -Child $child -Address $address -ExitSeconds 5 -PortSeconds 3\n"
        + "Write-Host ('STOPPED=' + $stop.Stopped + ' FREE=' + $stop.PortFree + ' MEMBERS=' + ($stop.Members -join ',') + ' REMAINING=' + ($stop.Remaining -join ','))\n"
        + "Write-Host ('MESSAGE=' + $stop.Message)\n"
    )


def _tree_pids(parent: int) -> set[int]:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        listed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"(Get-CimInstance Win32_Process -Filter 'ParentProcessId={parent}').ProcessId",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        children = {int(item) for item in listed.stdout.split() if item.isdigit()}
        if children:
            return {parent} | children
        time.sleep(0.3)
    raise AssertionError("the tree's child never started")


def _alive(pid: int) -> bool:
    listed = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return f'"{pid}"' in listed.stdout


@windows_only
@pytest.mark.parametrize(("edition", "executable"), EDITIONS, ids=lambda v: v)
def test_ps_the_started_server_and_its_children_are_stopped_and_the_port_freed(
    edition: str, executable: str, tmp_path: Path, spawned: Spawned
) -> None:
    _require(executable)
    tree = tmp_path / "tree.py"
    tree.write_text(TREE, encoding="utf-8")
    port = _free_port()
    parent = spawned.start(tree, str(port))
    _wait_listening(port)
    pids = _tree_pids(parent.pid)
    script = tmp_path / "stop.ps1"
    script.write_text(
        _ps_harness(STOP_FUNCTIONS, _stop_body(parent.pid, port)), encoding="utf-8"
    )
    completed = _run_ps(executable, script)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "STOPPED=True FREE=True" in completed.stdout, completed.stdout
    members = re.search(r"MEMBERS=([\d,]*)", completed.stdout)
    assert members
    assert pids <= {int(item) for item in members.group(1).split(",") if item}
    assert not any(_alive(pid) for pid in pids)


@windows_only
@pytest.mark.parametrize(("edition", "executable"), EDITIONS, ids=lambda v: v)
def test_ps_a_stop_that_is_refused_is_reported_with_the_pids_still_running(
    edition: str, executable: str, tmp_path: Path, spawned: Spawned
) -> None:
    _require(executable)
    tree = tmp_path / "tree.py"
    tree.write_text(TREE, encoding="utf-8")
    port = _free_port()
    parent = spawned.start(tree, str(port))
    _wait_listening(port)
    pids = _tree_pids(parent.pid)
    refused = (
        "function Stop-ProcessByExactId { param([int] $ProcessId) return $false }\n"
    )
    script = tmp_path / "stop.ps1"
    script.write_text(
        _ps_harness(STOP_FUNCTIONS, _stop_body(parent.pid, port, refused)),
        encoding="utf-8",
    )
    completed = _run_ps(executable, script)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "STOPPED=False FREE=False" in completed.stdout, completed.stdout
    remaining = re.search(r"REMAINING=([\d,]*)", completed.stdout)
    assert remaining
    assert pids <= {int(item) for item in remaining.group(1).split(",") if item}
    assert f"(pid {parent.pid}) could not be stopped" in completed.stdout
    assert all(_alive(pid) for pid in pids)


@windows_only
@pytest.mark.parametrize(("edition", "executable"), EDITIONS, ids=lambda v: v)
def test_ps_a_port_held_by_a_process_this_run_did_not_start_is_not_free(
    edition: str, executable: str, tmp_path: Path, spawned: Spawned
) -> None:
    """The started server is gone; a stranger holds the port. Stopped, but the
    port is NOT free -- and the stranger is not touched."""
    _require(executable)
    sleeper = tmp_path / "sleeper.py"
    sleeper.write_text(SLEEPER, encoding="utf-8")
    child = spawned.start(sleeper, "60")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as stranger:
        stranger.bind(("127.0.0.1", 0))
        stranger.listen(5)
        port = int(stranger.getsockname()[1])
        script = tmp_path / "stop.ps1"
        script.write_text(
            _ps_harness(STOP_FUNCTIONS, _stop_body(child.pid, port)), encoding="utf-8"
        )
        completed = _run_ps(executable, script)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "STOPPED=True FREE=False" in completed.stdout, completed.stdout
    assert f"Port {port} is still held." in completed.stdout


@posix_only
@pytest.mark.parametrize(("name", "shell"), SHELLS, ids=lambda v: v)
def test_sh_the_started_server_and_its_children_are_stopped(
    name: str, shell: str, tmp_path: Path
) -> None:
    _require(shell)
    port = _free_port()
    body = f"""
sh -c 'sleep 60 & wait' &
stop_root=$!
sleep 1
server_reachable_host=127.0.0.1
server_port={port}
port_is_occupied() {{ return 1; }}
stop_started_server "$stop_root"
printf 'OK=%s FREE=%s REMAINING=%s\\n' "$stop_started_ok" "$stop_started_port_free" "$stop_started_remaining"
printf 'MESSAGE=%s\\n' "$stop_started_message"
"""
    script = tmp_path / "stop.sh"
    script.write_text(
        _sh_harness(
            (
                "started_process_alive",
                "parent_pid_of",
                "answer_is_from_started_server",
                "started_process_tree",
                "stop_started_server",
            ),
            body,
        ),
        encoding="utf-8",
        newline="\n",
    )
    completed = _run_sh(shell, script)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "OK=1 FREE=1 REMAINING=\n" in completed.stdout, completed.stdout
    assert "Stopped the server this install started (pid " in completed.stdout
    assert "2 process(es)" in completed.stdout


# ----------------------------- the rollback refuses, stops, checks (A2, A3)


RESTORE_FUNCTIONS = (
    "Get-StartedProcessStartTime",
    "Test-AnswerIsFromStartedServer",
    "Get-StartedProcessTree",
    "Test-PortIsOccupied",
    "Test-PortHasListener",
    "Test-ServerPortFree",
    "Get-EnvironmentStateLine",
    "Convert-OutputLine",
    "Get-ToolEnvironmentProblem",
    "Restore-PreviousEnvironment",
)


@dataclass
class Layout:
    tools: Path
    canonical: Path
    staging: Path
    previous_dir: Path
    aside: Path
    uv: Path


def _layout(tmp_path: Path, *, aside_runnable: bool = True) -> Layout:
    tools = tmp_path / "uv" / "tools"
    canonical = tools / "my-claude-code"
    _touch(canonical / "Scripts" / "python.exe", "new")
    _touch(canonical / "pyvenv.cfg", "new")
    _touch(canonical / "uv-receipt.toml", "new")
    _touch(canonical / "VERSION", "9.9.9")
    staging = tmp_path / "uv" / ".mcc-staging"
    staging.mkdir(parents=True)
    previous_dir = tmp_path / "uv" / ".mcc-previous" / "20261021-010000"
    aside = previous_dir / "my-claude-code"
    if aside_runnable:
        _touch(aside / "Scripts" / "python.exe", "old")
        _touch(aside / "pyvenv.cfg", "old")
    _touch(aside / "uv-receipt.toml", "old")
    _touch(aside / "VERSION", "9.9.8")
    uv = tmp_path / "fake-uv.cmd"
    uv.write_text(
        "@echo off\r\n"
        'if "%FAKE_MALFORMED%"=="1" echo warning: Ignoring malformed tool `my-claude-code` 1>&2\r\n'
        "exit /b 0\r\n",
        encoding="utf-8",
    )
    return Layout(tools, canonical, staging, previous_dir, aside, uv)


def _restore_body(layout: Layout, prelude: str = "", port: int = 0) -> str:
    return (
        prelude
        + f"function Get-MccServerAddress {{ return [pscustomobject]@{{ BindHost = '127.0.0.1'; ReachableHost = '127.0.0.1'; Port = {port or _free_port()} }} }}\n"
        + "$ok = Restore-PreviousEnvironment"
        + f" -ToolDir '{layout.canonical}' -AsideEnv '{layout.aside}'"
        + f" -StagingRoot '{layout.staging}' -Stamp '20261021-010000'"
        + f" -PreviousDir '{layout.previous_dir}' -UvPath '{layout.uv}'\n"
        + "Write-Host ('RESTORED=' + $ok + ' REFUSED=' + $script:RollbackRefused + ' ROLLEDBACK=' + $script:StagedRolledBack)\n"
        + "Write-Host ('STATE=' + $script:RollbackState)\n"
    )


def _version(path: Path) -> str:
    return (path / "VERSION").read_text(encoding="utf-8")


@windows_only
@pytest.mark.parametrize(("edition", "executable"), EDITIONS, ids=lambda v: v)
def test_ps_the_rollback_refuses_to_move_anything_while_the_started_server_lives(
    edition: str, executable: str, tmp_path: Path, spawned: Spawned
) -> None:
    """The guard is INSIDE Restore-PreviousEnvironment, so no caller can skip it."""
    _require(executable)
    layout = _layout(tmp_path)
    sleeper = tmp_path / "sleeper.py"
    sleeper.write_text(SLEEPER, encoding="utf-8")
    child = spawned.start(sleeper, "60")
    prelude = _ps_child(child.pid) + "$script:StartedServerChild = $child\n"
    script = tmp_path / "restore.ps1"
    script.write_text(
        _ps_harness(RESTORE_FUNCTIONS, _restore_body(layout, prelude)),
        encoding="utf-8",
    )
    completed = _run_ps(executable, script)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "RESTORED=False REFUSED=True ROLLEDBACK=False" in completed.stdout
    assert (
        f"The rollback was not attempted: the server this install started is still running (pid {child.pid}"
        in completed.stdout
    )
    assert "Nothing was moved." in completed.stdout
    assert _version(layout.canonical) == "9.9.9"
    assert _version(layout.aside) == "9.9.8"
    assert list(layout.staging.iterdir()) == []


@windows_only
@pytest.mark.parametrize(("edition", "executable"), EDITIONS, ids=lambda v: v)
def test_ps_the_rollback_refuses_while_the_port_is_held_after_its_server_is_gone(
    edition: str, executable: str, tmp_path: Path, spawned: Spawned
) -> None:
    _require(executable)
    layout = _layout(tmp_path)
    sleeper = tmp_path / "sleeper.py"
    sleeper.write_text(SLEEPER, encoding="utf-8")
    child = spawned.start(sleeper, "0")
    start_time = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            f"(Get-Process -Id {child.pid}).StartTime.ToFileTimeUtc()",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    ).stdout.strip()
    child.wait(timeout=30)
    prelude = f"$script:StartedServerChild = [pscustomobject]@{{ Id = {child.pid}; StartTime = [long] '{start_time or 0}' }}\n"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen(5)
        port = int(holder.getsockname()[1])
        script = tmp_path / "restore.ps1"
        script.write_text(
            _ps_harness(RESTORE_FUNCTIONS, _restore_body(layout, prelude, port)),
            encoding="utf-8",
        )
        completed = _run_ps(executable, script)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "RESTORED=False REFUSED=True" in completed.stdout, completed.stdout
    assert f"port {port} is still held" in completed.stdout
    assert _version(layout.canonical) == "9.9.9"
    assert _version(layout.aside) == "9.9.8"


@windows_only
@pytest.mark.parametrize(("edition", "executable"), EDITIONS, ids=lambda v: v)
def test_ps_the_rollback_refuses_a_previous_environment_that_cannot_run(
    edition: str, executable: str, tmp_path: Path
) -> None:
    """Moving the live environment away for a husk would gain nothing."""
    _require(executable)
    layout = _layout(tmp_path, aside_runnable=False)
    script = tmp_path / "restore.ps1"
    script.write_text(
        _ps_harness(RESTORE_FUNCTIONS, _restore_body(layout)), encoding="utf-8"
    )
    completed = _run_ps(executable, script)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "RESTORED=False REFUSED=True" in completed.stdout
    assert "has no Scripts\\python.exe or pyvenv.cfg to run" in completed.stdout
    assert _version(layout.canonical) == "9.9.9"


@windows_only
@pytest.mark.parametrize(("edition", "executable"), EDITIONS, ids=lambda v: v)
def test_ps_a_rollback_step_that_fails_stops_the_rollback_and_says_what_is_where(
    edition: str, executable: str, tmp_path: Path
) -> None:
    """Decision A3. A file in the previous environment held open without
    FILE_SHARE_DELETE makes Windows refuse to rename that directory: step 2
    fails, nothing more is tried, and the state names every path."""
    _require(executable)
    layout = _layout(tmp_path)
    script = tmp_path / "restore.ps1"
    script.write_text(
        _ps_harness(RESTORE_FUNCTIONS, _restore_body(layout)), encoding="utf-8"
    )
    with open(layout.aside / "pyvenv.cfg", encoding="utf-8"):
        completed = _run_ps(executable, script)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    out = completed.stdout
    assert "RESTORED=False REFUSED=False ROLLEDBACK=False" in out, out
    assert "The rollback stopped at step 2 of 2" in out
    failed = layout.staging / "20261021-010000-failed" / "my-claude-code"
    assert f"Canonical path: {layout.canonical} does not exist." in out
    assert f"Failed version: {failed} exists" in out
    assert f"Previous version: {layout.aside} exists" in out
    assert _version(failed) == "9.9.9"
    assert _version(layout.aside) == "9.9.8"
    assert not layout.canonical.exists()


@windows_only
@pytest.mark.parametrize(("edition", "executable"), EDITIONS, ids=lambda v: v)
@pytest.mark.parametrize("malformed", ["0", "1"], ids=["clean", "uv-says-malformed"])
def test_ps_a_finished_rollback_is_checked_before_it_counts(
    edition: str, executable: str, malformed: str, tmp_path: Path
) -> None:
    _require(executable)
    layout = _layout(tmp_path)
    script = tmp_path / "restore.ps1"
    script.write_text(
        _ps_harness(RESTORE_FUNCTIONS, _restore_body(layout)), encoding="utf-8"
    )
    completed = _run_ps(
        executable, script, env=os.environ | {"FAKE_MALFORMED": malformed}
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    out = completed.stdout
    assert "The previous environment is back at the canonical path." in out
    assert _version(layout.canonical) == "9.9.8"
    if malformed == "1":
        assert "RESTORED=False REFUSED=False ROLLEDBACK=False" in out, out
        assert "but it cannot be used: uv tool list reports it as malformed" in out
    else:
        assert "RESTORED=True REFUSED=False ROLLEDBACK=True" in out, out
        assert (
            "Scripts\\python.exe and pyvenv.cfg are there and uv does not call it malformed."
            in out
        )
        assert not layout.previous_dir.exists()


def _sh_layout(tmp_path: Path, *, aside_runnable: bool = True) -> Layout:
    tools = tmp_path / "uv" / "tools"
    canonical = tools / "my-claude-code"
    _touch(canonical / "bin" / "python", "#!/bin/sh\nexit 0\n")
    (canonical / "bin" / "python").chmod(0o755)
    _touch(canonical / "pyvenv.cfg", "new")
    _touch(canonical / "uv-receipt.toml", "new")
    _touch(canonical / "VERSION", "9.9.9")
    staging = tmp_path / "uv" / ".mcc-staging"
    staging.mkdir(parents=True)
    previous_dir = tmp_path / "uv" / ".mcc-previous" / "20261021-010000"
    aside = previous_dir / "my-claude-code"
    if aside_runnable:
        _touch(aside / "bin" / "python", "#!/bin/sh\nexit 0\n")
        (aside / "bin" / "python").chmod(0o755)
        _touch(aside / "pyvenv.cfg", "old")
    _touch(aside / "uv-receipt.toml", "old")
    _touch(aside / "VERSION", "9.9.8")
    uv = tmp_path / "fake-uv"
    uv.write_text(
        '#!/bin/sh\n[ "${FAKE_MALFORMED:-0}" = "1" ] && echo "warning: Ignoring malformed tool \\`my-claude-code\\`" >&2\nexit 0\n',
        encoding="utf-8",
        newline="\n",
    )
    uv.chmod(0o755)
    return Layout(tools, canonical, staging, previous_dir, aside, uv)


SH_RESTORE_FUNCTIONS = (
    "started_process_alive",
    "started_process_tree",
    "port_is_occupied",
    "environment_state_line",
    "tool_environment_problem",
    "restore_previous_environment",
)


def _sh_restore_body(layout: Layout, prelude: str = "") -> str:
    return f"""
{prelude}
uv_bin='{_p(layout.uv)}'
staging_root='{_p(layout.staging)}'
staged_stamp=20261021-010000
swap_aside_env='{_p(layout.aside)}'
swap_previous_dir='{_p(layout.previous_dir)}'
server_reachable_host=127.0.0.1
server_port={_free_port()}
if restore_previous_environment '{_p(layout.canonical)}'; then restored=1; else restored=0; fi
printf 'RESTORED=%s REFUSED=%s\\n' "$restored" "$rollback_refused"
printf 'STATE=%s\\n' "$rollback_state"
"""


@pytest.mark.parametrize(("name", "shell"), SHELLS, ids=lambda v: v)
def test_sh_the_rollback_refuses_to_move_anything_while_the_started_server_lives(
    name: str, shell: str, tmp_path: Path
) -> None:
    _require(shell)
    layout = _sh_layout(tmp_path)
    prelude = "sleep 30 &\nrun_started_server_pid=$!\nport_is_occupied() { return 1; }"
    script = tmp_path / "restore.sh"
    script.write_text(
        _sh_harness(SH_RESTORE_FUNCTIONS, _sh_restore_body(layout, prelude))
        + 'kill "$run_started_server_pid" 2>/dev/null || true\n',
        encoding="utf-8",
        newline="\n",
    )
    completed = _run_sh(shell, script)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "RESTORED=0 REFUSED=1" in completed.stdout, completed.stdout
    assert "the server this install started is still running (pid " in completed.stdout
    assert "Nothing was moved." in completed.stdout
    assert _version(layout.canonical) == "9.9.9"
    assert _version(layout.aside) == "9.9.8"


@pytest.mark.parametrize(("name", "shell"), SHELLS, ids=lambda v: v)
def test_sh_the_rollback_refuses_a_previous_environment_that_cannot_run(
    name: str, shell: str, tmp_path: Path
) -> None:
    _require(shell)
    layout = _sh_layout(tmp_path, aside_runnable=False)
    script = tmp_path / "restore.sh"
    script.write_text(
        _sh_harness(SH_RESTORE_FUNCTIONS, _sh_restore_body(layout)),
        encoding="utf-8",
        newline="\n",
    )
    completed = _run_sh(shell, script)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "RESTORED=0 REFUSED=1" in completed.stdout
    assert "has no runnable bin/python or pyvenv.cfg" in completed.stdout
    assert _version(layout.canonical) == "9.9.9"


@pytest.mark.parametrize(("name", "shell"), SHELLS, ids=lambda v: v)
@pytest.mark.parametrize("malformed", ["0", "1"], ids=["clean", "uv-says-malformed"])
def test_sh_a_finished_rollback_is_checked_before_it_counts(
    name: str, shell: str, malformed: str, tmp_path: Path
) -> None:
    _require(shell)
    layout = _sh_layout(tmp_path)
    script = tmp_path / "restore.sh"
    script.write_text(
        _sh_harness(
            SH_RESTORE_FUNCTIONS,
            _sh_restore_body(layout, f"export FAKE_MALFORMED={malformed}"),
        ),
        encoding="utf-8",
        newline="\n",
    )
    completed = _run_sh(shell, script)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    out = completed.stdout
    assert "The previous environment is back at the canonical path." in out
    assert _version(layout.canonical) == "9.9.8"
    if malformed == "1":
        assert "RESTORED=0 REFUSED=0" in out, out
        assert "but it cannot be used: uv tool list reports it as malformed" in out
    else:
        assert "RESTORED=1 REFUSED=0" in out, out
        assert (
            "bin/python and pyvenv.cfg are there and uv does not call it malformed."
            in out
        )


# ------------------------------------------- the wiring, read from the scripts


def _ps_main() -> str:
    text = _ps_text()
    return text[text.index("\nif ($Help) {") :]


def test_the_ps1_rollback_stops_the_started_server_before_it_moves_anything() -> None:
    main = _ps_main()
    rollback = main[main.index("# ROLLBACK. The new version is installed") :]
    rollback = rollback[
        : rollback.index("\nelseif ((-not $DryRun) -and (-not $script:Deferred)) {")
    ]
    stop = rollback.index(
        "$stop = Stop-StartedServer -Child $StartedServer -Address $Address"
    )
    refuse = rollback.index("if (-not ($stop.Stopped -and $stop.PortFree)) {")
    restore = rollback.index("$restored = Restore-PreviousEnvironment")
    assert stop < refuse < restore
    refusal = rollback[refuse:restore]
    assert "so nothing was moved" in refusal
    assert "exit 1" in refusal
    # A rollback that did not finish exits non-zero (decision A3).
    tail = rollback[restore:]
    assert tail.index(
        "Write-InstallProgress -Stage 'failed' -Message $message"
    ) < tail.index("exit 1")


def test_the_sh_rollback_stops_the_started_server_before_it_moves_anything() -> None:
    text = _sh_text()
    body = text[text.index('parse_args "$@"') :]
    rollback = body[body.index("# ROLLBACK. The new version is installed") :]
    stop = rollback.index('stop_started_server "$started_server_pid"')
    refuse = rollback.index(
        'if [ "$stop_started_ok" -ne 1 ] || [ "$stop_started_port_free" -ne 1 ]; then'
    )
    restore = rollback.index('if restore_previous_environment "$staged_tool_dir"; then')
    assert stop < refuse < restore
    assert "exit 1" in rollback[refuse:restore]
    tail = rollback[restore:]
    assert tail.index('write_install_progress failed "$restart_message"') < tail.index(
        "exit 1"
    )


def test_both_rollbacks_refuse_inside_the_function_before_the_first_move() -> None:
    restore = _extract_ps(_ps_text(), "Restore-PreviousEnvironment")
    assert restore.index("if ($null -ne $script:StartedServerChild) {") < restore.index(
        "if ($refusal) {"
    )
    assert restore.index("if ($refusal) {") < restore.index(
        "[System.IO.Directory]::Move("
    )
    assert restore.index(
        "return $false", restore.index("if ($refusal) {")
    ) < restore.index("[System.IO.Directory]::Move(")
    shell = _extract_sh(_sh_text(), "restore_previous_environment")
    assert shell.index(
        'if [ "${run_started_server_pid:-0}" -gt 0 ]; then'
    ) < shell.index('if [ -n "$restore_refusal" ]; then')
    assert shell.index('if [ -n "$restore_refusal" ]; then') < shell.index(" mv ")


def test_the_start_gate_is_the_liveness_gate_and_success_needs_the_version_route() -> (
    None
):
    confirm = _extract_ps(_ps_text(), "Confirm-RestartedServer")
    assert "Wait-ForStartedServer -Url $HealthUrl -Child $Child" in confirm
    assert "Wait-ForServerHealth" not in confirm
    gate = confirm.index("Wait-ForStartedServer")
    serve = confirm.index("$serve = Test-ServerCanServe -HealthUrl $HealthUrl")
    success = confirm.index('is installed and answering on port $Port."')
    assert gate < serve < success
    # No wait anywhere gives up at the old fixed budget any more.
    assert "-BudgetSeconds (Get-ServerStartTimeoutSeconds)" not in _ps_text()
    shell_confirm = _extract_sh(_sh_text(), "confirm_restarted_server")
    assert (
        'wait_for_started_server "$restart_health_url" "$started_server_pid"'
        in shell_confirm
    )
    assert "wait_for_server_health" not in shell_confirm
    assert shell_confirm.index(
        'if ! server_can_serve "$restart_health_url"; then'
    ) < shell_confirm.index('is installed and answering on port $server_port."')
    assert '"$(server_start_budget_seconds)"' not in _sh_text().replace(
        "ceiling_budget=$(server_start_budget_seconds)", ""
    )


def test_neither_swap_runs_while_the_listener_table_still_shows_the_old_server() -> (
    None
):
    main = _ps_main()
    verdict = main.index("$StopOutcome = $stopVerdict.Outcome")
    guard = main.index("-and (-not (Test-ServerPortFree -Address $Address))")
    swap = main.index("$swapped = Invoke-EnvironmentSwap")
    assert verdict < guard < swap
    shell = _sh_text()
    body = shell[shell.index('parse_args "$@"') :]
    shell_guard = body.index(
        'if port_is_occupied "$server_reachable_host" "$server_port"; then\n                    write_install_log "The stop reported the port free'
    )
    shell_swap = body.index('if swap_environment "$staged_tool_dir"; then')
    assert shell_guard < shell_swap
