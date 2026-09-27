"""``GET /admin/api/analytics/media``: the Analytics page's Media block (7.67.0).

Media rows sit in ``requests`` beside chat rows. This block reads only the
media ones, over the page's own time window, per rail and per provider/model,
and never changes what the chat stats count. Every id and ref is fake.
"""

import time

import pytest
from fastapi.testclient import TestClient

from my_claude_code.config.settings import Settings
from my_claude_code.core.request_log import (
    MediaJobRecord,
    RequestRecord,
    get_request_log_store,
)
from tests.api.support import create_test_app

NOW = time.time()
HOUR = 3600.0


def _record(request_id: str, operation: str | None, **values) -> RequestRecord:
    endpoint = "/v1/messages" if operation is None else f"/v1/media/{operation}"
    return RequestRecord(
        id=request_id,
        endpoint=endpoint,
        protocol="openai" if operation else "anthropic",
        media_operation=operation,
        status=values.pop("status", "success"),
        ts_epoch=values.pop("ts_epoch", NOW - 60),
        **values,
    )


ROWS = (
    _record("chat-1", None, provider="groq", resolved_model="llama", duration_ms=50.0),
    _record(
        "img-1",
        "image_generate",
        provider="together",
        resolved_model="flux",
        output_image_count=2,
        media_bytes_out=1000,
        duration_ms=100.0,
    ),
    _record(
        "img-2",
        "image_generate",
        provider="together",
        resolved_model="flux",
        status="error",
        duration_ms=400.0,
    ),
    _record(
        "img-3",
        "image_edit",
        provider="xai",
        resolved_model="grok-2-image",
        output_image_count=1,
        media_bytes_out=500,
        duration_ms=1000.0,
    ),
    # An MP3 answer states no length: measured bytes, unmeasured seconds.
    _record(
        "tts-1",
        "speech",
        provider="groq",
        resolved_model="playai-tts",
        media_bytes_out=2048,
        duration_ms=300.0,
    ),
    _record(
        "tts-2",
        "speech",
        provider="groq",
        resolved_model="playai-tts",
        status="cancelled",
    ),
    _record(
        "asr-1",
        "transcribe",
        provider="groq",
        resolved_model="whisper",
        input_audio_seconds=12.5,
        duration_ms=700.0,
    ),
    _record(
        "asr-2",
        "translate",
        provider="groq",
        resolved_model="whisper",
        input_audio_seconds=7.5,
        duration_ms=900.0,
    ),
    _record(
        "vid-1",
        "video_create",
        provider="open_router",
        resolved_model="veo",
        media_job_id="video_1",
        output_video_seconds=8.0,
        duration_ms=2000.0,
    ),
    _record(
        "vid-2",
        "video_create",
        provider="open_router",
        resolved_model="veo",
        status="error",
        duration_ms=150.0,
    ),
    # Written by a newer build: no rail names it, and it is not dropped.
    _record("holo-1", "hologram", provider="acme", resolved_model="h1"),
    # Outside a one-hour window.
    _record(
        "img-old",
        "image_generate",
        provider="together",
        resolved_model="flux",
        output_image_count=9,
        ts_epoch=NOW - 3 * HOUR,
    ),
)


def _job(job_id: str, status: str | None, created_at: float) -> MediaJobRecord:
    return MediaJobRecord(
        job_id=job_id,
        request_id=f"req-{job_id}",
        provider="open_router",
        model="veo",
        upstream_id=f"up-{job_id}",
        created_at=created_at,
        status=status,
    )


@pytest.fixture
def seeded(tmp_path):
    store = get_request_log_store(tmp_path / "requests.db")
    assert store is not None
    for record in ROWS:
        store.enqueue(record)
    store.close()
    store = get_request_log_store(tmp_path / "requests.db")
    assert store is not None
    store.insert_media_job(_job("video_1", "completed", NOW - 50))
    store.insert_media_job(_job("video_2", "in_progress", NOW - 40))
    store.insert_media_job(_job("video_3", None, NOW - 30))
    store.insert_media_job(_job("video_old", "queued", NOW - 3 * HOUR))
    return store


@pytest.fixture
def client():
    return TestClient(create_test_app(), client=("127.0.0.1", 50000))


def _media(client: TestClient, **params) -> dict:
    response = client.get("/admin/api/analytics/media", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _rail(payload: dict, rail: str) -> dict:
    (entry,) = [entry for entry in payload["rails"] if entry["rail"] == rail]
    return entry


def test_the_window_filters_rows_and_chat_rows_never_count(client, seeded):
    windowed = _media(client, since=NOW - HOUR)
    everything = _media(client)

    assert windowed["enabled"] is True
    assert windowed["window"] == {"since": NOW - HOUR, "until": None}
    # 10 media rows in the hour; the chat row is not one of them.
    assert windowed["total"] == 10
    assert everything["total"] == 11
    assert _rail(windowed, "image")["images_out"] == 3
    assert _rail(everything, "image")["images_out"] == 12
    providers = {row["provider"] for row in everything["models"]}
    assert "groq" in providers
    assert not any(row["model"] == "llama" for row in everything["models"])
    # An ``until`` bound trims the other end the same way.
    assert _media(client, until=NOW - 2 * HOUR)["total"] == 1


def test_rows_are_grouped_by_rail_then_provider_and_model(client, seeded):
    payload = _media(client, since=NOW - HOUR)

    assert [(r["rail"], r["label"], r["requests"]) for r in payload["rails"]] == [
        ("image", "Image", 3),
        ("tts", "Speech", 2),
        ("asr", "Transcription", 2),
        ("video", "Video", 2),
        ("hologram", "hologram", 1),
    ]
    image = _rail(payload, "image")
    assert (image["succeeded"], image["failed"], image["cancelled"]) == (2, 1, 0)
    assert image["bytes_out"] == 1500
    speech = _rail(payload, "tts")
    assert (speech["succeeded"], speech["cancelled"]) == (1, 1)
    asr = _rail(payload, "asr")
    assert asr["audio_seconds_in"] == 20.0
    assert asr["audio_seconds_in_measured"] == 2
    video = _rail(payload, "video")
    assert (video["video_jobs"], video["video_seconds"], video["failed"]) == (
        1,
        8.0,
        1,
    )
    by_model = {
        (row["label"], row["provider"], row["model"]): row for row in payload["models"]
    }
    flux = by_model[("Image", "together", "flux")]
    assert (flux["requests"], flux["failed"], flux["images_out"]) == (2, 1, 2)
    assert flux["provider_name"] == "Together AI"
    grok = by_model[("Image", "xai", "grok-2-image")]
    assert grok["images_out"] == 1
    whisper = by_model[("Transcription", "groq", "whisper")]
    assert whisper["requests"] == 2


def test_a_sum_nothing_measured_is_not_measured_never_zero(client, seeded):
    payload = _media(client, since=NOW - HOUR)

    speech = _rail(payload, "tts")
    # MP3 states no length: unknown, not 0 seconds.
    assert speech["audio_seconds_out"] is None
    assert speech["audio_seconds_out_measured"] == 0
    assert speech["bytes_out"] == 2048
    # Nothing on the Speech rail is an image either.
    assert speech["images_out"] is None
    # A rail with no traffic at all: 0 requests, every measure unknown.
    empty = _media(client, since=NOW + HOUR)
    assert empty["total"] == 0
    for rail in empty["rails"]:
        assert rail["requests"] == 0
        assert rail["images_out"] is None
        assert rail["median_duration_ms"] is None
        assert rail["avg_duration_ms"] is None
    assert empty["models"] == []


def test_durations_are_the_median_and_average_of_the_stats_row_path(client, seeded):
    payload = _media(client, since=NOW - HOUR)

    image = _rail(payload, "image")
    # 100, 400, 1000: the interpolated p50 and the plain mean.
    assert image["median_duration_ms"] == 400.0
    assert image["avg_duration_ms"] == 500.0
    flux = next(row for row in payload["models"] if row["model"] == "flux")
    assert flux["median_duration_ms"] == 250.0
    # The cancelled speech row has no duration; it is not counted as 0.
    assert _rail(payload, "tts")["median_duration_ms"] == 300.0


def test_video_job_states_are_counted_for_jobs_created_in_the_window(client, seeded):
    payload = _media(client, since=NOW - HOUR)

    assert payload["job_states"] == {"completed": 1, "in_progress": 1, "unknown": 1}
    (jobs,) = payload["jobs"]
    assert jobs["provider_name"] == "OpenRouter"
    assert jobs["model"] == "veo"
    assert jobs["total"] == 3
    everything = _media(client)
    assert everything["job_states"]["queued"] == 1


def test_the_chat_stats_are_unchanged_and_still_count_every_row(client, seeded):
    """Characterisation, not a change: the chat totals include media rows today.

    ``stats()`` has no ``media_operation`` predicate, so its total is every row
    in the window. This block is additive and leaves that exactly as it was.
    """

    stats = client.get("/admin/api/requests/stats", params={"since": NOW - HOUR})
    assert stats.status_code == 200
    assert stats.json()["total"] == 11


def test_a_disabled_request_log_says_so(monkeypatch, tmp_path):
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    settings = Settings.model_validate({"REQUEST_LOG_ENABLED": "false"})
    client = TestClient(create_test_app(settings), client=("127.0.0.1", 50000))

    assert _media(client) == {"enabled": False}


def test_the_route_is_local_only(seeded):
    client = TestClient(create_test_app(), client=("203.0.113.9", 50000))

    assert client.get("/admin/api/analytics/media").status_code == 403
