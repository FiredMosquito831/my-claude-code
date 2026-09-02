"""How ``mcc-qwen`` hands Qwen Code a provider list without touching ~/.qwen.

Two decisions make this launcher different from the others and both are
asserted here. The auth type is selected on the command line rather than by
environment or settings, because that is the only source Qwen ranks above a
value the user saved. And the base URL written into the generated document is
the proxy *root*: Qwen reaches MCC through the official Anthropic SDK, which
appends ``/v1/messages`` itself.
"""

import json
from pathlib import Path

from my_claude_code.application.catalogue_model import CatalogueModel
from my_claude_code.application.catalogues import serialise
from my_claude_code.cli.launchers import qwen
from my_claude_code.config.harness_base_url import with_root_base_url
from my_claude_code.config.harnesses import (
    QWEN_API_KEY_ENV,
    QWEN_BASE_URL_SENTINEL,
    harness_spec,
)
from my_claude_code.config.proxy_auth import proxy_auth_token

SPEC = harness_spec("qwen_code")


def _model() -> CatalogueModel:
    return CatalogueModel(
        gateway_id="anthropic/openrouter/sonnet",
        provider_model_ref="openrouter/sonnet",
        display_name="openrouter/sonnet",
        context_length=200_000,
        max_output_tokens=64_000,
    )


def test_the_auth_type_flag_precedes_the_users_arguments() -> None:
    """``argv.authType`` is the only source that outranks a saved choice.

    ``loadCliConfig`` resolves ``argv.authType || settings.security.auth
    .selectedType || getAuthTypeFromEnv()``, so a user who has ever picked an
    auth type in Qwen's UI would silently outrank MCC's environment. The flag
    leads because yargs binds root options ahead of a subcommand; the user's
    own arguments follow unchanged, and their own ``--auth-type`` still wins
    because yargs keeps the last occurrence.
    """

    command = qwen.build_qwen_command(
        "/bin/qwen", Path("/home/u/.fcc/qwen-code-settings.json"), ["-p", "hello"]
    )

    assert command == ["/bin/qwen", "--auth-type", "anthropic", "-p", "hello"]


def test_no_config_means_no_auth_type_flag_either() -> None:
    """A failed refresh degrades to a plain launch, never to a broken one.

    Telling Qwen to authenticate as ``anthropic`` while giving it no Anthropic
    provider would turn a degraded launch into one that cannot start.
    """

    command = qwen.build_qwen_command("/bin/qwen", None, ["-p", "hello"])

    assert command == ["/bin/qwen", "-p", "hello"]


def test_maintenance_subcommands_reach_the_cli_untouched() -> None:
    assert qwen.is_passthrough(SPEC, ["mcp"]) is True
    assert qwen.is_passthrough(SPEC, ["extensions", "list"]) is True
    assert qwen.is_passthrough(SPEC, ["--version"]) is True
    assert qwen.is_passthrough(SPEC, ["-v"]) is True
    assert qwen.is_passthrough(SPEC, []) is False
    assert qwen.is_passthrough(SPEC, ["-p", "hello"]) is False
    # ``review`` runs an agent, so it needs MCC's provider.
    assert qwen.is_passthrough(SPEC, ["review"]) is False


def test_the_environment_names_the_settings_file_and_the_key_variable() -> None:
    env = qwen.build_qwen_launcher_env(
        spec=SPEC,
        config_path=Path("/home/u/.fcc/qwen-code-settings.json"),
        auth_token="secret-token",
        base_env={"PATH": "/usr/bin"},
    )

    assert env["QWEN_CODE_SYSTEM_SETTINGS_PATH"] == str(
        Path("/home/u/.fcc/qwen-code-settings.json")
    )
    assert env[QWEN_API_KEY_ENV] == proxy_auth_token("secret-token")
    assert env["PATH"] == "/usr/bin"


def test_inherited_anthropic_variables_are_stripped() -> None:
    """A leftover ANTHROPIC_BASE_URL must not become this session's base URL.

    ``AUTH_ENV_MAPPINGS.anthropic`` reads all three of these, and
    ``getAuthTypeFromEnv`` infers an auth type from them, so a value inherited
    from a parent shell would quietly outrank nothing at all -- it would
    simply be used.
    """

    env = qwen.build_qwen_launcher_env(
        spec=SPEC,
        config_path=Path("/home/u/.fcc/qwen-code-settings.json"),
        auth_token="secret-token",
        base_env={
            "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
            "ANTHROPIC_API_KEY": "someone-elses",
            "ANTHROPIC_AUTH_TOKEN": "someone-elses",
            "ANTHROPIC_MODEL": "claude-3",
            QWEN_API_KEY_ENV: "stale",
        },
    )

    assert "ANTHROPIC_BASE_URL" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert "ANTHROPIC_MODEL" not in env
    assert env[QWEN_API_KEY_ENV] == proxy_auth_token("secret-token")


def test_no_config_leaves_the_environment_alone_apart_from_the_strip() -> None:
    env = qwen.build_qwen_launcher_env(
        spec=SPEC,
        config_path=None,
        auth_token="secret-token",
        base_env={"PATH": "/usr/bin"},
    )

    assert env == {"PATH": "/usr/bin"}


def test_the_written_document_is_json_qwen_can_load(tmp_path: Path) -> None:
    """End to end over the real serialiser and the real substitution."""

    document, _ = serialise("qwen", [_model()])
    resolved = with_root_base_url(
        document, QWEN_BASE_URL_SENTINEL, "http://127.0.0.1:8199"
    )
    path = tmp_path / "qwen-code-settings.json"
    path.write_text(json.dumps(resolved), encoding="utf-8")

    loaded = json.loads(path.read_text(encoding="utf-8"))
    entry = loaded["modelProviders"]["anthropic"][0]
    # The proxy root, with no /v1: the official Anthropic SDK appends
    # /v1/messages itself and a trailing /v1 would produce /v1/v1/messages.
    assert entry["baseUrl"] == "http://127.0.0.1:8199"
    assert entry["envKey"] == QWEN_API_KEY_ENV
    assert entry["generationConfig"]["contextWindowSize"] == 200_000


def test_a_base_url_that_already_ends_in_v1_is_not_doubled() -> None:
    document, _ = serialise("qwen", [_model()])

    resolved = with_root_base_url(
        document, QWEN_BASE_URL_SENTINEL, "http://127.0.0.1:8199/v1"
    )

    entry = resolved["modelProviders"]["anthropic"][0]
    assert entry["baseUrl"] == "http://127.0.0.1:8199"
