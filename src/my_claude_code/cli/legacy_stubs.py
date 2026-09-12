"""Tombstones for the ``fcc-*`` command family, retired in 7.0.0.

Every command this project ever installed under the ``fcc-`` prefix (plus
``free-claude-code``) resolved, since 5.0.0, to exactly the same implementation
as its ``mcc-`` twin. 7.0.0 removes the legacy surface: the names remain
installed for ONE major version, but they no longer run anything. Each prints a
single line naming its replacement and exits 1.

Why a tombstone rather than nothing:

* A removed console script leaves nothing behind on PATH, and on Windows a
  ``fcc-claude`` typed after the upgrade can silently resolve to a *stale shim*
  that an older install left in another PATH entry -- i.e. run 6.x code against
  a 7.x config. An installed stub always wins over that.
* "command not found" does not say what to type instead. One line does.

These entry points are removed in 8.0.0. Nothing else in the product reads this
module; it exists only to be the target of the retired ``[project.scripts]``
entries.
"""

import os
import sys

#: Legacy command -> the command that replaces it. Pinned by
#: ``tests/cli/test_legacy_stubs.py`` against ``pyproject.toml`` so a retired
#: name can never be registered without a replacement to name.
LEGACY_COMMAND_REPLACEMENTS: dict[str, str] = {
    "fcc-server": "mcc-server",
    "fcc-init": "mcc-init",
    "fcc-claude": "mcc-claude",
    "fcc-claude-old": "mcc-claude-old",
    "fcc-codex": "mcc-codex",
    "fcc-pi": "mcc-pi",
    "fcc-chatgpt-oauth-login": "mcc-chatgpt-oauth-login",
    "fcc-anthropic-oauth-login": "mcc-anthropic-oauth-login",
    "fcc-compact-log": "mcc-compact-log",
    "fcc-rtk": "mcc-rtk",
    "fcc-help": "mcc-help",
    "fcc-migrate": "mcc-migrate",
    "fcc-desktop": "mcc-desktop",
    "free-claude-code": "my-claude-code",
}


def invoked_name(argv0: str | None = None) -> str:
    """Return the bare command name this process was started as.

    ``sys.argv[0]`` is the full path of the console-script shim, and on Windows
    it carries a ``.exe`` suffix. Neither is what the user typed.
    """

    raw = sys.argv[0] if argv0 is None else argv0
    name = os.path.basename(raw)
    root, extension = os.path.splitext(name)
    if extension.lower() in (".exe", ".py", ".cmd", ".bat"):
        name = root
    return name


def retirement_message(name: str) -> str:
    """Return the single line a retired command prints."""

    replacement = LEGACY_COMMAND_REPLACEMENTS.get(name)
    if replacement is None:
        # An ``fcc-`` name that is installed but not in the table: still say the
        # family is gone rather than pretending the command works.
        return (
            f"{name} was removed in My Claude Code 7.0.0; the commands are now "
            f"named mcc-*. Run mcc-help for the list."
        )
    return (
        f"{name} was renamed to {replacement} in My Claude Code 7.0.0. "
        f"Run {replacement} instead (this name is removed in 8.0.0)."
    )


def main() -> None:
    """Print the rename line on stderr and exit 1."""

    print(retirement_message(invoked_name()), file=sys.stderr)
    raise SystemExit(1)
