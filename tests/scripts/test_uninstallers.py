"""End-to-end uninstaller behaviour on both platforms.

The uninstaller removes the ``my-claude-code`` uv tool (plus the legacy
``free-claude-code`` tool left by pre-5.14 installs), verifies that every
published entry point is gone before touching user data, and preserves
``~/.fcc`` whenever removal cannot be confirmed. The shim fixtures here are
derived from ``pyproject.toml`` so the scenarios always cover exactly the
entry points a real wheel publishes; the cross-file parity contract lives in
tests/contracts/test_uninstaller_parity.py.
"""

import os
import shutil
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

import pytest

PRIMARY_PACKAGE = "my-claude-code"
LEGACY_PACKAGE = "free-claude-code"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _published_commands() -> tuple[str, ...]:
    pyproject = _repo_root() / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    project = data["project"]
    return tuple(
        sorted(set(project.get("scripts", {})) | set(project.get("gui-scripts", {})))
    )


PUBLISHED_COMMANDS = _published_commands()


def _write_executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _powershells() -> tuple[str, ...]:
    candidates = (shutil.which("pwsh"), shutil.which("powershell"))
    return tuple(dict.fromkeys(path for path in candidates if path is not None))


@dataclass
class PosixUninstallHarness:
    home: Path
    bin_dir: Path
    tool_bin: Path
    fcc_home: Path
    log: Path
    env: dict[str, str]

    def run(
        self,
        *args: str,
        fail_step: str = "",
        include_uv: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        uv = self.bin_dir / "uv"
        if not include_uv and uv.exists():
            uv.unlink()
        return subprocess.run(
            ["/bin/sh", str(_repo_root() / "scripts" / "uninstall.sh"), *args],
            check=False,
            capture_output=True,
            text=True,
            env=self.env | {"FAIL_STEP": fail_step},
        )

    def calls(self) -> list[str]:
        if not self.log.exists():
            return []
        return self.log.read_text(encoding="utf-8").splitlines()

    def remove_entry_points(self) -> None:
        for name in PUBLISHED_COMMANDS:
            (self.tool_bin / name).unlink(missing_ok=True)


@pytest.fixture
def posix_uninstall_harness(tmp_path: Path) -> PosixUninstallHarness:
    if os.name == "nt":
        pytest.skip("POSIX uninstaller scenarios run on POSIX hosts")

    home = tmp_path / "home"
    bin_dir = home / ".local" / "bin"
    tool_bin = tmp_path / "tool-bin"
    fcc_home = home / ".fcc"
    log = tmp_path / "calls.log"
    for path in (bin_dir, tool_bin, fcc_home):
        path.mkdir(parents=True)
    (fcc_home / "config.json").write_text("{}", encoding="utf-8")
    for name in PUBLISHED_COMMANDS:
        _write_executable(tool_bin / name, "#!/bin/sh\nexit 0\n")

    _write_executable(bin_dir / "claude", "#!/bin/sh\nexit 0\n")
    _write_executable(bin_dir / "codex", "#!/bin/sh\nexit 0\n")
    _write_executable(bin_dir / "pi", "#!/bin/sh\nexit 0\n")
    _write_executable(
        bin_dir / "uv",
        r"""#!/bin/sh
echo "uv:$*" >> "$CALL_LOG"
if [ "${1:-}" = "tool" ] && [ "${2:-}" = "dir" ] && [ "${3:-}" = "--bin" ]; then
    if [ "$FAIL_STEP" = "tool-dir" ]; then
        echo "tool directory unavailable" >&2
        exit 41
    fi
    printf '%s\n' "$FAKE_TOOL_BIN"
    exit 0
fi
if [ "${1:-}" = "tool" ] && [ "${2:-}" = "uninstall" ]; then
    if [ "$FAIL_STEP" = "uninstall" ]; then
        echo "permission denied while removing tool" >&2
        exit 42
    fi
    if [ "$FAIL_STEP" = "missing" ] || [ "$FAIL_STEP" = "stale-entrypoint" ]; then
        printf 'Tool `%s` is not installed\n' "${3:-}" >&2
        exit 2
    fi
    if [ "$FAIL_STEP" = "missing-primary" ] && [ "${3:-}" = "my-claude-code" ]; then
        printf 'Tool `%s` is not installed\n' "${3:-}" >&2
        exit 2
    fi
    if [ "$FAIL_STEP" = "missing-legacy" ] && [ "${3:-}" = "free-claude-code" ]; then
        printf 'Tool `%s` is not installed\n' "${3:-}" >&2
        exit 2
    fi
    for shim in "$FAKE_TOOL_BIN"/*; do
        /bin/rm -f "$shim"
    done
    echo "Uninstalled ${3:-}"
    exit 0
fi
exit 43
""",
    )
    _write_executable(
        bin_dir / "rm",
        r"""#!/bin/sh
echo "rm:$*" >> "$CALL_LOG"
if [ "$FAIL_STEP" = "purge" ]; then
    echo "simulated purge failure" >&2
    exit 44
fi
exec /bin/rm "$@"
""",
    )

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "CALL_LOG": str(log),
            "FAKE_TOOL_BIN": str(tool_bin),
            "FAIL_STEP": "",
        }
    )
    env.pop("XDG_BIN_HOME", None)
    return PosixUninstallHarness(home, bin_dir, tool_bin, fcc_home, log, env)


def test_uninstall_sh_removes_and_verifies_only_mcc(
    posix_uninstall_harness: PosixUninstallHarness,
) -> None:
    result = posix_uninstall_harness.run()

    assert result.returncode == 0, result.stderr
    assert "My Claude Code has been removed and verified." in result.stdout
    assert not posix_uninstall_harness.fcc_home.exists()
    assert all(
        not (posix_uninstall_harness.tool_bin / name).exists()
        for name in PUBLISHED_COMMANDS
    )
    assert (posix_uninstall_harness.bin_dir / "uv").exists()
    assert (posix_uninstall_harness.bin_dir / "claude").exists()
    assert (posix_uninstall_harness.bin_dir / "codex").exists()
    assert (posix_uninstall_harness.bin_dir / "pi").exists()
    assert posix_uninstall_harness.calls() == [
        "uv:tool dir --bin",
        f"uv:tool uninstall {PRIMARY_PACKAGE}",
        f"uv:tool uninstall {LEGACY_PACKAGE}",
        f"rm:-rf {posix_uninstall_harness.fcc_home}",
    ]


def test_uninstall_sh_is_idempotent_when_both_tools_are_absent(
    posix_uninstall_harness: PosixUninstallHarness,
) -> None:
    posix_uninstall_harness.remove_entry_points()

    result = posix_uninstall_harness.run(fail_step="missing")

    assert result.returncode == 0, result.stderr
    assert not posix_uninstall_harness.fcc_home.exists()
    assert "already absent" in result.stdout
    assert posix_uninstall_harness.calls() == [
        "uv:tool dir --bin",
        f"uv:tool uninstall {PRIMARY_PACKAGE}",
        f"uv:tool uninstall {LEGACY_PACKAGE}",
        f"rm:-rf {posix_uninstall_harness.fcc_home}",
    ]


def test_uninstall_sh_cleans_up_legacy_tool_when_primary_is_absent(
    posix_uninstall_harness: PosixUninstallHarness,
) -> None:
    result = posix_uninstall_harness.run(fail_step="missing-primary")

    assert result.returncode == 0, result.stderr
    assert f"{PRIMARY_PACKAGE} uv tool is already absent" in result.stdout
    assert not posix_uninstall_harness.fcc_home.exists()
    assert all(
        not (posix_uninstall_harness.tool_bin / name).exists()
        for name in PUBLISHED_COMMANDS
    )


def test_uninstall_sh_tolerates_legacy_tool_absence(
    posix_uninstall_harness: PosixUninstallHarness,
) -> None:
    result = posix_uninstall_harness.run(fail_step="missing-legacy")

    assert result.returncode == 0, result.stderr
    assert f"{LEGACY_PACKAGE} uv tool is already absent" in result.stdout
    assert not posix_uninstall_harness.fcc_home.exists()
    assert all(
        not (posix_uninstall_harness.tool_bin / name).exists()
        for name in PUBLISHED_COMMANDS
    )


@pytest.mark.parametrize("failure", ["tool-dir", "uninstall", "stale-entrypoint"])
def test_uninstall_sh_preserves_config_when_tool_removal_is_unconfirmed(
    posix_uninstall_harness: PosixUninstallHarness,
    failure: str,
) -> None:
    result = posix_uninstall_harness.run(fail_step=failure)

    assert result.returncode != 0
    assert posix_uninstall_harness.fcc_home.exists()
    assert "My Claude Code has been removed and verified." not in result.stdout
    assert not any(call.startswith("rm:") for call in posix_uninstall_harness.calls())


def test_uninstall_sh_requires_uv_before_deleting_config(
    posix_uninstall_harness: PosixUninstallHarness,
) -> None:
    result = posix_uninstall_harness.run(include_uv=False)

    assert result.returncode != 0
    assert posix_uninstall_harness.fcc_home.exists()
    assert "uv is required" in result.stderr
    assert posix_uninstall_harness.calls() == []


def test_uninstall_sh_reports_purge_failure_after_verified_tool_removal(
    posix_uninstall_harness: PosixUninstallHarness,
) -> None:
    result = posix_uninstall_harness.run(fail_step="purge")

    assert result.returncode != 0
    assert posix_uninstall_harness.fcc_home.exists()
    assert all(
        not (posix_uninstall_harness.tool_bin / name).exists()
        for name in PUBLISHED_COMMANDS
    )
    assert f"uv:tool uninstall {PRIMARY_PACKAGE}" in posix_uninstall_harness.calls()
    assert "My Claude Code has been removed and verified." not in result.stdout


def test_uninstall_sh_dry_run_is_non_mutating(
    posix_uninstall_harness: PosixUninstallHarness,
) -> None:
    result = posix_uninstall_harness.run("--dry-run")

    assert result.returncode == 0, result.stderr
    assert posix_uninstall_harness.fcc_home.exists()
    assert all(
        (posix_uninstall_harness.tool_bin / name).exists()
        for name in PUBLISHED_COMMANDS
    )
    assert posix_uninstall_harness.calls() == []
    assert "Dry run complete. No changes were made." in result.stdout


def test_uninstall_sh_rejects_invalid_options_before_mutation(
    posix_uninstall_harness: PosixUninstallHarness,
) -> None:
    result = posix_uninstall_harness.run("--unknown")

    assert result.returncode != 0
    assert posix_uninstall_harness.fcc_home.exists()
    assert posix_uninstall_harness.calls() == []


@dataclass
class PowerShellUninstallHarness:
    home: Path
    bin_dir: Path
    tool_bin: Path
    fcc_home: Path
    log: Path
    env: dict[str, str]
    powershell: str
    wrapper: Path

    def run(
        self,
        *,
        fail_step: str = "",
        include_uv: bool = True,
        dry_run: bool = False,
        fake_running_process: str = "",
    ) -> subprocess.CompletedProcess[str]:
        uv = self.bin_dir / "uv.cmd"
        if not include_uv and uv.exists():
            uv.unlink()
        return subprocess.run(
            [
                self.powershell,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(self.wrapper),
            ],
            check=False,
            capture_output=True,
            text=True,
            env=self.env
            | {
                "FAIL_STEP": fail_step,
                "FAKE_RUNNING_PROCESS": fake_running_process,
                "UNINSTALL_DRY_RUN": "1" if dry_run else "0",
            },
        )

    def calls(self) -> list[str]:
        if not self.log.exists():
            return []
        return self.log.read_text(encoding="utf-8").splitlines()

    def remove_entry_points(self) -> None:
        for name in PUBLISHED_COMMANDS:
            (self.tool_bin / f"{name}.cmd").unlink(missing_ok=True)


@pytest.fixture(
    params=_powershells() or (None,),
    ids=lambda path: Path(path).name if path is not None else "unavailable",
)
def powershell_uninstall_harness(
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> PowerShellUninstallHarness:
    powershell = request.param
    if powershell is None or os.name != "nt":
        pytest.skip("PowerShell uninstaller scenarios run on Windows hosts")

    home = tmp_path / "home"
    bin_dir = home / ".local" / "bin"
    tool_bin = tmp_path / "tool-bin"
    fcc_home = home / ".fcc"
    log = tmp_path / "calls.log"
    for path in (bin_dir, tool_bin, fcc_home):
        path.mkdir(parents=True)
    (fcc_home / "config.json").write_text("{}", encoding="utf-8")
    for name in PUBLISHED_COMMANDS:
        (tool_bin / f"{name}.cmd").write_text(
            "@echo off\nexit /b 0\n", encoding="utf-8"
        )
    for name in ("claude", "codex", "pi"):
        (bin_dir / f"{name}.cmd").write_text("@echo off\nexit /b 0\n", encoding="utf-8")

    (bin_dir / "uv.cmd").write_text(
        r"""@echo off
echo uv:%*>>"%CALL_LOG%"
if "%1"=="tool" if "%2"=="dir" if "%3"=="--bin" goto tool_bin
if "%1"=="tool" if "%2"=="uninstall" goto uninstall
exit /b 53
:tool_bin
if "%FAIL_STEP%"=="tool-dir" echo tool directory unavailable 1>&2 & exit /b 51
echo %FAKE_TOOL_BIN%
exit /b 0
:uninstall
if "%FAIL_STEP%"=="uninstall" echo permission denied while removing tool 1>&2 & exit /b 52
if "%FAIL_STEP%"=="missing" goto not_installed
if "%FAIL_STEP%"=="stale-entrypoint" goto not_installed
if "%FAIL_STEP%"=="missing-primary" if "%~3"=="my-claude-code" goto not_installed
if "%FAIL_STEP%"=="missing-legacy" if "%~3"=="free-claude-code" goto not_installed
for %%F in ("%FAKE_TOOL_BIN%\*.cmd") do del /q "%%F" 2>nul
echo Uninstalled %~3
exit /b 0
:not_installed
echo Tool `%~3` is not installed 1>&2
exit /b 2
""",
        encoding="utf-8",
    )

    wrapper = tmp_path / "run-uninstaller.ps1"
    wrapper.write_text(
        r"""Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
function Remove-Item {
    [CmdletBinding()]
    param(
        [string] $LiteralPath,
        [switch] $Recurse,
        [switch] $Force
    )
    Add-Content -LiteralPath $env:CALL_LOG -Value "remove:$LiteralPath"
    if ($env:FAIL_STEP -eq "purge") {
        throw "simulated purge failure"
    }
    Microsoft.PowerShell.Management\Remove-Item @PSBoundParameters
}
function Get-Process {
    # Hermetic stand-in for the process guard: the real cmdlet sees whatever
    # happens to run on this machine (mcc-claude, mcc-desktop, pythonw are
    # routinely live here), which made the suite non-hermetic once the guard
    # covered the full published command list.
    [CmdletBinding()]
    param(
        [Parameter(Position = 0)]
        [string[]] $Name
    )
    if ($env:FAIL_STEP -ne "running-processes") {
        return
    }
    foreach ($processName in @($Name)) {
        if ($processName -eq $env:FAKE_RUNNING_PROCESS) {
            [pscustomobject] @{ Id = 4242; ProcessName = $processName }
        }
    }
}
$installer = [scriptblock]::Create([IO.File]::ReadAllText($env:FCC_UNINSTALLER))
if ($env:UNINSTALL_DRY_RUN -eq "1") {
    & $installer -DryRun
}
else {
    & $installer
}
""",
        encoding="utf-8",
    )

    system_root = os.environ["SYSTEMROOT"]
    env = os.environ.copy()
    env.update(
        {
            "PATH": os.pathsep.join(
                [str(bin_dir), str(Path(system_root) / "System32"), system_root]
            ),
            "PATHEXT": ".COM;.EXE;.BAT;.CMD",
            "HOME": str(home),
            "USERPROFILE": str(home),
            "CALL_LOG": str(log),
            "FAKE_TOOL_BIN": str(tool_bin),
            "FCC_UNINSTALLER": str(_repo_root() / "scripts" / "uninstall.ps1"),
            "FAIL_STEP": "",
            "UNINSTALL_DRY_RUN": "0",
        }
    )
    return PowerShellUninstallHarness(
        home, bin_dir, tool_bin, fcc_home, log, env, powershell, wrapper
    )


def test_uninstall_ps1_removes_and_verifies_only_mcc(
    powershell_uninstall_harness: PowerShellUninstallHarness,
) -> None:
    result = powershell_uninstall_harness.run()

    assert result.returncode == 0, result.stderr
    assert "My Claude Code has been removed and verified." in result.stdout
    assert not powershell_uninstall_harness.fcc_home.exists()
    assert all(
        not (powershell_uninstall_harness.tool_bin / f"{name}.cmd").exists()
        for name in PUBLISHED_COMMANDS
    )
    assert (powershell_uninstall_harness.bin_dir / "uv.cmd").exists()
    assert (powershell_uninstall_harness.bin_dir / "claude.cmd").exists()
    assert (powershell_uninstall_harness.bin_dir / "codex.cmd").exists()
    assert (powershell_uninstall_harness.bin_dir / "pi.cmd").exists()
    assert powershell_uninstall_harness.calls() == [
        "uv:tool dir --bin",
        f"uv:tool uninstall {PRIMARY_PACKAGE}",
        f"uv:tool uninstall {LEGACY_PACKAGE}",
        f"remove:{powershell_uninstall_harness.fcc_home}",
    ]


def test_uninstall_ps1_is_idempotent_when_both_tools_are_absent(
    powershell_uninstall_harness: PowerShellUninstallHarness,
) -> None:
    powershell_uninstall_harness.remove_entry_points()

    result = powershell_uninstall_harness.run(fail_step="missing")

    assert result.returncode == 0, result.stderr
    assert not powershell_uninstall_harness.fcc_home.exists()
    assert "already absent" in result.stdout
    assert powershell_uninstall_harness.calls() == [
        "uv:tool dir --bin",
        f"uv:tool uninstall {PRIMARY_PACKAGE}",
        f"uv:tool uninstall {LEGACY_PACKAGE}",
        f"remove:{powershell_uninstall_harness.fcc_home}",
    ]


def test_uninstall_ps1_cleans_up_legacy_tool_when_primary_is_absent(
    powershell_uninstall_harness: PowerShellUninstallHarness,
) -> None:
    result = powershell_uninstall_harness.run(fail_step="missing-primary")

    assert result.returncode == 0, result.stderr
    assert f"{PRIMARY_PACKAGE} uv tool is already absent" in result.stdout
    assert not powershell_uninstall_harness.fcc_home.exists()
    assert all(
        not (powershell_uninstall_harness.tool_bin / f"{name}.cmd").exists()
        for name in PUBLISHED_COMMANDS
    )


def test_uninstall_ps1_tolerates_legacy_tool_absence(
    powershell_uninstall_harness: PowerShellUninstallHarness,
) -> None:
    result = powershell_uninstall_harness.run(fail_step="missing-legacy")

    assert result.returncode == 0, result.stderr
    assert f"{LEGACY_PACKAGE} uv tool is already absent" in result.stdout
    assert not powershell_uninstall_harness.fcc_home.exists()
    assert all(
        not (powershell_uninstall_harness.tool_bin / f"{name}.cmd").exists()
        for name in PUBLISHED_COMMANDS
    )


@pytest.mark.parametrize("failure", ["tool-dir", "uninstall", "stale-entrypoint"])
def test_uninstall_ps1_preserves_config_when_tool_removal_is_unconfirmed(
    powershell_uninstall_harness: PowerShellUninstallHarness,
    failure: str,
) -> None:
    result = powershell_uninstall_harness.run(fail_step=failure)

    assert result.returncode != 0
    assert powershell_uninstall_harness.fcc_home.exists()
    assert "My Claude Code has been removed and verified." not in result.stdout
    assert not any(
        call.startswith("remove:") for call in powershell_uninstall_harness.calls()
    )


def test_uninstall_ps1_requires_uv_before_deleting_config(
    powershell_uninstall_harness: PowerShellUninstallHarness,
) -> None:
    result = powershell_uninstall_harness.run(include_uv=False)

    assert result.returncode != 0
    assert powershell_uninstall_harness.fcc_home.exists()
    assert "uv is required" in result.stderr
    assert powershell_uninstall_harness.calls() == []


def test_uninstall_ps1_reports_purge_failure_after_verified_tool_removal(
    powershell_uninstall_harness: PowerShellUninstallHarness,
) -> None:
    result = powershell_uninstall_harness.run(fail_step="purge")

    assert result.returncode != 0
    assert powershell_uninstall_harness.fcc_home.exists()
    assert all(
        not (powershell_uninstall_harness.tool_bin / f"{name}.cmd").exists()
        for name in PUBLISHED_COMMANDS
    )
    assert (
        f"uv:tool uninstall {PRIMARY_PACKAGE}" in powershell_uninstall_harness.calls()
    )
    assert "My Claude Code has been removed and verified." not in result.stdout


def test_uninstall_ps1_dry_run_is_non_mutating(
    powershell_uninstall_harness: PowerShellUninstallHarness,
) -> None:
    result = powershell_uninstall_harness.run(dry_run=True)

    assert result.returncode == 0, result.stderr
    assert powershell_uninstall_harness.fcc_home.exists()
    assert all(
        (powershell_uninstall_harness.tool_bin / f"{name}.cmd").exists()
        for name in PUBLISHED_COMMANDS
    )
    assert powershell_uninstall_harness.calls() == []
    assert "Dry run complete. No changes were made." in result.stdout


def test_uninstall_ps1_refuses_to_run_while_a_launcher_is_alive(
    powershell_uninstall_harness: PowerShellUninstallHarness,
) -> None:
    """A live launcher holds the tool directory open on Windows.

    Deleting shims underneath it half-removes the install, and purging
    ~/.fcc afterwards would destroy config the user still needs.
    """

    result = powershell_uninstall_harness.run(
        fail_step="running-processes",
        fake_running_process="mcc-desktop",
    )

    assert result.returncode != 0
    assert "still running (mcc-desktop)" in result.stderr
    assert powershell_uninstall_harness.fcc_home.exists()
    assert not any(
        call.startswith("remove:") for call in powershell_uninstall_harness.calls()
    )


def test_uninstall_ps1_blocks_on_the_gui_host_image_name(
    powershell_uninstall_harness: PowerShellUninstallHarness,
) -> None:
    """pythonw.exe is invisible under its launcher names, yet holds the tool.

    Only the GuardProcessImages list can see it, so dropping that list must
    fail here instead of surfacing mid-uninstall on a user's machine.
    """

    result = powershell_uninstall_harness.run(
        fail_step="running-processes",
        fake_running_process="pythonw",
    )

    assert result.returncode != 0
    assert "still running (pythonw)" in result.stderr
    assert powershell_uninstall_harness.fcc_home.exists()


def test_uninstallers_guard_running_commands_and_preserve_shared_owners() -> None:
    shell = (_repo_root() / "scripts" / "uninstall.sh").read_text(encoding="utf-8")
    powershell = (_repo_root() / "scripts" / "uninstall.ps1").read_text(
        encoding="utf-8"
    )

    assert "pgrep" in shell
    assert "Get-Process" in powershell
    # GUI launchers run as pythonw.exe out of the tool environment, invisible
    # to shim-name checks; only the Windows guard needs the interpreter image.
    assert "pythonw" in powershell
    for text in (shell, powershell):
        assert "npm uninstall" not in text
        assert "uv self uninstall" not in text
        assert "uv python uninstall" not in text
        assert "is not installed" in text
        assert "no tool" not in text
        assert "nothing to uninstall" not in text


def test_readme_uninstall_uses_raw_urls_and_verification_contract() -> None:
    text = (_repo_root() / "README.md").read_text(encoding="utf-8")

    assert (
        'curl -fsSL "https://raw.githubusercontent.com/'
        'FiredMosquito831/my-claude-code/main/scripts/uninstall.sh" | sh'
    ) in text
    assert (
        '& ([scriptblock]::Create((irm "https://raw.githubusercontent.com/'
        'FiredMosquito831/my-claude-code/main/scripts/uninstall.ps1")))'
    ) in text
    assert "verifies every MCC command is gone" in text
