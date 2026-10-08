"""7.78.9, the POSIX installer: the same four defects, checked by RUNNING it.

``scripts/install.sh`` never had the empty-version crash (a shell variable takes
"" and ``stop_configured_server`` already says "version unknown") -- pinned
here so it stays that way. It DID have the rest:

* ``missing_launcher_shims`` compared uv's bin directory with the
  environment's own ``bin/``, which also holds every dependency's console
  scripts, so every update reported "This release adds fastapi, ..." and ran
  ``uv tool install --force`` over the environment the swap had just put in
  place;
* that in-place finish ran as ``install_my_claude_code || true``, but the
  failure path inside it is ``fail``, which EXITS -- so a failed finish ended
  the script with the server stopped, nothing started and nothing put back;
* a broken environment at the canonical path was staged beside, so it became
  "the previous version" -- the copy a rollback restores and the sweep keeps;
* staging directories were swept only after a new server answered /health.

Each case extracts the real functions from the script and runs them under
``set -eu`` in every POSIX shell this machine has (``sh`` and ``dash``).
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"


def _shells() -> list[tuple[str, str]]:
    found = []
    for name in ("sh", "dash"):
        resolved = shutil.which(name)
        if resolved:
            found.append((name, resolved))
    return found


SHELLS = _shells() or [("unavailable", "")]

pytestmark = pytest.mark.local_serial


def _require(shell: str) -> None:
    if not shell:
        pytest.skip("no POSIX shell")


def _text() -> str:
    return INSTALL_SH.read_text(encoding="utf-8").replace("\r\n", "\n")


def _extract(text: str, name: str) -> str:
    start = text.index(f"\n{name}() {{") + 1
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise AssertionError(f"function {name} is not closed")


PREAMBLE = """set -eu
STAGING_ENV_DIRNAME=".mcc-staging"
PREVIOUS_ENV_DIRNAME=".mcc-previous"
PREVIOUS_ENVS_KEPT=1
PACKAGE_ENV_DIRNAME="my-claude-code"
RESTART_AWARE_VERSION="6.73.0"
write_install_log() { printf 'LOG: %s\\n' "$*"; }
write_install_progress() { printf 'PROGRESS: %s\\n' "$1"; }
"""


def _harness(names: tuple[str, ...], body: str) -> str:
    text = _text()
    bodies = "\n\n".join(_extract(text, name) for name in names)
    return f"{PREAMBLE}\n{bodies}\n\n{body}\n"


def _p(path: Path) -> str:
    return path.as_posix()


def _run(
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


def _touch(path: Path, text: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _executable(path: Path) -> None:
    _touch(path, "#!/bin/sh\nexit 0\n")
    path.chmod(0o755)


def _receipt(names: list[str], bin_dir: Path) -> str:
    entries = "".join(
        f'    {{ name = "{name}", install-path = "{_p(bin_dir / name)}", from = "my-claude-code" }},\n'
        for name in names
    )
    return f'[tool]\nrequirements = [{{ name = "my-claude-code" }}]\nentrypoints = [\n{entries}]\n'


DEPENDENCY_SCRIPTS = ("fastapi", "uvicorn", "typer", "tqdm", "normalizer")
SHIM_FUNCTIONS = ("missing_launcher_shims", "package_entry_point_names")


@pytest.mark.parametrize(("name", "shell"), SHELLS, ids=lambda v: v)
def test_dependency_console_scripts_are_never_a_missing_launcher_sh(
    name: str, shell: str, tmp_path: Path
) -> None:
    _require(shell)
    bin_dir = tmp_path / "bin"
    env_dir = tmp_path / "tools" / "my-claude-code"
    for script in (
        "python",
        "python3",
        "pip",
        "activate",
        *DEPENDENCY_SCRIPTS,
        "mcc-server",
        "mcc-claude",
    ):
        _touch(env_dir / "bin" / script)
    for command in ("mcc-server", "mcc-claude"):
        _touch(bin_dir / command)
    (env_dir / "uv-receipt.toml").write_text(
        _receipt(["mcc-server", "mcc-claude"], bin_dir), encoding="utf-8", newline="\n"
    )
    bare_env = tmp_path / "bare" / "my-claude-code"
    for script in ("python", *DEPENDENCY_SCRIPTS, "mcc-server"):
        _touch(bare_env / "bin" / script)
    staging_bin = tmp_path / "staging" / ".bin"
    for command in ("mcc-server", "mcc-claude", "mcc-newcmd"):
        _touch(staging_bin / command)
    adds_env = tmp_path / "adds" / "my-claude-code"
    for script in (
        "python",
        *DEPENDENCY_SCRIPTS,
        "mcc-server",
        "mcc-claude",
        "mcc-newcmd",
    ):
        _touch(adds_env / "bin" / script)
    (adds_env / "uv-receipt.toml").write_text(
        _receipt(["mcc-server", "mcc-claude", "mcc-newcmd"], bin_dir),
        encoding="utf-8",
        newline="\n",
    )
    body = "\n".join(
        f"printf '{label}=[%s]\\n' \"$(missing_launcher_shims '{_p(bin_dir)}' '{_p(source)}')\""
        for label, source in (
            ("ENV_WITH_RECEIPT", env_dir / "bin"),
            ("ENV_WITHOUT_RECEIPT", bare_env / "bin"),
            ("STAGING_BIN", staging_bin),
            ("ENV_ADDING_A_COMMAND", adds_env / "bin"),
        )
    )
    script = tmp_path / "shims.sh"
    script.write_text(_harness(SHIM_FUNCTIONS, body), encoding="utf-8", newline="\n")
    completed = _run(shell, script)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    out = completed.stdout
    assert "ENV_WITH_RECEIPT=[]" in out, out
    assert "ENV_WITHOUT_RECEIPT=[]" in out, out
    assert "STAGING_BIN=[mcc-newcmd]" in out, out
    assert "ENV_ADDING_A_COMMAND=[mcc-newcmd]" in out, out


@pytest.mark.skipif(os.name == "nt", reason="dangling symlinks are a POSIX fact")
@pytest.mark.parametrize(("name", "shell"), SHELLS, ids=lambda v: v)
def test_the_staging_bin_counts_its_dangling_links_sh(
    name: str, shell: str, tmp_path: Path
) -> None:
    """After the swap the staging bin's entries point into a directory that
    was moved away. ``-f`` follows a link and would skip every one of them;
    a real new command must still be found."""

    _require(shell)
    bin_dir = tmp_path / "bin"
    for command in ("mcc-server", "mcc-claude"):
        os.makedirs(bin_dir, exist_ok=True)
        os.symlink(tmp_path / "gone" / command, bin_dir / command)
    staging_bin = tmp_path / "staging" / ".bin"
    staging_bin.mkdir(parents=True)
    for command in ("mcc-server", "mcc-claude", "mcc-newcmd"):
        os.symlink(tmp_path / "moved-away" / command, staging_bin / command)
    script = tmp_path / "links.sh"
    script.write_text(
        _harness(
            SHIM_FUNCTIONS,
            f"printf 'MISSING=[%s]\\n' \"$(missing_launcher_shims '{_p(bin_dir)}' '{_p(staging_bin)}')\"",
        ),
        encoding="utf-8",
        newline="\n",
    )
    completed = _run(shell, script)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "MISSING=[mcc-newcmd]" in completed.stdout, completed.stdout


@pytest.mark.parametrize(("name", "shell"), SHELLS, ids=lambda v: v)
def test_an_empty_version_is_version_unknown_and_never_fatal_sh(
    name: str, shell: str, tmp_path: Path
) -> None:
    """The POSIX twin of the PowerShell crash never existed; pin it. A
    launcher that cannot run yields "", the caller's ``|| printf ''`` keeps
    ``set -e`` from ending the script, and the stop takes the "version
    unknown" branch: never ask, only check the port."""

    _require(shell)
    launcher = tmp_path / "mcc-server"
    _touch(
        launcher,
        "#!/bin/sh\necho 'error: uv trampoline failed to canonicalize script path' >&2\nexit 1\n",
    )
    launcher.chmod(0o755)
    body = f"""
port_is_occupied() {{ return 1; }}
ask_the_product_about_the_port() {{ echo 'an unknown version must never be asked' >&2; exit 9; }}
report_other_servers() {{ :; }}
server_reachable_host=127.0.0.1
server_port=18999
stop_configured_server '{_p(launcher)}' "$(installed_server_version '{_p(launcher)}' || printf '')"
printf 'OUTCOME=%s\\n' "$stop_outcome"
"""
    script = tmp_path / "stop.sh"
    script.write_text(
        _harness(
            ("installed_server_version", "stop_configured_server", "version_at_least"),
            body,
        ),
        encoding="utf-8",
        newline="\n",
    )
    completed = _run(shell, script)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "OUTCOME=nothing-listening" in completed.stdout
    assert (
        "LOG: The installed mcc-server (version unknown) predates --report-holder; not asking it."
        in completed.stdout
    )


HEAL_FUNCTIONS = (
    "tool_environment_problem",
    "move_environment_aside",
    "update_aside_root",
)


@pytest.mark.parametrize(("name", "shell"), SHELLS, ids=lambda v: v)
def test_a_husk_is_found_and_moved_beside_the_tools_root_sh(
    name: str, shell: str, tmp_path: Path
) -> None:
    _require(shell)
    tools = tmp_path / "uv" / "tools"
    husk = tools / "my-claude-code"
    _touch(husk / "lib" / "leftover.txt")
    whole = tmp_path / "whole" / "tools" / "my-claude-code"
    _executable(whole / "bin" / "python")
    _touch(whole / "uv-receipt.toml", "[tool]\n")
    body = f"""
tool_environment_problem '{_p(husk)}'; printf 'HUSK=[%s]\\n' "$tool_problem"
tool_environment_problem '{_p(whole)}'; printf 'WHOLE=[%s]\\n' "$tool_problem"
tool_environment_problem '{_p(tmp_path / "absent" / "my-claude-code")}'; printf 'ABSENT=[%s]\\n' "$tool_problem"
move_environment_aside '{_p(husk)}' '{_p(tools)}' 20261008-161206 broken
printf 'DEST=[%s]\\n' "$moved_aside_to"
"""
    script = tmp_path / "husk.sh"
    script.write_text(_harness(HEAL_FUNCTIONS, body), encoding="utf-8", newline="\n")
    completed = _run(shell, script)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    out = completed.stdout
    assert "HUSK=[it has no runnable bin/python; it has no uv-receipt.toml]" in out, out
    assert "WHOLE=[]" in out, out
    assert "ABSENT=[]" in out, out
    destination = (
        tmp_path / "uv" / ".mcc-staging" / "20261008-161206-broken" / "my-claude-code"
    )
    assert f"DEST=[{_p(destination)}]" in out, out
    assert (destination / "lib" / "leftover.txt").is_file()
    assert list(tools.iterdir()) == []


@pytest.mark.parametrize(("name", "shell"), SHELLS, ids=lambda v: v)
def test_the_sweep_keeps_this_run_and_one_previous_sh(
    name: str, shell: str, tmp_path: Path
) -> None:
    _require(shell)
    root = tmp_path / "uv"
    tools = root / "tools"
    tools.mkdir(parents=True)
    staging = root / ".mcc-staging"
    previous = root / ".mcc-previous"
    for entry in (
        "20261008-160332",
        "20261008-161208",
        "20261008-170000",
        "20261008-170000-failed",
    ):
        _touch(staging / entry / "my-claude-code" / "pyvenv.cfg")
    for stamp in ("20260928-113903", "20261003-221036", "20261008-160332"):
        _touch(previous / stamp / "my-claude-code" / "pyvenv.cfg")
    script = tmp_path / "sweep.sh"
    script.write_text(
        _harness(
            (
                "remove_update_leftovers",
                "remove_stale_previous_environment",
                "update_aside_root",
            ),
            f"remove_update_leftovers '{_p(tools)}' 20261008-170000\nprintf 'SWEPT\\n'",
        ),
        encoding="utf-8",
        newline="\n",
    )
    completed = _run(shell, script)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "SWEPT" in completed.stdout
    assert sorted(p.name for p in staging.iterdir()) == [
        "20261008-170000",
        "20261008-170000-failed",
    ]
    assert sorted(p.name for p in previous.iterdir()) == ["20261008-160332"]


UNDO_FUNCTIONS = ("undo_in_place_finish", "restore_previous_environment")


@pytest.mark.parametrize(("name", "shell"), SHELLS, ids=lambda v: v)
@pytest.mark.parametrize(
    "may_start", [0, 1], ids=["stopped-nothing", "restarts-previous"]
)
def test_a_failed_finish_puts_the_previous_version_back_sh(
    name: str, shell: str, may_start: int, tmp_path: Path
) -> None:
    """``undo_in_place_finish``: the previous version goes back, the server this
    run stopped is started FROM it, and the script exits 1 saying so."""

    _require(shell)
    tools = tmp_path / "uv" / "tools"
    canonical = tools / "my-claude-code"
    _touch(canonical / "lib" / "half-written.txt")
    staging_root = tmp_path / "uv" / ".mcc-staging"
    previous_dir = tmp_path / "uv" / ".mcc-previous" / "20261008-170000"
    previous_env = previous_dir / "my-claude-code"
    _executable(previous_env / "bin" / "python")
    _touch(previous_env / "VERSION", "9.9.8")
    body = f"""
start_server_detached() {{ started_server_pid=4242; printf 'STARTED %s\\n' "$1"; }}
wait_for_server_health() {{ return 0; }}
server_start_budget_seconds() {{ printf '1'; }}
mcc_config_dir() {{ printf '%s' '{_p(tmp_path / "config")}'; }}
staged_tool_dir='{_p(canonical)}'
staging_root='{_p(staging_root)}'
staged_stamp=20261008-170000
swap_aside_env='{_p(previous_env)}'
swap_previous_dir='{_p(previous_dir)}'
stage_may_start={may_start}
restart_launcher='{_p(tmp_path / "bin" / "mcc-server")}'
restart_health_url=http://127.0.0.1:18999/health
server_port=18999
install_progress_log=''
undo_in_place_finish "uv tool install --force exited non-zero"
printf 'NOT REACHED\\n'
"""
    script = tmp_path / "undo.sh"
    script.write_text(_harness(UNDO_FUNCTIONS, body), encoding="utf-8", newline="\n")
    completed = _run(shell, script)
    out = completed.stdout

    assert completed.returncode == 1, out + completed.stderr
    assert "NOT REACHED" not in out
    assert (
        "The install could not be finished in place: uv tool install --force exited non-zero"
        in out
    )
    assert "PROGRESS: rolling-back" in out and "PROGRESS: recovered" in out
    assert (canonical / "VERSION").read_text(encoding="utf-8") == "9.9.8"
    assert (
        staging_root
        / "20261008-170000-failed"
        / "my-claude-code"
        / "lib"
        / "half-written.txt"
    ).is_file()
    if may_start:
        assert "STARTED " in out
        assert (
            "The previous version was put back and is answering on port 18999." in out
        )
    else:
        assert "STARTED " not in out
        assert "The previous version was put back.\n" in out


def test_the_posix_update_path_wires_the_fixes_in_order() -> None:
    """The order the functions above are used in, read from the script: the
    broken-environment check BEFORE the stage-or-not decision, the staging bin
    (never the environment's bin/) for the missing-launcher check, the finish
    in a subshell with the rollback after it, no start from an environment
    with no interpreter, and the sweep on every exit."""

    text = _text()
    body = text[text.index("\nresolve_install_plan\n") :]
    heal = body.index('tool_environment_problem "$staged_tool_dir"')
    stage_gate = body.index(
        'if [ "$staged_broken" -ne 1 ] && [ -n "$staged_tool_dir" ]'
    )
    assert heal < stage_gate
    assert 'missing_launcher_shims "$staged_bin_dir" "$staging_bin"' in body
    assert 'missing_launcher_shims "$staged_bin_dir" "$staged_tool_dir/bin"' not in text
    assert "install_my_claude_code || true" not in text
    finish = body.index("if ( install_my_claude_code ); then")
    assert finish < body.index('undo_in_place_finish "$finish_failure"')
    assert body.index('if [ -x "$staged_tool_dir/bin/python" ]; then') < body.index(
        'start_restarted_server "$restart_launcher"'
    )
    # The script the start runs names the canonical interpreter BEFORE the
    # start; the other entry scripts are still fixed after it.
    repoint = body.index(
        'repoint_entry_script "$staged_tool_dir/bin/mcc-server" "$staging_env" "$staged_tool_dir"'
    )
    assert repoint < body.index('start_restarted_server "$restart_launcher"')
    assert body.index('start_restarted_server "$restart_launcher"') < body.index(
        'complete_environment_swap "$staged_tool_dir" "$staging_env"'
    )
    cleanup = _extract(text, "cleanup")
    assert cleanup.index("remove_update_leftovers") < cleanup.index("exit_update_lock")


@pytest.mark.parametrize(("name", "shell"), SHELLS, ids=lambda v: v)
def test_the_script_the_start_runs_names_the_canonical_interpreter_sh(
    name: str, shell: str, tmp_path: Path
) -> None:
    """After the swap every entry script still names the STAGING interpreter;
    uv's bin entries are symlinks to them, so a start before the rewrite died
    with "nohup: failed to run command ... No such file or directory" (exit
    127; install-smoke, Linux). The rewrite is per script, idempotent, and
    complete_environment_swap still counts the rest."""

    _require(shell)
    staging_env = (
        tmp_path / "uv" / ".mcc-staging" / "20261008-170000" / "my-claude-code"
    )
    tool_dir = tmp_path / "uv" / "tools" / "my-claude-code"
    for entry in ("mcc-server", "mcc-claude"):
        _touch(
            tool_dir / "bin" / entry,
            f"#!{_p(staging_env)}/bin/python\nimport sys\n",
        )
    body = f"""
if repoint_entry_script '{_p(tool_dir / "bin" / "mcc-server")}' '{_p(staging_env)}' '{_p(tool_dir)}'; then echo FIRST=rewritten; fi
if repoint_entry_script '{_p(tool_dir / "bin" / "mcc-server")}' '{_p(staging_env)}' '{_p(tool_dir)}'; then echo SECOND=rewritten; else echo SECOND=nothing-to-do; fi
complete_environment_swap '{_p(tool_dir)}' '{_p(staging_env)}'
"""
    script = tmp_path / "repoint.sh"
    script.write_text(
        _harness(("repoint_entry_script", "complete_environment_swap"), body),
        encoding="utf-8",
        newline="\n",
    )
    completed = _run(shell, script)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    out = completed.stdout
    assert "FIRST=rewritten" in out
    assert "SECOND=nothing-to-do" in out
    assert "LOG: Re-pointed 1 launcher(s) inside the new environment." in out
    for entry in ("mcc-server", "mcc-claude"):
        first_line = (
            (tool_dir / "bin" / entry).read_text(encoding="utf-8").splitlines()[0]
        )
        assert first_line == f"#!{_p(tool_dir)}/bin/python", first_line
