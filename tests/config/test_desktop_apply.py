"""Configure, undo and probe, against documents in a scratch home.

Every test here drives the same engine the Coding agents page and ``mcc-apps``
drive, and none of them touches a real application's configuration file: the
home directory is a ``tmp_path`` and the documents in it are written by the
test.

The properties asserted are the four the merge model promises, plus the two
undo modes that are new in 6.55.0:

* one key, one owner -- everything MCC does not own survives
* backed up once, before the first write, at the source file's mode
* idempotent on the owned subtree, not on the file's bytes
* reversible, in both senses: remove MCC's keys, or restore what MCC replaced
"""

import json
from pathlib import Path

import pytest

from my_claude_code.config import desktop_apply
from my_claude_code.config.desktop_apps import (
    DesktopAppState,
    desktop_app,
)
from my_claude_code.config.restore_record import UndoMode

BLOCK: dict[str, object] = {
    "name": "My Claude Code",
    "base_url": "http://127.0.0.1:8082/v1",
    "env_key": "MCC_AUTH_TOKEN",
    "http_headers": {"x-mcc-harness": "codex_desktop"},
}


def document_path(spec, env) -> Path:
    """Return the app's document path, which every spec used here declares.

    ``desktop_apply.document_path_for`` returns ``None`` for a card with no
    document at all -- Claude Desktop, and every NOT_ROUTABLE row. None of
    those is under test in this file, so asserting here keeps every caller
    below free of a narrowing branch that could never be taken.
    """

    path = desktop_apply.document_path_for(spec, env)
    assert path is not None
    return path


def env_for(home) -> dict[str, str]:
    return {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "APPDATA": str(home / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(home / "AppData" / "Local"),
    }


def prepare(home, spec, contents: str | None) -> None:
    """Create the app's marker directory, and its document when given one."""

    env = env_for(home)
    marker = desktop_apply.resolve_path(spec.detect.markers, env)
    assert marker is not None
    marker.mkdir(parents=True, exist_ok=True)
    if contents is None:
        return
    path = document_path(spec, env)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8", newline="")


# --------------------------------------------------------------- TOML


CODEX_DOCUMENT = """# a comment the user wrote
model = "gpt-5.6-luna"
model_reasoning_effort = "xhigh"

[projects."C:/work"]
trust_level = "trusted"

[mcp_servers.fetch]
command = "uvx"
"""


def test_toml_merge_preserves_comments_and_every_other_table(tmp_path):
    """The lines MCC does not own come back byte-for-byte, comments included."""

    spec = desktop_app("codex_desktop")
    prepare(tmp_path, spec, CODEX_DOCUMENT)
    env = env_for(tmp_path)

    desktop_apply.apply(
        spec,
        env=env,
        block=BLOCK,
        scalars={"model_provider": "mcc", "model": "mcc/best"},
        record_path=tmp_path / "record.json",
    )

    path = document_path(spec, env)
    text = path.read_text(encoding="utf-8", newline=None)
    assert "# a comment the user wrote" in text
    assert '[projects."C:/work"]' in text
    assert "[mcp_servers.fetch]" in text
    assert 'model = "mcc/best"' in text
    assert 'model_provider = "mcc"' in text


def test_reapply_with_the_same_capabilities_does_not_touch_the_file(tmp_path):
    """Idempotence is measured on MCC's key, so a refresh causes no mtime churn."""

    spec = desktop_app("codex_desktop")
    prepare(tmp_path, spec, CODEX_DOCUMENT)
    env = env_for(tmp_path)
    record = tmp_path / "record.json"
    scalars = {"model_provider": "mcc", "model": "mcc/best"}

    desktop_apply.apply(spec, env=env, block=BLOCK, scalars=scalars, record_path=record)
    path = document_path(spec, env)
    first = path.read_bytes()

    again = desktop_apply.apply(
        spec, env=env, block=BLOCK, scalars=scalars, record_path=record
    )
    assert not again.changed
    assert path.read_bytes() == first


def test_undo_keys_only_removes_mcc_and_leaves_the_rest(tmp_path):
    spec = desktop_app("codex_desktop")
    prepare(tmp_path, spec, CODEX_DOCUMENT)
    env = env_for(tmp_path)
    record = tmp_path / "record.json"

    desktop_apply.apply(
        spec,
        env=env,
        block=BLOCK,
        scalars={"model_provider": "mcc", "model": "mcc/best"},
        record_path=record,
    )
    desktop_apply.undo(spec, env=env, mode=UndoMode.KEYS_ONLY, record_path=record)

    text = document_path(spec, env).read_text(encoding="utf-8", newline=None)
    assert "model_providers.mcc" not in text
    assert "model_provider" not in text
    # The user's own model was *overwritten*, so keys-only leaves it removed:
    # putting it back is what the other mode is for.
    assert 'model = "gpt-5.6-luna"' not in text
    assert "# a comment the user wrote" in text
    assert '[projects."C:/work"]' in text


def test_undo_restore_puts_back_the_scalar_mcc_overwrote(tmp_path):
    """The whole point of the side record: the user's own model comes back."""

    spec = desktop_app("codex_desktop")
    prepare(tmp_path, spec, CODEX_DOCUMENT)
    env = env_for(tmp_path)
    record = tmp_path / "record.json"
    path = document_path(spec, env)
    original = path.read_bytes()

    desktop_apply.apply(
        spec,
        env=env,
        block=BLOCK,
        scalars={"model_provider": "mcc", "model": "mcc/best"},
        record_path=record,
    )
    result = desktop_apply.undo(
        spec, env=env, mode=UndoMode.RESTORE, record_path=record
    )

    assert result.restored_keys == ("model",)
    assert path.read_bytes() == original


def test_undo_restore_refuses_when_the_document_changed_since_apply(tmp_path):
    """A restore into a rewritten document would revert an edit made on purpose."""

    spec = desktop_app("codex_desktop")
    prepare(tmp_path, spec, CODEX_DOCUMENT)
    env = env_for(tmp_path)
    record = tmp_path / "record.json"
    path = document_path(spec, env)

    desktop_apply.apply(
        spec,
        env=env,
        block=BLOCK,
        scalars={"model_provider": "mcc", "model": "mcc/best"},
        record_path=record,
    )
    path.write_text(
        path.read_text(encoding="utf-8", newline=None)
        + '\nnew_key = "added by hand"\n',
        encoding="utf-8",
        newline="",
    )

    with pytest.raises(desktop_apply.DesktopApplyError, match="changed since"):
        desktop_apply.undo(spec, env=env, mode=UndoMode.RESTORE, record_path=record)


def test_backup_is_taken_once_and_is_always_the_pre_mcc_file(tmp_path):
    spec = desktop_app("codex_desktop")
    prepare(tmp_path, spec, CODEX_DOCUMENT)
    env = env_for(tmp_path)
    record = tmp_path / "record.json"

    first = desktop_apply.apply(
        spec,
        env=env,
        block=BLOCK,
        scalars={"model_provider": "mcc"},
        record_path=record,
    )
    backup = tmp_path / ".codex" / "config.toml.mcc-backup"
    assert backup.read_text(encoding="utf-8", newline=None) == CODEX_DOCUMENT
    assert first.backup_path == str(backup)

    changed = dict(BLOCK) | {"base_url": "http://127.0.0.1:9999/v1"}
    desktop_apply.apply(
        spec,
        env=env,
        block=changed,
        scalars={"model_provider": "mcc"},
        record_path=record,
    )
    # Still the user's file, not yesterday's MCC output.
    assert backup.read_text(encoding="utf-8", newline=None) == CODEX_DOCUMENT


def test_an_unparseable_document_is_never_written(tmp_path):
    spec = desktop_app("codex_desktop")
    prepare(tmp_path, spec, "this is [not valid toml\n")
    env = env_for(tmp_path)
    path = document_path(spec, env)
    before = path.read_bytes()

    with pytest.raises(desktop_apply.DesktopApplyError, match="cannot parse"):
        desktop_apply.apply(
            spec, env=env, block=BLOCK, scalars={}, record_path=tmp_path / "r.json"
        )
    assert path.read_bytes() == before

    probe = desktop_apply.probe(spec, env=env, record_path=tmp_path / "r.json")
    assert probe.state is DesktopAppState.UNREADABLE
    assert probe.error


# ---------------------------------------------------------- JSON_ARRAY


def test_json_array_merge_appends_exactly_one_element_and_reorders_nothing(tmp_path):
    spec = desktop_app("vscode_copilot")
    existing = [
        {"vendor": "customendpoint", "name": "Team endpoint", "url": "https://a"},
        {"vendor": "customendpoint", "name": "Other", "url": "https://b"},
    ]
    prepare(tmp_path, spec, json.dumps(existing, indent=2) + "\n")
    env = env_for(tmp_path)
    record = tmp_path / "record.json"
    block = {"vendor": "customendpoint", "url": "http://127.0.0.1:8082"}

    desktop_apply.apply(spec, env=env, block=block, scalars={}, record_path=record)
    path = document_path(spec, env)
    after = json.loads(path.read_text(encoding="utf-8"))

    assert len(after) == 3
    assert after[:2] == existing
    assert after[2]["name"] == "My Claude Code"

    desktop_apply.undo(spec, env=env, mode=UndoMode.KEYS_ONLY, record_path=record)
    assert json.loads(path.read_text(encoding="utf-8")) == existing


def test_json_array_merge_creates_the_file_when_it_is_absent(tmp_path):
    spec = desktop_app("vscode_copilot")
    prepare(tmp_path, spec, None)
    env = env_for(tmp_path)

    desktop_apply.apply(
        spec,
        env=env,
        block={"vendor": "customendpoint", "url": "http://127.0.0.1:8082"},
        scalars={},
        record_path=tmp_path / "record.json",
    )
    after = json.loads(document_path(spec, env).read_text(encoding="utf-8"))
    assert [element["name"] for element in after] == ["My Claude Code"]


# ---------------------------------------------------------------- YAML


GOOSE_DOCUMENT = """# Goose configuration
# hand-written, must survive
GOOSE_MODEL: gpt-4o
extensions:
  developer:
    enabled: true
"""


def test_yaml_scalar_edit_preserves_comments_and_nested_blocks(tmp_path):
    spec = desktop_app("goose_desktop")
    prepare(tmp_path, spec, GOOSE_DOCUMENT)
    env = env_for(tmp_path)
    record = tmp_path / "record.json"

    desktop_apply.apply(
        spec,
        env=env,
        block=None,
        scalars={"GOOSE_PROVIDER": "mcc"},
        sidecar_document={"name": "mcc", "api_url": "http://127.0.0.1:8082"},
        record_path=record,
    )
    path = document_path(spec, env)
    text = path.read_text(encoding="utf-8", newline=None)
    assert "# Goose configuration" in text
    assert "  developer:" in text
    assert "GOOSE_PROVIDER: mcc" in text

    sidecar = desktop_apply.sidecar_path_for(spec, env)
    assert sidecar is not None and sidecar.exists()

    desktop_apply.undo(spec, env=env, mode=UndoMode.RESTORE, record_path=record)
    assert path.read_text(encoding="utf-8", newline=None) == GOOSE_DOCUMENT
    assert not sidecar.exists()


# --------------------------------------------------------------- probe


def test_probe_reports_not_installed_when_no_marker_path_exists(tmp_path):
    spec = desktop_app("codex_desktop")
    probe = desktop_apply.probe(
        spec, env=env_for(tmp_path), record_path=tmp_path / "r.json"
    )
    assert probe.state is DesktopAppState.NOT_INSTALLED


def test_probe_reports_drifted_when_an_owned_key_is_hand_edited(tmp_path):
    spec = desktop_app("codex_desktop")
    prepare(tmp_path, spec, CODEX_DOCUMENT)
    env = env_for(tmp_path)
    record = tmp_path / "record.json"
    scalars = {"model_provider": "mcc", "model": "mcc/best"}

    desktop_apply.apply(spec, env=env, block=BLOCK, scalars=scalars, record_path=record)
    assert (
        desktop_apply.probe(
            spec,
            env=env,
            expected_block=BLOCK,
            expected_scalars=scalars,
            record_path=record,
        ).state
        is DesktopAppState.CONFIGURED
    )

    path = document_path(spec, env)
    path.write_text(
        path.read_text(encoding="utf-8", newline=None).replace("8082", "9999"),
        encoding="utf-8",
        newline="",
    )
    assert (
        desktop_apply.probe(
            spec,
            env=env,
            expected_block=BLOCK,
            expected_scalars=scalars,
            record_path=record,
        ).state
        is DesktopAppState.DRIFTED
    )


def test_probe_reports_not_routable_without_looking_at_the_disk(tmp_path):
    """A NOT_ROUTABLE card is a statement, not a measurement of this machine."""

    spec = desktop_app("warp")
    probe = desktop_apply.probe(
        spec, env=env_for(tmp_path), record_path=tmp_path / "r.json"
    )
    assert probe.state is DesktopAppState.NOT_ROUTABLE
    assert probe.document_path == ""


def test_probe_reports_whether_the_token_variable_is_exported(tmp_path):
    """MCC never sets it, so the card has to be able to say whether it took."""

    spec = desktop_app("codex_desktop")
    prepare(tmp_path, spec, CODEX_DOCUMENT)
    env = env_for(tmp_path)
    assert not desktop_apply.probe(
        spec, env=env, record_path=tmp_path / "r.json"
    ).token_env_present
    assert desktop_apply.probe(
        spec, env=env | {"MCC_AUTH_TOKEN": "x"}, record_path=tmp_path / "r.json"
    ).token_env_present


# ---------------------------------------------------------------- plan


def test_plan_writes_nothing_to_disk(tmp_path):
    spec = desktop_app("codex_desktop")
    prepare(tmp_path, spec, CODEX_DOCUMENT)
    env = env_for(tmp_path)
    path = document_path(spec, env)
    before = path.read_bytes()

    plan = desktop_apply.plan(
        spec, env=env, block=BLOCK, scalars={"model_provider": "mcc"}
    )
    assert plan.diff
    assert path.read_bytes() == before
    assert not (tmp_path / ".codex" / "config.toml.mcc-backup").exists()


def test_plan_masks_a_credential_in_the_rendered_diff(tmp_path):
    """The diff is rendered into a browser and a terminal; a key must not be."""

    spec = desktop_app("crush_desktop")
    prepare(tmp_path, spec, "{}\n")
    plan = desktop_apply.plan(
        spec,
        env=env_for(tmp_path),
        block={
            "api_key": "sk-super-secret-value",
            "base_url": "http://127.0.0.1:8082/v1",
        },
        scalars={},
    )
    assert "sk-super-secret-value" not in plan.diff
    assert desktop_apply.MASK in plan.diff


def test_plan_names_the_export_and_the_restart_without_performing_either(tmp_path):
    spec = desktop_app("codex_desktop")
    prepare(tmp_path, spec, CODEX_DOCUMENT)
    plan = desktop_apply.plan(
        spec, env=env_for(tmp_path), block=BLOCK, scalars={"model_provider": "mcc"}
    )
    joined = " ".join(plan.actions)
    assert "MCC_AUTH_TOKEN" in joined
    assert "Restart" in joined


def test_a_second_configure_does_not_bury_the_users_original_value(tmp_path):
    """The bug a browser drive found, and the reason the record is write-once.

    Configure, hand-edit an owned key, Configure again to clear the drift, then
    Restore. If the second Configure had replaced the record, it would have
    stored MCC's own ``mcc/best`` as "what the user had" and Restore would have
    put MCC's value back while reporting success.
    """

    spec = desktop_app("codex_desktop")
    prepare(tmp_path, spec, CODEX_DOCUMENT)
    env = env_for(tmp_path)
    record = tmp_path / "record.json"
    scalars = {"model_provider": "mcc", "model": "mcc/best"}
    path = document_path(spec, env)
    original = path.read_bytes()

    desktop_apply.apply(spec, env=env, block=BLOCK, scalars=scalars, record_path=record)

    # A hand edit to an owned key, exactly what the drift badge reports.
    path.write_text(
        path.read_text(encoding="utf-8", newline=None).replace("8082", "9999"),
        encoding="utf-8",
        newline="",
    )
    desktop_apply.apply(spec, env=env, block=BLOCK, scalars=scalars, record_path=record)

    result = desktop_apply.undo(
        spec, env=env, mode=UndoMode.RESTORE, record_path=record
    )

    assert result.restored_keys == ("model",)
    assert path.read_bytes() == original


def test_the_recorded_hash_tracks_the_latest_write(tmp_path):
    """Otherwise a re-apply would make every later restore refuse."""

    spec = desktop_app("codex_desktop")
    prepare(tmp_path, spec, CODEX_DOCUMENT)
    env = env_for(tmp_path)
    record = tmp_path / "record.json"
    scalars = {"model_provider": "mcc", "model": "mcc/best"}

    desktop_apply.apply(spec, env=env, block=BLOCK, scalars=scalars, record_path=record)
    changed = dict(BLOCK) | {"base_url": "http://127.0.0.1:9999/v1"}
    desktop_apply.apply(
        spec, env=env, block=changed, scalars=scalars, record_path=record
    )

    # No refusal: the document is still the one MCC left.
    desktop_apply.undo(spec, env=env, mode=UndoMode.RESTORE, record_path=record)


def test_a_masked_json_preview_is_still_valid_json(tmp_path):
    """A preview that is not the format it claims to be is not a preview."""

    spec = desktop_app("opencode_desktop")
    prepare(tmp_path, spec, '{"theme": "opencode"}\n')
    plan = desktop_apply.plan(
        spec,
        env=env_for(tmp_path),
        block={
            "options": {
                "baseURL": "http://127.0.0.1:8082/v1",
                "apiKey": "sk-super-secret-value",
            }
        },
        scalars={},
    )

    assert "sk-super-secret-value" not in plan.diff
    added = "\n".join(
        line[1:]
        for line in plan.diff.splitlines()
        if line.startswith(("+", " ")) and not line.startswith("+++")
    )
    json.loads(added)
