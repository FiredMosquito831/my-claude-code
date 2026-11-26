"""Git repository detection, isolated worktree creation, and diff helpers."""

import asyncio
import contextlib
import subprocess
import sys
from pathlib import Path
from typing import Any

from my_claude_code.application.remote_ports import RemoteControlError
from my_claude_code.config.paths import RC_REPO_WORKTREES_DIRNAME, rc_worktrees_dir_path

_GIT_TIMEOUT_SECONDS = 10.0
_MAX_DIFF_BYTES = 512 * 1024


def _subprocess_flags() -> int:
    if sys.platform == "win32":
        return int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    return 0


async def _run_git(cwd: Path, *args: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(cwd),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=_subprocess_flags(),
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=_GIT_TIMEOUT_SECONDS
        )
    except TimeoutError:
        with contextlib.suppress(OSError):
            proc.kill()
        await proc.wait()
        return -1, "", "git command timed out"
    return (
        int(proc.returncode or 0),
        stdout_b.decode("utf-8", errors="replace"),
        stderr_b.decode("utf-8", errors="replace"),
    )


async def detect_git_root(cwd: Path) -> Path | None:
    """Return the git repository top-level root for ``cwd``, or ``None``."""

    rc, stdout, _stderr = await _run_git(cwd, "rev-parse", "--show-toplevel")
    if rc != 0 or not stdout.strip():
        return None
    return Path(stdout.strip()).resolve()


async def create_session_worktree(repo_root: Path, session_id: str) -> tuple[Path, str]:
    """Create an isolated git worktree and branch ``rc/<short>`` for a session."""

    branch_name = f"rc/{session_id[:8]}"
    worktree_parent = repo_root / RC_REPO_WORKTREES_DIRNAME
    try:
        worktree_parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        worktree_parent = rc_worktrees_dir_path()
        worktree_parent.mkdir(parents=True, exist_ok=True)
    worktree_path = worktree_parent / session_id

    rc, _stdout, stderr = await _run_git(
        repo_root, "worktree", "add", "-b", branch_name, str(worktree_path)
    )
    if rc != 0:
        raise RemoteControlError(
            f"Could not create git worktree at {worktree_path}: {stderr.strip() or 'git worktree add failed'}"
        )
    return worktree_path.resolve(), branch_name


async def remove_session_worktree(repo_root: Path, worktree_path: Path) -> None:
    """Best-effort removal of a session-owned git worktree."""

    await _run_git(repo_root, "worktree", "remove", "--force", str(worktree_path))


async def collect_git_diff(
    workspace: Path, *, max_bytes: int = _MAX_DIFF_BYTES
) -> dict[str, Any]:
    """Return porcelain status plus bounded unstaged and staged git diffs."""

    root = await detect_git_root(workspace)
    if root is None:
        return {
            "is_git": False,
            "branch": "",
            "status_lines": [],
            "diff": "",
            "staged_diff": "",
            "truncated": False,
        }

    rc_b, branch_out, _ = await _run_git(workspace, "rev-parse", "--abbrev-ref", "HEAD")
    branch = branch_out.strip() if rc_b == 0 else ""

    rc_s, status_out, _ = await _run_git(workspace, "status", "--porcelain=v1")
    status_lines = (
        [line for line in status_out.splitlines() if line.strip()] if rc_s == 0 else []
    )

    _, unstaged, _ = await _run_git(workspace, "diff")
    _, staged, _ = await _run_git(workspace, "diff", "--cached")

    truncated = False
    if len(unstaged.encode("utf-8")) > max_bytes:
        unstaged = unstaged.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
        truncated = True
    if len(staged.encode("utf-8")) > max_bytes:
        staged = staged.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
        truncated = True

    return {
        "is_git": True,
        "branch": branch,
        "status_lines": status_lines,
        "diff": unstaged,
        "staged_diff": staged,
        "truncated": truncated,
    }
