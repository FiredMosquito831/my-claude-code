"""The JSON half of the codec, on documents people actually write.

``config/document_codecs`` edits TOML and YAML as text and, since 6.83.0, JSON
too. These are the properties that make the change worth making: the bytes MCC
does not own survive, a re-apply of the same value is not a write at all, and a
file VS Code is happy with does not make Configure fail.
"""

import pytest

from my_claude_code.config.document_codecs import (
    AddKeyIfAbsent,
    AppendToSequenceIfAbsent,
    DeleteKey,
    DocumentFormat,
    DocumentFormatError,
    RemoveFromSequence,
    SetScalar,
    SetTable,
    apply_edits,
    mask_json_text,
    parse_document,
)

JSONC = """{
    // VS Code ships its own settings file with comments in it.
    "editor.fontSize": 13,
    /* and block comments, which json.loads has never accepted */
    "files.exclude": {
        "**/.git": true
    },
}
"""


def edit(text: str, *edits) -> str:
    return apply_edits(text, DocumentFormat.JSON, list(edits))


def test_a_jsonc_document_parses():
    assert parse_document(JSONC, DocumentFormat.JSON) == {
        "editor.fontSize": 13,
        "files.exclude": {"**/.git": True},
    }


def test_a_comment_is_not_a_string_and_a_string_is_not_a_comment():
    text = '{"url": "https://example.test//path", "n": 1}'
    assert parse_document(text, DocumentFormat.JSON) == {
        "url": "https://example.test//path",
        "n": 1,
    }


def test_an_unparseable_document_raises_rather_than_guessing():
    with pytest.raises(DocumentFormatError):
        parse_document('{"a": 1', DocumentFormat.JSON)


def test_setting_a_key_touches_only_that_key():
    after = edit(JSONC, SetScalar(("mcc.path",), "D:/mcc/roo.json"))
    assert "// VS Code ships its own settings file" in after
    assert "/* and block comments" in after
    assert '    "editor.fontSize": 13,' in after
    assert '"mcc.path": "D:/mcc/roo.json"' in after
    parsed = parse_document(after, DocumentFormat.JSON)
    assert isinstance(parsed, dict)
    assert parsed.get("mcc.path") == "D:/mcc/roo.json"


def test_a_trailing_comma_habit_is_kept_rather_than_doubled():
    after = edit(JSONC, SetScalar(("mcc.path",), "x"))
    assert after.rstrip().endswith("}")
    assert ",," not in after
    assert parse_document(after, DocumentFormat.JSON)


def test_setting_the_same_value_again_changes_nothing_at_all():
    once = edit(JSONC, SetTable(("provider", "mcc"), {"name": "MCC"}))
    twice = edit(once, SetTable(("provider", "mcc"), {"name": "MCC"}))
    assert twice == once


def test_a_document_keeps_its_own_indentation_unit():
    tabs = '{\n\t"a": 1\n}\n'
    after = edit(tabs, SetTable(("provider", "mcc"), {"name": "MCC"}))
    assert '\t"provider": {\n\t\t"mcc": {\n\t\t\t"name": "MCC"' in after


def test_delete_takes_the_key_its_separator_and_its_line():
    once = edit(JSONC, SetTable(("provider", "mcc"), {"name": "MCC"}))
    assert edit(once, DeleteKey(("provider", "mcc"))) == JSONC


def test_delete_prunes_only_the_containers_mcc_emptied():
    text = '{\n  "provider": {\n    "other": {"k": 1},\n    "mcc": {"n": 2}\n  }\n}\n'
    after = edit(text, DeleteKey(("provider", "mcc")))
    assert '"other"' in after
    assert '"mcc"' not in after
    assert parse_document(after, DocumentFormat.JSON) == {
        "provider": {"other": {"k": 1}}
    }


def test_an_empty_document_comes_back_empty():
    filled = edit("{}\n", SetTable(("provider", "mcc"), {"name": "MCC"}))
    assert edit(filled, DeleteKey(("provider", "mcc"))) == "{}\n"


def test_a_key_on_a_shared_line_is_removed_without_joining_lines():
    text = '{"a": 1, "provider": {"mcc": {"n": 1}, "other": 2}}'
    after = edit(text, DeleteKey(("provider", "mcc")))
    assert after == '{"a": 1, "provider": {"other": 2}}'


def test_a_non_object_ancestor_is_replaced_rather_than_merged_into():
    text = '{\n  "provider": "nonsense"\n}\n'
    after = edit(text, SetTable(("provider", "mcc"), {"name": "MCC"}))
    assert parse_document(after, DocumentFormat.JSON) == {
        "provider": {"mcc": {"name": "MCC"}}
    }


def test_masking_replaces_the_value_and_leaves_the_document_valid():
    text = '{\n  "options": {\n    "apiKey": "sk-secret",\n    "baseURL": "u"\n  }\n}\n'
    masked = mask_json_text(text, {"apiKey"})
    assert "sk-secret" not in masked
    assert parse_document(masked, DocumentFormat.JSON) == {
        "options": {"apiKey": "***", "baseURL": "u"}
    }


def test_masking_a_document_it_cannot_parse_returns_it_unchanged():
    assert mask_json_text("not json at all", {"apiKey"}) == "not json at all"


def test_the_array_format_is_still_edited_through_its_object_model():
    with pytest.raises(DocumentFormatError):
        apply_edits("[]", DocumentFormat.JSON_ARRAY, [DeleteKey(("a",))])


# --------------------------------------------------------------------------
# Additive edits (8.20.3): add a key, append an element, and nothing else.
# --------------------------------------------------------------------------


def test_add_if_absent_creates_the_key_and_its_chain():
    after = edit(
        '{\n  "logging": {"level": "info"}\n}\n',
        AddKeyIfAbsent(("agents", "defaults", "models", "mcc/*"), {}),
    )

    assert parse_document(after, DocumentFormat.JSON) == {
        "logging": {"level": "info"},
        "agents": {"defaults": {"models": {"mcc/*": {}}}},
    }


def test_add_if_absent_leaves_an_existing_key_and_its_value_alone():
    text = '{\n  "models": {\n    "mcc/*": {"pickerRuntimes": ["cli"]}\n  }\n}\n'

    assert edit(text, AddKeyIfAbsent(("models", "mcc/*"), {})) == text


def test_add_if_absent_keeps_the_users_entries_and_comments_byte_for_byte():
    text = (
        "// mine\n"
        "{\n"
        '  "models": {\n'
        '    "openai/gpt": {},   // the one I use\n'
        '    "openrouter/auto": {}\n'
        "  }\n"
        "}\n"
    )
    after = edit(text, AddKeyIfAbsent(("models", "mcc/*"), {}))

    assert "// mine" in after
    assert '    "openai/gpt": {},   // the one I use\n' in after
    assert '    "openrouter/auto": {},\n' in after
    assert parse_document(after, DocumentFormat.JSON) == {
        "models": {
            "openai/gpt": {},
            "openrouter/auto": {},
            "mcc/*": {},
        }
    }


def test_add_if_absent_refuses_to_replace_a_value_it_sits_in():
    with pytest.raises(DocumentFormatError):
        edit(
            '{\n  "models": "nonsense"\n}\n',
            AddKeyIfAbsent(("models", "mcc/*"), {}),
        )


def test_append_if_absent_adds_one_element_to_a_block_array():
    text = '{\n  "allow": [\n    "openai/gpt",\n    "openrouter/auto"\n  ]\n}\n'
    after = edit(text, AppendToSequenceIfAbsent(("allow",), "mcc/*"))

    assert after == (
        '{\n  "allow": [\n    "openai/gpt",\n    "openrouter/auto",\n'
        '    "mcc/*"\n  ]\n}\n'
    )
    assert parse_document(after, DocumentFormat.JSON) == {
        "allow": ["openai/gpt", "openrouter/auto", "mcc/*"]
    }


def test_append_if_absent_keeps_a_one_line_array_on_one_line():
    text = '{\n  "allow": ["openai/gpt"]\n}\n'

    assert (
        edit(text, AppendToSequenceIfAbsent(("allow",), "mcc/*"))
        == '{\n  "allow": ["openai/gpt", "mcc/*"]\n}\n'
    )


def test_append_if_absent_respects_a_trailing_comma():
    text = '{\n  "allow": [\n    "openai/gpt",\n  ]\n}\n'
    after = edit(text, AppendToSequenceIfAbsent(("allow",), "mcc/*"))

    assert parse_document(after, DocumentFormat.JSON) == {
        "allow": ["openai/gpt", "mcc/*"]
    }


def test_append_if_absent_is_a_no_op_when_the_element_is_already_there():
    text = '{\n  "allow": ["openai/gpt", "mcc/*"]\n}\n'

    assert edit(text, AppendToSequenceIfAbsent(("allow",), "mcc/*")) == text


def test_append_if_absent_refuses_a_path_that_is_not_an_array():
    with pytest.raises(DocumentFormatError):
        edit(
            '{\n  "allow": "openai/gpt"\n}\n', AppendToSequenceIfAbsent(("allow",), "x")
        )


def test_remove_from_sequence_takes_back_only_its_own_element():
    text = '{\n  "allow": ["openai/gpt", "mcc/*"]\n}\n'

    assert (
        edit(text, RemoveFromSequence(("allow",), "mcc/*"))
        == '{\n  "allow": ["openai/gpt"]\n}\n'
    )


def test_remove_from_sequence_is_a_no_op_when_the_element_is_gone():
    text = '{\n  "allow": ["openai/gpt"]\n}\n'

    assert edit(text, RemoveFromSequence(("allow",), "mcc/*")) == text


def test_an_emptied_container_comes_back_as_the_empty_object_it_was():
    """Q6b, measured on the reported fixture: ``{}`` must not become ``{\\n}``."""

    text = '{\n  "models": {\n    "mode": "merge",\n    "providers": {}\n  }\n}\n'
    filled = edit(text, SetTable(("models", "providers", "mcc"), {"n": 1}))
    assert '"providers": {' in filled

    assert (
        edit(
            filled,
            DeleteKey(("models", "providers", "mcc"), prune_empty_ancestors=False),
        )
        == text
    )


def test_an_emptied_container_keeps_a_comment_the_user_wrote_in_it():
    text = '{\n  "providers": {\n    "mcc": {"n": 1}\n    // mine\n  }\n}\n'
    after = edit(text, DeleteKey(("providers", "mcc"), prune_empty_ancestors=False))

    # Not whitespace between the braces, so the collapse leaves it alone and
    # the user's comment is still there.
    assert after == '{\n  "providers": {\n    // mine\n  }\n}\n'


def test_an_empty_object_elsewhere_in_the_document_is_not_re_rendered():
    text = '{\n  "scripts": {\n  },\n  "providers": {"mcc": {"n": 1}}\n}\n'
    after = edit(text, DeleteKey(("providers", "mcc")))

    assert '"scripts": {\n  }' in after
    assert parse_document(after, DocumentFormat.JSON) == {"scripts": {}}


def test_additive_edits_are_json_only():
    for document_format in (DocumentFormat.TOML, DocumentFormat.YAML):
        with pytest.raises(DocumentFormatError):
            apply_edits("", document_format, [AddKeyIfAbsent(("a",), {})])
