"""Where MCC's own installed entrypoints actually live.

``uv tool install`` puts its shims in a directory it owns, which is on ``PATH``
for a login shell but *not* for a process launched from a Start Menu shortcut,
a LaunchAgent or a desktop file. Asking ``uv`` where that directory is, and only
then falling back to ``PATH``, is the difference between the desktop app finding
``mcc-server``/``mcc-desktop`` and reporting that My Claude Code is not
installed on a machine where it plainly is.

This is one function because two callers need exactly the same answer: the tray,
when it spawns the server child, and the desktop shell, which is handed the
``mcc-desktop`` command it must call back into for ``--print-status``.
"""

import os
import shutil
import subprocess
from pathlib import Path

#: Seconds allowed for ``uv tool dir --bin``. It is a local path lookup; a uv
#: that has not answered in this long is not going to.
UV_TOOL_DIR_TIMEOUT_SECONDS = 15


def uv_tool_bin_dir() -> Path | None:
    """Return the directory ``uv tool install`` puts shims in, if uv is here."""

    uv = shutil.which("uv")
    if uv is None:
        return None
    try:
        completed = subprocess.run(
            [uv, "tool", "dir", "--bin"],
            capture_output=True,
            text=True,
            timeout=UV_TOOL_DIR_TIMEOUT_SECONDS,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return None
    path = completed.stdout.strip()
    return Path(path) if completed.returncode == 0 and path else None


def resolve_installed_command(stem: str) -> str | None:
    """Return the absolute path of an installed MCC entrypoint, or ``None``.

    The uv shim directory wins over ``PATH`` deliberately: it is the copy this
    installation owns, and on Windows a stale shim earlier on ``PATH`` is a real
    failure mode the installer's rename-aside step exists to manage.
    """

    bin_dir = uv_tool_bin_dir()
    if bin_dir is not None:
        candidate = bin_dir / (f"{stem}.exe" if os.name == "nt" else stem)
        if candidate.is_file():
            return str(candidate)
    return shutil.which(stem)
