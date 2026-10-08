"""7.78.11: the installers say what actually happened, and WSL is server only.

Four findings of ``specs/INVESTIGATION-NPM-SUDO.md`` and
``specs/INVESTIGATION-NPM-ROUTE.md``, each RUN here against the real function
bodies, under the real shells:

* **"installed and answering on port N" when it was not.** A user cannot read
  which process owns another user's socket, so with a root server on the port
  (started through ``sudo``) ``--report-holder`` saw nothing, the installer
  printed "Nothing was listening", started a server that abandoned its start,
  and reported the ROOT server's ``/health`` answer as its own success.
  Before the start, one ``/health`` GET now tells "free" from "held by a server
  I cannot see"; after it, an answer counts only when the pid it names
  (``x-mcc-pid``, 7.70.0) is the started server or runs under it.
* **"An update is already running", exit 0, when none was.** A lock folder the
  user cannot write (root-owned, left by an install run as root with the
  user's HOME) read as a lock held by somebody. It now says whose lock it is,
  how old, why it cannot be taken, and exits 1.
* **WSL is server only**, whatever display WSLg reports -- by the product's own
  test: the kernel's release string names Microsoft, or WSL_DISTRO_NAME /
  WSL_INTEROP are set.
* **One Windows Start Menu entry.** The desktop app's setup and
  ``install.ps1 -Desktop`` write the same ``My Claude Code.lnk``; the installer
  now steps aside when it opens the installed app, as install.sh does on Linux
  and macOS.
"""

import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"
INSTALL_PS1 = REPO_ROOT / "scripts" / "install.ps1"

pytestmark = pytest.mark.local_serial


# ------------------------------------------------------------------ helpers


def _sh() -> str:
    return shutil.which("sh") or ""


def _require_sh() -> str:
    shell = _sh()
    if not shell:
        pytest.skip("no POSIX shell")
    return shell


def _sh_text() -> str:
    return INSTALL_SH.read_text(encoding="utf-8").replace("\r\n", "\n")


def _extract_sh(text: str, name: str) -> str:
    # Every function in install.sh closes with a "}" alone in column 0. That,
    # not brace counting, is the boundary: read_update_lock_field's sed
    # pattern holds an unbalanced "}" inside a bracket expression.
    start = text.index(f"\n{name}() {{") + 1
    end = text.index("\n}\n", start)
    return text[start : end + 2]


SH_PREAMBLE = """set -eu
RESTART_AWARE_VERSION="6.73.0"
write_install_log() { printf 'LOG: %s\\n' "$*"; }
write_install_progress() { printf 'PROGRESS: %s\\n' "$1"; }
"""


def _sh_harness(names: tuple[str, ...], body: str) -> str:
    text = _sh_text()
    bodies = "\n\n".join(_extract_sh(text, name) for name in names)
    return f"{SH_PREAMBLE}\n{bodies}\n\n{body}\n"


def _p(path: Path) -> str:
    """A path as the POSIX shell under test spells it."""
    return path.as_posix()


def _run_sh(
    shell: str, script: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [shell, str(script)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=env,
    )


def _powershells() -> list[tuple[str, str]]:
    found = []
    for name, executable in (("ps51", "powershell"), ("pwsh7", "pwsh")):
        resolved = shutil.which(executable)
        if resolved:
            found.append((name, resolved))
    return found


EDITIONS = _powershells() or [("unavailable", "")]

windows_only = pytest.mark.skipif(
    os.name != "nt", reason="PowerShell installer behaviour is a Windows fact"
)


def _require_ps(executable: str) -> None:
    if not executable:
        pytest.skip("no PowerShell on this machine")


def _ps_text() -> str:
    return INSTALL_PS1.read_text(encoding="utf-8").replace("\r\n", "\n")


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


PS_PREAMBLE = """Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$DryRun = $false
function Write-InstallLog { param([string] $Text) Write-Host ("LOG: " + $Text) }
function Write-InstallProgress { param([string] $Stage, [string] $Message) Write-Host ("PROGRESS: " + $Stage) }
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
        timeout=180,
        check=False,
        env=env,
    )


@dataclass
class HealthServer:
    """A /health that answers 200 and names whatever pid the test sets."""

    port: int
    pid_header: list[str]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/health"


@pytest.fixture
def health_server() -> Iterator[HealthServer]:
    pid_header: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if pid_header:
                self.send_header("x-mcc-pid", pid_header[0])
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield HealthServer(port=int(server.server_address[1]), pid_header=pid_header)
    finally:
        server.shutdown()
        server.server_close()


def _closed_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


# ------------------------------------------- "answering" -- before the start


PRE_START_FUNCTIONS = (
    "version_at_least",
    "health_answer",
    "process_owner",
    "port_answerer",
    "stop_hint_for_answerer",
    "stop_configured_server",
)


def _pre_start_body(port: int) -> str:
    # The installed build answers --report-holder and sees NO holder: what a
    # user's build says about a socket another user owns.
    return f"""
ask_the_product_about_the_port() {{
    mcc_holder_pid=0
    mcc_holder_is_server=0
    mcc_holder_description="nothing is listening on that port"
    mcc_holder_reason=""
    mcc_port_free=0
    mcc_stopped=0
    mcc_message=""
    mcc_other_servers=0
    mcc_other_server_lines=""
    if [ "$2" = "--stop-holder" ]; then echo 'nothing may be stopped here' >&2; exit 9; fi
    return 0
}}
report_other_servers() {{ :; }}
install_progress_holder=""
server_reachable_host=127.0.0.1
server_port={port}
stop_configured_server /nowhere/mcc-server 7.78.11
printf 'OUTCOME=%s\\n' "$stop_outcome"
printf 'MESSAGE=%s\\n' "$stop_message"
"""


def test_a_port_answered_by_a_server_the_build_cannot_see_is_not_free(
    tmp_path: Path, health_server: HealthServer
) -> None:
    """The root server case: no holder visible, but /health answers."""
    shell = _require_sh()
    health_server.pid_header.append("424242")
    script = tmp_path / "pre.sh"
    script.write_text(
        _sh_harness(PRE_START_FUNCTIONS, _pre_start_body(health_server.port)),
        encoding="utf-8",
        newline="\n",
    )

    completed = _run_sh(shell, script)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "OUTCOME=held-elsewhere" in completed.stdout
    assert (
        f"MESSAGE=Port {health_server.port} is answered by a My Claude Code server, pid 424242"
        in completed.stdout
    )
    assert "Nothing was stopped and nothing was started." in completed.stdout
    assert "then start yours with: mcc-server" in completed.stdout


def test_a_port_answered_without_a_pid_is_still_not_free(
    tmp_path: Path, health_server: HealthServer
) -> None:
    """An older server names no pid; it is still somebody's listener."""
    shell = _require_sh()
    script = tmp_path / "pre.sh"
    script.write_text(
        _sh_harness(PRE_START_FUNCTIONS, _pre_start_body(health_server.port)),
        encoding="utf-8",
        newline="\n",
    )

    completed = _run_sh(shell, script)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "OUTCOME=held-elsewhere" in completed.stdout
    assert "a server that does not name its process" in completed.stdout


def test_a_port_nobody_answers_on_is_still_free(tmp_path: Path) -> None:
    """Unchanged: a refused connect is "nothing listening", and the start goes ahead."""
    shell = _require_sh()
    script = tmp_path / "pre.sh"
    script.write_text(
        _sh_harness(PRE_START_FUNCTIONS, _pre_start_body(_closed_port())),
        encoding="utf-8",
        newline="\n",
    )

    completed = _run_sh(shell, script)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "OUTCOME=nothing-listening" in completed.stdout


# -------------------------------------------- "answering" -- after the start


POST_START_FUNCTIONS = (
    "health_answer",
    "parent_pid_of",
    "answer_is_from_started_server",
    "process_owner",
    "port_answerer",
    "stop_hint_for_answerer",
    "confirm_restarted_server",
)


def _post_start_body(url: str, port: int, started: str, start_log: Path) -> str:
    return f"""
wait_for_server_health() {{ return 0; }}
server_start_budget_seconds() {{ printf '1'; }}
start_desktop_app_if_wanted() {{ printf 'DESKTOP\\n'; }}
MCC_VERSION=9.9.9
server_port={port}
install_progress_restarted=null
install_progress_holder=""
port_held_by_another=0
restart_start_log='{_p(start_log)}'
started_server_pid={started}
if confirm_restarted_server '{url}' 1; then rc=0; else rc=$?; fi
printf 'RC=%s HELD=%s\\n' "$rc" "$port_held_by_another"
"""


posix_only = pytest.mark.skipif(
    os.name == "nt",
    reason="the parent walk reads /proc or ps for the pid a Windows process answers with",
)


@posix_only
def test_an_answer_from_another_server_is_not_this_installs_success(
    tmp_path: Path, health_server: HealthServer
) -> None:
    """The started server gave up because the port was served; the answer came
    from the other one. That is exit 2 to the caller -- never a rollback --
    and never "installed and answering"."""
    shell = _require_sh()
    # The pid that answers is this test process; the "started" one is a
    # sleeping child of the harness itself, which this process is not under.
    health_server.pid_header.append(str(os.getpid()))
    start_log = tmp_path / "server-start.log"
    start_log.write_text(
        f"Port {health_server.port} is already served by My Claude Code (pid {os.getpid()}).\n",
        encoding="utf-8",
    )
    body = _post_start_body(health_server.url, health_server.port, "$!", start_log)
    body = "sleep 30 &\n" + body + 'kill "$started_server_pid" 2>/dev/null || true\n'
    script = tmp_path / "post.sh"
    script.write_text(
        _sh_harness(POST_START_FUNCTIONS, body), encoding="utf-8", newline="\n"
    )

    completed = _run_sh(shell, script)
    out = completed.stdout

    assert completed.returncode == 0, out + completed.stderr
    assert "RC=2 HELD=1" in out
    assert "installed and answering" not in out
    assert f"is answered by a My Claude Code server, pid {os.getpid()}" in out
    assert "not by the server this install started (pid " in out
    assert "is already served by My Claude Code" in out, (
        "the started server's own words are shown"
    )
    assert "PROGRESS: failed" in out
    assert "DESKTOP" not in out


@posix_only
@pytest.mark.parametrize("relation", ["itself", "its-parent"])
def test_an_answer_from_the_started_server_or_a_process_under_it_is_success(
    tmp_path: Path, health_server: HealthServer, relation: str
) -> None:
    """The started pid itself, or a launcher whose child answers."""
    shell = _require_sh()
    health_server.pid_header.append(str(os.getpid()))
    started = str(os.getpid() if relation == "itself" else os.getppid())
    script = tmp_path / "post.sh"
    script.write_text(
        _sh_harness(
            POST_START_FUNCTIONS,
            _post_start_body(
                health_server.url, health_server.port, started, tmp_path / "start.log"
            ),
        ),
        encoding="utf-8",
        newline="\n",
    )

    completed = _run_sh(shell, script)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "RC=0 HELD=0" in completed.stdout
    assert (
        f"My Claude Code 9.9.9 is installed and answering on port {health_server.port}."
        in completed.stdout
    )
    assert "DESKTOP" in completed.stdout


def test_an_answer_that_names_no_pid_keeps_the_old_reading(
    tmp_path: Path, health_server: HealthServer
) -> None:
    """A server from before 7.70.0 names no pid: nothing can be said against
    it, so the answer is success exactly as it always was."""
    shell = _require_sh()
    script = tmp_path / "post.sh"
    script.write_text(
        _sh_harness(
            POST_START_FUNCTIONS,
            _post_start_body(
                health_server.url, health_server.port, "4242", tmp_path / "start.log"
            ),
        ),
        encoding="utf-8",
        newline="\n",
    )

    completed = _run_sh(shell, script)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "RC=0 HELD=0" in completed.stdout
    assert "is installed and answering on port" in completed.stdout


def test_the_posix_main_flow_never_rolls_back_for_a_port_held_elsewhere() -> None:
    """The wiring, read from the script: status 2 is its own branch BEFORE the
    rollback, and the script ends with exit 1 when the port was not ours."""
    text = _sh_text()
    body = text[text.index('parse_args "$@"') :]
    terminal = body[body.index('elif [ "$staged_swapped" -eq 1 ]; then') :]
    assert terminal.index('elif [ "$staged_confirm" -eq 2 ]; then') < terminal.index(
        "# ROLLBACK."
    )
    assert terminal.index(
        'if confirm_restarted_server "$restart_health_url" 1; then'
    ) < terminal.index('if [ "$stage_may_start" -ne 1 ]; then')
    tail = body[body.rindex("restart_after_install || true") :]
    assert 'if [ "$port_held_by_another" -eq 1 ]; then\n    exit 1\nfi' in tail
    stage = body[body.index('case "$stop_outcome" in') :]
    assert stage.index("held-elsewhere)") < stage.index("            *)")


# ---------------------------------------------------------- the update lock


LOCK_FUNCTIONS = (
    "mcc_config_dir",
    "update_lock_path",
    "read_update_lock_field",
    "enter_update_lock",
    "pid_is_running",
    "path_owner",
    "write_lock_unavailable_notice",
)


def _lock_body(config: Path) -> str:
    return f"""
dry_run=0
holds_update_lock=0
MCC_CONFIG_DIR='{_p(config)}'
if enter_update_lock; then status=0; else status=$?; fi
printf 'STATUS=%s\\n' "$status"
if [ "$status" -eq 2 ]; then write_lock_unavailable_notice; fi
"""


def test_a_lock_that_cannot_be_taken_and_has_no_live_owner_is_not_a_running_update(
    tmp_path: Path,
) -> None:
    """Something that is not a lock file sits where the lock goes and cannot be
    removed -- the portable stand-in for a folder this user cannot write. The
    loop reclaims nothing, and that is status 2, not "already running"."""
    shell = _require_sh()
    config = tmp_path / "config"
    blocker = config / "updates" / "update.lock"
    blocker.mkdir(parents=True)
    (blocker / "inside").write_text("x", encoding="utf-8")
    script = tmp_path / "lock.sh"
    script.write_text(
        _sh_harness(LOCK_FUNCTIONS, _lock_body(config)), encoding="utf-8", newline="\n"
    )

    completed = _run_sh(shell, script)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "STATUS=2" in completed.stdout
    assert (
        "The update lock could not be taken, and no running update holds it."
        in completed.stderr
    )
    assert "sudo chown -R" in completed.stderr
    assert "already running" not in completed.stdout + completed.stderr


@posix_only
def test_a_leftover_lock_in_an_unwritable_folder_says_whose_and_how_old(
    tmp_path: Path,
) -> None:
    """Case a2 of the sudo investigation, with the lock root's run left behind."""
    if os.geteuid() == 0:
        pytest.skip("root writes through any mode bits")
    shell = _require_sh()
    config = tmp_path / "config"
    updates = config / "updates"
    updates.mkdir(parents=True)
    (updates / "update.lock").write_text(
        json.dumps(
            {
                "pid": 999999,
                "started_at": int(time.time()) - 600,
                "started_display": "10:00:00",
                "source": "install.sh",
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    updates.chmod(0o555)
    script = tmp_path / "lock.sh"
    script.write_text(
        _sh_harness(LOCK_FUNCTIONS, _lock_body(config)), encoding="utf-8", newline="\n"
    )
    try:
        completed = _run_sh(shell, script)
    finally:
        updates.chmod(0o755)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "STATUS=2" in completed.stdout
    err = completed.stderr
    assert f"Lock: {_p(updates / 'update.lock')}" in err
    assert (
        "written by install.sh, pid 999999 (no longer running), written 10 min ago"
        in err
    )
    assert "is not writable by" in err


def test_a_live_owner_is_still_an_update_already_running(tmp_path: Path) -> None:
    """Unchanged: a live owner is believed (status 1), and the caller watches."""
    shell = _require_sh()
    config = tmp_path / "config"
    (config / "updates").mkdir(parents=True)
    body = f"""
sleep 30 &
owner=$!
mkdir -p '{_p(config / "updates")}'
printf '{{"pid":%s,"started_at":1,"source":"install.ps1"}}' "$owner" > '{_p(config / "updates" / "update.lock")}'
{_lock_body(config)}
kill "$owner" 2>/dev/null || true
"""
    script = tmp_path / "lock.sh"
    script.write_text(_sh_harness(LOCK_FUNCTIONS, body), encoding="utf-8", newline="\n")

    completed = _run_sh(shell, script)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "STATUS=1" in completed.stdout
    assert "could not be taken" not in completed.stderr


def test_the_posix_main_flow_exits_1_for_an_untakeable_lock_and_0_for_a_live_one() -> (
    None
):
    text = _sh_text()
    body = text[text.index('parse_args "$@"') :]
    gate = body[
        body.index("if enter_update_lock; then") : body.index(
            "# What this run will do about the server, in one line"
        )
    ]
    assert gate.index('if [ "$lock_status" -eq 2 ]; then') < gate.index(
        "write_watching_instead_notice"
    )
    assert "write_lock_unavailable_notice\n        exit 1" in gate
    assert "write_watching_instead_notice\n    exit 0" in gate


# ------------------------------------------------------------------ WSL (sh)


def _wsl_body(osrelease: Path) -> str:
    return f"""
WSL_OSRELEASE_PATH='{_p(osrelease)}'
if running_under_wsl; then echo WSL=yes; else echo WSL=no; fi
"""


@pytest.mark.parametrize(
    ("kernel", "distro", "expected"),
    [
        ("6.6.87.2-microsoft-standard-WSL2\n", "", "yes"),
        ("4.4.0-19041-Microsoft\n", "", "yes"),
        ("6.8.0-1021-azure\n", "", "no"),
        ("6.8.0-1021-azure\n", "Ubuntu", "yes"),
    ],
    ids=["wsl2-kernel", "wsl1-kernel", "plain-linux", "wsl-env-only"],
)
def test_wsl_is_recognised_the_way_the_product_recognises_it(
    tmp_path: Path, kernel: str, distro: str, expected: str
) -> None:
    """config/paths.py: "microsoft" in /proc/sys/kernel/osrelease; the sign-in
    flows: WSL_DISTRO_NAME / WSL_INTEROP. sudo strips the variables, not the
    kernel string."""
    shell = _require_sh()
    osrelease = tmp_path / "osrelease"
    osrelease.write_text(kernel, encoding="utf-8")
    script = tmp_path / "wsl.sh"
    script.write_text(
        _sh_harness(("running_under_wsl",), _wsl_body(osrelease)),
        encoding="utf-8",
        newline="\n",
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"WSL_DISTRO_NAME", "WSL_INTEROP"}
    }
    if distro:
        env["WSL_DISTRO_NAME"] = distro

    completed = _run_sh(shell, script, env=env)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert f"WSL={expected}" in completed.stdout


# --------------------------------------------------- the update lock (ps1)


PS_LOCK_FUNCTIONS = (
    "Get-MccConfigDir",
    "Get-UpdateLockPath",
    "Read-UpdateLockOwner",
    "Test-UpdateLockOwnerAlive",
    "Enter-UpdateLock",
    "Get-PathOwner",
    "Write-UpdateLockUnavailableNotice",
)


@windows_only
@pytest.mark.parametrize(("edition", "executable"), EDITIONS, ids=lambda v: v)
def test_ps1_a_lock_that_cannot_be_taken_and_has_no_live_owner_says_so(
    edition: str, executable: str, tmp_path: Path
) -> None:
    """The PowerShell twin. ``CreateNew`` fails, the leftover cannot be
    removed, nobody alive owns it: not "an update is already running"."""
    _require_ps(executable)
    config = tmp_path / edition
    blocker = config / "updates" / "update.lock"
    blocker.mkdir(parents=True)
    (blocker / "inside").write_text("x", encoding="utf-8")
    body = f"""
$env:MCC_CONFIG_DIR = '{config}'
$script:HoldsUpdateLock = $false
$script:UpdateLockPath = ""
$script:UpdateLockOwner = $null
$script:UpdateLockUnavailable = $false
if (Enter-UpdateLock) {{ Write-Output 'TOOK' }}
elseif ($script:UpdateLockUnavailable) {{
    Write-UpdateLockUnavailableNotice -Owner $script:UpdateLockOwner
    Write-Output 'UNAVAILABLE'
}}
else {{ Write-Output 'HELD' }}
"""
    script = tmp_path / f"lock-{edition}.ps1"
    script.write_text(_ps_harness(PS_LOCK_FUNCTIONS, body), encoding="utf-8")

    completed = _run_ps(executable, script)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "UNAVAILABLE" in completed.stdout
    assert (
        "The update lock could not be taken, and no running update holds it."
        in completed.stdout
    )
    assert f"Lock: {blocker}" in completed.stdout
    assert "Nothing was installed." in completed.stdout
    assert "already running" not in completed.stdout


@windows_only
@pytest.mark.parametrize(("edition", "executable"), EDITIONS, ids=lambda v: v)
def test_ps1_a_live_owner_is_still_an_update_already_running(
    edition: str, executable: str, tmp_path: Path
) -> None:
    _require_ps(executable)
    config = tmp_path / edition
    (config / "updates").mkdir(parents=True)
    (config / "updates" / "update.lock").write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "started_at": 1,
                "started_display": "10:00:00",
                "source": "the dashboard update",
            }
        ),
        encoding="utf-8",
    )
    body = f"""
$env:MCC_CONFIG_DIR = '{config}'
$script:HoldsUpdateLock = $false
$script:UpdateLockPath = ""
$script:UpdateLockOwner = $null
$script:UpdateLockUnavailable = $false
if (Enter-UpdateLock) {{ Write-Output 'TOOK' }}
elseif ($script:UpdateLockUnavailable) {{ Write-Output 'UNAVAILABLE' }}
else {{ Write-Output 'HELD' }}
"""
    script = tmp_path / f"live-{edition}.ps1"
    script.write_text(_ps_harness(PS_LOCK_FUNCTIONS, body), encoding="utf-8")

    completed = _run_ps(executable, script)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "HELD" in completed.stdout
    assert "UNAVAILABLE" not in completed.stdout


def test_the_ps1_main_flow_exits_1_for_an_untakeable_lock_and_returns_for_a_live_one() -> (
    None
):
    text = _ps_text()
    gate = text[text.index("if (-not (Enter-UpdateLock)) {") :]
    gate = gate[: gate.index("\n}\n") + 3]
    assert gate.index("if ($script:UpdateLockUnavailable) {") < gate.index(
        "Write-WatchingInsteadNotice"
    )
    assert (
        "Write-UpdateLockUnavailableNotice -Owner $script:UpdateLockOwner\n        exit 1"
        in gate
    )


# ------------------------------------------------ "answering" (ps1), RUN


PS_CONFIRM_FUNCTIONS = (
    "Get-HealthAnswerPid",
    "Test-AnswerIsFromStartedServer",
    "Get-ChildFailureDetail",
    "Confirm-RestartedServer",
)


def _ps_confirm_body(url: str, port: int, started: str, tmp_path: Path) -> str:
    return f"""
function Wait-ForServerHealth {{ param([string] $Url, [double] $BudgetSeconds) return $true }}
function Get-ServerStartTimeoutSeconds {{ return 1 }}
function Start-DesktopAppIfWanted {{ Write-Host 'DESKTOP'; return $true }}
$script:PortHeldByAnother = $false
$script:InstallProgressRestarted = $null
$script:InstallProgressHolder = ""
$child = [pscustomobject]@{{ Id = {started}; StdOut = '{tmp_path / "out.log"}'; StdErr = '{tmp_path / "err.log"}' }}
$ok = Confirm-RestartedServer -Child $child -InstalledVersion '9.9.9' -HealthUrl '{url}' -Port {port}
Write-Output ("OK=" + $ok + " HELD=" + $script:PortHeldByAnother)
"""


@windows_only
@pytest.mark.parametrize(("edition", "executable"), EDITIONS, ids=lambda v: v)
def test_ps1_an_answer_from_another_server_is_not_this_installs_success(
    edition: str, executable: str, tmp_path: Path, health_server: HealthServer
) -> None:
    """The answer names this test process; the "started" server is the
    harness PowerShell itself, which this process is not under."""
    _require_ps(executable)
    health_server.pid_header.append(str(os.getpid()))
    script = tmp_path / f"confirm-{edition}.ps1"
    script.write_text(
        _ps_harness(
            PS_CONFIRM_FUNCTIONS,
            _ps_confirm_body(health_server.url, health_server.port, "$PID", tmp_path),
        ),
        encoding="utf-8",
    )

    completed = _run_ps(executable, script)
    out = completed.stdout

    assert completed.returncode == 0, out + completed.stderr
    assert "OK=False HELD=True" in out
    assert "installed and answering" not in out
    assert (
        f"is answered by a My Claude Code server, pid {os.getpid()}, not by the server this install started"
        in out
    )
    assert "PROGRESS: failed" in out
    assert "DESKTOP" not in out


@windows_only
@pytest.mark.parametrize(("edition", "executable"), EDITIONS, ids=lambda v: v)
@pytest.mark.parametrize("relation", ["itself", "its-parent", "no-pid-named"])
def test_ps1_an_answer_from_the_started_server_or_under_it_is_success(
    edition: str,
    executable: str,
    relation: str,
    tmp_path: Path,
    health_server: HealthServer,
) -> None:
    """The started pid, a launcher whose child answers (the .cmd ->
    trampoline -> python.exe chain), and a pre-7.70.0 server naming no pid."""
    _require_ps(executable)
    if relation != "no-pid-named":
        health_server.pid_header.append(str(os.getpid()))
    started = str(os.getppid() if relation == "its-parent" else os.getpid())
    script = tmp_path / f"confirm-{edition}.ps1"
    script.write_text(
        _ps_harness(
            PS_CONFIRM_FUNCTIONS,
            _ps_confirm_body(health_server.url, health_server.port, started, tmp_path),
        ),
        encoding="utf-8",
    )

    completed = _run_ps(executable, script)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "OK=True HELD=False" in completed.stdout
    assert (
        f"My Claude Code 9.9.9 is installed and answering on port {health_server.port}."
        in completed.stdout
    )


def test_the_ps1_staged_path_never_rolls_back_for_a_port_held_elsewhere() -> None:
    text = _ps_text()
    terminal = text[text.index("elseif ($script:StagedSwapped) {") :]
    assert terminal.index("elseif ($script:PortHeldByAnother) {") < terminal.index(
        "# ROLLBACK."
    )
    tail = text[
        text.rindex("$null = Invoke-RestartAfterInstall") : text.rindex("finally {")
    ]
    assert "if ($script:PortHeldByAnother) {\n    exit 1\n}" in tail


# ---------------------------------------------- the Start Menu entry (ps1)


SHORTCUT_FUNCTIONS = (
    "Get-ShortcutTarget",
    "Test-ShortcutOpensDesktopApp",
    "New-DesktopShortcut",
)


def _shortcut_body(
    app_data: Path, config: Path, launcher: Path, existing_target: str
) -> str:
    seed = ""
    if existing_target:
        seed = f"""
$seedShell = New-Object -ComObject WScript.Shell
$seed = $seedShell.CreateShortcut($shortcutPath)
$seed.TargetPath = '{existing_target}'
$seed.Save()
"""
    return f"""
$env:APPDATA = '{app_data}'
$script:EnableDesktop = $true
$script:DesktopShortcutPath = ""
$script:DesktopShortcutError = ""
function Write-Step {{ param([string] $Text) Write-Host ("STEP: " + $Text) }}
function Get-ApplicationCommand {{ param([string] $Name) return [pscustomobject]@{{ Source = '{launcher}' }} }}
function Get-MccConfigDir {{ return '{config}' }}
# The icon export would run the launcher; this one "fails", which the real
# function survives by design (a shortcut with the default icon).
function Start-Process {{ return [pscustomobject]@{{ ExitCode = 1 }} }}
$startMenu = Join-Path $env:APPDATA 'Microsoft\\Windows\\Start Menu\\Programs'
New-Item -ItemType Directory -Path $startMenu -Force | Out-Null
$shortcutPath = Join-Path $startMenu 'My Claude Code.lnk'
{seed}
New-DesktopShortcut
$check = New-Object -ComObject WScript.Shell
Write-Output ("TARGET=" + $check.CreateShortcut($shortcutPath).TargetPath)
Write-Output ("RECORDED=" + $script:DesktopShortcutPath)
"""


@windows_only
@pytest.mark.parametrize(("edition", "executable"), EDITIONS, ids=lambda v: v)
@pytest.mark.parametrize(
    "existing",
    ["installed-app", "nothing", "our-own", "uninstalled-app"],
)
def test_ps1_desktop_shortcut_steps_aside_only_for_the_installed_app(
    edition: str, executable: str, existing: str, tmp_path: Path
) -> None:
    """The Start Menu entry is the app's while the app is installed; every
    other case writes (or rewrites) the mcc-desktop launcher as before."""
    _require_ps(executable)
    app_data = tmp_path / "appdata"
    config = tmp_path / "config"
    app_data.mkdir()
    config.mkdir()
    launcher = tmp_path / "bin" / "mcc-desktop.exe"
    launcher.parent.mkdir()
    launcher.write_bytes(b"fake")
    app = tmp_path / "Programs" / "My Claude Code" / "MyClaudeCode.exe"
    app.parent.mkdir(parents=True)
    if existing != "uninstalled-app":
        app.write_bytes(b"fake app")
    seeded = {
        "installed-app": str(app),
        "uninstalled-app": str(app),
        "our-own": str(launcher),
        "nothing": "",
    }[existing]
    script = tmp_path / f"shortcut-{edition}.ps1"
    script.write_text(
        _ps_harness(
            SHORTCUT_FUNCTIONS, _shortcut_body(app_data, config, launcher, seeded)
        ),
        encoding="utf-8",
    )

    completed = _run_ps(executable, script)
    out = completed.stdout

    assert completed.returncode == 0, out + completed.stderr
    shortcut = (
        app_data
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "My Claude Code.lnk"
    )
    assert f"RECORDED={shortcut}" in out
    if existing == "installed-app":
        assert f"TARGET={app}" in out, (
            "the installed app's Start Menu entry was replaced"
        )
        assert "not replacing it with a second launcher" in out
    else:
        assert f"TARGET={launcher}" in out
        assert "Created Start Menu shortcut:" in out
        assert "not replacing it" not in out


def _code_lines(text: str, name: str) -> list[str]:
    """A function's statements, without its comments or help block."""
    body = re.sub(r"<#.*?#>", "", _extract_ps(text, name), flags=re.S)
    return [
        line.strip()
        for line in body.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_install_ps1_and_uninstall_ps1_tell_the_shortcuts_apart_the_same_way() -> None:
    """One rule, two scripts: the installer's step-aside and the uninstaller's
    keep must read a shortcut identically, or one of them strands the other."""
    install = _ps_text()
    uninstall = (
        (REPO_ROOT / "scripts" / "uninstall.ps1")
        .read_text(encoding="utf-8")
        .replace("\r\n", "\n")
    )

    for name in ("Get-ShortcutTarget", "Test-ShortcutOpensDesktopApp"):
        assert _code_lines(install, name) == _code_lines(uninstall, name), name
    # The name the app's setup gives its executable (MyClaudeCode.iss AppExeName).
    iss = (
        REPO_ROOT / "desktop-shell" / "installer" / "windows" / "MyClaudeCode.iss"
    ).read_text(encoding="utf-8")
    assert '#define AppExeName "MyClaudeCode.exe"' in iss
    assert '"MyClaudeCode.exe"' in _extract_ps(install, "Test-ShortcutOpensDesktopApp")
    assert 'Name: "{autoprograms}\\{#AppName}"' in iss, (
        "the setup's Start Menu entry moved"
    )
