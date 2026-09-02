import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

FCC_VERSION = "9.9.9"
FCC_WHEEL_NAME = f"my_claude_code-{FCC_VERSION}-py3-none-any.whl"
FCC_WHEEL_URL = (
    "https://github.com/FiredMosquito831/my-claude-code/releases/download/"
    f"v{FCC_VERSION}/{FCC_WHEEL_NAME}"
)
FCC_WHEEL_SHA256 = "91aaec9d83e2e931dbad653e74faa3c106acd6f8bd30a21a7985d77d870aef8b"
# Helpers each installer must keep defining. Deleting one only surfaces when a
# user runs the script, so it is asserted here instead.
_SHELL_HELPER_NAMES = frozenset(
    {
        "resolve_release",
        "download_verified_release_wheel",
        "install_my_claude_code",
        "configure_and_verify_my_claude_code",
        "ensure_uv",
        "verify_uv",
        "current_uv_version",
        "version_ge",
        "create_desktop_shortcut",
        "create_linux_desktop_entry",
        "create_macos_app_bundle",
    }
)
_POWERSHELL_HELPER_NAMES = frozenset(
    {
        "Resolve-Release",
        "Get-VerifiedReleaseWheel",
        "Install-FreeClaudeCode",
        "Ensure-Uv",
        "Confirm-Uv",
        "Get-UvVersion",
        "Convert-UvVersionOutput",
        "Test-UvVersionAtLeast",
        "New-DesktopShortcut",
    }
)

FCC_LATEST_RELEASE_URL = (
    "https://api.github.com/repos/FiredMosquito831/my-claude-code/releases/latest"
)

# Mirrors the shape the installers parse: the first "tag_name" line and the
# first "digest" line each on their own line.
RELEASE_FEED_JSON = f"""{{
  "tag_name": "v{FCC_VERSION}",
  "name": "v{FCC_VERSION}",
  "assets": [
    {{
      "name": "{FCC_WHEEL_NAME}",
      "digest": "sha256:{FCC_WHEEL_SHA256}",
      "browser_download_url": "{FCC_WHEEL_URL}"
    }}
  ]
}}
"""

PINNED_FEED_URL = (
    "https://api.github.com/repos/FiredMosquito831/my-claude-code/releases/tags/"
    f"v{FCC_VERSION}"
)

# Same shape as RELEASE_FEED_JSON, but the wheel asset carries no digest while
# a sibling asset after it does one: a correctly scoped extractor must refuse
# the install instead of borrowing the sibling's digest.
RELEASE_FEED_NO_TARGET_DIGEST_JSON = f"""{{
  "tag_name": "v{FCC_VERSION}",
  "name": "v{FCC_VERSION}",
  "assets": [
    {{
      "name": "{FCC_WHEEL_NAME}",
      "browser_download_url": "{FCC_WHEEL_URL}"
    }},
    {{
      "name": "sibling-asset.zip",
      "digest": "sha256:{"b" * 64}",
      "browser_download_url": "https://example.invalid/sibling-asset.zip"
    }}
  ]
}}
"""


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _write_executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _braced_body(text: str, declaration: str) -> str:
    start = text.index(declaration)
    brace_start = text.index("{", start)
    depth = 0
    for index, char in enumerate(text[brace_start:], start=brace_start):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[brace_start + 1 : index]
    raise AssertionError(f"Unclosed function body for {declaration}")


def _posix_command(name: str) -> str:
    help_output = (
        '    echo "  --extension, -e <path>  Load an extension"\n'
        '    echo "  --models <patterns>     Scope models"'
        if name == "pi"
        else "    :"
    )
    return f"""#!/bin/sh
echo "{name}:$*" >> "$CALL_LOG"
if [ "$FAIL_STEP" = "{name}-verify" ]; then
    exit 31
fi
if [ "${{1:-}}" = "--version" ]; then
    echo "{name} 1.0.0"
fi
if [ "${{1:-}}" = "--help" ]; then
{help_output}
fi
"""


def _posix_npm_command() -> str:
    return """#!/bin/sh
echo "npm:$*" >> "$CALL_LOG"
if [ "${1:-}" = "prefix" ] && [ "${2:-}" = "-g" ]; then
    printf '%s\n' "$FAKE_NPM_PREFIX"
    exit 0
fi
if [ "${1:-}" = "config" ] && [ "${2:-}" = "get" ] && [ "${3:-}" = "prefix" ]; then
    printf '%s\n' "$FAKE_NPM_PREFIX"
    exit 0
fi
exit 71
"""


def _posix_uv_command(version: str) -> str:
    return f"""#!/bin/sh
echo "uv:$*" >> "$CALL_LOG"
if [ "${{1:-}}" = "--version" ]; then
    if [ "$FAIL_STEP" = "uv-verify" ]; then
        exit 32
    fi
    echo "uv {version}"
    exit 0
fi
if [ "${{1:-}}" = "tool" ] && [ "${{2:-}}" = "install" ]; then
    if [ "$FAIL_STEP" = "fcc-install" ]; then
        exit 33
    fi
    mkdir -p "$FAKE_TOOL_BIN"
    for name in mcc-server mcc-claude mcc-claude-old mcc-codex mcc-pi \
        mcc-opencode mcc-opencode2 mcc-kilo mcc-commandcode mcc-kimi \
        mcc-qwen mcc-crush \
        mcc-init mcc-chatgpt-oauth-login mcc-compact-log mcc-help mcc-rtk \
        mcc-desktop my-claude-code fcc-server fcc-claude fcc-claude-old fcc-pi \
        fcc-init fcc-chatgpt-oauth-login fcc-compact-log free-claude-code \
        fcc-codex; do
        cp "$FAKE_FIXTURES/fcc-command.sh" "$FAKE_TOOL_BIN/$name"
    done
    if [ "$FAIL_STEP" = "fcc-missing" ]; then
        rm -f "$FAKE_TOOL_BIN/mcc-server"
    fi
    chmod +x "$FAKE_TOOL_BIN"/fcc-* "$FAKE_TOOL_BIN"/mcc-* "$FAKE_TOOL_BIN"/my-claude-code "$FAKE_TOOL_BIN"/free-claude-code 2>/dev/null || true
    exit 0
fi
if [ "${{1:-}}" = "tool" ] && [ "${{2:-}}" = "update-shell" ]; then
    if [ "$FAIL_STEP" = "path-update" ]; then
        exit 34
    fi
    exit 0
fi
if [ "${{1:-}}" = "tool" ] && [ "${{2:-}}" = "dir" ] && [ "${{3:-}}" = "--bin" ]; then
    printf '%s\n' "$FAKE_TOOL_BIN"
    exit 0
fi
exit 35
"""


@dataclass
class PosixHarness:
    root: Path
    bin_dir: Path
    fixtures: Path
    tool_bin: Path
    log: Path
    env: dict[str, str]

    def add_uv(self, version: str) -> None:
        _write_executable(self.bin_dir / "uv", _posix_uv_command(version))

    def run(self, *args: str, fail_step: str = "") -> subprocess.CompletedProcess[str]:
        env = self.env | {"FAIL_STEP": fail_step}
        return subprocess.run(
            ["/bin/sh", str(_repo_root() / "scripts" / "install.sh"), *args],
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

    def calls(self) -> list[str]:
        if not self.log.exists():
            return []
        return self.log.read_text(encoding="utf-8").splitlines()


@pytest.fixture
def posix_harness(tmp_path: Path) -> PosixHarness:
    if os.name == "nt":
        pytest.skip("POSIX installer scenarios run on POSIX hosts")

    bin_dir = tmp_path / "bin"
    fixtures = tmp_path / "fixtures"
    tool_bin = tmp_path / "tool-bin"
    home = tmp_path / "home"
    log = tmp_path / "calls.log"
    for path in (bin_dir, fixtures, tool_bin, home):
        path.mkdir(parents=True)

    _write_executable(
        bin_dir / "curl",
        """#!/bin/sh
url=""
output=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        -o)
            shift
            output=$1
            ;;
        http*)
            url=$1
            ;;
    esac
    shift
done
echo "download:$url" >> "$CALL_LOG"
case "$url:$FAIL_STEP" in
    *astral.sh*:uv-download|*github.com/FiredMosquito831*:fcc-download)
        exit 41
        ;;
esac
case "$url:$FAIL_STEP" in
    *api.github.com*:fcc-feed)
        exit 43
        ;;
esac
case "$url" in
    *api.github.com*)
        cat "$FAKE_FIXTURES/release-feed.json"
        exit 0
        ;;
esac
case "$url" in
    *astral.sh*) source="$FAKE_FIXTURES/uv-installer.sh" ;;
    *github.com/FiredMosquito831*) source="$FAKE_FIXTURES/release-wheel.whl" ;;
    *) exit 42 ;;
esac
cp "$source" "$output"
""",
    )
    _write_executable(
        bin_dir / "sha256sum",
        f"""#!/bin/sh
echo "sha256sum:$*" >> "$CALL_LOG"
if [ "$FAIL_STEP" = "fcc-checksum" ]; then
    printf '%064d  %s\\n' 0 "$1"
else
    printf '%s  %s\\n' "{FCC_WHEEL_SHA256}" "$1"
fi
""",
    )
    (fixtures / "release-wheel.whl").write_bytes(b"test release wheel")
    (fixtures / "release-feed.json").write_text(RELEASE_FEED_JSON, encoding="utf-8")
    _write_executable(
        fixtures / "uv-installer.sh",
        """#!/bin/sh
echo "uv-install" >> "$CALL_LOG"
[ "$FAIL_STEP" = "uv-install" ] && exit 23
mkdir -p "$HOME/.local/bin"
cp "$FAKE_FIXTURES/uv-command.sh" "$HOME/.local/bin/uv"
chmod +x "$HOME/.local/bin/uv"
""",
    )
    _write_executable(fixtures / "claude-command.sh", _posix_command("claude"))
    _write_executable(fixtures / "codex-command.sh", _posix_command("codex"))
    _write_executable(fixtures / "pi-command.sh", _posix_command("pi"))
    _write_executable(fixtures / "uv-command.sh", _posix_uv_command("0.11.28"))
    _write_executable(
        fixtures / "fcc-command.sh",
        f"""#!/bin/sh
name=${{0##*/}}
echo "$name:$*" >> "$CALL_LOG"
if [ "$FAIL_STEP" = "fcc-verify" ]; then
    exit 36
fi
if [ "$name" = "fcc-server" ] && [ "${{1:-}}" = "--version" ]; then
    echo "free-claude-code {FCC_VERSION}"
fi
if [ "$name" = "mcc-server" ] && [ "${{1:-}}" = "--version" ]; then
    echo "my-claude-code {FCC_VERSION}"
fi
""",
    )

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "HOME": str(home),
            "CALL_LOG": str(log),
            "FAKE_FIXTURES": str(fixtures),
            "FAKE_TOOL_BIN": str(tool_bin),
            "FAIL_STEP": "",
        }
    )
    env.pop("XDG_BIN_HOME", None)
    return PosixHarness(tmp_path, bin_dir, fixtures, tool_bin, log, env)


def test_install_sh_fresh_install_is_verified(posix_harness: PosixHarness) -> None:
    result = posix_harness.run()

    assert result.returncode == 0, result.stderr
    assert "is installed and verified." in result.stdout
    calls = posix_harness.calls()
    assert calls.index("uv-install") < calls.index("uv:--version")
    assert any(
        call.startswith(
            "uv:tool install --force --refresh-package my-claude-code "
            "--python 3.14.0 my-claude-code @ file://"
        )
        and FCC_WHEEL_NAME in call
        for call in calls
    )
    assert f"download:{FCC_WHEEL_URL}" in calls
    assert any(call.startswith("sha256sum:") for call in calls)
    assert not any(call.startswith("git:") for call in calls)
    # The version comes from the release feed, not a pin baked into the script.
    assert f"download:{FCC_LATEST_RELEASE_URL}" in calls
    assert not any(
        host in call
        for call in calls
        for host in ("claude.ai", "chatgpt.com", "pi.dev")
    ), calls


def test_install_sh_digest_comes_from_the_asset_not_the_release_body(
    posix_harness: PosixHarness,
) -> None:
    """A release body that mentions a sha256 must not pollute the wheel digest.

    GitHub places the release ``body`` after the ``assets`` in the payload, and
    release notes often repeat the wheel digest as prose (the v5.0.0 notes do).
    The installer must verify against the asset's own ``digest`` field, never a
    digest borrowed from any other object in the feed.
    """
    poisoned = RELEASE_FEED_JSON.replace(
        '"name": "v{FCC_VERSION}",',
        '"name": "v{FCC_VERSION}",\n  "body": "built sha256:0000000000000000000000000000000000000000000000000000000000000000",',
        1,
    )
    (posix_harness.fixtures / "release-feed.json").write_text(
        poisoned, encoding="utf-8"
    )

    result = posix_harness.run()

    # The body's bogus digest must not have been used, or the checksum check
    # (which compares against the real FCC_WHEEL_SHA256) would have refused.
    assert result.returncode == 0, result.stderr
    assert "is installed and verified." in result.stdout


def test_install_sh_pinned_version_installs_verified_from_tag_feed(
    posix_harness: PosixHarness,
) -> None:
    """An explicit --version stays verified via the tag-scoped release feed."""
    result = posix_harness.run("--version", FCC_VERSION)

    assert result.returncode == 0, result.stderr
    assert "Verified FCC v" in result.stdout
    assert "is installed and verified." in result.stdout
    calls = posix_harness.calls()
    assert f"download:{PINNED_FEED_URL}" in calls
    assert f"download:{FCC_WHEEL_URL}" in calls
    assert any(call.startswith("sha256sum:") for call in calls)
    # A pin resolves against its own tag's feed, never /releases/latest.
    assert f"download:{FCC_LATEST_RELEASE_URL}" not in calls


def test_install_sh_pinned_version_proceeds_unverified_when_feed_unreachable(
    posix_harness: PosixHarness,
) -> None:
    result = posix_harness.run("--version", FCC_VERSION, fail_step="fcc-feed")

    assert result.returncode == 0, result.stderr
    assert "proceeding unverified" in result.stderr
    assert "is installed and verified." in result.stdout
    calls = posix_harness.calls()
    assert f"download:{PINNED_FEED_URL}" in calls
    assert f"download:{FCC_WHEEL_URL}" in calls
    # Unverified by design: no expected digest existed, so nothing was hashed.
    assert not any(call.startswith("sha256sum:") for call in calls)


def test_install_sh_pinned_version_refuses_target_asset_without_digest(
    posix_harness: PosixHarness,
) -> None:
    (posix_harness.fixtures / "release-feed.json").write_text(
        RELEASE_FEED_NO_TARGET_DIGEST_JSON, encoding="utf-8"
    )

    result = posix_harness.run("--version", FCC_VERSION)

    assert result.returncode != 0
    assert "No digest published for this asset" in result.stderr
    assert "is installed and verified." not in result.stdout
    assert not any("uv:tool install" in call for call in posix_harness.calls())


def test_install_sh_refuses_latest_release_asset_without_digest(
    posix_harness: PosixHarness,
) -> None:
    """The shared no-digest guard also covers the default latest-release path."""
    (posix_harness.fixtures / "release-feed.json").write_text(
        RELEASE_FEED_NO_TARGET_DIGEST_JSON, encoding="utf-8"
    )

    result = posix_harness.run()

    assert result.returncode != 0
    assert "No digest published for this asset" in result.stderr
    assert not any("uv:tool install" in call for call in posix_harness.calls())


def test_install_sh_replaces_obsolete_uv(posix_harness: PosixHarness) -> None:
    posix_harness.add_uv("0.5.9")

    result = posix_harness.run()

    assert result.returncode == 0, result.stderr
    assert "uv 0.5.9 is below 0.11.0" in result.stdout
    assert "uv-install" in posix_harness.calls()


@pytest.mark.parametrize(
    "failure",
    [
        "uv-download",
        "uv-install",
        "uv-verify",
        "fcc-download",
        "fcc-checksum",
        "fcc-install",
        "path-update",
        "fcc-missing",
        "fcc-verify",
    ],
)
def test_install_sh_stops_without_success_on_each_failure(
    posix_harness: PosixHarness,
    failure: str,
) -> None:
    result = posix_harness.run(fail_step=failure)

    assert result.returncode != 0
    assert "is installed and verified." not in result.stdout
    forbidden = {
        "uv-download": "uv-install",
        "uv-install": "uv:--version",
        "uv-verify": "uv:tool install",
        "fcc-download": "sha256sum:",
        "fcc-checksum": "uv:tool install",
        "fcc-install": "uv:tool update-shell",
        "path-update": "uv:tool dir --bin",
        "fcc-missing": "mcc-server:--version",
    }.get(failure)
    if forbidden is not None:
        assert not any(forbidden in call for call in posix_harness.calls())


def test_install_sh_dry_run_never_executes_commands(
    posix_harness: PosixHarness,
) -> None:
    result = posix_harness.run("--dry-run")

    assert result.returncode == 0, result.stderr
    assert posix_harness.calls() == [f"download:{FCC_LATEST_RELEASE_URL}"], (
        "a dry run may read the release feed, but must change nothing"
    )
    assert "Dry run complete. No changes were made." in result.stdout
    assert "is installed and verified." not in result.stdout


def test_install_sh_rejects_unparseable_existing_uv(
    posix_harness: PosixHarness,
) -> None:
    posix_harness.add_uv("not-a-version")

    result = posix_harness.run()

    assert result.returncode != 0
    assert not any("astral.sh" in call for call in posix_harness.calls())


def test_install_sh_voice_flags_only_change_fcc_spec(
    posix_harness: PosixHarness,
) -> None:
    result = posix_harness.run("--voice-all", "--torch-backend", "cu130")

    assert result.returncode == 0, result.stderr
    assert any(
        "--torch-backend cu130 my-claude-code[voice,voice_local] @ file://" in call
        and FCC_WHEEL_NAME in call
        for call in posix_harness.calls()
    )


def test_install_sh_rejects_invalid_options_before_mutation(
    posix_harness: PosixHarness,
) -> None:
    result = posix_harness.run("--torch-backend", "cu130")

    assert result.returncode != 0
    assert posix_harness.calls() == []


def _powershells() -> tuple[str, ...]:
    candidates = (shutil.which("pwsh"), shutil.which("powershell"))
    return tuple(dict.fromkeys(path for path in candidates if path is not None))


def _batch_client(name: str) -> str:
    help_output = (
        "echo   --extension, -e ^<path^>  Load an extension\n"
        "echo   --models ^<patterns^>     Scope models"
        if name == "pi"
        else "rem no product help"
    )
    return f"""@echo off
echo {name}:%*>>"%CALL_LOG%"
if "%FAIL_STEP%"=="{name}-verify" exit /b 51
if "%1"=="--version" echo {name} 1.0.0
if "%1"=="--help" (
{help_output}
)
exit /b 0
"""


def _batch_npm() -> str:
    return r"""@echo off
echo npm:%*>>"%CALL_LOG%"
if "%1"=="prefix" if "%2"=="-g" echo %FAKE_NPM_PREFIX%& exit /b 0
if "%1"=="config" if "%2"=="get" if "%3"=="prefix" echo %FAKE_NPM_PREFIX%& exit /b 0
exit /b 71
"""


def _batch_uv(version: str) -> str:
    return rf"""@echo off
echo uv:%*>>"%CALL_LOG%"
if "%1"=="--version" goto version
if "%1"=="tool" if "%2"=="install" goto install
if "%1"=="tool" if "%2"=="update-shell" goto update_shell
if "%1"=="tool" if "%2"=="dir" if "%3"=="--bin" goto tool_bin
exit /b 59
:version
if "%FAIL_STEP%"=="uv-verify" exit /b 52
echo uv {version}
exit /b 0
:install
if "%FAIL_STEP%"=="fcc-install" exit /b 53
if not exist "%FAKE_TOOL_BIN%" mkdir "%FAKE_TOOL_BIN%"
for %%N in (mcc-server mcc-claude mcc-claude-old mcc-codex mcc-pi mcc-opencode mcc-opencode2 mcc-kilo mcc-commandcode mcc-kimi mcc-qwen mcc-crush mcc-init mcc-chatgpt-oauth-login mcc-compact-log mcc-help mcc-rtk mcc-desktop my-claude-code fcc-server fcc-claude fcc-claude-old fcc-codex fcc-pi fcc-init fcc-chatgpt-oauth-login fcc-compact-log free-claude-code) do copy /y "%FAKE_FIXTURES%\fcc-command.cmd" "%FAKE_TOOL_BIN%\%%N.cmd" >nul
if "%FAIL_STEP%"=="fcc-missing" del /q "%FAKE_TOOL_BIN%\mcc-server.cmd" >nul
exit /b 0
:update_shell
if "%FAIL_STEP%"=="path-update" exit /b 54
exit /b 0
:tool_bin
echo %FAKE_TOOL_BIN%
exit /b 0
"""


@dataclass
class PowerShellHarness:
    root: Path
    bin_dir: Path
    fixtures: Path
    tool_bin: Path
    log: Path
    env: dict[str, str]
    powershell: str
    wrapper: Path

    def add_client(self, name: str) -> None:
        _write_executable(self.bin_dir / f"{name}.cmd", _batch_client(name))

    def add_unrelated_pi(self) -> None:
        _write_executable(self.bin_dir / "pi.cmd", _batch_client("unrelated-pi"))

    def add_npm_prefix(self, prefix: Path) -> None:
        prefix.mkdir(parents=True)
        self.env["FAKE_NPM_PREFIX"] = str(prefix)
        _write_executable(self.bin_dir / "npm.cmd", _batch_npm())

    def add_uv(self, version: str) -> None:
        _write_executable(self.bin_dir / "uv.cmd", _batch_uv(version))

    def run(self, *args: str, fail_step: str = "") -> subprocess.CompletedProcess[str]:
        env = self.env | {"FAIL_STEP": fail_step}
        return subprocess.run(
            [
                self.powershell,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(self.wrapper),
                *args,
            ],
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )

    def calls(self) -> list[str]:
        if not self.log.exists():
            return []
        return self.log.read_text(encoding="utf-8").splitlines()


@pytest.fixture(
    params=[
        pytest.param(
            path,
            id=Path(path).name,
            marks=pytest.mark.xdist_group(
                name=f"powershell-installer-{Path(path).stem.lower()}"
            ),
        )
        for path in _powershells()
    ]
    or [pytest.param(None, id="unavailable")],
)
def powershell_harness(
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> PowerShellHarness:
    powershell = request.param
    if powershell is None or os.name != "nt":
        pytest.skip("PowerShell installer scenarios run on Windows hosts")

    bin_dir = tmp_path / "bin"
    fixtures = tmp_path / "fixtures"
    tool_bin = tmp_path / "tool-bin"
    home = tmp_path / "home"
    local_app_data = tmp_path / "local-app-data"
    app_data = tmp_path / "app-data"
    log = tmp_path / "calls.log"
    for path in (bin_dir, fixtures, tool_bin, home, local_app_data, app_data):
        path.mkdir(parents=True)

    (fixtures / "claude-command.cmd").write_text(
        _batch_client("claude"), encoding="utf-8"
    )
    (fixtures / "codex-command.cmd").write_text(
        _batch_client("codex"), encoding="utf-8"
    )
    (fixtures / "pi-command.cmd").write_text(_batch_client("pi"), encoding="utf-8")
    (fixtures / "uv-command.cmd").write_text(_batch_uv("0.11.28"), encoding="utf-8")
    (fixtures / "fcc-command.cmd").write_text(
        f"""@echo off
for %%I in ("%~f0") do set "FCC_NAME=%%~nI"
echo %FCC_NAME%:%*>>"%CALL_LOG%"
if "%FAIL_STEP%"=="fcc-verify" exit /b 55
if "%FCC_NAME%"=="fcc-server" if "%1"=="--version" echo free-claude-code {FCC_VERSION}
if "%FCC_NAME%"=="mcc-server" if "%1"=="--version" echo my-claude-code {FCC_VERSION}
exit /b 0
""",
        encoding="utf-8",
    )
    (fixtures / "release-wheel.whl").write_bytes(b"test release wheel")
    (fixtures / "release-feed.json").write_text(RELEASE_FEED_JSON, encoding="utf-8")
    (fixtures / "claude-installer.ps1").write_text(
        r"""if ($env:FAIL_STEP -eq "claude-install") { exit 61 }
$bin = Join-Path $env:USERPROFILE ".local\bin"
New-Item -ItemType Directory -Force -Path $bin | Out-Null
Copy-Item (Join-Path $env:FAKE_FIXTURES "claude-command.cmd") (Join-Path $bin "claude.cmd") -Force
Add-Content -LiteralPath $env:CALL_LOG -Value "claude-install"
""",
        encoding="utf-8",
    )
    (fixtures / "codex-installer.ps1").write_text(
        r"""if ($env:FAIL_STEP -eq "codex-install") { exit 62 }
$bin = Join-Path $env:LOCALAPPDATA "Programs\OpenAI\Codex\bin"
New-Item -ItemType Directory -Force -Path $bin | Out-Null
Copy-Item (Join-Path $env:FAKE_FIXTURES "codex-command.cmd") (Join-Path $bin "codex.cmd") -Force
Add-Content -LiteralPath $env:CALL_LOG -Value "codex-install:$env:CODEX_NON_INTERACTIVE"
""",
        encoding="utf-8",
    )
    (fixtures / "pi-installer.ps1").write_text(
        r"""if ($env:FAIL_STEP -eq "pi-install") { exit 64 }
$bin = if ($env:FAKE_NPM_PREFIX) { $env:FAKE_NPM_PREFIX } else { Join-Path $env:APPDATA "npm" }
New-Item -ItemType Directory -Force -Path $bin | Out-Null
Copy-Item (Join-Path $env:FAKE_FIXTURES "pi-command.cmd") (Join-Path $bin "pi.cmd") -Force
Add-Content -LiteralPath $env:CALL_LOG -Value "pi-install"
""",
        encoding="utf-8",
    )
    (fixtures / "uv-installer.ps1").write_text(
        r"""if ($env:FAIL_STEP -eq "uv-install") { exit 63 }
$bin = Join-Path $env:USERPROFILE ".local\bin"
New-Item -ItemType Directory -Force -Path $bin | Out-Null
Copy-Item (Join-Path $env:FAKE_FIXTURES "uv-command.cmd") (Join-Path $bin "uv.cmd") -Force
Add-Content -LiteralPath $env:CALL_LOG -Value "uv-install"
""",
        encoding="utf-8",
    )

    wrapper = tmp_path / "run-installer.ps1"
    wrapper.write_text(
        """Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
function Invoke-RestMethod {
    [CmdletBinding()]
    param([string] $Uri, [string] $OutFile, [hashtable] $Headers)

    Add-Content -LiteralPath $env:CALL_LOG -Value "download:$Uri"
    if ($Uri.Contains("api.github.com")) {
        return (Get-Content -LiteralPath (Join-Path $env:FAKE_FIXTURES "release-feed.json") -Raw | ConvertFrom-Json)
    }
    if (
        ($env:FAIL_STEP -eq "uv-download" -and $Uri.Contains("astral.sh")) -or
        ($env:FAIL_STEP -eq "fcc-download" -and $Uri.Contains("github.com/FiredMosquito831"))
    ) {
        throw "simulated download failure"
    }
    if ($Uri.Contains("astral.sh")) {
        $source = Join-Path $env:FAKE_FIXTURES "uv-installer.ps1"
    }
    elseif ($Uri.Contains("github.com/FiredMosquito831")) {
        $source = Join-Path $env:FAKE_FIXTURES "release-wheel.whl"
    }
    else {
        throw "unexpected installer URL: $Uri"
    }
    Copy-Item -LiteralPath $source -Destination $OutFile -Force
}
function Get-FileHash {
    [CmdletBinding()]
    param([string] $LiteralPath, [string] $Algorithm)

    Add-Content -LiteralPath $env:CALL_LOG -Value "sha256:$LiteralPath"
    $hash = if ($env:FAIL_STEP -eq "fcc-checksum") {
        "0000000000000000000000000000000000000000000000000000000000000000"
    }
    else {
        "679565810225215AE3C045CC5C8EF43E4FA53676179DDB1583A25412E811B770"
    }
    return [pscustomobject]@{ Hash = $hash }
}
$installer = [scriptblock]::Create([IO.File]::ReadAllText($env:FCC_INSTALLER))
& $installer @args
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
            "USERPROFILE": str(home),
            "LOCALAPPDATA": str(local_app_data),
            "APPDATA": str(app_data),
            "CALL_LOG": str(log),
            "FAKE_FIXTURES": str(fixtures),
            "FAKE_TOOL_BIN": str(tool_bin),
            "FCC_INSTALLER": str(_repo_root() / "scripts" / "install.ps1"),
            "FAIL_STEP": "",
        }
    )
    return PowerShellHarness(
        tmp_path, bin_dir, fixtures, tool_bin, log, env, powershell, wrapper
    )


def test_install_ps1_fresh_install_is_verified(
    powershell_harness: PowerShellHarness,
) -> None:
    result = powershell_harness.run()

    assert result.returncode == 0, result.stderr
    assert "is installed and verified." in result.stdout
    calls = powershell_harness.calls()
    assert calls.index("uv-install") < calls.index("uv:--version")
    assert any(
        call.startswith(
            "uv:tool install --force --refresh-package my-claude-code "
            '--python 3.14.0 "my-claude-code @ file:///'
        )
        and FCC_WHEEL_NAME in call
        for call in calls
    )
    assert f"download:{FCC_WHEEL_URL}" in calls
    assert any(call.startswith("sha256:") for call in calls)
    assert not any(call.startswith("git:") for call in calls)
    # The version comes from the release feed, not a pin baked into the script.
    assert f"download:{FCC_LATEST_RELEASE_URL}" in calls
    assert not any(
        host in call
        for call in calls
        for host in ("claude.ai", "chatgpt.com", "pi.dev")
    ), calls


def test_install_ps1_replaces_obsolete_uv(
    powershell_harness: PowerShellHarness,
) -> None:
    powershell_harness.add_uv("0.5.9")

    result = powershell_harness.run()

    assert result.returncode == 0, result.stderr
    assert "uv 0.5.9 is below 0.11.0" in result.stdout
    assert "uv-install" in powershell_harness.calls()


@pytest.mark.parametrize(
    "failure",
    [
        "uv-download",
        "uv-install",
        "uv-verify",
        "fcc-download",
        "fcc-checksum",
        "fcc-install",
        "path-update",
        "fcc-missing",
        "fcc-verify",
    ],
)
def test_install_ps1_stops_without_success_on_each_failure(
    powershell_harness: PowerShellHarness,
    failure: str,
) -> None:
    result = powershell_harness.run(fail_step=failure)

    assert result.returncode != 0
    assert "is installed and verified." not in result.stdout
    forbidden = {
        "uv-download": "uv-install",
        "uv-install": "uv:--version",
        "uv-verify": "uv:tool install",
        "fcc-download": "sha256:",
        "fcc-checksum": "uv:tool install",
        "fcc-install": "uv:tool update-shell",
        "path-update": "uv:tool dir --bin",
        "fcc-missing": "mcc-server:--version",
    }.get(failure)
    if forbidden is not None:
        assert not any(forbidden in call for call in powershell_harness.calls())


def test_install_ps1_dry_run_never_executes_commands(
    powershell_harness: PowerShellHarness,
) -> None:
    result = subprocess.run(
        [
            powershell_harness.powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(_repo_root() / "scripts" / "install.ps1"),
            "-DryRun",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=powershell_harness.env,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert powershell_harness.calls() == []
    assert "Dry run complete. No changes were made." in result.stdout
    assert "is installed and verified." not in result.stdout


def test_install_ps1_rejects_unparseable_existing_uv(
    powershell_harness: PowerShellHarness,
) -> None:
    powershell_harness.add_uv("not-a-version")

    result = powershell_harness.run()

    assert result.returncode != 0
    assert not any("astral.sh" in call for call in powershell_harness.calls())


def test_install_ps1_voice_flags_only_change_fcc_spec(
    powershell_harness: PowerShellHarness,
) -> None:
    result = powershell_harness.run("-VoiceAll", "-TorchBackend", "cu130")

    assert result.returncode == 0, result.stderr
    assert any(
        '--torch-backend cu130 "my-claude-code[voice,voice_local] @ file:///' in call
        and FCC_WHEEL_NAME in call
        for call in powershell_harness.calls()
    )


def test_install_ps1_deferred_helper_invokes_uv_as_command() -> None:
    # The deferred (app running) path hands a detached helper that must run
    # `uv tool install`. It must call uv through the call operator on an array,
    # never by placing a command string at statement position: PowerShell treats
    # a statement-position string as a command NAME and never executes it, so uv
    # would not run and the staged update would create no mcc-* commands.
    powershell = (_repo_root() / "scripts" / "install.ps1").read_text(encoding="utf-8")

    assert "& $uvPath @$installArgs" in powershell
    assert "$argumentsLiteral" not in powershell


def test_install_ps1_deferred_helper_runs_uv(
    powershell_harness: PowerShellHarness,
    tmp_path: Path,
) -> None:
    # End-to-end proof: load the real Start-DeferredInstall, point it at a stub
    # uv, and let a short-lived process stand in for the running launcher. The
    # detached helper must actually invoke `uv tool install` once the process
    # exits -- otherwise the staged update installs nothing.
    installer_text = (_repo_root() / "scripts" / "install.ps1").read_text(
        encoding="utf-8"
    )
    func_body = _braced_body(installer_text, "function Start-DeferredInstall")
    func_file = tmp_path / "StartDeferredInstall.ps1"
    func_file.write_text(
        "function Start-DeferredInstall {\n" + func_body + "\n}\n", encoding="utf-8"
    )

    stub_dir = tmp_path / "stubuv"
    stub_dir.mkdir(parents=True)
    uv_log = stub_dir / "uv-calls.log"
    stub_uv = stub_dir / "stub-uv.cmd"
    stub_uv.write_text(
        '@echo off\r\necho uv:%*>>"' + str(uv_log) + '"\r\nexit /b 0\r\n',
        encoding="utf-8",
    )

    wheel_dir = tmp_path / "wheel-in"
    wheel_dir.mkdir(parents=True)
    wheel = wheel_dir / "dummy.whl"
    wheel.write_text("x", encoding="utf-8")

    runner = tmp_path / "run-deferred.ps1"
    runner.write_text(
        f"""Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. "{(func_file.as_posix())}"
function Write-MccCommandReference {{ }}
$uvPath = "{(stub_uv.as_posix())}"
$arguments = @("tool", "install", "--force", "--refresh-package", "my-claude-code", "--python", "3.14.0", "my-claude-code @ file:///{(wheel.as_posix())}")
$wheelPath = "{(wheel.as_posix())}"
$running = @(Start-Process -FilePath "ping" -ArgumentList "-n", "3", "127.0.0.1" -PassThru)
Start-DeferredInstall -UvPath $uvPath -Arguments $arguments -WheelPath $wheelPath -Running $running -Version "5.2.2"
$deadline = (Get-Date).AddSeconds(25)
while ((Get-Date) -lt $deadline) {{
    if (Test-Path -LiteralPath "{(uv_log.as_posix())}") {{
        $c = Get-Content -LiteralPath "{(uv_log.as_posix())}" -Raw
        if ($c -match "tool install") {{ Write-Host "DEFERRED_UV_OK"; exit 0 }}
    }}
    Start-Sleep -Milliseconds 500
}}
Write-Host "DEFERRED_UV_MISSING"
exit 2
""",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            powershell_harness.powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(runner),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=powershell_harness.env,
    )
    assert "DEFERRED_UV_OK" in result.stdout, result.stdout + result.stderr


def _extract_function_definition(installer_text: str, name: str) -> str:
    """Return a dot-sourcable `function <name> { ... }` block from install.ps1."""
    body = _braced_body(installer_text, f"function {name}")
    return f"function {name} {{\n{body}\n}}\n"


def test_install_ps1_rename_reinstall_renames_tool_dir_and_runs_uv(
    powershell_harness: PowerShellHarness,
    tmp_path: Path,
) -> None:
    # The running-launcher path now renames the old tool env aside and installs
    # fresh immediately, so open windows keep old code and new sessions get the
    # update (no waiting for launchers to exit). This is the same Windows rename
    # semantic proven live: a loaded .pyd / the whole tool dir CAN be renamed.
    installer_text = (_repo_root() / "scripts" / "install.ps1").read_text(
        encoding="utf-8"
    )
    func_file = tmp_path / "RenameThenReinstall.ps1"
    func_file.write_text(
        _extract_function_definition(installer_text, "Invoke-RenameThenReinstall"),
        encoding="utf-8",
    )

    stub_dir = tmp_path / "stubuv"
    stub_dir.mkdir(parents=True)
    uv_log = stub_dir / "uv-calls.log"
    stub_uv = stub_dir / "stub-uv.cmd"
    # The stub mimics real `uv`: records invocations, answers `tool dir` and
    # `tool dir --bin`, and on `tool install` recreates the canonical tool dir.
    stub_uv.write_text(
        '@echo off\r\necho uv:%*>>"' + str(uv_log) + '"\r\n'
        'if "%1"=="tool" if "%2"=="dir" if "%3"=="--bin" echo %FAKE_TOOL_ROOT%& exit /b 0\r\n'
        'if "%1"=="tool" if "%2"=="dir" echo %FAKE_TOOL_ROOT%& exit /b 0\r\n'
        'if not "%FAKE_TOOL_ROOT%"=="" mkdir "%FAKE_TOOL_ROOT%\\my-claude-code" 2>nul\r\n'
        "exit /b 0\r\n",
        encoding="utf-8",
    )

    wheel_dir = tmp_path / "wheel-in"
    wheel_dir.mkdir(parents=True)
    wheel = wheel_dir / "dummy.whl"
    wheel.write_text("x", encoding="utf-8")

    tool_root = tmp_path / "tools"
    tool_root.mkdir(parents=True)
    tool_dir = tool_root / "my-claude-code"
    tool_dir.mkdir()
    (tool_dir / "marker.txt").write_text("old", encoding="utf-8")

    runner = tmp_path / "run-rename.ps1"
    runner.write_text(
        f"""Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
function Invoke-NativeCommand {{
    param([string] $FilePath, [string[]] $Arguments = @())
    $global:LASTEXITCODE = 0
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {{ throw "Command failed with exit code $($LASTEXITCODE): $FilePath" }}
}}
function Invoke-NativeCapture {{
    param([string] $FilePath, [string[]] $Arguments = @())
    $global:LASTEXITCODE = 0
    $out = & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {{ throw "Command failed with exit code $($LASTEXITCODE): $FilePath" }}
    return ($out | Out-String).Trim()
}}
. "{(func_file.as_posix())}"
$uvPath = "{(stub_uv.as_posix())}"
$arguments = @("tool", "install", "--force", "--refresh-package", "my-claude-code", "--python", "3.14.0", "my-claude-code @ file:///{(wheel.as_posix())}")
$wheelPath = "{(wheel.as_posix())}"
$toolDir = "{(tool_dir.as_posix())}"
$ok = Invoke-RenameThenReinstall -UvPath $uvPath -Arguments $arguments -WheelPath $wheelPath -ToolDir $toolDir -Version "5.3.2"
if ($ok -ne $true) {{ throw "expected rename+reinstall to report success, got: $ok" }}
$oldDirs = @(Get-ChildItem -Path "{(tool_root.as_posix())}" -Directory -Filter "my-claude-code.old-*")
if ($oldDirs.Count -gt 0) {{ throw "old dir was NOT removed after successful install: $($oldDirs.Count) remain" }}
if (-not (Test-Path -LiteralPath "{(tool_dir.as_posix())}" -PathType Container)) {{ throw "fresh tool dir not recreated by install" }}
if (-not (Test-Path -LiteralPath "{(uv_log.as_posix())}")) {{ throw "stub uv never invoked" }}
$c = Get-Content -LiteralPath "{(uv_log.as_posix())}" -Raw
if ($c -notmatch "tool install") {{ throw "stub uv not called with tool install: $c" }}
Write-Host "RENAME_REINSTALL_OK"
""",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            powershell_harness.powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(runner),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=powershell_harness.env | {"FAKE_TOOL_ROOT": str(tool_root)},
    )
    assert "RENAME_REINSTALL_OK" in result.stdout, result.stdout + result.stderr


def test_install_ps1_rename_reinstall_restores_old_dir_on_failed_install(
    powershell_harness: PowerShellHarness,
    tmp_path: Path,
) -> None:
    # If the fresh install fails after the rename, the old tool env must be
    # renamed back so the user is never left with no working install.
    installer_text = (_repo_root() / "scripts" / "install.ps1").read_text(
        encoding="utf-8"
    )
    func_file = tmp_path / "RenameThenReinstall.ps1"
    func_file.write_text(
        _extract_function_definition(installer_text, "Invoke-RenameThenReinstall"),
        encoding="utf-8",
    )

    stub_dir = tmp_path / "stubuv"
    stub_dir.mkdir(parents=True)
    fail_uv = stub_dir / "fail-uv.cmd"
    fail_uv.write_text(
        "@echo off\r\n"
        'if "%1"=="tool" if "%2"=="dir" if "%3"=="--bin" echo %FAKE_TOOL_ROOT%& exit /b 0\r\n'
        'if "%1"=="tool" if "%2"=="dir" echo %FAKE_TOOL_ROOT%& exit /b 0\r\n'
        "exit /b 33\r\n",
        encoding="utf-8",
    )

    wheel_dir = tmp_path / "wheel-in"
    wheel_dir.mkdir(parents=True)
    wheel = wheel_dir / "dummy.whl"
    wheel.write_text("x", encoding="utf-8")

    tool_root = tmp_path / "tools"
    tool_root.mkdir(parents=True)
    tool_dir = tool_root / "my-claude-code"
    tool_dir.mkdir()
    (tool_dir / "marker.txt").write_text("old", encoding="utf-8")

    runner = tmp_path / "run-rename-fail.ps1"
    runner.write_text(
        f"""Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
function Invoke-NativeCommand {{
    param([string] $FilePath, [string[]] $Arguments = @())
    $global:LASTEXITCODE = 0
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {{ throw "Command failed with exit code $($LASTEXITCODE): $FilePath" }}
}}
function Invoke-NativeCapture {{
    param([string] $FilePath, [string[]] $Arguments = @())
    $global:LASTEXITCODE = 0
    $out = & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {{ throw "Command failed with exit code $($LASTEXITCODE): $FilePath" }}
    return ($out | Out-String).Trim()
}}
. "{(func_file.as_posix())}"
$uvPath = "{(fail_uv.as_posix())}"
$arguments = @("tool", "install", "--force", "--refresh-package", "my-claude-code", "--python", "3.14.0", "my-claude-code @ file:///{(wheel.as_posix())}")
$wheelPath = "{(wheel.as_posix())}"
$toolDir = "{(tool_dir.as_posix())}"
$threw = $false
try {{
    Invoke-RenameThenReinstall -UvPath $uvPath -Arguments $arguments -WheelPath $wheelPath -ToolDir $toolDir -Version "5.3.2"
}} catch {{
    $threw = $true
}}
if (-not $threw) {{ throw "expected the failed install to throw" }}
if (-not (Test-Path -LiteralPath "{(tool_dir.as_posix())}/marker.txt")) {{ throw "old tool dir was not restored after failed install" }}
$oldDirs = @(Get-ChildItem -Path "{(tool_root.as_posix())}" -Directory -Filter "my-claude-code.old-*")
if ($oldDirs.Count -ne 0) {{ throw "stale .old-* dir left behind: $($oldDirs.Count)" }}
Write-Host "RENAME_ROLLBACK_OK"
""",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            powershell_harness.powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(runner),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=powershell_harness.env | {"FAKE_TOOL_ROOT": str(tool_root)},
    )
    assert "RENAME_ROLLBACK_OK" in result.stdout, result.stdout + result.stderr


def test_install_ps1_rename_keeps_install_when_running_shim_is_locked(
    powershell_harness: PowerShellHarness,
    tmp_path: Path,
) -> None:
    # A running launcher's own shim cannot be overwritten (os error 32 /
    # "being used by another process"). That failure is EXPECTED: the tool dir
    # and free shims already updated, and the shim is version-agnostic. The
    # rename path must keep the new install, report success, and flag that a
    # detached helper finishes the locked shim after the window closes -- NOT
    # roll back and NOT treat it as fatal.
    installer_text = (_repo_root() / "scripts" / "install.ps1").read_text(
        encoding="utf-8"
    )
    func_file = tmp_path / "RenameThenReinstall.ps1"
    func_file.write_text(
        _extract_function_definition(installer_text, "Invoke-RenameThenReinstall"),
        encoding="utf-8",
    )

    stub_dir = tmp_path / "stubuv"
    stub_dir.mkdir(parents=True)
    uv_log = stub_dir / "uv-calls.log"
    locked_uv = stub_dir / "locked-uv.cmd"
    # uv succeeds on the tool env (recreates the dir + installs the package
    # dist-info, proving the fresh install landed) but fails copying a shim that
    # a running launcher holds -> os error 32.
    locked_uv.write_text(
        '@echo off\r\necho uv:%*>>"' + str(uv_log) + '"\r\n'
        'if "%1"=="tool" if "%2"=="dir" if "%3"=="--bin" echo %FAKE_TOOL_ROOT%& exit /b 0\r\n'
        'if "%1"=="tool" if "%2"=="dir" echo %FAKE_TOOL_ROOT%& exit /b 0\r\n'
        'if not "%FAKE_TOOL_ROOT%"=="" mkdir "%FAKE_TOOL_ROOT%\\my-claude-code" 2>nul\r\n'
        'if not "%FAKE_TOOL_ROOT%"=="" mkdir "%FAKE_TOOL_ROOT%\\my-claude-code\\Lib" 2>nul\r\n'
        'if not "%FAKE_TOOL_ROOT%"=="" mkdir "%FAKE_TOOL_ROOT%\\my-claude-code\\Lib\\site-packages" 2>nul\r\n'
        'if not "%FAKE_TOOL_ROOT%"=="" mkdir "%FAKE_TOOL_ROOT%\\my-claude-code\\Lib\\site-packages\\my_claude_code-%FAKE_INSTALL_VERSION%.dist-info" 2>nul\r\n'
        ">&2 echo Failed to install entrypoint\r\n"
        ">&2 echo Caused by: failed to copy file from ...\r\n"
        ">&2 echo ...being used by another process. (os error 32)\r\n"
        "exit /b 2\r\n",
        encoding="utf-8",
    )

    wheel_dir = tmp_path / "wheel-in"
    wheel_dir.mkdir(parents=True)
    wheel = wheel_dir / "dummy.whl"
    wheel.write_text("x", encoding="utf-8")

    tool_root = tmp_path / "tools"
    tool_root.mkdir(parents=True)
    tool_dir = tool_root / "my-claude-code"
    tool_dir.mkdir()
    (tool_dir / "marker.txt").write_text("old", encoding="utf-8")

    runner = tmp_path / "run-locked.ps1"
    runner.write_text(
        f"""Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
function Invoke-NativeCommand {{
    param([string] $FilePath, [string[]] $Arguments = @())
    $global:LASTEXITCODE = 0
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {{ throw "Command failed with exit code $($LASTEXITCODE): $FilePath" }}
}}
function Invoke-NativeCapture {{
    param([string] $FilePath, [string[]] $Arguments = @())
    $global:LASTEXITCODE = 0
    $out = & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {{ throw "Command failed with exit code $($LASTEXITCODE): $FilePath" }}
    return ($out | Out-String).Trim()
}}
$script:RenamedWhileRunning = $false
$script:NeedDeferredFinish = $false
. "{(func_file.as_posix())}"
$uvPath = "{(locked_uv.as_posix())}"
$arguments = @("tool", "install", "--force", "--refresh-package", "my-claude-code", "--python", "3.14.0", "my-claude-code @ file:///{(wheel.as_posix())}")
$wheelPath = "{(wheel.as_posix())}"
$toolDir = "{(tool_dir.as_posix())}"
$ok = Invoke-RenameThenReinstall -UvPath $uvPath -Arguments $arguments -WheelPath $wheelPath -ToolDir $toolDir -Version "5.3.3"
if ($ok -ne $true) {{ throw "expected success despite locked shim, got: $ok" }}
if ($script:NeedDeferredFinish -ne $true) {{ throw "expected NeedDeferredFinish to be set" }}
if ($script:RenamedWhileRunning -ne $true) {{ throw "expected RenamedWhileRunning to be set" }}
if (-not (Test-Path -LiteralPath "{(tool_dir.as_posix())}" -PathType Container)) {{ throw "fresh tool dir not kept" }}
Write-Host "LOCKED_SHIM_OK"
""",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            powershell_harness.powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(runner),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=powershell_harness.env
        | {"FAKE_TOOL_ROOT": str(tool_root), "FAKE_INSTALL_VERSION": "5.3.3"},
    )
    assert "LOCKED_SHIM_OK" in result.stdout, result.stdout + result.stderr


def test_install_ps1_configure_confirm_tolerates_deferred_finish(
    powershell_harness: PowerShellHarness,
    tmp_path: Path,
) -> None:
    # A deferred finish means uv aborted partway through the shim writes because
    # a running launcher's own shim could not be overwritten (os error 32), so
    # mcc-rtk / mcc-desktop were never created. Configure-AndConfirmFreeClaudeCode
    # must NOT throw "did not create 'mcc-rtk'" in that state: it verifies only
    # mcc-server when it landed, and reports "staged" when even mcc-server is
    # missing instead of failing the whole install.
    installer_text = (_repo_root() / "scripts" / "install.ps1").read_text(
        encoding="utf-8"
    )
    func_file = tmp_path / "ConfigureAndConfirm.ps1"
    func_file.write_text(
        _extract_function_definition(
            installer_text, "Configure-AndConfirmFreeClaudeCode"
        ),
        encoding="utf-8",
    )

    runner = tmp_path / "run-configure-confirm.ps1"
    runner.write_text(
        f"""Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
function Get-ApplicationCommand {{
    param([string] $Name)
    if ($Name -eq "uv") {{ return [pscustomobject]@{{ Source = "C:\\fake\\uv.exe" }} }}
    if ($Name -eq "mcc-server" -and $env:FAKE_MCC_SERVER -eq "present") {{
        return [pscustomobject]@{{ Source = "C:\\fake\\tool-bin\\mcc-server.cmd" }}
    }}
    return $null
}}
function Invoke-NativeCommand {{
    param([string] $FilePath, [string[]] $Arguments = @())
}}
function Invoke-NativeCapture {{
    param([string] $FilePath, [string[]] $Arguments = @())
    if ($FilePath -eq "C:\\fake\\uv.exe") {{ return "C:\\fake\\tool-bin" }}
    if ($Arguments -contains "--version") {{ return "my-claude-code 5.3.3" }}
    return ""
}}
function Add-PathEntry {{ param([string] $PathEntry) }}
$script:NeedDeferredFinish = $true
$DryRun = $false
. "{(func_file.as_posix())}"
Configure-AndConfirmFreeClaudeCode -ExpectedVersion "5.3.3"
Write-Host "CONFIGURE_CONFIRM_OK"
""",
        encoding="utf-8",
    )

    # mcc-server landed: verify succeeds via mcc-server --version, no throw for
    # the missing mcc-rtk / mcc-desktop shims.
    result = subprocess.run(
        [
            powershell_harness.powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(runner),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=powershell_harness.env | {"FAKE_MCC_SERVER": "present"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "CONFIGURE_CONFIRM_OK" in result.stdout
    assert "did not create" not in result.stdout

    # mcc-server itself missing (deeper failure): report staged, still no throw.
    result = subprocess.run(
        [
            powershell_harness.powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(runner),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=powershell_harness.env | {"FAKE_MCC_SERVER": "missing"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "CONFIGURE_CONFIRM_OK" in result.stdout
    assert "staged" in result.stdout
    assert "did not create" not in result.stdout


def test_installers_use_native_clients_and_single_python_selection() -> None:
    shell = (_repo_root() / "scripts" / "install.sh").read_text(encoding="utf-8")
    powershell = (_repo_root() / "scripts" / "install.ps1").read_text(encoding="utf-8")

    for text in (shell, powershell):
        assert "@anthropic-ai/claude-code" not in text
        assert "@openai/codex" not in text
        assert "@earendil-works/pi-coding-agent" not in text
        assert "git+" not in text
        assert "git --version" not in text
        # The wheel URL is now assembled from the release the feed reports, so
        # the script must carry the repo and the feed, not a pinned version.
        assert "FiredMosquito831/my-claude-code" in text
        assert "releases/latest" in text
        assert "releases/download/" in text
        assert FCC_VERSION not in text, "no product version may be pinned"
        assert FCC_WHEEL_SHA256 not in text, "no checksum may be pinned"
        # The proxy installs on its own; the coding agents are not its business.
        assert "claude.ai/install" not in text
        assert "chatgpt.com/codex/install" not in text
        assert "pi.dev/install" not in text
        assert "Alishahryar1/free-claude-code" not in text
        assert "refs/heads/main" not in text
        assert "python install" not in text
        assert "--refresh-package" in text
        assert "tool update-shell" in text
        assert "--python" in text
        assert "checksum mismatch" in text

    assert "https://astral.sh/uv/install.sh" in shell
    assert "https://astral.sh/uv/install.ps1" in powershell


def test_install_ps1_single_running_launcher_does_not_break_strict_count() -> None:
    """A single running launcher must not trip `.Count` under Set-StrictMode.

    Get-RunningLaunchers must emit matches to the pipeline (a `return $running`
    would unwrap a single Process object into a scalar, and a later
    `$running.Count` on that scalar throws "The property 'Count' cannot be
    found" under Set-StrictMode -Version Latest). The caller must wrap the call
    in @(...) so 0, 1, or many results are always an array.
    """
    powershell = (_repo_root() / "scripts" / "install.ps1").read_text(encoding="utf-8")

    # The function emits matches to the pipeline, not `return $running`.
    assert "foreach ($process in $running) {" in powershell
    assert "return $running" not in powershell
    assert "return ,$running" not in powershell
    # The caller wraps the result so .Count is always valid.
    assert "$running = @(Get-RunningLaunchers)" in powershell


def test_installers_allow_install_while_running_and_lead_with_mcc() -> None:
    """Install-while-running must keep working; Windows defers; message is mcc.

    POSIX unlinks open files, so uv tool install --force works while the app
    runs (the new version is picked up on restart) -- the sh installer must not
    hard-block. Windows cannot overwrite a running .exe, so the PS installer
    detects running launchers and defers the install to a detached helper that
    completes it after the app stops, rather than refusing or dying with os
    error 32. The post-install message leads with the native mcc command family
    and does not advertise the legacy fcc names.
    """
    shell = (_repo_root() / "scripts" / "install.sh").read_text(encoding="utf-8")
    powershell = (_repo_root() / "scripts" / "install.ps1").read_text(encoding="utf-8")

    # POSIX install-while-running is native: no running-process guard in sh.
    assert "assert_no_running_launchers" not in shell
    assert "launcher_is_running" not in shell
    assert "still running" not in shell

    # Windows defers: detect launchers (Get-Process), stage, and hand to a
    # detached helper -- it must not throw "is still running".
    assert "Get-Process" in powershell
    assert "Start-DeferredInstall" in powershell
    assert "Get-RunningLaunchers" in powershell
    assert "staged" in powershell
    assert "is still running" not in powershell
    for name in ("fcc-server", "fcc-claude", "fcc-codex", "mcc-server", "mcc-claude"):
        assert name in powershell
    assert "Install-FreeClaudeCode" in powershell
    # The command reference is shared so both the direct and deferred (app
    # running) paths show the same mcc message as the WSL/Linux installer.
    assert "Write-MccCommandReference" in powershell
    assert powershell.count("Write-MccCommandReference") >= 2  # def + both calls

    # The success message leads with mcc and does not advertise legacy commands.
    assert "Start the proxy" in shell and "Start the proxy" in powershell
    assert "mcc-server" in shell and "mcc-server" in powershell
    assert "mcc-claude" in shell and "mcc-claude" in powershell
    assert "Start the proxy with: fcc-server" not in shell
    assert "Start the proxy with: fcc-server" not in powershell
    assert "with fcc-claude, fcc-codex, or fcc-pi" not in shell
    assert "with fcc-claude, fcc-codex, or fcc-pi" not in powershell
    assert "Use fcc-claude-old instead" not in shell
    assert "Use fcc-claude-old instead" not in powershell


def test_readme_install_section_has_no_manual_git_prerequisite() -> None:
    readme = (_repo_root() / "README.md").read_text(encoding="utf-8")
    install_section = readme.split("### 1. Install Or Update", 1)[1].split(
        "### 2. Start The Server", 1
    )[0]

    assert "Install Git" not in install_section
    assert "official native installers" not in install_section


@pytest.mark.parametrize("powershell", _powershells())
def test_install_ps1_falls_back_when_pshome_executable_is_unavailable(
    tmp_path: Path,
    powershell: str,
) -> None:
    text = (_repo_root() / "scripts" / "install.ps1").read_text(encoding="utf-8")
    body = _braced_body(text, "function Get-PowerShellExecutable")
    fallback = tmp_path / "fallback" / "powershell.exe"
    script = tmp_path / "test-powershell-resolution.ps1"
    script.write_text(
        f"""Set-StrictMode -Version Latest
function Get-ApplicationCommand {{
    param([string] $Name)
    return [pscustomobject] @{{ Source = {str(fallback)!r} }}
}}
function Get-PowerShellExecutable {{
{body}
}}
$resolved = Get-PowerShellExecutable -PowerShellHome {str(tmp_path / "missing")!r}
if ($resolved -ne {str(fallback)!r}) {{
    throw "Unexpected fallback: $resolved"
}}
""",
        encoding="utf-8",
    )

    result = subprocess.run(
        [powershell, "-NoProfile", "-File", str(script)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_installers_define_every_helper_they_call() -> None:
    """Guard against deleting a helper that is still referenced.

    Stripping the coding-agent code once removed the uv version helpers that
    happened to sit beside it, and neither script fails until the moment a
    user runs it -- the PowerShell path is not exercised on the Linux CI
    runners at all. This catches that statically.
    """
    shell = (_repo_root() / "scripts" / "install.sh").read_text(encoding="utf-8")
    powershell = (_repo_root() / "scripts" / "install.ps1").read_text(encoding="utf-8")

    shell_defined = set(re.findall(r"^([a-z_][a-z0-9_]*)\(\)\s*\{", shell, re.M))
    shell_called = set(re.findall(r"^\s*([a-z_][a-z0-9_]*)(?:\s|$)", shell, re.M))
    missing_shell = {
        name for name in shell_called & _SHELL_HELPER_NAMES if name not in shell_defined
    }
    assert not missing_shell, f"install.sh calls undefined helpers: {missing_shell}"
    assert shell_defined >= _SHELL_HELPER_NAMES, (
        f"install.sh is missing helpers: {_SHELL_HELPER_NAMES - shell_defined}"
    )

    ps_defined = set(
        re.findall(r"^function\s+([A-Za-z][A-Za-z0-9-]*)\s*\{", powershell, re.M)
    )
    assert ps_defined >= _POWERSHELL_HELPER_NAMES, (
        f"install.ps1 is missing helpers: {_POWERSHELL_HELPER_NAMES - ps_defined}"
    )


def test_install_sh_rtk_flag_in_usage_and_parse_args() -> None:
    shell = (_repo_root() / "scripts" / "install.sh").read_text(encoding="utf-8")

    assert "Enable RTK token optimization" in shell
    assert "mcc-rtk enable claude,codex,pi" in shell
    # A parse_args case entry sets the flag.
    assert "\n            --rtk)\n                enable_rtk=1\n" in shell


def test_install_sh_rtk_dry_run_prints_enable_command() -> None:
    shell = (_repo_root() / "scripts" / "install.sh").read_text(encoding="utf-8")

    # The dry-run branch of enable_rtk_for_agents must print, not execute.
    assert "print_command mcc-rtk enable claude,codex,pi" in shell
    # The real path runs the shim from the uv tool bin dir.
    assert '"$tool_bin/mcc-rtk" enable claude,codex,pi' in shell


def test_install_sh_rtk_enable_step_runs_after_configure() -> None:
    shell = (_repo_root() / "scripts" / "install.sh").read_text(encoding="utf-8")

    # The RTK step is invoked in the main flow after PATH verification.
    configure_call = shell.index("configure_and_verify_my_claude_code\n")
    enable_call = shell.index("enable_rtk_for_agents\n")
    assert configure_call < enable_call


def test_install_ps1_rtk_flag_in_usage_and_param() -> None:
    powershell = (_repo_root() / "scripts" / "install.ps1").read_text(encoding="utf-8")

    assert "[switch] $Rtk," in powershell
    assert "-Rtk                   Enable RTK token optimization" in powershell
    assert "$script:EnableRtk = $Rtk.IsPresent" in powershell
    assert "mcc-rtk enable claude,codex,pi" in powershell


def test_install_ps1_rtk_dry_run_prints_enable_command() -> None:
    powershell = (_repo_root() / "scripts" / "install.ps1").read_text(encoding="utf-8")

    assert 'Write-Host "+ mcc-rtk enable claude,codex,pi"' in powershell


def test_install_sh_desktop_flag_in_usage_and_parse_args() -> None:
    shell = (_repo_root() / "scripts" / "install.sh").read_text(encoding="utf-8")

    assert "Create a desktop launcher" in shell
    # A parse_args case entry sets the flag.
    assert "\n            --desktop)\n                enable_desktop=1\n" in shell
    # Opt-in only: default is off.
    assert "enable_desktop=0" in shell


def test_install_sh_desktop_shortcut_step_runs_after_rtk() -> None:
    shell = (_repo_root() / "scripts" / "install.sh").read_text(encoding="utf-8")

    # The desktop shortcut step is invoked in the main flow after RTK.
    rtk_call = shell.index("enable_rtk_for_agents\n")
    desktop_call = shell.index("create_desktop_shortcut\n")
    assert rtk_call < desktop_call


def test_install_sh_desktop_shortcut_never_fails_install() -> None:
    shell = (_repo_root() / "scripts" / "install.sh").read_text(encoding="utf-8")

    # A failed shortcut creation is downgraded to a warning, not a `fail` call,
    # and the closing summary reports the real error rather than hedging.
    assert "desktop_launcher_created=$(create_macos_app_bundle" in shell
    assert "desktop_launcher_created=$(create_linux_desktop_entry" in shell
    assert (
        "printf 'warning: %s; continuing without it.\\n' \"$desktop_launcher_error\""
        in shell
    )
    assert (
        "fail "
        not in shell.split("create_desktop_shortcut() {")[1].split(
            "\ncreate_linux_desktop_entry"
        )[0]
    )


def test_install_ps1_desktop_flag_in_usage_and_param() -> None:
    powershell = (_repo_root() / "scripts" / "install.ps1").read_text(encoding="utf-8")

    assert "[switch] $Desktop," in powershell
    assert "-Desktop               Create a Start Menu shortcut" in powershell
    assert "$script:EnableDesktop = $Desktop.IsPresent" in powershell


def test_install_ps1_desktop_shortcut_never_fails_install() -> None:
    powershell = (_repo_root() / "scripts" / "install.ps1").read_text(encoding="utf-8")

    # The shortcut function wraps its work in try/catch and only warns on
    # failure -- it must never let an exception propagate and abort install.
    function_start = powershell.index("function New-DesktopShortcut {")
    function_end = powershell.index("\nfunction ", function_start + 1)
    function_body = powershell[function_start:function_end]
    assert "try {" in function_body
    assert "catch {" in function_body
    assert "Write-Warning" in function_body


def test_install_ps1_desktop_shortcut_runs_after_rtk() -> None:
    powershell = (_repo_root() / "scripts" / "install.ps1").read_text(encoding="utf-8")

    call_sites = re.findall(r"^ {4}Enable-RtkForAgents\n(.*\n)", powershell, re.M)
    assert call_sites, "expected at least one Enable-RtkForAgents call site"
    for following_line in call_sites:
        assert following_line.startswith("    New-DesktopShortcut"), (
            "New-DesktopShortcut must be called immediately after "
            "Enable-RtkForAgents at every call site"
        )


def test_install_ps1_does_not_rely_on_script_scope_for_release_state() -> None:
    """The published command runs this file as a scriptblock, not a file.

    Under `irm ... | iex` a function's `$script:` writes are not visible to the
    rest of the script, so resolved release state must be returned and passed
    explicitly. This failed only when actually run: the file still parsed, and
    -File execution worked.
    """
    powershell = (_repo_root() / "scripts" / "install.ps1").read_text(encoding="utf-8")

    assert "$script:FccVersion" not in powershell
    assert "$script:FccWheelUrl" not in powershell
    assert "$script:FccWheelName" not in powershell
    assert "$script:FccWheelSha256" not in powershell
    assert "return [pscustomobject]@{" in powershell
    assert "Get-VerifiedReleaseWheel -Release" in powershell
