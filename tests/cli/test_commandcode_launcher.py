"""Command Code is served without the proxy token ever reaching disk.

This is the only launcher that edits a file the user owns, so the cases here
are about restraint: what reaches the document, what reaches the environment,
what a passthrough subcommand skips, and what ``--disconnect`` takes back.
"""

import json
from pathlib import Path
from typing import Any

from my_claude_code.application.catalogue_model import CatalogueModel
from my_claude_code.application.catalogues import serialise
from my_claude_code.cli.launchers import commandcode
from my_claude_code.config.harness_config_merge import (
    merge_owned_block,
    owned_block,
    with_base_url,
)
from my_claude_code.config.harnesses import (
    COMMANDCODE_API_KEY_ENV,
    harness_spec,
)
from my_claude_code.config.proxy_auth import PROXY_NO_AUTH_SENTINEL

SPEC = harness_spec("commandcode_cli")


def _model() -> CatalogueModel:
    return CatalogueModel(
        gateway_id="anthropic/openrouter/sonnet",
        provider_model_ref="openrouter/sonnet",
        display_name="openrouter/sonnet",
        context_length=200_000,
        max_output_tokens=64_000,
    )


def test_the_launched_environment_carries_the_token_and_nothing_stale() -> None:
    env = commandcode.build_commandcode_launcher_env(
        merged=True,
        auth_token="secret-token",
        base_env={"PATH": "p", COMMANDCODE_API_KEY_ENV: "stale"},
    )

    assert env["PATH"] == "p"
    assert env[COMMANDCODE_API_KEY_ENV] == "secret-token"


def test_a_proxy_with_no_token_still_gets_a_value_the_reference_can_expand() -> None:
    """Command Code throws when a "$VAR" reference names an unset variable."""

    env = commandcode.build_commandcode_launcher_env(
        merged=True, auth_token="   ", base_env={}
    )

    assert env[COMMANDCODE_API_KEY_ENV] == PROXY_NO_AUTH_SENTINEL


def test_a_failed_merge_leaves_no_mcc_variables_behind() -> None:
    env = commandcode.build_commandcode_launcher_env(
        merged=False,
        auth_token="secret-token",
        base_env={"PATH": "p", COMMANDCODE_API_KEY_ENV: "stale"},
    )

    assert COMMANDCODE_API_KEY_ENV not in env


def test_maintenance_subcommands_reach_the_cli_untouched() -> None:
    assert commandcode.is_passthrough(SPEC, ["update"]) is True
    assert commandcode.is_passthrough(SPEC, ["--version"]) is True
    assert commandcode.is_passthrough(SPEC, ["mcp", "list"]) is True
    assert commandcode.is_passthrough(SPEC, []) is False
    assert commandcode.is_passthrough(SPEC, ["-p", "hello"]) is False


def test_the_config_path_is_the_one_the_cli_reads(tmp_path: Path) -> None:
    resolved = commandcode.config_path_for(SPEC, {"HOME": str(tmp_path)})

    assert resolved == tmp_path / ".commandcode" / "providers.json"


def test_the_written_block_names_the_token_and_never_holds_it(
    tmp_path: Path,
) -> None:
    """End to end over the real serialiser: the token is a reference on disk."""

    document, _ = serialise("commandcode", [_model()])
    merge = SPEC.catalogue.merge if SPEC.catalogue is not None else None
    assert merge is not None
    path = tmp_path / ".commandcode" / "providers.json"

    merge_owned_block(
        path=path,
        owned_key_path=merge.owned_key_path,
        block=with_base_url(
            owned_block(document, merge.owned_key_path), "http://127.0.0.1:8199"
        ),
        backup_suffix=merge.backup_suffix,
    )

    raw = path.read_text(encoding="utf-8")
    assert "secret-token" not in raw
    written: dict[str, Any] = json.loads(raw)
    block = written["provider"]["mcc"]
    assert block["apiKey"] == f"${COMMANDCODE_API_KEY_ENV}"
    assert block["baseURL"] == "http://127.0.0.1:8199/v1"
    assert block["api"] == "anthropic-messages"
    assert block["models"]["openrouter/sonnet"]["contextWindow"] == 200_000
    assert block["models"]["openrouter/sonnet"]["maxOutput"] == 64_000
    # Nothing of MCC's outside its one key.
    assert set(written) == {"provider"}


def test_disconnect_reports_when_there_was_nothing_to_remove(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    assert commandcode.disconnect(SPEC) == 0

    assert "nothing to remove" in capsys.readouterr().out


def test_disconnect_removes_the_key_and_says_so(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    path = tmp_path / ".commandcode" / "providers.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {"provider": {"mcc": {"models": {}}, "ollama": {}}, "theme": "dark"}
        ),
        encoding="utf-8",
    )

    assert commandcode.disconnect(SPEC) == 0

    assert "Removed provider.mcc" in capsys.readouterr().out
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after == {"provider": {"ollama": {}}, "theme": "dark"}
