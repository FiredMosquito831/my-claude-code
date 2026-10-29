"""OpenClaw's Configure/Undo: one block in, the same bytes back out.

``~/.openclaw/openclaw.json`` is the second file MCC writes into that the user
owns, and the first one that is meant to be hand-edited: OpenClaw reads JSON5,
so it can carry comments, its author's indentation and a trailing comma. The
promise is ``config/document_codecs``': every byte MCC does not own comes back
byte for byte, Configure touches one block and one recorded value, and Undo --
either mode -- returns the document to exactly what it was.

The seeds below are the shapes a hand-written document actually takes:
canonical two-space, four-space with comments, CRLF, a trailing comma, no
trailing newline, and a file that does not exist at all.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from my_claude_code.application.catalogue_model import CatalogueModel
from my_claude_code.application.catalogues import serialise
from my_claude_code.config.document_codecs import parse_jsonc
from my_claude_code.config.harness_config_merge import (
    MergeDocumentError,
    configure_owned_block,
    merge_config_path,
    owned_block_names,
    read_text_document,
    unconfigure_owned_block,
)
from my_claude_code.config.harnesses import HarnessConfigMerge, harness_spec
from my_claude_code.config.restore_record import UndoMode, read_entry

SUBJECT = "openclaw"
ROOT = "http://127.0.0.1:8082"


def _merge() -> HarnessConfigMerge:
    catalogue = harness_spec(SUBJECT).catalogue
    assert catalogue is not None and catalogue.merge is not None
    return catalogue.merge


def _document() -> dict[str, Any]:
    models = [
        CatalogueModel(
            gateway_id="anthropic/openrouter/configured",
            provider_model_ref="openrouter/configured",
            display_name="Configured",
            context_length=400_000,
            max_output_tokens=32_000,
            supports_vision=True,
            input_price=1.25,
            output_price=10.0,
            is_primary_route=True,
        ),
        CatalogueModel(
            gateway_id="anthropic/openrouter/other",
            provider_model_ref="openrouter/other",
            display_name="Other",
            context_length=128_000,
        ),
    ]
    document, _defaulted = serialise("openclaw", models)
    return document


#: The document shapes a user's own openclaw.json takes. Each is a whole file.
SEEDS: dict[str, str] = {
    "canonical": (
        '{\n  "channels": {\n    "telegram": {"token": "x"}\n  },\n'
        '  "logging": {"level": "info"}\n}\n'
    ),
    "comments": (
        "// my openclaw config\n"
        "{\n"
        '  "channels": {\n'
        '    "telegram": { "token": "x" } // the bot\n'
        "  },\n"
        '  "logging": {"level": "info"},\n'
        "}\n"
    ),
    "four-space": (
        '{\n    "channels": {"telegram": {"token": "x"}},\n    "logging": {"level": "info"}\n}\n'
    ),
    "crlf": (
        '{\r\n  "channels": {"telegram": {"token": "x"}},\r\n  "logging": {"level": "info"}\r\n}\r\n'
    ),
    "no-trailing-newline": '{\n  "logging": {"level": "info"}\n}',
    "existing-string-default": (
        '{\n  "agents": {"defaults": {"model": "openai/gpt-5"}},\n'
        '  "logging": {"level": "info"}\n}\n'
    ),
    "existing-object-default": (
        "{\n"
        '  "agents": {"defaults": {"model": {"primary": "openai/gpt-5",'
        ' "fallbacks": ["openai/gpt-5-mini"]}}},\n'
        '  "logging": {"level": "info"}\n}\n'
    ),
}


def _parsed(text: str) -> dict[str, Any]:
    """Parse a JSON5 document for an assertion, with the type narrowed."""

    parsed = parse_jsonc(text)
    assert isinstance(parsed, dict)
    return {str(key): value for key, value in parsed.items()}


def _configured(seed: str, tmp_path: Path) -> tuple[Path, Path]:
    path = tmp_path / "openclaw.json"
    path.write_text(seed, encoding="utf-8", newline="")
    record = tmp_path / "restore-record.json"
    configure_owned_block(
        merge=_merge(),
        path=path,
        document=_document(),
        proxy_root_url=ROOT,
        subject=SUBJECT,
        record_path=record,
    )
    return path, record


@pytest.mark.parametrize("seed_name", sorted(SEEDS))
def test_undo_gives_every_seed_back_byte_for_byte(seed_name: str, tmp_path) -> None:
    """The whole promise, per document shape: Configure then Undo, same bytes."""

    seed = SEEDS[seed_name]
    path, record = _configured(seed, tmp_path)
    after_configure = path.read_text(encoding="utf-8")

    assert after_configure != seed, "Configure must actually have written"
    assert "models" in after_configure

    result = unconfigure_owned_block(
        merge=_merge(),
        path=path,
        subject=SUBJECT,
        mode=UndoMode.KEYS_ONLY,
        record_path=record,
        proxy_root_url=ROOT,
    )

    assert result.changed is True
    assert path.read_bytes().decode("utf-8") == seed, seed_name


@pytest.mark.parametrize("seed_name", sorted(SEEDS))
def test_restore_mode_returns_the_same_bytes_and_consumes_the_record(
    seed_name: str, tmp_path
) -> None:
    seed = SEEDS[seed_name]
    path, record = _configured(seed, tmp_path)

    unconfigure_owned_block(
        merge=_merge(),
        path=path,
        subject=SUBJECT,
        mode=UndoMode.RESTORE,
        record_path=record,
        proxy_root_url=ROOT,
    )

    assert path.read_bytes().decode("utf-8") == seed
    assert read_entry(SUBJECT, path=record) is None


def test_only_mccs_block_and_the_default_model_change(tmp_path) -> None:
    """Everything else -- comments, spacing, key order -- is the user's."""

    seed = SEEDS["comments"]
    path, _record = _configured(seed, tmp_path)

    text = path.read_text(encoding="utf-8")
    assert "// my openclaw config" in text
    assert "// the bot" in text
    document = _parsed(text)
    assert document["logging"]["level"] == "info"
    assert document["models"]["providers"]["mcc"]["baseUrl"] == f"{ROOT}/v1"
    assert document["models"]["providers"]["mcc"]["apiKey"] == ("$MCC_OPENCLAW_API_KEY")
    assert document["agents"]["defaults"]["model"]["primary"] == (
        "openrouter/configured"
    )


def test_the_users_default_model_is_replaced_and_put_back(tmp_path) -> None:
    """A string becomes ``{"primary": …}``; Undo writes the string's own bytes.

    The recorded key is the *value that went* -- ``agents.defaults.model`` --
    not ``… .primary``, which never existed. Recording the leaf would leave an
    empty object behind on Undo and lose the model the user had chosen.
    """

    seed = SEEDS["existing-string-default"]
    path, record = _configured(seed, tmp_path)

    assert json.loads(path.read_text(encoding="utf-8"))["agents"]["defaults"][
        "model"
    ] == {"primary": "openrouter/configured"}
    entry = read_entry(SUBJECT, path=record)
    assert entry is not None
    assert [value.key_path for value in entry.overwritten] == [
        ("agents", "defaults", "model")
    ]
    # Recorded twice over: the value, and the exact bytes it occupied, so
    # Undo does not re-render a string the user may have spelled differently.
    assert entry.overwritten[0].prior_present is True
    assert entry.overwritten[0].prior_value == "openai/gpt-5"
    assert entry.overwritten[0].prior_line == '"openai/gpt-5"'

    unconfigure_owned_block(
        merge=_merge(),
        path=path,
        subject=SUBJECT,
        record_path=record,
        proxy_root_url=ROOT,
    )
    assert path.read_bytes().decode("utf-8") == seed


def test_an_existing_primary_keeps_the_rest_of_the_object(tmp_path) -> None:
    seed = SEEDS["existing-object-default"]
    path, record = _configured(seed, tmp_path)

    model = json.loads(path.read_text(encoding="utf-8"))["agents"]["defaults"]["model"]
    assert model == {
        "primary": "openrouter/configured",
        "fallbacks": ["openai/gpt-5-mini"],
    }

    unconfigure_owned_block(
        merge=_merge(),
        path=path,
        subject=SUBJECT,
        record_path=record,
        proxy_root_url=ROOT,
    )
    assert path.read_bytes().decode("utf-8") == seed


def test_a_missing_file_is_created_and_undo_removes_it(tmp_path) -> None:
    path = tmp_path / "openclaw.json"
    record = tmp_path / "restore-record.json"

    result = configure_owned_block(
        merge=_merge(),
        path=path,
        document=_document(),
        proxy_root_url=ROOT,
        subject=SUBJECT,
        record_path=record,
    )

    assert result.created is True
    assert result.backup_path is None, "nothing to back up: there was no file"
    assert path.exists()
    entry = read_entry(SUBJECT, path=record)
    assert entry is not None and entry.created_file is True

    undone = unconfigure_owned_block(
        merge=_merge(),
        path=path,
        subject=SUBJECT,
        record_path=record,
        proxy_root_url=ROOT,
    )

    assert undone.removed_file is True
    assert not path.exists(), "a file MCC created and emptied must not be left"


def test_a_foreign_mcc_block_is_never_claimed_or_removed(tmp_path) -> None:
    """``models.providers.mcc`` that MCC did not write is the user's."""

    path = tmp_path / "openclaw.json"
    path.write_text(
        '{\n  "models": {"providers": {"mcc": {"baseUrl": "http://elsewhere"}}},\n'
        '  "logging": {"level": "info"}\n}\n',
        encoding="utf-8",
    )
    before = path.read_bytes()
    record = tmp_path / "restore-record.json"

    result = unconfigure_owned_block(
        merge=_merge(),
        path=path,
        subject=SUBJECT,
        record_path=record,
        proxy_root_url=ROOT,
    )

    assert result.changed is False
    assert path.read_bytes() == before


def test_a_document_that_does_not_parse_is_never_written(tmp_path) -> None:
    path = tmp_path / "openclaw.json"
    path.write_text('{ "models": ', encoding="utf-8")
    before = path.read_bytes()
    record = tmp_path / "restore-record.json"

    with pytest.raises(MergeDocumentError):
        configure_owned_block(
            merge=_merge(),
            path=path,
            document=_document(),
            proxy_root_url=ROOT,
            subject=SUBJECT,
            record_path=record,
        )

    assert path.read_bytes() == before
    assert not record.exists()


def test_the_backup_is_the_users_file_not_yesterdays_output(tmp_path) -> None:
    seed = SEEDS["comments"]
    path, record = _configured(seed, tmp_path)
    backup = path.with_name(path.name + ".mcc-backup")

    assert backup.read_bytes().decode("utf-8") == seed

    # A second Configure with a different block updates the file and leaves
    # the first backup alone.
    configure_owned_block(
        merge=_merge(),
        path=path,
        document=_document(),
        proxy_root_url="http://127.0.0.1:9000",
        subject=SUBJECT,
        record_path=record,
    )
    assert backup.read_bytes().decode("utf-8") == seed


def test_a_second_configure_keeps_the_first_users_value(tmp_path) -> None:
    """The record answers "what did the user have *before MCC*", once."""

    seed = SEEDS["existing-object-default"]
    path, record = _configured(seed, tmp_path)

    configure_owned_block(
        merge=_merge(),
        path=path,
        document=_document(),
        proxy_root_url=ROOT,
        subject=SUBJECT,
        record_path=record,
    )

    entry = read_entry(SUBJECT, path=record)
    assert entry is not None
    assert entry.overwritten[0].prior_value == "openai/gpt-5"

    unconfigure_owned_block(
        merge=_merge(),
        path=path,
        subject=SUBJECT,
        record_path=record,
        proxy_root_url=ROOT,
    )
    assert path.read_bytes().decode("utf-8") == seed


def test_restore_refuses_when_the_document_changed_since(tmp_path) -> None:
    seed = SEEDS["canonical"]
    path, record = _configured(seed, tmp_path)
    path.write_text(
        path.read_text(encoding="utf-8").replace('"info"', '"debug"'),
        encoding="utf-8",
        newline="",
    )
    edited = path.read_bytes()

    with pytest.raises(MergeDocumentError):
        unconfigure_owned_block(
            merge=_merge(),
            path=path,
            subject=SUBJECT,
            mode=UndoMode.RESTORE,
            record_path=record,
            proxy_root_url=ROOT,
        )

    assert path.read_bytes() == edited, "a refused restore writes nothing"

    # Keys-only still works, and is honest about what it does.
    result = unconfigure_owned_block(
        merge=_merge(),
        path=path,
        subject=SUBJECT,
        record_path=record,
        proxy_root_url=ROOT,
    )
    assert result.changed is True


def test_keys_only_with_no_record_removes_only_what_it_can_prove(tmp_path) -> None:
    """No record: MCC's block names this server, so it goes; the rest is guesswork."""

    seed = '{\n  "agents": {"defaults": {"model": "openai/gpt-5"}},\n  "logging": {"level": "info"}\n}\n'
    path, record = _configured(seed, tmp_path)
    record.unlink()

    unconfigure_owned_block(
        merge=_merge(),
        path=path,
        subject=SUBJECT,
        record_path=record,
        proxy_root_url=ROOT,
    )

    # No record means no proof the default model was ever MCC's, so this mode
    # deletes the key rather than guessing -- exactly what the desktop engine
    # does, and why RESTORE exists. The empty containers go with the block
    # because without the record there is nothing to say MCC did not create
    # them, and an empty object holds nothing of the user's.
    document = _parsed(path.read_text(encoding="utf-8"))
    assert "models" not in document
    assert document["logging"]["level"] == "info"


def test_the_refresh_updates_the_block_and_never_the_users_model(tmp_path) -> None:
    """A provider refresh must not move the user's chosen model back."""

    seed = SEEDS["canonical"]
    path, record = _configured(seed, tmp_path)
    text = path.read_text(encoding="utf-8")
    chosen = text.replace(
        '"primary": "openrouter/configured"', '"primary": "openrouter/other"'
    )
    path.write_text(chosen, encoding="utf-8", newline="")

    configure_owned_block(
        merge=_merge(),
        path=path,
        document=_document(),
        proxy_root_url="http://127.0.0.1:9999",
        subject=SUBJECT,
        claim=False,
        record_path=record,
    )

    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["agents"]["defaults"]["model"]["primary"] == "openrouter/other"
    assert document["models"]["providers"]["mcc"]["baseUrl"].endswith(":9999/v1")


def test_a_refresh_keeps_a_later_restore_possible(tmp_path) -> None:
    """The recorded hash follows MCC's own writes, not the user's edits."""

    seed = SEEDS["canonical"]
    path, record = _configured(seed, tmp_path)

    configure_owned_block(
        merge=_merge(),
        path=path,
        document=_document(),
        proxy_root_url="http://127.0.0.1:9999",
        subject=SUBJECT,
        claim=False,
        record_path=record,
    )

    unconfigure_owned_block(
        merge=_merge(),
        path=path,
        subject=SUBJECT,
        mode=UndoMode.RESTORE,
        record_path=record,
        proxy_root_url=ROOT,
    )
    assert path.read_bytes().decode("utf-8") == seed


def test_a_commented_document_is_read_the_way_openclaw_reads_it(tmp_path) -> None:
    """The refresh gate and the card both need this reader, not ``json.loads``."""

    path, _record = _configured(SEEDS["comments"], tmp_path)

    document = read_text_document(path)
    assert document is not None
    assert owned_block_names(
        path,
        _merge().owned_key_path,
        ROOT,
        key=_merge().base_url_key,
        sentinel=_merge().base_url_sentinel,
        preserve_bytes=True,
    )
    assert not owned_block_names(
        path,
        _merge().owned_key_path,
        "http://127.0.0.1:9999",
        key=_merge().base_url_key,
        sentinel=_merge().base_url_sentinel,
        preserve_bytes=True,
    )


def test_the_path_is_resolved_the_way_openclaw_resolves_it(tmp_path) -> None:
    merge = _merge()
    empty: dict[str, str] = {}

    assert merge_config_path(merge, {"HOME": str(tmp_path)}) == (
        tmp_path / ".openclaw" / "openclaw.json"
    )
    assert merge_config_path(
        merge, {"HOME": str(tmp_path), "OPENCLAW_HOME": str(tmp_path / "home")}
    ) == (tmp_path / "home" / ".openclaw" / "openclaw.json")
    assert merge_config_path(
        merge, {"HOME": str(tmp_path), "OPENCLAW_STATE_DIR": str(tmp_path / "state")}
    ) == (tmp_path / "state" / "openclaw.json")
    assert merge_config_path(
        merge,
        {
            "HOME": str(tmp_path),
            "OPENCLAW_STATE_DIR": str(tmp_path / "state"),
            "OPENCLAW_CONFIG_PATH": str(tmp_path / "mine.json"),
        },
    ) == (tmp_path / "mine.json")
    assert merge_config_path(merge, empty).name == "openclaw.json"
