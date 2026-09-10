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
import re
from pathlib import Path

import pytest

from my_claude_code.config import desktop_apply
from my_claude_code.config.desktop_apps import (
    CLAUDE_DESKTOP_CONFIG_ID,
    CLAUDE_DESKTOP_LEGACY_CONFIG_ID,
    DesktopAppState,
    desktop_app,
)
from my_claude_code.config.restore_record import UndoMode

#: Claude Desktop 1.46388.4.0's own validator for a configuration-library id,
#: extracted from ``app.asar`` -> ``.vite/build/index.chunk--WuAOADe.js`` line
#: 35967 (``var vAe = /^[a-f0-9-]{36}$/;``) and applied at boot, in ``$je``
#: (:36626), *before* the document is read. This regex is the reason this file
#: no longer asserts a value MCC chose: for three releases it asserted exactly
#: the string that fails it.
CLAUDE_DESKTOP_ID_RULE = re.compile(r"^[a-f0-9-]{36}$")

BLOCK: dict[str, object] = {
    "name": "My Claude Code",
    "base_url": "http://127.0.0.1:8082/v1",
    "experimental_bearer_token": "scratch-token",
    "wire_api": "responses",
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


def make_marker(home, spec) -> Path:
    """Create one directory that makes this app read as installed.

    A glob marker cannot be resolved before it exists -- that is the whole
    point of it, and why ``resolve_path`` answers ``None`` for one that matches
    nothing -- so the pattern is turned into a concrete name here. ``*`` stands
    in for a publisher hash or an extension version, and any value satisfies
    the glob, so the test uses a fixed one.
    """

    env = env_for(home)
    for candidate in spec.detect.markers:
        if candidate.platforms and "win32" not in candidate.platforms:
            continue
        if not candidate.glob:
            resolved = desktop_apply.resolve_path((candidate,), env)
            if resolved is None:
                continue
            resolved.mkdir(parents=True, exist_ok=True)
            return resolved
        base, _directed = desktop_apply._base_directory(candidate, env)
        assert base is not None
        parts = [part.replace("*", "0test0") for part in candidate.relative_parts]
        resolved = base.joinpath(*parts)
        resolved.mkdir(parents=True, exist_ok=True)
        return resolved
    raise AssertionError(f"{spec.id} declares no marker for this platform")


def prepare(home, spec, contents: str | None) -> None:
    """Create the app's marker directory, and its document when given one."""

    env = env_for(home)
    make_marker(home, spec)
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
    result = desktop_apply.undo(
        spec, env=env, mode=UndoMode.KEYS_ONLY, record_path=record
    )

    text = document_path(spec, env).read_text(encoding="utf-8", newline=None)
    assert "model_providers.mcc" not in text
    # ``model_provider`` did not exist before MCC, so it is deleted.
    assert "model_provider" not in text
    # ``model`` did, and MCC overwrote it. Keys-only puts it back: the mode
    # says "remove MCC's keys", and this was never one of MCC's keys. Before
    # 6.56.0 this line was deleted outright and the record was then emptied,
    # so the value was unrecoverable through the UI.
    assert 'model = "gpt-5.6-luna"' in text
    assert result.restored_keys == ("model",)
    assert "# a comment the user wrote" in text
    assert '[projects."C:/work"]' in text


def test_undo_keys_only_keeps_the_record_so_restore_still_works(tmp_path):
    """The record answers a question that survives an undo."""

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

    from my_claude_code.config.restore_record import read_entry

    entry = read_entry(spec.id, path=record)
    assert entry is not None
    assert any(
        value.key_path == ("model",) and value.prior_value == "gpt-5.6-luna"
        for value in entry.overwritten
    )
    # Before 6.56.0 this was ``{"subjects": []}`` and the pre-MCC value existed
    # nowhere the UI could reach. The record is what makes a later Configure
    # remember the *first* answer rather than MCC's own, so keeping it is not
    # bookkeeping: it is the only surviving copy of what the user had.


@pytest.mark.parametrize(
    "app_id, key",
    [
        ("codex_desktop", "model"),
        ("goose_desktop", "GOOSE_PROVIDER"),
        ("antigravity", "modelProvider"),
        ("roo_code", "roo-cline.autoImportSettingsPath"),
    ],
)
def test_keys_only_undo_restores_every_apps_overwritten_value(tmp_path, app_id, key):
    """The regression, for every app that declares ``overwritten_keys``.

    One shape per format -- TOML, YAML, JSON, and JSON with a dotted key that
    is a single literal key rather than a path -- because the delete that lost
    the value lived in two different branches of ``undo``.
    """

    spec = desktop_app(app_id)
    prepare(tmp_path, spec, None)
    env = env_for(tmp_path)
    record = tmp_path / "record.json"
    path = document_path(spec, env)
    path.parent.mkdir(parents=True, exist_ok=True)

    assert spec.document is not None
    if spec.document.document_format.value == "toml":
        path.write_text(f'{key} = "mine"\n', encoding="utf-8", newline="")
    elif spec.document.document_format.value == "yaml":
        path.write_text(f"{key}: mine\n", encoding="utf-8", newline="")
    else:
        path.write_text(json.dumps({key: "mine"}, indent=2) + "\n", encoding="utf-8")

    desktop_apply.apply(
        spec, env=env, block=None, scalars={key: "mcc"}, record_path=record
    )
    assert "mcc" in path.read_text(encoding="utf-8", newline=None)

    result = desktop_apply.undo(
        spec, env=env, mode=UndoMode.KEYS_ONLY, record_path=record
    )

    assert key in result.restored_keys
    assert "mine" in path.read_text(encoding="utf-8", newline=None)


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
    """MCC never sets it, so the card has to be able to say whether it took.

    Asked of Goose, which is the kind of app the question is still *for*: one
    that reads its credential from the environment or a keyring and has no
    field MCC could write. Codex used to be the subject here, and is no longer
    -- it names no variable at all since 6.67.0, because the variable it named
    was one nothing ever set.
    """

    spec = desktop_app("goose_desktop")
    make_marker(tmp_path, spec)
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


def test_plan_names_the_restart_without_performing_it(tmp_path):
    spec = desktop_app("codex_desktop")
    prepare(tmp_path, spec, CODEX_DOCUMENT)
    plan = desktop_apply.plan(
        spec, env=env_for(tmp_path), block=BLOCK, scalars={"model_provider": "mcc"}
    )
    joined = " ".join(plan.actions)
    assert "Restart" in joined
    # And *not* an export. Codex takes the literal now, so telling the reader
    # to export a variable would be an instruction to do something with no
    # effect -- which is the whole of the bug this release fixes, restated as
    # advice.
    assert "Export" not in joined


def test_plan_names_the_export_for_an_app_that_really_reads_one(tmp_path):
    spec = desktop_app("goose_desktop")
    make_marker(tmp_path, spec)
    plan = desktop_apply.plan(spec, env=env_for(tmp_path), block=None, scalars={})
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


# ------------------------------------------------- Claude Desktop

CLAUDE_META = (
    json.dumps(
        {
            "appliedId": "3fd258a0-0379-416e-b3b5-0b72a6ac5392",
            "entries": [
                {"id": "3fd258a0-0379-416e-b3b5-0b72a6ac5392", "name": "My own gateway"}
            ],
        },
        indent=2,
    )
    + "\n"
)

CLAUDE_BLOCK: dict[str, object] = {"name": "My Claude Code (MCC)"}
CLAUDE_SIDECAR: dict[str, object] = {
    "inferenceProvider": "gateway",
    "inferenceGatewayBaseUrl": "http://127.0.0.1:8082",
    "inferenceGatewayApiKey": "scratch-token",
    "inferenceCredentialKind": "static",
    "modelDiscoveryEnabled": False,
    "inferenceModels": [
        {
            "name": "mcc/best",
            "labelOverride": "Best",
            "supports1m": False,
            "prefer1m": False,
            "anthropicFamilyTier": "opus",
            "isFamilyDefault": True,
        }
    ],
}


def test_the_claude_desktop_entry_id_satisfies_the_apps_own_boot_regex():
    """The one assertion that would have prevented the bug report.

    Not "MCC writes the id MCC declares" -- that was asserted for three
    releases and was true the whole time. This asserts the rule the
    *application* applies, taken from its own shipped bundle: a configuration
    library id that fails it makes Claude Desktop discard its entire local
    configuration tier at boot, silently, including whatever the user set up by
    hand. The filename MCC owns has to satisfy it too, since the loader builds
    the path from the id.
    """

    assert CLAUDE_DESKTOP_ID_RULE.match(CLAUDE_DESKTOP_CONFIG_ID)
    spec = desktop_app("claude_desktop")
    assert spec.document is not None
    assert spec.document.match_value == CLAUDE_DESKTOP_CONFIG_ID
    assert spec.sidecar is not None
    for path in spec.sidecar.paths:
        stem = Path(path.relative_parts[-1]).stem
        assert CLAUDE_DESKTOP_ID_RULE.match(stem)

    # And the value that broke it does not, so the rule is doing work.
    assert not CLAUDE_DESKTOP_ID_RULE.match(CLAUDE_DESKTOP_LEGACY_CONFIG_ID)


def _legacy_library(tmp_path):
    """Return a library in the state 6.56.0-6.66.1 left on a real machine."""

    spec = desktop_app("claude_desktop")
    prepare(tmp_path, spec, CLAUDE_META)
    env = env_for(tmp_path)
    path = document_path(spec, env)
    meta = json.loads(path.read_text(encoding="utf-8"))
    meta["appliedId"] = CLAUDE_DESKTOP_LEGACY_CONFIG_ID
    meta["entries"].append(
        {"id": CLAUDE_DESKTOP_LEGACY_CONFIG_ID, "name": "My Claude Code (MCC)"}
    )
    path.write_text(json.dumps(meta, indent=2), encoding="utf-8", newline="")
    legacy_file = path.parent / f"{CLAUDE_DESKTOP_LEGACY_CONFIG_ID}.json"
    legacy_file.write_text(
        json.dumps(CLAUDE_SIDECAR, indent=2), encoding="utf-8", newline=""
    )
    return spec, env, path, legacy_file


def test_a_legacy_claude_desktop_entry_is_repaired_on_probe(tmp_path):
    """Existing users are all in the broken state, and none of them will press
    Configure again -- their card told them it was already configured."""

    spec, env, path, legacy_file = _legacy_library(tmp_path)

    probe = desktop_apply.probe(spec, env=env, record_path=tmp_path / "r.json")

    assert probe.repaired
    assert not legacy_file.exists()
    meta = json.loads(path.read_text(encoding="utf-8"))
    assert meta["appliedId"] == CLAUDE_DESKTOP_CONFIG_ID
    assert CLAUDE_DESKTOP_ID_RULE.match(meta["appliedId"])
    ids = [entry["id"] for entry in meta["entries"]]
    assert CLAUDE_DESKTOP_LEGACY_CONFIG_ID not in ids
    # The user's own entry is untouched, which is the thing the original bug
    # took away from them.
    assert "3fd258a0-0379-416e-b3b5-0b72a6ac5392" in ids
    # And MCC's is renamed rather than dropped: ``entries`` is the app's
    # configuration picker, and an applied configuration missing from it is one
    # the user can neither see nor switch away from.
    renamed = next(
        entry for entry in meta["entries"] if entry["id"] == CLAUDE_DESKTOP_CONFIG_ID
    )
    assert renamed["name"] == "My Claude Code (MCC)"

    # MCC's configuration is not lost, only renamed: the content moved to the
    # id the app accepts, so the next launch works without pressing anything.
    moved = desktop_apply.sidecar_path_for(spec, env)
    assert moved is not None
    assert json.loads(moved.read_text(encoding="utf-8")) == CLAUDE_SIDECAR


def test_the_claude_desktop_repair_happens_once(tmp_path):
    spec, env, path, _legacy = _legacy_library(tmp_path)

    assert desktop_apply.probe(spec, env=env, record_path=tmp_path / "r.json").repaired
    after_first = path.read_bytes()
    second = desktop_apply.probe(spec, env=env, record_path=tmp_path / "r.json")

    assert second.repaired == ()
    assert path.read_bytes() == after_first


def test_an_already_repaired_claude_desktop_library_is_left_alone(tmp_path):
    """The state on the machine this was written for.

    The user repaired it by hand on 2026-09-09: they deleted MCC's file and put
    ``appliedId`` back on their own entry. A repair that "helped" here would be
    a second incident, so nothing may move unless MCC's own legacy id is
    actually found.
    """

    spec = desktop_app("claude_desktop")
    prepare(tmp_path, spec, CLAUDE_META)
    env = env_for(tmp_path)
    path = document_path(spec, env)
    before = path.read_bytes()

    probe = desktop_apply.probe(spec, env=env, record_path=tmp_path / "r.json")

    assert probe.repaired == ()
    assert path.read_bytes() == before
    sidecar = desktop_apply.sidecar_path_for(spec, env)
    assert sidecar is not None
    assert not sidecar.exists()


def test_the_whole_library_is_backed_up_before_the_first_edit(tmp_path, monkeypatch):
    """A per-file backup of an index cannot restore a directory of documents."""

    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path / "mcc"))
    spec = desktop_app("claude_desktop")
    prepare(tmp_path, spec, CLAUDE_META)
    env = env_for(tmp_path)

    desktop_apply.apply(
        spec,
        env=env,
        block=CLAUDE_BLOCK,
        scalars={"appliedId": CLAUDE_DESKTOP_CONFIG_ID},
        sidecar_document=CLAUDE_SIDECAR,
        record_path=tmp_path / "record.json",
    )

    backups = sorted((tmp_path / "mcc" / "backups").glob("claude_desktop-*"))
    assert len(backups) == 1
    copied = json.loads((backups[0] / "_meta.json").read_text(encoding="utf-8"))
    assert copied["appliedId"] == "3fd258a0-0379-416e-b3b5-0b72a6ac5392"

    # And a second Configure does not overwrite the copy taken before the
    # first, which is the same promise the per-file backup makes.
    desktop_apply.apply(
        spec,
        env=env,
        block=CLAUDE_BLOCK,
        scalars={"appliedId": CLAUDE_DESKTOP_CONFIG_ID},
        sidecar_document=dict(CLAUDE_SIDECAR) | {"modelDiscoveryEnabled": True},
        record_path=tmp_path / "record.json",
    )
    assert sorted((tmp_path / "mcc" / "backups").glob("claude_desktop-*")) == backups


def test_claude_desktop_owns_a_file_and_merges_exactly_one_foreign_key(tmp_path):
    """The shape read off this machine: a document library plus its index.

    MCC writes a whole document of its own and touches ``_meta.json`` in two
    places -- ``appliedId``, which is what makes the app load it, and one
    element of ``entries``. Every configuration the user authored survives.
    """

    spec = desktop_app("claude_desktop")
    prepare(tmp_path, spec, CLAUDE_META)
    env = env_for(tmp_path)
    record = tmp_path / "record.json"

    result = desktop_apply.apply(
        spec,
        env=env,
        block=CLAUDE_BLOCK,
        scalars={"appliedId": CLAUDE_DESKTOP_CONFIG_ID},
        sidecar_document=CLAUDE_SIDECAR,
        record_path=record,
    )

    meta = json.loads(document_path(spec, env).read_text(encoding="utf-8"))
    assert meta["appliedId"] == CLAUDE_DESKTOP_CONFIG_ID
    names = [entry["name"] for entry in meta["entries"]]
    assert "My own gateway" in names
    assert "My Claude Code (MCC)" in names
    assert len(meta["entries"]) == 2

    sidecar = desktop_apply.sidecar_path_for(spec, env)
    assert sidecar is not None
    assert json.loads(sidecar.read_text(encoding="utf-8")) == CLAUDE_SIDECAR
    assert result.changed


def test_claude_desktop_undo_puts_the_users_own_configuration_back(tmp_path):
    spec = desktop_app("claude_desktop")
    prepare(tmp_path, spec, CLAUDE_META)
    env = env_for(tmp_path)
    record = tmp_path / "record.json"
    path = document_path(spec, env)
    original = path.read_bytes()

    desktop_apply.apply(
        spec,
        env=env,
        block=CLAUDE_BLOCK,
        scalars={"appliedId": CLAUDE_DESKTOP_CONFIG_ID},
        sidecar_document=CLAUDE_SIDECAR,
        record_path=record,
    )
    result = desktop_apply.undo(
        spec, env=env, mode=UndoMode.RESTORE, record_path=record
    )

    assert path.read_bytes() == original
    assert result.removed_sidecar
    sidecar = desktop_apply.sidecar_path_for(spec, env)
    assert sidecar is not None
    assert not sidecar.exists()


def test_claude_desktop_keys_only_undo_also_restores_the_applied_id(tmp_path):
    """The user's applied configuration is a value MCC replaced, not one it made."""

    spec = desktop_app("claude_desktop")
    prepare(tmp_path, spec, CLAUDE_META)
    env = env_for(tmp_path)
    record = tmp_path / "record.json"

    desktop_apply.apply(
        spec,
        env=env,
        block=CLAUDE_BLOCK,
        scalars={"appliedId": CLAUDE_DESKTOP_CONFIG_ID},
        sidecar_document=CLAUDE_SIDECAR,
        record_path=record,
    )
    result = desktop_apply.undo(
        spec, env=env, mode=UndoMode.KEYS_ONLY, record_path=record
    )

    meta = json.loads(document_path(spec, env).read_text(encoding="utf-8"))
    assert meta["appliedId"] == "3fd258a0-0379-416e-b3b5-0b72a6ac5392"
    assert result.restored_keys == ("appliedId",)


def test_a_policy_key_disables_configure_and_the_probe_says_why(tmp_path, monkeypatch):
    """The local library is the lowest-precedence source Claude Desktop reads.

    A managed profile replaces it wholesale and makes the app's own
    configuration window read-only, so a Configure that wrote the library
    anyway would leave a file that does nothing and a card claiming otherwise.
    """

    spec = desktop_app("claude_desktop")
    prepare(tmp_path, spec, CLAUDE_META)
    env = env_for(tmp_path)

    monkeypatch.setattr(
        desktop_apply,
        "_registry_value_names",
        lambda hive, subkey: ("inferenceProvider",) if hive == "HKLM" else (),
    )

    probe = desktop_apply.probe(spec, env=env)
    assert probe.state is DesktopAppState.MANAGED
    assert "Machine policy" in probe.managed_by
    assert probe.managed_keys == ("inferenceProvider",)

    with pytest.raises(desktop_apply.DesktopApplyError) as failure:
        desktop_apply.apply(
            spec,
            env=env,
            block=CLAUDE_BLOCK,
            scalars={},
            sidecar_document=CLAUDE_SIDECAR,
        )
    assert "outranks the file MCC writes" in str(failure.value)


def test_an_update_only_policy_leaves_configure_available(tmp_path, monkeypatch):
    """Anthropic documents an app-behavior key group that does not take over.

    A fleet may pin an update policy without managing the whole configuration,
    and on those devices the locally authored config still applies -- so MCC's
    button has to stay live.
    """

    spec = desktop_app("claude_desktop")
    prepare(tmp_path, spec, CLAUDE_META)
    env = env_for(tmp_path)

    monkeypatch.setattr(
        desktop_apply,
        "_registry_value_names",
        lambda hive, subkey: ("disableAutoUpdates", "egressProxyUrl"),
    )

    assert desktop_apply.managed_override(spec, env) == ("", ())
    assert desktop_apply.probe(spec, env=env).state is not DesktopAppState.MANAGED


def test_a_json_document_written_without_a_trailing_newline_keeps_none(tmp_path):
    """A Configure/Undo cycle has to end exactly where it started.

    Claude Desktop writes its own ``_meta.json`` with no trailing newline --
    read off this machine on 2026-09-07 -- and MCC's JSON writer appended one,
    so the cycle finished one byte away from the original. Every other
    normalisation an object document suffers is the format's price; this one
    was avoidable.
    """

    spec = desktop_app("claude_desktop")
    prepare(tmp_path, spec, CLAUDE_META.rstrip("\n"))
    env = env_for(tmp_path)
    record = tmp_path / "record.json"
    path = document_path(spec, env)
    original = path.read_bytes()
    assert not original.endswith(b"\n")

    desktop_apply.apply(
        spec,
        env=env,
        block=CLAUDE_BLOCK,
        scalars={"appliedId": CLAUDE_DESKTOP_CONFIG_ID},
        sidecar_document=CLAUDE_SIDECAR,
        record_path=record,
    )
    assert not path.read_bytes().endswith(b"\n")

    desktop_apply.undo(spec, env=env, mode=UndoMode.KEYS_ONLY, record_path=record)
    assert path.read_bytes() == original


def test_an_untouched_configuration_library_reads_as_installed_not_drifted(tmp_path):
    """ "Drifted" is a claim that MCC wrote here and something changed it.

    Claude Desktop's footprint is an element of ``entries`` plus the
    ``appliedId`` pointing at it -- not the scalar alone. Judging it by the
    scalar, as Goose and Antigravity are judged, made a library holding only
    the user's own configuration report ``drifted`` on a machine MCC had never
    written to. Caught on the scratch server before this shipped.
    """

    spec = desktop_app("claude_desktop")
    prepare(tmp_path, spec, CLAUDE_META)
    env = env_for(tmp_path)

    probe = desktop_apply.probe(
        spec,
        env=env,
        expected_block=CLAUDE_BLOCK,
        expected_scalars={"appliedId": CLAUDE_DESKTOP_CONFIG_ID},
        expected_sidecar=CLAUDE_SIDECAR,
    )

    assert probe.state is DesktopAppState.INSTALLED
