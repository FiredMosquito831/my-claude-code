"""The operator's media request-field statements file (7.98.0, decision 5).

``model_overrides.json``'s conventions: folded refs, atomic writes, and a
save that keeps the order things were typed in. A row it cannot read is
dropped with a log line; a file it cannot read is "nothing stated".
"""

import json
from pathlib import Path

from my_claude_code.config.media_request_fields import (
    EMPTY_STATEMENTS,
    OperatorFieldStatement,
    load_media_request_fields,
    save_media_request_fields,
    with_statement,
    without_statement,
)

REF = "custom_agnes/agnes-video-2.5"


def test_a_save_keeps_the_order_things_were_typed_in(tmp_path: Path) -> None:
    path = tmp_path / "media_request_fields.json"
    table = EMPTY_STATEMENTS
    for name in ("mode", "aspect_ratio", "camera"):
        table = with_statement(
            table, REF, "video_create", name, OperatorFieldStatement(state="accepted")
        )
    # Re-stating the first keeps its place.
    table = with_statement(
        table,
        REF,
        "video_create",
        "mode",
        OperatorFieldStatement(state="required", values=("std", "pro")),
    )
    save_media_request_fields(table, path)

    document = json.loads(path.read_text(encoding="utf-8"))
    fields = document["models"][REF]["video_create"]
    assert list(fields) == ["mode", "aspect_ratio", "camera"]
    assert fields["mode"] == {"state": "required", "values": ["std", "pro"]}
    assert load_media_request_fields(path) == table


def test_refs_are_folded_for_lookup(tmp_path: Path) -> None:
    table = with_statement(
        EMPTY_STATEMENTS,
        "Custom_Agnes/Agnes-Video-2.5",
        "video_create",
        "mode",
        OperatorFieldStatement(state="required"),
    )
    assert "mode" in table.for_model(REF)["video_create"]


def test_unusable_rows_are_dropped_and_the_rest_kept(tmp_path: Path) -> None:
    path = tmp_path / "media_request_fields.json"
    path.write_text(
        json.dumps(
            {
                "models": {
                    REF: {
                        "video_create": {
                            "mode": {"state": "required", "since": "2026-10-18"},
                            "bad state": {"state": "required"},
                            "speed": {"state": "sometimes"},
                            "fps": {"state": "accepted", "type": "uuid"},
                            "tags": {"state": "accepted", "values": [{"x": 1}]},
                        },
                        "video_delete": {"mode": {"state": "required"}},
                    },
                    "noslash": {"image_generate": {"n": {"state": "accepted"}}},
                }
            }
        ),
        encoding="utf-8",
    )

    table = load_media_request_fields(path)

    assert list(table.models) == [REF]
    assert list(table.for_model(REF)) == ["video_create"]
    (only,) = table.for_model(REF)["video_create"].items()
    assert only == (
        "mode",
        OperatorFieldStatement(state="required", since="2026-10-18"),
    )


def test_an_unreadable_file_is_nothing_stated(tmp_path: Path) -> None:
    path = tmp_path / "media_request_fields.json"
    assert load_media_request_fields(path) == EMPTY_STATEMENTS
    path.write_text("{not json", encoding="utf-8")
    assert load_media_request_fields(path) == EMPTY_STATEMENTS
    path.write_text("[]", encoding="utf-8")
    assert load_media_request_fields(path) == EMPTY_STATEMENTS


def test_removing_the_last_statement_removes_the_model(tmp_path: Path) -> None:
    table = with_statement(
        EMPTY_STATEMENTS,
        REF,
        "video_create",
        "mode",
        OperatorFieldStatement(state="required"),
    )
    emptied, removed = without_statement(table, REF, "video_create", "mode")
    assert removed is True
    assert emptied.models == {}
    unchanged, removed = without_statement(table, REF, "video_create", "other")
    assert removed is False
    assert unchanged is table
