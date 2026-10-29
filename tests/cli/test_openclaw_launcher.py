"""``mcc-openclaw``: the token stays in the child, the file stays the user's.

The launcher is OpenClaw's explicit Configure, so these tests are about what it
writes, what it refuses to write, and what its three MCC-only flags do --
``--models``, ``--disconnect`` and ``--restore``. The merge engine itself is
covered in ``tests/config/test_harness_config_merge_openclaw.py``.
"""

import json
from pathlib import Path
from typing import Any

from my_claude_code.application.catalogue_model import CatalogueModel
from my_claude_code.application.catalogues import serialise
from my_claude_code.cli.launchers import openclaw
from my_claude_code.config.harnesses import OPENCLAW_API_KEY_ENV, harness_spec
from my_claude_code.config.proxy_auth import PROXY_NO_AUTH_SENTINEL
from my_claude_code.config.restore_record import UndoMode

SPEC = harness_spec("openclaw")
ROOT = "http://127.0.0.1:8082"


def _payload(model_count: int = 2) -> dict[str, Any]:
    document, defaulted = serialise(
        "openclaw",
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
            "openclaw": {
                "document": document,
                "model_count": model_count,
                "defaulted": defaulted.as_document(),
            }
        }
    }


SEED = (
    "// hand-written\n"
    "{\n"
    '  "channels": {"telegram": {"token": "x"}},\n'
    '  "agents": {"defaults": {"model": "openai/gpt-5"}},\n'
    '  "theme": "dark"\n'
    "}\n"
)


def _seed(tmp_path: Path) -> Path:
    path = tmp_path / "openclaw.json"
    path.write_text(SEED, encoding="utf-8", newline="")
    return path


def _patch(monkeypatch, path: Path, payload: dict[str, Any] | None = None) -> None:
    """Point the launcher at a scratch document, record and canned catalogue."""

    canned = payload if payload is not None else _payload()
    monkeypatch.setattr(openclaw, "fetch_catalogue_models", lambda *_a, **_k: canned)
    monkeypatch.setattr(openclaw, "config_path_for", lambda _spec, _env=None: path)
    # The record lives beside the document here rather than in the real
    # ``~/.mcc``: an xdist worker and the developer's own install must never
    # share one subject.
    monkeypatch.setattr(
        "my_claude_code.config.restore_record.restore_record_path",
        lambda: path.with_name("restore-record.json"),
    )
    # The ownership check asks this server's own address; a canned settings
    # object keeps the test away from the real settings file.
    monkeypatch.setattr(openclaw, "get_settings", lambda: object())
    monkeypatch.setattr(openclaw, "local_proxy_root_url", lambda _settings: ROOT)


def test_the_launched_environment_carries_the_token_and_nothing_stale() -> None:
    env = openclaw.build_openclaw_launcher_env(
        merged=True,
        auth_token="secret-token",
        base_env={"PATH": "p", OPENCLAW_API_KEY_ENV: "stale"},
    )

    assert env["PATH"] == "p"
    assert env[OPENCLAW_API_KEY_ENV] == "secret-token"


def test_a_proxy_with_no_token_still_gets_a_value_the_reference_can_expand() -> None:
    env = openclaw.build_openclaw_launcher_env(
        merged=True, auth_token="   ", base_env={}
    )

    assert env[OPENCLAW_API_KEY_ENV] == PROXY_NO_AUTH_SENTINEL


def test_a_failed_merge_leaves_no_mcc_variables_behind() -> None:
    env = openclaw.build_openclaw_launcher_env(
        merged=False,
        auth_token="secret-token",
        base_env={"PATH": "p", OPENCLAW_API_KEY_ENV: "stale"},
    )

    assert OPENCLAW_API_KEY_ENV not in env


def test_maintenance_subcommands_reach_the_cli_untouched() -> None:
    assert openclaw.is_passthrough(SPEC, ["config", "get", "models"]) is True
    assert openclaw.is_passthrough(SPEC, ["--version"]) is True
    assert openclaw.is_passthrough(SPEC, ["doctor"]) is True
    assert openclaw.is_passthrough(SPEC, []) is False
    assert openclaw.is_passthrough(SPEC, ["-m", "mcc/openrouter/other"]) is False


def test_the_config_path_is_the_one_the_cli_reads() -> None:
    resolved = openclaw.config_path_for(SPEC, {"HOME": str(Path("C:/u/ada"))})

    assert resolved == Path("C:/u/ada/.openclaw/openclaw.json")


def test_the_written_block_never_holds_the_token(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    path = _seed(tmp_path)
    _patch(monkeypatch, path)

    assert openclaw.write_provider_block(SPEC, ROOT, "secret-token") is True

    text = path.read_bytes().decode("utf-8")
    assert "secret-token" not in text
    assert "$MCC_OPENCLAW_API_KEY" in text
    assert "// hand-written" in text
    document = json.loads(text.replace("// hand-written", ""))
    assert document["models"]["providers"]["mcc"]["baseUrl"] == f"{ROOT}/v1"
    assert [
        entry["id"] for entry in document["models"]["providers"]["mcc"]["models"]
    ] == ["openrouter/configured", "openrouter/other"]
    assert document["agents"]["defaults"]["model"] == {
        "primary": "openrouter/configured"
    }
    assert "Updated models.providers.mcc" in capsys.readouterr().err


def test_disconnect_puts_the_users_own_file_back(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    path = _seed(tmp_path)
    _patch(monkeypatch, path)
    openclaw.write_provider_block(SPEC, ROOT, "secret-token")

    assert openclaw.disconnect(SPEC, UndoMode.KEYS_ONLY) == 0

    assert path.read_bytes().decode("utf-8") == SEED
    assert "Removed models.providers.mcc" in capsys.readouterr().out


def test_restore_refuses_when_the_file_moved_and_says_so(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    path = _seed(tmp_path)
    _patch(monkeypatch, path)
    openclaw.write_provider_block(SPEC, ROOT, "secret-token")
    path.write_text(
        path.read_text(encoding="utf-8").replace('"dark"', '"light"'),
        encoding="utf-8",
        newline="",
    )

    assert openclaw.disconnect(SPEC, UndoMode.RESTORE) == 1

    assert "has changed since MCC configured it" in capsys.readouterr().err


def test_an_empty_catalogue_never_writes_an_empty_provider(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    path = _seed(tmp_path)
    _patch(monkeypatch, path, _payload(model_count=0))

    assert openclaw.write_provider_block(SPEC, ROOT, "secret-token") is False

    assert path.read_bytes().decode("utf-8") == SEED
    assert "launching without an MCC provider" in capsys.readouterr().err


def test_a_document_mcc_cannot_parse_is_left_alone(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    path = tmp_path / "openclaw.json"
    path.write_text("{ not json", encoding="utf-8")
    _patch(monkeypatch, path)

    assert openclaw.write_provider_block(SPEC, ROOT, "secret-token") is False

    assert path.read_text(encoding="utf-8") == "{ not json"
    err = capsys.readouterr().err
    assert "left" in err
    assert "untouched" in err


def test_list_models_reads_the_users_own_document(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    path = _seed(tmp_path)
    _patch(monkeypatch, path)
    openclaw.write_provider_block(SPEC, ROOT, "secret-token")
    capsys.readouterr()

    assert openclaw.list_models(SPEC) == 0

    out = capsys.readouterr().out
    assert "openrouter/configured (Configured)" in out
    assert "openrouter/other (Other)" in out


def test_list_models_says_so_when_there_is_nothing_to_list(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    path = _seed(tmp_path)
    _patch(monkeypatch, path)

    assert openclaw.list_models(SPEC) == 0
    assert "carries no models.providers.mcc models" in capsys.readouterr().out


def test_the_flags_are_names_openclaw_does_not_use() -> None:
    """MCC's flags must not shadow a flag the vendor documents."""

    for flag in (openclaw.MODELS_FLAG, openclaw.DISCONNECT_FLAG, openclaw.RESTORE_FLAG):
        assert flag.startswith("--")
        assert flag not in {"--help", "--version", "--profile", "--config"}
