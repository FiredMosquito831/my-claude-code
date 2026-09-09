"""Brand contract + import-boundary tests for the My Claude Code (MCC) rebrand.

These are deterministic, file-based checks. They assert that the public rebrand
landed AND that the published legacy contracts the rebrand must NOT break are
still intact:

Kept contracts (must not change):
  - FCC_* environment variables (e.g. FCC_OPEN_BROWSER)
  - Release repository FiredMosquito831/my-claude-code (RELEASE_REPO)
  - Config dir ".fcc" (still read, and migrated once on a first start)
  - Legacy fcc-* command family (preserved as aliases)
  - LEGACY_DISPLAY_NAME = "Free Claude Code"

Rebrand (must be present):
  - Product name "My Claude Code", server command "mcc-server"
  - Package name "my-claude-code"
"""

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


# â”€â”€ Rebrand present â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def test_readme_rebranded():
    readme = _read("README.md")
    assert "My Claude Code" in readme
    assert "Free Claude Code" not in readme


def test_readme_primary_server_command_is_mcc():
    assert "mcc-server" in _read("README.md")


def test_pyproject_package_and_dual_commands():
    pyproject = _read("pyproject.toml")
    assert 'name = "my-claude-code"' in pyproject
    assert "mcc-server = " in pyproject
    # Legacy family preserved as aliases.
    assert "fcc-server = " in pyproject


def test_brand_doc_is_source_of_truth():
    brand = _read("docs/BRAND.md")
    assert "My Claude Code" in brand
    assert "FCC_*" in brand  # documents the preserved legacy contract


# â”€â”€ Kept contracts intact â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def test_kept_fcc_env_vars():
    env = _read(".env.example")
    assert "FCC_OPEN_BROWSER" in env  # FCC_* env var must stay


def test_the_shipped_template_never_carries_a_published_password():
    """``freecc`` was a password printed in a public repository.

    It shipped as the value of ``ANTHROPIC_AUTH_TOKEN`` in ``.env.example``
    beside a ``HOST=0.0.0.0`` default, which made every MCC install on a
    LAN accept the same token. 6.65.0 ships the line empty and generates a
    per-machine token when a first start writes the file. The model ids
    ``claude-3-freecc-*`` are a different, published contract and stay.
    """
    env = _read(".env.example")
    token_lines = [
        line for line in env.splitlines() if line.startswith("ANTHROPIC_AUTH_TOKEN=")
    ]
    assert token_lines == ["ANTHROPIC_AUTH_TOKEN="], token_lines
    assert "HOST=127.0.0.1" in env
    assert "PORT=8082" in env


def test_kept_config_dir():
    readme = _read("README.md")
    assert ".fcc" in readme  # config directory


def test_kept_release_repo():
    # RELEASE_REPO inside the application code.
    assert 'RELEASE_REPO = "FiredMosquito831/my-claude-code"' in _read(
        "src/my_claude_code/application/release_updates.py"
    )
    # Badges / install URLs in public docs still point at the release repo.
    assert "FiredMosquito831/my-claude-code" in _read("README.md")


def test_kept_legacy_display_name():
    assert 'LEGACY_DISPLAY_NAME = "Free Claude Code"' in _read(
        "src/my_claude_code/core/identity.py"
    )


def test_validate_workflow_accepts_dual_versions():
    wf = _read(".github/workflows/validate-bug-report-version.yml")
    # Accepts both the old and new package-name prefixes.
    assert "free-claude-code" in wf
    assert "my-claude-code" in wf
    # Accepts the legacy "FCC version" header and the rebranded ones.
    assert "FCC|MCC|App" in wf


def test_issue_template_uses_app_version():
    tpl = _read(".github/ISSUE_TEMPLATE/bug-report.yml")
    assert "App version" in tpl
    assert "mcc-server --version" in tpl


# â”€â”€ Import boundary â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def test_my_claude_code_imports():
    import my_claude_code  # light __init__ (docstring only)

    assert my_claude_code.__name__ == "my_claude_code"


def test_legacy_shim_package_present():
    # The free_claude_code compatibility shim is still shipped (re-exports
    # my_claude_code); its absence would break the fcc-* aliases.
    assert (REPO / "src" / "free_claude_code" / "__init__.py").exists()
