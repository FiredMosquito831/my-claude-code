"""``mcc-pi``'s identity probe waits 30 s for ``pi --help`` (7.58.4).

Measured 2026-09-27 (``specs/INVESTIGATION-ZEN-SPOOF-PER-HARNESS.md`` F4): Pi
0.82.1's ``pi --help`` took 12.3 s and 14.3 s on the user's machine while it
was busy, the launcher gave it 5 s, ``subprocess.run`` raised
``TimeoutExpired``, and ``mcc-pi`` refused a working Pi with "not a compatible
Pi Coding Agent" and exit 126 -- MCC never received a request. The only
change is the constant; what the probe checks (exit 0 and both help markers)
and what the launcher says when it fails are untouched.
"""

import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from my_claude_code.cli.harnesses.registry import spec_for
from my_claude_code.cli.launchers import pi as pi_launcher

MARKERS = "--extension <path>\n--models <patterns>\n"


def test_the_identity_probe_allows_thirty_seconds() -> None:
    assert pi_launcher._HELP_TIMEOUT_SECONDS == 30.0

    seen: dict[str, Any] = {}

    def run(command: list[str], **kwargs: Any) -> SimpleNamespace:
        seen["command"] = command
        seen.update(kwargs)
        return SimpleNamespace(returncode=0, stdout=MARKERS)

    with patch("my_claude_code.cli.launchers.pi.subprocess.run", side_effect=run):
        assert pi_launcher.pi_binary_is_compatible("resolved-pi") is True
    assert seen["command"] == ["resolved-pi", "--help"]
    assert seen["timeout"] == 30.0


def _help_that_takes(seconds: float) -> Any:
    """A ``subprocess.run`` stand-in for a ``pi --help`` that needs ``seconds``."""

    def run(command: list[str], *, timeout: float, **_kwargs: Any) -> SimpleNamespace:
        if timeout < seconds:
            raise subprocess.TimeoutExpired(command, timeout)
        return SimpleNamespace(returncode=0, stdout=MARKERS)

    return run


@pytest.mark.parametrize("measured", [12.3, 14.3])
def test_the_measured_help_duration_now_passes(measured: float) -> None:
    with patch(
        "my_claude_code.cli.launchers.pi.subprocess.run",
        side_effect=_help_that_takes(measured),
    ):
        assert pi_launcher.pi_binary_is_compatible("resolved-pi") is True


@pytest.mark.parametrize("measured", [12.3, 14.3])
def test_the_old_five_second_probe_refused_it(
    measured: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect, reproduced: the same Pi under the 7.58.3 constant."""

    monkeypatch.setattr(pi_launcher, "_HELP_TIMEOUT_SECONDS", 5.0)
    with patch(
        "my_claude_code.cli.launchers.pi.subprocess.run",
        side_effect=_help_that_takes(measured),
    ):
        assert pi_launcher.pi_binary_is_compatible("resolved-pi") is False


def test_a_help_slower_than_thirty_seconds_is_still_refused() -> None:
    """The probe still has a bound; past it the launcher answers as before."""

    with patch(
        "my_claude_code.cli.launchers.pi.subprocess.run",
        side_effect=_help_that_takes(31.0),
    ):
        assert pi_launcher.pi_binary_is_compatible("resolved-pi") is False


def _slow_fake_pi(tmp_path: Path, seconds: float) -> Path:
    """A real executable that answers ``--help`` like Pi, ``seconds`` late.

    Not named ``pi``: the hermetic suite refuses to launch a coding-agent CLI
    by that name, and this is a stand-in, not Pi.
    """

    script = tmp_path / "fake_pi.py"
    script.write_text(
        f"import sys, time\ntime.sleep({seconds})\nsys.stdout.write({MARKERS!r})\n",
        encoding="utf-8",
    )
    if os.name == "nt":
        launcher = tmp_path / "slow_pi_stand_in.cmd"
        launcher.write_text(
            f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8"
        )
        return launcher
    launcher = tmp_path / "slow_pi_stand_in"
    launcher.write_text(
        f"#!/bin/sh\nexec '{sys.executable}' '{script}' \"$@\"\n", encoding="utf-8"
    )
    launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
    return launcher


def test_a_real_pi_slower_than_five_seconds_is_launched(tmp_path: Path) -> None:
    """End to end through ``launch``: a real process that answers after 6 s.

    6 s sits between the old bound and the new one, so this test fails on the
    7.58.3 constant and passes on this one. The probe runs for real; only the
    child session itself is a mock.
    """

    fake = _slow_fake_pi(tmp_path, 6.0)
    assert "--extension" in spec_for("pi").identity_help_markers
    with (
        patch(
            "my_claude_code.cli.launchers.common.shutil.which",
            return_value=str(fake),
        ),
        # The session, not the probe: ``subprocess`` is one module object, so
        # mocking its ``Popen`` would mock the probe's ``run`` too.
        patch("my_claude_code.cli.launchers.pi.run_client_process") as session,
    ):
        pi_launcher.launch(["--version"])

    assert session.call_count == 1
    assert session.call_args.kwargs["command"] == [str(fake), "--version"]
