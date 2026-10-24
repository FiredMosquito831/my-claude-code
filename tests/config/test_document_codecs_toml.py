"""The TOML root-scalar edit changes the value and nothing else on the line (7.111.4).

Until 7.111.4 ``_toml_set_root_scalar`` replaced the whole line it matched
with MCC's own ``key = "value"``, so a Codex user's
``model = "gpt-5.6"   # my default`` lost its comment on Configure, and Undo
could only re-render the bare value. These pin the line-keeping edit and the
verbatim ``prior_line`` restore it now makes.
"""

import pytest

from my_claude_code.config.document_codecs import (
    DeleteKey,
    DocumentFormat,
    SetScalar,
    apply_edits,
    root_scalar_line,
)


def _set(text: str, value: object, *, key: str = "model", prior_line: str = "") -> str:
    return apply_edits(
        text, DocumentFormat.TOML, [SetScalar((key,), value, prior_line=prior_line)]
    )


@pytest.mark.parametrize(
    "line, configured",
    [
        # The repro's line: three spaces and a comment after the value.
        ('model = "gpt-5.6"   # my default', 'model = "mcc/best"   # my default'),
        # Tab-indented, a tab before the comment.
        ('\tmodel = "gpt-5.6"\t# tabbed', '\tmodel = "mcc/best"\t# tabbed'),
        # Four-space indent, wide spacing around ``=``.
        ('    model  =  "gpt-5.6"    # spaced', '    model  =  "mcc/best"    # spaced'),
        # A quoted key and a literal-string value keep their spelling.
        ("\"model\" = 'gpt-5.6' # literal", "\"model\" = 'mcc/best' # literal"),
        # A ``#`` inside the string is not the comment.
        ('model = "a#b" # c', 'model = "mcc/best" # c'),
        # No comment at all: the canonical line, as before.
        ('model = "gpt-5.6"', 'model = "mcc/best"'),
    ],
)
def test_setting_a_root_scalar_changes_only_its_value(line: str, configured: str):
    text = f'# mine\n{line}\n\n[profiles.fast]\nmodel = "x"\n'

    assert _set(text, "mcc/best") == text.replace(line, configured)


def test_a_document_without_a_trailing_newline_keeps_its_last_line_and_its_comment():
    assert _set('top = 1\nmodel = "gpt-5.6" # last', "mcc/best") == (
        'top = 1\nmodel = "mcc/best" # last'
    )


def test_a_line_already_holding_the_value_is_left_exactly_as_written():
    text = "model = 'mcc/best'   # mine\n"

    assert _set(text, "mcc/best") == text


def test_a_value_a_literal_string_cannot_hold_is_written_as_a_basic_string():
    assert _set("model = 'gpt' # c\n", "it's") == 'model = "it\'s" # c\n'


def test_the_recorded_line_is_put_back_verbatim():
    original = '  model = "gpt-5.6"   # my default'
    text = 'model = "mcc/best"\n[profiles.fast]\nmodel = "x"\n'

    assert _set(text, "gpt-5.6", prior_line=original) == (
        f'{original}\n[profiles.fast]\nmodel = "x"\n'
    )


def test_a_recorded_line_that_no_longer_says_the_value_is_not_trusted():
    # A line for a different value, or one that is not a single assignment,
    # is never written; the value alone is edited instead.
    text = 'model = "mcc/best" # c\n'

    assert _set(text, "gpt-5.6", prior_line='model = "other" # x') == (
        'model = "gpt-5.6" # c\n'
    )
    assert _set(text, "gpt-5.6", prior_line='model = "gpt-5.6"\n[x]') == (
        'model = "gpt-5.6" # c\n'
    )


def test_a_recorded_line_for_a_key_that_is_gone_is_inserted_as_it_was():
    original = 'model = "gpt-5.6"   # my default'

    assert _set("a = 1\n\n[t]\nb = 2\n", "gpt-5.6", prior_line=original) == (
        f"a = 1\n\n{original}\n[t]\nb = 2\n"
    )


def test_a_value_continuing_onto_the_next_line_is_rewritten_whole_as_before():
    text = 'model = """\nlong"""\n'

    assert _set(text, "mcc/best") == 'model = "mcc/best"\nlong"""\n'


def test_a_missing_key_is_inserted_exactly_as_before():
    assert _set("a = 1\n\n[t]\nb = 2\n", "mcc/best") == (
        'a = 1\n\nmodel = "mcc/best"\n[t]\nb = 2\n'
    )


def test_root_scalar_line_reads_the_users_line_for_toml_only():
    text = '# c\n\tmodel = "x" # mine\n[t]\nmodel = "y"\n'

    assert root_scalar_line(text, DocumentFormat.TOML, ("model",)) == (
        '\tmodel = "x" # mine'
    )
    assert root_scalar_line(text, DocumentFormat.TOML, ("absent",)) == ""
    assert root_scalar_line(text, DocumentFormat.TOML, ("t", "model")) == ""
    assert root_scalar_line("model: x\n", DocumentFormat.YAML, ("model",)) == ""
    assert root_scalar_line('{"model": 1}', DocumentFormat.JSON, ("model",)) == ""


def test_deleting_a_root_scalar_is_unchanged():
    text = 'a = 1\nmodel_provider = "mcc" # x\n[t]\nb = 2\n'

    assert apply_edits(text, DocumentFormat.TOML, [DeleteKey(("model_provider",))]) == (
        "a = 1\n[t]\nb = 2\n"
    )
