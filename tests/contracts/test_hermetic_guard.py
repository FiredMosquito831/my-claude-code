"""The hermeticity guard has to fail a leaking test, so leak one at it.

Every case below is a deliberately non-hermetic test executed by a *child*
pytest with ``tests/support/hermetic.py`` loaded as its only plugin. Asserting
on the child's exit status and output is the only way to prove the guard fails
the run rather than merely logging something: an in-process assertion would be
testing the interceptor's return value, not its effect on a test report. That
distinction is exactly what went wrong before -- the previous config-directory
assertion looked correct and never fired once, across 1619 tests that resolved
the developer's real home.

The child never touches this machine either. It is handed a *stand-in* profile
under ``tmp_path`` as its ``HOME``/``USERPROFILE``/``APPDATA``/``LOCALAPPDATA``,
so the guard captures that as "the real machine" at import time, and the leaking
case writes at the stand-in rather than at the developer.
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The child reads the stand-in profile from here: inside the child, ``HOME`` has
# already been redirected into ``tmp_path`` by the guard's own fixture, which is
# the whole point -- a leaking test has to reconstruct a real path to leak at.
_PROFILE_VARIABLE = "HERMETIC_SELFTEST_PROFILE"

_PREAMBLE = """
import os, socket, subprocess, sys
from pathlib import Path

import pytest

PROFILE = Path(os.environ["HERMETIC_SELFTEST_PROFILE"])
"""


def _run_child(tmp_path: Path, body: str) -> subprocess.CompletedProcess[str]:
    """Run ``body`` as a one-file test session under the guard, and report."""

    profile = tmp_path / "profile"
    for relative in (".fcc", "AppData/Roaming", "AppData/Local"):
        (profile / relative).mkdir(parents=True, exist_ok=True)
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    case = tmp_path / "test_leaking_case.py"
    case.write_text(_PREAMBLE + textwrap.dedent(body), encoding="utf-8")

    environment = dict(os.environ)
    environment.update(
        {
            "HOME": str(profile),
            "USERPROFILE": str(profile),
            "APPDATA": str(profile / "AppData" / "Roaming"),
            "LOCALAPPDATA": str(profile / "AppData" / "Local"),
            "TEMP": str(scratch),
            "TMP": str(scratch),
            "TMPDIR": str(scratch),
            _PROFILE_VARIABLE: str(profile),
            "PYTHONPATH": os.pathsep.join((str(_REPO_ROOT), str(_REPO_ROOT / "src"))),
        }
    )
    environment.pop("MCC_CONFIG_DIR", None)

    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(case),
            "-p",
            "tests.support.hermetic",
            "-p",
            "no:cacheprovider",
            "-q",
            "--no-header",
            "--tb=long",
        ],
        cwd=str(tmp_path),
        env=environment,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def _squashed(completed: subprocess.CompletedProcess[str]) -> str:
    """The child's whole output with every run of whitespace removed.

    Failure text is wrapped at the terminal width and the paths under test are
    long, so a needle that spans a wrap point would never match verbatim.
    """

    return "".join((completed.stdout + completed.stderr).split())


def _assert_refused(completed: subprocess.CompletedProcess[str], *needles: str) -> None:
    output = _squashed(completed)
    assert completed.returncode != 0, (
        "the guard let the leaking case pass:\n" + completed.stdout
    )
    assert "HERMETICITYVIOLATION" in output, completed.stdout
    for needle in needles:
        assert "".join(needle.split()) in output, (needle, completed.stdout)


def _assert_green(completed: subprocess.CompletedProcess[str]) -> None:
    assert completed.returncode == 0, (
        "the guard refused a legitimate case:\n" + completed.stdout + completed.stderr
    )


# ------------------------------------------------------------------ registry


def test_a_registry_write_without_the_marker_fails_the_run(tmp_path: Path) -> None:
    """B1, the incident itself: this shape deleted the developer's Run value."""

    completed = _run_child(
        tmp_path,
        """
        def test_leaks():
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\\Microsoft\\Windows\\CurrentVersion\\Run",
                0,
                winreg.KEY_SET_VALUE,
            ) as key:
                winreg.SetValueEx(
                    key, "MyClaudeCodeDesktop", 0, winreg.REG_SZ, "x"
                )
        """,
    )
    _assert_refused(completed, "winreg.OpenKey(..., write access) refused")


def test_a_marked_registry_write_is_allowed_and_stays_in_memory(
    tmp_path: Path,
) -> None:
    """The opt-in still gets the fake. Nothing may reach the real registry."""

    completed = _run_child(
        tmp_path,
        """
        @pytest.mark.touches_registry
        def test_records(hermetic_marker_gates):
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\\Microsoft\\Windows\\CurrentVersion\\Run",
                0,
                winreg.KEY_SET_VALUE,
            ) as key:
                winreg.SetValueEx(
                    key, "MyClaudeCodeDesktop", 0, winreg.REG_SZ, "x"
                )
                assert winreg.QueryValueEx(key, "MyClaudeCodeDesktop")[0] == "x"
            assert ("SetValueEx", key.path, "MyClaudeCodeDesktop") in (
                hermetic_marker_gates.writes
            )
        """,
    )
    _assert_green(completed)


def test_a_registry_delete_without_the_marker_fails_the_run(tmp_path: Path) -> None:
    completed = _run_child(
        tmp_path,
        """
        def test_leaks():
            import winreg

            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\\Microsoft\\Windows\\CurrentVersion\\Run",
                0,
                winreg.KEY_QUERY_VALUE,
            )
            winreg.DeleteValue(key, "MyClaudeCodeDesktop")
        """,
    )
    _assert_refused(completed, "winreg.DeleteValue refused")


# --------------------------------------------------------------- file writes


def test_a_write_below_the_config_directory_fails_the_run(tmp_path: Path) -> None:
    """B2, and the exact shape the 6.41.1 equality guard let through."""

    completed = _run_child(
        tmp_path,
        """
        def test_leaks():
            target = PROFILE / ".fcc" / "crush" / "crush.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("x", encoding="utf-8")
        """,
    )
    _assert_refused(completed, "crush", "HERMETICITYVIOLATION")


def test_opening_the_real_dotenv_for_writing_fails_the_run(tmp_path: Path) -> None:
    completed = _run_child(
        tmp_path,
        """
        def test_leaks():
            with open(PROFILE / ".fcc" / ".env", "w", encoding="utf-8") as handle:
                handle.write("ANTHROPIC_AUTH_TOKEN=leaked")
        """,
    )
    _assert_refused(completed, ".env")


def test_replacing_a_file_into_the_config_directory_fails_the_run(
    tmp_path: Path,
) -> None:
    """The atomic-write idiom the catalogue publisher uses."""

    completed = _run_child(
        tmp_path,
        """
        def test_leaks(tmp_path):
            source = tmp_path / "staged"
            source.write_text("x", encoding="utf-8")
            os.replace(source, PROFILE / ".fcc" / "desktop.json")
        """,
    )
    _assert_refused(completed, "os.replace", "desktop.json")


def test_a_start_menu_shortcut_fails_the_run(tmp_path: Path) -> None:
    """B6: nothing does this today, so this is the fence that keeps it so."""

    completed = _run_child(
        tmp_path,
        """
        def test_leaks():
            start_menu = (
                PROFILE
                / "AppData"
                / "Roaming"
                / "Microsoft"
                / "Windows"
                / "Start Menu"
                / "Programs"
            )
            start_menu.mkdir(parents=True, exist_ok=True)
            (start_menu / "My Claude Code.lnk").write_text("x", encoding="utf-8")
        """,
    )
    _assert_refused(completed, "StartMenu")


def test_copying_into_the_config_directory_fails_the_run(tmp_path: Path) -> None:
    completed = _run_child(
        tmp_path,
        """
        import shutil

        def test_leaks(tmp_path):
            source = tmp_path / "wheel.whl"
            source.write_bytes(b"actual-bytes")
            shutil.copy2(source, PROFILE / ".fcc" / "w.whl")
        """,
    )
    _assert_refused(completed, "shutil.copy2", "w.whl")


# ---------------------------------------------------------------- subprocess


def test_launching_a_browser_fails_the_run(tmp_path: Path) -> None:
    completed = _run_child(
        tmp_path,
        """
        def test_leaks():
            subprocess.Popen(["msedge", "--headless"])
        """,
    )
    _assert_refused(completed, "subprocess launch of 'msedge' refused")


def test_a_marked_launch_is_allowed_through(tmp_path: Path) -> None:
    """Opting in gets the real ``Popen``, whatever the machine then says."""

    completed = _run_child(
        tmp_path,
        """
        @pytest.mark.spawns_process
        def test_allowed():
            try:
                process = subprocess.Popen(["msedge", "--headless"])
            except OSError:
                # No Edge on this machine: the guard still let the call through,
                # which is the whole assertion.
                return
            process.kill()
            process.wait(timeout=30)
        """,
    )
    _assert_green(completed)


# -------------------------------------------------------------------- ports


def test_binding_the_default_server_port_fails_the_run(tmp_path: Path) -> None:
    """The developer runs a server on 8082. No test may fight it for the port."""

    completed = _run_child(
        tmp_path,
        """
        def test_leaks():
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 8082))
        """,
    )
    _assert_refused(completed, "bind to port 8082 refused")


# ------------------------------------------------------- config-dir teardown


def test_resolving_the_real_config_directory_fails_at_teardown(
    tmp_path: Path,
) -> None:
    """(c)(6): the assertion this replaces was dead code for three releases."""

    completed = _run_child(
        tmp_path,
        """
        def test_leaks(monkeypatch):
            from my_claude_code.config import paths

            monkeypatch.setenv("HOME", str(PROFILE))
            monkeypatch.setenv("USERPROFILE", str(PROFILE))
            paths.reset_config_dir_cache()
            assert paths.config_dir_path() == PROFILE / ".fcc"
        """,
    )
    _assert_refused(completed, "resolved the config directory to")


# ------------------------------------------------------------------ control


def test_an_isolated_case_still_passes(tmp_path: Path) -> None:
    """The guard has to be a filter, not a wall.

    Writing under ``tmp_path``, reading the registry, binding an ephemeral port
    and launching ``python`` are all things the suite does on purpose, and every
    one of them must stay green -- otherwise the guard would simply be refusing
    everything and the cases above would prove nothing.
    """

    completed = _run_child(
        tmp_path,
        """
        def test_is_hermetic(tmp_path):
            import winreg

            (tmp_path / "artefact.json").write_text("{}", encoding="utf-8")
            (tmp_path / "nested" / "deep").mkdir(parents=True)
            with open(tmp_path / "nested" / "deep" / "f", "w") as handle:
                handle.write("x")

            # A read of the registry answers from the in-memory store rather
            # than failing: 720 tests reach it through urllib and platform.
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                with pytest.raises(FileNotFoundError):
                    winreg.QueryValueEx(key, "PATH")

            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                assert listener.getsockname()[1] != 0

            assert subprocess.run(
                [sys.executable, "-c", "print(1)"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip() == "1"

            # And the redirect really happened.
            assert Path.home() != PROFILE
            assert Path.home().is_relative_to(tmp_path.parent.parent)
        """,
    )
    _assert_green(completed)


def test_the_default_home_redirect_keeps_the_config_dir_out_of_the_real_one(
    tmp_path: Path,
) -> None:
    """B3: with HOME redirected, nothing reads the developer's real ``.env``."""

    completed = _run_child(
        tmp_path,
        """
        def test_config_dir_is_isolated():
            from my_claude_code.config.paths import config_dir_path

            resolved = config_dir_path()
            assert not resolved.is_relative_to(PROFILE)
        """,
    )
    _assert_green(completed)
