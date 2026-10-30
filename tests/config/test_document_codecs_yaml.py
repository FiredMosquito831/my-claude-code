"""Nested YAML tables, edited as text, byte-preserving (7.117.0).

Until 7.117.0 ``config/document_codecs.py`` promised that MCC never writes a
table into a YAML document it does not own: there was exactly one YAML edit,
Goose's top-level ``active_provider``. A harness that merges ``providers.mcc``
into Hermes' ``config.yaml`` needs a nested mapping, and the promise that
matters there is the one the JSON path already makes -- *every byte MCC does
not own survives*. These pin what that promise means for YAML:

* creation of a table, and of the parents it needs, at the document's own
  indentation and with its own line endings;
* replacement of the table MCC owns, touching no other line: comments above,
  below and inside it, quoting of untouched scalars, key order, a same-named
  key under another parent, anchors and aliases;
* removal of the table, giving the file back byte for byte -- including a
  document that ends without a newline and one written in CRLF;
* idempotence: the same edit twice changes nothing the second time, and
  deleting an absent key changes nothing at all.

Every produced document is also read back with PyYAML, so "the right text"
cannot be a text that does not mean what was asked. PyYAML is already a test
dependency of the contract suites; it is not a runtime dependency and the
codec still does not parse YAML.

The existing YAML documents in the suite -- Goose's two seeds and the empty
file -- are round-tripped at the bottom, because the whole point of the edit
is that adding it changed nothing about the documents that were already there.
"""

import difflib
from collections.abc import Mapping
from typing import cast

import pytest
import yaml

from my_claude_code.config.document_codecs import (
    DeleteKey,
    DocumentFormat,
    DocumentFormatError,
    SetScalar,
    SetTable,
    apply_edits,
    yaml_mapping_at,
    yaml_root_is_sequence,
)

#: The table the Hermes engine gap was measured on (INVESTIGATION-HARNESS-
#: MODEL-LISTS.md §3.2): a nested provider entry with a nested model map.
MCC_TABLE: dict[str, object] = {
    "api": "http://127.0.0.1:8082/v1",
    "key_env": "MCC_HERMES_API_KEY",
    "default_model": "anthropic/openrouter/gpt-5",
    "models": {"anthropic/openrouter/gpt-5": {"context_length": 400000}},
}

#: Exactly what the block above renders to, at the document's top level.
CREATED_BLOCK = (
    "providers:\n"
    "  mcc:\n"
    '    api: "http://127.0.0.1:8082/v1"\n'
    "    key_env: MCC_HERMES_API_KEY\n"
    '    default_model: "anthropic/openrouter/gpt-5"\n'
    "    models:\n"
    '      "anthropic/openrouter/gpt-5":\n'
    "        context_length: 400000\n"
)

#: Hand-written documents that exercise one preservation rule each.
SEEDS: dict[str, str] = {
    "plain": "model:\n  provider: openrouter\n",
    "comments-above-and-below": (
        "# Hermes configuration, hand-written, must survive\n"
        "model:\n"
        "  provider: openrouter\n"
        "\n"
        "# providers are MCC's to add, everything above is mine\n"
    ),
    "crlf": "# mine\r\nmodel:\r\n  provider: openrouter\r\n",
    "no-trailing-newline": "model:\n  provider: openrouter",
    "four-space": "model:\n    provider: openrouter\n",
    "tab-indent": "model:\n\tprovider: openrouter\n",
    "anchors-and-aliases": (
        "defaults: &defaults\n  timeout: 30\nservice:\n  <<: *defaults\n  retries: 3\n"
    ),
    "same-name-elsewhere": "mcc:\n  api: root\nother:\n  mcc:\n    api: theirs\n",
    "document-marker": "---\nmodel:\n  provider: openrouter\n",
    "quoted-scalars": (
        "model: 'gpt-5.6'\nnote: \"a # that is not a comment\"\nratio: 1.50\n"
    ),
}

#: Seeds PyYAML itself refuses -- a tab is not legal YAML indentation -- and
#: which therefore prove only text preservation, not readability.
TEXT_ONLY_SEEDS = frozenset({"tab-indent"})


def _set(
    text: str,
    value: Mapping[str, object] = MCC_TABLE,
    path: tuple[str, ...] = ("providers", "mcc"),
) -> str:
    return apply_edits(text, DocumentFormat.YAML, [SetTable(path, value)])


def _delete(
    text: str, path: tuple[str, ...] = ("providers", "mcc"), *, prune: bool = True
) -> str:
    return apply_edits(
        text, DocumentFormat.YAML, [DeleteKey(path, prune_empty_ancestors=prune)]
    )


def _only_insertions(before: str, after: str) -> bool:
    """Whether ``before``'s lines are ``after``'s with lines inserted and no more.

    Compared by body, not by terminator: a document that ends without a newline
    is the one line that must change when a block is appended after it -- the
    terminator an append needs -- and that is asserted on its own below. The
    line endings themselves are pinned by the CRLF test.
    """

    remaining = iter(after.splitlines())
    return all(line in remaining for line in before.splitlines())


@pytest.mark.parametrize("name", sorted(SEEDS))
def test_creating_the_table_changes_no_other_line(name: str):
    text = SEEDS[name]
    after = _set(text)

    assert _only_insertions(text, after), (name, text, after)
    if name in TEXT_ONLY_SEEDS:
        # PyYAML refuses a tab-indented block outright; the codec still has to
        # preserve such a document rather than normalise it into validity.
        assert _set(after) == after
        return
    assert yaml.safe_load(after)["providers"]["mcc"] == MCC_TABLE
    # And the document's own values are still what they were.
    for key, value in (yaml.safe_load(text) or {}).items():
        assert yaml.safe_load(after)[key] == value


def test_a_document_without_a_providers_table_gains_one_at_its_end():
    text = SEEDS["comments-above-and-below"]

    assert _set(text) == text + CREATED_BLOCK


@pytest.mark.parametrize("name", sorted(SEEDS))
def test_applying_the_same_table_twice_changes_nothing(name: str):
    once = _set(SEEDS[name])

    assert _set(once) == once


@pytest.mark.parametrize("name", sorted(SEEDS))
def test_deleting_the_table_gives_the_document_back_byte_for_byte(name: str):
    text = SEEDS[name]

    assert _delete(_set(text)) == text


@pytest.mark.parametrize("name", sorted(SEEDS))
def test_deleting_an_absent_key_changes_nothing(name: str):
    text = SEEDS[name]

    assert _delete(text) == text


def test_a_crlf_document_stays_crlf_and_gains_crlf_lines():
    after = _set(SEEDS["crlf"])

    assert "\r\n" in after
    assert after.replace("\r\n", "").count("\n") == 0
    assert yaml.safe_load(after)["providers"]["mcc"]["key_env"] == (
        "MCC_HERMES_API_KEY"
    )


def test_a_document_ending_without_a_newline_still_does():
    after = _set(SEEDS["no-trailing-newline"])

    assert not after.endswith("\n")
    assert after.endswith("context_length: 400000")


@pytest.mark.parametrize("unit", ["    ", "\t", "   "])
def test_the_created_block_uses_the_documents_own_indentation(unit: str):
    text = f"model:\n{unit}provider: openrouter\n"

    after = _set(text)

    assert f"providers:\n{unit}mcc:\n{unit}{unit}api:" in after
    assert f"{unit}key_env: MCC_HERMES_API_KEY\n" in after


def test_a_re_apply_keeps_the_indentation_the_block_already_has():
    # The block MCC owns is rewritten at the indent it is found at, not at the
    # document's step: a table hand-indented with eight spaces stays that way.
    text = "providers:\n    mcc:\n        api: http://old/v1\n"

    after = _set(text)

    assert "        key_env: MCC_HERMES_API_KEY\n" in after
    assert "http://old/v1" not in after


def test_anchors_and_aliases_are_never_rewritten():
    text = SEEDS["anchors-and-aliases"]

    after = _set(text)

    assert "defaults: &defaults\n  timeout: 30\n" in after
    assert "  <<: *defaults\n  retries: 3\n" in after
    assert yaml.safe_load(after)["service"] == {"timeout": 30, "retries": 3}


def test_quoting_of_untouched_scalars_is_unchanged():
    text = SEEDS["quoted-scalars"]

    after = _set(text)

    for line in text.splitlines():
        assert line in after.splitlines(), line


def test_a_unified_diff_of_the_edit_names_only_the_owned_lines():
    before = SEEDS["comments-above-and-below"]

    diff = [
        line
        for line in difflib.unified_diff(
            before.splitlines(), _set(before).splitlines(), lineterm="", n=0
        )
        if line[:1] in {"+", "-"} and not line.startswith(("+++", "---"))
    ]

    assert diff == [f"+{line}" for line in CREATED_BLOCK.splitlines()]


def test_a_same_named_key_under_another_parent_is_untouched():
    text = "other:\n  mcc:\n    api: theirs\nproviders:\n  mcc:\n    api: ours\n"

    after = _set(text)

    assert "other:\n  mcc:\n    api: theirs\n" in after
    assert yaml.safe_load(after)["other"]["mcc"] == {"api": "theirs"}
    assert yaml.safe_load(after)["providers"]["mcc"] == MCC_TABLE
    assert _delete(after) == "other:\n  mcc:\n    api: theirs\n"


def test_a_comment_inside_the_owned_table_survives_a_re_apply():
    text = (
        "providers:\n"
        "  mcc:\n"
        "    # what this block is for\n"
        "    api: http://old/v1\n"
        "    # keep this note\n"
        "    legacy: true\n"
        "    # and this one, after the last key\n"
        "  mine:\n"
        "    api: http://mine/v1\n"
    )

    after = _set(text)

    assert after == (
        "providers:\n"
        "  mcc:\n"
        "    # what this block is for\n"
        '    api: "http://127.0.0.1:8082/v1"\n'
        "    # keep this note\n"
        "    # and this one, after the last key\n"
        "    key_env: MCC_HERMES_API_KEY\n"
        '    default_model: "anthropic/openrouter/gpt-5"\n'
        "    models:\n"
        '      "anthropic/openrouter/gpt-5":\n'
        "        context_length: 400000\n"
        "  mine:\n"
        "    api: http://mine/v1\n"
    )
    # MCC's own stale key goes: the table is replaced, not merged into.
    assert "legacy" not in after


def test_the_untouched_keys_keep_their_order_and_their_lines():
    text = "zeta: 1\nmodel:\n  provider: openrouter\nalpha: 2\n"

    after = _set(text)

    assert after == (
        "zeta: 1\nmodel:\n  provider: openrouter\nalpha: 2\n" + CREATED_BLOCK
    )
    assert list(yaml.safe_load(after)) == ["zeta", "model", "alpha", "providers"]


def test_replacing_the_table_touches_only_its_own_lines():
    text = (
        "# top\n"
        "model:\n"
        "  provider: openrouter\n"
        "providers:\n"
        "  mcc:\n"
        "    api: http://old/v1\n"
        "  mine:\n"
        "    api: http://mine/v1\n"
        "# bottom\n"
    )

    after = _set(text)

    assert "# top\nmodel:\n  provider: openrouter\n" in after
    assert "  mine:\n    api: http://mine/v1\n# bottom\n" in after
    assert "http://old/v1" not in after


def test_deleting_the_key_prunes_the_parent_mcc_created():
    text = "model:\n  provider: openrouter\n"

    configured = _set(text)

    assert "providers:" in configured
    assert _delete(configured) == text


def test_deleting_the_key_keeps_a_parent_it_did_not_create_when_asked():
    text = "providers:\n  mcc:\n    api: http://old/v1\n"

    assert _delete(text, prune=False) == "providers:\n"


def test_a_document_marker_is_not_taken_for_a_list():
    text = "---\nproviders:\n  mcc:\n    api: http://old/v1\n"

    after = _set(text)

    assert after.count("providers:") == 1
    assert after.startswith("---\n")
    assert yaml.safe_load(after)["providers"]["mcc"] == MCC_TABLE


def test_a_parent_left_holding_a_comment_is_not_pruned():
    text = "providers:\n  mcc:\n    api: http://old/v1\n  # mine\n"

    assert _delete(text) == "providers:\n  # mine\n"


def test_a_parent_left_holding_another_key_is_not_pruned():
    text = "providers:\n  mcc:\n    api: http://old/v1\n  mine:\n    api: x\n"

    assert _delete(text) == "providers:\n  mine:\n    api: x\n"


def test_deleting_a_nested_scalar_removes_its_line():
    text = "providers:\n  mcc: http://old/v1  # inline\n  mine: x\n"

    assert _delete(text) == "providers:\n  mine: x\n"


def test_a_parent_that_is_a_list_is_replaced_with_the_mapping():
    text = "providers:\n  - a\n  - b\n"

    after = _set(text)

    assert yaml.safe_load(after) == {"providers": {"mcc": MCC_TABLE}}


def test_a_parent_holding_a_scalar_is_opened_for_the_table():
    text = "providers: null  # none yet\n"

    after = _set(text)

    assert yaml.safe_load(after) == {"providers": {"mcc": MCC_TABLE}}
    assert "# none yet" in after


def test_an_empty_flow_mapping_is_replaced_with_the_block():
    after = _set("providers: {}\n")

    assert yaml.safe_load(after) == {"providers": {"mcc": MCC_TABLE}}
    assert after.splitlines()[0] == "providers:"


def test_an_empty_document_gains_the_whole_chain():
    after = _set("")

    assert after == CREATED_BLOCK
    assert yaml.safe_load(after) == {"providers": {"mcc": MCC_TABLE}}


def test_a_top_level_table_is_written_the_same_way():
    after = _set("model: x\n", {"api": "y"}, path=("providers",))

    assert after == "model: x\nproviders:\n  api: y\n"


@pytest.mark.parametrize(
    "value",
    [
        "400000",
        "true",
        "null",
        "1.5",
        "http://127.0.0.1:8082/v1",
        "a # b",
        "",
        " leading",
        "gpt-5 (OpenRouter)",
        "yes",
        "MCC_HERMES_API_KEY",
    ],
)
def test_a_string_value_reads_back_as_the_same_string(value: str):
    after = _set("model: x\n", {"value": value})

    assert yaml.safe_load(after)["providers"]["mcc"]["value"] == value


def test_scalar_types_are_written_as_themselves():
    value = {"count": 400000, "ratio": 0.25, "enabled": True, "off": None}

    values = yaml.safe_load(_set("model: x\n", value))["providers"]["mcc"]

    assert values == value


def test_a_sequence_of_scalars_is_written_as_a_block_sequence():
    value = {"input": ["text", "image"]}

    values = yaml.safe_load(_set("model: x\n", value))["providers"]["mcc"]

    assert values == value


def test_a_collection_inside_a_sequence_is_refused():
    with pytest.raises(DocumentFormatError):
        _set("model: x\n", {"rows": [{"id": "a"}]})


def test_a_nested_scalar_edit_is_still_refused():
    # SetTable owns a whole mapping; a scalar inside one is not this PR's scope.
    with pytest.raises(DocumentFormatError):
        apply_edits(
            "providers:\n  mcc:\n    api: x\n",
            DocumentFormat.YAML,
            [SetScalar(("providers", "mcc", "api"), "y")],
        )


def test_a_table_edit_needs_a_key_and_a_mapping():
    with pytest.raises(DocumentFormatError):
        apply_edits("a: 1\n", DocumentFormat.YAML, [SetTable((), {})])
    with pytest.raises(DocumentFormatError):
        not_a_mapping = cast(Mapping[str, object], ["x"])
        apply_edits("a: 1\n", DocumentFormat.YAML, [SetTable(("a",), not_a_mapping)])
    with pytest.raises(DocumentFormatError):
        apply_edits("a: 1\n", DocumentFormat.YAML, [DeleteKey(())])


#: YAML documents that already exist in the suite. They must be unaffected by
#: the new edit path: a no-op edit, an absent-key delete and a create/delete
#: cycle all have to return them byte for byte.
EXISTING_YAML_DOCUMENTS: tuple[str, ...] = (
    # tests/config/test_desktop_apply.py, GOOSE_DOCUMENT
    (
        "# Goose configuration\n"
        "# hand-written, must survive\n"
        "GOOSE_MODEL: gpt-4o\n"
        "extensions:\n"
        "  developer:\n"
        "    enabled: true\n"
    ),
    # tests/contracts/test_mcp_media_off_is_byte_identical.py, DESKTOP_SEEDS
    "# goose\nGOOSE_MODEL: gpt-5.6\nGOOSE_MODE: approve\n",
    # tests/contracts/desktop_configure_undo_baseline.json, goose_desktop:empty
    "",
)


@pytest.mark.parametrize("text", EXISTING_YAML_DOCUMENTS)
def test_every_existing_yaml_document_round_trips_byte_identically(text: str):
    assert apply_edits(text, DocumentFormat.YAML, []) == text
    assert _delete(text) == text
    assert _only_insertions(text, _set(text))
    assert _delete(_set(text)) == text


# ---------------------------------------------------------------------------
# Reading one table back (7.118.0)
#
# ``yaml_mapping_at`` is the read half the nested-table edit needed once a
# *merge target* was YAML: the merge engine asks whether ``providers.mcc.api``
# names this server, and the Coding agents card asks how many models the block
# holds. It walks the same entry grammar the writer writes with, so what MCC
# reads back is exactly what it would have written -- and it is not
# ``parse_document``, which reports a nested block as present with no value.
# ---------------------------------------------------------------------------


def test_a_nested_table_reads_back_as_the_mapping_that_was_written():
    text = (
        "# mine\n"
        "_config_version: 33\n"
        "providers:\n"
        "  mcc:\n"
        '    api: "http://127.0.0.1:8082/v1"\n'
        "    key_env: MCC_HERMES_API_KEY\n"
        "    transport: openai_chat\n"
        "    models:\n"
        '      "openrouter/configured":\n'
        "        context_length: 400000\n"
        '      "openrouter/other": {}\n'
    )

    block = yaml_mapping_at(text, ("providers", "mcc"))

    assert block == {
        "api": "http://127.0.0.1:8082/v1",
        "key_env": "MCC_HERMES_API_KEY",
        "transport": "openai_chat",
        "models": {
            "openrouter/configured": {"context_length": 400000},
            "openrouter/other": {},
        },
    }


def test_reading_the_root_gives_every_section_and_their_order():
    text = "_config_version: 33\nmodel:\n  provider: openrouter\n"

    root = yaml_mapping_at(text, ())
    assert root is not None
    assert list(root) == ["_config_version", "model"]
    assert root["model"] == {"provider": "openrouter"}


def test_a_path_that_is_not_a_mapping_reads_as_none():
    text = "providers: http://not-a-mapping\n"

    assert yaml_mapping_at(text, ("providers", "mcc")) is None
    assert yaml_mapping_at(text, ("absent",)) is None
    assert yaml_mapping_at("providers: [1, 2]\n", ("providers", "models")) is None


def test_an_empty_table_reads_as_empty_rather_than_absent():
    assert yaml_mapping_at("providers:\n  mcc: {}\n", ("providers", "mcc")) == {}
    assert yaml_mapping_at("providers:\n  mcc:\n", ("providers", "mcc")) == {}


def test_a_same_named_key_under_another_parent_is_not_read_as_mccs():
    text = (
        "providers:\n"
        "  myown:\n"
        "    api: https://example.test/v1\n"
        "    models:\n"
        "      theirs: {}\n"
    )

    assert yaml_mapping_at(text, ("providers", "mcc")) is None
    assert yaml_mapping_at(text, ("providers", "myown")) == {
        "api": "https://example.test/v1",
        "models": {"theirs": {}},
    }


def test_a_root_sequence_is_reported_and_never_read_as_a_mapping():
    text = "- id: one\n- id: two\n"

    assert yaml_root_is_sequence(text) is True
    assert yaml_root_is_sequence("---\nkey: value\n") is False
    assert yaml_root_is_sequence("# only a comment\n\n") is False
    assert yaml_mapping_at(text, ()) == {}


def test_what_the_reader_returns_is_what_the_writer_wrote():
    document = {
        "api": "http://127.0.0.1:8082/v1",
        "key_env": "MCC_HERMES_API_KEY",
        "transport": "openai_chat",
        "default_model": "openrouter/configured",
        "models": {
            "openrouter/configured": {"context_length": 400000},
            "openrouter/silent": {},
        },
        "extra_headers": {"x-mcc-harness": "hermes"},
    }

    text = apply_edits(
        "model:\n  provider: openrouter\n",
        DocumentFormat.YAML,
        [SetTable(("providers", "mcc"), document)],
    )

    assert yaml_mapping_at(text, ("providers", "mcc")) == document
