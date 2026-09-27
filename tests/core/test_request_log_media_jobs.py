"""``media_jobs`` (7.64.0): video jobs, their two request columns, prune and clear.

A job row is written the moment a host accepts the job, by the request that
served it -- not by the batched writer -- so its request row may land later.
Retention therefore drops a job with its request row only after a grace, and
"Clear log" drops every job.
"""

import contextlib
import sqlite3
import time
from pathlib import Path

import pytest

from my_claude_code.core.media_store import MediaOutputRecord
from my_claude_code.core.request_log import (
    MediaJobRecord,
    RequestLogStore,
    RequestRecord,
)

NEW_REQUEST_COLUMNS = ("media_job_id", "output_video_seconds")


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _connect(path: Path) -> contextlib.closing[sqlite3.Connection]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return contextlib.closing(conn)


def _insert_request(path: Path, request_id: str) -> None:
    with _connect(path) as conn, conn:
        conn.execute(
            "INSERT INTO requests (id, ts_epoch, ts_iso, endpoint, protocol, status,"
            " stream) VALUES (?, ?, 'then', '/v1/videos', 'openai_images',"
            " 'success', 0)",
            (request_id, time.time()),
        )


def _job(job_id: str, request_id: str, created_at: float) -> MediaJobRecord:
    return MediaJobRecord(
        job_id=job_id,
        request_id=request_id,
        provider="open_router",
        model="google/veo-3.1",
        upstream_id=f"up-{job_id}",
        created_at=created_at,
        key_index=0,
        key_fingerprint="sha256:abc",
        status="queued",
        status_raw="pending",
    )


def test_old_database_gains_the_columns_and_the_table(tmp_path: Path) -> None:
    path = tmp_path / "requests.db"
    RequestLogStore(path).close()
    with _connect(path) as conn, conn:
        for column in NEW_REQUEST_COLUMNS:
            conn.execute(f"ALTER TABLE requests DROP COLUMN {column}")
        conn.execute("DROP TABLE media_jobs")
    store = RequestLogStore(path)
    try:
        with _connect(path) as conn:
            assert set(NEW_REQUEST_COLUMNS) <= _columns(conn, "requests")
            assert {"job_id", "upstream_id", "row_seconds_written"} <= _columns(
                conn, "media_jobs"
            )
    finally:
        store.close()


def test_request_row_carries_the_job(tmp_path: Path) -> None:
    path = tmp_path / "requests.db"
    store = RequestLogStore(path)
    store.enqueue(
        RequestRecord(
            id="req-v",
            endpoint="/v1/videos",
            protocol="openai_images",
            status="success",
            media_operation="video_create",
            media_job_id="video_1",
        )
    )
    store.close()
    with _connect(path) as conn:
        row = conn.execute("SELECT * FROM requests WHERE id = 'req-v'").fetchone()
    assert row["media_job_id"] == "video_1"
    # Not measured until a poll reads the job completed with a length.
    assert row["output_video_seconds"] is None


def test_prune_drops_a_job_with_its_request_row_after_the_grace(
    tmp_path: Path,
) -> None:
    path = tmp_path / "requests.db"
    store = RequestLogStore(path)
    try:
        now = time.time()
        _insert_request(path, "kept")
        store.insert_media_job(_job("video_kept", "kept", now - 7200))
        store.insert_media_job(_job("video_orphan", "gone", now - 7200))
        # Accepted a moment ago; its request row is not flushed yet.
        store.insert_media_job(_job("video_young", "unwritten", now))
        store.prune()
        assert sorted(job["job_id"] for job in store.list_media_jobs()) == [
            "video_kept",
            "video_young",
        ]
    finally:
        store.close()


def test_clear_forgets_every_job(tmp_path: Path) -> None:
    store = RequestLogStore(tmp_path / "requests.db")
    try:
        store.insert_media_job(_job("video_a", "a", time.time()))
        store.clear()
        assert store.list_media_jobs() == []
        assert store.media_job("video_a") is None
    finally:
        store.close()


def test_video_seconds_wait_for_the_create_row(tmp_path: Path) -> None:
    path = tmp_path / "requests.db"
    store = RequestLogStore(path)
    try:
        assert store.set_request_video_seconds("req-late", 8.0) is False
        _insert_request(path, "req-late")
        assert store.set_request_video_seconds("req-late", 8.0) is True
        with _connect(path) as conn:
            row = conn.execute(
                "SELECT output_video_seconds FROM requests WHERE id = 'req-late'"
            ).fetchone()
        assert row["output_video_seconds"] == 8.0
    finally:
        store.close()


def test_update_changes_only_what_a_poll_may_change(tmp_path: Path) -> None:
    store = RequestLogStore(tmp_path / "requests.db")
    try:
        store.insert_media_job(_job("video_u", "u", time.time()))
        assert store.update_media_job("video_u", status="completed", progress=100)
        assert store.update_media_job("video_missing", status="failed") is False
        with pytest.raises(ValueError, match="upstream_id"):
            store.update_media_job("video_u", upstream_id="elsewhere")
        job = store.media_job("video_u")
        assert job is not None
        assert (job["status"], job["progress"], job["upstream_id"]) == (
            "completed",
            100,
            "up-video_u",
        )
    finally:
        store.close()


def test_list_pages_by_job_id(tmp_path: Path) -> None:
    store = RequestLogStore(tmp_path / "requests.db")
    try:
        for offset, job_id in enumerate(("video_1", "video_2", "video_3")):
            store.insert_media_job(_job(job_id, job_id, 1000.0 + offset))

        def ids(**kwargs) -> list[str]:
            return [job["job_id"] for job in store.list_media_jobs(**kwargs)]

        assert ids() == ["video_3", "video_2", "video_1"]
        assert ids(limit=2) == ["video_3", "video_2"]
        assert ids(after="video_2") == ["video_1"]
        assert ids(order="asc", after="video_1") == ["video_2", "video_3"]
        assert store.delete_media_job("video_2") is True
        assert store.delete_media_job("video_2") is False
        assert ids() == ["video_3", "video_1"]
    finally:
        store.close()


def test_downloaded_content_is_linked_to_the_create_row(tmp_path: Path) -> None:
    path = tmp_path / "requests.db"
    store = RequestLogStore(path)
    try:
        store.insert_media_job(_job("video_c", "req-c", time.time()))
        store.record_media_job_content(
            "video_c",
            "req-c",
            MediaOutputRecord(sha256="f" * 64, mime="video/mp4", bytes=42, stored=True),
            at=time.time(),
        )
        job = store.media_job("video_c")
        assert job is not None
        assert (job["content_sha"], job["content_bytes"], job["content_mime"]) == (
            "f" * 64,
            42,
            "video/mp4",
        )
        with _connect(path) as conn:
            link = conn.execute(
                "SELECT direction, idx, sha256 FROM request_media"
                " WHERE request_id = 'req-c'"
            ).fetchone()
            blob = conn.execute(
                "SELECT mime, bytes, stored FROM media_blobs WHERE sha256 = ?",
                ("f" * 64,),
            ).fetchone()
        assert tuple(link) == ("out", 0, "f" * 64)
        assert tuple(blob) == ("video/mp4", 42, 1)
    finally:
        store.close()
