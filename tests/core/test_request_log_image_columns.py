"""The ten columns 6.53.0 adds, and what NULL means on every row before them.

The rule these tests exist to hold: NULL means "not measured", never zero. A
database written by 6.52.0 must open, migrate and read back NULL for every one
of the new columns -- because "nobody was counting" and "it counted nothing"
are different facts, and only NULL can say the first one.
"""

import sqlite3
from pathlib import Path

from my_claude_code.core.request_images import CapturedImage
from my_claude_code.core.request_log import (
    RequestLogStore,
    RequestRecord,
    RouteAttempt,
    RouteAttemptOutcome,
)

NEW_REQUEST_COLUMNS = (
    "adapter_tokens_in",
    "adapter_tokens_out",
    "est_tokens_in",
    "est_image_tokens",
    "image_bytes_in",
    "image_bytes_out",
)
NEW_ATTEMPT_COLUMNS = ("tokens_in", "tokens_out")
NEW_IMAGE_COLUMNS = ("sent_width", "sent_height")


def columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def old_database(path: Path) -> None:
    """Write a database with the pre-6.53.0 shape and one row in each table.

    Built by letting this version create its own schema and then dropping the
    ten columns this PR adds, rather than by hand-writing a 6.52.0 CREATE
    TABLE. Hand-writing it would drift from what 6.52.0 actually shipped the
    first time anything else changed, and the question here is only "do the
    guarded ALTERs put these ten columns back".
    """
    RequestLogStore(path).close()
    conn = sqlite3.connect(path)
    # The partial index over the image columns has to go first: SQLite refuses
    # to drop a column any index names. Nothing is being weakened here -- the
    # migration recreates it, and the assertions below are untouched.
    conn.execute("DROP INDEX IF EXISTS idx_requests_image_v1")
    for table, dropped in (
        ("requests", NEW_REQUEST_COLUMNS),
        ("request_attempts", NEW_ATTEMPT_COLUMNS),
    ):
        for column in dropped:
            conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    # ``image_blobs`` is rebuilt rather than altered: SQLite's DROP COLUMN
    # re-parses the stored CREATE TABLE, and this one carries block comments
    # it cannot round-trip. The shape below is the 6.52.0 one verbatim.
    conn.executescript(
        """
        DROP TABLE image_blobs;
        CREATE TABLE image_blobs (
            sha TEXT PRIMARY KEY, kind TEXT NOT NULL, media_type TEXT,
            source_bytes INTEGER, width INTEGER, height INTEGER,
            thumbnail_media_type TEXT, thumbnail BLOB,
            description TEXT, described_by TEXT, described_at REAL
        );
        """
    )
    conn.execute(
        "INSERT INTO requests (id, ts_epoch, ts_iso, endpoint, protocol, status,"
        " stream, tokens_in) VALUES ('old', 1.0, 'then', '/v1/messages',"
        " 'anthropic', 'success', 0, 4242)"
    )
    conn.execute(
        "INSERT INTO request_attempts (request_id, attempt, provider, model_ref,"
        " outcome, duration_ms) VALUES ('old', 0, 'nvidia_nim', 'nim/m',"
        " 'succeeded', 12.0)"
    )
    conn.execute(
        "INSERT INTO image_blobs (sha, kind, media_type, source_bytes, width,"
        " height) VALUES ('sha-old', 'image', 'image/png', 900, 640, 480)"
    )
    conn.execute("INSERT INTO request_images VALUES ('old', 0, 'sha-old')")
    conn.commit()
    conn.close()


def test_migrations_add_every_new_column_to_an_existing_db(tmp_path: Path):
    path = tmp_path / "requests.db"
    old_database(path)

    store = RequestLogStore(path)
    try:
        with sqlite3.connect(path) as conn:
            assert set(NEW_REQUEST_COLUMNS) <= columns(conn, "requests")
            assert set(NEW_ATTEMPT_COLUMNS) <= columns(conn, "request_attempts")
            assert set(NEW_IMAGE_COLUMNS) <= columns(conn, "image_blobs")
    finally:
        store.close()


def test_migrations_are_idempotent_on_an_existing_db(tmp_path: Path):
    path = tmp_path / "requests.db"
    old_database(path)

    for _ in range(3):
        RequestLogStore(path).close()

    with sqlite3.connect(path) as conn:
        assert set(NEW_REQUEST_COLUMNS) <= columns(conn, "requests")


def test_old_rows_read_back_null_not_zero(tmp_path: Path):
    path = tmp_path / "requests.db"
    old_database(path)

    store = RequestLogStore(path)
    try:
        with sqlite3.connect(path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM requests WHERE id = 'old'").fetchone()
            for column in NEW_REQUEST_COLUMNS:
                assert row[column] is None, column
            attempt = conn.execute(
                "SELECT * FROM request_attempts WHERE request_id = 'old'"
            ).fetchone()
            for column in NEW_ATTEMPT_COLUMNS:
                assert attempt[column] is None, column
            image = conn.execute(
                "SELECT * FROM image_blobs WHERE sha = 'sha-old'"
            ).fetchone()
            for column in NEW_IMAGE_COLUMNS:
                assert image[column] is None, column
            # And the counter that already existed is untouched: this PR adds
            # a second number beside tokens_in, it never rewrites it.
            assert row["tokens_in"] == 4242
    finally:
        store.close()


def test_new_columns_round_trip_through_a_record(tmp_path: Path):
    path = tmp_path / "requests.db"
    store = RequestLogStore(path)
    store.enqueue(
        RequestRecord(
            id="r1",
            endpoint="/v1/messages",
            protocol="anthropic",
            provider="anthropic",
            tokens_in=1000,
            adapter_tokens_in=120,
            adapter_tokens_out=45,
            est_tokens_in=1180,
            est_image_tokens=1560,
            image_bytes_in=2_000_000,
            image_bytes_out=400_000,
            input_image_count=1,
            images=(
                CapturedImage(
                    sha256="sha-new",
                    kind="image",
                    media_type="image/png",
                    source_bytes=2_000_000,
                    width=3840,
                    height=2160,
                    sent_width=1456,
                    sent_height=819,
                ),
            ),
            attempts=(
                RouteAttempt(
                    attempt=1000,
                    provider="anthropic",
                    model_ref="anthropic/claude-sonnet-4-5",
                    outcome=RouteAttemptOutcome.SUCCEEDED,
                    params={"kind": "describe", "image_sha": "sha-new"},
                    tokens_in=120,
                    tokens_out=45,
                ),
            ),
        )
    )
    store.close()

    reader = RequestLogStore(path)
    try:
        row = reader.get_request("r1")
        assert row is not None
        assert row["adapter_tokens_in"] == 120
        assert row["adapter_tokens_out"] == 45
        assert row["est_tokens_in"] == 1180
        assert row["est_image_tokens"] == 1560
        assert row["image_bytes_in"] == 2_000_000
        assert row["image_bytes_out"] == 400_000
        # The answering model's own counter is untouched by any of it.
        assert row["tokens_in"] == 1000
        assert row["input_images"][0]["sent_width"] == 1456
        assert row["input_images"][0]["sent_height"] == 819
        assert row["route_attempts"][0]["tokens_in"] == 120
        assert row["route_attempts"][0]["tokens_out"] == 45
    finally:
        reader.close()


def test_an_ordinary_attempt_leaves_its_token_columns_null(tmp_path: Path):
    path = tmp_path / "requests.db"
    store = RequestLogStore(path)
    store.enqueue(
        RequestRecord(
            id="r2",
            endpoint="/v1/messages",
            protocol="anthropic",
            attempts=(
                RouteAttempt(
                    attempt=0,
                    provider="nvidia_nim",
                    model_ref="nvidia_nim/m",
                    outcome=RouteAttemptOutcome.SUCCEEDED,
                ),
            ),
        )
    )
    store.close()

    reader = RequestLogStore(path)
    try:
        row = reader.get_request("r2")
        assert row is not None
        assert row["route_attempts"][0]["tokens_in"] is None
        assert row["adapter_tokens_in"] is None
        assert row["est_tokens_in"] is None
    finally:
        reader.close()
