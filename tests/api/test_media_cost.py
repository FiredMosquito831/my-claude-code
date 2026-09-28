"""Media requests priced from the chat ladder (7.69.0): reported, models.dev, LiteLLM.

Every price in this file is fake test data written into a fixture catalogue
under the test's own ``MCC_CONFIG_DIR``: models.dev in USD per million tokens,
LiteLLM per token / per image / per second / per character, exactly the units
those two sources publish. No catalogue is fetched and no host is reached --
the upstreams are ``httpx.MockTransport`` fakes.
"""

import asyncio
import base64
import io
import json
import sqlite3
import wave
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from my_claude_code.api import media_capture
from my_claude_code.api.admin_media_routes import media_analytics_payload
from my_claude_code.api.media_capture import (
    MediaPricing,
    price_media_row,
    reported_usd,
)
from my_claude_code.api.request_pricing import media_rate_cards, price_media
from my_claude_code.application.cost import (
    MODE_AUTO,
    MODE_COMPUTED_ONLY,
    MODE_REPORTED_ONLY,
)
from my_claude_code.application.media_cost import MediaUsage, reported_audio_tokens
from my_claude_code.config.settings import Settings
from my_claude_code.core import request_log
from my_claude_code.core.export import (
    DEFAULT_REQUEST_FIELDS,
    REQUEST_FIELD_IDS,
    request_detail_columns,
    request_detail_headers,
)
from my_claude_code.core.gemini_native_media import (
    measured_usage,
    speech_answer,
    token_usage,
    transcript_answer,
)
from my_claude_code.core.openai_speech import parse_speech_stream
from my_claude_code.core.request_log import RequestLogStore, RequestRecord
from my_claude_code.providers.media.registry import MediaRegistry
from my_claude_code.providers.runtime.models_dev import models_dev_cache_path
from tests.api.support import create_test_app

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
PNG_B64 = base64.b64encode(PNG).decode()
XAI = "api.x.ai"
TOGETHER = "api.together.ai"
GROQ = "api.groq.com"
OPENROUTER = "openrouter.ai"
OR_VIDEOS = "/api/v1/videos"
ON = MediaPricing(enabled=True, mode=MODE_AUTO, litellm_enabled=True)
OFF_LITELLM = MediaPricing(enabled=True, mode=MODE_AUTO, litellm_enabled=False)


def _wav(seconds: float, rate: int = 16000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(b"\x00\x00" * int(seconds * rate))
    return buffer.getvalue()


WAV_4S = _wav(4.0)
WAV_SPEECH = _wav(1.5, rate=24000)


# ------------------------------------------------------------- fixtures


def _write_cache(path: Path, index: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"fetched_at": datetime.now(UTC).isoformat(), "index": index}
    path.write_text(json.dumps(payload), encoding="utf-8")


def _models_dev(index: dict[str, Any]) -> None:
    """A models.dev catalogue fixture; ``cost`` in USD per million tokens.

    Written where the lookup reads it: the suite's autouse isolation points
    the models.dev module at this test's own directory.
    """
    _write_cache(models_dev_cache_path(), index)


def _litellm(tmp_path: Path, index: dict[str, Any]) -> None:
    """A LiteLLM price-map fixture, keys spelled as LiteLLM's cost map spells them."""
    _write_cache(tmp_path / "cache" / "litellm-prices.json", index)


def _bucket(provider: str, models: dict[str, dict[str, float]]) -> dict[str, Any]:
    return {
        provider: {
            "id": provider,
            "models": {
                model_id: {"id": model_id, "cost": cost}
                for model_id, cost in models.items()
            },
        }
    }


def _settings(monkeypatch, tmp_path: Path, **values: str) -> Settings:
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    base = {
        "XAI_API_KEY": "xai-" + "a" * 40,
        "TOGETHER_API_KEY": "tg-" + "b" * 40,
        "GROQ_API_KEY": "gsk_" + "c" * 40,
        "OPENROUTER_API_KEY": "sk-or-v1-" + "d" * 48,
        "PROVIDER_RETRY_ATTEMPTS": "1",
        "MODEL_IMAGE": "xai/gpt-image-1",
        "MODEL_TTS": "together/cartesia/sonic-2",
        "MODEL_ASR": "groq/whisper-large-v3",
        "MODEL_VIDEO": "open_router/google/veo-3.1",
    }
    base.update(values)
    return Settings.model_validate(base)


class Upstream:
    """Scripted hosts keyed by ``(method, host, path)``; a missing route is a 404."""

    def __init__(
        self, routes: dict[tuple[str, str, str], list[httpx.Response]]
    ) -> None:
        self.routes = routes
        self.seen: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.seen.append(request)
        queue = self.routes.get((request.method, request.url.host, request.url.path))
        if queue:
            return queue.pop(0) if len(queue) > 1 else queue[0]
        return httpx.Response(404, json={"error": {"message": "no such route"}})


def _client(settings: Settings, upstream: Upstream) -> TestClient:
    registry = MediaRegistry(transport=httpx.MockTransport(upstream.handler))
    return TestClient(create_test_app(settings, media=registry))


def _rows(tmp_path: Path) -> list[sqlite3.Row]:
    # Closing the stores drains the writer thread, so every row is on disk.
    request_log.reset_request_log_stores()
    conn = sqlite3.connect(tmp_path / "requests.db")
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute("SELECT * FROM requests ORDER BY ts_epoch"))
    finally:
        conn.close()


def _image_answer(**usage: Any) -> httpx.Response:
    payload: dict[str, Any] = {"created": 1, "data": [{"b64_json": PNG_B64}]}
    if usage:
        payload["usage"] = usage
    return httpx.Response(200, json=payload)


def _generate_image(
    monkeypatch, tmp_path: Path, answer: httpx.Response, **values: str
) -> sqlite3.Row:
    settings = _settings(monkeypatch, tmp_path, **values)
    upstream = Upstream({("POST", XAI, "/v1/images/generations"): [answer]})
    with _client(settings, upstream) as client:
        response = client.post(
            "/v1/images/generations", json={"model": "mcc/image", "prompt": "a cube"}
        )
    assert response.status_code == 200, response.text
    (row,) = _rows(tmp_path)
    return row


# ---------------------------------------------------------------- tokens


def test_token_priced_gpt_image(monkeypatch, tmp_path) -> None:
    """models.dev token rates (USD/1M) times the host's own token counts."""

    _models_dev(_bucket("xai", {"gpt-image-1": {"input": 5, "output": 40}}))
    row = _generate_image(
        monkeypatch, tmp_path, _image_answer(input_tokens=10, output_tokens=1000)
    )
    assert row["cost_source"] == "models_dev"
    assert row["cost_usd"] == pytest.approx(10 * 5e-6 + 1000 * 40e-6)
    assert (row["tokens_in"], row["tokens_out"]) == (10, 1000)


def test_reported_cost_wins(monkeypatch, tmp_path) -> None:
    """The host's own ``usage.cost`` beats every table, exactly as for chat."""

    _models_dev(_bucket("xai", {"gpt-image-1": {"input": 5, "output": 40}}))
    _litellm(
        tmp_path,
        {"xai/gpt-image-1": {"litellm_provider": "xai", "output_cost_per_image": 0.5}},
    )
    row = _generate_image(
        monkeypatch,
        tmp_path,
        _image_answer(input_tokens=10, output_tokens=1000, cost=0.123, is_byok=False),
        COST_SOURCE_LITELLM_ENABLED="true",
    )
    assert (row["cost_usd"], row["cost_source"]) == (0.123, "provider")


def test_a_bare_cost_is_declined_and_byok_adds_the_upstream_charge() -> None:
    """The chat BYOK rule, unchanged: a ``cost`` that may be a surcharge is no bill."""

    assert reported_usd({"cost": 0.2}) is None
    assert reported_usd({"cost": 0.2, "is_byok": False}) == 0.2
    byok = {
        "cost": 0.01,
        "is_byok": True,
        "cost_details": {"upstream_inference_cost": 0.2},
    }
    assert reported_usd(byok) == pytest.approx(0.21)
    assert reported_usd(None) is None


def test_the_modes_are_chats(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    _models_dev(_bucket("fakehost", {"m": {"input": 1, "output": 2}}))
    usage = MediaUsage(operation="image_generate", tokens_in=100, tokens_out=100)

    def run(mode: str, reported: float | None) -> tuple[float | None, str | None]:
        return price_media(
            "fakehost",
            "m",
            usage,
            reported_usd=reported,
            mode=mode,
            litellm_enabled=False,
        )

    assert run(MODE_AUTO, 0.5) == (0.5, "provider")
    assert run(MODE_REPORTED_ONLY, None) == (None, "unpriced")
    assert run(MODE_COMPUTED_ONLY, 0.5) == (pytest.approx(0.0003), "models_dev")


def test_audio_tokens_price_at_the_audio_rate_only_when_one_is_stated(
    tmp_path, monkeypatch
) -> None:
    """models.dev ``cost.input_audio`` refines the audio part the host itemised."""

    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    _models_dev(
        {
            **_bucket("fakehost", {"asr": {"input": 1, "output": 2, "input_audio": 3}}),
            **_bucket("otherhost", {"asr": {"input": 1, "output": 2}}),
        },
    )
    usage = MediaUsage(
        operation="transcribe",
        tokens_in=1000,
        tokens_out=10,
        input_audio_tokens=800,
    )
    priced, source = price_media(
        "fakehost",
        "asr",
        usage,
        reported_usd=None,
        mode=MODE_AUTO,
        litellm_enabled=False,
    )
    assert source == "models_dev"
    assert priced == pytest.approx(200 * 1e-6 + 800 * 3e-6 + 10 * 2e-6)
    # No audio rate published: the model's own input rate, as chat prices
    # reasoning as output when no reasoning rate is stated.
    plain, _ = price_media(
        "otherhost",
        "asr",
        usage,
        reported_usd=None,
        mode=MODE_AUTO,
        litellm_enabled=False,
    )
    assert plain == pytest.approx(1000 * 1e-6 + 10 * 2e-6)


def test_reported_audio_tokens_read_both_openai_spellings() -> None:
    assert reported_audio_tokens(
        {"input_token_details": {"audio_tokens": 14, "text_tokens": 0}}
    ) == (14, None)
    assert reported_audio_tokens({"output_tokens_details": {"audio_tokens": 90}}) == (
        None,
        90,
    )
    assert reported_audio_tokens({"input_tokens": 5}) == (None, None)
    assert reported_audio_tokens(None) == (None, None)


# ------------------------------------------------------------ LiteLLM units


def test_unit_priced_from_litellm_when_present(tmp_path, monkeypatch) -> None:
    """Per image, per second, per character -- each only for its own operation."""

    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    _litellm(
        tmp_path,
        {
            "fakehost/img": {
                "litellm_provider": "fakehost",
                "output_cost_per_image": 0.5,
            },
            "fakehost/asr": {
                "litellm_provider": "fakehost",
                "input_cost_per_second": 0.25,
                # A transcript has no seconds: never multiplied by anything.
                "output_cost_per_second": 9.0,
            },
            "fakehost/tts": {
                "litellm_provider": "fakehost",
                "input_cost_per_character": 0.001,
                "output_cost_per_second": 9.0,
            },
            "fakehost/tts-seconds": {
                "litellm_provider": "fakehost",
                "output_cost_per_second": 0.01,
            },
            "fakehost/vid": {
                "litellm_provider": "fakehost",
                "output_cost_per_second": 0.75,
            },
        },
    )

    def price(model: str, usage: MediaUsage) -> tuple[float | None, str | None]:
        return price_media(
            "fakehost",
            model,
            usage,
            reported_usd=None,
            mode=MODE_AUTO,
            litellm_enabled=True,
        )

    assert price("img", MediaUsage(operation="image_generate", images_out=2)) == (
        pytest.approx(1.0),
        "litellm",
    )
    assert price(
        "asr", MediaUsage(operation="transcribe", input_audio_seconds=4.0)
    ) == (pytest.approx(1.0), "litellm")
    assert price(
        "tts",
        MediaUsage(operation="speech", input_chars=500, output_audio_seconds=3.0),
    ) == (pytest.approx(0.5), "litellm")
    assert price(
        "tts-seconds", MediaUsage(operation="speech", output_audio_seconds=3.0)
    ) == (pytest.approx(0.03), "litellm")
    assert price(
        "vid", MediaUsage(operation="video_create", output_video_seconds=8.0)
    ) == (pytest.approx(6.0), "litellm")
    # The unit the operation measures was not measured: nothing to multiply.
    assert price("vid", MediaUsage(operation="video_create")) == (None, "unpriced")
    assert price("asr", MediaUsage(operation="transcribe")) == (None, "unpriced")


def test_litellm_per_image_prices_an_image_request_end_to_end(
    monkeypatch, tmp_path
) -> None:
    _litellm(
        tmp_path,
        {"xai/gpt-image-1": {"litellm_provider": "xai", "output_cost_per_image": 0.5}},
    )
    row = _generate_image(
        monkeypatch, tmp_path, _image_answer(), COST_SOURCE_LITELLM_ENABLED="true"
    )
    assert (row["cost_usd"], row["cost_source"]) == (0.5, "litellm")
    assert row["output_image_count"] == 1


def test_speech_priced_per_character_and_transcription_per_second(
    monkeypatch, tmp_path
) -> None:
    _litellm(
        tmp_path,
        {
            "together/cartesia/sonic-2": {
                "litellm_provider": "together_ai",
                "input_cost_per_character": 0.001,
            },
            "groq/whisper-large-v3": {
                "litellm_provider": "groq",
                "input_cost_per_second": 0.25,
                "output_cost_per_second": 0.25,
            },
        },
    )
    settings = _settings(monkeypatch, tmp_path, COST_SOURCE_LITELLM_ENABLED="true")
    upstream = Upstream(
        {
            ("POST", TOGETHER, "/v1/audio/speech"): [
                httpx.Response(
                    200, content=WAV_SPEECH, headers={"content-type": "audio/wav"}
                )
            ],
            ("POST", GROQ, "/openai/v1/audio/transcriptions"): [
                httpx.Response(200, json={"text": "hello world"})
            ],
        }
    )
    with _client(settings, upstream) as client:
        spoken = client.post(
            "/v1/audio/speech",
            json={"model": "mcc/tts", "input": "x" * 40, "voice": "alloy"},
        )
        heard = client.post(
            "/v1/audio/transcriptions",
            data={"model": "whisper-1", "response_format": "json"},
            files=[("file", ("clip.wav", WAV_4S, "audio/wav"))],
        )
    assert spoken.status_code == 200, spoken.text
    assert heard.status_code == 200, heard.text
    speech, transcript = _rows(tmp_path)
    assert speech["media_operation"] == "speech"
    assert (speech["cost_usd"], speech["cost_source"]) == (
        pytest.approx(40 * 0.001),
        "litellm",
    )
    assert transcript["media_operation"] == "transcribe"
    # 4 s of audio heard at the per-second input rate; the output rate stated
    # beside it has no unit to multiply.
    assert (transcript["cost_usd"], transcript["cost_source"]) == (
        pytest.approx(4.0 * 0.25),
        "litellm",
    )


def test_litellm_off_stays_off(monkeypatch, tmp_path) -> None:
    """A LiteLLM source switched off for chat is off for media: the file is ignored."""

    _litellm(
        tmp_path,
        {"xai/gpt-image-1": {"litellm_provider": "xai", "output_cost_per_image": 0.5}},
    )
    row = _generate_image(monkeypatch, tmp_path, _image_answer())
    assert row["cost_usd"] is None
    assert row["cost_source"] == "unpriced"
    assert media_rate_cards("xai", "gpt-image-1", litellm_enabled=False) == ()
    (card,) = media_rate_cards("xai", "gpt-image-1", litellm_enabled=True)
    assert (card.source, card.per_image) == ("litellm", 0.5)


# --------------------------------------------------------------- unpriced


def test_unpriced_is_null_not_zero(monkeypatch, tmp_path) -> None:
    """Nothing publishes a rate: NULL with ``unpriced`` -- and "listed free" is no source."""

    _models_dev(_bucket("xai", {"gpt-image-1": {"input": 0, "output": 0}}))
    row = _generate_image(
        monkeypatch, tmp_path, _image_answer(input_tokens=10, output_tokens=1000)
    )
    assert row["cost_usd"] is None
    assert row["cost_source"] == "unpriced"


def test_a_failed_request_is_never_priced_per_unit(monkeypatch, tmp_path) -> None:
    """A failed speech request still knows its characters; it was not billed for them."""

    _litellm(
        tmp_path,
        {
            "together/cartesia/sonic-2": {
                "litellm_provider": "together_ai",
                "input_cost_per_character": 0.001,
            }
        },
    )
    settings = _settings(monkeypatch, tmp_path, COST_SOURCE_LITELLM_ENABLED="true")
    upstream = Upstream(
        {
            ("POST", TOGETHER, "/v1/audio/speech"): [
                httpx.Response(400, json={"error": {"message": "bad voice"}})
            ]
        }
    )
    with _client(settings, upstream) as client:
        response = client.post(
            "/v1/audio/speech", json={"model": "mcc/tts", "input": "hello"}
        )
    assert response.status_code >= 400
    (row,) = _rows(tmp_path)
    assert row["status"] == "error"
    assert (row["cost_usd"], row["cost_source"]) == (None, "unpriced")


def test_cost_estimation_off_prices_nothing(monkeypatch, tmp_path) -> None:
    _models_dev(_bucket("xai", {"gpt-image-1": {"input": 5, "output": 40}}))
    row = _generate_image(
        monkeypatch,
        tmp_path,
        _image_answer(input_tokens=10, output_tokens=1000, cost=0.1, is_byok=False),
        COST_ESTIMATION_ENABLED="false",
    )
    assert (row["cost_usd"], row["cost_source"]) == (None, None)
    off = MediaPricing(enabled=False, mode=MODE_AUTO, litellm_enabled=True)
    usage = MediaUsage(operation="image_generate", images_out=1)
    assert price_media_row(off, "xai", "gpt-image-1", usage, 0.1) == (None, None)


def test_media_pricing_never_runs_on_the_event_loop(monkeypatch, tmp_path) -> None:
    """The catalogue lookups run on the request log's writer thread."""

    calls: list[str] = []
    real = media_capture.price_media_row

    def sentinel(*args: Any) -> tuple[float | None, str | None]:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            calls.append("priced")
            return real(*args)
        raise AssertionError("media pricing ran on the event loop thread")

    monkeypatch.setattr(media_capture, "price_media_row", sentinel)
    _litellm(
        tmp_path,
        {"xai/gpt-image-1": {"litellm_provider": "xai", "output_cost_per_image": 0.5}},
    )
    row = _generate_image(
        monkeypatch, tmp_path, _image_answer(), COST_SOURCE_LITELLM_ENABLED="true"
    )
    assert calls == ["priced"]
    assert (row["cost_usd"], row["cost_source"]) == (0.5, "litellm")


def test_a_pricer_that_fails_leaves_the_row_written_and_unpriced(tmp_path) -> None:
    def broken() -> tuple[float | None, str | None]:
        raise RuntimeError("catalogue unreadable")

    store = RequestLogStore(tmp_path / "requests.db")
    store.enqueue(
        RequestRecord(id="r1", endpoint="/v1/audio/speech", protocol="x", pricer=broken)
    )
    # A row that already carries a figure keeps it; its pricer is not asked.
    store.enqueue(
        RequestRecord(
            id="r2",
            endpoint="/v1/audio/speech",
            protocol="x",
            cost_usd=0.2,
            cost_source="provider",
            pricer=broken,
        )
    )
    store.close()
    conn = sqlite3.connect(tmp_path / "requests.db")
    try:
        rows = {
            row[0]: row[1:]
            for row in conn.execute("SELECT id, cost_usd, cost_source FROM requests")
        }
    finally:
        conn.close()
    assert rows == {"r1": (None, None), "r2": (0.2, "provider")}


# ------------------------------------------------------------------ video


def _job(job_id: str, status: str, code: int = 200, **extra: Any) -> httpx.Response:
    return httpx.Response(code, json={"id": job_id, "status": status, **extra})


def _video_rows(
    monkeypatch, tmp_path: Path, completed: httpx.Response, **values: str
) -> tuple[sqlite3.Row, sqlite3.Row]:
    """The create row as first written, and again after the completed poll."""

    settings = _settings(monkeypatch, tmp_path, **values)
    upstream = Upstream(
        {
            ("POST", OPENROUTER, OR_VIDEOS): [_job("or-job", "pending", 202)],
            ("GET", OPENROUTER, f"{OR_VIDEOS}/or-job"): [completed],
        }
    )
    with _client(settings, upstream) as client:
        video = client.post("/v1/videos", json={"prompt": "p"}).json()
        (created,) = _rows(tmp_path)
        polled = client.get(f"/v1/videos/{video['id']}").json()
        assert polled["status"] == "completed"
    (finished,) = _rows(tmp_path)
    return created, finished


def test_video_priced_when_seconds_known(monkeypatch, tmp_path) -> None:
    """Unpriced at create; the poll that reads the length prices the create row."""

    _litellm(
        tmp_path,
        {
            "openrouter/google/veo-3.1": {
                "litellm_provider": "openrouter",
                "output_cost_per_second": 0.5,
            }
        },
    )
    created, finished = _video_rows(
        monkeypatch,
        tmp_path,
        _job("or-job", "completed", duration=8),
        COST_SOURCE_LITELLM_ENABLED="true",
    )
    assert created["output_video_seconds"] is None
    assert (created["cost_usd"], created["cost_source"]) == (None, "unpriced")
    assert finished["output_video_seconds"] == 8.0
    assert (finished["cost_usd"], finished["cost_source"]) == (
        pytest.approx(8 * 0.5),
        "litellm",
    )


def test_a_video_jobs_reported_cost_prices_it_on_completion(
    monkeypatch, tmp_path
) -> None:
    _, finished = _video_rows(
        monkeypatch,
        tmp_path,
        _job("or-job", "completed", duration=8, usage={"cost": 0.2, "is_byok": False}),
    )
    assert (finished["cost_usd"], finished["cost_source"]) == (0.2, "provider")


def test_a_video_nothing_prices_stays_unpriced(monkeypatch, tmp_path) -> None:
    _, finished = _video_rows(
        monkeypatch, tmp_path, _job("or-job", "completed", duration=8)
    )
    assert finished["output_video_seconds"] == 8.0
    assert (finished["cost_usd"], finished["cost_source"]) == (None, "unpriced")


def test_a_later_price_never_replaces_one_already_stored(tmp_path) -> None:
    store = RequestLogStore(tmp_path / "requests.db")
    store.enqueue(
        RequestRecord(
            id="v1",
            endpoint="/v1/videos",
            protocol="openai_images",
            media_operation="video_create",
            cost_usd=0.3,
            cost_source="provider",
        )
    )
    store.enqueue(
        RequestRecord(
            id="v2",
            endpoint="/v1/videos",
            protocol="openai_images",
            media_operation="video_create",
            cost_source="unpriced",
        )
    )
    store.close()
    assert store.set_request_video_seconds("v1", 8.0, cost=(4.0, "litellm"))
    assert store.set_request_video_seconds("v2", 8.0, cost=(4.0, "litellm"))
    conn = sqlite3.connect(tmp_path / "requests.db")
    try:
        rows = {
            row[0]: row[1:]
            for row in conn.execute(
                "SELECT id, output_video_seconds, cost_usd, cost_source FROM requests"
            )
        }
    finally:
        conn.close()
    assert rows == {"v1": (8.0, 0.3, "provider"), "v2": (8.0, 4.0, "litellm")}


# ------------------------------------------------------------- Media card


def _media_record(
    request_id: str,
    operation: str | None,
    cost: float | None,
    source: str | None,
    ts: float,
) -> RequestRecord:
    return RequestRecord(
        id=request_id,
        endpoint="/v1/images/generations" if operation else "/v1/messages",
        protocol="openai_images" if operation else "anthropic",
        ts_epoch=ts,
        provider="xai",
        resolved_model="gpt-image-1",
        status="success",
        media_operation=operation,
        cost_usd=cost,
        cost_source=source,
    )


def test_media_block_cost_sums_priced_and_counts_unpriced(tmp_path) -> None:
    store = RequestLogStore(tmp_path / "requests.db")
    for record in (
        _media_record("a", "image_generate", 0.04, "provider", 1.0),
        _media_record("b", "image_generate", 0.08, "litellm", 2.0),
        _media_record("c", "image_generate", None, "unpriced", 3.0),
        _media_record("d", "speech", None, None, 4.0),
        # A chat row is not the Media card's to count, priced or not.
        _media_record("e", None, 1.0, "models_dev", 5.0),
    ):
        store.enqueue(record)
    store.close()
    stats = store.media_stats(groups={"image_generate": "image", "speech": "tts"})
    groups = {group["group"]: group for group in stats["groups"]}
    image = groups["image"]
    assert image["cost_usd"] == pytest.approx(0.12)
    assert image["cost_reported_usd"] == pytest.approx(0.04)
    assert image["cost_estimated_usd"] == pytest.approx(0.08)
    assert image["cost_usd_measured"] == 2
    assert image["cost_unpriced"] == 1
    speech = groups["tts"]
    # Nothing priced: NULL, never 0, and the one request counted as unpriced.
    assert speech["cost_usd"] is None
    assert speech["cost_reported_usd"] is None
    assert speech["cost_usd_measured"] == 0
    assert speech["cost_unpriced"] == 1
    (model_row,) = [row for row in stats["models"] if row["group"] == "image"]
    assert model_row["cost_usd"] == pytest.approx(0.12)
    assert model_row["cost_unpriced"] == 1

    payload = media_analytics_payload(stats)
    rails = {rail["rail"]: rail for rail in payload["rails"]}
    assert rails["image"]["cost_unpriced"] == 1
    # A rail with no traffic: no amount and nothing unpriced.
    assert rails["video"]["cost_usd"] is None
    assert rails["video"]["cost_unpriced"] == 0


# ----------------------------------------------------------------- export


def test_export_has_media_columns(tmp_path) -> None:
    store = RequestLogStore(tmp_path / "requests.db")
    record = _media_record("m1", "transcribe", 1.0, "litellm", 1.0)
    record.input_audio_seconds = 4.0
    store.enqueue(record)
    video = _media_record("m2", "video_create", None, "unpriced", 2.0)
    video.media_job_id = "video_abc"
    store.enqueue(video)
    store.close()

    media_columns = [
        "media_operation",
        "output_image_count",
        "input_audio_seconds",
        "output_audio_seconds",
        "output_video_seconds",
        "media_job_id",
    ]
    columns = request_detail_columns(["media", "cost"])
    assert [column for column in columns if column in media_columns] == media_columns
    assert {"cost_usd", "cost_source"} <= set(columns)
    assert request_detail_headers(media_columns) == [
        "Media operation",
        "Images out",
        "Audio in (s)",
        "Audio out (s)",
        "Video (s)",
        "Video job",
    ]
    rows = list(store.iter_export_rows(columns=columns, need_bodies=False))
    by_id = {row["id"]: row for row in rows}
    assert by_id["m1"]["media_operation"] == "transcribe"
    assert by_id["m1"]["input_audio_seconds"] == 4.0
    assert (by_id["m1"]["cost_usd"], by_id["m1"]["cost_source"]) == (1.0, "litellm")
    assert by_id["m2"]["media_job_id"] == "video_abc"
    assert by_id["m2"]["cost_usd"] is None
    # Opt-in: the default export and every field but ``media`` carry none of them.
    assert "media" in REQUEST_FIELD_IDS and "media" not in DEFAULT_REQUEST_FIELDS
    assert REQUEST_FIELD_IDS[-1] == "origin"
    others = [field for field in REQUEST_FIELD_IDS if field != "media"]
    assert not set(request_detail_columns(others)) & set(media_columns)


# ------------------------------------------------ what the hosts reported


def test_gemini_audio_tokens_reach_the_log_never_the_client() -> None:
    payload = {
        "candidates": [{"content": {"parts": [{"text": "hello"}]}}],
        "usageMetadata": {
            "promptTokenCount": 110,
            "candidatesTokenCount": 3,
            "totalTokenCount": 113,
            "promptTokensDetails": [
                {"modality": "TEXT", "tokenCount": 10},
                {"modality": "AUDIO", "tokenCount": 100},
            ],
        },
    }
    measured = measured_usage(payload)
    assert measured is not None
    assert measured["input_token_details"] == {"audio_tokens": 100}
    assert "output_token_details" not in measured
    assert reported_audio_tokens(measured) == (100, None)

    answer = transcript_answer(json.dumps(payload).encode(), "json")
    # The client's transcript keeps the three counters it always had.
    assert json.loads(answer.body)["usage"] == token_usage(payload)
    assert answer.usage == measured


def test_gemini_speech_usage_carries_its_audio_output_tokens() -> None:
    pcm = base64.b64encode(b"\x00\x01" * 24).decode()
    payload = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "inlineData": {
                                "mimeType": "audio/L16;rate=24000",
                                "data": pcm,
                            }
                        }
                    ]
                }
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 7,
            "candidatesTokenCount": 120,
            "candidatesTokensDetails": [{"modality": "AUDIO", "tokenCount": 120}],
        },
    }
    answer = speech_answer(json.dumps(payload).encode(), None)
    assert answer.usage is not None
    assert answer.usage["output_token_details"] == {"audio_tokens": 120}
    assert answer.usage["input_tokens"] == 7


def test_a_streamed_speech_answer_keeps_its_usage() -> None:
    frames = (
        b'data: {"type": "speech.audio.delta", "audio": "AAAA"}\n\n'
        b'data: {"type": "speech.audio.done", "usage": '
        b'{"input_tokens": 14, "output_tokens": 101, "total_tokens": 115}}\n\n'
    )
    outputs = parse_speech_stream(frames)
    assert outputs.usage == {
        "input_tokens": 14,
        "output_tokens": 101,
        "total_tokens": 115,
    }
    assert outputs.count == 1
    assert parse_speech_stream(b"").usage is None
