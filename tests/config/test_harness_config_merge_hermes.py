"""Hermes' Configure/Undo: one table into the user's own config.yaml.

``~/.hermes/config.yaml`` is the first merge target MCC edits as **YAML**, and
the first whose block only ever *creates* keys -- ``providers.mcc`` replaces
nothing, so there is no "prior value" for the record to keep. What the record
still has to carry is the document's own history: whether MCC created the file,
and whether it created the ``providers:`` container above its block. Without
that, Undo either leaves an empty file in the CLI's own directory or removes a
container the user wrote.

The seeds below are the shapes a hand-written ``config.yaml`` actually takes:
two-space, four-space, comments above and inside the table, CRLF, no trailing
newline, a user with their own providers beside MCC's, an empty ``providers:``,
and a file that does not exist yet.

The promise is ``config/document_codecs``': every byte MCC does not own comes
back byte for byte -- and Hermes is the harness where that matters *less* than
usual and is still kept, because Hermes itself re-dumps the file on its own
migrations (``save_config`` -> ``utils.atomic_yaml_write``), which is why
``RESTORE`` is hash-guarded and there is no version MCC can pin.
"""

import difflib
from pathlib import Path
from typing import Any

import pytest

from my_claude_code.application.catalogue_model import CatalogueModel
from my_claude_code.application.catalogues import serialise
from my_claude_code.config.document_codecs import yaml_mapping_at
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

SUBJECT = "hermes"
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
            is_primary_route=True,
        ),
        CatalogueModel(
            gateway_id="anthropic/openrouter/other",
            provider_model_ref="openrouter/other",
            display_name="Other",
        ),
    ]
    document, _defaulted = serialise("hermes", models)
    return document


#: The document shapes a user's own config.yaml takes. Each is a whole file.
#: Seeds whose round trip is byte-identical. The empty-flow container is
#: deliberately *not* one of them: Configure has to open ``providers: {}`` to
#: put a block inside it, and Undo leaves it open. That seed has its own test.
SEEDS: dict[str, str] = {
    "canonical": (
        "_config_version: 33\n"
        "model:\n"
        "  provider: openrouter\n"
        "  default: anthropic/claude-sonnet-4\n"
    ),
    "comments": (
        "# my hermes config, hand-written\n"
        "_config_version: 33\n"
        "\n"
        "agent:\n"
        "  reasoning_effort: high   # do not change\n"
    ),
    "four-space": (
        "_config_version: 33\n"
        "model:\n"
        "    provider: openrouter\n"
        "    default: anthropic/claude-sonnet-4\n"
    ),
    "crlf": ("_config_version: 33\r\nmodel:\r\n  provider: openrouter\r\n"),
    "no-trailing-newline": "_config_version: 33\nmodel:\n  provider: openrouter",
    "with-own-providers": (
        "_config_version: 33\n"
        "providers:\n"
        "  myown:\n"
        "    api: https://example.test/v1\n"
        "    key_env: MY_OWN_KEY\n"
        "    models:\n"
        "      their-model:\n"
        "        context_length: 64000\n"
    ),
}

#: A user with a ``providers:`` key and nothing under it. Configure opens the
#: flow mapping to write into it; Undo must leave the key, not prune it.
EMPTY_CONTAINER_SEED = "_config_version: 33\nproviders: {}\n"

#: What the seeds above hold before MCC writes anything, for the byte-identity
#: assertions: the file with MCC's block taken out is the seed itself.
_GOLDEN = dict(SEEDS)


def _map(value: object) -> dict[str, Any]:
    """Narrow one nested value of a parsed YAML document for an assertion."""

    assert isinstance(value, dict)
    return {str(key): item for key, item in value.items()}


def _configured(seed: str, tmp_path: Path) -> tuple[Path, Path]:
    path = tmp_path / "config.yaml"
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


def _undo(path: Path, record: Path, mode: UndoMode = UndoMode.KEYS_ONLY) -> Any:
    return unconfigure_owned_block(
        merge=_merge(),
        path=path,
        subject=SUBJECT,
        mode=mode,
        record_path=record,
        proxy_root_url=ROOT,
    )


def _changed_lines(before: str, after: str) -> list[str]:
    """The lines a unified diff names as removed or added."""

    return [
        line
        for line in difflib.unified_diff(
            before.splitlines(), after.splitlines(), lineterm="", n=0
        )
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    ]


@pytest.mark.parametrize("seed_name", sorted(SEEDS))
def test_configure_writes_the_block_and_undo_returns_the_same_bytes(
    seed_name: str, tmp_path: Path
) -> None:
    seed = SEEDS[seed_name]
    path, record = _configured(seed, tmp_path)

    block = yaml_mapping_at(path.read_text(encoding="utf-8"), ("providers", "mcc"))
    assert block is not None
    assert block["api"] == f"{ROOT}/v1"
    assert block["key_env"] == "MCC_HERMES_API_KEY"
    assert list(_map(block["models"])) == [
        "openrouter/configured",
        "openrouter/other",
    ]

    _undo(path, record, UndoMode.RESTORE)

    assert path.read_bytes().decode("utf-8") == seed
    assert read_entry(SUBJECT, path=record) is None


@pytest.mark.parametrize("seed_name", sorted(SEEDS))
def test_no_line_outside_mccs_table_is_ever_touched(
    seed_name: str, tmp_path: Path
) -> None:
    """The diff names the owned table's lines and nothing else."""

    seed = SEEDS[seed_name]
    path, _record = _configured(seed, tmp_path)

    after = path.read_text(encoding="utf-8")
    changed = _changed_lines(seed, after)
    assert changed, "the fixture would prove nothing if nothing was written"
    # Every changed line belongs to MCC's table, opens the container for it, or
    # is the one header ``_yaml_opened_header`` has to rewrite (an empty flow
    # mapping the block goes inside -- the codec's documented behaviour).
    for line in changed:
        if line == "-providers: {}":
            continue
        assert line.startswith("+"), line
        body = line[1:].strip()
        assert (
            body.startswith("providers:")
            or body.startswith("mcc:")
            or body.startswith("api:")
            or body.startswith("key_env:")
            or body.startswith("transport:")
            or body.startswith("default_model:")
            or body.startswith("models:")
            or body.startswith("extra_headers:")
            or body.startswith("x-mcc-harness:")
            or body.startswith('"openrouter/')
            or body == "context_length: 400000"
            or body.startswith('api: "')
        ), line


def test_the_written_block_carries_no_token_and_no_defaulted_record(
    tmp_path: Path,
) -> None:
    path, _record = _configured(SEEDS["comments"], tmp_path)

    text = path.read_text(encoding="utf-8")

    assert "MCC_HERMES_API_KEY" in text
    assert "proxy-token" not in text
    assert "Bearer" not in text
    # Hermes warns about any provider key its normaliser does not know, so
    # MCC's bookkeeping never goes into the block.
    assert "_mcc_defaulted" not in text
    assert "# my hermes config, hand-written" in text
    assert "reasoning_effort" in text


def test_a_second_configure_writes_nothing(tmp_path: Path) -> None:
    """Idempotent on MCC's own table, not on the file."""

    path, record = _configured(SEEDS["canonical"], tmp_path)
    before = path.read_bytes()

    result = configure_owned_block(
        merge=_merge(),
        path=path,
        document=_document(),
        proxy_root_url=ROOT,
        subject=SUBJECT,
        record_path=record,
    )

    assert result.changed is False
    assert path.read_bytes() == before


def test_keys_only_needs_no_record_at_all(tmp_path: Path) -> None:
    """``--disconnect`` works on a file MCC configured and then forgot.

    Hermes rewrites its own config.yaml, and a user may restore an older copy;
    either way the block can be there with no record beside it. ``KEYS_ONLY``
    never refuses -- it removes exactly MCC's table and says what it did.
    """

    path = tmp_path / "config.yaml"
    path.write_text(
        "_config_version: 33\n"
        "providers:\n"
        "  mcc:\n"
        f'    api: "{ROOT}/v1"\n'
        "    key_env: MCC_HERMES_API_KEY\n"
        "    models:\n"
        '      "openrouter/configured": {}\n',
        encoding="utf-8",
        newline="",
    )
    record = tmp_path / "restore-record.json"

    result = _undo(path, record, UndoMode.KEYS_ONLY)

    assert result.changed is True
    assert path.read_text(encoding="utf-8") == "_config_version: 33\n"


def test_keys_only_on_a_file_with_nothing_of_mccs_is_an_honest_no_op(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(SEEDS["comments"], encoding="utf-8", newline="")
    before = path.read_bytes()

    result = _undo(path, tmp_path / "restore-record.json", UndoMode.KEYS_ONLY)

    assert result.changed is False
    assert path.read_bytes() == before


def test_restore_refuses_when_there_is_no_record_at_all(tmp_path: Path) -> None:
    """MCC's block, this server's address, and no record: RESTORE must refuse.

    ``KEYS_ONLY`` is the mode that never refuses; ``RESTORE`` is the mode that
    refuses precisely when it cannot prove what the document held.
    """

    path = tmp_path / "config.yaml"
    path.write_text(
        "_config_version: 33\n"
        "providers:\n"
        "  mcc:\n"
        f'    api: "{ROOT}/v1"\n'
        "    key_env: MCC_HERMES_API_KEY\n",
        encoding="utf-8",
        newline="",
    )

    with pytest.raises(MergeDocumentError) as excinfo:
        _undo(path, tmp_path / "restore-record.json", UndoMode.RESTORE)

    assert "no record of what MCC replaced" in str(excinfo.value)
    assert "KEYS_ONLY" in str(excinfo.value)


def test_a_consumed_record_makes_a_second_restore_a_no_op(tmp_path: Path) -> None:
    """After RESTORE, nothing of MCC's is there, and that is not an error."""

    path, record = _configured(SEEDS["canonical"], tmp_path)
    _undo(path, record, UndoMode.RESTORE)
    assert read_entry(SUBJECT, path=record) is None
    before = path.read_bytes()

    result = _undo(path, record, UndoMode.RESTORE)

    assert result.changed is False
    assert path.read_bytes() == before


def test_restore_refuses_when_hermes_rewrote_the_file(tmp_path: Path) -> None:
    """Hermes re-dumps config.yaml itself; RESTORE must not revert that."""

    path, record = _configured(SEEDS["canonical"], tmp_path)
    # Hermes' own save_config drops the comment-and-layout question entirely:
    # any edit by the app is an edit, and the hash has to notice it.
    path.write_text(
        path.read_text(encoding="utf-8").replace("openrouter", "nous"),
        encoding="utf-8",
        newline="",
    )

    with pytest.raises(MergeDocumentError) as excinfo:
        _undo(path, record, UndoMode.RESTORE)

    assert "has changed since MCC configured it" in str(excinfo.value)


def test_a_file_mcc_created_is_removed_again(tmp_path: Path) -> None:
    """The record carries ``created_file`` even when nothing was replaced."""

    path = tmp_path / "config.yaml"
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
    assert result.backup_path is None
    entry = read_entry(SUBJECT, path=record)
    assert entry is not None and entry.created_file is True
    assert path.exists()

    undone = _undo(path, record)

    assert undone.removed_file is True
    assert not path.exists()


def test_an_empty_providers_key_the_user_had_is_not_pruned(tmp_path: Path) -> None:
    """``providers: {}`` existed before MCC wrote, so Undo keeps the key.

    Configure has to open the flow mapping to put a block inside it, so the
    header line comes back as ``providers:`` rather than ``providers: {}`` --
    the same empty mapping, spelled the way a block reads. The key itself is
    the user's and must survive; pruning it would be MCC deleting a key it
    never wrote.
    """

    path, record = _configured(EMPTY_CONTAINER_SEED, tmp_path)
    entry = read_entry(SUBJECT, path=record)
    assert entry is not None
    # The container was there: MCC created neither it nor the file.
    assert entry.created_file is False
    assert entry.created_parents == ()

    _undo(path, record, UndoMode.KEYS_ONLY)

    assert path.read_text(encoding="utf-8") == "_config_version: 33\nproviders:\n"


def test_a_providers_key_mcc_created_is_removed(tmp_path: Path) -> None:
    seed = SEEDS["canonical"]

    path, record = _configured(seed, tmp_path)
    entry = read_entry(SUBJECT, path=record)
    assert entry is not None and (("providers",) in entry.created_parents)

    _undo(path, record)

    assert path.read_text(encoding="utf-8") == seed
    assert "providers:" not in path.read_text(encoding="utf-8")


def test_a_foreign_mcc_table_is_never_claimed_or_removed(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "_config_version: 33\n"
        "providers:\n"
        "  mcc:\n"
        '    api: "http://elsewhere.invalid/v1"\n'
        "    models:\n"
        '      "someone/elses": {}\n',
        encoding="utf-8",
        newline="",
    )
    before = path.read_bytes()

    result = _undo(path, tmp_path / "restore-record.json")

    assert result.changed is False
    assert path.read_bytes() == before


def test_a_yaml_list_document_is_refused_rather_than_corrupted(
    tmp_path: Path,
) -> None:
    """YAML has no parse error here -- the reader is narrow, not strict.

    A document whose root is a block sequence would take the codec's insert
    after its last ``- item`` and produce a file that is neither shape, so the
    merge engine refuses before it writes. The user's bytes come back.
    """

    path = tmp_path / "config.yaml"
    path.write_text("- id: llm-pi-ai\n- id: other\n", encoding="utf-8")
    before = path.read_bytes()
    record = tmp_path / "restore-record.json"

    with pytest.raises(MergeDocumentError) as excinfo:
        configure_owned_block(
            merge=_merge(),
            path=path,
            document=_document(),
            proxy_root_url=ROOT,
            subject=SUBJECT,
            record_path=record,
        )

    assert "sequence" in str(excinfo.value)
    assert path.read_bytes() == before
    assert not record.exists()


def test_an_unreadable_table_is_replaced_knowingly_the_way_the_codec_does(
    tmp_path: Path,
) -> None:
    """A flow value where the table goes is opened, not treated as an error.

    ``providers: [x`` is a shape the 7.117.0 engine replaces at the level it
    needs (``test_a_parent_holding_a_scalar_is_opened_for_the_table``). Pinned
    here so the Hermes path inherits that decision visibly rather than
    silently: the line is rewritten because MCC is about to own that key.
    """

    path = tmp_path / "config.yaml"
    path.write_text("providers: [unterminated\n", encoding="utf-8")
    record = tmp_path / "restore-record.json"

    configure_owned_block(
        merge=_merge(),
        path=path,
        document=_document(),
        proxy_root_url=ROOT,
        subject=SUBJECT,
        record_path=record,
    )

    assert yaml_mapping_at(path.read_text(encoding="utf-8"), ("providers", "mcc"))


def test_the_backup_is_the_users_file_not_yesterdays_output(tmp_path: Path) -> None:
    seed = SEEDS["comments"]
    path, _record = _configured(seed, tmp_path)

    backup = path.with_name(path.name + ".mcc-backup")

    assert backup.read_bytes().decode("utf-8") == seed


# --------------------------------------------------------------- path resolution


def _resolve(env: dict[str, str], platform: str) -> str:
    return str(_merge().resolve(env, platform)).replace("\\", "/")


def test_the_document_is_where_hermes_looks_for_it() -> None:
    """``hermes_constants.py:46-67`` of 0.19.0, rung for rung."""

    assert _resolve({"HERMES_HOME": "D:/h"}, "win32").endswith("D:/h/config.yaml")
    assert _resolve({"HERMES_HOME": "D:/h"}, "linux").endswith("D:/h/config.yaml")
    assert (
        _resolve({"LOCALAPPDATA": "C:/Users/ada/AppData/Local"}, "win32")
        == "C:/Users/ada/AppData/Local/hermes/config.yaml"
    )
    assert _resolve({}, "linux").endswith("/.hermes/config.yaml")


def test_merge_config_path_and_the_launcher_resolve_the_same_file(
    tmp_path: Path,
) -> None:
    env = {"HOME": str(tmp_path), "USERPROFILE": str(tmp_path)}
    resolved = merge_config_path(_merge(), env)

    assert resolved.name == "config.yaml"
    assert resolved.parent.name == ".hermes" or "hermes" in str(resolved.parent)


def test_detection_reports_the_document_mcc_would_write(tmp_path: Path) -> None:
    spec = harness_spec("hermes")
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("_config_version: 33\n", encoding="utf-8")

    probed = spec.config_probe_path(
        {"HOME": str(tmp_path), "USERPROFILE": str(tmp_path), "HERMES_HOME": str(home)}
    )

    assert probed is not None and probed.endswith("config.yaml")


def test_a_block_that_names_another_server_is_not_ours(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "providers:\n  mcc:\n    api: http://127.0.0.1:9999/v1\n", encoding="utf-8"
    )

    assert (
        owned_block_names(
            path,
            ("providers", "mcc"),
            ROOT,
            key="api",
            sentinel=_merge().base_url_sentinel,
            preserve_bytes=True,
            document_format="yaml",
        )
        is False
    )


def test_read_back_is_the_nested_mapping_mcc_wrote(tmp_path: Path) -> None:
    path, _record = _configured(SEEDS["with-own-providers"], tmp_path)

    document = read_text_document(path, "yaml")

    assert document is not None
    providers = _map(document["providers"])
    assert _map(providers["myown"])["api"] == "https://example.test/v1"
    assert _map(providers["mcc"])["transport"] == "openai_chat"
