"""``GET /admin/api/media/{sha}`` and the request detail's ``media`` rows (7.68.0).

The request detail lists every media row a request has -- direction, type,
size, address -- and whether the media store holds the file. Only a stored
file is served, only to the local machine, and only as a picture, a sound or
a film: anything else goes out as opaque bytes.
"""

import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from my_claude_code.config.settings import Settings
from my_claude_code.core import request_log
from my_claude_code.core.media_store import (
    MediaOutputRecord,
    media_file_path,
    media_root,
    write_media_file,
)
from my_claude_code.core.request_log import RequestRecord
from tests.api.support import create_test_app

PNG = b"\x89PNG\r\n\x1a\n" + b"\x07" * 64
MP3 = b"ID3\x04\x00" + b"\x09" * 64
UPLOAD = b"\x89PNG\r\n\x1a\n" + b"\x08" * 64
LOCAL = ("127.0.0.1", 50000)
REMOTE = ("203.0.113.9", 50000)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _settings(monkeypatch, tmp_path: Path) -> Settings:
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    return Settings.model_validate({"MEDIA_STORE_ENABLED": "true"})


def _record(
    settings: Settings,
    request_id: str,
    outputs: tuple[tuple[bytes, str | None, bool], ...],
    *,
    inputs: tuple[tuple[bytes, str], ...] = (),
) -> None:
    """One media row: ``outputs`` written to the store when marked stored."""
    store = request_log.store_from_settings(settings)
    assert store is not None
    root = media_root(store.db_path)
    records = [
        MediaOutputRecord(
            sha256=_sha(data),
            mime=mime,
            bytes=len(data),
            stored=False,
            idx=position,
            direction="in",
        )
        for position, (data, mime) in enumerate(inputs)
    ]
    for position, (data, mime, stored) in enumerate(outputs):
        if stored:
            assert write_media_file(root, _sha(data), mime, data)
        records.append(
            MediaOutputRecord(
                sha256=_sha(data),
                mime=mime,
                bytes=len(data),
                stored=stored,
                idx=position,
            )
        )
    store.enqueue(
        RequestRecord(
            id=request_id,
            endpoint="/v1/images/edits",
            protocol="openai_images",
            status="success",
            media_operation="image_edit",
            media_outputs=tuple(records),
        )
    )
    # Closing drains the writer: the row and its links are on disk.
    request_log.reset_request_log_stores()


def _client(settings: Settings, address: tuple[str, int] = LOCAL) -> TestClient:
    return TestClient(create_test_app(settings), client=address)


def test_request_detail_lists_media_rows_with_their_stored_flag(
    monkeypatch, tmp_path
) -> None:
    settings = _settings(monkeypatch, tmp_path)
    _record(
        settings,
        "req-edit",
        ((PNG, "image/png", True), (MP3, "audio/mpeg", False)),
        inputs=((UPLOAD, "image/png"),),
    )
    with _client(settings) as client:
        row = client.get("/admin/api/requests/req-edit").json()
    assert row["media"] == [
        {
            "direction": "in",
            "idx": 0,
            "sha256": _sha(UPLOAD),
            "mime": "image/png",
            "bytes": len(UPLOAD),
            "stored": False,
        },
        {
            "direction": "out",
            "idx": 0,
            "sha256": _sha(PNG),
            "mime": "image/png",
            "bytes": len(PNG),
            "stored": True,
        },
        {
            "direction": "out",
            "idx": 1,
            "sha256": _sha(MP3),
            "mime": "audio/mpeg",
            "bytes": len(MP3),
            "stored": False,
        },
    ]
    # Never the bytes themselves: previews are fetched by address.
    assert "base64" not in str(row["media"])


def test_request_detail_of_a_chat_row_has_no_media(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path)
    store = request_log.store_from_settings(settings)
    assert store is not None
    store.enqueue(
        RequestRecord(
            id="req-chat",
            endpoint="/v1/messages",
            protocol="anthropic_messages",
            status="success",
        )
    )
    request_log.reset_request_log_stores()
    with _client(settings) as client:
        assert client.get("/admin/api/requests/req-chat").json()["media"] == []


def test_serves_stored_file_with_mime(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path)
    _record(settings, "req-a", ((PNG, "image/png", True), (MP3, "audio/mpeg", True)))
    with _client(settings) as client:
        picture = client.get(f"/admin/api/media/{_sha(PNG)}")
        sound = client.get(f"/admin/api/media/{_sha(MP3).upper()}")
    assert picture.status_code == 200
    assert picture.content == PNG
    assert picture.headers["content-type"] == "image/png"
    assert picture.headers["x-content-type-options"] == "nosniff"
    assert picture.headers["content-security-policy"] == "sandbox"
    assert sound.status_code == 200
    assert sound.content == MP3
    assert sound.headers["content-type"] == "audio/mpeg"


@pytest.mark.parametrize(
    "mime", ["text/html", "image/svg+xml", "application/javascript", None]
)
def test_a_type_that_could_run_script_is_served_as_bytes(
    monkeypatch, tmp_path, mime
) -> None:
    """The type came from a host; only pictures, sounds and films keep theirs."""
    settings = _settings(monkeypatch, tmp_path)
    data = b"<svg onload=alert(1)><script>alert(1)</script></svg>"
    _record(settings, "req-x", ((data, mime, True),))
    with _client(settings) as client:
        response = client.get(f"/admin/api/media/{_sha(data)}")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == "sandbox"


def test_admin_media_route_loopback_only(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path)
    _record(settings, "req-a", ((PNG, "image/png", True),))
    with _client(settings, REMOTE) as client:
        remote = client.get(f"/admin/api/media/{_sha(PNG)}")
    assert remote.status_code == 403
    assert remote.content != PNG
    with _client(settings) as client:
        foreign = client.get(
            f"/admin/api/media/{_sha(PNG)}",
            headers={"origin": "https://evil.example"},
        )
        local = client.get(f"/admin/api/media/{_sha(PNG)}")
    assert foreign.status_code == 403
    assert local.status_code == 200


@pytest.mark.parametrize(
    "sha",
    [
        "abc",
        "a" * 63,
        "a" * 65,
        "g" * 64,
        "a" * 32 + "." + "a" * 31,
    ],
    ids=["short", "63", "65", "not-hex", "dotted"],
)
def test_bad_sha_400(monkeypatch, tmp_path, sha) -> None:
    settings = _settings(monkeypatch, tmp_path)
    with _client(settings) as client:
        assert client.get(f"/admin/api/media/{sha}").status_code == 400


def test_not_stored_404(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path)
    missing = b"\x89PNG\r\n\x1a\n" + b"\x0a" * 64
    _record(
        settings,
        "req-a",
        ((PNG, "image/png", False), (missing, "image/png", True)),
    )
    # Recorded as stored, but the file has gone from disk since.
    store = request_log.store_from_settings(settings)
    assert store is not None
    media_file_path(media_root(store.db_path), _sha(missing), "image/png").unlink()
    request_log.reset_request_log_stores()
    with _client(settings) as client:
        never_seen = client.get(f"/admin/api/media/{'e' * 64}")
        metadata_only = client.get(f"/admin/api/media/{_sha(PNG)}")
        vanished = client.get(f"/admin/api/media/{_sha(missing)}")
    assert never_seen.status_code == 404
    assert metadata_only.status_code == 404
    assert vanished.status_code == 404


def test_the_models_path_is_not_read_as_an_address(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path)
    with _client(settings) as client:
        assert client.get("/admin/api/media/models").status_code == 200
