"""The retired ``fcc-*`` commands are tombstones, and say so (7.0.0)."""

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from my_claude_code.cli import legacy_stubs

REPO_ROOT = Path(__file__).resolve().parents[2]
STUB_TARGET = "my_claude_code.cli.legacy_stubs:main"


def _scripts() -> dict[str, str]:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["scripts"]


def test_every_registered_stub_has_a_replacement_to_name() -> None:
    """A tombstone that cannot say what to type instead is worse than nothing."""

    stubs = {name for name, target in _scripts().items() if target == STUB_TARGET}

    assert stubs == set(legacy_stubs.LEGACY_COMMAND_REPLACEMENTS), (
        "pyproject.toml and LEGACY_COMMAND_REPLACEMENTS disagree about which "
        "names are retired; every registered stub must have a replacement."
    )


def test_every_replacement_is_a_command_that_exists() -> None:
    scripts = _scripts()
    gui = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]["gui-scripts"]
    for legacy, replacement in legacy_stubs.LEGACY_COMMAND_REPLACEMENTS.items():
        assert replacement in scripts or replacement in gui, (
            f"{legacy} points at {replacement}, which is not installed"
        )


def test_no_legacy_name_still_resolves_to_a_working_implementation() -> None:
    """The whole point: not one fcc- name may run product code."""

    for name, target in _scripts().items():
        if name.startswith("fcc-") or name == "free-claude-code":
            assert target == STUB_TARGET, name


@pytest.mark.parametrize(
    ("argv0", "expected"),
    [
        ("fcc-claude", "fcc-claude"),
        (r"C:\uv\tools\bin\fcc-claude.exe", "fcc-claude"),
        ("/home/u/.local/bin/fcc-server", "fcc-server"),
        ("FCC-HELP.EXE", "FCC-HELP"),
    ],
)
def test_invoked_name_strips_the_shim_path_and_suffix(
    argv0: str, expected: str
) -> None:
    """Both separators, on both platforms.

    The Windows case is not skipped off Windows on purpose: CI runs Linux, and
    ``os.path.basename`` there does not split on ``\\`` -- which is how the
    first version of this shipped green locally and failed on the runner.
    """

    assert legacy_stubs.invoked_name(argv0) == expected


def test_the_message_names_both_the_old_and_the_new_command() -> None:
    message = legacy_stubs.retirement_message("fcc-claude")

    assert "fcc-claude" in message
    assert "mcc-claude" in message
    assert "7.0.0" in message
    assert "8.0.0" in message
    assert "\n" not in message  # one line


def test_an_unknown_legacy_name_still_says_the_family_is_gone() -> None:
    message = legacy_stubs.retirement_message("fcc-something-nobody-shipped")

    assert "mcc-help" in message
    assert "7.0.0" in message


def test_running_a_stub_prints_one_line_on_stderr_and_exits_one() -> None:
    """Exit 1, not 0: a script that still calls the old name must fail loudly."""

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.argv[0]='fcc-codex'; "
            "from my_claude_code.cli.legacy_stubs import main; main()",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert completed.stderr.strip().count("\n") == 0
    assert "fcc-codex" in completed.stderr
    assert "mcc-codex" in completed.stderr
