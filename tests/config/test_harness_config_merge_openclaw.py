"""OpenClaw's Configure/Undo: one block in, the same bytes back out.

``~/.openclaw/openclaw.json`` is the second file MCC writes into that the user
owns, and the first one that is meant to be hand-edited: OpenClaw reads JSON5,
so it can carry comments, its author's indentation and a trailing comma. The
promise is ``config/document_codecs``': every byte MCC does not own comes back
byte for byte, Configure touches one block and the additive keys beside it, and
Undo -- either mode -- returns the document to exactly what it was.

Since 8.20.3 the change is additive and these tests say so: the default model
is written only when the file has none, one ``mcc/*`` wildcard goes into
whichever container governs the picker, and the proxy token is written
literally into ``apiKey`` (the user's Q1/Q2/Q3 decisions).

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
from my_claude_code.config.harnesses import (
    OPENCLAW_API_KEY_FALLBACK,
    HarnessConfigMerge,
    harness_spec,
)
from my_claude_code.config.restore_record import UndoMode, read_entry

SUBJECT = "openclaw"
ROOT = "http://127.0.0.1:8082"

#: Fake, always. A real token must never reach a test's file or its output.
FAKE_TOKEN = "mcc-fake-token-0123456789abcdef"


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
    #: The user's own shape: an allowlist of their models already in place.
    "user-models-map": (
        "// my openclaw config\n"
        "{\n"
        '  "agents": {\n'
        '    "defaults": {\n'
        '      "models": {\n'
        '        "openai/gpt-6-astra": {},   // the one I use\n'
        '        "openrouter/auto": {}\n'
        "      }\n"
        "    }\n"
        "  }\n"
        "}\n"
    ),
}


def _parsed(text: str) -> dict[str, Any]:
    """Parse a JSON5 document for an assertion, with the type narrowed."""

    parsed = parse_jsonc(text)
    assert isinstance(parsed, dict)
    return {str(key): value for key, value in parsed.items()}


def _configured(
    seed: str, tmp_path: Path, *, token: str = FAKE_TOKEN, claim: bool = True
) -> tuple[Path, Path]:
    path = tmp_path / "openclaw.json"
    path.write_text(seed, encoding="utf-8", newline="")
    record = tmp_path / "restore-record.json"
    configure_owned_block(
        merge=_merge(),
        path=path,
        document=_document(),
        proxy_root_url=ROOT,
        subject=SUBJECT,
        claim=claim,
        record_path=record,
        proxy_token=token,
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


def test_only_mccs_block_and_the_additive_keys_change(tmp_path) -> None:
    """Everything else -- comments, spacing, key order -- is the user's."""

    seed = SEEDS["comments"]
    path, _record = _configured(seed, tmp_path)

    text = path.read_text(encoding="utf-8")
    assert "// my openclaw config" in text
    assert "// the bot" in text
    document = _parsed(text)
    assert document["logging"]["level"] == "info"
    assert document["models"]["providers"]["mcc"]["baseUrl"] == f"{ROOT}/v1"
    # The token is written, not referenced: OpenClaw decides availability from
    # credentials it can resolve in its own process (the user's Q2).
    assert document["models"]["providers"]["mcc"]["apiKey"] == FAKE_TOKEN
    assert document["agents"]["defaults"]["model"]["primary"] == (
        "openrouter/configured"
    )
    # The one ref that keeps every mcc/… row in OpenClaw's picker. This seed
    # has no ``agents.defaults.models``, so the map is created holding it.
    assert document["agents"]["defaults"]["models"] == {"mcc/*": {}}


def test_a_default_model_is_written_only_when_the_file_has_none(tmp_path) -> None:
    """Q1: a value that is there is the user's, and is never replaced.

    The absent case is the fresh-install one, and it is written and recorded
    with ``prior_present=False`` so Undo deletes it again.
    """

    seed = SEEDS["canonical"]
    path, record = _configured(seed, tmp_path)

    assert json.loads(path.read_text(encoding="utf-8"))["agents"]["defaults"][
        "model"
    ] == {"primary": "openrouter/configured"}
    entry = read_entry(SUBJECT, path=record)
    assert entry is not None
    assert [value.key_path for value in entry.overwritten] == [
        ("agents", "defaults", "model", "primary")
    ]
    assert entry.overwritten[0].prior_present is False

    unconfigure_owned_block(
        merge=_merge(),
        path=path,
        subject=SUBJECT,
        record_path=record,
        proxy_root_url=ROOT,
    )
    assert path.read_bytes().decode("utf-8") == seed


def test_an_existing_default_model_is_left_alone_and_said_so(tmp_path, capsys) -> None:
    """A ``primary`` already there is the user's: kept, and nothing recorded."""

    seed = SEEDS["existing-object-default"]
    path, record = _configured(seed, tmp_path)

    model = json.loads(path.read_text(encoding="utf-8"))["agents"]["defaults"]["model"]
    assert model["primary"] == "openai/gpt-5"
    entry = read_entry(SUBJECT, path=record)
    assert entry is not None
    assert entry.overwritten == ()
    assert "agents.defaults.model.primary is already set" in capsys.readouterr().err

    unconfigure_owned_block(
        merge=_merge(),
        path=path,
        subject=SUBJECT,
        record_path=record,
        proxy_root_url=ROOT,
    )
    assert path.read_bytes().decode("utf-8") == seed


def test_a_non_object_default_model_is_left_alone_and_said_so(tmp_path, capsys) -> None:
    """A string ``agents.defaults.model`` is never replaced by an object.

    Writing ``primary`` there would mean replacing the whole value, which is
    the opposite of "pure addition" (the brief's F6).
    """

    seed = SEEDS["existing-string-default"]
    path, record = _configured(seed, tmp_path)

    assert (
        json.loads(path.read_text(encoding="utf-8"))["agents"]["defaults"]["model"]
        == "openai/gpt-5"
    )
    entry = read_entry(SUBJECT, path=record)
    assert entry is not None
    assert entry.overwritten == ()
    assert "agents.defaults.model is not an object" in capsys.readouterr().err

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
        "primary": "openai/gpt-5",
        "fallbacks": ["openai/gpt-5-mini"],
    }
    # The wildcard still went in: the picker is a different question from the
    # default model, and the user's own entries are untouched.
    defaults = json.loads(path.read_text(encoding="utf-8"))["agents"]["defaults"]
    assert defaults["models"] == {"mcc/*": {}}

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


def test_a_second_configure_keeps_the_first_configures_record(tmp_path) -> None:
    """The record answers "what did the user have *before MCC*", once.

    The second run adds nothing -- every key is there, and the default model
    it wrote itself now reads as "already set" -- so it must not drop the
    record the first run wrote: Undo needs it to take the default model back
    out again.
    """

    seed = SEEDS["canonical"]
    path, record = _configured(seed, tmp_path)

    configure_owned_block(
        merge=_merge(),
        path=path,
        document=_document(),
        proxy_root_url=ROOT,
        subject=SUBJECT,
        record_path=record,
        proxy_token=FAKE_TOKEN,
    )

    entry = read_entry(SUBJECT, path=record)
    assert entry is not None
    assert [value.key_path for value in entry.overwritten] == [
        ("agents", "defaults", "model", "primary")
    ]
    assert entry.overwritten[0].prior_present is False
    assert entry.created_keys[0].key_path == (
        "agents",
        "defaults",
        "models",
        "mcc/*",
    )

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
        proxy_token=FAKE_TOKEN,
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


# ------------------------------------------------- the model allowlist (8.20.3)


def test_an_explicit_model_policy_gets_the_wildcard_appended_once(tmp_path) -> None:
    """Q3's governing container, and the vendor's precedence, case one."""

    seed = (
        "// my openclaw config\n"
        "{\n"
        '  "agents": {\n'
        '    "defaults": {\n'
        '      "modelPolicy": {\n'
        '        "allow": ["openai/gpt-6-astra", "openrouter/auto"]\n'
        "      }\n"
        "    }\n"
        "  },\n"
        '  "logging": {"level": "info"}\n'
        "}\n"
    )
    path, record = _configured(seed, tmp_path)

    document = _parsed(path.read_text(encoding="utf-8"))
    defaults = document["agents"]["defaults"]
    assert defaults["modelPolicy"]["allow"] == [
        "openai/gpt-6-astra",
        "openrouter/auto",
        "mcc/*",
    ]
    # No legacy map was created beside it: the policy governs.
    assert "models" not in defaults

    unconfigure_owned_block(
        merge=_merge(),
        path=path,
        subject=SUBJECT,
        record_path=record,
        proxy_root_url=ROOT,
    )
    assert path.read_bytes().decode("utf-8") == seed


def test_a_model_policy_without_allow_gets_no_visibility_write(
    tmp_path, capsys
) -> None:
    """The vendor ignores the legacy map once ``modelPolicy`` is a record.

    ``collectLegacyDefaultModelAllowRefs`` returns null for
    ``isRecord(defaults.modelPolicy)``, so nothing restricts the picker and
    MCC adds nothing -- and says why.
    """

    seed = (
        "{\n"
        '  "agents": {\n'
        '    "defaults": {\n'
        '      "modelPolicy": {"repair": true},\n'
        '      "models": {"openai/gpt-6-astra": {}}\n'
        "    }\n"
        "  }\n"
        "}\n"
    )
    path, record = _configured(seed, tmp_path)

    defaults = _parsed(path.read_text(encoding="utf-8"))["agents"]["defaults"]
    assert defaults["models"] == {"openai/gpt-6-astra": {}}
    assert "modelPolicy" in defaults
    assert "reads as no restriction" in capsys.readouterr().err

    unconfigure_owned_block(
        merge=_merge(),
        path=path,
        subject=SUBJECT,
        record_path=record,
        proxy_root_url=ROOT,
    )
    assert path.read_bytes().decode("utf-8") == seed


def test_a_wildcard_already_there_is_not_added_twice(tmp_path) -> None:
    seed = (
        "{\n"
        '  "agents": {"defaults": {"models": {"mcc/*": {}, "openai/x": {}}}},\n'
        '  "logging": {"level": "info"}\n'
        "}\n"
    )
    path, _record = _configured(seed, tmp_path)

    text = path.read_text(encoding="utf-8")
    assert text.count('"mcc/*"') == 1


# ------------------------------------------------------ the token (8.20.3 Q2)


def test_no_token_configured_writes_the_variable_reference(tmp_path) -> None:
    """Never an empty ``apiKey``: the launcher can satisfy a reference."""

    path, _record = _configured(SEEDS["canonical"], tmp_path, token="")

    block = json.loads(path.read_text(encoding="utf-8"))["models"]["providers"]["mcc"]
    assert block["apiKey"] == OPENCLAW_API_KEY_FALLBACK == "$MCC_OPENCLAW_API_KEY"


def test_a_token_is_upgraded_from_the_reference_on_the_next_run(tmp_path) -> None:
    """An install configured before the token existed converges on a re-run."""

    path, _record = _configured(SEEDS["canonical"], tmp_path, token="")
    assert "$MCC_OPENCLAW_API_KEY" in path.read_text(encoding="utf-8")

    configure_owned_block(
        merge=_merge(),
        path=path,
        document=_document(),
        proxy_root_url=ROOT,
        subject=SUBJECT,
        record_path=tmp_path / "restore-record.json",
        proxy_token=FAKE_TOKEN,
    )

    block = json.loads(path.read_text(encoding="utf-8"))["models"]["providers"]["mcc"]
    assert block["apiKey"] == FAKE_TOKEN


def test_the_token_is_never_in_the_record_nor_anywhere_else_in_the_file(
    tmp_path,
) -> None:
    """The secret is written once, in ``apiKey``, and nowhere else (IV.5)."""

    path, record = _configured(SEEDS["canonical"], tmp_path)

    raw = path.read_bytes().decode("utf-8")
    assert raw.count(FAKE_TOKEN) == 1
    assert FAKE_TOKEN not in record.read_text(encoding="utf-8")
    assert FAKE_TOKEN not in path.with_name("openclaw.json.mcc-backup").read_text(
        encoding="utf-8"
    )


def test_the_record_names_what_was_created_so_undo_can_take_it_back(tmp_path) -> None:
    _path, record = _configured(SEEDS["canonical"], tmp_path)

    entry = read_entry(SUBJECT, path=record)
    assert entry is not None
    assert [
        (key.key_path, key.value, key.in_sequence) for key in entry.created_keys
    ] == [(("agents", "defaults", "models", "mcc/*"), {}, False)]


def test_a_second_configure_of_the_same_document_writes_nothing(tmp_path) -> None:
    path, record = _configured(SEEDS["canonical"], tmp_path)
    before = path.read_bytes()

    result = configure_owned_block(
        merge=_merge(),
        path=path,
        document=_document(),
        proxy_root_url=ROOT,
        subject=SUBJECT,
        record_path=record,
        proxy_token=FAKE_TOKEN,
    )

    assert result.changed is False
    assert path.read_bytes() == before


def test_a_refresh_adds_a_missing_wildcard_and_never_the_default_model(
    tmp_path,
) -> None:
    """``claim=False`` may add MCC's own keys; the user's model is theirs."""

    seed = (
        "{\n"
        '  "agents": {"defaults": {"model": {"primary": "openai/gpt-5"}}},\n'
        '  "models": {"providers": {"mcc": {"baseUrl": "http://127.0.0.1:8082/v1"}}}\n'
        "}\n"
    )
    path, record = _configured(seed, tmp_path, claim=False)

    document = _parsed(path.read_text(encoding="utf-8"))
    defaults = document["agents"]["defaults"]
    assert defaults["model"] == {"primary": "openai/gpt-5"}
    assert defaults["models"]["mcc/*"] == {}
    # Nothing replaced, and no record of a claim that never happened.
    assert read_entry(SUBJECT, path=record) is None


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
