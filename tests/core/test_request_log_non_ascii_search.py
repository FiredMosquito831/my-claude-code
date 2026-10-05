"""Content search finds non-ASCII words inside stored bodies (7.77.1).

Bodies are packed as JSON with ``ensure_ascii`` on, so "ș" is stored as the six
characters ``\\u0219``. The byte pre-filter used to look for the UTF-8 bytes of
"ș" in that blob, never found them, and rejected every row before the decoded
text was checked: a search for "ș", "ă", "—" or "→" returned 0.
"""

from typing import Any

import pytest

from my_claude_code.core import request_log
from my_claude_code.core.request_log import RequestLogStore, RequestRecord

_TEXTS = {
    "ro": "Pe șosea, lângă casă, am văzut-o ieri.",
    "dash": "one — two → three … done ✓",
    "emoji": "deploy finished 😀 at last",
    "accent": "café résumé naïve",
    # A backslash, then "u0219": the escape written out, not the character.
    "escaped": "the six characters " + "\\" + "u0219 written out",
    "plain": "nothing special in here at all",
}


def _record(request_id: str, **overrides) -> RequestRecord:
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


@pytest.fixture(params=[True, False], ids=["compressed", "inline"])
def searchable(request, tmp_path):
    store = RequestLogStore(
        tmp_path / "requests.db", max_rows=1000, compress_bodies=request.param
    )
    store.enqueue(_record("ro", input_text=_TEXTS["ro"]))
    store.enqueue(_record("dash", output_text=_TEXTS["dash"]))
    store.enqueue(_record("emoji", thinking_text=_TEXTS["emoji"]))
    store.enqueue(_record("accent", output_text=_TEXTS["accent"]))
    store.enqueue(_record("escaped", input_text=_TEXTS["escaped"]))
    store.enqueue(_record("plain", input_text=_TEXTS["plain"]))
    store.close()
    yield store


def _ids(store: RequestLogStore, q: str) -> set[str]:
    rows, total = store.list_requests(q=q)
    assert total == len(rows)
    return {row["id"] for row in rows}


@pytest.mark.parametrize(
    ("q", "expected"),
    [
        ("ș", {"ro"}),
        ("ă", {"ro"}),
        ("lângă", {"ro"}),
        ("—", {"dash"}),
        ("→", {"dash"}),
        ("…", {"dash"}),
        ("✓", {"dash"}),
        ("😀", {"emoji"}),
        ("résumé", {"accent"}),
        # Every term must appear, as for ASCII words.
        ("șosea casă", {"ro"}),
        ("șosea absent", set()),
        # Case folding stays ASCII-only, as SQLite's LIKE: "Ș" is not "ș",
        # while the ASCII letters around it still fold.
        ("Ș", set()),
        ("PE șosea", {"ro"}),
        ("CAFÉ", set()),
        ("CAFé", {"accent"}),
    ],
)
def test_non_ascii_words_are_found_whichever_way_bodies_are_stored(
    searchable: RequestLogStore, q: str, expected: set[str]
) -> None:
    assert _ids(searchable, q) == expected


def test_the_escape_sequence_written_out_is_not_the_character(
    searchable: RequestLogStore,
) -> None:
    # The blob holding r"ș" contains the bytes of the escaped "ș" too;
    # the decoded text decides, and it holds a backslash, not "ș".
    assert _ids(searchable, "ș") == {"ro"}
    assert _ids(searchable, "\\" + "u0219") == {"escaped"}


def test_ascii_words_find_exactly_what_they_found(searchable: RequestLogStore) -> None:
    assert _ids(searchable, "deploy") == {"emoji"}
    assert _ids(searchable, "SPECIAL") == {"plain"}
    assert _ids(searchable, "written out") == {"escaped"}
    assert _ids(searchable, "u0219") == {"escaped"}
    assert _ids(searchable, "zzz") == set()


def test_non_ascii_words_inside_compressed_tool_calls_are_found(tmp_path) -> None:
    store = RequestLogStore(tmp_path / "requests.db", max_rows=1000)
    store.enqueue(
        _record(
            "tool",
            tool_calls=[{"name": "Write", "input": {"content": _TEXTS["accent"]}}],
        )
    )
    store.enqueue(_record("other", input_text=_TEXTS["plain"]))
    store.close()
    assert _ids(store, "résumé") == {"tool"}
    assert _ids(store, "naïve café") == {"tool"}


def test_a_non_ascii_word_still_skips_rows_without_decoding_them(
    tmp_path, monkeypatch
) -> None:
    # The pre-filter keeps a search cheap: a row whose blob cannot contain the
    # word is rejected before its JSON is parsed.
    store = RequestLogStore(tmp_path / "requests.db", max_rows=1000)
    for index in range(20):
        store.enqueue(_record(f"r{index}", input_text=f"plain row {index}"))
    store.enqueue(_record("hit", input_text=_TEXTS["ro"]))
    store.close()
    parsed: list[bytes] = []
    real = request_log.unpack_bodies

    def counting(raw: bytes):
        parsed.append(raw)
        return real(raw)

    monkeypatch.setattr(request_log, "unpack_bodies", counting)
    assert _ids(store, "ș") == {"hit"}
    # Only the matching request's blobs were ever parsed (once per query that
    # evaluated it), none of the twenty without the word.
    assert parsed
    assert not [raw for raw in parsed if b"plain row" in raw]
