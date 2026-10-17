"""Non-ASCII words inside an uncompressed row's tool calls are found (7.97.0).

With body compression off a row keeps its bodies inline, and its tool calls as
``json.dumps`` text with ``ensure_ascii`` on: "résumé" is stored as
``r\\u00e9sum\\u00e9``. The inline half of a search is a LIKE over those
columns, so a non-ASCII word inside an uncompressed row's tool calls was never
found, while the same row compressed was (7.77.1). 7.92.0 and 7.93.0 changed
how compressed bodies are searched and left the inline half as it was; this
pins it, and pins that every all-ASCII search is the clause it always was.
"""

from typing import Any

import pytest

from my_claude_code.core import request_log
from my_claude_code.core.request_log import (
    _SEARCHED_COLUMNS,
    RequestLogStore,
    RequestRecord,
)

ACCENT = "café résumé naïve"
ARROWS = "step one → step two — done ✓ 😀"


def _record(request_id: str, **overrides: Any) -> RequestRecord:
    values: dict[str, Any] = {
        "id": request_id,
        "endpoint": "/v1/messages",
        "protocol": "anthropic",
        "requested_model": "claude-sonnet-4-5",
        "provider": "nvidia_nim",
        "resolved_model": "test-model",
        "stream": True,
        "input_text": "hello",
        "output_text": "world",
        "duration_ms": 120.0,
        "status": "success",
    }
    values.update(overrides)
    return RequestRecord(**values)


@pytest.fixture(params=[False, True], ids=["inline", "compressed"])
def store(request, tmp_path) -> RequestLogStore:
    store = RequestLogStore(
        tmp_path / "requests.db", max_rows=1000, compress_bodies=request.param
    )
    store.enqueue(
        _record("accent", tool_calls=[{"name": "Write", "input": {"content": ACCENT}}])
    )
    store.enqueue(
        _record(
            "arrows",
            tool_calls=[{"name": "Bash", "input": {"command": f"echo '{ARROWS}'"}}],
        )
    )
    store.enqueue(_record("plain", input_text="nothing special in here"))
    store.close()
    return store


def _ids(store: RequestLogStore, q: str) -> set[str]:
    rows, total = store.list_requests(q=q)
    assert total == len(rows)
    return {row["id"] for row in rows}


@pytest.mark.parametrize(
    ("q", "expected"),
    [
        ("résumé", {"accent"}),
        ("naïve café", {"accent"}),
        ("→", {"arrows"}),
        ("—", {"arrows"}),
        ("✓", {"arrows"}),
        ("😀", {"arrows"}),
        ("résumé absent", set()),
        # ASCII words, as before.
        ("special", {"plain"}),
        ("zzz", set()),
    ],
)
def test_non_ascii_words_inside_tool_calls_are_found_however_rows_are_stored(
    store: RequestLogStore, q: str, expected: set[str]
) -> None:
    assert _ids(store, q) == expected


def _before_7_97_0(terms: list[str]) -> tuple[str, list[str]]:
    """The inline clause exactly as 7.96.1 built it, in both of its places."""

    inline = " AND ".join(
        "(" + " OR ".join(f"{column} LIKE ?" for column in _SEARCHED_COLUMNS) + ")"
        for _ in terms
    )
    args: list[str] = []
    for term in terms:
        args.extend([f"%{term}%"] * len(_SEARCHED_COLUMNS))
    return inline, args


@pytest.mark.parametrize(
    "q",
    [
        "proxy",
        "proxy 8082",
        "C:\\Users",
        'say "hi"',
        "50% off_peak",
        "UPPER lower MiXeD",
        "a",
    ],
)
def test_an_ascii_search_is_the_clause_it_always_was(q: str) -> None:
    terms = q.split()
    assert request_log._inline_search(terms) == _before_7_97_0(terms)


def test_a_non_ascii_term_adds_only_its_escaped_tool_calls_test() -> None:
    sql, args = request_log._inline_search(["résumé", "plain"])
    old_sql, old_args = _before_7_97_0(["résumé", "plain"])
    first, second = sql.split(" AND ")
    old_first, old_second = old_sql.split(" AND ")
    assert first == old_first[:-1] + " OR tool_calls LIKE ?)"
    assert second == old_second
    assert args == [
        *old_args[: len(_SEARCHED_COLUMNS)],
        "%r\\u00e9sum\\u00e9%",
        *old_args[len(_SEARCHED_COLUMNS) :],
    ]


def test_a_term_json_would_rewrite_unpredictably_gets_no_extra_test() -> None:
    # A quote or backslash beside a non-ASCII character: the stored spelling
    # cannot be predicted from the term, so the clause is the old one.
    for term in ('"é"', "é\\x"):
        assert request_log._inline_search([term]) == _before_7_97_0([term])
