"""Neutral tool-owner identity for the Free Claude Code / My Claude Code bridge.

Two uv-installable tool owners share one implementation. The *legacy* owner
(``free-claude-code``) is the distribution this release still ships; the
*native* owner (``my-claude-code``) is the renamed canonical package that the
legacy compat wheel depends on. Centralising both identities stops tool-name
literals from scattering across the installer, self-updater, launchers, and
compatibility wheel.
"""

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from my_claude_code.core.version import (
    LEGACY_DISTRIBUTION,
    NATIVE_DISTRIBUTION,
    distribution_name,
)

# Human-facing display names used in help text and error messages. The native
# name leads; the legacy name is presented as a legacy alias.
NATIVE_DISPLAY_NAME = "My Claude Code"
LEGACY_DISPLAY_NAME = "Free Claude Code"

# Console-script command families. Each legacy command keeps its original
# implementation; each native command is the same implementation under a new
# name so the two owners are interchangeable entry points.
LEGACY_COMMANDS = (
    "free-claude-code",
    "fcc-server",
    "fcc-init",
    "fcc-claude",
    "fcc-claude-old",
    "fcc-codex",
    "fcc-pi",
    "fcc-chatgpt-oauth-login",
    "fcc-compact-log",
    "fcc-rtk",
    "fcc-help",
)

NATIVE_COMMANDS = (
    "my-claude-code",
    "mcc-server",
    "mcc-init",
    "mcc-claude",
    "mcc-claude-old",
    "mcc-codex",
    "mcc-pi",
    "mcc-chatgpt-oauth-login",
    "mcc-compact-log",
    "mcc-rtk",
    "mcc-help",
)

ALL_COMMANDS = LEGACY_COMMANDS + NATIVE_COMMANDS

#: Suffixes a console-script shim carries on disk. uv writes ``.exe`` shims on
#: Windows and extension-less ones elsewhere; ``.cmd``/``.bat`` are what a
#: hand-written wrapper is called, and ``.py`` is ``python -m``-style entry.
_SCRIPT_EXTENSIONS = frozenset({".exe", ".cmd", ".bat", ".py"})

# Every console-script name maps to exactly one owner key.
_COMMAND_OWNER: dict[str, str] = dict.fromkeys(
    LEGACY_COMMANDS, "legacy"
) | dict.fromkeys(NATIVE_COMMANDS, "native")


@dataclass(frozen=True, slots=True)
class ToolOwner:
    """An installable uv tool identity that owns the shared implementation.

    A tool owner groups everything that must stay consistent across the
    installer, self-updater, and launchers: the distribution name, the command
    family it exposes, the stable launcher that survives an in-place upgrade,
    and where uv records the installed extras and Python pin.
    """

    key: str
    distribution: str
    display_name: str
    commands: tuple[str, ...]
    # Stable launcher within uv's tool bin dir that survives a tool-environment
    # replacement during an in-place upgrade.
    stable_launcher: str
    # Tool-environment directory (under uv's tool root) holding uv-receipt.toml.
    receipt_distribution: str
    # Requirement name inside that receipt whose extras/Python pin to carry
    # across an upgrade.
    extras_requirement: str


LEGACY_OWNER = ToolOwner(
    key="legacy",
    distribution=LEGACY_DISTRIBUTION,
    display_name=LEGACY_DISPLAY_NAME,
    commands=LEGACY_COMMANDS,
    stable_launcher="fcc-server",
    receipt_distribution=LEGACY_DISTRIBUTION,
    # The compat wheel depends on my-claude-code, so the real extras/Python pin
    # lives on the canonical requirement recorded alongside free-claude-code.
    extras_requirement=NATIVE_DISTRIBUTION,
)

NATIVE_OWNER = ToolOwner(
    key="native",
    distribution=NATIVE_DISTRIBUTION,
    display_name=NATIVE_DISPLAY_NAME,
    commands=NATIVE_COMMANDS,
    stable_launcher="my-claude-code",
    receipt_distribution=NATIVE_DISTRIBUTION,
    extras_requirement=NATIVE_DISTRIBUTION,
)

OWNERS: tuple[ToolOwner, ...] = (LEGACY_OWNER, NATIVE_OWNER)
_OWNER_BY_KEY = {owner.key: owner for owner in OWNERS}
_OWNER_BY_DISTRIBUTION = {owner.distribution: owner for owner in OWNERS}


def owner_for_command(command: str) -> ToolOwner | None:
    """Return the owner that exposes ``command``, if any."""

    key = _COMMAND_OWNER.get(command)
    return _OWNER_BY_KEY.get(key) if key is not None else None


def owner_for_distribution(distribution: str) -> ToolOwner | None:
    """Return the owner whose distribution matches ``distribution``."""

    return _OWNER_BY_DISTRIBUTION.get(distribution)


def owner_for_invocation(invocation: str | None = None) -> ToolOwner:
    """Resolve the tool owner from how this process was launched.

    A ``mcc-*`` / ``my-claude-code`` invocation resolves to the native owner.
    The name is taken from ``sys.argv[0]``, which on Windows is the console
    script's shim -- ``...\\Scripts\\mcc-migrate.exe`` -- so the script
    extension is stripped before the lookup. Until 8.0.0 it was not, every
    ``mcc-*.exe`` missed the table and fell through to the legacy owner, and
    ``mcc-migrate --version`` therefore answered with the retired distribution
    name on Windows and with the native one on POSIX.

    Anything unrecognised (a direct ``python -m`` run, a test runner) reports
    the native owner: since 8.0.0 the legacy command family does not exist, so
    there is no invocation left that could belong to it.
    """

    name = invocation if invocation is not None else (sys.argv[0] if sys.argv else "")
    leaf = Path(name).name
    # ``os.path.splitext`` rather than ``with_suffix``: the latter raises on a
    # name that is only an extension, and a Windows shim is ``.exe``/``.cmd``.
    stem, extension = os.path.splitext(leaf)
    if extension.lower() in _SCRIPT_EXTENSIONS:
        leaf = stem
    owner = owner_for_command(leaf)
    return owner if owner is not None else NATIVE_OWNER


def running_owner() -> ToolOwner:
    """Resolve the tool owner this process belongs to.

    Uses the distribution recorded for the running package and falls back to the
    legacy owner when the metadata is unavailable (e.g. a source checkout).
    """

    owner = owner_for_distribution(distribution_name())
    return owner if owner is not None else LEGACY_OWNER


def display_name_for_invocation(invocation: str | None = None) -> str:
    """Return the pretty display name matching the invocation.

    Native invocations lead with ``My Claude Code``; everything else leads with
    ``Free Claude Code``.
    """

    return owner_for_invocation(invocation).display_name
