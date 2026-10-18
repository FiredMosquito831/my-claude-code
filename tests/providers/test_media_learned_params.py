"""A media host's refusal that names a field is written down -- and only that.

7.98.0, user decisions 4 and 6 of 2026-10-16: MCC learns from a refusal like
Agnes' ``mode is required`` and shows it, and never acts on it. These hold
both halves: the exact refusal bodies from the user's own log become learned
facts of the right kind, and the request that drew them is routed, retried,
charged, logged and answered exactly as it is without the recorder.

The upstream is a MockTransport answering the two bodies verbatim; every key
and host is fake.
"""

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from my_claude_code.application.media.executor import media_route_health_registry
from my_claude_code.application.media.request import MediaRail, MediaRequest
from my_claude_code.config.media_surfaces import (
    gemini_speech_surface,
    video_surfaces,
)
from my_claude_code.config.provider_registry import get_provider_registry
from my_claude_code.config.settings import Settings
from my_claude_code.core import request_log
from my_claude_code.providers.media import leaf as leaf_module
from my_claude_code.providers.media import param_refusal
from my_claude_code.providers.media.param_refusal import (
    REFUSED,
    REQUIRED,
    note_param_refusal,
    param_refusals,
)
from my_claude_code.providers.media.registry import MediaRegistry
from my_claude_code.providers.recovery import (
    FACT_MEDIA_PARAM_REFUSED,
    FACT_MEDIA_PARAM_REQUIRED,
    STATED_FACT_TTL_SECONDS,
    learned_fact_store,
    reset_learned_fact_store,
)
from my_claude_code.providers.recovery.facts import FACT_TTL_SECONDS
from tests.api.support import attempts_as_read, create_test_app

KEY = "sk-agnes-" + "a" * 32
#: The user's two refusal bodies, verbatim from the request log (spec §0.1).
AGNES_VIDEO_400 = (
    b'{"code":"invalid_request","message":"mode is required (request id: '
    b'20261012125824369744566iLErSsKz)","data":{"param":"mode"}}'
)
AGNES_IMAGE_400 = (
    b'{"error":{"message":"image_config is not supported by text image queue '
    b'(request id: 20261004182414434784892jbgsWW6X)","type":"invalid_request",'
    b'"param":"","code":"invalid_request"}}'
)


# -- the parser ----------------------------------------------------------------


def test_the_two_agnes_bodies_name_their_fields() -> None:
    (video,) = param_refusals(400, AGNES_VIDEO_400, sent={"prompt", "model"})
    assert (video.direction, video.field) == (REQUIRED, "mode")
    assert video.message.startswith("mode is required")

    (image,) = param_refusals(
        400, AGNES_IMAGE_400, sent={"prompt", "model", "image_config"}
    )
    assert (image.direction, image.field) == (REFUSED, "image_config")


@pytest.mark.parametrize(
    ("body", "sent", "expected"),
    [
        # OpenAI's own envelope, both directions.
        (
            {
                "error": {
                    "message": "Unsupported parameter: 'quality'.",
                    "param": "quality",
                }
            },
            {"quality"},
            (REFUSED, "quality"),
        ),
        (
            {
                "error": {
                    "message": "Missing required parameter: 'voice'.",
                    "param": "voice",
                }
            },
            set(),
            (REQUIRED, "voice"),
        ),
        # A pydantic gateway's structured field.
        (
            {
                "detail": [
                    {
                        "type": "missing",
                        "loc": ["body", "mode"],
                        "msg": "Field required",
                    }
                ]
            },
            set(),
            (REQUIRED, "mode"),
        ),
        (
            {
                "detail": [
                    {
                        "type": "extra_forbidden",
                        "loc": ["body", "image_config"],
                        "msg": "Extra inputs are not permitted",
                    }
                ]
            },
            {"image_config"},
            (REFUSED, "image_config"),
        ),
        # A sentence with no structured field.
        (
            {"error": {"message": "Unknown parameter: style"}},
            {"style"},
            (REFUSED, "style"),
        ),
    ],
)
def test_every_shape_the_parser_reads(body, sent, expected) -> None:
    (found,) = param_refusals(400, json.dumps(body).encode(), sent=sent | {"model"})
    assert (found.direction, found.field) == expected


@pytest.mark.parametrize(
    ("status", "body", "sent"),
    [
        # Not a 4xx about a field.
        (500, AGNES_VIDEO_400, set()),
        (503, b'{"error":{"message":"video_queue_full"}}', set()),
        (401, AGNES_VIDEO_400, set()),
        (403, AGNES_VIDEO_400, set()),
        (404, AGNES_VIDEO_400, set()),
        (429, AGNES_VIDEO_400, set()),
        # A 400 about a VALUE, not about the field being needed or refused.
        (
            400,
            b'{"error":{"message":"Invalid value for size","param":"size"}}',
            {"size"},
        ),
        (400, b"not json at all", set()),
        (400, b'{"error":{"message":"Malformed request: bad size"}}', {"size"}),
        # Contradicted by the request itself.
        (400, AGNES_IMAGE_400, set()),
        (400, AGNES_VIDEO_400, {"mode"}),
        # A capitalised sentence start is English, not a field.
        (400, b'{"error":{"message":"Authentication is required"}}', set()),
        # A nested field, and the model itself, are never field facts.
        (
            400,
            b'{"error":{"message":"not supported","param":"image_config.size"}}',
            {"image_config"},
        ),
        (400, b'{"error":{"message":"model is not supported"}}', set()),
    ],
)
def test_nothing_is_learned_without_a_field_naming_4xx(status, body, sent) -> None:
    assert param_refusals(status, body, sent=sent | {"model"}) == ()


def test_a_renamed_field_is_learned_under_the_clients_name() -> None:
    body = b'{"error":{"message":"duration is not supported","param":""}}'
    (found,) = param_refusals(
        400, body, sent={"seconds", "model"}, renames={"duration": "seconds"}
    )
    assert (found.direction, found.field) == (REFUSED, "seconds")


def _status_error(status: int, content: bytes) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://agnes.example/v1/videos")
    response = httpx.Response(status, content=content, request=request)
    return httpx.HTTPStatusError("refused", request=request, response=response)


def _video_request(**body: Any) -> MediaRequest:
    return MediaRequest(
        operation="video_create", rail=MediaRail.VIDEO, model="", body=body
    )


def test_a_native_gemini_body_teaches_nothing() -> None:
    """MCC translated that body: a field its host names is not a client's."""

    note_param_refusal(
        _status_error(400, AGNES_VIDEO_400),
        provider_id="gemini",
        model_id="gemini-tts",
        surface=gemini_speech_surface(),
        request=MediaRequest(
            operation="speech", rail=MediaRail.TTS, model="", body={"input": "hi"}
        ),
    )
    assert learned_fact_store().all_facts() == ()


def test_the_recorder_never_raises(monkeypatch) -> None:
    """Total: an error while writing the note is never the request's error."""

    def explode() -> None:
        raise RuntimeError("the store is on fire")

    monkeypatch.setattr(param_refusal, "learned_fact_store", explode)
    note_param_refusal(
        _status_error(400, AGNES_VIDEO_400),
        provider_id="custom_agnes",
        model_id="agnes-video-2.5",
        surface=video_surfaces()[0],
        request=_video_request(prompt="a cat"),
    )
    note_param_refusal(
        ValueError("not an HTTP error"),
        provider_id="custom_agnes",
        model_id="agnes-video-2.5",
        surface=video_surfaces()[0],
        request=_video_request(prompt="a cat"),
    )


def test_the_facts_are_stated_class_and_expire() -> None:
    assert FACT_TTL_SECONDS[FACT_MEDIA_PARAM_REQUIRED] == STATED_FACT_TTL_SECONDS
    assert FACT_TTL_SECONDS[FACT_MEDIA_PARAM_REFUSED] == STATED_FACT_TTL_SECONDS
    note_param_refusal(
        _status_error(400, AGNES_VIDEO_400),
        provider_id="custom_agnes",
        model_id="agnes-video-2.5",
        surface=video_surfaces()[0],
        request=_video_request(prompt="a cat"),
    )
    (fact,) = learned_fact_store().all_facts()
    assert fact.fact_kind == FACT_MEDIA_PARAM_REQUIRED
    assert fact.detail == "video_create:mode"
    assert fact.source == "rejection"
    assert fact.value is True
    # No reader of a provider's recovery memory is handed it: shown only.
    memory = learned_fact_store().memory_for("custom_agnes")
    assert memory.cap_for("agnes-video-2.5") is None


# -- end to end: recorded, and routed identically ---------------------------------


class _Upstream:
    def __init__(self, answers: dict[str, tuple[int, bytes]]) -> None:
        self.answers = answers
        self.seen: list[tuple[str, str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        try:
            body: Any = json.loads(request.content)
        except ValueError:
            body = request.content.decode("latin-1")
        self.seen.append((request.url.host, request.url.path, body))
        status, content = self.answers.get(
            request.url.path, (200, b'{"created":1,"data":[{"b64_json":"AAAA"}]}')
        )
        return httpx.Response(
            status, content=content, headers={"content-type": "application/json"}
        )


def _run(
    monkeypatch,
    home: Path,
    *,
    record: bool,
    path: str,
    body: dict[str, Any],
    log: Path,
    seen_ids: set[str],
) -> dict[str, Any]:
    """One request through the shipping app; everything it left behind.

    ``log`` is the test's own request log (the suite registers one per test);
    the rows this run wrote are the ones not in ``seen_ids``.
    """

    reset_learned_fact_store()
    request_log.reset_request_log_stores()
    monkeypatch.setenv("MCC_CONFIG_DIR", str(home))
    registry = get_provider_registry()
    if "custom_agnes" not in registry.all_descriptors():
        registry.add(
            display_name="Agnes",
            base_url="https://agnes.example/v1",
            api_keys=(KEY,),
            media_operations=("image_generate", "video"),
        )
    if not record:
        monkeypatch.setattr(leaf_module, "note_param_refusal", lambda *a, **k: None)
    else:
        monkeypatch.setattr(leaf_module, "note_param_refusal", note_param_refusal)
    settings = Settings.model_validate(
        {
            "MODEL_IMAGE": "custom_agnes/agnes-image-1",
            "MODEL_IMAGE_FALLBACKS": "custom_agnes/agnes-image-lite",
            "MODEL_VIDEO": "custom_agnes/agnes-video-2.5",
            "MODEL_VIDEO_FALLBACKS": "custom_agnes/agnes-video-lite",
            "PROVIDER_RETRY_ATTEMPTS": "1",
        }
    )
    upstream = _Upstream(
        {
            "/v1/videos": (400, AGNES_VIDEO_400),
            "/v1/images/generations": (400, AGNES_IMAGE_400),
        }
    )
    media = MediaRegistry(transport=httpx.MockTransport(upstream.handler))
    with TestClient(create_test_app(settings, media=media)) as client:
        response = client.post(path, json=body)
    health = media_route_health_registry(settings)
    benches: dict[str, str | None] = {}
    for ref in (
        "custom_agnes/agnes-image-1",
        "custom_agnes/agnes-image-lite",
        "custom_agnes/agnes-video-2.5",
        "custom_agnes/agnes-video-lite",
    ):
        reason = health.why(ref)
        benches[ref] = None if reason is None else reason.sentence()
    facts = [
        (fact.fact_kind, fact.model_id, fact.detail)
        for fact in learned_fact_store().all_facts()
    ]
    request_log.reset_request_log_stores()
    conn = sqlite3.connect(log)
    conn.row_factory = sqlite3.Row
    try:
        rows = [
            dict(row)
            for row in conn.execute("SELECT * FROM requests ORDER BY ts_epoch")
            if row["id"] not in seen_ids
        ]
        seen_ids.update(row["id"] for row in rows)
        attempts = [
            [dict(attempt) for attempt in attempts_as_read(conn, row["id"])]
            for row in rows
        ]
    finally:
        conn.close()
    return {
        "status": response.status_code,
        "answer": response.json(),
        "upstream": upstream.seen,
        "benches": benches,
        "rows": rows,
        "attempts": attempts,
        "facts": facts,
    }


def _noise(first: dict[str, Any], second: dict[str, Any]) -> set[str]:
    """Columns two identical runs disagree on by themselves (ids, clocks)."""

    noisy: set[str] = set()
    for one, two in zip(first["rows"], second["rows"], strict=True):
        noisy |= {name for name in one if one[name] != two.get(name)}
    return noisy


def _no_request_id(value: Any) -> str:
    """MCC's own request id, which every run mints afresh, taken out."""

    return re.sub(r"req_[0-9a-f]+", "req_*", json.dumps(value, sort_keys=True))


def _without(rows: list[dict[str, Any]], names: set[str]) -> list[dict[str, Any]]:
    return [{k: v for k, v in row.items() if k not in names} for row in rows]


def _attempt_shape(attempts: list[list[dict[str, Any]]]) -> list[list[tuple]]:
    keep = ("provider", "model", "outcome", "status_code", "error_kind")
    return [[tuple(a.get(k) for k in keep) for a in rows] for rows in attempts]


@pytest.mark.parametrize(
    ("path", "body", "facts"),
    [
        (
            "/v1/videos",
            {"prompt": "a cat on a skateboard", "seconds": "8"},
            [
                (FACT_MEDIA_PARAM_REQUIRED, "agnes-video-2.5", "video_create:mode"),
                (FACT_MEDIA_PARAM_REQUIRED, "agnes-video-lite", "video_create:mode"),
            ],
        ),
        (
            "/v1/images/generations",
            {"prompt": "a red cube", "image_config": {"aspect_ratio": "16:9"}},
            [
                (
                    FACT_MEDIA_PARAM_REFUSED,
                    "agnes-image-1",
                    "image_generate:image_config",
                ),
                (
                    FACT_MEDIA_PARAM_REFUSED,
                    "agnes-image-lite",
                    "image_generate:image_config",
                ),
            ],
        ),
    ],
)
def test_a_400_naming_param_records_a_fact_and_routes_identically(
    monkeypatch, tmp_path, path, body, facts
) -> None:
    """The request's answer, upstream calls, route, charge and log row are the
    same with the recorder as without it; the only difference is the facts --
    one per model that refused, since the rail tried both."""

    log = tmp_path / "requests.db"
    seen: set[str] = set()
    runs = {
        name: _run(
            monkeypatch,
            tmp_path / name,
            record=name == "recorded",
            path=path,
            body=body,
            log=log,
            seen_ids=seen,
        )
        for name in ("without_a", "without_b", "recorded")
    }
    without_a, without_b, recorded = (
        runs["without_a"],
        runs["without_b"],
        runs["recorded"],
    )
    assert len(recorded["rows"]) == 1

    assert without_a["facts"] == []
    assert sorted(recorded["facts"]) == sorted(facts)

    noise = _noise(without_a, without_b)
    # Calibrated: nothing but identity and clock columns differ by themselves
    # (the error text quotes MCC's own request id).
    clocks = {name for name in noise if name.endswith(("_ms", "_epoch", "_at", "_iso"))}
    assert noise - clocks <= {"id", "error_message"}
    for key in ("status", "upstream", "benches"):
        assert recorded[key] == without_a[key], key
    assert _no_request_id(recorded["answer"]) == _no_request_id(without_a["answer"])
    assert _without(recorded["rows"], noise) == _without(without_a["rows"], noise)
    assert [_no_request_id(row["error_message"]) for row in recorded["rows"]] == [
        _no_request_id(row["error_message"]) for row in without_a["rows"]
    ]
    assert _attempt_shape(recorded["attempts"]) == _attempt_shape(without_a["attempts"])
    # The refusal reached the client as it always did.
    assert recorded["status"] == 400
