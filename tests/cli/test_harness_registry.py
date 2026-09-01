"""The harness registry is the single source every surface is generated from.

These are contract tests, not unit tests: each one compares a surface a user
touches -- a console script, an install hint, an exit code -- back to
``config/harnesses.py``. T1-T3 additionally pin the three launchers that
existed before the registry to byte-identical behaviour, because the registry
was allowed to change *where* those strings live and nothing else.
"""

import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest

from my_claude_code.cli.harnesses.registry import (
    install_hint,
    resolve_harness_binary,
    spec_for,
)
from my_claude_code.cli.launchers.claude import build_claude_launcher_command
from my_claude_code.cli.launchers.codex import (
    build_codex_launcher_env,
    codex_config_args,
)
from my_claude_code.cli.launchers.pi import (
    build_pi_launcher_command,
    build_pi_launcher_env,
    is_pi_passthrough,
    pi_install_hint,
)
from my_claude_code.config.harnesses import (
    HARNESS_SPECS,
    catalogue_specs,
    harness_command_lines,
    harness_commands,
    harness_ids,
    harness_spec,
    harness_specs,
    rtk_capable_ids,
)
from my_claude_code.config.paths import CODEX_MODEL_CATALOG_FILENAME

REPO_ROOT = Path(__file__).resolve().parents[2]


def _pyproject_scripts() -> dict[str, str]:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["scripts"]


def test_every_spec_has_a_console_script_entry() -> None:
    scripts = _pyproject_scripts()
    for command in harness_commands():
        assert scripts.get(command.command) == command.target, command.command
        if command.legacy_command is not None:
            assert scripts.get(command.legacy_command) == command.target


def test_every_launcher_console_script_belongs_to_a_registered_harness() -> None:
    registered = {command.command for command in harness_commands()}
    registered |= {
        command.legacy_command
        for command in harness_commands()
        if command.legacy_command is not None
    }
    launcher_scripts = {
        name
        for name, target in _pyproject_scripts().items()
        if ".cli.launchers." in target
    }
    assert launcher_scripts == registered


def test_binary_names_are_unique_across_specs() -> None:
    binaries = [spec.binary for spec in HARNESS_SPECS]
    assert len(binaries) == len(set(binaries))
    assert len(harness_ids()) == len(set(harness_ids()))


def test_catalogue_filename_matches_the_published_path_constant() -> None:
    codex = harness_spec("codex")
    assert codex.catalogue is not None
    assert codex.catalogue.filename == CODEX_MODEL_CATALOG_FILENAME


def test_only_verified_agents_are_marked_rtk_capable() -> None:
    # RTK wraps an agent's own shell tool. Marking a harness capable installs
    # hooks into that agent's global config, so the flag is only ever set for
    # an agent someone has confirmed, never by default.
    assert rtk_capable_ids() == ("claude", "codex", "pi")


def test_pi_is_the_only_harness_with_a_process_local_catalogue() -> None:
    by_delivery = {
        spec.id: spec.catalogue.writes_file
        for spec in catalogue_specs()
        if spec.catalogue is not None
    }
    assert by_delivery == {
        "codex": True,
        "pi": False,
        "opencode": True,
        "opencode2": True,
        "kilo": True,
    }


# ------------------------------------------------------------------- T1 / T2 / T3


def test_claude_spec_builds_the_same_env_as_before() -> None:
    """T1: the Claude launcher's binary, hint and command are unchanged."""

    spec = spec_for("claude")
    assert spec.binary == "claude"
    assert spec.display_name == "Claude Code"
    assert spec.install_hint == (
        "Install Claude Code with: npm install -g @anthropic-ai/claude-code"
    )
    assert install_hint(spec, "win32") == spec.install_hint
    assert build_claude_launcher_command(
        binary_path="claude.cmd", argv=["-p", "hi"]
    ) == ["claude.cmd", "-p", "hi"]


def test_codex_spec_builds_the_same_argv_as_before() -> None:
    """T2: the exact ``-c`` assignment list Codex received before the registry."""

    spec = spec_for("codex")
    assert spec.binary == "codex"
    assert spec.display_name == "Codex CLI"
    assert spec.install_hint == "Install Codex with: npm install -g @openai/codex"
    assert codex_config_args(api_url="http://127.0.0.1:8082/v1") == [
        "-c",
        'model_provider="fcc"',
        "-c",
        'model_providers.fcc.name="My Claude Code"',
        "-c",
        'model_providers.fcc.base_url="http://127.0.0.1:8082/v1"',
        "-c",
        'model_providers.fcc.env_key="FCC_CODEX_API_KEY"',
        "-c",
        'model_providers.fcc.wire_api="responses"',
    ]
    env = build_codex_launcher_env(
        auth_token="token",
        base_env={"OPENAI_API_KEY": "x", "CODEX_HOME": "keep", "PATH": "p"},
    )
    assert env == {"CODEX_HOME": "keep", "PATH": "p", "FCC_CODEX_API_KEY": "token"}


def test_pi_spec_builds_the_same_command_and_env_as_before() -> None:
    """T3: Pi's argv, its FCC-only env and its passthrough list are unchanged."""

    spec = spec_for("pi")
    assert spec.binary == "pi"
    assert spec.display_name == "Pi"
    assert spec.identity_help_markers == ("--extension", "--models")
    assert pi_install_hint("linux") == (
        "Install Pi with: curl -fsSL https://pi.dev/install.sh | sh"
    )
    assert pi_install_hint("win32") == (
        'Install Pi with: powershell -c "irm https://pi.dev/install.ps1 | iex"'
    )
    assert build_pi_launcher_command(
        binary_path="pi.exe", extension_path=Path("ext.ts"), argv=["hello"]
    ) == ["pi.exe", "-e", "ext.ts", "--models", "free-claude-code/**", "hello"]
    env = build_pi_launcher_env(
        proxy_root_url="http://127.0.0.1:8082/",
        auth_token="token",
        base_env={"FCC_PI_STALE": "x", "PATH": "p"},
    )
    assert env == {
        "PATH": "p",
        "FCC_PI_BASE_URL": "http://127.0.0.1:8082",
        "FCC_PI_API_KEY": "token",
    }
    for command in ("config", "install", "list", "remove", "uninstall", "update"):
        assert is_pi_passthrough([command])
    for flag in ("--help", "-h", "--version", "-v"):
        assert is_pi_passthrough([flag])
    assert not is_pi_passthrough(["chat"])


# ------------------------------------------------------------ never install a CLI


@pytest.mark.parametrize("harness_id", harness_ids())
def test_missing_binary_exits_127_with_install_hint(
    harness_id: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """MCC never installs a third-party CLI; it prints the vendor's own line.

    Bundling agents broke installs once. The rule is enforced in exactly one
    place -- ``resolve_client_binary`` -- and this test pins the exit code and
    the hint for every registered harness so a new one cannot quietly acquire
    a download step.
    """

    spec = spec_for(harness_id)
    with (
        patch("my_claude_code.cli.launchers.common.shutil.which", return_value=None),
        pytest.raises(SystemExit) as exc_info,
    ):
        resolve_harness_binary(spec)

    assert exc_info.value.code == 127
    error_output = capsys.readouterr().err
    assert f"Could not find {spec.display_name} command: {spec.binary}" in error_output
    assert install_hint(spec) in error_output


# --------------------------------------------------- generated user surfaces


def _installer_sources() -> dict[str, str]:
    return {
        name: (REPO_ROOT / "scripts" / name).read_text(encoding="utf-8")
        for name in ("install.sh", "install.ps1", "uninstall.sh", "uninstall.ps1")
    }


def test_installers_verify_every_registered_harness_command() -> None:
    """A shim the installer never checks is a shim nobody notices is missing.

    Both installers verify their command list after install and both
    uninstallers remove it again. A harness added to the registry without an
    entry in all four ships a console script that is created, never verified,
    and left behind on uninstall.
    """

    missing: list[str] = []
    for command in harness_commands():
        for name, source in _installer_sources().items():
            if command.command not in source:
                missing.append(f"{name}: {command.command}")
    assert missing == []


def test_the_generated_help_lists_every_registered_harness_command() -> None:
    from my_claude_code.cli.entrypoints import _help_text

    text = _help_text()
    for command in harness_commands():
        assert command.command in text, command.command


def test_every_command_line_carries_a_sentence_of_its_own() -> None:
    """The dashboard renders these verbatim; an empty help is a blank row."""

    for spec in harness_specs():
        lines = harness_command_lines(spec)
        assert lines, spec.id
        for line in lines:
            assert line.help_text, f"{spec.id}: {line.command}"
            assert line.kind in ("primary", "flag", "legacy", "rtk")
        assert lines[0].command == spec.command


def test_only_harnesses_that_shipped_before_the_registry_have_an_fcc_alias() -> None:
    """No install in the world carries an ``fcc-opencode``."""

    with_alias = {
        spec.id
        for spec in harness_specs()
        for command in spec.commands
        if command.legacy_command is not None
    }
    assert with_alias == {"claude", "codex", "pi"}


def test_a_config_owning_harness_names_the_variable_it_is_pointed_with() -> None:
    """MCC owns a file only where the CLI documents a way to be handed one.

    Without such a variable the only way to configure these CLIs would be to
    edit the user's own document, which this project does not do.
    """

    by_id = {
        spec.id: spec.catalogue.config_env_var
        for spec in catalogue_specs()
        if spec.catalogue is not None
    }
    assert by_id == {
        "codex": None,
        "pi": None,
        "opencode": "OPENCODE_CONFIG",
        "opencode2": "OPENCODE_CONFIG",
        "kilo": "KILO_CONFIG",
    }
