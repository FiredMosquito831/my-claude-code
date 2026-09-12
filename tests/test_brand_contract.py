"""Brand contract + import-boundary tests for the My Claude Code (MCC) rebrand.

These are deterministic, file-based checks. They assert that the public rebrand
landed AND that the published legacy contracts the rebrand must NOT break are
still intact:

Kept contracts (must not change):
  - RETIRED in 7.0.0: the FCC_* environment variables (rewritten once in a
    managed .env) and the working fcc-* command family (now tombstones)
  - Release repository FiredMosquito831/my-claude-code (RELEASE_REPO)
  - Config dir ".fcc" (still read, and migrated once on a first start)
  - Legacy fcc-* names still REGISTERED, so a retired name says what replaced it
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
    assert "FCC_*" in brand  # documents what 7.0.0 retired


# â”€â”€ Kept contracts intact â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def test_the_template_ships_only_canonical_env_names():
    env = _read(".env.example")
    assert "MCC_OPEN_BROWSER" in env  # the canonical name, not the FCC_* alias
    assert "FCC_OPEN_BROWSER" not in env  # retired in 7.0.0


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


def test_legacy_shim_package_is_gone():
    # 7.0.0 removed the free_claude_code compatibility namespace. Nothing in
    # the product ever imported it; it existed for third-party code that never
    # materialised, and shipping a second importable name for one package is
    # exactly the kind of surface this release removes.
    assert not (REPO / "src" / "free_claude_code").exists()

    pyproject = _read("pyproject.toml")
    assert 'packages = ["src/my_claude_code"]' in pyproject
    assert "free_claude_code" not in pyproject


# -- The dashboard wears the real mark ---------------------------------------


def test_dashboard_brand_mark_is_the_packaged_mark_not_initials():
    """BRAND.md S5: the mark is the shipped icon, never gradient initials.

    The sidebar carried a literal `<div class="brand-mark">MC</div>` text
    badge on an accent gradient for the product's whole life, which is
    exactly the "gradient initials" the brand document forbids. Guard both
    halves: the image is wired up, and the text badge does not come back.
    """

    index = _read("src/my_claude_code/api/admin_static/index.html")
    assert '<div class="brand-mark">MC</div>' not in index
    assert 'src="/admin/img/app-icon-96.png"' in index
    assert 'alt="My Claude Code"' in index
    # The same mark is the page's favicon rather than a blank data: URI.
    assert '<link rel="icon" type="image/png" href="/admin/img/app-icon-96.png" />' in (
        index
    )
    # The mark ships with the package, small enough to load with the page.
    mark = REPO / "src/my_claude_code/api/admin_static/img/app-icon-96.png"
    assert mark.is_file()
    assert mark.stat().st_size <= 20 * 1024


def test_dashboard_brand_mark_is_not_recolored_or_glowed():
    """BRAND.md S5: no gradient, glow or drop shadow on top of the mark.

    The mark's inner glyph is near-black navy on a transparent ground, so it
    needs an opaque light plate to survive the three dark themes -- but the
    plate is behind it, and the mark itself stays untouched.
    """

    css = _read("src/my_claude_code/api/admin_static/admin.css")
    start = css.index(".brand-mark {")
    block = css[start : css.index("}", start)]
    assert "background: #ffffff;" in block
    assert "linear-gradient" not in block
    assert "box-shadow" not in block
    # The footprint the sidebar layout depends on is unchanged.
    assert "width: 40px;" in block
    assert "height: 40px;" in block
