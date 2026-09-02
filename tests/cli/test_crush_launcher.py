"""How ``mcc-crush`` hands Crush a provider without touching ~/.config/crush.

Crush is the one harness whose ``config_env_var`` does not carry the catalogue
path verbatim: ``CRUSH_GLOBAL_CONFIG`` names a *directory* and Crush looks for
``crush.json`` inside it. That, and the fact that the base URL written into the
document is the proxy root rather than ``…/v1``, are what these tests pin.
"""

import json
from pathlib import Path

from my_claude_code.application.catalogue_model import CatalogueModel
from my_claude_code.application.catalogues import serialise
from my_claude_code.cli.launchers import crush
from my_claude_code.config.harness_base_url import with_root_base_url
from my_claude_code.config.harnesses import (
    CRUSH_API_KEY_ENV,
    CRUSH_BASE_URL_SENTINEL,
    harness_spec,
)
from my_claude_code.config.proxy_auth import proxy_auth_token

SPEC = harness_spec("crush")


def _model() -> CatalogueModel:
    return CatalogueModel(
        gateway_id="anthropic/openrouter/sonnet",
        provider_model_ref="openrouter/sonnet",
        display_name="openrouter/sonnet",
        context_length=200_000,
        max_output_tokens=64_000,
    )


def test_maintenance_subcommands_reach_the_cli_untouched() -> None:
    assert crush.is_passthrough(SPEC, ["login"]) is True
    assert crush.is_passthrough(SPEC, ["update-providers"]) is True
    assert crush.is_passthrough(SPEC, ["--version"]) is True
    assert crush.is_passthrough(SPEC, []) is False
    # ``run`` and ``models`` both need MCC's provider to say anything useful,
    # and ``dirs`` should report the directory this launch actually uses.
    assert crush.is_passthrough(SPEC, ["run", "hello"]) is False
    assert crush.is_passthrough(SPEC, ["models"]) is False
    assert crush.is_passthrough(SPEC, ["dirs"]) is False


def test_the_config_variable_names_the_directory_not_the_file() -> None:
    """``crush dirs`` echoes CRUSH_GLOBAL_CONFIG as the config directory.

    Crush then looks for ``crush.json`` inside it, which is why the registry
    spells the filename ``crush/crush.json``: the directory has to be MCC's
    alone or the variable would hand Crush the whole of ``~/.fcc``.
    """

    env = crush.build_crush_launcher_env(
        spec=SPEC,
        config_path=Path("/home/u/.fcc/crush/crush.json"),
        auth_token="secret-token",
        base_env={"PATH": "/usr/bin"},
    )

    assert env["CRUSH_GLOBAL_CONFIG"] == str(Path("/home/u/.fcc/crush"))
    assert env[CRUSH_API_KEY_ENV] == proxy_auth_token("secret-token")
    assert env["PATH"] == "/usr/bin"


def test_an_inherited_config_variable_is_stripped() -> None:
    env = crush.build_crush_launcher_env(
        spec=SPEC,
        config_path=Path("/home/u/.fcc/crush/crush.json"),
        auth_token="secret-token",
        base_env={
            "CRUSH_GLOBAL_CONFIG": "/somewhere/else",
            CRUSH_API_KEY_ENV: "stale",
        },
    )

    assert env["CRUSH_GLOBAL_CONFIG"] == str(Path("/home/u/.fcc/crush"))
    assert env[CRUSH_API_KEY_ENV] == proxy_auth_token("secret-token")


def test_no_config_leaves_the_environment_alone_apart_from_the_strip() -> None:
    env = crush.build_crush_launcher_env(
        spec=SPEC,
        config_path=None,
        auth_token="secret-token",
        base_env={"PATH": "/usr/bin", "CRUSH_GLOBAL_CONFIG": "/somewhere/else"},
    )

    assert env == {"PATH": "/usr/bin"}


def test_the_written_document_is_json_crush_can_load(tmp_path: Path) -> None:
    """End to end over the real serialiser and the real substitution."""

    document, _ = serialise("crush", [_model()])
    resolved = with_root_base_url(
        document, CRUSH_BASE_URL_SENTINEL, "http://127.0.0.1:8199"
    )
    path = tmp_path / "crush.json"
    path.write_text(json.dumps(resolved), encoding="utf-8")

    loaded = json.loads(path.read_text(encoding="utf-8"))
    provider = loaded["providers"]["mcc"]
    # The proxy root, with no /v1: anthropic-sdk-go appends /v1/messages
    # itself and a trailing /v1 would produce /v1/v1/messages.
    assert provider["base_url"] == "http://127.0.0.1:8199"
    # The token stays in the environment; only its variable name is on disk.
    assert provider["api_key"] == f"${CRUSH_API_KEY_ENV}"
    assert provider["models"][0]["context_window"] == 200_000
    assert loaded["models"]["large"]["provider"] == "mcc"


def test_a_base_url_that_already_ends_in_v1_is_not_doubled() -> None:
    document, _ = serialise("crush", [_model()])

    resolved = with_root_base_url(
        document, CRUSH_BASE_URL_SENTINEL, "http://127.0.0.1:8199/v1/"
    )

    assert resolved["providers"]["mcc"]["base_url"] == "http://127.0.0.1:8199"
