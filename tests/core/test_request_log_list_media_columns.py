"""The request list carries what a media request did (7.95.0, user decision 3).

Six columns that already existed on every media row since 7.60.0-7.69.0 --
the operation, the images that came back, the audio and video lengths, the
video job -- are now projected by the list query, so a Requests row can say
"1 image generated" without being opened. Additive JSON keys only: NULL on a
chat row and on any row written before the media columns, and every key the
list carried before carries the same value.

Every value in this file is fake.
"""

from typing import Any

import pytest

from my_claude_code.core.request_log import (
    _LIST_METADATA_COLUMNS,
    RequestLogStore,
    RequestRecord,
)

MEDIA_COLUMNS = (
    "media_operation",
    "output_image_count",
    "output_audio_seconds",
    "input_audio_seconds",
    "output_video_seconds",
    "media_job_id",
)
BASE_TS = 1_789_500_000.0


def _record(request_id: str, offset: int, **overrides: Any) -> RequestRecord:
    values: dict[str, Any] = {
        "id": request_id,
        "endpoint": "/v1/messages",
        "protocol": "anthropic",
        "ts_epoch": BASE_TS + offset,
        "requested_model": "claude-sonnet-4-5",
        "provider": "nvidia_nim",
        "resolved_model": "test-model",
        "stream": True,
        "tokens_in": 10,
        "tokens_out": 20,
        "duration_ms": 120.0,
        "status": "success",
        "input_text": "hi",
        "output_text": "hello",
    }
    values.update(overrides)
    return RequestRecord(**values)


@pytest.fixture
def store(tmp_path):
    store = RequestLogStore(tmp_path / "requests.db", max_rows=10_000)
    yield store
    store.close()


def test_list_rows_carry_media_columns_and_chat_rows_null(store) -> None:
    store.enqueue(_record("chat", 0))
    store.enqueue(
        _record(
            "image",
            1,
            endpoint="/v1/images/generations",
            protocol="openai_images",
            stream=False,
            media_operation="image_generate",
            output_image_count=2,
            output_text=None,
        )
    )
    store.enqueue(
        _record(
            "speech",
            2,
            endpoint="/v1/audio/speech",
            protocol="openai_speech",
            stream=False,
            media_operation="speech",
            output_audio_seconds=4.25,
            output_text=None,
        )
    )
    store.enqueue(
        _record(
            "asr",
            3,
            endpoint="/v1/audio/transcriptions",
            protocol="openai_transcriptions",
            stream=False,
            media_operation="transcribe",
            input_audio_seconds=31.0,
        )
    )
    store.enqueue(
        _record(
            "video",
            4,
            endpoint="/v1/videos",
            protocol="openai_videos",
            stream=False,
            media_operation="video_create",
            media_job_id="video_abc",
            output_text=None,
        )
    )
    store.close()

    rows, total, _more = store.list_requests_page(limit=10)
    assert total == 5
    by_id = {row["id"]: row for row in rows}
    for row in rows:
        assert set(MEDIA_COLUMNS) <= row.keys()
    assert {column: by_id["chat"][column] for column in MEDIA_COLUMNS} == dict.fromkeys(
        MEDIA_COLUMNS
    )
    assert by_id["image"]["media_operation"] == "image_generate"
    assert by_id["image"]["output_image_count"] == 2
    assert by_id["speech"]["output_audio_seconds"] == 4.25
    assert by_id["asr"]["input_audio_seconds"] == 31.0
    assert by_id["video"]["media_job_id"] == "video_abc"
    assert by_id["video"]["output_video_seconds"] is None


def test_the_media_columns_are_appended_after_every_existing_one() -> None:
    """Additive: the six are the list's last columns, after the origin five."""
    assert _LIST_METADATA_COLUMNS[-len(MEDIA_COLUMNS) :] == MEDIA_COLUMNS
    assert _LIST_METADATA_COLUMNS[-len(MEDIA_COLUMNS) - 1] == "origin_source"
    assert len(set(_LIST_METADATA_COLUMNS)) == len(_LIST_METADATA_COLUMNS)


def test_a_chat_row_keeps_every_key_it_had(store) -> None:
    """Equality: removing the six keys gives the row exactly as before 7.95.0."""
    store.enqueue(_record("chat", 0))
    store.close()
    (row,), _total, _more = store.list_requests_page(limit=10)
    for column in MEDIA_COLUMNS:
        assert row.pop(column) is None
    assert row["id"] == "chat"
    assert row["status"] == "success"
    assert row["tokens_in"] == 10
