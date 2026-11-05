"""Mistral image generation end to end: the chat tool, the file fetch, the row.

Spec §8.2's ten assertions, on the shipping app over a real provider manager;
only the upstream HTTP exchange is a ``MockTransport``, which records every
request so the wire is asserted rather than assumed.

The shape under test: ``POST /v1/images/generations`` from the client is
answered upstream by ``POST {base}/chat/completions`` carrying the
``image_generation`` built-in tool, and the picture is then fetched with
``GET {base}/files/{id}/content`` -- a SECOND call, on the key and the leg
that answered the first.
"""

import base64
import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from my_claude_code.config.settings import Settings
from my_claude_code.core import request_log
from my_claude_code.core.media_store import media_file_path, media_root
from my_claude_code.core.mistral_media import images_answer
from my_claude_code.providers.media.registry import MediaRegistry
from tests.api.support import attempts_as_read, create_test_app, requests_as_read

#: A Mistral-shaped fake key: alphanumeric of the host's own shape, never a
#: real one, and never a ``demo-`` prefix (which made MCC mark eight providers
#: "Missing key" once -- WORKING-NOTES II.6 rule 5).
FAKE_KEY = "MistralFakeKey" + "0123456789abcdef" * 2

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
PNG_B64 = base64.b64encode(PNG).decode()
FILE_ID = "file_abc123def456"


def _chat_answer(
    *,
    file_id: str | None = FILE_ID,
    b64: str | None = None,
    url: str | None = None,
    text: str | None = "Here is your image.",
    created: int = 1_700_000_000,
    usage: dict[str, Any] | None = None,
) -> httpx.Response:
    """A chat answer carrying the image tool's result."""

    message: dict[str, Any] = {"role": "assistant", "content": text}
    if file_id is not None:
        message["tool_calls"] = [
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "image_generation",
                    "arguments": json.dumps({"file_id": file_id}),
                },
            }
        ]
    if b64 is not None:
        message["b64_json"] = b64
    if url is not None:
        message["url"] = url
    payload: dict[str, Any] = {
        "id": "cmpl-1",
        "created": created,
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
    }
    if usage is not None:
        payload["usage"] = usage
    return httpx.Response(200, json=payload)


class Upstream:
    """Scripted upstream: answers per (method, path), records every request."""

    def __init__(
        self,
        chat: list[httpx.Response] | None = None,
        content: list[httpx.Response] | None = None,
    ) -> None:
        self.chat = list(chat or [])
        self.content = list(content or [])
        self.seen: list[tuple[str, str, str, dict[str, Any]]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = {}
        if request.content:
            body = json.loads(request.content)
        self.seen.append(
            (
                request.method,
                request.url.path,
                request.headers.get("authorization", ""),
                body,
            )
        )
        if request.method == "GET":
            queue = self.content
            if queue:
                return queue.pop(0) if len(queue) > 1 else queue[0]
            return httpx.Response(
                200, content=PNG, headers={"content-type": "image/png"}
            )
        queue = self.chat
        if queue:
            return queue.pop(0) if len(queue) > 1 else queue[0]
        return _chat_answer()

    @property
    def calls(self) -> list[tuple[str, str]]:
        return [(method, path) for method, path, _auth, _body in self.seen]


def _settings(monkeypatch, tmp_path: Path, **values: str) -> Settings:
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    base = {
        "MISTRAL_API_KEY": FAKE_KEY,
        "PROVIDER_RETRY_ATTEMPTS": "1",
        "MODEL_IMAGE": "mistral/mistral-medium-3-5",
    }
    base.update(values)
    return Settings.model_validate(base)


def _client(settings: Settings, upstream: Upstream) -> TestClient:
    registry = MediaRegistry(transport=httpx.MockTransport(upstream.handler))
    return TestClient(create_test_app(settings, media=registry))


def _rows(tmp_path: Path) -> list[sqlite3.Row]:
    request_log.reset_request_log_stores()
    conn = sqlite3.connect(tmp_path / "requests.db")
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute("SELECT * FROM requests ORDER BY ts_epoch"))
    finally:
        conn.close()


def _read_rows(tmp_path: Path) -> list[dict[str, Any]]:
    """The rows as the log reads them back, ``params`` decoded."""

    request_log.reset_request_log_stores()
    conn = sqlite3.connect(tmp_path / "requests.db")
    conn.row_factory = sqlite3.Row
    try:
        rows = requests_as_read(
            conn, conn.execute("SELECT * FROM requests ORDER BY ts_epoch").fetchall()
        )
    finally:
        conn.close()
    for row in rows:
        if isinstance(row.get("params"), str):
            row["params"] = json.loads(row["params"])
    return rows


def _attempts(tmp_path: Path, request_id: str) -> list[Mapping[str, Any]]:
    conn = sqlite3.connect(tmp_path / "requests.db")
    try:
        return attempts_as_read(conn, request_id)
    finally:
        conn.close()


# ------------------------------------------------------- 1-5: the happy path


def test_answers_with_the_picture_and_makes_exactly_two_calls(
    monkeypatch, tmp_path
) -> None:
    """§8.2 (1)-(3): one client request, two upstream calls, same key and leg."""

    settings = _settings(monkeypatch, tmp_path)
    upstream = Upstream(
        chat=[_chat_answer(usage={"prompt_tokens": 9, "completion_tokens": 4})]
    )
    with _client(settings, upstream) as client:
        response = client.post(
            "/v1/images/generations",
            json={"model": "gpt-image-2", "prompt": "a red cube", "size": "1024x1024"},
        )
    assert response.status_code == 200, response.text
    assert response.json()["data"][0]["b64_json"] == PNG_B64

    # (3) exactly two upstream calls, the second on the same key.
    assert upstream.calls == [
        ("POST", "/v1/chat/completions"),
        ("GET", f"/v1/files/{FILE_ID}/content"),
    ]
    assert {seen[2] for seen in upstream.seen} == {f"Bearer {FAKE_KEY}"}

    # The chat body: the prompt as one user message, the tool, no stream.
    _method, _path, _auth, body = upstream.seen[0]
    assert body == {
        "model": "mistral-medium-3-5",
        "messages": [{"role": "user", "content": "a red cube"}],
        "tools": [{"type": "image_generation"}],
        "stream": False,
    }


def test_the_row_names_the_door_the_provider_the_count_and_the_sha(
    monkeypatch, tmp_path
) -> None:
    """§8.2 (4)-(5): one ``requests`` row and one ``request_attempts`` row."""

    settings = _settings(monkeypatch, tmp_path)
    upstream = Upstream(
        chat=[_chat_answer(usage={"prompt_tokens": 9, "completion_tokens": 4})]
    )
    with _client(settings, upstream) as client:
        client.post("/v1/images/generations", json={"prompt": "a red cube"})

    (row,) = _rows(tmp_path)
    assert row["endpoint"] == "/v1/images/generations"
    assert row["provider"] == "mistral"
    assert row["media_operation"] == "image_generate"
    assert row["output_image_count"] == 1
    assert row["media_bytes_out"] == len(PNG)
    assert row["media_sha_out"] is not None
    assert row["status"] == "success"
    # The chat call's token usage, in the counters a media row reads.
    assert row["tokens_in"] == 9
    assert row["tokens_out"] == 4
    # Q3: the prose the chat answer carried is measured on the row (the text
    # itself lives in the row's packed body, so its length is the column) and
    # is nowhere in the client's answer -- asserted in its own test below.
    assert row["output_chars"] == len("Here is your image.")
    # The masked fake key, never the key.
    label = row["key_label"]
    assert label is not None
    assert FAKE_KEY not in str(label)
    assert label.startswith(FAKE_KEY[:4])

    attempts = _attempts(tmp_path, row["id"])
    assert [attempt["provider"] for attempt in attempts] == ["mistral"]
    assert [attempt["outcome"] for attempt in attempts] == ["succeeded"]


def test_every_images_field_but_the_prompt_is_recorded_as_not_forwarded(
    monkeypatch, tmp_path
) -> None:
    """Non-negotiable 3: never silently dropped, never translated by guess."""

    settings = _settings(monkeypatch, tmp_path)
    upstream = Upstream()
    with _client(settings, upstream) as client:
        client.post(
            "/v1/images/generations",
            json={
                "prompt": "p",
                "n": 2,
                "size": "1024x1024",
                "quality": "hd",
                "response_format": "b64_json",
                "style": "vivid",
            },
        )
    (row,) = _read_rows(tmp_path)
    media = row["params"]["media"]
    assert sorted(media["not_forwarded"]) == [
        "n",
        "quality",
        "response_format",
        "size",
        "style",
    ]
    # And none of them reached the host.
    _method, _path, _auth, body = upstream.seen[0]
    assert set(body) == {"model", "messages", "tools", "stream"}


# ----------------------------------------------------- 6-7: the media store


def test_the_picture_is_stored_when_the_store_is_on(monkeypatch, tmp_path) -> None:
    """§8.2 (6): ``media/<sha[:2]>/<sha>.png`` on disk, ``stored=1``."""

    settings = _settings(monkeypatch, tmp_path, MEDIA_STORE_ENABLED="true")
    upstream = Upstream()
    with _client(settings, upstream) as client:
        response = client.post("/v1/images/generations", json={"prompt": "p"})
    assert response.status_code == 200
    (row,) = _rows(tmp_path)
    sha = row["media_sha_out"]
    assert sha is not None
    path = media_file_path(media_root(tmp_path / "requests.db"), sha, "image/png")
    assert path.read_bytes() == PNG
    conn = sqlite3.connect(tmp_path / "requests.db")
    try:
        assert conn.execute("SELECT stored FROM media_blobs").fetchall() == [(1,)]
    finally:
        conn.close()


def test_metadata_is_written_and_nothing_stored_when_the_store_is_off(
    monkeypatch, tmp_path
) -> None:
    """§8.2 (7): the row still measures; no file is written."""

    settings = _settings(monkeypatch, tmp_path, MEDIA_STORE_ENABLED="false")
    upstream = Upstream()
    with _client(settings, upstream) as client:
        client.post("/v1/images/generations", json={"prompt": "p"})
    (row,) = _rows(tmp_path)
    assert row["media_sha_out"] is not None
    assert row["media_bytes_out"] == len(PNG)
    assert row["output_image_count"] == 1
    conn = sqlite3.connect(tmp_path / "requests.db")
    try:
        assert conn.execute("SELECT stored FROM media_blobs").fetchall() == [(0,)]
    finally:
        conn.close()
    root = media_root(tmp_path / "requests.db")
    assert not list(root.rglob("*.png"))


# ------------------------------------------------- 8: a failed file fetch


def test_a_404_on_the_file_fetch_fails_the_attempt_and_falls_through(
    monkeypatch, tmp_path
) -> None:
    """§8.2 (8): charged once, the chain moves on, ``error_kind`` names upstream."""

    settings = _settings(
        monkeypatch,
        tmp_path,
        MODEL_IMAGE="mistral/mistral-medium-3-5",
        MODEL_IMAGE_FALLBACKS="mistral/mistral-large-4",
    )
    upstream = Upstream(
        chat=[_chat_answer(), _chat_answer()],
        content=[
            httpx.Response(404, json={"error": {"message": "no such file"}}),
            httpx.Response(200, content=PNG, headers={"content-type": "image/png"}),
        ],
    )
    with _client(settings, upstream) as client:
        response = client.post("/v1/images/generations", json={"prompt": "p"})
    assert response.status_code == 200, response.text
    assert response.json()["data"][0]["b64_json"] == PNG_B64

    (row,) = _rows(tmp_path)
    attempts = _attempts(tmp_path, row["id"])
    assert [attempt["outcome"] for attempt in attempts] == ["failed", "succeeded"]
    failed = attempts[0]
    # The failure names the upstream, not the key.
    assert failed["error_kind"] == "upstream"
    assert failed["error_kind"] != "auth"
    # The message says the picture was produced and could not be delivered.
    assert "could not be downloaded" in (failed["error_message"] or "")


# --------------------------------------------------------- 9: inline bytes


def test_inline_base64_means_one_call(monkeypatch, tmp_path) -> None:
    """§8.2 (9): the bytes are in the answer, so the fetch is skipped."""

    settings = _settings(monkeypatch, tmp_path)
    upstream = Upstream(chat=[_chat_answer(file_id=None, b64=PNG_B64)])
    with _client(settings, upstream) as client:
        response = client.post("/v1/images/generations", json={"prompt": "p"})
    assert response.status_code == 200, response.text
    assert response.json()["data"][0]["b64_json"] == PNG_B64
    assert upstream.calls == [("POST", "/v1/chat/completions")]


# ------------------------------------------------------------ 10: a CDN URL


def test_a_cdn_url_is_fetched_with_no_key(monkeypatch, tmp_path) -> None:
    """§8.2 (10): ``same_host`` is false, so the credential is not sent."""

    settings = _settings(monkeypatch, tmp_path)
    upstream = Upstream(
        chat=[_chat_answer(file_id=None, url="https://cdn.example.test/a.png")]
    )
    with _client(settings, upstream) as client:
        response = client.post("/v1/images/generations", json={"prompt": "p"})
    assert response.status_code == 200, response.text
    assert response.json()["data"][0]["b64_json"] == PNG_B64
    assert upstream.calls == [
        ("POST", "/v1/chat/completions"),
        ("GET", "/a.png"),
    ]
    # The chat call carried the key; the CDN call did not.
    assert upstream.seen[0][2] == f"Bearer {FAKE_KEY}"
    assert upstream.seen[1][2] == ""


def test_the_provider_host_url_is_fetched_with_the_key(monkeypatch, tmp_path) -> None:
    """The mirror of (10): the provider's own host is the host that asked."""

    settings = _settings(monkeypatch, tmp_path)
    upstream = Upstream(
        chat=[
            _chat_answer(
                file_id=None, url="https://api.mistral.ai/v1/files/abc123def456/content"
            )
        ]
    )
    with _client(settings, upstream) as client:
        client.post("/v1/images/generations", json={"prompt": "p"})
    assert upstream.seen[1][2] == f"Bearer {FAKE_KEY}"


# -------------------------------------------------------- an answer with none


def test_a_chat_answer_with_no_picture_fails_and_falls_through(
    monkeypatch, tmp_path
) -> None:
    settings = _settings(
        monkeypatch,
        tmp_path,
        MODEL_IMAGE="mistral/mistral-medium-3-5",
        MODEL_IMAGE_FALLBACKS="mistral/mistral-large-4",
    )
    upstream = Upstream(
        chat=[
            _chat_answer(file_id=None, text="I cannot do that."),
            _chat_answer(),
        ]
    )
    with _client(settings, upstream) as client:
        response = client.post("/v1/images/generations", json={"prompt": "p"})
    assert response.status_code == 200, response.text
    (row,) = _rows(tmp_path)
    attempts = _attempts(tmp_path, row["id"])
    assert [attempt["outcome"] for attempt in attempts] == ["failed", "succeeded"]
    assert attempts[0]["error_kind"] == "upstream"


# --------------------------------------------------- the shape stays private


def test_a_streaming_client_is_answered_whole(monkeypatch, tmp_path) -> None:
    """§4.5: the surface declares no stream, so the whole-answer fold serves it."""

    settings = _settings(monkeypatch, tmp_path)
    upstream = Upstream()
    with _client(settings, upstream) as client:
        response = client.post(
            "/v1/images/generations", json={"prompt": "p", "stream": True}
        )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "image_generation.completed" in response.text
    # The host was never asked to stream.
    _method, _path, _auth, body = upstream.seen[0]
    assert body["stream"] is False
    (row,) = _read_rows(tmp_path)
    assert "stream" in row["params"]["media"]["not_forwarded"]


def test_the_answer_is_an_openai_images_answer_and_carries_no_prose(
    monkeypatch, tmp_path
) -> None:
    """§4.8: the client's JSON gains no field its own SDK does not define."""

    settings = _settings(monkeypatch, tmp_path)
    upstream = Upstream()
    with _client(settings, upstream) as client:
        response = client.post("/v1/images/generations", json={"prompt": "p"})
    payload = response.json()
    assert set(payload) == {"created", "data"}
    assert set(payload["data"][0]) == {"b64_json"}
    assert payload == json.loads(images_answer(created=1_700_000_000, data=PNG))


# ------------------------------------------- §8.4: the retention path fires


def _picture(seed: int, size: int) -> bytes:
    return PNG + bytes([seed]) * size


def _serve_pictures(settings: Settings, pictures: list[bytes]) -> None:
    """One client request per picture, each answered by its own file fetch."""

    for picture in pictures:
        upstream = Upstream(
            content=[
                httpx.Response(
                    200, content=picture, headers={"content-type": "image/png"}
                )
            ]
        )
        with _client(settings, upstream) as client:
            response = client.post("/v1/images/generations", json={"prompt": "p"})
        assert response.status_code == 200, response.text


def test_a_small_cap_drops_the_oldest_file_and_keeps_every_row(
    monkeypatch, tmp_path
) -> None:
    """§8.4 (1): the oldest files go, the newest stay, every row keeps its metadata.

    ``_trim_media`` and ``_store_media`` are NOT edited by this PR, so the
    proof is that they still fire for a Mistral image's rows.
    """

    cap = 1000
    settings = _settings(
        monkeypatch, tmp_path, MEDIA_STORE_ENABLED="true", MEDIA_STORE_MAX_MB="0"
    )
    pictures = [_picture(seed, 400) for seed in (7, 8, 9)]
    _serve_pictures(settings, pictures)

    rows = _rows(tmp_path)
    assert len(rows) == 3
    shas = [row["media_sha_out"] for row in rows]
    assert len(set(shas)) == 3

    # Now apply the cap the way the writer thread does, and read back.
    from my_claude_code.core.request_log import RequestLogStore

    store = RequestLogStore(tmp_path / "requests.db")
    try:
        assert store.trim_media_store(cap) >= 1
    finally:
        store.close()

    root = media_root(tmp_path / "requests.db")
    present = [media_file_path(root, sha, "image/png").is_file() for sha in shas]
    # The oldest went; the newest stayed.
    assert present[0] is False
    assert present[-1] is True
    conn = sqlite3.connect(tmp_path / "requests.db")
    conn.row_factory = sqlite3.Row
    try:
        stored = {
            row["sha256"]: row["stored"]
            for row in conn.execute("SELECT sha256, stored FROM media_blobs")
        }
        metadata = {
            row["sha256"]: (row["mime"], row["bytes"])
            for row in conn.execute("SELECT sha256, mime, bytes FROM media_blobs")
        }
    finally:
        conn.close()
    assert stored[shas[0]] == 0
    assert metadata[shas[0]] == ("image/png", len(pictures[0]))
    # Every request row survives with its own measurement.
    for row in _rows(tmp_path):
        assert row["media_sha_out"] is not None
        assert row["media_bytes_out"] is not None


def test_clearing_the_log_takes_the_stored_pictures_with_it(
    monkeypatch, tmp_path
) -> None:
    """§8.4 (2): the stored files go with the rows."""

    settings = _settings(monkeypatch, tmp_path, MEDIA_STORE_ENABLED="true")
    _serve_pictures(settings, [_picture(1, 200), _picture(2, 200)])
    # Draining the stores is what puts every row on disk (``_rows`` does it).
    assert len(_rows(tmp_path)) == 2
    root = media_root(tmp_path / "requests.db")
    assert len(list(root.rglob("*.png"))) == 2

    from my_claude_code.core.request_log import RequestLogStore

    store = RequestLogStore(tmp_path / "requests.db")
    try:
        assert store.clear() >= 2
    finally:
        store.close()
    assert list(root.rglob("*.png")) == []


def test_pruning_a_row_removes_its_file(monkeypatch, tmp_path) -> None:
    """§8.4 (3): a pruned row's file is removed when nothing else names it."""

    settings = _settings(monkeypatch, tmp_path, MEDIA_STORE_ENABLED="true")
    _serve_pictures(settings, [_picture(3, 200), _picture(4, 200)])
    first, _newest = _rows(tmp_path)
    sha = first["media_sha_out"]
    root = media_root(tmp_path / "requests.db")
    path = media_file_path(root, sha, "image/png")
    assert path.is_file()

    from my_claude_code.core.request_log import RequestLogStore

    # A cap of one row: the prune deletes everything beyond the newest.
    store = RequestLogStore(tmp_path / "requests.db", max_rows=1)
    try:
        assert store.prune() >= 1
    finally:
        store.close()
    assert not path.exists()


@pytest.mark.parametrize("model", ["mistral_vibe/mistral-medium-3-5"])
def test_a_vibe_ref_on_the_image_rail_is_skipped_uncharged(
    monkeypatch, tmp_path, model: str
) -> None:
    """Decision 2: ``mistral_vibe`` declares no image surface, by absence."""

    settings = _settings(
        monkeypatch,
        tmp_path,
        MISTRAL_VIBE_KEY="VibeFakeKey" + "0123456789abcdef" * 2,
        MODEL_IMAGE=model,
    )
    upstream = Upstream()
    with _client(settings, upstream) as client:
        response = client.post("/v1/images/generations", json={"prompt": "p"})
    # Nothing served it and nothing was sent.
    assert response.status_code >= 400
    assert upstream.seen == []
