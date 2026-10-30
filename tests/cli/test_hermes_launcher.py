"""``mcc-hermes``: the token stays in the child, the config.yaml stays the user's.

The launcher is Hermes' explicit Configure, so these tests are about what it
writes, what it refuses to write, and what its three MCC-only flags do --
``--models``, ``--disconnect`` and ``--restore``. The merge engine itself is
covered in ``tests/config/test_harness_config_merge_hermes.py``.
"""

from pathlib import Path
from typing import Any

from my_claude_code.application.catalogue_model import CatalogueModel
from my_claude_code.application.catalogues import serialise
from my_claude_code.cli.launchers import hermes
from my_claude_code.config.document_codecs import yaml_mapping_at
from my_claude_code.config.harnesses import HERMES_API_KEY_ENV, harness_spec
from my_claude_code.config.proxy_auth import PROXY_NO_AUTH_SENTINEL
from my_claude_code.config.restore_record import UndoMode

SPEC = harness_spec("hermes")
ROOT = "http://127.0.0.1:8082"

SEED = (
    "# my hermes config\n"
    "_config_version: 33\n"
    "model:\n"
    "  provider: openrouter\n"
    "  default: anthropic/claude-sonnet-4\n"
)


def _payload(model_count: int = 2) -> dict[str, Any]:
    document, defaulted = serialise(
        "hermes",
        [
            CatalogueModel(
                gateway_id="anthropic/openrouter/configured",
                provider_model_ref="openrouter/configured",
                display_name="Configured",
                context_length=400_000,
                is_primary_route=True,
            ),
            CatalogueModel(
                gateway_id="anthropic/openrouter/other",
                provider_model_ref="openrouter/other",
                display_name="Other",
            ),
        ],
    )
    return {
        "catalogues": {
            "hermes": {
                "document": document,
                "model_count": model_count,
                "defaulted": defaulted.as_document(),
            }
        }
    }


def _map(value: object) -> dict[str, Any]:
    """Narrow one nested value of a parsed YAML document for an assertion."""

    assert isinstance(value, dict)
    return {str(key): item for key, item in value.items()}


def _seed(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(SEED, encoding="utf-8", newline="")
    return path


def _patch(monkeypatch, path: Path, payload: dict[str, Any] | None = None) -> None:
    """Point the launcher at a scratch document, record and canned catalogue."""

    canned = payload if payload is not None else _payload()
    monkeypatch.setattr(hermes, "fetch_catalogue_models", lambda *_a, **_k: canned)
    monkeypatch.setattr(hermes, "config_path_for", lambda _spec, _env=None: path)
    monkeypatch.setattr(
        "my_claude_code.config.restore_record.restore_record_path",
        lambda: path.with_name("restore-record.json"),
    )
    monkeypatch.setattr(hermes, "get_settings", lambda: object())
    monkeypatch.setattr(hermes, "local_proxy_root_url", lambda _settings: ROOT)


def test_the_launched_environment_carries_the_token_and_nothing_stale() -> None:
    env = hermes.build_hermes_launcher_env(
        merged=True,
        auth_token="secret-token",
        base_env={"PATH": "p", HERMES_API_KEY_ENV: "stale"},
    )

    assert env["PATH"] == "p"
    assert env[HERMES_API_KEY_ENV] == "secret-token"


def test_a_proxy_with_no_token_still_gets_a_value_the_name_can_resolve() -> None:
    env = hermes.build_hermes_launcher_env(merged=True, auth_token="   ", base_env={})

    assert env[HERMES_API_KEY_ENV] == PROXY_NO_AUTH_SENTINEL


def test_a_failed_merge_leaves_no_mcc_variables_behind() -> None:
    env = hermes.build_hermes_launcher_env(
        merged=False,
        auth_token="secret-token",
        base_env={"PATH": "p", HERMES_API_KEY_ENV: "stale"},
    )

    assert HERMES_API_KEY_ENV not in env


def test_maintenance_subcommands_reach_the_cli_untouched() -> None:
    assert hermes.is_passthrough(SPEC, ["config", "get", "model"]) is True
    assert hermes.is_passthrough(SPEC, ["--version"]) is True
    assert hermes.is_passthrough(SPEC, ["sessions", "list"]) is True
    assert hermes.is_passthrough(SPEC, []) is False
    # A picker and a session both need MCC's provider written first.
    assert hermes.is_passthrough(SPEC, ["model"]) is False
    assert hermes.is_passthrough(SPEC, ["chat"]) is False


def test_the_config_path_follows_hermes_own_home() -> None:
    assert hermes.config_path_for(SPEC, {"HERMES_HOME": "D:/h"}).name == "config.yaml"
    assert (
        str(hermes.config_path_for(SPEC, {"HERMES_HOME": "D:/h"}))
        .replace("\\", "/")
        .endswith("D:/h/config.yaml")
    )


def test_the_written_block_never_holds_the_token(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    path = _seed(tmp_path)
    _patch(monkeypatch, path)

    assert hermes.write_provider_block(SPEC, ROOT, "secret-token") is True

    text = path.read_bytes().decode("utf-8")
    assert "secret-token" not in text
    assert "Bearer" not in text
    assert "key_env: MCC_HERMES_API_KEY" in text
    assert "# my hermes config" in text

    block = yaml_mapping_at(text, ("providers", "mcc"))
    assert block is not None
    assert block["api"] == f"{ROOT}/v1"
    assert block["transport"] == "openai_chat"
    assert block["default_model"] == "openrouter/configured"
    assert list(_map(block["models"])) == [
        "openrouter/configured",
        "openrouter/other",
    ]
    err = capsys.readouterr().err
    assert "Updated providers.mcc" in err
    assert "context_length" in err


def test_disconnect_puts_the_users_own_file_back(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    path = _seed(tmp_path)
    _patch(monkeypatch, path)
    hermes.write_provider_block(SPEC, ROOT, "secret-token")
    capsys.readouterr()

    assert hermes.disconnect(SPEC, UndoMode.KEYS_ONLY) == 0

    assert path.read_bytes().decode("utf-8") == SEED
    assert "Removed providers.mcc" in capsys.readouterr().out


def test_disconnect_without_a_record_is_honest_about_it(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Hermes rewrites its own file, so a record may not survive; say so."""

    path = _seed(tmp_path)
    _patch(monkeypatch, path)

    assert hermes.disconnect(SPEC, UndoMode.KEYS_ONLY) == 0

    assert "carries nothing of MCC's; nothing to remove" in capsys.readouterr().out
    assert path.read_bytes().decode("utf-8") == SEED


def test_restore_refuses_when_the_file_moved_and_says_so(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    path = _seed(tmp_path)
    _patch(monkeypatch, path)
    hermes.write_provider_block(SPEC, ROOT, "secret-token")
    capsys.readouterr()
    path.write_text(
        path.read_text(encoding="utf-8").replace("openrouter", "nous"),
        encoding="utf-8",
        newline="",
    )

    assert hermes.disconnect(SPEC, UndoMode.RESTORE) == 1

    assert "has changed since MCC configured it" in capsys.readouterr().err


def test_an_empty_catalogue_never_writes_an_empty_provider(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    path = _seed(tmp_path)
    _patch(monkeypatch, path, _payload(model_count=0))

    assert hermes.write_provider_block(SPEC, ROOT, "secret-token") is False

    assert path.read_bytes().decode("utf-8") == SEED
    assert "launching without an MCC provider" in capsys.readouterr().err


def test_a_yaml_list_document_is_left_alone(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("- id: something\n", encoding="utf-8")
    _patch(monkeypatch, path)

    assert hermes.write_provider_block(SPEC, ROOT, "secret-token") is False

    assert path.read_text(encoding="utf-8") == "- id: something\n"
    err = capsys.readouterr().err
    assert "left" in err
    assert "untouched" in err


def test_list_models_reads_the_users_own_document(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    path = _seed(tmp_path)
    _patch(monkeypatch, path)
    hermes.write_provider_block(SPEC, ROOT, "secret-token")
    capsys.readouterr()

    assert hermes.list_models(SPEC) == 0

    out = capsys.readouterr().out
    assert "openrouter/configured (400000 tokens)" in out
    assert "openrouter/other" in out


def test_list_models_says_so_when_there_is_nothing_to_list(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    path = _seed(tmp_path)
    _patch(monkeypatch, path)

    assert hermes.list_models(SPEC) == 0
    assert "carries no providers.mcc models" in capsys.readouterr().out


def test_list_models_says_so_when_the_file_is_missing(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _patch(monkeypatch, tmp_path / "config.yaml")

    assert hermes.list_models(SPEC) == 1
    assert "no Hermes Agent config" in capsys.readouterr().err


def test_the_flags_are_names_hermes_does_not_use() -> None:
    """MCC's flags must not shadow a flag the vendor documents."""

    asserted = {
        hermes.MODELS_FLAG,
        hermes.DISCONNECT_FLAG,
        hermes.RESTORE_FLAG,
    }
    assert asserted == {"--models", "--disconnect", "--restore"}
    # Hermes' own top-level flags, from hermes_cli/_parser.py of 0.19.0.
    assert not asserted & {
        "--help",
        "-h",
        "--version",
        "-V",
        "--oneshot",
        "-z",
        "--model",
        "-m",
        "--provider",
        "--resume",
        "-r",
        "--continue",
        "-c",
        "--tui",
        "--cli",
        "--profile",
        "-p",
    }
