"""Storing the images a request carried, and getting rid of them again."""

import base64
import io
from pathlib import Path

from PIL import Image

from my_claude_code.core.request_images import CapturedImage
from my_claude_code.core.request_log import RequestLogStore, RequestRecord


def _thumbnail() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (32, 24), (5, 5, 5)).save(buffer, format="WEBP")
    return buffer.getvalue()


def _image(sha: str = "abc123") -> CapturedImage:
    return CapturedImage(
        sha256=sha,
        kind="image",
        media_type="image/png",
        source_bytes=2_048_000,
        width=1600,
        height=1200,
        thumbnail=_thumbnail(),
        thumbnail_media_type="image/webp",
    )


def _record(request_id: str, images: tuple[CapturedImage, ...]) -> RequestRecord:
    return RequestRecord(
        id=request_id,
        endpoint="/v1/messages",
        protocol="anthropic",
        requested_model="claude-sonnet-5",
        provider="nous_portal",
        resolved_model="tencent/hy3:free",
        input_image_count=len(images) or None,
        images=images,
    )


def test_an_image_round_trips_into_the_detail_payload(tmp_path: Path):
    path = tmp_path / "requests.db"
    store = RequestLogStore(path)
    store.enqueue(_record("r1", (_image(),)))
    store.close()

    reader = RequestLogStore(path)
    try:
        row = reader.get_request("r1")
        assert row is not None
        assert row["input_image_count"] == 1
        image = row["input_images"][0]
        assert image["media_type"] == "image/png"
        assert image["width"] == 1600
        assert image["source_bytes"] == 2_048_000
        assert base64.b64decode(image["thumbnail_base64"]) == _thumbnail()
    finally:
        reader.close()


def test_the_same_image_on_many_requests_is_stored_once(tmp_path: Path):
    path = tmp_path / "requests.db"
    store = RequestLogStore(path)
    for index in range(5):
        store.enqueue(_record(f"r{index}", (_image(),)))
    store.close()

    reader = RequestLogStore(path)
    try:
        # Read the tables directly: dedup and orphan cleanup are storage-level
        # facts with no public accessor.
        with reader._connection() as conn:
            blobs = conn.execute("SELECT COUNT(*) FROM image_blobs").fetchone()[0]
            links = conn.execute("SELECT COUNT(*) FROM request_images").fetchone()[0]
        assert (blobs, links) == (1, 5)
    finally:
        reader.close()


def test_images_appear_in_order(tmp_path: Path):
    path = tmp_path / "requests.db"
    store = RequestLogStore(path)
    store.enqueue(_record("r1", (_image("aaa"), _image("bbb"), _image("ccc"))))
    store.close()

    reader = RequestLogStore(path)
    try:
        row = reader.get_request("r1")
        assert row is not None
        assert [image["sha256"] for image in row["input_images"]] == [
            "aaa",
            "bbb",
            "ccc",
        ]
    finally:
        reader.close()


def test_a_request_without_images_reports_an_empty_list(tmp_path: Path):
    path = tmp_path / "requests.db"
    store = RequestLogStore(path)
    store.enqueue(_record("r1", ()))
    store.close()

    reader = RequestLogStore(path)
    try:
        row = reader.get_request("r1")
        assert row is not None
        assert row["input_images"] == []
        assert row["input_image_count"] is None
    finally:
        reader.close()


def test_retention_takes_the_pictures_with_the_rows(tmp_path: Path):
    path = tmp_path / "requests.db"
    store = RequestLogStore(path, max_rows=1)
    store.enqueue(_record("old", (_image("old-image"),)))
    store.enqueue(_record("new", (_image("new-image"),)))
    store.close()

    reader = RequestLogStore(path, max_rows=1)
    try:
        reader.prune()
        # Read the tables directly: dedup and orphan cleanup are storage-level
        # facts with no public accessor.
        with reader._connection() as conn:
            shas = {
                str(row[0])
                for row in conn.execute("SELECT sha FROM image_blobs").fetchall()
            }
        # The surviving row keeps its picture; the pruned one's is gone rather
        # than orphaned, which is what would otherwise grow the file forever.
        assert shas == {"new-image"}
    finally:
        reader.close()


def test_a_shared_image_survives_until_its_last_request_is_pruned(tmp_path: Path):
    path = tmp_path / "requests.db"
    store = RequestLogStore(path, max_rows=1)
    store.enqueue(_record("old", (_image("shared"),)))
    store.enqueue(_record("new", (_image("shared"),)))
    store.close()

    reader = RequestLogStore(path, max_rows=1)
    try:
        reader.prune()
        row = reader.get_request("new")
        assert row is not None
        assert row["input_images"][0]["sha256"] == "shared"
    finally:
        reader.close()


def test_clearing_the_log_removes_the_images(tmp_path: Path):
    path = tmp_path / "requests.db"
    store = RequestLogStore(path)
    store.enqueue(_record("r1", (_image(),)))
    store.close()

    reader = RequestLogStore(path)
    try:
        reader.clear()
        # Read the tables directly: dedup and orphan cleanup are storage-level
        # facts with no public accessor.
        with reader._connection() as conn:
            blobs = conn.execute("SELECT COUNT(*) FROM image_blobs").fetchone()[0]
            links = conn.execute("SELECT COUNT(*) FROM request_images").fetchone()[0]
        assert (blobs, links) == (0, 0)
    finally:
        reader.close()


def test_stats_counts_requests_that_carried_an_image(tmp_path: Path):
    path = tmp_path / "requests.db"
    store = RequestLogStore(path)
    store.enqueue(_record("r1", (_image(),)))
    store.enqueue(_record("r2", ()))
    store.close()

    reader = RequestLogStore(path)
    try:
        assert reader.stats()["with_images"] == 1
    finally:
        reader.close()


def _diverted(request_id: str, *, diverted_from: str | None, diversion: str | None):
    record = _record(request_id, (_image(request_id),))
    record.route_chain = "chatgpt_oauth/gpt-5.6-luna,nous_portal/step-3.7:free"
    record.route_attempt = 0
    record.route_diverted_from = diverted_from
    record.route_diversion = diversion
    return record


def test_an_image_with_no_vision_route_is_counted_apart_from_a_diversion(
    tmp_path: Path,
):
    """The safety net working and the safety net having nowhere to put the
    request are different facts, and the counters must not merge them."""
    path = tmp_path / "requests.db"
    store = RequestLogStore(path)
    store.enqueue(
        _diverted(
            "real", diverted_from="nous_portal/tencent/hy3:free", diversion="vision"
        )
    )
    store.enqueue(
        _diverted("blind", diverted_from=None, diversion="vision_unavailable")
    )
    store.close()

    reader = RequestLogStore(path)
    try:
        stats = reader.stats()
        assert stats["diverted"] == 1
        assert stats["vision_unavailable"] == 1
        assert stats["with_images"] == 2
        # The lifetime counter must agree with the window one.
        assert reader.lifetime()["diverted"] == 1
    finally:
        reader.close()


def test_a_description_is_stored_on_the_picture_and_read_back(tmp_path: Path):
    """The cache the vision adapter's describe mode reads before it spends."""
    store = RequestLogStore(tmp_path / "requests.db")
    store.store_image_description(
        sha="sha_described",
        kind="image",
        media_type="image/png",
        source_bytes=1234,
        description="a terminal showing a failing test",
        described_by="groq/eyes",
    )

    found = store.image_descriptions(["sha_described", "sha_unknown"])
    assert found == {
        "sha_described": ("a terminal showing a failing test", "groq/eyes")
    }
    store.close()


def test_a_description_written_first_keeps_the_thumbnail_written_after(
    tmp_path: Path,
):
    """Describe mode runs while the request is in flight; the row must merge.

    ``INSERT OR IGNORE`` would have left this picture without a thumbnail
    forever, because the description created its row before the request that
    carried it was ever flushed.
    """
    store = RequestLogStore(tmp_path / "requests.db")
    store.store_image_description(
        sha="abc123",
        kind="image",
        media_type="image/png",
        source_bytes=None,
        description="what the picture shows",
        described_by="groq/eyes",
    )
    store.enqueue(_record("r1", (_image(),)))
    store.close()

    store = RequestLogStore(tmp_path / "requests.db")
    row = store.get_request("r1")
    assert row is not None
    image = row["input_images"][0]
    assert image["description"] == "what the picture shows"
    assert image["described_by"] == "groq/eyes"
    assert image["thumbnail_base64"]
    assert image["width"] == 1600
    store.close()


def test_clearing_the_descriptions_keeps_the_pictures(tmp_path: Path):
    store = RequestLogStore(tmp_path / "requests.db")
    store.enqueue(_record("r1", (_image(),)))
    store.close()
    store = RequestLogStore(tmp_path / "requests.db")
    store.store_image_description(
        sha="abc123",
        kind="image",
        media_type="image/png",
        source_bytes=None,
        description="gone soon",
        described_by="groq/eyes",
    )

    assert store.clear_image_descriptions() == 1
    assert store.image_descriptions(["abc123"]) == {}
    row = store.get_request("r1")
    assert row is not None
    assert row["input_images"][0]["thumbnail_base64"]
    # Idempotent: a second click clears nothing and says so.
    assert store.clear_image_descriptions() == 0
    store.close()


def test_an_old_database_gains_the_description_columns(tmp_path: Path):
    """A log written before 6.51.0 must open, not crash and not lose rows."""
    import sqlite3

    path = tmp_path / "requests.db"
    store = RequestLogStore(path)
    store.enqueue(_record("r1", (_image(),)))
    store.close()

    # Rewind the table to its pre-6.51.0 shape, the way an installed release
    # would have left it: the columns simply are not there. Rebuilt rather
    # than DROP COLUMNed so the stored DDL is the one 6.50.1 actually shipped.
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE image_blobs RENAME TO image_blobs_new")
        conn.execute(
            "CREATE TABLE image_blobs (sha TEXT PRIMARY KEY, kind TEXT NOT NULL,"
            " media_type TEXT, source_bytes INTEGER, width INTEGER,"
            " height INTEGER, thumbnail_media_type TEXT, thumbnail BLOB)"
        )
        conn.execute(
            "INSERT INTO image_blobs SELECT sha, kind, media_type, source_bytes,"
            " width, height, thumbnail_media_type, thumbnail FROM image_blobs_new"
        )
        conn.execute("DROP TABLE image_blobs_new")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(image_blobs)")}
    assert "description" not in columns

    upgraded = RequestLogStore(path)
    row = upgraded.get_request("r1")
    assert row is not None
    image = row["input_images"][0]
    # NULL, which reads as "nobody has described this picture" -- the same
    # thing a fresh row says, and what the cache should conclude.
    assert image["description"] is None
    assert image["thumbnail_base64"]
    assert upgraded.image_descriptions(["abc123"]) == {}
    upgraded.close()


def test_an_old_rollup_gains_the_described_counter(tmp_path: Path):
    """A counter added after the rollup shipped is an ALTER, not a rebuild."""
    import sqlite3

    path = tmp_path / "requests.db"
    store = RequestLogStore(path)
    store.enqueue(_record("r1", (_image(),)))
    store.close()

    with sqlite3.connect(path) as conn:
        conn.execute(
            "ALTER TABLE request_stats_rollup RENAME TO request_stats_rollup_old"
        )
        columns = [
            row[1]
            for row in conn.execute("PRAGMA table_info(request_stats_rollup_old)")
            if row[1] != "vision_described"
        ]
        conn.execute(
            "CREATE TABLE request_stats_rollup ("
            + ", ".join(f"{name} NUMERIC" for name in columns)
            + ")"
        )
        conn.execute(
            f"INSERT INTO request_stats_rollup SELECT {', '.join(columns)}"
            " FROM request_stats_rollup_old"
        )
        conn.execute("DROP TABLE request_stats_rollup_old")
        columns_now = {
            row[1] for row in conn.execute("PRAGMA table_info(request_stats_rollup)")
        }
    assert "vision_described" not in columns_now

    upgraded = RequestLogStore(path)
    with sqlite3.connect(path) as conn:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(request_stats_rollup)")
        }
    assert "vision_described" in columns
    # And the hours rolled up before it existed report zero rather than
    # refusing to be read.
    assert upgraded.stats()["vision_described"] == 0
    upgraded.close()
