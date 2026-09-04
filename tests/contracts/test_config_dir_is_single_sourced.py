"""Pin the config-directory rules so they can never drift again.

``config.paths`` is the single source of the directory name and the legacy
health-check column inventory; ``core.request_log`` mirrors the column set for
its schema guard (``core`` may not import ``config``). This contract fails if
the two ever diverge, which is the latent bug the spec flagged.
"""

from pathlib import Path

from my_claude_code.config import paths
from my_claude_code.core import request_log


def test_paths_columns_match_request_log_required_columns() -> None:
    """The legacy health check and the store agree on the required columns."""
    assert set(paths.LEGACY_REQUEST_LOG_COLUMNS) == set(
        request_log.required_request_columns()
    )


def test_custom_providers_filename_matches_provider_registry() -> None:
    """``paths`` and ``provider_registry`` name the same file."""
    from my_claude_code.config import provider_registry

    assert (
        paths.CUSTOM_PROVIDERS_FILENAME == provider_registry.CUSTOM_PROVIDERS_FILENAME
    )


def _hardcoded_dirnames(root: Path) -> list[str]:
    """Return every non-allowlisted ``.fcc``/``.mcc`` literal in ``root``."""
    import ast

    allowlisted = {
        "config/paths.py",
        "cli/migrate_config_dir.py",
        "cli/commands.py",
        "cli/entrypoints.py",
    }
    hits: list[str] = []
    for path in root.rglob("*.py"):
        if path.name == "__init__.py":
            continue
        rel = path.relative_to(root).as_posix()
        if rel in allowlisted:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        hits.extend(
            f"{rel}:{node.lineno}: {node.value}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value in (".fcc", ".mcc")
        )
    return hits


def test_no_module_hardcodes_a_dot_fcc_or_dot_mcc_dirname() -> None:
    """Only ``paths`` (and the migration module it powers) may name the dirs."""
    root = Path(__file__).resolve().parents[2] / "src" / "my_claude_code"
    offenders = _hardcoded_dirnames(root)
    assert not offenders, "dirnames belong in config.paths: " + "; ".join(offenders)


def test_config_dir_path_is_the_single_choke_point(tmp_path, monkeypatch) -> None:
    """``config_dir_path`` resolves through ``config_dir_resolution``.

    Pinned to a ``tmp_path`` rather than left to resolve whatever this machine
    happens to have. Unpinned, this contract test built a ``Settings`` from the
    developer's real ``.env`` and opened their real ``requests.db`` -- read-only,
    so harmless, but real-home I/O inside a contract test is how the file next
    door came to rename a live ``~/.fcc``.
    """
    from my_claude_code.config import paths

    monkeypatch.setenv(paths.CONFIG_DIR_ENV, str(tmp_path / "config"))
    paths.reset_config_dir_cache()

    assert paths.config_dir_path() == paths.config_dir_resolution().path
    assert paths.config_dir_path() == tmp_path / "config"


#: Files outside ``src/**/*.py`` that a user reads or that ``mcc-init`` copies
#: into a brand-new install. ``test_no_module_hardcodes_a_dot_fcc_or_dot_mcc_dirname``
#: AST-scans Python string constants and sees none of these: ``.env.example`` is
#: the template a fresh ``~/.mcc`` is written from, and the dashboard Guide is
#: markup, so both kept telling new users their config was in ``~/.fcc``.
_USER_FACING_NON_PYTHON = (
    ".env.example",
    "src/my_claude_code/api/admin_static/index.html",
    "src/my_claude_code/api/admin_static/admin.js",
    "src/my_claude_code/api/admin_static/admin.css",
)

#: Legitimate ``fcc`` spellings that are not config-directory names. Each is a
#: user-visible identifier that predates the rename and would orphan real files
#: or real configuration if it moved -- see the comment at each definition.
_ALLOWED_FCC_TOKENS = (
    ".fcc-tmp",  # atomic-write staging suffix, config/atomic_json.py
    ".fcc-backup",  # Claude settings backup suffix, config/claude_settings.py
    ".fcc-old",  # the rollback-note directory mcc-migrate writes
    "fcc-",  # the preserved console-script command family
    "provider.fcc",  # the Codex provider id passed for one launch
    "providers.fcc",
    "model_providers.fcc",
)


def _bare_fcc_hits(text: str) -> list[str]:
    """Return ``.fcc`` mentions that are not one of the allowed spellings."""

    import re

    hits: list[str] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        for match in re.finditer(r"\.fcc", line):
            tail = line[match.start() :]
            if any(tail.startswith(token) for token in _ALLOWED_FCC_TOKENS):
                continue
            head = line[: match.start()]
            if any(head.endswith(token[:-4]) for token in _ALLOWED_FCC_TOKENS):
                continue
            # "legacy" on the same line is a deliberate mention of the old home.
            if "legacy" in line.lower():
                continue
            hits.append(f"{line_number}: {line.strip()}")
    return hits


def test_user_facing_files_outside_python_do_not_name_the_legacy_dir() -> None:
    """``.env.example`` and the dashboard must not describe ``~/.fcc`` as home."""
    repo_root = Path(__file__).resolve().parents[2]
    offenders: list[str] = []
    for relative in _USER_FACING_NON_PYTHON:
        path = repo_root / relative
        if not path.is_file():
            continue
        with open(path, encoding="utf-8", newline=None) as handle:
            text = handle.read()
        offenders.extend(f"{relative}:{hit}" for hit in _bare_fcc_hits(text))

    assert not offenders, (
        "these user-facing files still call the config directory .fcc: "
        + "; ".join(offenders)
        + ". The default is .mcc; a deliberate mention of the old home has to "
        "say 'legacy' on the same line."
    )
