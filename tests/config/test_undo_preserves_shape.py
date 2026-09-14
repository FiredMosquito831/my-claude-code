"""Undo gives the file back the shape it had.

``config/desktop_apply.edits_the_text()`` answers ``False`` for any document
with an ``owned_element_path`` -- Claude Desktop's ``_meta.json`` owns *one
element of a list*, which no ``SetTable``/``SetScalar`` key path can name -- so
Configure and Undo both go through the object model and re-serialise with
``_json_text``. Until 7.6.7 that always emitted two-space JSON, so a
hand-authored compact, four-space or tab document came back re-formatted: Undo
returned the file in a shape it had never had, which is worse than it found it.

The invariant: Undo returns the file to the bytes it had before MCC touched it,
or as close as the format allows -- and never *worse* than it found it.

The fixture at the centre of this is the user's real 165-byte `_meta.json`,
copied read-only from
``FCC_PATCH/backups/claude-desktop-configLibrary-20260910-144813/``. Its
sibling in that backup holds a credential and is never read here; this file
holds two ids and a display name.
"""

import json
import pathlib

import pytest

from my_claude_code.config.desktop_apply import _json_shape, _json_text

#: Byte-for-byte the user's real `_meta.json`: two-space indent, and no
#: trailing newline, which is the habit 6.83.0 already had to learn.
REAL_META = (
    "{\n"
    '  "appliedId": "3fd258a0-0379-416e-b3b5-0b72a6ac5392",\n'
    '  "entries": [\n'
    "    {\n"
    '      "id": "3fd258a0-0379-416e-b3b5-0b72a6ac5392",\n'
    '      "name": "Default"\n'
    "    }\n"
    "  ]\n"
    "}"
)

DOCUMENT = {"appliedId": "a", "entries": [{"id": "a", "name": "Default"}]}

SHAPES = {
    "compact": '{"appliedId":"a","entries":[{"id":"a","name":"Default"}]}',
    "compact with spaced punctuation": (
        '{"appliedId": "a", "entries": [{"id": "a", "name": "Default"}]}'
    ),
    "two spaces": json.dumps(DOCUMENT, indent=2),
    "four spaces": json.dumps(DOCUMENT, indent=4),
    "tabs": json.dumps(DOCUMENT, indent="\t"),
}


def test_the_real_meta_json_round_trips_byte_exactly() -> None:
    """165 bytes in, 165 bytes out, and the same 165 bytes."""

    assert len(REAL_META.encode("utf-8")) == 165
    document = json.loads(REAL_META)

    assert _json_text(document, REAL_META) == REAL_META


def test_the_real_meta_json_matches_the_backup_on_disk() -> None:
    """The fixture is the user's actual file, not a plausible-looking one.

    Skipped rather than failed when the backup is not on this machine: the
    round trip above is the assertion, and this only pins where it came from.
    """

    backup = pathlib.Path(
        "C:/Users/fgghk/Downloads/FCC_PATCH/backups/"
        "claude-desktop-configLibrary-20260910-144813/_meta.json"
    )
    if not backup.exists():
        pytest.skip("the reference backup is not on this machine")

    assert backup.read_bytes().decode("utf-8") == REAL_META


@pytest.mark.parametrize("name", sorted(SHAPES))
def test_every_shape_a_person_writes_survives(name: str) -> None:
    """Compact, spaced-compact, two-space, four-space and tab, all identical."""

    text = SHAPES[name]
    document = json.loads(text)

    assert _json_text(document, text) == text, name


@pytest.mark.parametrize("name", sorted(SHAPES))
def test_the_trailing_newline_habit_is_kept_in_every_shape(name: str) -> None:
    text = SHAPES[name] + "\n"
    document = json.loads(text)

    assert _json_text(document, text) == text, name


class TestTheShapeReader:
    def test_an_indented_document_answers_with_its_own_unit(self) -> None:
        assert _json_shape(json.dumps(DOCUMENT, indent=4))[0] == "    "
        assert _json_shape(json.dumps(DOCUMENT, indent="\t"))[0] == "\t"
        assert _json_shape(json.dumps(DOCUMENT, indent=2))[0] == "  "

    def test_the_first_indented_line_is_the_unit_not_the_deepest(self) -> None:
        """A nested document's deepest line is a multiple of the unit, and
        reading that instead would double the indent on every Undo."""

        assert _json_shape(json.dumps(DOCUMENT, indent=3))[0] == "   "

    def test_a_single_line_document_answers_no_indent(self) -> None:
        indent, separators = _json_shape(SHAPES["compact"])

        assert indent is None
        assert separators == (",", ":")

    def test_a_single_line_document_that_spaces_its_punctuation_says_so(
        self,
    ) -> None:
        indent, separators = _json_shape(SHAPES["compact with spaced punctuation"])

        assert indent is None
        assert separators == (", ", ": ")

    def test_a_multi_line_document_with_no_indent_keeps_the_old_default(self) -> None:
        """The one shape that cannot be told apart, and the only place the
        pre-7.6.7 two-space default still applies."""

        assert _json_shape('{\n"a": 1\n}')[0] == 2

    def test_an_empty_before_text_keeps_the_canonical_document(self) -> None:
        """A file that did not exist has no bytes of anybody's to preserve.

        This is a *creation*, not an Undo, and the canonical two-space document
        is the better answer -- which is also what this has always produced.
        """

        assert _json_shape("")[0] == 2
        assert _json_shape("   \n  ")[0] == 2
        assert _json_text(DOCUMENT, "") == json.dumps(DOCUMENT, indent=2) + "\n"

    def test_a_blank_line_is_not_an_indent(self) -> None:
        """Trailing spaces on an otherwise empty line must not become the unit."""

        text = '{\n   \n    "a": 1\n}'

        assert _json_shape(text)[0] == "    "
