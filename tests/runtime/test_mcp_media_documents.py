"""MCC's media MCP server in the config layers MCC owns (7.99.0, 7.107.0).

For OpenCode, OpenCode 2, Kilo, Crush, Qwen Code and Gemini CLI the server is
an entry in the document MCC already writes under its config folder, added by
the background publisher and by ``GET /admin/api/catalogue-models`` (the
launcher's fallback) alike -- after the serialiser and outside it, so the
desktop apps' Configure documents never carry it (risk R13). Off, every
document is the one it always was. Never ``trust`` (R16); OpenCode and Kilo
get their own ``ask`` for the tools that spend money (decision 8). Codex's
document is its model list: with its button on, only ``supports_search_tool``
changes, to ``false`` on every model (7.107.0).
"""

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft7Validator

from my_claude_code.application.catalogues import serialise
from my_claude_code.config import mcp_media
from my_claude_code.config.harnesses import harness_specs
from my_claude_code.config.mcp_media import (
    MCP_MEDIA_SPENDING_TOOLS,
    McpMediaShape,
    mcp_media_document,
    with_mcp_media_block,
)
from my_claude_code.config.settings import Settings
from my_claude_code.runtime import harness_catalogues
from my_claude_code.runtime.harness_catalogues import HarnessCatalogueFanoutPublisher
from tests.runtime.test_catalogue_fanout import FakeRuntime

SCHEMAS = Path(__file__).resolve().parents[1] / "fixtures" / "schemas"
OWNED = ("opencode", "opencode2", "kilo", "crush", "qwen_code", "gemini_cli")
COMMAND = "/opt/mcc/bin/mcc-mcp"


@pytest.fixture(autouse=True)
def _documents_land_in_tmp_path(monkeypatch, tmp_path: Path) -> None:
    # The token is in the publisher's environment too, so a registration that
    # passed the environment on would carry it.
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "proxy-secret-token")
    monkeypatch.setattr(
        harness_catalogues,
        "harness_catalogue_path",
        lambda name: tmp_path / "catalogues" / name,
    )
    monkeypatch.setattr(mcp_media, "mcp_media_command", lambda **_kw: COMMAND)


def _publish(tmp_path: Path, apps: str, name: str) -> dict[str, bytes]:
    """Every MCC-owned document the publisher writes, as bytes."""

    settings = Settings().model_copy(
        update={
            "model": "nvidia_nim/configured",
            "mcp_media_apps": apps,
            "anthropic_auth_token": "proxy-secret-token",
        }
    )
    runtime = FakeRuntime(settings=settings)
    paths = {
        spec.id: tmp_path / name / f"{spec.id}{Path(spec.catalogue.filename).suffix}"
        for spec in harness_specs()
        if spec.catalogue is not None
        and spec.catalogue.filename is not None
        and spec.catalogue.merge is None
    }
    HarnessCatalogueFanoutPublisher(paths).ensure_exists(runtime)
    return {harness_id: path.read_bytes() for harness_id, path in paths.items()}


def test_off_every_document_is_the_one_it_always_was(tmp_path: Path) -> None:
    unset = _publish(tmp_path, "", "unset")
    # A value naming nothing that has a button changes nothing either.
    nonsense = _publish(tmp_path, "aider,pi,not-an-app", "nonsense")
    assert unset == nonsense
    assert all(b"mcc_media" not in document for document in unset.values())


def test_on_only_the_named_apps_documents_change(tmp_path: Path) -> None:
    off = _publish(tmp_path, "", "off")
    on = _publish(tmp_path, "opencode,qwen_code", "on")

    changed = sorted(app for app in off if off[app] != on[app])
    assert changed == ["opencode", "qwen_code"]
    opencode = json.loads(on["opencode"])
    assert opencode["mcp"]["mcc_media"] == {
        "type": "local",
        "command": [COMMAND, "--app", "opencode"],
        "enabled": True,
    }
    # Decision 8: OpenCode does not ask by default, so MCC's layer does.
    assert opencode["permission"] == {
        f"mcc_media_{tool}": "ask" for tool in MCP_MEDIA_SPENDING_TOOLS
    }
    # Everything else in the document is what it was.
    without = {k: v for k, v in opencode.items() if k not in {"mcp", "permission"}}
    assert without == json.loads(off["opencode"])
    # The token is never written into a document for the server: an app whose
    # own provider entry already carries it (MCC's documents for some apps
    # do) carries it exactly as often as before, and the blocks never.
    token = b"proxy-secret-token"
    assert all(on[app].count(token) == off[app].count(token) for app in on)
    assert "proxy-secret-token" not in json.dumps(opencode["mcp"])
    assert "proxy-secret-token" not in json.dumps(
        json.loads(on["qwen_code"])["mcpServers"]
    )


def test_codex_on_only_tool_search_changes_to_false_on_each_model(
    tmp_path: Path,
) -> None:
    """7.107.0, decision 1: Codex's button on → its model list says "no tool
    search", so the launch line's tools reach the model; nothing else moves."""

    off = _publish(tmp_path, "", "off")
    on = _publish(tmp_path, "codex", "on")

    changed = sorted(app for app in off if off[app] != on[app])
    assert changed == ["codex"]
    before = json.loads(off["codex"])
    after = json.loads(on["codex"])
    assert before["models"], "the fake runtime gives Codex at least one model"
    assert all(entry["supports_search_tool"] is True for entry in before["models"])
    assert all(entry["supports_search_tool"] is False for entry in after["models"])
    # Every other key, the entries' order and their keys' order are as before.
    flipped = [{**entry, "supports_search_tool": True} for entry in after["models"]]
    assert flipped == before["models"]
    assert [list(entry) for entry in after["models"]] == [
        list(entry) for entry in before["models"]
    ]
    assert {k: v for k, v in after.items() if k != "models"} == {
        k: v for k, v in before.items() if k != "models"
    }
    # The model list carries no server entry: Codex gets it on the launch line.
    assert b"mcc_media" not in on["codex"]


def test_remove_puts_every_document_back_byte_for_byte(tmp_path: Path) -> None:
    before = _publish(tmp_path, "", "same")
    _publish(tmp_path, ",".join((*OWNED, "codex")), "same")
    after = _publish(tmp_path, "", "same")
    assert after == before


def test_codex_without_mcc_mcp_keeps_tool_search(tmp_path: Path, monkeypatch) -> None:
    """The launcher leaves the server out when ``mcc-mcp`` is missing, so the
    model list keeps tool search then too: the two always agree."""

    off = _publish(tmp_path, "", "off")
    monkeypatch.setattr(mcp_media, "mcp_media_command", lambda **_kw: None)
    missing = _publish(tmp_path, "codex", "missing")
    assert missing == off


def test_gemini_and_qwen_never_mark_the_server_trusted(tmp_path: Path) -> None:
    on = _publish(tmp_path, "qwen_code,gemini_cli", "trust")
    for app in ("qwen_code", "gemini_cli"):
        entry = json.loads(on[app])["mcpServers"]["mcc_media"]
        assert entry == {"command": COMMAND, "args": ["--app", app]}
        assert "trust" not in entry


def test_qwens_own_version_key_is_tolerated() -> None:
    """Qwen rewrites MCC's file with ``"$version": 4``; the merge keeps it."""

    document = {"$version": 4, "mcpServers": {"users_own": {"command": "x"}}}
    merged = with_mcp_media_block(
        McpMediaShape.MCP_SERVERS, "qwen_code", document, COMMAND, {}
    )
    assert merged["$version"] == 4
    assert merged["mcpServers"]["users_own"] == {"command": "x"}
    assert merged["mcpServers"]["mcc_media"]["command"] == COMMAND
    assert document["mcpServers"] == {"users_own": {"command": "x"}}


def test_the_block_is_added_outside_the_serialiser() -> None:
    """R13: the OpenCode desktop Configure document comes from ``serialise()``."""

    for format_id in ("opencode", "kilo", "crush", "qwen", "gemini_cli"):
        document, _defaulted = serialise(format_id, ())
        assert "mcc_media" not in json.dumps(document), format_id


@pytest.mark.parametrize(
    ("schema_file", "shape", "app"),
    [
        ("opencode-config.schema.json", McpMediaShape.OPENCODE_MCP, "opencode"),
        ("opencode-config.schema.json", McpMediaShape.OPENCODE_MCP, "kilo"),
        ("crush.schema.json", McpMediaShape.CRUSH_MCP, "crush"),
    ],
)
def test_the_entry_validates_against_the_apps_own_schema(
    schema_file: str, shape: McpMediaShape, app: str
) -> None:
    schema = json.loads((SCHEMAS / schema_file).read_text(encoding="utf-8"))
    document: dict[str, Any] = with_mcp_media_block(
        shape, app, {}, COMMAND, {"MCC_CONFIG_DIR": "/cfg"}
    )
    errors = sorted(
        Draft7Validator(schema).iter_errors(document), key=lambda error: error.path
    )
    assert [error.message for error in errors] == []


def test_mcp_media_document_is_the_same_object_when_off() -> None:
    document = {"provider": {}}
    for apps in ("", "codex", "claude_desktop"):
        assert mcp_media_document(
            "opencode", document, apps_value=apps, environ={}
        ) is (document)
    # And for an app with no MCC-owned document of this kind at all.
    assert (
        mcp_media_document("aider", document, apps_value="aider", environ={})
        is document
    )


def test_codexs_model_list_is_the_same_object_while_its_button_is_off() -> None:
    document = {"models": [{"slug": "a", "supports_search_tool": True}]}
    for apps in ("", "claude,opencode", "aider,pi"):
        assert (
            mcp_media_document("codex", document, apps_value=apps, environ={})
            is document
        )
    on = mcp_media_document("codex", document, apps_value="codex", environ={})
    assert on == {"models": [{"slug": "a", "supports_search_tool": False}]}
    # A copy: the caller's document is never changed in place.
    assert document == {"models": [{"slug": "a", "supports_search_tool": True}]}
