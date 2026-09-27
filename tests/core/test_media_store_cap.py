"""``MEDIA_STORE_MAX_MB`` (7.68.0): the media store trimmed to a cap, oldest file first.

Only files go. Every request row, its ``request_media`` links and the
``media_blobs`` metadata stay; ``stored`` drops to 0 for a file that went, so
the request detail keeps saying what was produced and stops previewing it.
0 is no cap, and a 0 cap never even adds the stored sizes up.
"""

import contextlib
import hashlib
import sqlite3
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient

from my_claude_code.config.settings import Settings
from my_claude_code.core import request_log
from my_claude_code.core.media_store import (
    MediaOutputRecord,
    media_file_path,
    media_root,
    sha256_hex,
    write_media_file,
)
from my_claude_code.core.request_log import RequestLogStore, RequestRecord
from my_claude_code.providers.media.registry import MediaRegistry
from tests.api.support import create_test_app

PNG = b"\x89PNG\r\n\x1a\n"
MP4_HEAD = b"\x00\x00\x00\x18ftypmp42"
MB = 1024 * 1024
OPENROUTER = "openrouter.ai"
OR_VIDEOS = "/api/v1/videos"
OR_KEY = "sk-or-v1-" + "a" * 48


def _picture(seed: int, size: int) -> bytes:
    return PNG + bytes([seed]) * size


def _connect(path: Path) -> contextlib.closing[sqlite3.Connection]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return contextlib.closing(conn)


def _stored(
    store: RequestLogStore, request_id: str, data: bytes, *, ts: float, cap: int
) -> str:
    """Write ``data`` into the store's media folder and queue the row that kept it."""
    sha = sha256_hex(data)
    assert write_media_file(media_root(store.db_path), sha, "image/png", data)
    store.enqueue(
        RequestRecord(
            id=request_id,
            endpoint="/v1/images/generations",
            protocol="openai_images",
            status="success",
            ts_epoch=ts,
            media_operation="image_generate",
            output_image_count=1,
            media_outputs=(
                MediaOutputRecord(
                    sha256=sha, mime="image/png", bytes=len(data), stored=True
                ),
            ),
            media_store_max_bytes=cap,
        )
    )
    return sha


def _file(store_path: Path, sha: str, mime: str = "image/png") -> Path:
    return media_file_path(media_root(store_path), sha, mime)


def _blobs(path: Path) -> dict[str, dict[str, Any]]:
    with _connect(path) as conn:
        return {
            str(row["sha256"]): dict(row)
            for row in conn.execute("SELECT * FROM media_blobs")
        }


def _links(path: Path) -> list[tuple[str, str, int, str]]:
    with _connect(path) as conn:
        return [
            tuple(row)
            for row in conn.execute(
                "SELECT request_id, direction, idx, sha256 FROM request_media"
                " ORDER BY request_id"
            )
        ]


def _ordered_by_sha_descending(*pictures: bytes) -> list[bytes]:
    """Oldest first *and* largest address first: the order must come from time."""
    return sorted(pictures, key=sha256_hex, reverse=True)


def test_max_mb_zero_never_drops(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "requests.db"
    scans: list[int] = []
    real = RequestLogStore._trim_media

    def spy(self: RequestLogStore, conn: sqlite3.Connection, max_bytes: int) -> int:
        scans.append(max_bytes)
        return real(self, conn, max_bytes)

    monkeypatch.setattr(RequestLogStore, "_trim_media", spy)
    store = RequestLogStore(path)
    pictures = [_picture(seed, 4000) for seed in (1, 2, 3)]
    shas = [
        _stored(store, f"req-{index}", data, ts=1000.0 + index, cap=0)
        for index, data in enumerate(pictures)
    ]
    store.close()

    assert scans == [], "a 0 cap must never add the stored sizes up"
    for sha in shas:
        assert _file(path, sha).is_file()
    assert {sha: blob["stored"] for sha, blob in _blobs(path).items()} == dict.fromkeys(
        shas, 1
    )

    # The direct call too: 0 (or less) returns before opening a connection.
    reopened = RequestLogStore(path)
    try:

        def refuse() -> sqlite3.Connection:
            raise AssertionError("a 0 cap opened the database")

        monkeypatch.setattr(reopened, "_connect", refuse)
        assert reopened.trim_media_store(0) == 0
        assert reopened.trim_media_store(-5) == 0
    finally:
        monkeypatch.undo()
        reopened.close()


def test_max_mb_drops_oldest_keeps_metadata(tmp_path: Path) -> None:
    path = tmp_path / "requests.db"
    oldest, middle, newest = _ordered_by_sha_descending(
        _picture(7, 400), _picture(8, 400), _picture(9, 400)
    )
    cap = 1000  # bytes: two of the three fit
    store = RequestLogStore(path)
    first = _stored(store, "req-a", oldest, ts=1000.0, cap=cap)
    second = _stored(store, "req-b", middle, ts=2000.0, cap=cap)
    third = _stored(store, "req-c", newest, ts=3000.0, cap=cap)
    store.close()

    assert not _file(path, first).exists(), "the oldest file goes first"
    assert _file(path, second).is_file()
    assert _file(path, third).is_file()
    blobs = _blobs(path)
    assert (
        blobs[first]["stored"],
        blobs[second]["stored"],
        blobs[third]["stored"],
    ) == (
        0,
        1,
        1,
    )
    # The metadata stays: type, size, address.
    assert (blobs[first]["mime"], blobs[first]["bytes"]) == ("image/png", len(oldest))
    assert _links(path) == [
        ("req-a", "out", 0, first),
        ("req-b", "out", 0, second),
        ("req-c", "out", 0, third),
    ]
    reader = RequestLogStore(path)
    try:
        row = reader.get_request("req-a")
        assert row is not None
        assert row["output_image_count"] == 1
        assert row["media"] == [
            {
                "direction": "out",
                "idx": 0,
                "sha256": first,
                "mime": "image/png",
                "bytes": len(oldest),
                "stored": False,
            }
        ]
        assert reader.stored_media_file(first) is None
        found = reader.stored_media_file(third)
        assert found is not None
        assert found[0].read_bytes() == newest
        assert found[1] == "image/png"
    finally:
        reader.close()


def test_a_file_stored_again_is_the_newest(tmp_path: Path) -> None:
    """Re-stored after the cap dropped it, a file is new again, not the oldest."""
    path = tmp_path / "requests.db"
    oldest, middle, newest = _ordered_by_sha_descending(
        _picture(4, 400), _picture(5, 400), _picture(6, 400)
    )
    cap = 1000
    store = RequestLogStore(path)
    first = _stored(store, "req-a", oldest, ts=1000.0, cap=cap)
    second = _stored(store, "req-b", middle, ts=2000.0, cap=cap)
    _stored(store, "req-c", newest, ts=3000.0, cap=cap)
    store.close()
    assert not _file(path, first).exists()

    store = RequestLogStore(path)
    assert _stored(store, "req-d", oldest, ts=4000.0, cap=cap) == first
    store.close()

    assert _file(path, first).is_file(), "the file just stored again stays"
    assert not _file(path, second).exists(), "the now-oldest file went instead"
    blobs = _blobs(path)
    assert blobs[first]["stored"] == 1
    assert blobs[first]["created_at"] == 4000.0
    assert blobs[second]["stored"] == 0


def test_a_file_that_cannot_be_deleted_stays_counted(
    tmp_path: Path, monkeypatch
) -> None:
    """Held open elsewhere: still on disk, so still stored, and the next one goes."""
    path = tmp_path / "requests.db"
    oldest, middle, newest = _ordered_by_sha_descending(
        _picture(1, 400), _picture(2, 400), _picture(3, 400)
    )
    stuck = sha256_hex(oldest)
    real = request_log.remove_media_file

    def remove(root: Path, sha256: str) -> bool:
        return False if sha256 == stuck else real(root, sha256)

    monkeypatch.setattr(request_log, "remove_media_file", remove)
    store = RequestLogStore(path)
    _stored(store, "req-a", oldest, ts=1000.0, cap=1000)
    second = _stored(store, "req-b", middle, ts=2000.0, cap=1000)
    third = _stored(store, "req-c", newest, ts=3000.0, cap=1000)
    store.close()

    blobs = _blobs(path)
    assert blobs[stuck]["stored"] == 1
    assert _file(path, stuck).is_file()
    assert blobs[second]["stored"] == 0
    assert not _file(path, second).exists()
    assert blobs[third]["stored"] == 1


# ------------------------------------------------------------ the video tee


def _video_settings(monkeypatch, tmp_path: Path, **values: str) -> Settings:
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    base = {
        "OPENROUTER_API_KEY": OR_KEY,
        "PROVIDER_RETRY_ATTEMPTS": "1",
        "MODEL_VIDEO": "open_router/google/veo-3.1",
        "MEDIA_STORE_ENABLED": "true",
    }
    base.update(values)
    return Settings.model_validate(base)


def test_cap_applies_to_video_tee(monkeypatch, tmp_path: Path) -> None:
    """A video kept while it streams to the client is counted, and trims the store."""
    settings = _video_settings(monkeypatch, tmp_path, MEDIA_STORE_MAX_MB="1")
    store = request_log.store_from_settings(settings)
    assert store is not None
    earlier = _picture(11, 700 * 1024)
    earlier_sha = _stored(store, "req-earlier", earlier, ts=1000.0, cap=0)
    # Closing drains the writer: the earlier file's row is on disk.
    request_log.reset_request_log_stores()

    video = MP4_HEAD + bytes(range(256)) * (700 * 4)
    video_sha = hashlib.sha256(video).hexdigest()
    answers = {
        ("POST", OR_VIDEOS): httpx.Response(
            202, json={"id": "or-job-cap", "status": "pending"}
        ),
        ("GET", f"{OR_VIDEOS}/or-job-cap"): httpx.Response(
            200, json={"id": "or-job-cap", "status": "completed", "duration": 4}
        ),
        ("GET", f"{OR_VIDEOS}/or-job-cap/content"): httpx.Response(
            200, content=video, headers={"content-type": "video/mp4"}
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        request.read()
        assert request.url.host == OPENROUTER
        return answers[(request.method, request.url.path)]

    registry = MediaRegistry(transport=httpx.MockTransport(handler))
    with TestClient(create_test_app(settings, media=registry)) as client:
        created = client.post("/v1/videos", json={"prompt": "a kite"})
        assert created.status_code == 200, created.text
        video_id = created.json()["id"]
        assert client.get(f"/v1/videos/{video_id}").status_code == 200
        downloaded = client.get(f"/v1/videos/{video_id}/content")
        assert downloaded.status_code == 200, downloaded.text
        assert downloaded.content == video
    request_log.reset_request_log_stores()

    db = tmp_path / "requests.db"
    blobs = _blobs(db)
    # 700 KB + ~700 KB passes 1 MB: the earlier file went, the video stayed.
    assert blobs[earlier_sha]["stored"] == 0
    assert not _file(db, earlier_sha).exists()
    assert blobs[video_sha]["stored"] == 1
    assert _file(db, video_sha, "video/mp4").read_bytes() == video
    assert ("req-earlier", "out", 0, earlier_sha) in _links(db)
