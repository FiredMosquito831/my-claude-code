"""Editing a file MCC does not own, safely.

Command Code publishes no config-path override, so ``mcc-commandcode`` writes
one key into the user's own ``providers.json``. That is the one place in this
codebase where MCC touches a document it did not create, so each guarantee it
makes is pinned here: one owner, one backup taken before the first edit,
byte-identical non-MCC keys, an idempotent refresh, and a clean removal.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from my_claude_code.config.harness_config_merge import (
    merge_config_path,
    merge_owned_block,
    owned_block,
    owned_block_present,
    remove_owned_block,
    with_base_url,
)
from my_claude_code.config.harnesses import (
    COMMANDCODE_BASE_URL_SENTINEL,
    HarnessConfigMerge,
    harness_spec,
)

OWNED = ("provider", "mcc")
BACKUP = ".mcc-backup"

USER_PROVIDERS: dict[str, Any] = {
    "ollama": {
        "baseURL": "http://127.0.0.1:11434/v1",
        "apiKey": False,
        "models": {"llama3.3:70b": {"contextWindow": 131072}},
    }
}

USER_DOCUMENT: dict[str, Any] = {
    "provider": USER_PROVIDERS,
    "theme": "dark",
}


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def _merge(path: Path, block: dict[str, object]):
    return merge_owned_block(
        path=path, owned_key_path=OWNED, block=block, backup_suffix=BACKUP
    )


def test_the_path_follows_the_clis_own_home_resolution_not_pythons() -> None:
    """Command Code reads HOME first; Path.home() on Windows reads USERPROFILE."""

    merge = HarnessConfigMerge(
        relative_parts=(".commandcode", "providers.json"),
        owned_key_path=OWNED,
        display_path="~/.commandcode/providers.json",
    )
    both = {"HOME": "/home/ada", "USERPROFILE": "C:\\Users\\Ada"}

    assert merge_config_path(merge, both) == Path("/home/ada").joinpath(
        ".commandcode", "providers.json"
    )
    assert merge_config_path(merge, {"USERPROFILE": "C:\\Users\\Ada"}) == Path(
        "C:\\Users\\Ada"
    ).joinpath(".commandcode", "providers.json")


def test_an_empty_home_variable_is_not_a_home_directory() -> None:
    merge = harness_spec("commandcode_cli").catalogue
    assert merge is not None and merge.merge is not None
    resolved = merge_config_path(merge.merge, {"HOME": "  ", "USERPROFILE": "/u/ada"})
    assert resolved == Path("/u/ada").joinpath(".commandcode", "providers.json")


def test_every_key_mcc_did_not_write_survives_byte_for_byte(tmp_path: Path) -> None:
    path = tmp_path / "providers.json"
    _write(path, USER_DOCUMENT)

    _merge(path, {"api": "anthropic-messages", "models": {"a/b": {}}})

    after = _read(path)
    assert after["theme"] == "dark"
    assert after["provider"]["ollama"] == USER_PROVIDERS["ollama"]
    assert after["provider"]["mcc"]["api"] == "anthropic-messages"


def test_the_backup_is_the_users_file_not_yesterdays_mcc_output(
    tmp_path: Path,
) -> None:
    path = tmp_path / "providers.json"
    _write(path, USER_DOCUMENT)
    original = path.read_bytes()

    first = _merge(path, {"models": {"a/b": {}}})
    second = _merge(path, {"models": {"a/c": {}}})

    assert first.backup_path == path.with_name(path.name + BACKUP)
    assert second.backup_path == first.backup_path
    # Taken once, before the first edit, and never refreshed afterwards.
    assert first.backup_path.read_bytes() == original


def test_a_file_that_did_not_exist_is_created_without_a_backup(tmp_path: Path) -> None:
    path = tmp_path / "providers.json"

    result = _merge(path, {"models": {"a/b": {}}})

    assert result.created is True
    assert result.changed is True
    assert result.backup_path is None
    assert not path.with_name(path.name + BACKUP).exists()


def test_an_unchanged_refresh_writes_nothing_and_backs_nothing_up(
    tmp_path: Path,
) -> None:
    """A provider refresh that resolves the same numbers must not touch the file."""

    path = tmp_path / "providers.json"
    _write(path, USER_DOCUMENT)
    block: dict[str, object] = {"models": {"a/b": {}}}
    _merge(path, block)
    stamp = path.stat().st_mtime_ns
    before = path.read_bytes()

    result = _merge(path, block)

    assert result.changed is False
    assert result.backup_path is None
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == stamp


def test_a_document_that_is_not_json_is_replaced_rather_than_appended_to(
    tmp_path: Path,
) -> None:
    """A broken file is not a document MCC can merge into; it is backed up first."""

    path = tmp_path / "providers.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")

    result = _merge(path, {"models": {"a/b": {}}})

    assert result.changed is True
    assert result.backup_path is not None
    assert result.backup_path.read_text(encoding="utf-8") == "{ not json"
    assert "mcc" in _read(path)["provider"]


def test_disconnect_removes_only_mccs_key(tmp_path: Path) -> None:
    path = tmp_path / "providers.json"
    _write(path, USER_DOCUMENT)
    _merge(path, {"models": {"a/b": {}}})

    result = remove_owned_block(path=path, owned_key_path=OWNED, backup_suffix=BACKUP)

    assert result.changed is True
    after = _read(path)
    assert after["provider"] == USER_PROVIDERS
    assert after["theme"] == "dark"


def test_disconnect_on_a_document_mcc_never_touched_changes_nothing(
    tmp_path: Path,
) -> None:
    path = tmp_path / "providers.json"
    _write(path, USER_DOCUMENT)
    before = path.read_bytes()

    result = remove_owned_block(path=path, owned_key_path=OWNED, backup_suffix=BACKUP)

    assert result.changed is False
    assert path.read_bytes() == before
    assert not path.with_name(path.name + BACKUP).exists()


def test_presence_is_tested_on_mccs_key_never_on_the_file(tmp_path: Path) -> None:
    """The user's file existing must never make the refresh write into it."""

    path = tmp_path / "providers.json"
    _write(path, USER_DOCUMENT)

    assert owned_block_present(path, OWNED) is False
    _merge(path, {"models": {"a/b": {}}})
    assert owned_block_present(path, OWNED) is True


def test_the_defaulted_record_is_folded_inside_the_owned_key() -> None:
    """Removing MCC's key must remove every trace of MCC, the record included."""

    document = {
        "provider": {"mcc": {"api": "anthropic-messages"}},
        "_mcc_defaulted": {"a/b": ["maxOutput"]},
    }

    block = owned_block(document, OWNED)

    assert block["_mcc_defaulted"] == {"a/b": ["maxOutput"]}


def test_a_document_without_the_owned_key_is_a_hard_error() -> None:
    with pytest.raises(ValueError):
        owned_block({"provider": {}}, OWNED)


def test_the_base_url_sentinel_is_replaced_and_normalised() -> None:
    block = {"baseURL": COMMANDCODE_BASE_URL_SENTINEL}

    assert with_base_url(block, "http://127.0.0.1:8199")["baseURL"] == (
        "http://127.0.0.1:8199/v1"
    )
    assert with_base_url(block, "http://127.0.0.1:8199/v1/")["baseURL"] == (
        "http://127.0.0.1:8199/v1"
    )


def test_a_base_url_that_is_not_the_sentinel_is_left_alone() -> None:
    block = {"baseURL": "http://elsewhere/v1"}

    assert with_base_url(block, "http://127.0.0.1:8199") == block


def test_a_users_own_indentation_is_not_reformatted_on_an_unchanged_refresh(
    tmp_path: Path,
) -> None:
    """The idempotency check is on MCC's key, not on the document's bytes.

    A four-space or tab-indented file would otherwise be rewritten into the
    atomic writer's canonical two-space shape on every single launch -- a
    whole-file diff caused by a refresh that changed nothing.
    """

    path = tmp_path / "providers.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    block: dict[str, object] = {"models": {"a/b": {}}}
    document = {
        **USER_DOCUMENT,
        "provider": {**USER_PROVIDERS, "mcc": block},
    }
    path.write_text(json.dumps(document, indent=4) + "\n", encoding="utf-8")
    before = path.read_bytes()

    result = _merge(path, block)

    assert result.changed is False
    assert path.read_bytes() == before
