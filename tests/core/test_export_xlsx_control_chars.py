"""An XLSX export survives control characters in a cell (7.97.0).

openpyxl refuses the C0 control characters other than tab, line feed and
carriage return (``IllegalCharacterError``), and a prompt pasted from a
terminal carries them (``\\x1b[31m`` colour codes, a stray ``\\x00``): one such
character anywhere in the selection failed the whole export. The XLSX writer
now writes each as its Unicode control picture (``\\x00`` -> U+2400, ``\\x1b``
-> U+241B); every other character and every non-text cell is unchanged, and
CSV, JSON and TXT are not touched at all.
"""

import io
import json

from openpyxl import load_workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

from my_claude_code.core import export as export_engine

PROMPT = "before\x00nul \x1b[31mred\x1b[0m\ttab\nline\rcr \x07bell \x0b\x0c\x1f end é ✓"
COLUMNS = ["prompt", "tokens", "params", "empty"]
HEADERS = ["Prompt", "Tokens", "Params", "Empty"]
ROW = {
    "prompt": PROMPT,
    "tokens": 1234,
    "params": {"note": "\x1b inside json"},
    "empty": None,
}


def _xlsx_cells() -> list[object]:
    rendered = b"".join(export_engine.render_xlsx([ROW], COLUMNS, HEADERS))
    sheet = load_workbook(io.BytesIO(rendered)).active
    assert sheet is not None
    lines = list(sheet.values)
    assert list(lines[0]) == HEADERS
    return list(lines[1])


def test_the_mapped_set_is_exactly_the_set_openpyxl_refuses() -> None:
    for code in range(0x110):
        refused = ILLEGAL_CHARACTERS_RE.search(chr(code)) is not None
        assert (code in export_engine._XLSX_ILLEGAL) is refused, hex(code)


def test_a_prompt_with_control_characters_exports_as_control_pictures() -> None:
    prompt, tokens, params, empty = _xlsx_cells()
    assert prompt == ("before␀nul ␛[31mred␛[0m\ttab\nline\rcr ␇bell ␋␌␟ end é ✓")
    # Tab, line feed and carriage return are legal and stay themselves.
    assert isinstance(prompt, str) and "\t" in prompt and "\n" in prompt
    assert tokens == 1234
    # JSON already escapes control characters, so a dict cell is unchanged.
    assert params == json.dumps(ROW["params"], ensure_ascii=False)
    assert empty is None


def test_text_without_control_characters_is_byte_identical() -> None:
    plain = "Ünïcödé, tabs\tand\nnewlines, emoji ✓ and quotes \"'"
    assert export_engine._xlsx_cell(plain) == plain
    for value in (0, 1.5, True, None, [1, "\x1b"], {"k": "\x00"}):
        expected = (
            ""
            if value is None
            else json.dumps(value, ensure_ascii=False)
            if isinstance(value, (dict, list))
            else value
        )
        assert export_engine._xlsx_cell(value) == expected


def test_csv_json_and_txt_keep_the_characters_as_they_were() -> None:
    csv_text = b"".join(export_engine.render_csv([ROW], COLUMNS, HEADERS)).decode()
    assert "\x00nul \x1b[31mred" in csv_text
    json_text = b"".join(export_engine.render_json_array([ROW])).decode()
    assert json.loads(json_text)[0]["prompt"] == PROMPT
    txt = b"".join(
        export_engine.render_txt([ROW], COLUMNS, HEADERS, "Title", "Summary")
    ).decode()
    assert "\x1b[31mred" in txt
