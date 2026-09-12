"""The 7.0.0 grep gate: nothing may say ``fcc`` without a reason on this list.

The spec for 7.0.0 asked for one command:

    rg -n "\\.fcc|fcc-|FCC_" src/ scripts/ packaging/ docs/ README.md

and required that what comes back is only the migration code and docs plus the
retired-command tombstones. A bare grep cannot express "only": what keeps the
surface from growing back is this test, which runs the same search and checks
every hit against an enumerated allowlist. Each entry carries the reason the
string is still there. A new hit anywhere else fails, and the fix is to remove
the string -- not to add a line here without one.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Exactly the spec's pattern and exactly the spec's roots.
GATE_PATTERN = r"\.fcc|fcc-|FCC_"
GATE_ROOTS = ("src", "scripts", "packaging", "docs", "README.md")

#: path (posix, repo-relative) -> why any ``fcc`` string in it is deliberate.
#: A file listed here is allowed to carry the strings its reason describes; it
#: is NOT a blanket exemption for the repository.
ALLOWED_FILES: dict[str, str] = {
    # --- the one-time ~/.fcc -> ~/.mcc migration (shipped 6.65.0, still live) ---
    "src/my_claude_code/config/paths.py": (
        "names the legacy home, the rollback-note dir and the migrated pointer; "
        "the resolver that reads them is the migration"
    ),
    "src/my_claude_code/cli/first_start.py": "performs the one-time migration",
    "src/my_claude_code/cli/migrate_config_dir.py": "is mcc-migrate",
    "src/my_claude_code/api/admin_routes.py": "the config-home banner explains the move",
    "src/my_claude_code/core/request_log.py": "documents the config-dir rule",
    "src/my_claude_code/config/legacy_env_rewrite.py": (
        "is the one-time FCC_* -> MCC_* .env rewrite"
    ),
    "src/my_claude_code/config/settings.py": (
        "warns about FCC_* names still exported by a shell"
    ),
    "src/my_claude_code/config/env_files.py": "says the FCC_ENV_FILE alias is gone",
    "src/my_claude_code/config/admin/persistence.py": (
        "says why FCC_ is no longer an owned prefix"
    ),
    "src/my_claude_code/cli/desktop.py": "says the FCC_OPEN_BROWSER alias is gone",
    "src/my_claude_code/api/admin_static/index.html": (
        "the Guide's migration section and the retirement notice"
    ),
    # --- the retired command family (tombstones, removed in 8.0.0) ---
    "src/my_claude_code/cli/legacy_stubs.py": "is the tombstone module",
    "src/my_claude_code/cli/entrypoints.py": "mcc-help says the family is retired",
    "src/my_claude_code/cli/rtk_commands.py": "says fcc-rtk is retired",
    "src/my_claude_code/core/identity.py": (
        "the pre-5.0.0 free-claude-code tool owner is still recognised so such "
        "an install can be upgraded and uninstalled"
    ),
    "src/my_claude_code/core/mcc_processes.py": (
        "an fcc-* process from an older install is still an MCC process; the "
        "detector must keep seeing it"
    ),
    "scripts/install.ps1": "verifies the tombstone shims exist and says they are retired",
    "scripts/install.sh": "same, plus the legacy config-home rung",
    "scripts/uninstall.ps1": "removes the tombstone shims and both config homes",
    "scripts/uninstall.sh": "same",
    # --- persisted strings: changing the value would break existing installs ---
    "src/my_claude_code/config/atomic_json.py": (
        '".fcc-tmp" is the atomic staging suffix; renaming it would orphan a '
        "staging file written by the previous version"
    ),
    "src/my_claude_code/config/harnesses.py": (
        "the registry records which harnesses have a retired fcc- name, so the "
        "tombstone list and the installer command lists are generated, not typed"
    ),
    "src/my_claude_code/api/admin_static/admin.js": (
        "the config-home banner and the .fcc-backup notice on Coding agents"
    ),
    "src/my_claude_code/providers/anthropic_oauth/auth.py": (
        'names the old "fcc-...auth" placeholder the UI used to show'
    ),
    "src/my_claude_code/providers/anthropic_oauth/provider.py": "same",
    "src/my_claude_code/providers/chatgpt_oauth/provider.py": (
        'compares against the stored "fcc-managed" source name'
    ),
    "src/my_claude_code/config/claude_config_editor.py": "uses that staging suffix",
    "src/my_claude_code/config/env_migrations.py": "uses that staging suffix",
    "src/my_claude_code/config/claude_settings.py": (
        '".fcc-backup" is the name of a backup already on users\' disks'
    ),
    "src/my_claude_code/config/constants.py": (
        '"fcc-managed-oauth"/"fcc-managed-anthropic-oauth" are marker VALUES '
        "stored in users' .env files; changing them would drop their OAuth"
    ),
    "src/my_claude_code/providers/chatgpt_oauth/credentials.py": (
        '"fcc-managed" is the stored credential-source name'
    ),
    "src/my_claude_code/config/proxy_auth.py": (
        '"fcc-no-auth" is a stored sentinel value'
    ),
    "src/my_claude_code/cli/launchers/kimi.py": "documents that sentinel",
    "src/my_claude_code/cli/launchers/codex.py": (
        '"fcc" is the provider id inside the user\'s own Codex configuration; '
        "moving it would break their profiles and history (documented in-code)"
    ),
    # --- documentation of all of the above ---
    "README.md": "the migration bullet and the retired-commands paragraph",
    "docs/USAGE.md": "the migration walkthrough and the retired-commands notes",
    "docs/CLIENTS.md": "the Codex provider id and the retired-commands note",
    "docs/CLAUDE-CODE-CONFIG.md": "names the .fcc-backup file",
    "docs/ANTHROPIC-SUBSCRIPTION.md": "names the legacy store path",
    "docs/BRAND.md": "is the record of what the rename retired",
    "docs/RELEASE-CHECKLIST.md": "is the record of what the rename retired",
    "scripts/gen_claude_config_reference.py": "names the .fcc-backup file",
}


#: Binary and generated trees the search would otherwise walk for nothing.
_SKIP_DIRS = frozenset({"__pycache__", "node_modules", "target", "dist", ".venv"})
_SKIP_SUFFIXES = (".png", ".ico", ".icns", ".jpg", ".gif", ".woff", ".woff2", ".db")


def _gate_hits() -> list[tuple[str, int, str]]:
    """The spec's ``rg`` run, done in-process.

    Deliberately not a subprocess: ripgrep is not installed on the CI runner or
    in the wheel-e2e image, and a gate that silently skips itself when a tool is
    missing is the kind of test that cannot fail. Same regex, same roots.
    """

    pattern = re.compile(GATE_PATTERN)
    hits: list[tuple[str, int, str]] = []
    for root_name in GATE_ROOTS:
        root = REPO_ROOT / root_name
        candidates = [root] if root.is_file() else sorted(root.rglob("*"))
        for path in candidates:
            if not path.is_file() or path.suffix.lower() in _SKIP_SUFFIXES:
                continue
            if _SKIP_DIRS.intersection(path.relative_to(REPO_ROOT).parts):
                continue
            try:
                with open(path, encoding="utf-8", newline=None) as handle:
                    lines = handle.readlines()
            except OSError, UnicodeDecodeError:
                continue
            relative = path.relative_to(REPO_ROOT).as_posix()
            hits.extend(
                (relative, number, line.rstrip("\n"))
                for number, line in enumerate(lines, start=1)
                if pattern.search(line)
            )
    return hits


def test_the_grep_gate_returns_only_allowed_files() -> None:
    hits = _gate_hits()
    assert hits, "the gate found nothing at all -- the search itself is broken"

    offenders: dict[str, list[str]] = {}
    for path, line, text in hits:
        if path in ALLOWED_FILES:
            continue
        offenders.setdefault(path, []).append(f"{line}: {text.strip()[:100]}")

    assert not offenders, (
        "these files name a retired fcc surface and are not on the 7.0.0 "
        "allowlist:\n"
        + "\n".join(
            f"  {path}\n    " + "\n    ".join(lines)
            for path, lines in sorted(offenders.items())
        )
        + "\nRemove the string. Add an entry to ALLOWED_FILES only with the "
        "reason it must stay."
    )


def test_the_allowlist_has_no_stale_entries() -> None:
    """An entry that no longer matches anything is a licence nobody revoked."""

    seen = {path for path, _, _ in _gate_hits()}
    stale = sorted(set(ALLOWED_FILES) - seen)

    assert not stale, (
        f"these files are on the 7.0.0 allowlist but no longer contain any "
        f"fcc surface: {stale}. Delete the entries."
    )


def test_no_fcc_environment_variable_is_read_anywhere() -> None:
    """The alias surface is what 7.0.0 actually removed; prove it is gone.

    A literal ``"FCC_..."`` inside ``src/`` would be a name something can read.
    The only ones left are in the rewrite that renames them and the warning that
    says they are ignored.
    """

    pattern = re.compile(r"""["']FCC_[A-Z0-9_]+["']""")
    offenders: list[str] = []
    for path in sorted((REPO_ROOT / "src").rglob("*.py")):
        relative = path.relative_to(REPO_ROOT).as_posix()
        if relative in (
            "src/my_claude_code/config/legacy_env_rewrite.py",
            "src/my_claude_code/config/settings.py",
        ):
            continue
        text = path.read_text(encoding="utf-8")
        offenders.extend(f"{relative}: {match}" for match in pattern.findall(text))

    assert not offenders, (
        f"FCC_* environment names still appear as string literals: {offenders}"
    )


def test_the_shipped_env_template_names_no_retired_variable() -> None:
    """``.env.example`` is written verbatim into every new install's ``.env``.

    It is a dotfile, so ripgrep skips it by default and the spec's command
    never saw it -- which is exactly how it kept shipping ``FCC_OPEN_BROWSER``
    and sixty ``FCC_SMOKE_*`` keys that 7.0.0 stopped reading. Checked here on
    its own, and the only ``fcc`` string allowed is the migration sentence.
    """

    with open(REPO_ROOT / ".env.example", encoding="utf-8", newline=None) as handle:
        lines = handle.readlines()

    offenders = [
        f"{number}: {line.strip()}"
        for number, line in enumerate(lines, start=1)
        if re.search(GATE_PATTERN, line)
        and "migrat" not in line.lower()
        and not line.lstrip().startswith("# its legacy ~/.fcc")
    ]

    assert not offenders, f".env.example still names a retired surface: {offenders}"
