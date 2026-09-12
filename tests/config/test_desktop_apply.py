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
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

from my_claude_code.application.catalogue_model import CatalogueModel
from my_claude_code.application.desktop_documents import sidecar_document
from my_claude_code.config import desktop_apply
from my_claude_code.config.desktop_apps import (
    CLAUDE_DESKTOP_CONFIG_ID,
    CLAUDE_DESKTOP_LEGACY_CONFIG_ID,
    DesktopAppState,
    desktop_app,
)
from my_claude_code.config.document_codecs import DocumentFormat, parse_document
from my_claude_code.config.restore_record import UndoMode, read_entry

#: Two routes, enough to make a real owned document for the states below.
#: ``config`` is a leaf and may not import the serialisers; a test may.
SIDECAR_MODELS: tuple[CatalogueModel, ...] = (
    CatalogueModel(
        gateway_id="mcc/best",
        provider_model_ref="mcc/best",
        display_name="MCC best",
        context_length=400_000,
        input_price=3.0,
        output_price=15.0,
    ),
    CatalogueModel(
        gateway_id="mcc/cheap",
        provider_model_ref="mcc/cheap",
        display_name="MCC cheap",
        context_length=200_000,
    ),
)

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
    """Return a scratch environment, PATH included.

    ``PATH`` is part of it since 6.83.0 because detection is now a question
    about the *program*: a row whose marker is an executable is satisfied by
    :func:`make_marker` putting one in ``<home>/bin``, and a row whose markers
    are all paths is unaffected by the variable being present.
    """

    return {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "APPDATA": str(home / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(home / "AppData" / "Local"),
        "PATH": str(home / "bin"),
    }


def make_marker(home, spec) -> Path:
    """Create the one thing that makes this app read as installed.

    Since 6.83.0 that is a *program*: an executable on the PATH the app would
    be started from, or a declared install path. Both are exercised here --
    binaries first, because a row that declares one is a row whose real
    evidence is the binary.

    A glob marker cannot be resolved before it exists -- that is the whole
    point of it, and why ``resolve_path`` answers ``None`` for one that matches
    nothing -- so the pattern is turned into a concrete name here. ``*`` stands
    in for a publisher hash or an extension version, and any value satisfies
    the glob, so the test uses a fixed one.
    """

    env = env_for(home)
    if spec.detect.binaries:
        directory = Path(env["PATH"])
        directory.mkdir(parents=True, exist_ok=True)
        suffix = ".exe" if sys.platform == "win32" else ""
        executable = directory / f"{spec.detect.binaries[0]}{suffix}"
        executable.write_bytes(b"")
        executable.chmod(0o755)
        return executable
    for candidate in spec.detect.markers:
        if candidate.platforms and sys.platform not in candidate.platforms:
            continue
        if not candidate.glob:
            resolved = desktop_apply.resolve_path((candidate,), env)
            if resolved is None:
                continue
            _make_marker_node(resolved)
            return resolved
        base, _directed = desktop_apply._base_directory(candidate, env)
        assert base is not None
        parts = [part.replace("*", "0test0") for part in candidate.relative_parts]
        resolved = base.joinpath(*parts)
        _make_marker_node(resolved)
        return resolved
    raise AssertionError(f"{spec.id} declares no marker for this platform")


def _make_marker_node(path: Path) -> None:
    """Create a marker, as a file where the marker names one and a directory else."""

    if path.suffix:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
        return
    path.mkdir(parents=True, exist_ok=True)


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


def test_the_claude_desktop_repair_happens_once_and_says_so_afterwards(tmp_path):
    """The repair runs once; the sentence about it survives every later poll.

    Until 6.84.0 the note lived for exactly one probe. It is the only place a
    user ever learns that their configuration was silently not working and
    that the app has to be relaunched -- and the dashboard polls every few
    seconds, so the sentence was gone before anybody read it. It is now
    written down beside the restore record and cleared by Undo.
    """

    record = tmp_path / "r.json"
    spec, env, path, _legacy = _legacy_library(tmp_path)

    first = desktop_apply.probe(spec, env=env, record_path=record)
    assert first.repaired
    after_first = path.read_bytes()

    second = desktop_apply.probe(spec, env=env, record_path=record)
    assert second.repaired == first.repaired
    # The repair itself is over: nothing moved a second time.
    assert path.read_bytes() == after_first

    desktop_apply.undo(spec, env=env, record_path=record)
    third = desktop_apply.probe(spec, env=env, record_path=record)
    assert third.repaired == ()


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


# ------------------------------------------- the install gate (spec §3 fix 5)


def test_configure_refuses_when_the_app_is_not_installed(tmp_path):
    """A provider written into a file no program here reads is worse than none.

    There was no gate at all until 6.83.0: a scratch run wrote MCC's element
    into ``chatLanguageModels.json`` while the very same probe reported
    ``not_installed``. Nothing errored, the card went green, and the
    configuration did nothing forever.
    """

    spec = desktop_app("codex_desktop")
    env = env_for(tmp_path)
    # No marker: the document exists, the app does not.
    path = document_path(spec, env)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CODEX_DOCUMENT, encoding="utf-8", newline="")

    with pytest.raises(desktop_apply.DesktopApplyError) as raised:
        desktop_apply.apply(
            spec,
            env=env,
            block=BLOCK,
            scalars={"model_provider": "mcc"},
            record_path=tmp_path / "record.json",
        )

    assert "not installed" in str(raised.value)
    assert path.read_text(encoding="utf-8", newline=None) == CODEX_DOCUMENT
    assert not (tmp_path / "record.json").exists()


def test_a_data_directory_mcc_created_itself_is_not_proof_of_an_install(tmp_path):
    """Detection proves the program (spec §3 fix 15).

    ``%LOCALAPPDATA%\\crush`` and ``%APPDATA%\\Block\\goose`` both read
    *installed* on the machine this was written for, and neither machine had a
    ``crush.exe`` or a ``goose.exe`` anywhere: both directories had been
    created by MCC's own ``mcc-crush.exe`` and ``mcc-goose.exe`` launchers.
    MCC was reading its own footprint as evidence that somebody else's
    application was installed.
    """

    env = env_for(tmp_path)
    for app_id, data_directory in (
        ("crush_desktop", tmp_path / "AppData" / "Local" / "crush"),
        ("goose_desktop", tmp_path / "AppData" / "Roaming" / "Block" / "goose"),
        ("opencode_desktop", tmp_path / ".config" / "opencode"),
        ("commandcode", tmp_path / ".commandcode"),
    ):
        data_directory.mkdir(parents=True, exist_ok=True)
        spec = desktop_app(app_id)
        assert not desktop_apply.is_installed(spec, env), app_id

    # And the program itself still counts.
    crush = desktop_app("crush_desktop")
    make_marker(tmp_path, crush)
    assert desktop_apply.is_installed(crush, env)


def test_the_claude_desktop_configuration_library_is_not_its_own_evidence(tmp_path):
    """The library MCC writes into used to be one of the app's markers."""

    env = env_for(tmp_path)
    spec = desktop_app("claude_desktop")
    # The library MCC itself writes into, wherever this platform keeps it.
    library = document_path(spec, env).parent
    library.mkdir(parents=True, exist_ok=True)
    (library / "_meta.json").write_text("{}", encoding="utf-8", newline="")
    assert not desktop_apply.is_installed(spec, env)

    make_marker(tmp_path, spec)
    assert desktop_apply.is_installed(spec, env)


# ------------------------------- byte-preserving JSON edits (spec §3 fix 6)


SETTINGS_JSON = """{
    // Roo's debug proxy is a capture proxy, not a model endpoint.
    "roo-cline.debugProxy.serverUrl": "http://127.0.0.1:9999",
    "editor.fontSize": 13,
    /* four spaces, the way this user writes JSON */
    "files.exclude": {
        "**/.git": true
    }
}
"""


def test_settings_json_with_comments_is_not_destroyed(tmp_path):
    """VS Code's own settings file is JSONC, and MCC has to merge one key into it."""

    spec = desktop_app("roo_code")
    prepare(tmp_path, spec, SETTINGS_JSON)
    env = env_for(tmp_path)

    desktop_apply.apply(
        spec,
        env=env,
        block=None,
        scalars={"roo-cline.autoImportSettingsPath": "C:/mcc/roo-code-settings.json"},
        sidecar_document={"providerProfiles": {"currentApiConfigName": "MCC"}},
        record_path=tmp_path / "record.json",
    )

    text = document_path(spec, env).read_text(encoding="utf-8", newline=None)
    assert "// Roo's debug proxy is a capture proxy" in text
    assert "/* four spaces, the way this user writes JSON */" in text
    assert '"roo-cline.autoImportSettingsPath": "C:/mcc/roo-code-settings.json"' in text


def test_json_documents_keep_their_own_indentation(tmp_path):
    """Every line MCC does not own comes back byte for byte."""

    spec = desktop_app("roo_code")
    prepare(tmp_path, spec, SETTINGS_JSON)
    env = env_for(tmp_path)

    desktop_apply.apply(
        spec,
        env=env,
        block=None,
        scalars={"roo-cline.autoImportSettingsPath": "C:/mcc/roo-code-settings.json"},
        record_path=tmp_path / "record.json",
    )

    text = document_path(spec, env).read_text(encoding="utf-8", newline=None)
    untouched = [line for line in SETTINGS_JSON.splitlines() if line.strip()]
    after = text.splitlines()
    for line in untouched:
        if line.strip() == "}":
            continue
        assert line in after, line
    assert '    "editor.fontSize": 13,' in after


def test_a_reapply_of_identical_json_leaves_the_file_byte_identical(tmp_path):
    """The idempotence promise, now measured on the bytes and not on a subtree."""

    spec = desktop_app("roo_code")
    prepare(tmp_path, spec, SETTINGS_JSON)
    env = env_for(tmp_path)
    record = tmp_path / "record.json"
    scalars = {"roo-cline.autoImportSettingsPath": "C:/mcc/roo-code-settings.json"}

    desktop_apply.apply(spec, env=env, block=None, scalars=scalars, record_path=record)
    path = document_path(spec, env)
    first = path.read_bytes()

    again = desktop_apply.apply(
        spec, env=env, block=None, scalars=scalars, record_path=record
    )
    assert not again.changed
    assert path.read_bytes() == first

    plan = desktop_apply.plan(spec, env=env, block=None, scalars=scalars)
    assert plan.no_op
    assert plan.diff == ""


def test_a_json_configure_and_undo_return_the_file_to_its_bytes(tmp_path):
    spec = desktop_app("roo_code")
    prepare(tmp_path, spec, SETTINGS_JSON)
    env = env_for(tmp_path)
    record = tmp_path / "record.json"

    desktop_apply.apply(
        spec,
        env=env,
        block=None,
        scalars={"roo-cline.autoImportSettingsPath": "C:/mcc/roo-code-settings.json"},
        record_path=record,
    )
    desktop_apply.undo(spec, env=env, record_path=record)

    path = document_path(spec, env)
    assert path.read_text(encoding="utf-8", newline=None) == SETTINGS_JSON


OPENCODE_JSON = """{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "exa": {
      "type": "local",
      "enabled": true
    }
  }
}
"""


def test_an_opencode_configure_leaves_every_other_key_untouched(tmp_path):
    spec = desktop_app("opencode_desktop")
    prepare(tmp_path, spec, OPENCODE_JSON)
    env = env_for(tmp_path)
    block = {
        "npm": "@ai-sdk/openai-compatible",
        "name": "My Claude Code",
        "options": {
            "baseURL": "http://127.0.0.1:8299/v1",
            "apiKey": "scratch-token",
        },
        "models": {"mcc/best": {"name": "MCC best"}},
    }

    desktop_apply.apply(
        spec, env=env, block=block, record_path=tmp_path / "record.json"
    )
    path = document_path(spec, env)
    text = path.read_text(encoding="utf-8", newline=None)
    assert '"$schema": "https://opencode.ai/config.json",' in text
    assert '      "enabled": true' in text
    assert json.loads(text)["provider"]["mcc"] == block

    desktop_apply.undo(spec, env=env, record_path=tmp_path / "record.json")
    assert path.read_text(encoding="utf-8", newline=None) == OPENCODE_JSON


def test_a_json_plan_masks_the_credential_and_stays_valid_json(tmp_path):
    spec = desktop_app("opencode_desktop")
    prepare(tmp_path, spec, OPENCODE_JSON)
    env = env_for(tmp_path)
    block = {
        "name": "My Claude Code",
        "options": {"baseURL": "http://x/v1", "apiKey": "super-secret-value"},
    }

    plan = desktop_apply.plan(spec, env=env, block=block)
    assert "super-secret-value" not in plan.diff
    assert '"apiKey": "***"' in plan.diff


# ---------------------------- Roo Code writes a real document (spec §3 fix 4)


def test_roo_code_configure_writes_the_sidecar_and_the_import_key(tmp_path):
    """Both halves, which is what "no-op plus collateral damage" was missing.

    Before 6.83.0 this Configure wrote neither the settings key nor the file it
    names; its only effect on disk was to re-indent the user's settings.json.
    """

    spec = desktop_app("roo_code")
    prepare(tmp_path, spec, SETTINGS_JSON)
    env = env_for(tmp_path)
    sidecar = desktop_apply.sidecar_path_for(spec, env)
    assert sidecar is not None
    document = {
        "providerProfiles": {
            "currentApiConfigName": "My Claude Code",
            "apiConfigs": {"My Claude Code": {"apiProvider": "openai"}},
        }
    }

    result = desktop_apply.apply(
        spec,
        env=env,
        block=None,
        scalars={"roo-cline.autoImportSettingsPath": str(sidecar)},
        sidecar_document=document,
        record_path=tmp_path / "record.json",
    )

    assert result.changed
    assert sidecar.exists()
    assert json.loads(sidecar.read_text(encoding="utf-8")) == document
    # The document is JSONC; parse it the way VS Code does.
    settings = parse_document(
        document_path(spec, env).read_text(encoding="utf-8", newline=None),
        DocumentFormat.JSON,
    )
    assert isinstance(settings, Mapping)
    assert settings.get("roo-cline.autoImportSettingsPath") == str(sidecar)

    undone = desktop_apply.undo(spec, env=env, record_path=tmp_path / "record.json")
    assert undone.removed_sidecar
    assert not sidecar.exists()


# ------------------------ the sidecar is compared unmasked (spec §3 fix 7)


def test_a_reapply_is_a_no_op_for_the_app_whose_settings_live_in_a_sidecar(tmp_path):
    """Claude Desktop's preview claimed a change on every single re-apply.

    ``plan`` rendered the sidecar as ``json.dumps(_mask(document))`` and
    compared *that* to the unmasked bytes on disk, so the two could never be
    equal. The drift badge means nothing if a re-apply of identical content is
    a change (spec §4.5).
    """

    spec = desktop_app("claude_desktop")
    prepare(tmp_path, spec, CLAUDE_META)
    env = env_for(tmp_path)
    record = tmp_path / "record.json"
    scalars = {"appliedId": CLAUDE_DESKTOP_CONFIG_ID}

    desktop_apply.apply(
        spec,
        env=env,
        block=CLAUDE_BLOCK,
        scalars=scalars,
        sidecar_document=CLAUDE_SIDECAR,
        record_path=record,
    )

    plan = desktop_apply.plan(
        spec,
        env=env,
        block=CLAUDE_BLOCK,
        scalars=scalars,
        sidecar_document=CLAUDE_SIDECAR,
    )
    assert plan.sidecar_diff == ""
    assert plan.no_op

    again = desktop_apply.apply(
        spec,
        env=env,
        block=CLAUDE_BLOCK,
        scalars=scalars,
        sidecar_document=CLAUDE_SIDECAR,
        record_path=record,
    )
    assert not again.changed


def test_a_changed_sidecar_still_shows_a_diff_with_the_credential_masked(tmp_path):
    spec = desktop_app("claude_desktop")
    prepare(tmp_path, spec, CLAUDE_META)
    env = env_for(tmp_path)
    scalars = {"appliedId": CLAUDE_DESKTOP_CONFIG_ID}

    desktop_apply.apply(
        spec,
        env=env,
        block=CLAUDE_BLOCK,
        scalars=scalars,
        sidecar_document=CLAUDE_SIDECAR,
        record_path=tmp_path / "record.json",
    )
    changed = dict(CLAUDE_SIDECAR) | {"inferenceGatewayBaseUrl": "http://127.0.0.1:9/"}

    plan = desktop_apply.plan(
        spec, env=env, block=CLAUDE_BLOCK, scalars=scalars, sidecar_document=changed
    )
    assert plan.sidecar_diff
    assert not plan.no_op
    assert str(CLAUDE_SIDECAR["inferenceGatewayApiKey"]) not in plan.sidecar_diff
    assert "***" in plan.sidecar_diff


# ------------------------------------------------- fix 8: the credential
# ------------------------------------------------- fix 12: undo vs the app


def test_a_written_document_whose_credential_cannot_resolve_is_not_configured(
    tmp_path,
):
    """The state that used to be reported green while the app got a 401.

    Goose is the row: it reads its credential from a name, out of its own
    secret store, and MCC writes none. Everything MCC owns can be byte-perfect
    and the application still fail on its first request -- so the badge says
    that, instead of hanging "not exported yet" in a details table under
    "Configured by MCC".
    """

    spec = desktop_app("goose_desktop")
    prepare(tmp_path, spec, "")
    env = env_for(tmp_path)
    record = tmp_path / "record.json"
    scalars = {"GOOSE_PROVIDER": "mcc"}
    document = sidecar_document(
        spec, SIDECAR_MODELS, proxy_root_url="http://127.0.0.1:8299"
    )
    assert document is not None

    desktop_apply.apply(
        spec,
        env=env,
        block=None,
        scalars=scalars,
        sidecar_document=document,
        record_path=record,
    )

    unresolved = desktop_apply.probe(
        spec,
        env=env,
        expected_scalars=scalars,
        expected_sidecar=document,
        record_path=record,
    )
    assert unresolved.state is DesktopAppState.CREDENTIAL_UNRESOLVED
    assert unresolved.token_env_present is False

    exported = desktop_apply.probe(
        spec,
        env={**env, spec.token_env_var: "a-scratch-token"},
        expected_scalars=scalars,
        expected_sidecar=document,
        record_path=record,
    )
    assert exported.state is DesktopAppState.CONFIGURED
    assert exported.token_env_present is True


def test_a_variable_mcc_sets_itself_never_reports_the_credential_unresolved():
    """Command Code's reference resolves in the process MCC launches.

    Without this distinction the new badge would paint a working card red on
    every poll, which is the same class of lie as the green one it replaces.
    """

    commandcode = desktop_app("commandcode")
    assert commandcode.token_env_var
    assert commandcode.token_env_var_set_by_mcc is True


def _configured_codex(tmp_path):
    spec = desktop_app("codex_desktop")
    prepare(tmp_path, spec, CODEX_DOCUMENT)
    env = env_for(tmp_path)
    record = tmp_path / "record.json"
    scalars = {"model_provider": "mcc", "model": "mcc/best"}
    desktop_apply.apply(spec, env=env, block=BLOCK, scalars=scalars, record_path=record)
    return spec, env, record, scalars


def test_a_keys_only_undo_leaves_the_card_reading_installed_not_configured(tmp_path):
    """The user asked for this, so the card must not cry foul about it."""

    spec, env, record, scalars = _configured_codex(tmp_path)
    desktop_apply.undo(spec, env=env, record_path=record)

    probe = desktop_apply.probe(
        spec,
        env=env,
        expected_block=BLOCK,
        expected_scalars=scalars,
        record_path=record,
    )
    assert probe.state is DesktopAppState.INSTALLED
    # And the record survives, because the second undo mode needs it.
    assert probe.restorable is True
    stamped = read_entry(spec.id, path=record)
    assert stamped is not None and stamped.undone_at


def test_keys_removed_with_no_undo_read_as_the_application_removing_them(tmp_path):
    """Spec section 2.5's unanswerable question, answered.

    MCC's Codex block was written at 02:20 on a real machine and gone by
    02:36, and nothing anywhere could say whether the user had pressed Undo or
    Codex had rewritten the file. An unstamped record plus a surviving backup
    plus missing keys is the third history, and it now has a badge of its own.
    """

    spec, env, record, scalars = _configured_codex(tmp_path)
    path = document_path(spec, env)
    # The application rewrites its own configuration and drops a table it does
    # not own. No Undo was pressed, so the record is unstamped.
    path.write_text(CODEX_DOCUMENT, encoding="utf-8", newline="")

    probe = desktop_apply.probe(
        spec,
        env=env,
        expected_block=BLOCK,
        expected_scalars=scalars,
        record_path=record,
    )
    assert probe.state is DesktopAppState.REMOVED_BY_APP
    unstamped = read_entry(spec.id, path=record)
    assert unstamped is not None and unstamped.undone_at == ""


def test_a_machine_that_never_pressed_configure_is_installed_not_configured(
    tmp_path,
):
    """The first of the three histories, and the one that must not change."""

    spec = desktop_app("codex_desktop")
    prepare(tmp_path, spec, CODEX_DOCUMENT)
    env = env_for(tmp_path)

    probe = desktop_apply.probe(
        spec, env=env, expected_block=BLOCK, record_path=tmp_path / "record.json"
    )
    assert probe.state is DesktopAppState.INSTALLED


def test_a_second_configure_clears_the_undone_stamp(tmp_path):
    """Undone, then configured again: the record is live again, not stale."""

    spec, env, record, scalars = _configured_codex(tmp_path)
    desktop_apply.undo(spec, env=env, record_path=record)
    stamped = read_entry(spec.id, path=record)
    assert stamped is not None and stamped.undone_at

    desktop_apply.apply(spec, env=env, block=BLOCK, scalars=scalars, record_path=record)
    entry = read_entry(spec.id, path=record)
    assert entry is not None
    assert entry.undone_at == ""
    # And the pre-MCC value is still the user's own, not MCC's -- the record
    # answers "what did the user have before MCC ever wrote here?" and a
    # re-apply must not bury that under yesterday's MCC output.
    prior = {".".join(value.key_path): value.prior_value for value in entry.overwritten}
    assert prior["model"] == "gpt-5.6-luna"
