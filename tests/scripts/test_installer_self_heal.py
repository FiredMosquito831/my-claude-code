"""7.78.9: the installer never guts a working install, never crashes on a broken
one, and repairs a broken one by itself.

On 2026-10-08 a dashboard Update took the reporter's server down and the
installer could not bring it back:

1. ``Get-MissingLauncherShim`` compared uv's bin directory with the
   environment's ``Scripts\\`` directory, which also holds every DEPENDENCY's
   console scripts (fastapi, uvicorn, typer, ...). It reported "This release
   adds ..." on every update, so every update ran ``uv tool install --force``
   straight after a successful swap. With the desktop app holding a file that
   ``--force`` failed half way and left ``tools\\my-claude-code`` a HUSK holding
   only ``Scripts\\pythonw.exe``; the failure was swallowed and a server was
   "started" from the husk.
2. Every re-run then died before the swap that would have repaired it:
   ``Get-InstalledServerVersion`` returns "" when the launcher cannot run, and
   ``Stop-ConfiguredServer``'s Mandatory ``[string] $LauncherVersion`` refused
   "" before its body (which handles an unknown version) ever ran.

These tests RUN the real PowerShell, both editions, and assert on the lines the
installer itself prints. The full-run cases drive the WHOLE script -- the
published scriptblock form -- with only the network and process-table seams
replaced, against a fake ``uv`` and fake launchers that behave like uv's
trampolines: a launcher in the bin directory runs exactly
``<tool dir>\\Scripts\\python.exe`` and fails with uv's own "failed to
canonicalize script path" when it is missing.
"""

import os
import re
import shutil
import socket
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_PS1 = REPO_ROOT / "scripts" / "install.ps1"

pytestmark = [
    pytest.mark.skipif(
        os.name != "nt", reason="the PowerShell installer is a Windows fact"
    ),
    # Every case launches PowerShell; the full runs also serve /health.
    pytest.mark.local_serial,
]

OLD_VERSION = "9.9.8"
NEW_VERSION = "9.9.9"


def _powershells() -> list[tuple[str, str]]:
    found = []
    for name, executable in (("ps51", "powershell"), ("pwsh7", "pwsh")):
        resolved = shutil.which(executable)
        if resolved:
            found.append((name, resolved))
    return found


EDITIONS = _powershells() or [("unavailable", "")]


def _require(executable: str) -> None:
    if not executable:
        pytest.skip("no PowerShell on this machine")


def _extract_function(text: str, name: str) -> str:
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


def _installer_text() -> str:
    return INSTALL_PS1.read_text(encoding="utf-8").replace("\r\n", "\n")


PREAMBLE = """Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$DryRun = $false
$StagingEnvDirName = ".mcc-staging"
$PreviousEnvDirName = ".mcc-previous"
$PreviousEnvsKept = 1
$PackageEnvDirName = "my-claude-code"
$RestartAwareVersion = "6.73.0"
$script:RunStamp = ""
$script:UvFailureDetail = @()
function Write-InstallLog { param([string] $Text) Write-Host ("LOG: " + $Text) }
function Write-InstallProgress { param([string] $Stage, [string] $Message) Write-Host ("PROGRESS: " + $Stage) }
"""


def _function_harness(names: tuple[str, ...], body: str, *, text: str = "") -> str:
    source = text or _installer_text()
    bodies = "\n\n".join(_extract_function(source, name) for name in names)
    return f"{PREAMBLE}\n{bodies}\n\n{body}\n"


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


# ------------------------------------------------------------ defect 1, RUN


STOP_FUNCTIONS = ("Stop-ConfiguredServer", "Test-VersionAtLeast")
STOP_STUBS = """
function Test-PortIsOccupied { param([string] $ReachableHost, [int] $Port) return ($env:FAKE_PORT_BUSY -eq '1') }
function Get-PortHolderDocument { throw 'an unknown version must never be asked --report-holder' }
function Stop-PortHolderServer { throw 'nothing may be stopped here' }
function Write-OtherServerReport { param([object] $Document) }
$address = [pscustomobject]@{ ReachableHost = '127.0.0.1'; Port = 18999 }
$verdict = Stop-ConfiguredServer -Launcher 'C:\\nowhere\\mcc-server.exe' -LauncherVersion '' -Address $address
Write-Host ('OUTCOME=' + $verdict.Outcome)
Write-Host ('MESSAGE=' + $verdict.Message)
"""


@pytest.mark.parametrize(("edition", "executable"), EDITIONS, ids=lambda v: v)
@pytest.mark.parametrize(
    ("busy", "outcome"),
    [("0", "nothing-listening"), ("1", "unclassifiable")],
    ids=["port-free", "port-busy"],
)
def test_an_empty_launcher_version_takes_the_version_unknown_branch(
    edition: str, executable: str, busy: str, outcome: str, tmp_path: Path
) -> None:
    """``Stop-ConfiguredServer -LauncherVersion ''`` runs its body.

    The body has always handled an unknown version: it does not ask a build it
    cannot identify the port question (an old build answers it by STARTING a
    server) and falls back to "is the port free at all". The parameter binder
    used to refuse "" before that body ran.
    """

    _require(executable)
    script = tmp_path / f"stop-{edition}.ps1"
    script.write_text(_function_harness(STOP_FUNCTIONS, STOP_STUBS), encoding="utf-8")
    completed = _run_ps(executable, script, env=os.environ | {"FAKE_PORT_BUSY": busy})

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "because it is an empty string" not in completed.stderr
    assert f"OUTCOME={outcome}" in completed.stdout, completed.stdout
    assert (
        "LOG: The installed mcc-server (version unknown) predates --report-holder; "
        "not asking it."
    ) in completed.stdout


@pytest.mark.parametrize(("edition", "executable"), EDITIONS, ids=lambda v: v)
def test_the_7_78_8_parameter_really_crashed_on_an_empty_version(
    edition: str, executable: str, tmp_path: Path
) -> None:
    """The control: the same call against the 7.78.8 declaration fails.

    Proves the case above detects the defect -- with ``[AllowEmptyString()]``
    taken out again, the binder throws the exact error the reporter's two
    16:12 runs died with, and the body never runs.
    """

    _require(executable)
    text = _installer_text()
    declaration = (
        "[Parameter(Mandatory = $true)][AllowEmptyString()][string] $LauncherVersion"
    )
    assert declaration in text
    reverted = text.replace(
        declaration, "[Parameter(Mandatory = $true)][string] $LauncherVersion"
    )
    script = tmp_path / f"stop-reverted-{edition}.ps1"
    script.write_text(
        _function_harness(STOP_FUNCTIONS, STOP_STUBS, text=reverted), encoding="utf-8"
    )
    completed = _run_ps(executable, script, env=os.environ | {"FAKE_PORT_BUSY": "0"})

    assert completed.returncode != 0
    assert "Cannot bind argument to parameter 'LauncherVersion'" in completed.stderr
    assert "because it is an empty string" in completed.stderr
    assert "OUTCOME=" not in completed.stdout


@pytest.mark.parametrize(("edition", "executable"), EDITIONS, ids=lambda v: v)
def test_a_launcher_that_cannot_run_reports_no_version_and_says_why(
    edition: str, executable: str, tmp_path: Path
) -> None:
    """ "" is a legitimate answer, but the reason is now in the transcript."""

    _require(executable)
    launcher = tmp_path / "mcc-server.cmd"
    launcher.write_text(
        "@echo off\r\necho error: uv trampoline failed to canonicalize script path 1>&2\r\nexit /b 1\r\n",
        encoding="utf-8",
    )
    script = tmp_path / f"version-{edition}.ps1"
    script.write_text(
        _function_harness(
            ("Get-InstalledServerVersion", "Convert-OutputLine"),
            f"$v = Get-InstalledServerVersion -Launcher '{launcher}'\n"
            "Write-Host ('VERSION=<' + $v + '>')\n",
        ),
        encoding="utf-8",
    )
    completed = _run_ps(executable, script)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "VERSION=<>" in completed.stdout
    assert (
        "LOG: The installed mcc-server could not report its version (exit 1): "
        "error: uv trampoline failed to canonicalize script path"
    ) in completed.stdout


# ------------------------------------------------------------ defect 2, RUN

DEPENDENCY_SCRIPTS = ("fastapi", "uvicorn", "typer", "tqdm", "ddgs", "normalizer")
SHIM_FUNCTIONS = (
    "Get-MissingLauncherShim",
    "Get-PackageEntryPointName",
    "Test-ProductCommandName",
)


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MZ placeholder")


def _receipt(names: list[str], bin_dir: Path) -> str:
    entries = "".join(
        f'    {{ name = "{name}", install-path = "{(bin_dir / (name + ".exe")).as_posix()}", from = "my-claude-code" }},\n'
        for name in names
    )
    return (
        '[tool]\nrequirements = [{ name = "my-claude-code" }]\n'
        f"entrypoints = [\n{entries}]\n"
    )


@pytest.mark.parametrize(("edition", "executable"), EDITIONS, ids=lambda v: v)
def test_dependency_console_scripts_are_never_a_missing_launcher(
    edition: str, executable: str, tmp_path: Path
) -> None:
    """A Scripts dir full of dependency exes reports NOTHING missing.

    This is the false "This release adds agent-detector, cffi-gen-src, ddgs,
    ..." that ran the destructive in-place finish after every swap. A real new
    entry point -- in the staging bin directory, or in the receipt -- is still
    reported, so a release that adds a command is still finished.
    """

    _require(executable)
    bin_dir = tmp_path / "bin"
    env_dir = tmp_path / "tools" / "my-claude-code"
    scripts = env_dir / "Scripts"
    for name in (
        "python",
        "pythonw",
        "pip",
        *DEPENDENCY_SCRIPTS,
        "mcc-server",
        "mcc-claude",
    ):
        _touch(scripts / f"{name}.exe")
    for name in ("mcc-server", "mcc-claude"):
        _touch(bin_dir / f"{name}.exe")
    bare_env = tmp_path / "bare" / "my-claude-code"
    for name in ("python", *DEPENDENCY_SCRIPTS, "mcc-server"):
        _touch(bare_env / "Scripts" / f"{name}.exe")
    staging_bin = tmp_path / "staging" / ".bin"
    for name in ("mcc-server", "mcc-claude", "mcc-newcmd"):
        _touch(staging_bin / f"{name}.exe")
    adds_env = tmp_path / "adds" / "my-claude-code"
    for name in (
        "python",
        *DEPENDENCY_SCRIPTS,
        "mcc-server",
        "mcc-claude",
        "mcc-newcmd",
    ):
        _touch(adds_env / "Scripts" / f"{name}.exe")
    (env_dir / "uv-receipt.toml").write_text(
        _receipt(["mcc-server", "mcc-claude"], bin_dir), encoding="utf-8"
    )
    (adds_env / "uv-receipt.toml").write_text(
        _receipt(["mcc-server", "mcc-claude", "mcc-newcmd"], bin_dir), encoding="utf-8"
    )

    body = "\n".join(
        f"Write-Host ('{label}=' + (@(Get-MissingLauncherShim -BinDir '{bin_dir}' -StagingBinOrEnvScripts '{source}') -join ','))"
        for label, source in (
            ("ENV_WITH_RECEIPT", scripts),
            ("ENV_WITHOUT_RECEIPT", bare_env / "Scripts"),
            ("STAGING_BIN", staging_bin),
            ("ENV_ADDING_A_COMMAND", adds_env / "Scripts"),
        )
    )
    script = tmp_path / f"shims-{edition}.ps1"
    script.write_text(_function_harness(SHIM_FUNCTIONS, body), encoding="utf-8")
    completed = _run_ps(executable, script)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    lines = completed.stdout.splitlines()
    assert "ENV_WITH_RECEIPT=" in lines, completed.stdout
    assert "ENV_WITHOUT_RECEIPT=" in lines, completed.stdout
    assert "STAGING_BIN=mcc-newcmd" in lines, completed.stdout
    assert "ENV_ADDING_A_COMMAND=mcc-newcmd" in lines, completed.stdout
    for dependency in DEPENDENCY_SCRIPTS:
        assert dependency not in completed.stdout


# ------------------------------------------------------------ defect 3/4, RUN

HEAL_FUNCTIONS = (
    "Get-ToolEnvironmentProblem",
    "Move-EnvironmentAside",
    "Get-UpdateAsideRoot",
    "Remove-EmptyDirectory",
    "Convert-OutputLine",
)


@pytest.mark.parametrize(("edition", "executable"), EDITIONS, ids=lambda v: v)
def test_a_husk_is_found_and_moved_beside_the_tools_root(
    edition: str, executable: str, tmp_path: Path
) -> None:
    """The reporter's husk -- only ``Scripts\\pythonw.exe`` -- is broken; a whole
    environment is not; none at all is "not installed", not broken. The husk
    goes to ``<tools root>\\..\\.mcc-staging\\<stamp>-broken``: a SIBLING of uv's
    tools root, where ``uv tool list`` never sees it."""

    _require(executable)
    tools = tmp_path / "uv" / "tools"
    husk = tools / "my-claude-code"
    _touch(husk / "Scripts" / "pythonw.exe")
    whole = tmp_path / "whole" / "tools" / "my-claude-code"
    _touch(whole / "Scripts" / "python.exe")
    (whole / "uv-receipt.toml").write_text("[tool]\n", encoding="utf-8")
    unparseable = tmp_path / "unparseable" / "tools" / "my-claude-code"
    _touch(unparseable / "Scripts" / "python.exe")
    (unparseable / "uv-receipt.toml").write_text("[tool\n", encoding="utf-8")
    fake_uv = tmp_path / "uv.cmd"
    fake_uv.write_text(
        "@echo off\r\n"
        "echo warning: Ignoring malformed tool `my-claude-code` (run `uv tool uninstall my-claude-code` to remove) 1>&2\r\n"
        "exit /b 0\r\n",
        encoding="utf-8",
    )
    body = f"""
Write-Host ('HUSK=' + (Get-ToolEnvironmentProblem -ToolDir '{husk}'))
Write-Host ('WHOLE=' + (Get-ToolEnvironmentProblem -ToolDir '{whole}'))
Write-Host ('ABSENT=' + (Get-ToolEnvironmentProblem -ToolDir '{tmp_path / "absent" / "my-claude-code"}'))
Write-Host ('UNPARSEABLE=' + (Get-ToolEnvironmentProblem -ToolDir '{unparseable}' -UvPath '{fake_uv}'))
$moved = Move-EnvironmentAside -SourceDir '{husk}' -ToolsRoot '{tools}' -Stamp '20261008-161206' -Label 'broken'
Write-Host ('MOVED=' + $moved.Moved)
Write-Host ('DEST=' + $moved.Destination)
"""
    script = tmp_path / f"husk-{edition}.ps1"
    script.write_text(_function_harness(HEAL_FUNCTIONS, body), encoding="utf-8")
    completed = _run_ps(executable, script)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    out = completed.stdout
    assert "HUSK=it has no Scripts\\python.exe; it has no uv-receipt.toml" in out, out
    assert "WHOLE=\n" in out.replace("\r\n", "\n"), out
    assert "ABSENT=\n" in out.replace("\r\n", "\n"), out
    assert "UNPARSEABLE=uv tool list reports it as malformed" in out, out
    assert "MOVED=True" in out
    destination = (
        tmp_path / "uv" / ".mcc-staging" / "20261008-161206-broken" / "my-claude-code"
    )
    assert f"DEST={destination}" in out, out
    assert (destination / "Scripts" / "pythonw.exe").is_file()
    assert not husk.exists()
    # Nothing was left INSIDE uv's tools root under any name.
    assert list(tools.iterdir()) == []


SWEEP_FUNCTIONS = (
    "Remove-UpdateLeftover",
    "Remove-StalePreviousEnvironment",
    "Test-EnvironmentInUse",
    "Get-UpdateAsideRoot",
)


@pytest.mark.parametrize(("edition", "executable"), EDITIONS, ids=lambda v: v)
def test_the_sweep_keeps_this_run_one_previous_and_anything_in_use(
    edition: str, executable: str, tmp_path: Path
) -> None:
    """Orphans from failed runs go; this run's own leftovers stay for one run;
    one previous environment stays as the rollback; an environment a process
    still runs from is never deleted down to its locked files."""

    _require(executable)
    root = tmp_path / "uv"
    tools = root / "tools"
    tools.mkdir(parents=True)
    staging = root / ".mcc-staging"
    previous = root / ".mcc-previous"
    _touch(staging / "20261008-160332" / "my-claude-code" / "Scripts" / "python.exe")
    _touch(staging / "20261008-160415" / "my-claude-code" / "pyvenv.cfg")
    held = staging / "20261008-161208" / "my-claude-code" / "Scripts" / "python.exe"
    _touch(held)
    _touch(
        staging
        / "20261008-170000-failed"
        / "my-claude-code"
        / "Scripts"
        / "pythonw.exe"
    )
    for stamp in ("20260928-113903", "20261003-221036", "20261008-160332"):
        _touch(previous / stamp / "my-claude-code" / "Scripts" / "python.exe")

    body = f"""
$holder = [System.IO.File]::Open('{held}', [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
try {{
    Remove-UpdateLeftover -ToolsRoot '{tools}' -KeepPrefix '20261008-170000' -Keep 1
}}
finally {{
    $holder.Dispose()
}}
Write-Host 'SWEPT'
"""
    script = tmp_path / f"sweep-{edition}.ps1"
    script.write_text(_function_harness(SWEEP_FUNCTIONS, body), encoding="utf-8")
    completed = _run_ps(executable, script)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "SWEPT" in completed.stdout
    assert sorted(p.name for p in staging.iterdir()) == [
        "20261008-161208",
        "20261008-170000-failed",
    ]
    assert sorted(p.name for p in previous.iterdir()) == ["20261008-160332"]
    assert (
        f"LOG: Kept {staging / '20261008-161208'}: a process is running out of it."
        in (completed.stdout)
    )
    assert (
        f"LOG: Removed the leftover {staging / '20261008-160332'}." in completed.stdout
    )


# ---------------------------------------------------------------- full runs

FAKE_LAUNCHER_CS = r"""
using System;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;

static class FakeLauncher
{
    static string Env(string name)
    {
        string value = Environment.GetEnvironmentVariable(name);
        return value == null ? "" : value;
    }

    static void Log(string line)
    {
        string log = Env("CALL_LOG");
        if (log.Length == 0) return;
        for (int i = 0; i < 40; i++)
        {
            try { File.AppendAllText(log, line + Environment.NewLine); return; }
            catch (IOException) { Thread.Sleep(25); }
        }
    }

    static int Main(string[] args)
    {
        string self = System.Diagnostics.Process.GetCurrentProcess().MainModule.FileName;
        string name = Path.GetFileNameWithoutExtension(self).ToLowerInvariant();
        string dir = Path.GetDirectoryName(self);
        bool inScripts = string.Equals(Path.GetFileName(dir), "Scripts", StringComparison.OrdinalIgnoreCase);
        string envDir = inScripts ? Path.GetDirectoryName(dir) : Path.Combine(Env("FAKE_TOOLS_ROOT"), "my-claude-code");
        Log(name + ":" + string.Join(" ", args));

        if (name == "python" || name == "pythonw")
        {
            if (args.Length > 1 && args[0] == "--fake-hold")
            {
                DateTime until = DateTime.UtcNow.AddSeconds(90);
                while (DateTime.UtcNow < until && !File.Exists(args[1])) Thread.Sleep(100);
                return 0;
            }
            if (!File.Exists(Path.Combine(envDir, "VERSION")))
            {
                Console.Error.WriteLine("ModuleNotFoundError: No module named 'my_claude_code'");
                return 1;
            }
            return 0;
        }

        // A uv trampoline runs exactly <tool dir>\Scripts\python.exe.
        if (!File.Exists(Path.Combine(Path.Combine(envDir, "Scripts"), "python.exe")))
        {
            Console.Error.WriteLine("error: uv trampoline failed to canonicalize script path");
            return 1;
        }
        string versionFile = Path.Combine(envDir, "VERSION");
        if (!File.Exists(versionFile))
        {
            Console.Error.WriteLine("ModuleNotFoundError: No module named 'my_claude_code'");
            return 1;
        }
        string version = File.ReadAllText(versionFile).Trim();
        if (name.StartsWith("fcc-") || name == "free-claude-code")
        {
            Console.WriteLine(name + " was renamed in My Claude Code 7.0.0.");
            return 1;
        }
        if (args.Length > 0 && args[0] == "--version")
        {
            if (!inScripts && Env("FAKE_VERSION_FAILS_FOR") == version)
            {
                Console.Error.WriteLine("error: simulated --version failure");
                return 1;
            }
            Console.WriteLine("my-claude-code " + version);
            return 0;
        }
        if (args.Length > 0 && args[0].StartsWith("--"))
        {
            return 2;
        }
        if (name == "mcc-server" && args.Length == 0)
        {
            int pid = System.Diagnostics.Process.GetCurrentProcess().Id;
            File.AppendAllText(Env("FAKE_STARTED"), version + " " + pid + Environment.NewLine);
            int port = int.Parse(Env("FAKE_PORT"));
            TcpListener listener = new TcpListener(IPAddress.Loopback, port);
            listener.ExclusiveAddressUse = true;
            listener.Start();
            DateTime deadline = DateTime.UtcNow.AddSeconds(45);
            try
            {
                while (DateTime.UtcNow < deadline)
                {
                    if (!listener.Pending()) { Thread.Sleep(50); continue; }
                    using (TcpClient client = listener.AcceptTcpClient())
                    {
                        NetworkStream stream = client.GetStream();
                        byte[] buffer = new byte[4096];
                        int read = stream.Read(buffer, 0, buffer.Length);
                        string request = Encoding.ASCII.GetString(buffer, 0, read);
                        byte[] body = Encoding.ASCII.GetBytes("{\"status\":\"ok\",\"version\":\"" + version + "\"}");
                        string head = "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: " + body.Length + "\r\nConnection: close\r\n\r\n";
                        byte[] headBytes = Encoding.ASCII.GetBytes(head);
                        stream.Write(headBytes, 0, headBytes.Length);
                        stream.Write(body, 0, body.Length);
                        stream.Flush();
                        if (request.Contains("/health")) { Thread.Sleep(200); return 0; }
                    }
                }
            }
            finally { listener.Stop(); }
            return 0;
        }
        return 0;
    }
}
"""

# Every name Get-LauncherCommands lists -- read from the function itself, so
# the fake installs exactly what the verification step will check.
LAUNCHER_COMMANDS = tuple(
    re.findall(
        r'"([a-z][a-z0-9-]+)"',
        _extract_function(_installer_text(), "Get-LauncherCommands"),
    )
)

FAKE_UV_PY = r'''
"""A uv that does what uv does to the files, and nothing else."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

COMMANDS = os.environ["FAKE_COMMANDS"].split()
DEPENDENCY_SCRIPTS = ("fastapi", "uvicorn", "typer", "tqdm", "ddgs", "normalizer")


def log(line):
    with open(os.environ["CALL_LOG"], "a", encoding="utf-8") as handle:
        handle.write("uv:" + line + "\n")


def install(root, bin_dir, version, fake_exe, new_command):
    env = root / "my-claude-code"
    scripts = env / "Scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    for name in ("python", "pythonw", *DEPENDENCY_SCRIPTS):
        shutil.copyfile(fake_exe, scripts / (name + ".exe"))
    (env / "Lib" / "site-packages" / "my_claude_code").mkdir(parents=True, exist_ok=True)
    (env / "VERSION").write_text(version, encoding="utf-8")
    (env / "pyvenv.cfg").write_text("home = fake\n", encoding="utf-8")
    names = list(COMMANDS) + (["mcc-newcmd"] if new_command else [])
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        shutil.copyfile(fake_exe, scripts / (name + ".exe"))
        shutil.copyfile(fake_exe, bin_dir / (name + ".exe"))
    entries = "".join(
        '    { name = "%s", install-path = "%s", from = "my-claude-code" },\n'
        % (name, (bin_dir / (name + ".exe")).as_posix())
        for name in names
    )
    (env / "uv-receipt.toml").write_text(
        '[tool]\nrequirements = [{ name = "my-claude-code" }]\nentrypoints = [\n'
        + entries + "]\n",
        encoding="utf-8",
    )
    print("Installed %d executables: %s" % (len(names), ", ".join(names)))


def hold(pythonw):
    """What the desktop app's rescue did at 16:03: start an interpreter out of
    the environment uv is in the middle of writing."""
    holders = Path(os.environ["FAKE_HOLDERS"])
    child = subprocess.Popen(
        [str(pythonw), "--fake-hold", os.environ["FAKE_SENTINEL"]],
        cwd=os.environ["FAKE_HOME"],
        creationflags=0x00000008 | 0x00000200,
        close_fds=True,
    )
    with open(holders, "a", encoding="utf-8") as handle:
        handle.write(str(child.pid) + "\n")


def gut(env):
    """uv --force removes the environment first and stops at the first file a
    running process maps -- everything else is already gone."""
    keep = env / "Scripts" / "pythonw.exe"
    for path in sorted(env.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path == keep or path == keep.parent:
            continue
        if path.is_dir():
            try:
                path.rmdir()
            except OSError:
                pass
        else:
            path.unlink()


def main(args):
    log(" ".join(args))
    tools_root = Path(os.environ["FAKE_TOOLS_ROOT"])
    if args[:1] == ["--version"]:
        print("uv 0.11.28")
        return 0
    if args[:2] == ["python", "install"]:
        return 0
    if args[:2] == ["tool", "dir"]:
        print(os.environ["FAKE_BIN"] if "--bin" in args else str(tools_root))
        return 0
    if args[:2] == ["tool", "update-shell"]:
        return 0
    if args[:2] == ["tool", "list"]:
        env = tools_root / "my-claude-code"
        if env.is_dir() and not (env / "uv-receipt.toml").is_file():
            print("warning: Ignoring malformed tool `my-claude-code` (run `uv tool uninstall my-claude-code` to remove)", file=sys.stderr)
        return 0
    if args[:2] != ["tool", "install"]:
        return 59
    staging = os.environ.get("UV_TOOL_DIR")
    root = Path(staging) if staging else tools_root
    bin_dir = Path(os.environ.get("UV_TOOL_BIN_DIR") or os.environ["FAKE_BIN"])
    env = root / "my-claude-code"
    force = "--force" in args
    fake_exe = os.environ["FAKE_EXE"]
    if (not staging) and force and os.environ.get("FAKE_FINISH_FAILS") == "1":
        print("Resolved 97 packages in 143ms")
        if env.exists():
            gut(env)
            print("error: failed to remove directory `%s`: Access is denied. (os error 5)" % (env / "Scripts"), file=sys.stderr)
        else:
            (env / "Scripts").mkdir(parents=True)
            for name in ("python", "pythonw"):
                shutil.copyfile(fake_exe, env / "Scripts" / (name + ".exe"))
            hold(env / "Scripts" / "pythonw.exe")
            print("error: Failed to install entrypoint", file=sys.stderr)
            print(
                "  Caused by: failed to copy file from %s to %s: The process cannot access the file because it is being used by another process. (os error 32)"
                % (env / "Scripts" / "mcc-claude.exe", bin_dir / "mcc-claude.exe"),
                file=sys.stderr,
            )
        return 2
    if env.exists():
        if not force:
            print("error: `my-claude-code` is already installed", file=sys.stderr)
            return 2
        shutil.rmtree(env)
    install(root, bin_dir, os.environ["FAKE_RELEASE_VERSION"], fake_exe, os.environ.get("FAKE_NEW_COMMAND") == "1")
    return 0


sys.exit(main(sys.argv[1:]))
'''

# Only the seams the machine cannot fake: the release feed and the download
# (network), the process table (the reporter's own running launchers must
# never decide a test), and the deferred helper (it would wait on, then stop,
# real processes). Everything else is the published script, run as the
# published scriptblock.
INJECTED_STUBS = r"""
function Resolve-Release {
    return [pscustomobject]@{ Version = $env:FAKE_RELEASE_VERSION }
}
function Get-VerifiedReleaseWheel {
    param([object] $Release)
    $dir = Join-Path ([IO.Path]::GetTempPath()) ("fcc-wheel-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $dir | Out-Null
    $wheel = Join-Path $dir ("my_claude_code-" + $Release.Version + "-py3-none-any.whl")
    [IO.File]::WriteAllText($wheel, "fake wheel")
    return $wheel
}
function Get-RunningLaunchers {
    foreach ($name in @(([string] $env:FAKE_RUNNING) -split "," | Where-Object { $_ })) {
        [pscustomobject]@{ Id = 0; ProcessName = $name; StartTime = (Get-Date) }
    }
}
function Start-DeferredInstall { throw "Start-DeferredInstall must never run in this harness" }

"""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture(scope="session")
def fake_launcher(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if os.name != "nt":
        pytest.skip("the fake trampoline is a Windows executable")
    csc = Path(os.environ.get("WINDIR", r"C:\Windows")) / (
        r"Microsoft.NET\Framework64\v4.0.30319\csc.exe"
    )
    if not csc.is_file():
        pytest.skip("no .NET Framework C# compiler to build the fake trampoline")
    folder = tmp_path_factory.mktemp("fake-launcher")
    source = folder / "fake.cs"
    source.write_text(FAKE_LAUNCHER_CS, encoding="utf-8")
    output = folder / "fake.exe"
    built = subprocess.run(
        [str(csc), "-nologo", "-optimize+", f"-out:{output}", str(source)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    return output


@dataclass
class FullRun:
    root: Path
    tools: Path
    bin_dir: Path
    config: Path
    port: int
    env: dict[str, str]
    script: Path
    wrapper: Path
    holders: Path
    sentinel: Path
    started: Path
    transcript: Path
    calls: Path
    pids: list[int] = field(default_factory=list)

    def staging_root(self) -> Path:
        return self.tools.parent / ".mcc-staging"

    def previous_root(self) -> Path:
        return self.tools.parent / ".mcc-previous"

    def canonical(self) -> Path:
        return self.tools / "my-claude-code"

    def install(self, version: str) -> None:
        """Put a whole installed version in place, as the fake uv writes it."""
        result = subprocess.run(
            [
                sys.executable,
                str(self.root / "fake_uv.py"),
                "tool",
                "install",
                "--force",
                "spec",
            ],
            env=self.env
            | {
                "FAKE_RELEASE_VERSION": version,
                "FAKE_FINISH_FAILS": "0",
                "FAKE_NEW_COMMAND": "0",
            },
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        # The setup's own uv call is not part of the run under test.
        self.calls.unlink(missing_ok=True)

    def run(self, executable: str, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                executable,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(self.wrapper),
            ],
            capture_output=True,
            text=True,
            timeout=240,
            check=False,
            env=self.env | extra,
        )

    def started_versions(self) -> list[str]:
        if not self.started.exists():
            return []
        return [
            line.split()[0]
            for line in self.started.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def transcript_text(self) -> str:
        return (
            self.transcript.read_text(encoding="utf-8")
            if self.transcript.exists()
            else ""
        )

    def uv_calls(self) -> list[str]:
        if not self.calls.exists():
            return []
        return [
            line
            for line in self.calls.read_text(encoding="utf-8").splitlines()
            if line.startswith("uv:")
        ]

    def stop_everything_it_started(self) -> None:
        """Every process here is one this test started; each is stopped by its
        exact pid, and only while its image is still under this test's tmp."""
        self.sentinel.write_text("stop", encoding="utf-8")
        pids: list[int] = []
        for record in (self.holders, self.started):
            if record.exists():
                for line in record.read_text(encoding="utf-8").splitlines():
                    parts = line.split()
                    if parts and parts[-1].isdigit():
                        pids.append(int(parts[-1]))
        for pid in pids:
            probe = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    f"(Get-Process -Id {pid} -ErrorAction SilentlyContinue).Path",
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            image = probe.stdout.strip()
            if image and image.lower().startswith(str(self.root).lower()):
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/F"],
                    capture_output=True,
                    timeout=60,
                    check=False,
                )


@pytest.fixture
def full_run(tmp_path: Path, fake_launcher: Path) -> Iterator[FullRun]:
    root = tmp_path
    tools = root / "app-data" / "uv" / "tools"
    bin_dir = root / "home" / ".local" / "bin"
    config = root / "mcc-config"
    for path in (
        tools,
        bin_dir,
        config,
        root / "fake-path",
        root / "temp",
        root / "local-app-data",
    ):
        path.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    (config / ".env").write_text(f"HOST=127.0.0.1\nPORT={port}\n", encoding="utf-8")
    (root / "fake_uv.py").write_text(FAKE_UV_PY, encoding="utf-8")
    (root / "fake-path" / "uv.cmd").write_text(
        f'@"{sys.executable}" "{root / "fake_uv.py"}" %*\r\n', encoding="utf-8"
    )
    fake_exe = root / "fake.exe"
    shutil.copyfile(fake_launcher, fake_exe)
    text = _installer_text()
    marker = "\nif ($Help) {"
    assert text.count(marker) == 1
    script = root / "install-under-test.ps1"
    script.write_text(
        text.replace(marker, "\n" + INJECTED_STUBS + marker[1:]), encoding="utf-8"
    )
    wrapper = root / "run-installer.ps1"
    wrapper.write_text(
        "Set-StrictMode -Version Latest\n$ErrorActionPreference = 'Stop'\n"
        "$installer = [scriptblock]::Create([IO.File]::ReadAllText($env:FCC_INSTALLER))\n"
        "& $installer @args\n",
        encoding="utf-8",
    )
    system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper()
        not in {
            "PORT",
            "HOST",
            "MCC_CONFIG_DIR",
            "UV_TOOL_DIR",
            "UV_TOOL_BIN_DIR",
            "MCC_INSTALL_LOG",
        }
    }
    env.update(
        {
            "PATH": os.pathsep.join(
                [
                    str(root / "fake-path"),
                    str(Path(system_root) / "System32"),
                    system_root,
                    str(Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0"),
                ]
            ),
            "PATHEXT": ".COM;.EXE;.BAT;.CMD",
            "USERPROFILE": str(root / "home"),
            "HOME": str(root / "home"),
            "APPDATA": str(root / "app-data"),
            "LOCALAPPDATA": str(root / "local-app-data"),
            "TEMP": str(root / "temp"),
            "TMP": str(root / "temp"),
            "MCC_CONFIG_DIR": str(config),
            "MCC_INSTALL_LOG": str(root / "install.log"),
            "MCC_INSTALL_NO_DESKTOP": "1",
            "MCC_DESKTOP_SKIP_AUTOSTART": "1",
            "MCC_OPEN_BROWSER": "0",
            "PORT": str(port),
            "HOST": "127.0.0.1",
            "CALL_LOG": str(root / "calls.log"),
            "FAKE_TOOLS_ROOT": str(tools),
            "FAKE_BIN": str(bin_dir),
            "FAKE_EXE": str(fake_exe),
            "FAKE_COMMANDS": " ".join(LAUNCHER_COMMANDS),
            "FAKE_RELEASE_VERSION": NEW_VERSION,
            "FAKE_PORT": str(port),
            "FAKE_STARTED": str(root / "started.txt"),
            "FAKE_HOLDERS": str(root / "holders.txt"),
            "FAKE_SENTINEL": str(root / "stop-holding"),
            "FAKE_HOME": str(root),
            "FCC_INSTALLER": str(script),
        }
    )
    run = FullRun(
        root=root,
        tools=tools,
        bin_dir=bin_dir,
        config=config,
        port=port,
        env=env,
        script=script,
        wrapper=wrapper,
        holders=root / "holders.txt",
        sentinel=root / "stop-holding",
        started=root / "started.txt",
        transcript=root / "install.log",
        calls=root / "calls.log",
    )
    yield run
    run.stop_everything_it_started()


@pytest.mark.parametrize(("edition", "executable"), EDITIONS, ids=lambda v: v)
def test_a_healthy_update_swaps_once_and_never_finishes_in_place(
    edition: str, executable: str, full_run: FullRun
) -> None:
    """The ordinary update: staged, proved, swapped, started -- and NO second,
    destructive ``uv tool install --force``, although the environment's
    Scripts directory is full of dependency executables."""

    _require(executable)
    full_run.install(OLD_VERSION)
    completed = full_run.run(executable)
    out = completed.stdout
    transcript = full_run.transcript_text()

    assert completed.returncode == 0, out + completed.stderr
    assert "The new version ran; putting it in place." in out
    assert "This release adds" not in out
    assert "This release adds" not in transcript
    assert "Swapped in " in transcript
    assert (
        f"My Claude Code {NEW_VERSION} is installed and answering on port {full_run.port}."
        in out
    )
    assert full_run.started_versions() == [NEW_VERSION]
    installs = [
        call for call in full_run.uv_calls() if call.startswith("uv:tool install")
    ]
    assert len(installs) == 1, installs
    assert "--force" not in installs[0]
    assert (full_run.previous_root()).is_dir()


@pytest.mark.parametrize(("edition", "executable"), EDITIONS, ids=lambda v: v)
def test_an_installed_launcher_that_cannot_report_its_version_still_swaps_and_starts(
    edition: str, executable: str, full_run: FullRun
) -> None:
    """``mcc-server --version`` exits 1 on the installed build: the run no longer
    dies in parameter binding -- it says why, swaps, starts, and answers."""

    _require(executable)
    full_run.install(OLD_VERSION)
    completed = full_run.run(executable, FAKE_VERSION_FAILS_FOR=OLD_VERSION)
    out = completed.stdout
    transcript = full_run.transcript_text()

    assert completed.returncode == 0, out + completed.stderr
    assert "because it is an empty string" not in completed.stderr
    assert (
        "The installed mcc-server could not report its version (exit 1): "
        "error: simulated --version failure"
    ) in transcript
    assert (
        "The installed mcc-server (version unknown) predates --report-holder; not asking it."
        in transcript
    )
    assert "Swapped in " in transcript
    assert (
        f"My Claude Code {NEW_VERSION} is installed and answering on port {full_run.port}."
        in out
    )
    assert full_run.started_versions() == [NEW_VERSION]


@pytest.mark.parametrize(("edition", "executable"), EDITIONS, ids=lambda v: v)
def test_a_husk_is_moved_aside_and_installed_fresh(
    edition: str, executable: str, full_run: FullRun
) -> None:
    """The reporter's machine at 16:12, by the installer alone.

    ``tools\\my-claude-code`` holds only ``Scripts\\pythonw.exe``; a ghost
    ``my-claude-code.old-<stamp>`` sits inside the tools root; earlier failed
    runs left orphan staging directories. The run finds the husk, moves it
    aside as a sibling of the tools root, installs fresh, starts the server,
    and sweeps what the earlier runs left.
    """

    _require(executable)
    full_run.install(OLD_VERSION)
    canonical = full_run.canonical()
    # The 16:03 husk: everything gone but the one file a process held.
    for path in sorted(canonical.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.name == "pythonw.exe" or path == canonical / "Scripts":
            continue
        if path.is_dir():
            path.rmdir()
        else:
            path.unlink()
    ghost = full_run.tools / "my-claude-code.old-20261008-160352"
    _touch(ghost / "Scripts" / "python.exe")
    for orphan in ("20261008-160332", "20261008-160415", "20261008-161208"):
        _touch(full_run.staging_root() / orphan / "my-claude-code" / "pyvenv.cfg")

    completed = full_run.run(executable)
    out = completed.stdout
    transcript = full_run.transcript_text()

    assert completed.returncode == 0, out + completed.stderr
    assert "because it is an empty string" not in completed.stderr
    assert (
        f"Found a broken My Claude Code environment at {canonical}: "
        "it has no Scripts\\python.exe; it has no uv-receipt.toml."
    ) in out
    assert (
        "Moved it aside to " in out
        and "-broken\\my-claude-code and installing a fresh copy in its place." in out
    )
    assert (
        "There is no existing tool environment to stage beside; installing in place."
        in transcript
    )
    assert f"My Claude Code {NEW_VERSION} is installed and verified." in out
    assert (
        f"My Claude Code {NEW_VERSION} is installed and answering on port {full_run.port}."
        in out
    )
    assert full_run.started_versions() == [NEW_VERSION]
    assert (canonical / "Scripts" / "python.exe").is_file()
    assert (canonical / "VERSION").read_text(encoding="utf-8") == NEW_VERSION
    # Nothing but the tool itself inside uv's tools root.
    assert sorted(p.name for p in full_run.tools.iterdir()) == ["my-claude-code"]
    # The earlier runs' orphans are gone; this run's husk is kept for a look.
    remaining = sorted(p.name for p in full_run.staging_root().iterdir())
    assert all(name.endswith("-broken") for name in remaining), remaining
    for orphan in ("20261008-160332", "20261008-160415", "20261008-161208"):
        assert f"Removed the leftover {full_run.staging_root() / orphan}." in transcript


@pytest.mark.parametrize(("edition", "executable"), EDITIONS, ids=lambda v: v)
def test_a_failed_in_place_finish_puts_the_previous_version_back(
    edition: str, executable: str, full_run: FullRun
) -> None:
    """The 16:03 run, with the rollback it never had.

    A release that really adds a launcher still needs uv in place after the
    swap. Here that ``--force`` fails exactly as it did on the reporter's
    machine -- an entry point locked (os error 32), then the retry unable to
    remove ``Scripts`` because a process was started out of the half-written
    environment (os error 5). The previous version goes back, the server this
    run stopped comes back up FROM it, nothing is started from a husk, and the
    run ends non-zero with uv's own words.
    """

    _require(executable)
    full_run.install(OLD_VERSION)
    completed = full_run.run(
        executable,
        FAKE_NEW_COMMAND="1",
        FAKE_FINISH_FAILS="1",
        FAKE_RUNNING="mcc-claude",
    )
    out = completed.stdout
    transcript = full_run.transcript_text()

    assert completed.returncode == 1, out + completed.stderr
    assert (
        "This release adds mcc-newcmd; uv has to write the launcher(s), so the install is finished in place."
        in out
    )
    assert (
        "The install could not be finished in place: My Claude Code install failed:"
        in out
    )
    assert "uv said:" in out
    assert "(os error 32)" in out
    assert "Access is denied. (os error 5)" in out
    assert (
        f"The previous version was put back and is answering on port {full_run.port}. "
        "Nothing was lost; the update did not happen."
    ) in out
    # Started once, from the previous version -- never from a husk.
    assert full_run.started_versions() == [OLD_VERSION]
    assert "trampoline failed" not in out
    canonical = full_run.canonical()
    assert (canonical / "Scripts" / "python.exe").is_file()
    assert (canonical / "VERSION").read_text(encoding="utf-8") == OLD_VERSION
    assert sorted(p.name for p in full_run.tools.iterdir()) == ["my-claude-code"]
    assert "The previous environment is back at the canonical path." in transcript
