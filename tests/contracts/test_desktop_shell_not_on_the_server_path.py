"""The shell fetcher is desktop machinery, and the server must never pay for it.

``config/desktop_shell.py`` pulls in ``tarfile``, ``zipfile``, ``hashlib`` and
``urllib.request``, and its whole reason to exist is a launch-time download.
None of that belongs anywhere near ``mcc-server``'s cold start, and nothing on
the server path has any reason to ask it a question -- so the contract is the
strong one: a fresh interpreter that builds the ASGI app must not have imported
the module at all.

The second half is the import-boundary half. The fetcher lives in ``config``,
which is the package allowed to import nothing of ours, so it must not reach
into ``cli`` or ``application`` for the window it exists to install.
"""

import ast
import subprocess
import sys
from pathlib import Path

from my_claude_code.config import desktop_shell

_MODULE = "my_claude_code.config.desktop_shell"

_PROBE = "\n".join(
    (
        "import sys",
        "import my_claude_code.runtime.bootstrap",
        "import my_claude_code.api.app",
        f"print({_MODULE!r} in sys.modules)",
    )
)


def test_building_the_asgi_app_never_imports_the_shell_fetcher() -> None:
    completed = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert completed.stdout.strip() == "False", (
        "the server's startup path imported the desktop shell fetcher; it costs "
        "tarfile, zipfile and urllib for a download only mcc-desktop ever makes"
    )


def test_the_fetcher_imports_nothing_from_cli_or_application() -> None:
    """``config`` may import ``config`` and the standard library, and no more."""

    tree = ast.parse(Path(desktop_shell.__file__).read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)

    offenders = [
        name
        for name in imported
        if name.startswith("my_claude_code.")
        and not name.startswith("my_claude_code.config")
    ]
    assert offenders == []


def test_the_install_directory_is_overridable() -> None:
    """Without this a test, a smoke or an installer dry run writes at the user."""

    assert desktop_shell.DESKTOP_SHELL_DIR_ENV == "MCC_DESKTOP_SHELL_DIR"
    source = Path(desktop_shell.__file__).read_text(encoding="utf-8")
    assert source.count('Path.home() / ".local" / "bin"') == 1
