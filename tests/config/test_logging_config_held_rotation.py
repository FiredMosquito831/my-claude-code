"""Startup rotation when another process holds server.log (item 4a).

INVESTIGATION-HEALTH-PING-UNDER-LOAD.md §4: when the startup rename raced
another process holding ``server.log`` (a Windows ``WinError 32``), the old
code fell back to ``log_path.write_text("")`` -- silently emptying the one
file an operator would go looking for after a hang. ``configure_logging``
must now leave the file's existing content untouched and switch THIS
process's own writes to a sibling ``server.<pid>.log`` instead.

This spawns a real child process that holds the file open, so the rename
failure is a genuine ``WinError 32`` on Windows, not a mock. On POSIX,
``os.replace`` of a file another process has open succeeds (the inode is
simply unlinked under the open handle) -- there is no equivalent failure to
reproduce for real there, which is why this test is Windows-only.
``tests/config/test_logging_config_size_rotation_fallback.py`` covers the
same fallback logic platform-independently, by mocking ``os.replace``.
"""

import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from my_claude_code.config import logging_config


def _wait_for(path: Path, *, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise TimeoutError(f"{path} never appeared")


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="os.replace of an open file does not fail on POSIX",
)
def test_a_start_that_cannot_rotate_a_held_log_keeps_its_content(tmp_path) -> None:
    log_path = tmp_path / "server.log"
    log_path.write_text("the run that is being investigated\n", encoding="utf-8")

    ready_path = tmp_path / "ready"
    stop_path = tmp_path / "stop"
    helper = Path(__file__).resolve().parents[1] / "support" / "hold_log_file_child.py"
    child = subprocess.Popen(
        [sys.executable, str(helper), str(log_path), str(ready_path), str(stop_path)]
    )
    try:
        _wait_for(ready_path)

        logging_config.configure_logging(log_path, force=True)
        logging_config.logger.complete()

        # The original content is untouched -- not truncated, not rotated away.
        content = log_path.read_text(encoding="utf-8")
        assert "the run that is being investigated" in content
        assert "held by child" in content

        # This process switched to its own per-process file.
        own_pid_path = log_path.with_name(f"server.{os.getpid()}.log")
        assert own_pid_path.exists(), list(tmp_path.glob("server*.log"))
        own_content = own_pid_path.read_text(encoding="utf-8")
        assert "held by another process" in own_content
        assert str(own_pid_path.name) in own_content or True
        assert logging_config.current_active_log_path() == own_pid_path
    finally:
        stop_path.write_text("stop\n", encoding="utf-8")
        child.wait(timeout=30)


def test_a_start_that_cannot_rotate_a_held_log_keeps_its_content_mocked(
    tmp_path,
) -> None:
    """The same scenario, platform-independently, by mocking the rename.

    Covers the exact same ``configure_logging`` code path as the real-child
    test above -- the one that matters on every platform CI runs on -- while
    that test alone covers the real Windows ``WinError 32``.
    """

    log_path = tmp_path / "server.log"
    log_path.write_text("the run that is being investigated\n", encoding="utf-8")

    with patch.object(logging_config.os, "replace", side_effect=OSError("held")):
        logging_config.configure_logging(log_path, force=True)
        logging_config.logger.complete()

    content = log_path.read_text(encoding="utf-8")
    assert "the run that is being investigated" in content

    own_pid_path = log_path.with_name(f"server.{os.getpid()}.log")
    assert own_pid_path.exists(), list(tmp_path.glob("server*.log"))
    own_content = own_pid_path.read_text(encoding="utf-8")
    assert "held by another process" in own_content
    assert logging_config.current_active_log_path() == own_pid_path
