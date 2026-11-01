"""Tests for the neutral tool-owner identity behind the migration bridge."""

import sys
from unittest.mock import patch

from my_claude_code.core import identity


def test_legacy_commands_all_map_to_legacy_owner() -> None:
    for command in identity.LEGACY_COMMANDS:
        assert identity.owner_for_command(command) is identity.LEGACY_OWNER


def test_native_commands_all_map_to_native_owner() -> None:
    for command in identity.NATIVE_COMMANDS:
        assert identity.owner_for_command(command) is identity.NATIVE_OWNER


def test_unknown_command_has_no_owner() -> None:
    assert identity.owner_for_command("not-a-real-command") is None


def test_owner_for_distribution_matches_each_owner() -> None:
    assert identity.owner_for_distribution("free-claude-code") is identity.LEGACY_OWNER
    assert identity.owner_for_distribution("my-claude-code") is identity.NATIVE_OWNER
    assert identity.owner_for_distribution("nope") is None


def test_owner_for_invocation_native_names() -> None:
    assert identity.owner_for_invocation("mcc-server") is identity.NATIVE_OWNER
    assert identity.owner_for_invocation("my-claude-code") is identity.NATIVE_OWNER
    assert identity.owner_for_invocation("/abs/path/mcc-codex") is identity.NATIVE_OWNER


def test_owner_for_invocation_legacy_names_resolve_to_the_legacy_owner() -> None:
    assert identity.owner_for_invocation("fcc-server") is identity.LEGACY_OWNER
    assert identity.owner_for_invocation("free-claude-code") is identity.LEGACY_OWNER


def test_an_unknown_invocation_reports_the_native_owner() -> None:
    """8.0.0 removed the legacy command family, so nothing is launched as it.

    Until 8.0.0 an unrecognised ``sys.argv[0]`` fell back to the legacy owner --
    which is how ``mcc-migrate --version`` answered with the retired
    distribution name on Windows (see the extension test below). The native
    distribution is the only one a current install can run.
    """

    assert identity.owner_for_invocation("something-else") is identity.NATIVE_OWNER
    assert identity.owner_for_invocation("pytest") is identity.NATIVE_OWNER


def test_a_console_script_extension_is_stripped_before_the_lookup() -> None:
    """Windows shims are ``.exe``: the table's names never carry a suffix.

    7.122.4 and earlier looked the whole file name up, so ``mcc-migrate.exe``
    missed and ``mcc-migrate --version`` printed ``free-claude-code <ver>`` on
    Windows while the same command printed ``my-claude-code <ver>`` on POSIX.
    """

    for suffix in (".exe", ".EXE", ".cmd", ".bat", ".py"):
        assert (
            identity.owner_for_invocation(f"mcc-migrate{suffix}")
            is identity.NATIVE_OWNER
        ), suffix
        assert (
            identity.owner_for_invocation(f"C:\\tools\\mcc-server{suffix}")
            is identity.NATIVE_OWNER
        ), suffix
    assert identity.owner_for_invocation("/usr/local/bin/my-claude-code") is (
        identity.NATIVE_OWNER
    )


def test_owner_for_invocation_reads_sys_argv() -> None:
    with patch.object(sys, "argv", ["mcc-pi", "--help"]):
        assert identity.owner_for_invocation() is identity.NATIVE_OWNER


def test_running_owner_uses_recorded_distribution() -> None:
    with patch(
        "my_claude_code.core.identity.distribution_name",
        return_value="my-claude-code",
    ):
        assert identity.running_owner() is identity.NATIVE_OWNER


def test_running_owner_falls_back_when_distribution_unknown() -> None:
    with patch(
        "my_claude_code.core.identity.distribution_name",
        return_value="who-knows",
    ):
        assert identity.running_owner() is identity.LEGACY_OWNER


def test_display_name_for_invocation_native_vs_legacy() -> None:
    assert identity.display_name_for_invocation("mcc-server") == "My Claude Code"
    assert identity.display_name_for_invocation("fcc-server") == "Free Claude Code"


def test_each_owner_exposes_its_full_command_family() -> None:
    assert identity.LEGACY_OWNER.commands == identity.LEGACY_COMMANDS
    assert identity.NATIVE_OWNER.commands == identity.NATIVE_COMMANDS


def test_all_commands_is_the_union_of_both_families() -> None:
    assert identity.ALL_COMMANDS == identity.LEGACY_COMMANDS + identity.NATIVE_COMMANDS
    assert set(identity.ALL_COMMANDS) == set(identity.LEGACY_COMMANDS) | set(
        identity.NATIVE_COMMANDS
    )
    assert len(identity.ALL_COMMANDS) == len(identity.LEGACY_COMMANDS) + len(
        identity.NATIVE_COMMANDS
    )


def test_legacy_receipt_records_native_extras_requirement() -> None:
    # The compat wheel depends on my-claude-code, so the real extras/Python pin
    # is recorded against the canonical (native) requirement in the legacy receipt.
    assert identity.LEGACY_OWNER.extras_requirement == identity.NATIVE_DISTRIBUTION


def test_owner_stable_launchers_are_distinct() -> None:
    assert identity.LEGACY_OWNER.stable_launcher == "fcc-server"
    assert identity.NATIVE_OWNER.stable_launcher == "my-claude-code"


def test_migration_bridge_reexports_identity_symbols() -> None:
    import my_claude_code.core.identity as identity_module

    for symbol in (
        "LEGACY_DISTRIBUTION",
        "NATIVE_DISTRIBUTION",
        "LEGACY_COMMANDS",
        "NATIVE_COMMANDS",
        "LEGACY_OWNER",
        "NATIVE_OWNER",
        "owner_for_command",
        "owner_for_distribution",
        "owner_for_invocation",
        "running_owner",
        "display_name_for_invocation",
    ):
        assert hasattr(identity_module, symbol)
