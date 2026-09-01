"""Both installers must agree with the harness registry, and install no CLI.

The installer installs the proxy. It has never installed a coding agent and
must not start: bundling agents broke installs once, the README states the rule
twice, and every launcher enforces it in code by exiting 127 with the vendor's
own hint. This file encodes the rule for the shell side, and pins the three
generated lists -- verification, RTK enable, post-install summary -- to the
registry so a harness added there cannot be forgotten in one of them.
"""

from pathlib import Path

import pytest

from my_claude_code.config.harnesses import harness_commands, rtk_capable_ids

REPO_ROOT = Path(__file__).resolve().parents[2]

INSTALLERS = {
    "install.sh": (REPO_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8"),
    "install.ps1": (REPO_ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8"),
}

#: npm/pip/curl-installable names of the coding agents MCC can serve, or has
#: been asked to serve. None of them may appear in an installer.
THIRD_PARTY_AGENT_PACKAGES = (
    "@anthropic-ai/claude-code",
    "@openai/codex",
    "pi.dev/install",
    "opencode-ai",
    "@opencode-ai/cli",
    "@kilocode/cli",
    "command-code",
    "@moonshot-ai/kimi-code",
    "qwen-code",
    "@charmland/crush",
    "@google/gemini-cli",
    "aider-chat",
    "@sourcegraph/amp",
)

_INSTALLER_NAMES = sorted(INSTALLERS)


@pytest.mark.parametrize("installer", _INSTALLER_NAMES)
def test_installers_verify_every_registered_harness_command(installer: str) -> None:
    source = INSTALLERS[installer]
    for command in harness_commands():
        assert command.command in source, (
            f"{installer} never mentions {command.command}, so an install that "
            "failed to create that shim would be reported as successful."
        )


@pytest.mark.parametrize("installer", _INSTALLER_NAMES)
def test_installer_rtk_enable_list_matches_rtk_capable_harnesses(
    installer: str,
) -> None:
    expected = ",".join(rtk_capable_ids())
    assert f"mcc-rtk enable {expected}" in INSTALLERS[installer], (
        f"{installer}'s RTK enable list must be exactly the registry's "
        f"RTK-capable harnesses ({expected})."
    )


@pytest.mark.parametrize("installer", _INSTALLER_NAMES)
def test_installer_summary_lists_every_harness(installer: str) -> None:
    """The post-install reference is the first place a user looks."""

    summary = INSTALLERS[installer].split("Use a coding agent through the proxy", 1)
    assert len(summary) == 2, f"{installer} has no post-install agent section"
    for command in harness_commands():
        if not command.primary:
            continue
        assert command.command in summary[1], (
            f"{installer}'s post-install summary never names {command.command}"
        )


@pytest.mark.parametrize("installer", _INSTALLER_NAMES)
def test_installers_never_install_a_third_party_cli(installer: str) -> None:
    """The standing product rule, as a test rather than a sentence in a README."""

    source = INSTALLERS[installer]
    offenders = [package for package in THIRD_PARTY_AGENT_PACKAGES if package in source]
    assert offenders == [], (
        f"{installer} references a third-party coding agent package "
        f"({offenders}). The installer installs the proxy only; a missing "
        "agent is reported by its launcher with the vendor's own install line "
        "and exit 127."
    )


@pytest.mark.parametrize("installer", _INSTALLER_NAMES)
def test_installers_still_say_out_loud_that_they_install_no_agent(
    installer: str,
) -> None:
    source = INSTALLERS[installer]
    assert "does not install" in source.lower(), (
        f"{installer} no longer tells the user it installs no coding agent."
    )
