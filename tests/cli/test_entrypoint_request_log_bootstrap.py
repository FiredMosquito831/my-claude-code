"""Every console script must be able to open a request-log store.

``core.request_log.default_request_log_path()`` used to fall back to
``Path.home() / ".fcc" / "logs" / "requests.db"``. Removing that fallback was
right -- it was the one place a stray in-process consumer could be pointed at
the real database -- but it turned a silent wrong answer into a hard
``RuntimeError``, and the registration that replaces it happens in exactly two
functions. Nothing checked that every entrypoint reaches one of them.

So this file draws the line explicitly: an entrypoint either registers the path
before it does anything, or it demonstrably never opens a store. Both halves are
tested, because "this one doesn't need it" is a claim that rots.
"""

import ast
import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src" / "my_claude_code"

#: Names that mean "this code is about to touch the request log".
_STORE_ACCESS = ("default_request_log_path", "get_request_log_store")


def _scripts() -> dict[str, str]:
    pyproject = tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    project = pyproject["project"]
    return {**project.get("scripts", {}), **project.get("gui-scripts", {})}


@pytest.mark.parametrize("command", sorted(_scripts()))
def test_every_console_script_target_resolves(command: str) -> None:
    """``module:function`` in ``pyproject.toml`` must actually exist."""
    import importlib

    module_name, _, attribute = _scripts()[command].partition(":")
    module = importlib.import_module(module_name)

    assert callable(getattr(module, attribute)), (
        f"{command} points at {module_name}:{attribute}, which is not callable"
    )


#: The entrypoints that go on to open a request-log store, and the command
#: implementation each one delegates to once the bootstrap has run.
_STORE_OPENING_ENTRYPOINTS = (
    ("serve", "serve"),
    ("init", "init"),
    ("compact_log", "compact_log"),
)


@pytest.mark.parametrize(
    ("entrypoint_name", "command_name"),
    _STORE_OPENING_ENTRYPOINTS,
    ids=[name for name, _ in _STORE_OPENING_ENTRYPOINTS],
)
def test_entrypoint_registers_the_request_log_path_before_the_command_runs(
    entrypoint_name: str, command_name: str, tmp_path, monkeypatch
) -> None:
    """The path is registered *before* the command body, not somewhere in it."""
    from my_claude_code.cli import commands, entrypoints
    from my_claude_code.config import paths
    from my_claude_code.core import request_log

    config_dir = tmp_path / "config"
    monkeypatch.setenv(paths.CONFIG_DIR_ENV, str(config_dir))
    paths.reset_config_dir_cache()
    request_log.set_request_log_path(None)

    seen: list[Path] = []

    def record_the_path_the_command_would_see() -> None:
        seen.append(request_log.default_request_log_path())

    with patch.object(commands, command_name, record_the_path_the_command_would_see):
        getattr(entrypoints, entrypoint_name)(())

    assert seen == [config_dir / "logs" / "requests.db"]


def test_migrate_entrypoint_needs_no_registered_request_log_path(
    tmp_path, monkeypatch
) -> None:
    """``mcc-migrate`` runs on a machine where nothing has opened a store.

    It is a dependency-free command by design -- it has to work before the rest
    of the application is composed -- so it must not reach for a path nobody
    registered.
    """
    from my_claude_code.cli import entrypoints, migrate_config_dir
    from my_claude_code.core import request_log

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(migrate_config_dir, "_mcc_is_running", lambda home: "")
    request_log.set_request_log_path(None)

    # Neither directory exists: the command reports that and exits cleanly.
    assert entrypoints.migrate_config_dir(()) == 0
    assert request_log._default_request_log_path is None


def test_the_tray_never_builds_the_app_in_process() -> None:
    """``mcc-desktop`` registers no path, and is safe only because of this.

    The tray launches ``mcc-server`` as a child process rather than composing
    the application itself, so no store is ever opened inside the tray. If that
    ever changes, the tray needs a bootstrap of its own -- and this test is the
    thing that will say so.
    """
    from my_claude_code.cli import desktop

    assert desktop._SERVER_MODULE == "my_claude_code.cli.entrypoints"

    source = (SRC / "cli" / "desktop_entrypoint.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }

    assert not any("api" in name.split(".") for name in imported), (
        f"the tray entrypoint imports the API package ({sorted(imported)}); if it "
        f"now builds the app in-process it must register the request-log path first"
    )


#: Modules a launcher process runs through. None of them may open a store: an
#: ``mcc-claude`` session talks to the server over HTTP and reads documents off
#: disk, and pointing a launcher at the request database would be a second
#: writer on it.
_LAUNCHER_TREES = (
    "cli/launchers",
    "application/catalogues",
)


def _store_access_sites(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.Attribute):
            name = node.attr
        else:
            continue
        if name in _STORE_ACCESS:
            hits.append(
                f"{path.relative_to(REPO_ROOT).as_posix()}:{node.lineno}: {name}"
            )
    return hits


@pytest.mark.parametrize("tree_name", _LAUNCHER_TREES)
def test_launcher_code_never_opens_a_request_log_store(tree_name: str) -> None:
    """No launcher path reaches the request log, so none needs to register it."""
    offenders: list[str] = []
    for source in sorted((SRC / tree_name).rglob("*.py")):
        offenders.extend(_store_access_sites(source))

    assert not offenders, (
        f"{tree_name} touches the request log: {offenders}. A launcher process "
        f"has no bootstrap, so this raises RuntimeError for a real user. Either "
        f"register the path in the launcher entrypoint, or do not open a store."
    )
