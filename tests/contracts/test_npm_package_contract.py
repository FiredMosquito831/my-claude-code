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


def test_both_command_names_are_published() -> None:
    """`mcc` is the short one people actually type; `my-claude-code` is the name."""
    binaries = _mapping(_manifest()["bin"], "bin")
    assert set(binaries) == {"my-claude-code", "mcc"}
    for target in binaries.values():
        assert (PACKAGE_DIR / str(target)).is_file(), (
            f"package.json points `bin` at {target}, which is not in the package"
        )


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
@pytest.mark.parametrize("script", [LAUNCHER, POSTINSTALL], ids=lambda p: p.name)
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

#: Replaces `spawnSync` before `postinstall.js` requires it, records what it
#: was asked to run, and reports success. Nothing is executed: the point of
#: these tests is that the *decision* to install is right, and running a real
#: installer to find out would install a real server on the machine running
#: the suite.
_SPAWN_RECORDER = """
'use strict';
const fs = require('node:fs');
const childProcess = require('node:child_process');
const record = process.env.MCC_TEST_SPAWN_LOG;
childProcess.spawnSync = function (command, args) {
  fs.appendFileSync(record, JSON.stringify({ command, args }) + '\\n');
  return { status: 0, error: undefined };
};
"""


def _run_postinstall(
    tmp_path: Path, environment: dict[str, str]
) -> tuple[int, str, list[dict[str, object]]]:
    """Run the hook with a stubbed `spawnSync`; return status, output, spawns."""
    assert NODE is not None
    recorder = tmp_path / "recorder.js"
    recorder.write_text(_SPAWN_RECORDER, encoding="utf-8")
    log = tmp_path / "spawns.jsonl"
    log.write_text("", encoding="utf-8")

    # A clean environment, not the suite's: `CI` is set on every CI runner and
    # would make three of these four cases test the same branch.
    result = subprocess.run(
        [NODE, "--require", str(recorder), str(POSTINSTALL)],
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
    spawns = [
        _mapping(json.loads(line), "a recorded spawn")
        for line in log.read_text(encoding="utf-8").splitlines()
        if line
    ]
    return result.returncode, result.stdout + result.stderr, spawns


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
def test_a_global_install_runs_the_official_installer_with_the_desktop_flag(
    tmp_path: Path,
) -> None:
    """The whole point of the hook: same commands as the installer, plus the app."""
    status, _, spawns = _run_postinstall(tmp_path, {"npm_config_global": "true"})
    assert status == 0
    assert len(spawns) == 1, f"expected exactly one installer invocation, got {spawns}"

    arguments = spawns[0]["args"]
    assert isinstance(arguments, list)
    invocation = " ".join([str(spawns[0]["command"]), *(str(x) for x in arguments)])
    assert "raw.githubusercontent.com/FiredMosquito831/my-claude-code" in invocation, (
        "the hook must run the project's own install script, not a copy"
    )
    if sys.platform == "win32":
        assert "powershell" in invocation
        assert "install.ps1" in invocation
        # `-Desktop` bound to the script's own param() block, which needs the
        # scriptblock form: `irm … | iex` cannot pass a parameter at all.
        assert "scriptblock]::Create" in invocation
        assert invocation.rstrip().endswith("-Desktop")
    else:
        assert "install.sh" in invocation
        assert "sh -s -- --desktop" in invocation


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


def test_a_missing_secret_skips_instead_of_failing_the_release() -> None:
    """A convenience launcher must never turn a good server release red."""
    text = _read(WORKFLOW)
    assert "secrets.NPM_TOKEN" in text
    assert "::notice::NPM_TOKEN is not set" in text
    guarded = [
        step
        for step in _steps()
        if "if" in step and "steps.secret.outputs.have == 'true'" in str(step["if"])
    ]
    assert len(guarded) >= 3, (
        "every step after the secret check must be guarded by it, or a missing "
        "token fails the release instead of skipping the publish"
    )


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


def test_the_publish_step_is_public_and_carries_provenance() -> None:
    publish = [
        step for step in _steps() if str(step.get("run", "")).startswith("npm publish")
    ]
    assert len(publish) == 1, "expected exactly one npm publish step"
    step = publish[0]
    assert str(step["run"]).strip() == "npm publish --access public --provenance"
    assert step["working-directory"] == "packaging/npm"
    assert step["env"] == {"NODE_AUTH_TOKEN": "${{ secrets.NPM_TOKEN }}"}
