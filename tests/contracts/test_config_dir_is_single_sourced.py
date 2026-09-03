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


def test_config_dir_path_is_the_single_choke_point() -> None:
    """``config_dir_path`` resolves through ``config_dir_resolution``."""
    from my_claude_code.config import paths

    assert paths.config_dir_path() == paths.config_dir_resolution().path
