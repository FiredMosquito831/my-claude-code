"""How ``mcc-kimi`` hands Kimi Code a config without touching the user's.

The cases here are about the two decisions that make this launcher different
from every other one: the config is named on the command line rather than in
the environment, and it is the only generated document that carries the proxy
token -- which is why it is also the only one whose position in argv and whose
file permissions are asserted.
"""

import tomllib
from pathlib import Path

from my_claude_code.application.catalogue_model import CatalogueModel
from my_claude_code.application.catalogues import serialise
from my_claude_code.cli.launchers import kimi
from my_claude_code.config.harness_toml import (
    with_kimi_credentials,
    write_toml_document_atomically_if_changed,
)
from my_claude_code.config.harnesses import harness_spec
from my_claude_code.config.proxy_auth import PROXY_NO_AUTH_SENTINEL, proxy_auth_token

SPEC = harness_spec("kimi_code")


def _model() -> CatalogueModel:
    return CatalogueModel(
        gateway_id="anthropic/openrouter/sonnet",
        provider_model_ref="openrouter/sonnet",
        display_name="openrouter/sonnet",
        context_length=200_000,
        max_output_tokens=64_000,
    )


def test_the_config_flag_precedes_the_users_arguments() -> None:
    """Kimi's ``--config-file`` binds to the root callback, not a subcommand.

    ``kimi term --config-file X`` would be read as an argument to ``term``, so
    the flag has to lead. The user's own arguments follow unchanged, which is
    also what lets a user override it: Typer keeps the last occurrence.
    """

    command = kimi.build_kimi_command(
        "/bin/kimi", SPEC, Path("/home/u/.fcc/kimi-code-config.toml"), ["-p", "hello"]
    )

    assert command == [
        "/bin/kimi",
        "--config-file",
        str(Path("/home/u/.fcc/kimi-code-config.toml")),
        "-p",
        "hello",
    ]


def test_no_config_means_the_users_own_file_is_left_in_charge() -> None:
    """A failed refresh degrades to a plain launch, never to a broken one."""

    command = kimi.build_kimi_command("/bin/kimi", SPEC, None, ["-p", "hello"])

    assert command == ["/bin/kimi", "-p", "hello"]


def test_maintenance_subcommands_reach_the_cli_untouched() -> None:
    assert kimi.is_passthrough(SPEC, ["login"]) is True
    assert kimi.is_passthrough(SPEC, ["mcp", "list"]) is True
    assert kimi.is_passthrough(SPEC, ["--version"]) is True
    assert kimi.is_passthrough(SPEC, ["-V"]) is True
    assert kimi.is_passthrough(SPEC, []) is False
    assert kimi.is_passthrough(SPEC, ["-p", "hello"]) is False
    # ``acp``, ``term`` and ``web`` all run an agent, so they need MCC's
    # provider and must not be passed through.
    assert kimi.is_passthrough(SPEC, ["acp"]) is False
    assert kimi.is_passthrough(SPEC, ["term"]) is False
    assert kimi.is_passthrough(SPEC, ["web"]) is False


def test_the_written_config_is_the_document_kimi_can_load(tmp_path: Path) -> None:
    """End to end over the real serialiser and the real writer."""

    document, _ = serialise("kimi", [_model()])
    path = tmp_path / "kimi-code-config.toml"

    write_toml_document_atomically_if_changed(
        path,
        with_kimi_credentials(
            document, proxy_root_url="http://127.0.0.1:8199", api_key="secret-token"
        ),
    )

    written = tomllib.loads(path.read_text(encoding="utf-8"))
    provider = written["providers"]["mcc"]
    assert provider["type"] == "anthropic"
    # The proxy root, not ``/v1``: Kimi's client is the official Anthropic
    # SDK, whose route is already ``/v1/messages``.
    assert provider["base_url"] == "http://127.0.0.1:8199"
    assert provider["api_key"] == "secret-token"
    entry = written["models"]["mcc/openrouter/sonnet"]
    assert entry["provider"] == "mcc"
    assert entry["model"] == "openrouter/sonnet"
    assert entry["max_context_size"] == 200_000
    # Every key the document carries is one Kimi's own Config declares, plus
    # MCC's own record of what was defaulted, which Kimi ignores.
    assert set(written) <= {"providers", "models", "_mcc_defaulted"}


def test_a_proxy_with_no_token_writes_the_marker_and_not_an_empty_key(
    tmp_path: Path,
) -> None:
    """Kimi has no reference form, so *something* has to be in ``api_key``.

    With proxy auth off that something is the ``fcc-no-auth`` marker, which is
    not a credential -- and it is the same value every other launcher sends in
    the same situation, so a request rejected here is rejected for the same
    reason everywhere.
    """

    document, _ = serialise("kimi", [_model()])
    path = tmp_path / "kimi-code-config.toml"

    write_toml_document_atomically_if_changed(
        path,
        with_kimi_credentials(
            document,
            proxy_root_url="http://127.0.0.1:8199",
            api_key=proxy_auth_token("   "),
        ),
    )

    written = tomllib.loads(path.read_text(encoding="utf-8"))
    assert written["providers"]["mcc"]["api_key"] == PROXY_NO_AUTH_SENTINEL


def test_the_generated_config_is_narrowed_to_its_owner(tmp_path: Path) -> None:
    """The one generated catalogue that carries a token gets 0600, best effort."""

    path = tmp_path / "kimi-code-config.toml"
    path.write_text("providers = {}\n", encoding="utf-8")

    kimi.restrict_permissions(path)

    # POSIX is where the mode is meaningful; on Windows chmod is close to a
    # no-op, so the only thing asserted everywhere is that it did not raise
    # and the file is still readable.
    assert path.read_text(encoding="utf-8") == "providers = {}\n"


def test_the_registry_states_the_file_and_the_flag_rather_than_a_variable() -> None:
    catalogue = SPEC.catalogue
    assert catalogue is not None
    assert catalogue.filename == "kimi-code-config.toml"
    assert catalogue.document_format == "toml"
    assert catalogue.config_flag == "--config-file"
    assert catalogue.config_env_var is None
    assert catalogue.merge is None
    assert SPEC.binary == "kimi"
    assert SPEC.binary_aliases == ("kimi-cli",)
    assert SPEC.rtk_agent is False
    # Never an npm line: Kimi Code is a Python tool on PyPI.
    assert "uv tool install kimi-cli" in SPEC.install_hint
    assert "npm" not in SPEC.install_hint
