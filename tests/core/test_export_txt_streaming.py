"""``render_txt(widths=...)`` streams the same bytes the buffered report writes.

7.121.0 sizes a TXT report's columns before its first row so raw log lines can
be streamed instead of held in memory. That is only honest if the two paths
cannot be told apart: same widths, same padding, same overflow, same bytes.
"""

import pytest

from my_claude_code.core import export as export_engine

COLUMNS = ["id", "kind", "n", "text", "extra"]
HEADERS = ["ID", "Kind", "Line", "Text", "Extra (a long header label here)"]

ROWS = [
    {"id": "req_a", "kind": "sse", "n": 1, "text": "short", "extra": None},
    {"id": "req_a", "kind": "sse", "n": 22, "text": "x" * 300, "extra": {"k": [1, 2]}},
    {
        "id": "req_b",
        "kind": "payloads",
        "n": None,
        "text": "ünïcödé — ⟦marker⟧",
        "extra": "",
    },
    {
        "id": "req_with_a_much_longer_identifier_than_forty_characters",
        "kind": "cli",
        "n": 3,
    },
    {"id": "", "kind": "", "n": 0, "text": "tab\there", "extra": [None]},
]


@pytest.mark.parametrize("count", [0, 1, 2, len(ROWS)])
def test_streamed_txt_is_byte_identical_to_the_buffered_report(count: int) -> None:
    rows = ROWS[:count]
    buffered = b"".join(
        export_engine.render_txt(rows, COLUMNS, HEADERS, title="t", summary="s")
    )
    widths = export_engine.txt_column_widths(rows, COLUMNS, HEADERS)
    streamed = b"".join(
        export_engine.render_txt(
            iter(rows), COLUMNS, HEADERS, title="t", summary="s", widths=widths
        )
    )
    assert streamed == buffered


def test_the_streamed_path_reads_one_row_at_a_time() -> None:
    """With widths, the first row is written before the second is read."""

    seen: list[int] = []

    def rows():
        for index, row in enumerate(ROWS):
            seen.append(index)
            yield row

    widths = export_engine.txt_column_widths(ROWS, COLUMNS, HEADERS)
    body = export_engine.render_txt(
        rows(), COLUMNS, HEADERS, title="t", summary="s", widths=widths
    )
    for _ in range(4):  # title, summary, header row, rule
        next(body)
    next(body)  # the first data row
    assert seen == [0]
