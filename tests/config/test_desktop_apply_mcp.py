"""MCC's media MCP server entry in the user's own files (7.100.0).

Decision 11 of 2026-10-18: the second key a Desktop apps card owns. Every test
drives ``config/desktop_apply`` against documents in a scratch home, and the
properties asserted are the ones the card promises:

* Add writes the entry and nothing else -- the rest of the file, comments and
  key order included, is the same bytes, and the parsed document differs by
  exactly MCC's keys;
* the first write takes the one-time backup, and a second never replaces it;
* Remove, and Undo in both modes, give the file back byte for byte -- in
  whichever order Configure and Add were pressed;
* an ``mcc_media`` entry MCC did not write is refused, never overwritten or
  deleted;
* a file MCC created for the entry is deleted again, folder included;
* no credential ever goes into the entry, the plan or the record.
"""

import json
import tomllib
from pathlib import Path

import pytest

from my_claude_code.config import desktop_apply
from my_claude_code.config.desktop_apps import (
    CLAUDE_DESKTOP_CONFIG_ID,
    DESKTOP_APPS,
    DesktopMcpDocument,
    DesktopMcpState,
    desktop_app,
)
from my_claude_code.config.document_codecs import DocumentFormat, parse_document
from my_claude_code.config.mcp_media import (
    MCP_MEDIA_SERVER_NAME,
    desktop_file_entry,
    opencode_permission_keys,
)
from my_claude_code.config.restore_record import (
    UndoMode,
    forget_mcp_record,
    read_mcp_record,
)
from tests.config.test_desktop_apply import (
    BLOCK,
    CLAUDE_BLOCK,
    CLAUDE_META,
    env_for,
    make_marker,
)

TOKEN = "proxy-token-that-must-never-appear"

CODEX_SEED = """# my Codex settings, written by hand
model = "gpt-5.6"   # my default
approval_policy = "on-request"

[mcp_servers.github]
# the GitHub server I use every day
command = "npx"
args = ["-y", "@modelcontextprotocol/server-github"]

[mcp_servers.github.env]
GITHUB_PERSONAL_ACCESS_TOKEN = "ghp_example_not_real"

[profiles.fast]
model = "gpt-5.6-mini"
"""

OPENCODE_SEED = """{
  "$schema": "https://opencode.ai/config.json",
  // my own comment, kept by OpenCode's JSONC reader
  "theme": "tokyonight",
  "mcp": {
    "context7": {
      "type": "remote",
      "url": "https://mcp.context7.com/mcp"
    }
  },
  "permission": {
    "edit": "ask"
  }
}
"""

COMMANDCODE_MCP_SEED = """{
  "mcpServers": {
    "playwright": {
      "transport": "stdio",
      "enabled": true,
      "command": "npx",
      "args": ["@playwright/mcp@latest"]
    }
  }
}
"""

COMMANDCODE_PROVIDERS = """{
  "provider": {
    "openrouter": {"name": "OpenRouter"}
  }
}
"""

CLAUDE_CONFIG_SEED = """{
  "preferences": {
    "sidebarMode": "chat"
  },
  "mcpServers": {
    "filesystem": {
      "command": "C:\\\\tools\\\\fs-mcp.exe",
      "args": ["C:\\\\work"]
    }
  }
}
"""


def _entry(spec, home: Path) -> dict[str, object]:
    assert spec.mcp is not None
    return desktop_file_entry(
        spec.mcp.shape,
        spec.id,
        str(home / "MCC" / "Scripts" / "mcc-mcp.exe"),
        {},
        out_dir=str(home / "mcc" / "mcp-media") if spec.mcp.needs_out_dir else None,
    )


def _setup(tmp_path: Path, app_id: str, seed: str | None, *, newline: str = "\n"):
    spec = desktop_app(app_id)
    home = tmp_path / "home"
    env = env_for(home)
    make_marker(home, spec)
    path = desktop_apply.mcp_path_for(spec, env)
    assert path is not None
    if seed is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(seed.replace("\n", newline).encode("utf-8"))
    return spec, env, path, tmp_path / "record.json", _entry(spec, home)


def _parsed(path: Path, document_format: DocumentFormat) -> dict:
    text = path.read_text(encoding="utf-8")
    loaded = parse_document(text, document_format) if text.strip() else {}
    assert isinstance(loaded, dict)
    return loaded


def _add(spec, env, record, entry):
    return desktop_apply.apply_mcp(
        spec, env=env, add=True, entry=entry, record_path=record
    )


def _remove(spec, env, record):
    return desktop_apply.apply_mcp(
        spec, env=env, add=False, entry=None, record_path=record
    )


# ------------------------------------------------------------------ Codex TOML


def test_codex_add_appends_only_the_table_and_keeps_every_comment(tmp_path):
    spec, env, path, record, entry = _setup(tmp_path, "codex_desktop", CODEX_SEED)

    plan = desktop_apply.plan_mcp(
        spec, env=env, add=True, entry=entry, record_path=record
    )
    assert path.read_text(encoding="utf-8") == CODEX_SEED  # plan wrote nothing
    added = [line for line in plan.diff.splitlines() if line.startswith("+")][1:]
    assert "+[mcp_servers.mcc_media]" in added
    assert not [line for line in plan.diff.splitlines() if line.startswith("-")][1:]

    result = _add(spec, env, record, entry)
    after = path.read_text(encoding="utf-8")
    assert result.changed and result.added
    # Every byte of the user's file is still there, in order: the entry is a
    # table of its own after it, so not a comment or key is re-rendered.
    assert after.startswith(CODEX_SEED)
    assert "[mcp_servers.mcc_media]" in after[len(CODEX_SEED) :]
    loaded = tomllib.loads(after)
    seed = tomllib.loads(CODEX_SEED)
    assert loaded["mcp_servers"]["mcc_media"] == entry
    del loaded["mcp_servers"]["mcc_media"]
    assert loaded == seed
    # The one-time backup is the seed.
    backup = path.with_name(path.name + ".mcc-backup")
    assert backup.read_text(encoding="utf-8") == CODEX_SEED
    assert result.backup_path == str(backup)

    probe = desktop_apply.probe_mcp(
        spec, env=env, expected_entry=entry, record_path=record
    )
    assert probe.state is DesktopMcpState.ADDED

    # Pressing Add again changes nothing, and never retakes the backup.
    again = _add(spec, env, record, entry)
    assert again.changed is False
    assert backup.read_text(encoding="utf-8") == CODEX_SEED

    removed = _remove(spec, env, record)
    assert removed.changed
    assert path.read_bytes() == CODEX_SEED.encode("utf-8")
    assert read_mcp_record(spec.id, path=record) is None
    assert (
        desktop_apply.probe_mcp(
            spec, env=env, expected_entry=entry, record_path=record
        ).state
        is DesktopMcpState.ABSENT
    )


def test_the_codex_user_file_entry_is_never_marked_required(tmp_path):
    """Risk R6 (7.107.0): only ``mcc-codex``'s own launch line marks the server
    ``required``. The entry in the user's own ``config.toml`` is read by plain
    ``codex`` and the Codex desktop app too, and a required server that cannot
    start would stop both -- so it carries the command and arguments only."""

    spec, env, path, record, entry = _setup(tmp_path, "codex_desktop", CODEX_SEED)
    assert sorted(entry) == ["args", "command"]
    _add(spec, env, record, entry)
    written = tomllib.loads(path.read_text(encoding="utf-8"))
    assert "required" not in written["mcp_servers"]["mcc_media"]
    assert "required" not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "seed",
    [
        CODEX_SEED,
        CODEX_SEED.rstrip("\n"),  # no final newline
        CODEX_SEED + "\n\n",  # trailing blank lines
        "",  # an empty file
        "\n",  # a blank one
    ],
    ids=["plain", "no-final-newline", "trailing-blank-lines", "empty", "blank"],
)
def test_codex_add_then_remove_is_byte_identical_for_every_ending(tmp_path, seed):
    spec, env, path, record, entry = _setup(tmp_path, "codex_desktop", seed)
    _add(spec, env, record, entry)
    assert "mcc_media" in path.read_text(encoding="utf-8")
    _remove(spec, env, record)
    assert path.read_bytes() == seed.encode("utf-8")


def test_a_crlf_file_keeps_its_line_endings_through_add_and_remove(tmp_path):
    spec, env, path, record, entry = _setup(
        tmp_path, "codex_desktop", CODEX_SEED, newline="\r\n"
    )
    seed_bytes = path.read_bytes()
    _add(spec, env, record, entry)
    written = path.read_bytes()
    assert b"\r\n" in written
    assert b"\n" not in written.replace(b"\r\n", b"")
    _remove(spec, env, record)
    assert path.read_bytes() == seed_bytes


#: Configure *replaces* the user's ``model`` line, and Undo writes the value
#: back without a comment that sat on the same line -- measured on 9fa8f43d
#: (7.99.0), before this release, and not this key's doing. The byte-for-byte
#: Undo seed has its comments on lines of their own; the commented seed is
#: compared with what Configure and Undo alone give back.
CODEX_UNDO_SEED = CODEX_SEED.replace("   # my default", "")


def _configure_codex(spec, env, record):
    desktop_apply.apply(
        spec,
        env=env,
        block=BLOCK,
        scalars={"model_provider": "mcc", "model": "mcc/best"},
        record_path=record,
    )


@pytest.mark.parametrize(
    "seed", [CODEX_UNDO_SEED, CODEX_SEED], ids=["exact", "comment"]
)
@pytest.mark.parametrize("mode", [UndoMode.KEYS_ONLY, UndoMode.RESTORE])
@pytest.mark.parametrize("order", ["configure-first", "add-first"])
def test_codex_undo_takes_both_keys_out_byte_for_byte(tmp_path, seed, mode, order):
    # What Configure and Undo give back with no media server ever added.
    ref_spec, ref_env, ref_path, ref_record, _ = _setup(
        tmp_path / "reference", "codex_desktop", seed
    )
    _configure_codex(ref_spec, ref_env, ref_record)
    desktop_apply.undo(ref_spec, env=ref_env, mode=mode, record_path=ref_record)
    reference = ref_path.read_bytes()
    if seed == CODEX_UNDO_SEED:
        assert reference == seed.encode("utf-8")

    spec, env, path, record, entry = _setup(tmp_path, "codex_desktop", seed)
    if order == "configure-first":
        _configure_codex(spec, env, record)
        _add(spec, env, record, entry)
    else:
        _add(spec, env, record, entry)
        _configure_codex(spec, env, record)
    text = path.read_text(encoding="utf-8")
    assert "[mcp_servers.mcc_media]" in text and "[model_providers.mcc]" in text

    # RESTORE checks the document hash Configure recorded. Adding the media
    # server is MCC's own edit and must not count as drift.
    result = desktop_apply.undo(spec, env=env, mode=mode, record_path=record)
    assert result.removed_mcp_server is True
    assert path.read_bytes() == reference
    assert read_mcp_record(spec.id, path=record) is None
    assert result.restored_keys == ("model",)


def test_restore_still_refuses_after_a_hand_edit_even_with_the_entry_added(tmp_path):
    spec, env, path, record, entry = _setup(tmp_path, "codex_desktop", CODEX_SEED)
    desktop_apply.apply(
        spec,
        env=env,
        block=BLOCK,
        scalars={"model_provider": "mcc", "model": "mcc/best"},
        record_path=record,
    )
    with path.open("a", encoding="utf-8", newline="") as handle:
        handle.write("\n# an edit of my own\n")
    _add(spec, env, record, entry)
    with pytest.raises(desktop_apply.DesktopApplyError, match="has changed"):
        desktop_apply.undo(spec, env=env, mode=UndoMode.RESTORE, record_path=record)


def test_a_foreign_entry_is_refused_by_plan_add_and_remove(tmp_path):
    seed = CODEX_SEED + '\n[mcp_servers.mcc_media]\ncommand = "my-own-thing"\n'
    spec, env, path, record, entry = _setup(tmp_path, "codex_desktop", seed)

    probe = desktop_apply.probe_mcp(
        spec, env=env, expected_entry=entry, record_path=record
    )
    assert probe.state is DesktopMcpState.FOREIGN
    for add in (True, False):
        with pytest.raises(desktop_apply.DesktopApplyError, match="did not add"):
            desktop_apply.plan_mcp(
                spec, env=env, add=add, entry=entry, record_path=record
            )
        with pytest.raises(desktop_apply.DesktopApplyError, match="did not add"):
            desktop_apply.apply_mcp(
                spec, env=env, add=add, entry=entry, record_path=record
            )
    assert path.read_bytes() == seed.encode("utf-8")
    assert not path.with_name(path.name + ".mcc-backup").exists()


def test_an_entry_identical_to_mccs_but_not_written_by_it_is_still_the_users(
    tmp_path,
):
    """The copy line on the MCC media server card is made to be pasted here."""

    spec, env, path, record, entry = _setup(tmp_path, "codex_desktop", CODEX_UNDO_SEED)
    _configure_codex(spec, env, record)
    _add(spec, env, record, entry)
    forget_mcp_record(spec.id, path=record)  # MCC no longer remembers writing it
    probe = desktop_apply.probe_mcp(
        spec, env=env, expected_entry=entry, record_path=record
    )
    assert probe.state is DesktopMcpState.FOREIGN
    with pytest.raises(desktop_apply.DesktopApplyError):
        _remove(spec, env, record)
    # Undo takes Configure's keys out and leaves the entry where it is.
    result = desktop_apply.undo(spec, env=env, record_path=record)
    assert result.removed_mcp_server is False
    left = tomllib.loads(path.read_text(encoding="utf-8"))
    assert left["mcp_servers"]["mcc_media"] == entry
    assert "model_providers" not in left


def test_mccs_entry_changed_by_hand_is_no_longer_mccs(tmp_path):
    spec, env, path, record, entry = _setup(tmp_path, "codex_desktop", CODEX_SEED)
    _add(spec, env, record, entry)
    edited = path.read_text(encoding="utf-8") + "startup_timeout_sec = 30\n"
    path.write_text(edited, encoding="utf-8", newline="")
    assert (
        desktop_apply.probe_mcp(
            spec, env=env, expected_entry=entry, record_path=record
        ).state
        is DesktopMcpState.FOREIGN
    )
    with pytest.raises(desktop_apply.DesktopApplyError):
        _remove(spec, env, record)
    assert path.read_text(encoding="utf-8") == edited


def test_an_older_entry_of_mccs_reads_outdated_and_add_updates_it(tmp_path):
    spec, env, path, record, entry = _setup(tmp_path, "codex_desktop", CODEX_SEED)
    _add(spec, env, record, entry)
    moved = dict(entry, command=str(tmp_path / "elsewhere" / "mcc-mcp.exe"))
    assert (
        desktop_apply.probe_mcp(
            spec, env=env, expected_entry=moved, record_path=record
        ).state
        is DesktopMcpState.OUTDATED
    )
    _add(spec, env, record, moved)
    assert (
        tomllib.loads(path.read_text(encoding="utf-8"))["mcp_servers"]["mcc_media"]
        == moved
    )
    _remove(spec, env, record)
    assert path.read_bytes() == CODEX_SEED.encode("utf-8")


def test_an_inline_mcp_servers_table_is_refused_with_nothing_written(tmp_path):
    seed = 'mcp_servers = { github = { command = "npx" } }\n'
    spec, env, path, record, entry = _setup(tmp_path, "codex_desktop", seed)
    with pytest.raises(desktop_apply.DesktopApplyError, match="without changing"):
        _add(spec, env, record, entry)
    assert path.read_bytes() == seed.encode("utf-8")
    assert read_mcp_record(spec.id, path=record) is None


def test_a_missing_file_and_folder_are_created_and_removed_again(tmp_path):
    spec, env, path, record, entry = _setup(tmp_path, "codex_desktop", None)
    assert not path.parent.exists()
    plan = desktop_apply.plan_mcp(
        spec, env=env, add=True, entry=entry, record_path=record
    )
    assert plan.creates_file is True
    result = _add(spec, env, record, entry)
    assert result.created_file is True
    assert tomllib.loads(path.read_text(encoding="utf-8")) == {
        "mcp_servers": {MCP_MEDIA_SERVER_NAME: entry}
    }
    plan = desktop_apply.plan_mcp(
        spec, env=env, add=False, entry=None, record_path=record
    )
    assert plan.removes_file is True
    removed = _remove(spec, env, record)
    assert removed.removed_file is True
    assert not path.exists()
    assert not path.parent.exists()  # ~/.codex was MCC's too
    assert not path.with_name(path.name + ".mcc-backup").exists()


def test_undo_deletes_a_file_the_entry_created(tmp_path):
    spec, env, path, record, entry = _setup(tmp_path, "codex_desktop", None)
    path.parent.mkdir(parents=True)
    _add(spec, env, record, entry)
    result = desktop_apply.undo(spec, env=env, record_path=record)
    assert result.removed_mcp_server is True
    assert not path.exists()
    assert path.parent.is_dir()  # it was there before


def test_configure_keeps_the_entry_and_the_entry_keeps_configure(tmp_path):
    spec, env, path, record, entry = _setup(tmp_path, "codex_desktop", CODEX_SEED)
    scalars = {"model_provider": "mcc", "model": "mcc/best"}
    desktop_apply.apply(spec, env=env, block=BLOCK, scalars=scalars, record_path=record)
    configured = tomllib.loads(path.read_text(encoding="utf-8"))
    _add(spec, env, record, entry)
    with_entry = tomllib.loads(path.read_text(encoding="utf-8"))
    assert with_entry["model_providers"] == configured["model_providers"]
    assert with_entry["model"] == configured["model"]
    # A re-apply of Configure leaves the entry alone and reads as configured.
    desktop_apply.apply(spec, env=env, block=BLOCK, scalars=scalars, record_path=record)
    assert (
        tomllib.loads(path.read_text(encoding="utf-8"))["mcp_servers"]["mcc_media"]
        == entry
    )
    probe = desktop_apply.probe(
        spec,
        env=env,
        expected_block=BLOCK,
        expected_scalars=scalars,
        record_path=record,
    )
    assert probe.state.value == "configured"
    # Remove MCP server takes the entry out and leaves Configure's keys.
    _remove(spec, env, record)
    assert tomllib.loads(path.read_text(encoding="utf-8")) == configured


# --------------------------------------------------------------- OpenCode JSON


def test_opencode_add_sets_the_entry_and_its_own_ask_and_nothing_else(tmp_path):
    spec, env, path, record, entry = _setup(tmp_path, "opencode_desktop", OPENCODE_SEED)
    plan = desktop_apply.plan_mcp(
        spec, env=env, add=True, entry=entry, record_path=record
    )
    assert plan.ask_keys == tuple(
        f"permission.{key}" for key in opencode_permission_keys()
    )
    _add(spec, env, record, entry)
    text = path.read_text(encoding="utf-8")
    assert "// my own comment, kept by OpenCode's JSONC reader" in text
    loaded = _parsed(path, DocumentFormat.JSON)
    seed = parse_document(OPENCODE_SEED, DocumentFormat.JSON)
    assert isinstance(seed, dict)
    assert loaded["mcp"]["mcc_media"] == entry
    assert entry["type"] == "local"
    assert entry["command"][1:] == ["--app", "opencode_desktop"]
    for key in opencode_permission_keys():
        assert loaded["permission"][key] == "ask"
    # Nothing that spends no money is gated.
    assert "mcc_media_media_status" not in loaded["permission"]
    del loaded["mcp"]["mcc_media"]
    for key in opencode_permission_keys():
        del loaded["permission"][key]
    assert loaded == seed
    _remove(spec, env, record)
    assert path.read_bytes() == OPENCODE_SEED.encode("utf-8")


def test_a_one_word_permission_is_the_users_policy_and_is_left_alone(tmp_path):
    seed = '{\n  "permission": "allow",\n  "theme": "dark"\n}\n'
    spec, env, path, record, entry = _setup(tmp_path, "opencode_desktop", seed)
    _add(spec, env, record, entry)
    loaded = _parsed(path, DocumentFormat.JSON)
    assert loaded["permission"] == "allow"
    _remove(spec, env, record)
    assert path.read_bytes() == seed.encode("utf-8")


def test_a_permission_the_user_set_for_one_tool_is_kept(tmp_path):
    seed = '{\n  "permission": {\n    "mcc_media_generate_image": "allow"\n  }\n}\n'
    spec, env, path, record, entry = _setup(tmp_path, "opencode_desktop", seed)
    _add(spec, env, record, entry)
    loaded = _parsed(path, DocumentFormat.JSON)
    assert loaded["permission"]["mcc_media_generate_image"] == "allow"
    assert loaded["permission"]["mcc_media_speak"] == "ask"
    _remove(spec, env, record)
    assert path.read_bytes() == seed.encode("utf-8")


def test_an_ask_the_user_changed_since_stays_theirs(tmp_path):
    spec, env, path, record, entry = _setup(tmp_path, "opencode_desktop", OPENCODE_SEED)
    _add(spec, env, record, entry)
    text = path.read_text(encoding="utf-8").replace(
        '"mcc_media_speak": "ask"', '"mcc_media_speak": "allow"'
    )
    path.write_text(text, encoding="utf-8", newline="")
    _remove(spec, env, record)
    loaded = _parsed(path, DocumentFormat.JSON)
    assert "mcc_media" not in loaded["mcp"]
    assert loaded["permission"] == {"edit": "ask", "mcc_media_speak": "allow"}


@pytest.mark.parametrize(
    "seed",
    ['{\n  "theme": "dark"\n}\n', "{}\n", '{"theme":"dark"}', ""],
    ids=["no-mcp-key", "empty-object", "one-line", "missing"],
)
def test_opencode_containers_mcc_created_go_with_it(tmp_path, seed):
    spec, env, path, record, entry = _setup(
        tmp_path, "opencode_desktop", seed if seed else None
    )
    _add(spec, env, record, entry)
    assert "mcc_media" in _parsed(path, DocumentFormat.JSON)["mcp"]
    _remove(spec, env, record)
    if seed:
        assert path.read_bytes() == seed.encode("utf-8")
    else:
        assert not path.exists()


@pytest.mark.parametrize("mode", [UndoMode.KEYS_ONLY, UndoMode.RESTORE])
@pytest.mark.parametrize("order", ["configure-first", "add-first"])
def test_opencode_undo_takes_both_keys_out_byte_for_byte(tmp_path, mode, order):
    spec, env, path, record, entry = _setup(tmp_path, "opencode_desktop", OPENCODE_SEED)
    block = {
        "npm": "@ai-sdk/openai-compatible",
        "name": "My Claude Code",
        "options": {"baseURL": "http://127.0.0.1:8082/v1", "apiKey": TOKEN},
        "models": {"mcc/best": {"name": "MCC best"}},
    }

    def configure():
        desktop_apply.apply(spec, env=env, block=block, record_path=record)

    if order == "configure-first":
        configure()
        _add(spec, env, record, entry)
    else:
        _add(spec, env, record, entry)
        configure()
    result = desktop_apply.undo(spec, env=env, mode=mode, record_path=record)
    assert result.removed_mcp_server is True
    assert path.read_bytes() == OPENCODE_SEED.encode("utf-8")


# ------------------------------------------------------- Command Code mcp.json


def test_commandcode_entry_goes_into_mcp_json_and_never_providers_json(tmp_path):
    spec, env, path, record, entry = _setup(
        tmp_path, "commandcode", COMMANDCODE_MCP_SEED
    )
    providers = desktop_apply.document_path_for(spec, env)
    assert providers is not None and providers != path
    providers.write_text(COMMANDCODE_PROVIDERS, encoding="utf-8", newline="")
    _add(spec, env, record, entry)
    assert providers.read_text(encoding="utf-8") == COMMANDCODE_PROVIDERS
    loaded = _parsed(path, DocumentFormat.JSON)
    assert loaded["mcpServers"]["mcc_media"] == entry
    assert entry["transport"] == "stdio" and entry["enabled"] is True
    assert loaded["mcpServers"]["playwright"]["command"] == "npx"
    _remove(spec, env, record)
    assert path.read_bytes() == COMMANDCODE_MCP_SEED.encode("utf-8")


@pytest.mark.parametrize("mode", [UndoMode.KEYS_ONLY, UndoMode.RESTORE])
def test_commandcode_undo_restores_both_files(tmp_path, mode):
    spec, env, path, record, entry = _setup(tmp_path, "commandcode", None)
    providers = desktop_apply.document_path_for(spec, env)
    assert providers is not None
    providers.parent.mkdir(parents=True, exist_ok=True)
    providers.write_text(COMMANDCODE_PROVIDERS, encoding="utf-8", newline="")
    desktop_apply.apply(
        spec,
        env=env,
        block={"name": "My Claude Code", "baseURL": "http://127.0.0.1:8082/v1"},
        record_path=record,
    )
    _add(spec, env, record, entry)
    assert path.exists()
    result = desktop_apply.undo(spec, env=env, mode=mode, record_path=record)
    assert result.removed_mcp_server is True
    assert providers.read_bytes() == COMMANDCODE_PROVIDERS.encode("utf-8")
    assert not path.exists()  # mcp.json was MCC's, created for the entry


# ------------------------------------------ Claude Desktop fallback (decision 9)


def test_claude_desktop_fallback_is_its_own_config_file_with_an_out_folder(tmp_path):
    spec, env, path, record, entry = _setup(
        tmp_path, "claude_desktop", CLAUDE_CONFIG_SEED
    )
    assert spec.mcp is not None and spec.mcp.fallback is True
    assert path.name == "claude_desktop_config.json"
    assert path.parent.name == "Claude-3p"
    assert entry["args"][:2] == ["--app", "claude_desktop"]
    assert entry["args"][2] == "--out"
    _add(spec, env, record, entry)
    loaded = _parsed(path, DocumentFormat.JSON)
    assert loaded["mcpServers"]["mcc_media"] == entry
    assert loaded["preferences"] == {"sidebarMode": "chat"}
    _remove(spec, env, record)
    assert path.read_bytes() == CLAUDE_CONFIG_SEED.encode("utf-8")


def test_claude_desktop_undo_removes_the_fallback_entry_too(tmp_path):
    spec, env, path, record, entry = _setup(
        tmp_path, "claude_desktop", CLAUDE_CONFIG_SEED
    )
    meta = desktop_apply.document_path_for(spec, env)
    assert meta is not None
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text(CLAUDE_META, encoding="utf-8", newline="")
    desktop_apply.apply(
        spec,
        env=env,
        block=CLAUDE_BLOCK,
        scalars={"appliedId": CLAUDE_DESKTOP_CONFIG_ID},
        sidecar_document={"inferenceProvider": "gateway"},
        record_path=record,
    )
    _add(spec, env, record, entry)
    result = desktop_apply.undo(spec, env=env, record_path=record)
    assert result.removed_mcp_server is True
    assert path.read_bytes() == CLAUDE_CONFIG_SEED.encode("utf-8")


def test_a_managed_claude_desktop_offers_no_entry(tmp_path, monkeypatch):
    spec, env, path, record, entry = _setup(
        tmp_path, "claude_desktop", CLAUDE_CONFIG_SEED
    )
    monkeypatch.setattr(
        desktop_apply,
        "_registry_value_names",
        lambda hive, subkey: ("inferenceProvider",) if hive == "HKCU" else (),
    )
    probe = desktop_apply.probe_mcp(
        spec, env=env, expected_entry=entry, record_path=record
    )
    assert probe.state is DesktopMcpState.MANAGED
    with pytest.raises(desktop_apply.DesktopApplyError, match="outranks"):
        _add(spec, env, record, entry)
    assert path.read_bytes() == CLAUDE_CONFIG_SEED.encode("utf-8")


# ------------------------------------------------------------- cross-cutting


def test_an_app_that_is_not_installed_gets_no_entry(tmp_path):
    spec = desktop_app("codex_desktop")
    home = tmp_path / "home"
    env = env_for(home)  # no codex program anywhere
    entry = _entry(spec, home)
    assert (
        desktop_apply.probe_mcp(spec, env=env, expected_entry=entry).state
        is DesktopMcpState.NOT_INSTALLED
    )
    with pytest.raises(desktop_apply.DesktopApplyError, match="not installed"):
        desktop_apply.apply_mcp(spec, env=env, add=True, entry=entry)
    path = desktop_apply.mcp_path_for(spec, env)
    assert path is not None and not path.exists()


def test_an_unreadable_file_is_reported_and_never_written(tmp_path):
    seed = "{ this is not json"
    spec, env, path, record, entry = _setup(tmp_path, "opencode_desktop", seed)
    probe = desktop_apply.probe_mcp(
        spec, env=env, expected_entry=entry, record_path=record
    )
    assert probe.state is DesktopMcpState.UNREADABLE and probe.error
    with pytest.raises(desktop_apply.DesktopApplyError, match="cannot parse"):
        _add(spec, env, record, entry)
    assert path.read_bytes() == seed.encode("utf-8")


@pytest.mark.parametrize(
    ("app_id", "seed"),
    [
        ("codex_desktop", CODEX_SEED),
        ("opencode_desktop", OPENCODE_SEED),
        ("commandcode", COMMANDCODE_MCP_SEED),
        ("claude_desktop", CLAUDE_CONFIG_SEED),
    ],
)
def test_no_credential_reaches_the_entry_the_plan_or_the_record(tmp_path, app_id, seed):
    spec, env, path, record, entry = _setup(tmp_path, app_id, seed)
    assert TOKEN not in json.dumps(entry)
    assert not {"token", "apiKey", "api_key", "Authorization"} & set(entry)
    plan = desktop_apply.plan_mcp(
        spec, env=env, add=True, entry=entry, record_path=record
    )
    _add(spec, env, record, entry)
    for text in (plan.diff, path.read_text(encoding="utf-8"), record.read_text()):
        assert TOKEN not in text
        assert "sk-" not in text


def _declared(app_id: str) -> DesktopMcpDocument:
    mcp = desktop_app(app_id).mcp
    assert mcp is not None, app_id
    return mcp


def test_every_mcp_declaration_names_a_key_under_mccs_server_name():
    declared = {
        app_id: _declared(app_id)
        for app_id in (
            "codex_desktop",
            "opencode_desktop",
            "commandcode",
            "claude_desktop",
        )
    }
    for app_id, mcp in declared.items():
        assert mcp.entry_key_path[-1] == MCP_MEDIA_SERVER_NAME
        assert mcp.display_path, app_id
        assert mcp.asks, app_id
    # Only the apps with a document of their own; every other card says why not.
    assert {spec.id for spec in DESKTOP_APPS if spec.mcp is not None} == set(declared)
    assert declared["claude_desktop"].fallback is True
    assert "60 s" in declared["claude_desktop"].note
    assert not any(
        declared[app_id].fallback
        for app_id in ("codex_desktop", "opencode_desktop", "commandcode")
    )
    for app_id in ("vscode_copilot", "roo_code"):
        spec = desktop_app(app_id)
        assert spec.mcp is None
        assert "not measured yet" in spec.mcp_note
