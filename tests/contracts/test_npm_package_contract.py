"""The npm package is a second front door, and it must open onto the same room.

`packaging/npm/` is published to npm as `@firedmosquito831/my-claude-code`. It
carries no server code at all: its `postinstall` runs the repository's own
digest-verified install script with the desktop flag, and its `bin` is a
launcher that forwards to the `mcc-*` commands that script installs. That makes
it the one artefact in this repository whose correctness is entirely a matter of
agreement with things outside it -- the Python version, the repository LICENCE,
the installer's flag spelling, the release workflow -- and none of those
agreements can be checked by running the package.

So they are checked here:

* **the version**, because npm publishes from the release tag and a launcher
  advertising a version the server never had is unreconstructable later;
* **the manifest shape** (name, both bin names, the `files` allow-list, the
  Node floor, public access), because npm silently publishes whatever it is
  given and a dropped `bin` entry is only discovered by a user typing `mcc`;
* **the LICENCE bytes**, because AGPL redistribution through a second registry
  is exactly where a stale copy would matter;
* **the postinstall's four refusals**, because the expensive failure mode is
  the invisible one: an `npx` invocation or a CI job that quietly starts
  installing Python on a machine that never asked;
* **the release workflow**, because it is unrunnable on a pull request and
  would otherwise only be tested by publishing a wrong package to a registry
  that does not allow deletions.
"""

import hashlib
import json
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_DIR = REPO_ROOT / "packaging" / "npm"
MANIFEST_PATH = PACKAGE_DIR / "package.json"
LAUNCHER = PACKAGE_DIR / "bin" / "my-claude-code.js"
POSTINSTALL = PACKAGE_DIR / "bin" / "postinstall.js"
RUNTIME = PACKAGE_DIR / "bin" / "runtime-install.js"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "npm-release.yml"

PACKAGE_NAME = "@firedmosquito831/my-claude-code"

NODE = shutil.which("node")
requires_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _read(path: Path) -> str:
    # The worktree checks out CRLF; read with universal newlines so no
    # assertion below has to think about "\r".
    with open(path, encoding="utf-8", newline=None) as handle:
        return handle.read()


def _mapping(value: object, what: str) -> dict[str, object]:
    """`value` as a string-keyed mapping, or a failure naming what was expected."""
    assert isinstance(value, dict), f"{what} must be a mapping, got {type(value)}"
    return {str(key): item for key, item in value.items()}


def _manifest() -> dict[str, object]:
    return _mapping(json.loads(_read(MANIFEST_PATH)), "package.json")


def _pyproject_version() -> str:
    version = tomllib.loads(_read(REPO_ROOT / "pyproject.toml"))["project"]["version"]
    assert isinstance(version, str)
    return version


# ----------------------------------------------------------------- manifest


def test_the_package_name_is_the_scoped_one_npm_accepted() -> None:
    """The bare name is refused by npm; the scope is permanent, not a phase."""
    assert _manifest()["name"] == PACKAGE_NAME


def test_the_npm_version_equals_the_python_version() -> None:
    """One version number for both halves, or the release job refuses to ship.

    `packaging/` is not a production path, so the versioning rule does not
    force a bump when only this directory changes -- but whatever the number
    is, it has to be the server's.
    """
    assert _manifest()["version"] == _pyproject_version(), (
        "packaging/npm/package.json and pyproject.toml disagree about the "
        "version. The release workflow fails the publish on this, so fix it "
        "here rather than at release time."
    )


def test_the_published_bin_is_the_one_npx_runs() -> None:
    """One bin, `mcc`, and it is a file that exists in the package.

    This used to publish `my-claude-code` as well -- and that is a console
    script of the WHEEL (`[project.scripts]`). On Windows npm's global bin
    directory precedes the uv tool bin directory on PATH, so `npm install -g`
    put a shim in front of the real launcher, and `install.ps1` (which verified
    its launchers by asking PATH) decided a complete install had put its files
    somewhere illegal and threw -- for good, on every later run of the
    one-liner too.

    With a single bin, `npx @firedmosquito831/my-claude-code <args>` still runs
    it: npm runs the sole binary whatever it is called. Proved against npm
    11.14.1 with a fixture package whose only bin was named `mcc`.
    """
    binaries = _mapping(_manifest()["bin"], "bin")
    assert set(binaries) == {"mcc"}
    for target in binaries.values():
        assert (PACKAGE_DIR / str(target)).is_file(), (
            f"package.json points `bin` at {target}, which is not in the package"
        )


def test_no_npm_bin_name_is_a_python_console_script() -> None:
    """The standing guard: the two packages must never claim the same name.

    Both directions. A name in `[project.scripts]` or `[project.gui-scripts]`
    is installed into the uv tool bin directory by the wheel; a name in
    package.json's `bin` is installed into npm's global bin by npm. When one
    name is in both, PATH order decides which one wins, and on Windows npm
    wins -- which is how a correct install came to report itself broken.
    """
    manifest = tomllib.loads(_read(REPO_ROOT / "pyproject.toml"))
    project = _mapping(manifest["project"], "project")
    console_scripts = set(_mapping(project.get("scripts", {}), "scripts"))
    gui_scripts = set(_mapping(project.get("gui-scripts", {}), "gui-scripts"))
    wheel_commands = console_scripts | gui_scripts
    npm_commands = set(_mapping(_manifest()["bin"], "bin"))

    collisions = sorted(wheel_commands & npm_commands)
    assert collisions == [], (
        "these names are published by BOTH the wheel and the npm package: "
        f"{', '.join(collisions)}. Whichever directory comes first on PATH "
        "decides which one a user gets, and install.ps1 verifies the wheel's "
        "launchers -- so a collision breaks the installer on Windows. Rename "
        "the npm bin."
    )


@requires_node
def test_the_postinstall_removes_a_stale_my_claude_code_shim_it_used_to_own(
    tmp_path: Path,
) -> None:
    """A machine that ran 6.53.1-6.63.0 carries the shim that broke it.

    npm does not reliably reap a bin its package has stopped declaring, and the
    machines that need it reaped are exactly the broken ones, so the hook does
    it itself -- but only for a file whose text names THIS package. A
    `my-claude-code` belonging to somebody else is left alone.
    """
    prefix = tmp_path / "npm-prefix"
    prefix.mkdir()
    ours = prefix / "my-claude-code.cmd"
    ours.write_text(
        '@ECHO off\r\n"%~dp0\\node_modules\\@firedmosquito831\\my-claude-code\\bin\\my-claude-code.js" %*\r\n',
        encoding="utf-8",
    )
    theirs = prefix / "my-claude-code.ps1"
    theirs.write_text("# somebody else's program entirely\n", encoding="utf-8")

    status, output, _ = _run_postinstall(
        tmp_path,
        {
            "npm_config_global": "true",
            "npm_config_prefix": str(prefix),
            "MCC_TEST_PLATFORM": "win32",
            "MCC_NPM_INSTALL": "none",
        },
    )
    assert status == 0, output
    assert not ours.exists(), f"the stale shim was left behind:\n{output}"
    assert theirs.exists(), "a file this package never wrote was deleted"
    assert "removed the old my-claude-code command" in output, output


@requires_node
def test_the_stale_shim_goes_even_when_the_install_is_opted_out_of(
    tmp_path: Path,
) -> None:
    """The migration is not an install, so the install opt-outs do not skip it.

    Somebody who sets `MCC_NPM_SKIP_INSTALL=1` is asking for the launcher and
    nothing else -- they are not asking to keep a shim that shadows a command
    the wheel owns and breaks `install.ps1` on Windows.
    """
    prefix = tmp_path / "npm-prefix"
    prefix.mkdir()
    ours = prefix / "my-claude-code.cmd"
    ours.write_text(
        '@ECHO off\r\n"%~dp0\\node_modules\\@firedmosquito831\\my-claude-code'
        '\\bin\\my-claude-code.js" %*\r\n',
        encoding="utf-8",
    )

    status, output, spawns = _run_postinstall(
        tmp_path,
        {
            "npm_config_global": "true",
            "npm_config_prefix": str(prefix),
            "MCC_TEST_PLATFORM": "win32",
            "MCC_NPM_SKIP_INSTALL": "1",
        },
    )
    assert status == 0, output
    assert _spawns(spawns) == [], "the opt-out must still install nothing"
    assert not ours.exists(), f"the stale shim survived the opt-out:\n{output}"


def test_the_tarball_carries_only_the_launcher_the_readme_and_the_licence() -> None:
    """`files` is an allow-list: anything not named here is not published."""
    assert _manifest()["files"] == ["bin/", "README.md", "LICENSE"]


def test_the_node_floor_and_the_public_access_flag_are_declared() -> None:
    assert _mapping(_manifest()["engines"], "engines")["node"] == ">=18"
    # A scoped package is private unless it says otherwise, and npm's error for
    # that arrives only at publish time.
    publish_config = _mapping(_manifest()["publishConfig"], "publishConfig")
    assert publish_config["access"] == "public"


def test_the_postinstall_hook_is_wired_up() -> None:
    """Without this line `npm install -g` installs a launcher and nothing else."""
    scripts = _mapping(_manifest()["scripts"], "scripts")
    assert scripts["postinstall"] == "node bin/postinstall.js"
    assert POSTINSTALL.is_file()


def test_the_published_licence_is_the_repository_licence() -> None:
    """Byte-identical, not merely similar: this is a redistribution channel."""
    assert (PACKAGE_DIR / "LICENSE").read_bytes() == (
        REPO_ROOT / "LICENSE"
    ).read_bytes(), (
        "packaging/npm/LICENSE has drifted from the repository LICENSE. Copy "
        "the repository file over it; do not edit the copy."
    )


@requires_node
@pytest.mark.parametrize(
    "script", [LAUNCHER, POSTINSTALL, RUNTIME], ids=lambda p: p.name
)
def test_the_shipped_javascript_parses(script: Path) -> None:
    """A syntax error in a published bin is a broken install, not a red test."""
    assert NODE is not None
    result = subprocess.run(
        [NODE, "--check", str(script)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


# -------------------------------------------------------------- postinstall

#: Node preload. It stands in for everything the package touches outside
#: itself -- the platform it believes it is on, the processes it spawns, and
#: the network it downloads from -- and records each in one log. Nothing is
#: executed and nothing is fetched: the point of these tests is that the
#: *decision* is right on machines this suite will never run on, and finding
#: out by running a real installer would install a real server on this one.
_STUBS = """
'use strict';
const fs = require('node:fs');
const childProcess = require('node:child_process');
const record = process.env.MCC_TEST_SPAWN_LOG;

// `process.platform` and `process.arch` are read-only accessors, not fields;
// redefining them here is the only way to ask a Linux question on Windows.
for (const key of ['platform', 'arch']) {
  const value = process.env['MCC_TEST_' + key.toUpperCase()];
  if (value) Object.defineProperty(process, key, { value, configurable: true });
}

function log(entry) {
  fs.appendFileSync(record, JSON.stringify(entry) + '\\n');
}

// The installer is run through `spawn` (not spawnSync) since 6.64.0: the hook
// tees its output into a log file rather than letting `npm install -g` swallow
// it and replay the whole stream under an `npm error` prefix. The fake has to
// learn every shape the hook uses, or the tests reach the real powershell.
const { EventEmitter } = require('node:events');
childProcess.spawn = function (command, args) {
  log({ command, args });
  const child = new EventEmitter();
  child.stdout = new EventEmitter();
  child.stderr = new EventEmitter();
  const status = Number(process.env.MCC_TEST_SPAWN_STATUS || '0');
  const text = process.env.MCC_TEST_SPAWN_OUTPUT || '';
  setImmediate(function () {
    if (text) child.stdout.emit('data', Buffer.from(text, 'utf8'));
    child.emit('close', status);
  });
  return child;
};

childProcess.spawnSync = function (command, args) {
  log({ command, args });
  // The Linux branch asks `command -v dpkg` to choose between the .deb and
  // the tarball, and expects stdout, not just a status.
  if (String((args && args[1]) || '').includes('dpkg')) {
    const present = process.env.MCC_TEST_HAS_DPKG === '1';
    return { status: present ? 0 : 1, stdout: present ? '/usr/bin/dpkg\\n' : '' };
  }
  return { status: Number(process.env.MCC_TEST_SPAWN_STATUS || '0'), stdout: '' };
};

// { "<asset name>": "<body>" }; anything else is a 404.
const bodies = JSON.parse(process.env.MCC_TEST_FETCH || '{}');
globalThis.fetch = async function (url) {
  log({ fetch: String(url) });
  const name = String(url).split('/').pop();
  if (!(name in bodies)) return { ok: false, status: 404 };
  const payload = Buffer.from(bodies[name], 'utf8');
  return { ok: true, status: 200, arrayBuffer: async () => payload };
};
"""


def _run_node(
    tmp_path: Path,
    script: Path,
    environment: dict[str, str],
    argv: list[str] | None = None,
) -> tuple[int, str, list[dict[str, object]]]:
    """Run one of the bins under the stubs; return status, output, calls."""
    assert NODE is not None
    recorder = tmp_path / "recorder.js"
    recorder.write_text(_STUBS, encoding="utf-8")
    log = tmp_path / "spawns.jsonl"
    log.write_text("", encoding="utf-8")

    # A clean environment, not the suite's: `CI` is set on every CI runner and
    # would make three of these four cases test the same branch.
    result = subprocess.run(
        [NODE, "--require", str(recorder), str(script), *(argv or [])],
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": "",
            "SystemRoot": "C:\\Windows",
            "MCC_TEST_SPAWN_LOG": str(log),
            **environment,
        },
    )
    calls = [
        _mapping(json.loads(line), "a recorded call")
        for line in log.read_text(encoding="utf-8").splitlines()
        if line
    ]
    return result.returncode, result.stdout + result.stderr, calls


def _run_postinstall(
    tmp_path: Path, environment: dict[str, str]
) -> tuple[int, str, list[dict[str, object]]]:
    return _run_node(tmp_path, POSTINSTALL, environment)


def _spawns(calls: list[dict[str, object]]) -> list[dict[str, object]]:
    return [call for call in calls if "command" in call]


def _invocation(spawn: dict[str, object]) -> str:
    arguments = spawn["args"]
    assert isinstance(arguments, list)
    return " ".join([str(spawn["command"]), *(str(item) for item in arguments)])


@requires_node
def test_an_npx_run_or_a_local_install_installs_nothing(tmp_path: Path) -> None:
    """`npx … --version` must stay cheap and must not touch the machine."""
    status, output, spawns = _run_postinstall(tmp_path, {})
    assert status == 0
    assert spawns == [], f"the hook spawned {spawns} outside a global install"
    assert "not a global install" in output


@requires_node
def test_ci_never_triggers_a_machine_wide_install(tmp_path: Path) -> None:
    status, output, spawns = _run_postinstall(
        tmp_path, {"npm_config_global": "true", "CI": "true"}
    )
    assert status == 0
    assert spawns == []
    assert "CI is set" in output


@requires_node
def test_the_opt_out_is_honoured_on_a_global_install(tmp_path: Path) -> None:
    status, output, spawns = _run_postinstall(
        tmp_path, {"npm_config_global": "true", "MCC_NPM_SKIP_INSTALL": "1"}
    )
    assert status == 0
    assert spawns == []
    assert "MCC_NPM_SKIP_INSTALL=1" in output


@requires_node
def test_ignore_scripts_is_honoured_even_when_npm_still_runs_the_hook(
    tmp_path: Path,
) -> None:
    status, output, spawns = _run_postinstall(
        tmp_path,
        {"npm_config_global": "true", "npm_config_ignore_scripts": "true"},
    )
    assert status == 0
    assert spawns == []
    assert "--ignore-scripts" in output


@requires_node
def test_a_global_install_runs_the_official_installer_not_a_copy(
    tmp_path: Path,
) -> None:
    """The hook must never grow its own installer: it runs the repository's."""
    _, _, calls = _run_postinstall(
        tmp_path,
        {"npm_config_global": "true", "MCC_NPM_INSTALL": "server"},
    )
    spawns = _spawns(calls)
    assert len(spawns) == 1, f"expected exactly one installer invocation, got {spawns}"
    invocation = _invocation(spawns[0])
    assert "raw.githubusercontent.com/FiredMosquito831/my-claude-code" in invocation
    if sys.platform == "win32":
        assert "powershell" in invocation and "install.ps1" in invocation
        # The scriptblock form, because `irm … | iex` cannot pass a parameter
        # to the script's own param() block at all.
        assert "scriptblock]::Create" in invocation
    else:
        assert "install.sh" in invocation


# ------------------------------------------------------ the decision matrix

#: A body whose digest the checksum fixture will agree with, and one it will
#: not. The content is irrelevant; only the hash is under test.
_ASSET_BODY = "not really an installer"
_ASSET_DIGEST = hashlib.sha256(_ASSET_BODY.encode()).hexdigest()

SUMS_ASSET = "SHA256SUMS-desktop-shell.txt"


def _fetch_fixture(
    asset: str, *, body: str = _ASSET_BODY, digest: str | None = None
) -> str:
    """The stub's URL -> body table: the sums file plus one release asset.

    The sums file is written in the exact shape `shell-release.yml` asserts
    before it uploads: 64 hex characters, two spaces, the file name.
    """
    return json.dumps(
        {
            SUMS_ASSET: f"{digest or _ASSET_DIGEST}  {asset}\n",
            asset: body,
        }
    )


WINDOWS_SETUP = "MyClaudeCode-Setup-windows-x86_64.exe"
LINUX_DEB = "MyClaudeCode-linux-x86_64.deb"
LINUX_TARBALL = "MyClaudeCode-linux-x86_64.tar.gz"
MACOS_DMG = "MyClaudeCode-macos-universal.dmg"


def _install(
    tmp_path: Path,
    environment: dict[str, str],
    argv: list[str] | None = None,
) -> tuple[int, str, list[dict[str, object]]]:
    """`npx … install …` under the stubs, with a scratch download directory."""
    downloads = tmp_path / "downloads"
    downloads.mkdir(exist_ok=True)
    return _run_node(
        tmp_path,
        LAUNCHER,
        {"MCC_NPM_DOWNLOAD_DIR": str(downloads), **environment},
        ["install", *(argv or [])],
    )


#: platform, arch, environment, and whether a desktop app should be installed.
#: This is the table the whole feature exists to get right, and every row is a
#: machine this suite cannot run on.
_MATRIX = [
    ("win32", "x64", {}, True, "a Windows laptop"),
    ("darwin", "arm64", {}, True, "an Apple Silicon Mac"),
    ("linux", "x64", {"DISPLAY": ":0"}, True, "an X11 desktop"),
    ("linux", "x64", {"WAYLAND_DISPLAY": "wayland-0"}, True, "a Wayland desktop"),
    ("linux", "x64", {}, False, "a headless VPS"),
    ("linux", "x64", {"WSL_DISTRO_NAME": "Ubuntu"}, False, "WSL without a display"),
    (
        "linux",
        "x64",
        {"DISPLAY": ":0", "SSH_CONNECTION": "1 2 3 4"},
        False,
        "SSH with X11 forwarding",
    ),
    ("win32", "x64", {"CI": "true"}, False, "a Windows CI runner"),
    (
        "linux",
        "arm64",
        {"DISPLAY": ":0"},
        False,
        "an arm64 Linux desktop, which has no build",
    ),
]


@requires_node
@pytest.mark.parametrize(
    ("platform", "arch", "environment", "wants_desktop", "description"),
    _MATRIX,
    ids=[row[4] for row in _MATRIX],
)
def test_the_runtime_decides_which_shape_to_install(
    tmp_path: Path,
    platform: str,
    arch: str,
    environment: dict[str, str],
    wants_desktop: bool,
    description: str,
) -> None:
    """Server always; the desktop app only where there is a session for it.

    A headless box getting launcher shortcuts is merely untidy. A laptop
    getting no application is the whole reason people prefer the one-line
    installer to npm, so both directions are asserted here.
    """
    status, output, calls = _install(
        tmp_path,
        {
            "MCC_TEST_PLATFORM": platform,
            "MCC_TEST_ARCH": arch,
            "MCC_TEST_HAS_DPKG": "0",
            "MCC_TEST_FETCH": _fetch_fixture(
                {"win32": WINDOWS_SETUP, "darwin": MACOS_DMG}.get(
                    platform, LINUX_TARBALL
                )
            ),
            **environment,
        },
    )
    assert status == 0, output
    assert f"on {platform}/{arch}" in output, "the decision line must name the runtime"

    server = [
        call
        for call in _spawns(calls)
        if "install.ps1" in _invocation(call) or "install.sh" in _invocation(call)
    ]
    assert len(server) == 1, f"the server is installed on every machine; got {server}"
    invocation = _invocation(server[0])
    flagged = "-Desktop" in invocation or "--desktop" in invocation
    assert flagged is wants_desktop, (
        f"on {description} the installer flag should be "
        f"{'on' if wants_desktop else 'off'}: {invocation}"
    )
    downloaded = [call for call in calls if "fetch" in call]
    assert bool(downloaded) is wants_desktop, (
        f"on {description} the desktop download should "
        f"{'happen' if wants_desktop else 'not happen'}: {downloaded}"
    )


@requires_node
@pytest.mark.parametrize(
    ("argv", "environment", "server", "desktop"),
    [
        (["--server-only"], {}, True, False),
        (["--no-desktop"], {}, True, False),
        (["--desktop-only"], {}, False, True),
        ([], {"MCC_NPM_INSTALL": "server"}, True, False),
        ([], {"MCC_NPM_INSTALL": "desktop"}, False, True),
        ([], {"MCC_NPM_INSTALL": "both"}, True, True),
        ([], {"MCC_NPM_INSTALL": "none"}, False, False),
        # A flag beats the environment: whoever is typing decides last.
        (["--server-only"], {"MCC_NPM_INSTALL": "both"}, True, False),
    ],
    ids=lambda value: str(value),
)
def test_the_overrides_beat_the_detected_runtime(
    tmp_path: Path,
    argv: list[str],
    environment: dict[str, str],
    server: bool,
    desktop: bool,
) -> None:
    """Detection is a default, not a policy: images and scripts must be able to say."""
    status, output, calls = _install(
        tmp_path,
        {
            # A headless Linux box, so every "desktop" result below comes from
            # the override and not from the machine.
            "MCC_TEST_PLATFORM": "linux",
            "MCC_TEST_ARCH": "x64",
            "MCC_TEST_HAS_DPKG": "0",
            "MCC_TEST_FETCH": _fetch_fixture(LINUX_TARBALL),
            **environment,
        },
        argv,
    )
    assert status == 0, output
    ran_installer = any("install.sh" in _invocation(call) for call in _spawns(calls))
    assert ran_installer is server
    assert any("fetch" in call for call in calls) is desktop


@requires_node
def test_an_unknown_value_for_the_environment_override_is_refused(
    tmp_path: Path,
) -> None:
    """Silently installing "both" for a typo is how a Dockerfile gets a surprise."""
    status, output, calls = _install(tmp_path, {"MCC_NPM_INSTALL": "yes"})
    assert status == 1
    assert "not one of server, desktop, both, none" in output
    assert _spawns(calls) == []


@requires_node
def test_install_help_lists_every_override_and_installs_nothing(
    tmp_path: Path,
) -> None:
    status, output, calls = _install(
        tmp_path, {"MCC_TEST_PLATFORM": "win32", "MCC_TEST_ARCH": "x64"}, ["--help"]
    )
    assert status == 0
    for flag in (
        "--server-only",
        "--desktop-only",
        "--no-desktop",
        "--yes-sudo",
        "MCC_NPM_INSTALL",
    ):
        assert flag in output, f"`install --help` does not mention {flag}"
    assert calls == [], "`--help` must not install or download anything"


# ------------------------------------------------------- download and verify


@requires_node
def test_the_windows_installer_runs_silently_and_per_user(tmp_path: Path) -> None:
    """/VERYSILENT because a postinstall hook has no console to answer a dialog."""
    status, output, calls = _install(
        tmp_path,
        {
            "MCC_TEST_PLATFORM": "win32",
            "MCC_TEST_ARCH": "x64",
            "MCC_TEST_FETCH": _fetch_fixture(WINDOWS_SETUP),
        },
        ["--desktop-only"],
    )
    assert status == 0, output
    setup = [
        call for call in _spawns(calls) if str(call["command"]).endswith(WINDOWS_SETUP)
    ]
    assert len(setup) == 1, (
        f"expected the downloaded setup.exe to be run once, got {calls}"
    )
    arguments = setup[0]["args"]
    assert isinstance(arguments, list)
    assert "/VERYSILENT" in arguments
    assert "/SUPPRESSMSGBOXES" in arguments
    assert "/NORESTART" in arguments
    # No elevation: the .iss is PrivilegesRequired=lowest and nothing here asks
    # for more, so a global npm install never raises a UAC prompt.
    assert not any(
        str(argument).lower().startswith("/allusers") for argument in arguments
    )
    # It ran the file that was verified, from the scratch download directory.
    assert str(setup[0]["command"]).startswith(str(tmp_path / "downloads"))


@requires_node
def test_a_verified_download_is_written_before_it_is_run(tmp_path: Path) -> None:
    status, output, _ = _install(
        tmp_path,
        {
            "MCC_TEST_PLATFORM": "win32",
            "MCC_TEST_ARCH": "x64",
            "MCC_TEST_FETCH": _fetch_fixture(WINDOWS_SETUP),
        },
        ["--desktop-only"],
    )
    assert status == 0, output
    written = tmp_path / "downloads" / WINDOWS_SETUP
    assert written.read_text(encoding="utf-8") == _ASSET_BODY
    assert _ASSET_DIGEST in output, "the digest it verified must be printed"


@requires_node
def test_a_bad_digest_refuses_to_run_the_file_and_deletes_it(tmp_path: Path) -> None:
    """The one failure mode with a security consequence, so it is asserted twice.

    Nothing may be executed, and nothing may be left on disk for a later run
    -- or for anything else on the machine -- to pick up.
    """
    status, output, calls = _install(
        tmp_path,
        {
            "MCC_TEST_PLATFORM": "win32",
            "MCC_TEST_ARCH": "x64",
            "MCC_TEST_FETCH": _fetch_fixture(WINDOWS_SETUP, digest="0" * 64),
        },
        ["--desktop-only"],
    )
    assert status == 1, output
    assert "failed its SHA-256 check" in output
    assert not (tmp_path / "downloads" / WINDOWS_SETUP).exists(), (
        "an installer that failed verification was left on disk"
    )
    assert not any(
        str(call.get("command", "")).endswith(WINDOWS_SETUP) for call in _spawns(calls)
    ), "an unverified installer was executed"


@requires_node
def test_a_failed_desktop_half_still_leaves_the_server_installed(
    tmp_path: Path,
) -> None:
    """`npm install -g` on a flaky network should not throw away the server."""
    status, output, calls = _install(
        tmp_path,
        {
            "MCC_TEST_PLATFORM": "win32",
            "MCC_TEST_ARCH": "x64",
            # No fixture at all: every fetch 404s.
            "MCC_TEST_FETCH": "{}",
        },
    )
    assert status == 0, output
    assert any("install.ps1" in _invocation(call) for call in _spawns(calls))
    assert "the desktop app is not" in output
    assert "--desktop-only" in output, "it must say how to retry just the desktop half"


@requires_node
def test_a_failed_server_half_reports_what_actually_landed(tmp_path: Path) -> None:
    """It must never claim "nothing was left half-installed" -- it cannot know.

    That sentence was printed unconditionally, and on the machine that prompted
    this change the server WAS installed (41 executables) and only the
    verification threw. Say which half was attempted, how to check, where the
    full output is, and how to retry each half on its own.
    """
    status, output, _ = _run_postinstall(
        tmp_path,
        {
            "npm_config_global": "true",
            "MCC_NPM_INSTALL": "server",
            "MCC_TEST_SPAWN_STATUS": "1",
        },
    )
    assert status == 1, output
    assert "Nothing was left half-installed" not in output, (
        "the hook still claims to know something it cannot know:\n" + output
    )
    assert "the server installer exited 1" in output, output
    assert "mcc-server --version" in output, "it must say how to check the server"
    assert "--server-only" in output and "--desktop-only" in output, output
    assert "--foreground-scripts" in output, (
        "it must name the flag that shows the installer live"
    )
    assert "the full output is in" in output, "it must name the log file"


@requires_node
def test_linux_prints_the_sudo_line_instead_of_escalating(tmp_path: Path) -> None:
    """A package manager that silently calls sudo is one nobody should install."""
    status, output, calls = _install(
        tmp_path,
        {
            "MCC_TEST_PLATFORM": "linux",
            "MCC_TEST_ARCH": "x64",
            "DISPLAY": ":0",
            "MCC_TEST_HAS_DPKG": "1",
            "MCC_TEST_FETCH": _fetch_fixture(LINUX_DEB),
        },
        ["--desktop-only"],
    )
    assert status == 0, output
    assert f"sudo dpkg -i {tmp_path / 'downloads' / LINUX_DEB}" in output
    assert "--yes-sudo" in output
    assert not any(str(call["command"]) == "sudo" for call in _spawns(calls)), (
        "the .deb path escalated without being asked"
    )


@requires_node
def test_yes_sudo_is_the_only_thing_that_runs_dpkg(tmp_path: Path) -> None:
    status, output, calls = _install(
        tmp_path,
        {
            "MCC_TEST_PLATFORM": "linux",
            "MCC_TEST_ARCH": "x64",
            "DISPLAY": ":0",
            "MCC_TEST_HAS_DPKG": "1",
            "MCC_TEST_FETCH": _fetch_fixture(LINUX_DEB),
        },
        ["--desktop-only", "--yes-sudo"],
    )
    assert status == 0, output
    dpkg = [call for call in _spawns(calls) if str(call["command"]) == "sudo"]
    assert len(dpkg) == 1, f"expected one `sudo dpkg -i`, got {calls}"
    assert dpkg[0]["args"] == ["dpkg", "-i", str(tmp_path / "downloads" / LINUX_DEB)]


@requires_node
def test_a_linux_desktop_without_dpkg_takes_the_per_user_tarball(
    tmp_path: Path,
) -> None:
    """Fedora and Arch have a desktop and no dpkg; the tarball needs no root."""
    status, output, calls = _install(
        tmp_path,
        {
            "MCC_TEST_PLATFORM": "linux",
            "MCC_TEST_ARCH": "x64",
            "WAYLAND_DISPLAY": "wayland-0",
            "MCC_TEST_HAS_DPKG": "0",
            "MCC_TEST_FETCH": _fetch_fixture(LINUX_TARBALL),
        },
        ["--desktop-only"],
    )
    assert status == 0, output
    assert any(LINUX_TARBALL in str(call.get("fetch", "")) for call in calls)
    unpacked = [call for call in _spawns(calls) if str(call["command"]) == "tar"]
    assert len(unpacked) == 1, f"expected the tarball to be unpacked once, got {calls}"
    assert any("install-desktop.sh" in _invocation(call) for call in _spawns(calls)), (
        "the tarball's own per-user installer must be the thing that runs"
    )


@requires_node
def test_every_download_url_is_the_versionless_latest_one(tmp_path: Path) -> None:
    """`releases/latest/download/...` so a new release needs no npm publish."""
    _, _, calls = _install(
        tmp_path,
        {
            "MCC_TEST_PLATFORM": "win32",
            "MCC_TEST_ARCH": "x64",
            "MCC_TEST_FETCH": _fetch_fixture(WINDOWS_SETUP),
        },
        ["--desktop-only"],
    )
    fetched = [str(call["fetch"]) for call in calls if "fetch" in call]
    assert fetched, "nothing was downloaded"
    for url in fetched:
        assert url.startswith(
            "https://github.com/FiredMosquito831/my-claude-code/releases/latest/download/"
        ), url
    assert any(url.endswith(SUMS_ASSET) for url in fetched), (
        "the checksum file must be fetched from the same release as the asset"
    )


def test_the_asset_names_are_the_ones_the_release_workflow_uploads() -> None:
    """A renamed asset would 404 at install time and nowhere earlier."""
    workflow = _read(REPO_ROOT / ".github" / "workflows" / "shell-release.yml")
    for asset in (WINDOWS_SETUP, LINUX_DEB, LINUX_TARBALL, MACOS_DMG, SUMS_ASSET):
        assert asset in workflow, f"{asset} is not uploaded by shell-release.yml"
        assert asset in _read(RUNTIME), f"{asset} is not known to runtime-install.js"


# ----------------------------------------------------------------- workflow


def _workflow() -> dict[str, object]:
    # NOTE the key: YAML resolves a bare ``on:`` to the boolean True, so the
    # triggers live under `"True"` once the keys are stringified.
    return _mapping(yaml.safe_load(_read(WORKFLOW)), "npm-release.yml")


def _publish_job() -> dict[str, object]:
    jobs = _mapping(_workflow()["jobs"], "jobs")
    return _mapping(jobs["publish"], "the publish job")


def _steps() -> list[dict[str, object]]:
    steps = _publish_job()["steps"]
    assert isinstance(steps, list)
    return [_mapping(step, "a step") for step in steps]


def test_the_workflow_fires_on_a_published_release() -> None:
    """A bare `on:` key parses as the boolean True; a workflow without it never runs."""
    document = _workflow()
    assert "True" in document, "no `on:` block (bare `on:` parses as True)"
    triggers = _mapping(document["True"], "the on: block")
    assert triggers["release"] == {"types": ["published"]}


def test_the_job_can_mint_a_provenance_token_and_cannot_write_the_repository() -> None:
    permissions = _publish_job()["permissions"]
    assert permissions == {"contents": "read", "id-token": "write"}


def test_every_action_is_sha_pinned() -> None:
    """The house rule for every workflow in this repository."""
    for step in _steps():
        uses = step.get("uses")
        if uses is None:
            continue
        assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", str(uses)), (
            f"{uses} is not pinned to a 40-hex commit"
        )


def test_trusted_publishing_is_the_default_and_a_token_is_only_a_fallback() -> None:
    """npm's documented CI route is OIDC; no long-lived secret is required."""
    text = _read(WORKFLOW)
    assert "mode=oidc" in text and "mode=token" in text
    assert "npm install -g npm@latest" in text, (
        "trusted publishing needs npm >= 11.5.1, which a Node release can lag"
    )
    oidc = [s for s in _steps() if "mode == 'oidc'" in str(s.get("if", ""))]
    token = [s for s in _steps() if "mode == 'token'" in str(s.get("if", ""))]
    assert len(oidc) == 1 and len(token) == 1, (
        "exactly one publish step per authentication mode, guarded so they "
        "never both run"
    )
    assert "env" not in oidc[0], "an OIDC publish must not export a token"
    assert token[0]["env"] == {"NODE_AUTH_TOKEN": "${{ secrets.NPM_TOKEN }}"}


def test_a_refused_oidc_publish_explains_the_setup_and_keeps_the_release_green() -> (
    None
):
    """A convenience launcher must never turn a good server release red."""
    text = _read(WORKFLOW)
    assert "::warning::npm refused the trusted (OIDC) publish" in text
    assert "Settings -> Trusted Publisher" in text
    assert "workflow filename npm-release.yml" in text
    oidc = next(s for s in _steps() if "mode == 'oidc'" in str(s.get("if", "")))
    assert "if npm publish --access public --provenance; then" in str(oidc["run"])


def test_the_publish_refuses_a_tag_that_disagrees_with_pyproject() -> None:
    text = _read(WORKFLOW)
    assert 'version="${TAG#v}"' in text, "the tag's `v` prefix must be stripped"
    assert "pyproject.toml" in text
    assert "Refusing to publish a launcher that claims a version" in text


def test_an_already_published_version_is_a_no_op() -> None:
    """6.52.0 went out by hand, and a release can be re-run."""
    text = _read(WORKFLOW)
    assert "npm view" in text
    assert "is already published" in text


def test_every_publish_step_is_public_and_carries_provenance() -> None:
    publish = [
        step
        for step in _steps()
        if "npm publish --access public --provenance" in str(step.get("run", ""))
    ]
    assert len(publish) == 2, "expected one publish step per authentication mode"
    for step in publish:
        assert step["working-directory"] == "packaging/npm"
