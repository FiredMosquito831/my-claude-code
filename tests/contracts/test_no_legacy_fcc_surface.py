"""The grep gate: nothing may name the retired command family or product name.

7.0.0 asked for one command:

    rg -n "\\.fcc|fcc-|FCC_" src/ scripts/ packaging/ docs/ README.md

and required that what comes back is only the migration code and docs plus the
retired-command tombstones. A bare grep cannot express "only", so the gate was
this test, which runs the same search and checks every hit against an
enumerated allowlist.

**8.0.0 retires the family for good and tightens the gate** (USER DECISIONS
2026-10-31 ~07:00, Q13 — "fully retire old fcc commands and any mentions"):

* the pattern gains the two spellings of the product name, which the 7.0.0
  pattern never saw: ``free-claude-code`` contains no ``fcc-`` at all, so the
  tombstone's own target, the ``--version`` string and a docs sentence could all
  have come back with the gate green;
* the roots gain ``ARCHITECTURE.md`` (a published document), ``pyproject.toml``
  (where the commands are declared) and ``.github`` (the bug-report form is what
  a user fills in);
* the fourteen console scripts and the module behind them are gone, and
  ``test_no_console_script_is_a_retired_name`` below is the part that no
  allowlist entry can excuse.

What stays is deliberate and each entry says why. Three kinds:

1. **The one-time ``~/.fcc`` -> ``~/.mcc`` move** (6.65.0, still live): a data
   rescue for an install old enough to have a legacy home. It is not a command
   and not a product name.
2. **Values already written to users' disks** -- ``.fcc-tmp``, ``.fcc-backup``,
   ``fcc-managed``, ``fcc-no-auth``, the Codex provider id ``fcc``, the Pi
   provider id, the legacy distribution name. Renaming one silently breaks an
   install that has it, so they keep their spelling for ever.
3. **The record of what was retired** (this repo's own brand and release
   documents) and **the upstream MIT notice**, which travels with the code.

A new hit anywhere else fails, and the fix is to remove the string -- not to add
a line here without one.

**8.0.2 (OWNEDBY) closes the last user-visible occurrence.** ``/v1/models``
reported ``owned_by: free-claude-code`` on every row. 8.0.0 changed it and
reverted, because ``tests/contracts/test_metadata_ladder_equality.py`` and
``tests/contracts/test_mcp_media_off_is_byte_identical.py`` pin that field
byte-for-byte; 8.0.2 changed the value to :data:`NATIVE_DISTRIBUTION` and
recaptured both baselines (only the ``owned_by`` cells moved). So the
allow-list entry that excused ``api/model_catalog.py`` is **deleted** -- the
module that emits the field needs no licence for it -- and
``test_the_models_payload_reports_mcc_and_never_the_retired_owner`` below states
the half the scan cannot: the value built into the payload, not just the
literals in ``src/``.
"""

import json
import re
import tomllib
from pathlib import Path

from my_claude_code.api.model_catalog import build_models_list_response
from my_claude_code.config.settings import Settings
from my_claude_code.core.version import NATIVE_DISTRIBUTION
from tests.application.test_catalogue_model import FakeRuntime

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The spec's pattern, plus the product name the 7.0.0 pattern could not see.
GATE_PATTERN = r"\.fcc|fcc-|FCC_|free-claude-code|free_claude_code"

#: The spec's five roots, plus the three 8.0.0 adds: a published document, the
#: file the commands are declared in, and the GitHub templates a user reads.
GATE_ROOTS = (
    "src",
    "scripts",
    "packaging",
    "docs",
    "README.md",
    "ARCHITECTURE.md",
    "pyproject.toml",
    ".github",
)

#: A retired console-script name: the family prefix, or the old product name.
RETIRED_COMMAND_PATTERN = re.compile(r"^(?:fcc-|free-claude-code$)")

#: path (posix, repo-relative) -> why any ``fcc`` string in it is deliberate.
#: A file listed here is allowed to carry the strings its reason describes; it
#: is NOT a blanket exemption for the repository.
ALLOWED_FILES: dict[str, str] = {
    # --- 1. the one-time ~/.fcc -> ~/.mcc move (shipped 6.65.0, still live) ---
    "src/my_claude_code/config/paths.py": (
        "is the migration: the legacy home, the rollback-note dir, the "
        "migrated pointer and the resolver that reads all three"
    ),
    "src/my_claude_code/cli/first_start.py": "performs the one-time migration",
    "src/my_claude_code/cli/migrate_config_dir.py": (
        "is mcc-migrate, the command that moves the legacy home on request"
    ),
    "src/my_claude_code/cli/entrypoints.py": (
        "mcc-help prints mcc-migrate's line, which names the home it moves"
    ),
    "src/my_claude_code/api/admin_routes.py": "the config-home banner explains the move",
    "src/my_claude_code/core/request_log.py": "documents the config-dir rule",
    "scripts/install.ps1": (
        "Get-MccConfigDir keeps the legacy-home rung, and Get-ManagedShimName "
        "sweeps the family pattern so an older install's shims are moved aside "
        "instead of handed to uv (a shim uv cannot replace aborts the install)"
    ),
    "scripts/install.sh": (
        "the same legacy-home rung and the same family pattern in the "
        "missing-entry-point sweep"
    ),
    "scripts/uninstall.ps1": (
        "removes both config homes and both tool owners, so a pre-5.14 install "
        "still uninstalls cleanly"
    ),
    "scripts/uninstall.sh": "the same, in the POSIX uninstaller",
    ".github/workflows/install-smoke.yml": (
        "the install smoke asserts both config homes are left untouched"
    ),
    # --- 2. the one-time FCC_* -> MCC_* rewrite of a managed .env (7.0.0) ---
    "src/my_claude_code/config/legacy_env_rewrite.py": (
        "is the one-time FCC_* -> MCC_* rewrite of a managed .env"
    ),
    "src/my_claude_code/config/settings.py": (
        "warns about FCC_* names a shell still exports"
    ),
    "src/my_claude_code/config/env_files.py": "says the FCC_ENV_FILE alias is gone",
    "src/my_claude_code/config/admin/persistence.py": (
        "says why FCC_ is no longer an owned prefix"
    ),
    "src/my_claude_code/cli/desktop.py": "says the FCC_OPEN_BROWSER alias is gone",
    # 7.122.3 removed the dashboard's last mentions, so admin_static/ is not on
    # this list: the gate fails if any of them comes back.
    # --- 3. values already written to users' disks -------------------------
    "src/my_claude_code/config/atomic_json.py": (
        '".fcc-tmp" is the atomic staging suffix; renaming it would orphan a '
        "staging file written by the previous version"
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
    "src/my_claude_code/providers/chatgpt_oauth/provider.py": (
        "compares against that stored source name"
    ),
    "src/my_claude_code/config/proxy_auth.py": '"fcc-no-auth" is a stored sentinel value',
    "src/my_claude_code/cli/launchers/kimi.py": "documents that sentinel",
    "src/my_claude_code/cli/launchers/codex.py": (
        '"fcc" is the provider id inside the user\'s own Codex configuration; '
        "moving it would break their profiles and history (documented in-code)"
    ),
    "src/my_claude_code/cli/launchers/pi.py": (
        "the Pi provider id the bundled extension registers; both sides are one "
        "edit and Pi records model ids from it once per launch"
    ),
    "src/my_claude_code/cli/launchers/pi_extension.ts": "registers that provider id",
    "docs/CLAUDE-CODE-CONFIG.md": "names the .fcc-backup file",
    "docs/CLIENTS.md": (
        "shows the Codex provider id the user's own ~/.codex/config.toml carries"
    ),
    "docs/ANTHROPIC-SUBSCRIPTION.md": "names the legacy store path",
    "scripts/gen_claude_config_reference.py": "names the .fcc-backup file",
    # --- 4. the legacy tool owner an old install is upgraded/uninstalled through
    # 8.0.2 (OWNEDBY): ``api/model_catalog.py`` is NOT here any more. It carried
    # the ``owned_by`` value the two byte-identity baselines pinned; 8.0.2
    # changed it to NATIVE_DISTRIBUTION and recaptured both, so the module that
    # emits the field must carry no retired string and needs no entry.
    "src/my_claude_code/core/version.py": (
        "a pre-5.14 install's distribution is still named free-claude-code; "
        "the version lookup falls back to it so such an install reports its "
        "real version"
    ),
    "src/my_claude_code/application/release_updates.py": (
        "carries extras and the Python pin from whichever tool environment "
        "owns the install, including the pre-5.14 one"
    ),
    "src/my_claude_code/core/identity.py": (
        "records that owner's command family so an install can be recognised "
        "and uninstalled; nothing reads the OS-level script rows any more"
    ),
    "src/my_claude_code/core/mcc_processes.py": (
        "an fcc-* process from an older install is still an MCC process; the "
        "detector must keep seeing it, and forgetting it would let a live "
        "server be reported as dead"
    ),
    "src/my_claude_code/providers/chatgpt_oauth/oauth_login.py": (
        "the OAuth device-flow User-Agent, an outbound header rather than a "
        "user-visible surface; renaming it changes the sign-in handshake"
    ),
    # --- 5. the record of what was retired, and the inherited MIT notice ----
    "README.md": (
        "the Project history / credits section links the upstream repository "
        "the code came from, beside the MIT notice in LICENSE"
    ),
    "ARCHITECTURE.md": (
        "documents the legacy-home rule and the FCC_* rewrite that still run"
    ),
    "docs/USAGE.md": "the migration walkthrough and the sentinel values",
    "docs/BRAND.md": "is the record of what the rename retired",
    "docs/RELEASE-CHECKLIST.md": "is the record of what the rename retired",
    ".github/workflows/validate-bug-report-version.yml": (
        "accepts a legacy-prefixed version string in an issue filed before the "
        "rebrand, and its label and marker names are issue state on the "
        "server; renaming either orphans existing issues"
    ),
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


def _installed_commands() -> dict[str, str]:
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]
    commands = dict(project.get("scripts", {}))
    commands.update(project.get("gui-scripts", {}))
    return commands


def test_no_console_script_is_a_retired_name() -> None:
    """8.0.0's own subject, and the one gate no allowlist entry can excuse.

    The fourteen tombstones were the only way a retired name could be installed.
    ``pyproject.toml`` is a gate root as well, so a re-added entry fails both
    here and in the scan below -- but this test is the one that says *why*.
    """

    retired = sorted(
        name for name in _installed_commands() if RETIRED_COMMAND_PATTERN.search(name)
    )
    assert retired == [], (
        "pyproject.toml still installs retired command names: "
        f"{retired}. The family was removed in 8.0.0 (USER DECISIONS "
        "2026-10-31 ~07:00, Q13); a command that no longer works must not be "
        "installed at all."
    )


def test_no_entry_point_targets_the_tombstone_module() -> None:
    """The module is deleted -- nothing may still point at it."""

    targets = set(_installed_commands().values())
    offenders = sorted(target for target in targets if "cli.legacy_stubs" in target)
    assert offenders == [], (
        f"an entry point still resolves to the tombstones: {offenders}"
    )

    module = REPO_ROOT / "src" / "my_claude_code" / "cli" / "legacy_stubs.py"
    assert not module.exists(), (
        "src/my_claude_code/cli/legacy_stubs.py is back; the tombstone family "
        "was removed in 8.0.0 and nothing may register it again"
    )


def test_the_grep_gate_returns_only_allowed_files() -> None:
    hits = _gate_hits()
    assert hits, "the gate found nothing at all -- the search itself is broken"

    offenders: dict[str, list[str]] = {}
    for path, line, text in hits:
        if path in ALLOWED_FILES:
            continue
        offenders.setdefault(path, []).append(f"{line}: {text.strip()[:100]}")

    assert not offenders, (
        "these files name the retired command family or the old product name "
        "and are not on the gate's allowlist:\n"
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
        f"these files are on the gate's allowlist but no longer contain any "
        f"retired surface: {stale}. Delete the entries."
    )


def test_every_allowlist_entry_says_why() -> None:
    """A path with no reason is the failure mode this list exists to stop."""

    unexplained = sorted(
        path for path, reason in ALLOWED_FILES.items() if len(reason) < 20
    )
    assert unexplained == [], (
        f"these allowlist entries carry no real reason: {unexplained}"
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


def test_the_models_payload_reports_mcc_and_never_the_retired_owner() -> None:
    """8.0.2: the value on the wire, not only the literals in ``src/``.

    The scan above cannot see a name that is assembled or read from somewhere
    else, and ``owned_by`` is the one place a client actually reads the product
    name. 8.0.2 answers it with :data:`NATIVE_DISTRIBUTION` -- the same constant
    that names the distribution, not a second spelling of the brand -- and
    deleted this module's allow-list entry, so a reintroduced literal fails the
    scan above as well as this test.
    """

    settings = Settings.model_validate({"MODEL": "open_router/example"})
    payload = build_models_list_response(settings, FakeRuntime(settings=settings))

    assert payload.data, "the models payload is empty; this test would pass vacuously"
    assert {entry.owned_by for entry in payload.data} == {NATIVE_DISTRIBUTION}
    assert "free-claude-code" not in json.dumps(payload.model_dump())
