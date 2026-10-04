"""Retention prune: the orphan sweeps follow the deleted rows, and keep exactly what they kept.

Until 7.72.2 every prune pass -- one per hundred requests -- scanned seven whole
tables for orphans, inside the writer's write transaction: about a minute on a
12 GB log even when the pass deleted nothing. A pass now follows only the
requests it deleted, and sweeps a whole table only when something else may have
left an orphan in it.

The point of these tests is data safety. ``_reference_prune`` below is the
pre-7.72.2 ``prune`` body, statement for statement; every scenario runs it and
the new ``prune`` on identical copies of one database and compares every table,
row by row.
"""

import hashlib
import shutil
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from my_claude_code.core import request_log as request_log_module
from my_claude_code.core.media_store import MediaOutputRecord, media_root
from my_claude_code.core.request_images import CapturedImage
from my_claude_code.core.request_log import (
    RequestLogStore,
    RequestRecord,
    RouteAttempt,
    RouteAttemptOutcome,
)

_RECORDS = 40
_BASE_TS = 1_790_000_000.0
# A permutation of 0..39, so "oldest by time" is not "first written".
_TS_ORDER = [(index * 17) % _RECORDS for index in range(_RECORDS)]


def _reference_prune(path: Path, max_rows: int, state: dict[str, Any]) -> int:
    """The pre-7.72.2 ``RequestLogStore.prune`` body, kept as the reference.

    Only the store attributes it read are parameters: ``max_rows`` and the
    tool-sweep clock (``state``). The media files it would delete are handed to
    the same ``delete_media_files`` the store calls, so a test can watch them.
    """

    if max_rows <= 0:
        return 0
    conn = sqlite3.connect(path, timeout=10)
    try:
        with conn:
            cursor = conn.execute(
                "DELETE FROM requests WHERE id IN ("
                " SELECT id FROM requests ORDER BY ts_epoch DESC"
                " LIMIT -1 OFFSET ?"
                ")",
                (max_rows,),
            )
            removed = cursor.rowcount
            conn.execute(
                "DELETE FROM request_bodies WHERE NOT EXISTS ("
                " SELECT 1 FROM requests WHERE requests.id ="
                " request_bodies.request_id)"
            )
            conn.execute(
                "DELETE FROM body_blobs WHERE NOT EXISTS ("
                " SELECT 1 FROM request_bodies WHERE request_bodies.sha ="
                " body_blobs.sha OR request_bodies.input_sha = body_blobs.sha)"
            )
            conn.execute(
                "DELETE FROM request_images WHERE NOT EXISTS ("
                " SELECT 1 FROM requests WHERE requests.id ="
                " request_images.request_id)"
            )
            conn.execute(
                "DELETE FROM request_attempts WHERE NOT EXISTS ("
                " SELECT 1 FROM requests WHERE requests.id ="
                " request_attempts.request_id)"
            )
            conn.execute(
                "DELETE FROM image_blobs WHERE NOT EXISTS ("
                " SELECT 1 FROM request_images WHERE request_images.sha ="
                " image_blobs.sha)"
            )
            conn.execute(
                "DELETE FROM request_media WHERE NOT EXISTS ("
                " SELECT 1 FROM requests WHERE requests.id ="
                " request_media.request_id)"
            )
            orphaned_media = [
                str(row[0])
                for row in conn.execute(
                    "SELECT sha256 FROM media_blobs WHERE stored = 1"
                    " AND NOT EXISTS (SELECT 1 FROM request_media"
                    " WHERE request_media.sha256 = media_blobs.sha256)"
                )
            ]
            conn.execute(
                "DELETE FROM media_blobs WHERE NOT EXISTS ("
                " SELECT 1 FROM request_media WHERE request_media.sha256 ="
                " media_blobs.sha256)"
            )
            if orphaned_media:
                request_log_module.delete_media_files(media_root(path), orphaned_media)
            conn.execute(
                "DELETE FROM media_jobs WHERE created_at < ? AND NOT EXISTS ("
                " SELECT 1 FROM requests WHERE requests.id ="
                " media_jobs.request_id)",
                (time.time() - request_log_module._MEDIA_JOB_ORPHAN_GRACE_SECONDS,),
            )
            now = time.monotonic()
            if removed and (
                state.get("last_tool_sweep") is None
                or now - state["last_tool_sweep"]
                >= request_log_module._TOOL_SWEEP_INTERVAL_SECONDS
            ):
                state["last_tool_sweep"] = now
                RequestLogStore._sweep_tool_catalogues(conn)
        if removed:
            conn.execute("PRAGMA incremental_vacuum")
        return removed
    finally:
        conn.close()


def _image(sha: str) -> CapturedImage:
    return CapturedImage(
        sha256=sha, kind="image", media_type="image/png", source_bytes=len(sha)
    )


def _record(index: int, **overrides: Any) -> RequestRecord:
    """Shared prompts, replies, pictures and files between old and new rows."""

    images: list[CapturedImage] = []
    if index % 2 == 0:
        images.append(_image("img-shared"))
    if index % 5 == 0:
        images.append(_image(f"img-{index}"))
    media: list[MediaOutputRecord] = []
    if index % 4 == 0:
        media.append(
            MediaOutputRecord(
                sha256="media-shared", mime="image/png", bytes=10, stored=True
            )
        )
    if index % 6 == 0:
        media.append(
            MediaOutputRecord(
                sha256=f"media-{index}",
                mime="image/png",
                bytes=11,
                stored=index % 12 == 0,
                idx=1,
            )
        )
    prompt = (
        f"unique prompt {index} " * 30
        if index % 3 == 0
        else f"shared prompt {index % 5} " * 30
    )
    fields: dict[str, Any] = {
        "id": f"r{index:02d}",
        "ts_epoch": _BASE_TS + _TS_ORDER[index],
        "endpoint": "/v1/messages",
        "protocol": "anthropic",
        "requested_model": "claude-sonnet-4-5",
        "provider": "nvidia_nim",
        "resolved_model": "test-model",
        "stream": True,
        "input_text": prompt,
        "output_text": f"reply {index % 4}",
        "thinking_text": "considered it" if index % 7 == 0 else None,
        "tokens_in": 10,
        "tokens_out": 20,
        "duration_ms": 120.0,
        "status": "success",
        "images": tuple(images),
        "media_outputs": tuple(media),
        "tools": (
            [{"name": f"tool{index % 2}", "input_schema": {"type": "object"}}]
            if index % 3 == 0
            else None
        ),
        "attempts": (
            RouteAttempt(
                attempt=0,
                provider="nvidia_nim",
                model_ref="nvidia_nim/test-model",
                outcome=RouteAttemptOutcome.FAILED,
                error_kind="timeout",
                duration_ms=10.0,
            ),
            RouteAttempt(
                attempt=1,
                provider="nvidia_nim",
                model_ref="nvidia_nim/test-model",
                outcome=RouteAttemptOutcome.SUCCEEDED,
                duration_ms=110.0,
            ),
        ),
    }
    fields.update(overrides)
    return RequestRecord(**fields)


@pytest.fixture(scope="module")
def template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One database written by the real writer, with a dictionary in use."""

    path = tmp_path_factory.mktemp("prune-template") / "requests.db"
    RequestLogStore(path, max_rows=0).close()
    samples = [
        (f"shared prompt {index % 5} " * 30 + f"sample {index}").encode()
        for index in range(300)
    ]
    dictionary = request_log_module.zstd.train_dict(samples, 2048)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "INSERT INTO body_dictionaries (created_at, content) VALUES (?, ?)",
            (_BASE_TS, dictionary.dict_content),
        )
    writer = RequestLogStore(path, max_rows=0)
    for index in range(_RECORDS):
        writer.enqueue(_record(index))
    writer.close()
    with closing(sqlite3.connect(path)) as conn, conn:
        # One job per side of the cut, both long past the orphan grace.
        for job, request_id in (("job-old", "r01"), ("job-new", "r00")):
            conn.execute(
                "INSERT INTO media_jobs (job_id, request_id, provider, model,"
                " upstream_id, created_at) VALUES (?, ?, 'p', 'm', 'u', 1.0)",
                (job, request_id),
            )
        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "requests",
                "request_bodies",
                "body_blobs",
                "body_dictionaries",
                "request_images",
                "image_blobs",
                "request_attempts",
                "request_media",
                "media_blobs",
                "media_jobs",
                "tool_catalogues",
            )
        }
        assert conn.execute(
            "SELECT COUNT(*) FROM body_blobs WHERE dict_id IS NOT NULL"
        ).fetchone()[0]
    # Every table the sweeps touch has rows to lose, or the comparison is empty.
    assert all(counts.values()), counts
    return path


def _dump(path: Path) -> dict[str, list[tuple[Any, ...]]]:
    """Every table, every row, ordered by primary key (rowid where there is none).

    ``server_sessions`` holds one row per store opened, stamped with the time
    and process; prune never touches it, so it is compared by count.
    """

    with closing(sqlite3.connect(path)) as conn:
        tables = [
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        ]
        dump: dict[str, list[tuple[Any, ...]]] = {}
        for table in tables:
            if table == "server_sessions":
                dump[table] = conn.execute(
                    "SELECT COUNT(*) FROM server_sessions"
                ).fetchall()
                continue
            keys = sorted(
                (int(row[5]), str(row[1]))
                for row in conn.execute(f'PRAGMA table_info("{table}")')
                if int(row[5])
            )
            order = ", ".join(f'"{name}"' for _, name in keys) or "rowid"
            dump[table] = conn.execute(
                f'SELECT * FROM "{table}" ORDER BY {order}'
            ).fetchall()
        return dump


@pytest.fixture
def deleted_files(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        request_log_module,
        "delete_media_files",
        lambda root, shas: calls.append(tuple(sorted(shas))),
    )
    return calls


def _pair(template: Path, tmp_path: Path) -> tuple[Path, Path]:
    paths = []
    for side in ("reference", "new"):
        (tmp_path / side).mkdir()
        target = tmp_path / side / "requests.db"
        shutil.copyfile(template, target)
        paths.append(target)
    return paths[0], paths[1]


def _warm_new_store(path: Path) -> RequestLogStore:
    """A store whose first pass -- always a whole sweep -- has already run."""

    store = RequestLogStore(path, max_rows=1_000)
    store.close()
    assert store.prune() == 0
    assert store._full_sweeps_owed == set()
    return store


@pytest.mark.parametrize(
    ("scenario", "caps"),
    [
        ("nothing to delete", [1_000, 40]),
        ("the oldest by time, not by write order", [30]),
        ("by row cap, pass after pass", [35, 33, 25]),
        ("all but the newest row", [1]),
    ],
)
def test_the_new_prune_keeps_exactly_what_the_old_one_kept(
    template: Path,
    tmp_path: Path,
    deleted_files: list[tuple[str, ...]],
    scenario: str,
    caps: list[int],
) -> None:
    reference, new = _pair(template, tmp_path)

    RequestLogStore(reference, max_rows=1_000).close()
    state: dict[str, Any] = {}
    assert _reference_prune(reference, 1_000, state) == 0
    reference_removed = [_reference_prune(reference, cap, state) for cap in caps]
    reference_files = list(deleted_files)
    deleted_files.clear()

    store = _warm_new_store(new)
    new_removed = []
    for cap in caps:
        store._max_rows = cap
        new_removed.append(store.prune())
        # Nothing but this pass deleted anything: the next one owes no sweep.
        assert store._full_sweeps_owed == set()

    assert new_removed == reference_removed, scenario
    assert deleted_files == reference_files
    reference_rows, new_rows = _dump(reference), _dump(new)
    assert reference_rows.keys() == new_rows.keys()
    for table in reference_rows:
        assert new_rows[table] == reference_rows[table], (scenario, table)


_NAMED = (
    ("body_blobs", "sha", "SELECT request_id, sha FROM request_bodies"),
    ("body_blobs", "sha", "SELECT request_id, input_sha FROM request_bodies"),
    ("image_blobs", "sha", "SELECT request_id, sha FROM request_images"),
    ("media_blobs", "sha256", "SELECT request_id, sha256 FROM request_media"),
)


def test_a_blob_shared_with_a_surviving_request_survives(
    template: Path, tmp_path: Path, deleted_files: list[tuple[str, ...]]
) -> None:
    _, new = _pair(template, tmp_path)
    with closing(sqlite3.connect(new)) as conn:
        gone = {
            str(row[0])
            for row in conn.execute(
                "SELECT id FROM requests ORDER BY ts_epoch DESC LIMIT -1 OFFSET 25"
            )
        }
        shared: list[tuple[str, str, str]] = []
        only_gone: list[tuple[str, str, str]] = []
        for table, column, links in _NAMED:
            names: dict[str, set[str]] = {}
            for request_id, sha in conn.execute(links):
                if sha is not None:
                    names.setdefault(str(sha), set()).add(str(request_id))
            for sha, owners in names.items():
                if owners & gone:
                    target = shared if owners - gone else only_gone
                    target.append((table, column, sha))
    assert len(gone) == 15
    # The fixture shares a prompt, a reply, a picture and a file across the cut.
    assert {table for table, _, _ in shared} == {
        "body_blobs",
        "image_blobs",
        "media_blobs",
    }
    assert {table for table, _, _ in only_gone} >= {"body_blobs", "image_blobs"}

    store = _warm_new_store(new)
    store._max_rows = 25
    assert store.prune() == 15

    with closing(sqlite3.connect(new)) as conn:

        def present(table: str, column: str, sha: str) -> bool:
            return bool(
                conn.execute(
                    f"SELECT 1 FROM {table} WHERE {column} = ?", (sha,)
                ).fetchall()
            )

        assert all(present(*blob) for blob in shared)
        assert not any(present(*blob) for blob in only_gone)
        for table in ("request_bodies", "request_images", "request_attempts"):
            assert not conn.execute(
                f"SELECT 1 FROM {table} WHERE request_id IN"
                f" ({', '.join('?' * len(gone))})",
                sorted(gone),
            ).fetchall()
        # Every surviving link still finds its blob.
        assert conn.execute(
            "SELECT COUNT(*) FROM request_bodies WHERE (sha IS NOT NULL AND sha"
            " NOT IN (SELECT sha FROM body_blobs)) OR (input_sha IS NOT NULL AND"
            " input_sha NOT IN (SELECT sha FROM body_blobs))"
        ).fetchone() == (0,)
    # A file a surviving row still names is never deleted from disk.
    shared_files = {sha for table, _, sha in shared if table == "media_blobs"}
    assert all(not shared_files & set(call) for call in deleted_files)


def test_a_pass_above_the_targeted_bound_sweeps_whole_tables_and_agrees(
    template: Path,
    tmp_path: Path,
    deleted_files: list[tuple[str, ...]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference, new = _pair(template, tmp_path)
    RequestLogStore(reference, max_rows=1_000).close()
    assert _reference_prune(reference, 20, {}) == 20
    reference_files = list(deleted_files)
    deleted_files.clear()

    store = _warm_new_store(new)
    monkeypatch.setattr(request_log_module, "_TARGETED_SWEEP_MAX_ROWS", 3)
    statements = _trace(store, monkeypatch)
    store._max_rows = 20
    assert store.prune() == 20

    assert any("NOT EXISTS ( SELECT 1 FROM requests" in sql for sql in statements)
    assert deleted_files == reference_files
    assert _dump(new) == _dump(reference)


def _trace(store: RequestLogStore, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every statement the store's connections run from now on."""

    statements: list[str] = []
    connect = store._connect

    def traced() -> sqlite3.Connection:
        conn = connect()
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(store, "_connect", traced)
    return statements


_SWEPT_TABLES = (
    "request_bodies",
    "body_blobs",
    "request_images",
    "request_attempts",
    "image_blobs",
    "request_media",
    "media_blobs",
)


def _file_digest(path: Path) -> str:
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_a_pass_that_deletes_nothing_runs_no_sweep_and_writes_nothing(
    template: Path,
    tmp_path: Path,
    deleted_files: list[tuple[str, ...]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference, new = _pair(template, tmp_path)
    RequestLogStore(reference, max_rows=1_000).close()
    assert _reference_prune(reference, 1_000, {}) == 0
    reference_before = _file_digest(reference)
    assert _reference_prune(reference, 1_000, {}) == 0
    # What today's code changes on such a pass: nothing.
    assert _file_digest(reference) == reference_before

    store = _warm_new_store(new)
    before = _file_digest(new)
    statements = _trace(store, monkeypatch)
    assert store.prune() == 0

    swept = [
        sql
        for sql in statements
        if any(f"FROM {table}" in sql for table in _SWEPT_TABLES)
    ]
    assert swept == []
    assert _file_digest(new) == before
    assert _dump(new) == _dump(reference)
    assert deleted_files == []


def test_orphans_made_outside_a_pass_are_swept_by_the_next_pass_as_before(
    template: Path, tmp_path: Path, deleted_files: list[tuple[str, ...]]
) -> None:
    """A re-written request, a picture described before its request is logged,
    and a video file linked to a request that is gone: the old prune removed
    each on its next pass even when that pass deleted no request, and so does
    the new one."""

    def make_orphans(store: RequestLogStore) -> None:
        store.store_image_description(
            sha="img-never-logged",
            kind="image",
            media_type="image/png",
            source_bytes=3,
            description="a cat",
            described_by="vision-model",
        )
        # Re-written with a new prompt: its old prompt, named by no other
        # request, loses its last name without any request being deleted.
        conn = store._connect()
        try:
            store._flush([_record(9, input_text="a different prompt")], conn)
        finally:
            conn.close()
        store.record_media_job_content(
            "job-none",
            "r-never-written",
            MediaOutputRecord(
                sha256="media-lost", mime="video/mp4", bytes=5, stored=True
            ),
            at=_BASE_TS,
        )

    reference, new = _pair(template, tmp_path)
    old_side = RequestLogStore(reference, max_rows=1_000)
    old_side.close()
    state: dict[str, Any] = {}
    assert _reference_prune(reference, 1_000, state) == 0
    make_orphans(old_side)
    assert _reference_prune(reference, 1_000, state) == 0
    reference_files = list(deleted_files)
    deleted_files.clear()

    store = _warm_new_store(new)
    make_orphans(store)
    assert store._full_sweeps_owed == {
        "body_blobs",
        "image_blobs",
        "media_blobs",
        "request_media",
    }
    assert store.prune() == 0
    assert store._full_sweeps_owed == set()

    assert deleted_files == reference_files == [("media-lost",)]
    new_rows = _dump(new)
    assert new_rows == _dump(reference)
    assert not [row for row in new_rows["image_blobs"] if row[0] == "img-never-logged"]


def test_the_first_pass_of_a_process_removes_every_orphan_already_there(
    template: Path, tmp_path: Path, deleted_files: list[tuple[str, ...]]
) -> None:
    """Orphans left by an older version, a crash or another process: the first
    pass sweeps whole tables, as every pass did, even when it deletes nothing."""

    reference, new = _pair(template, tmp_path)
    for path in (reference, new):
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute(
                "INSERT INTO request_bodies (request_id, sha, input_sha)"
                " VALUES ('gone', 'body-orphan', NULL)"
            )
            conn.execute(
                "INSERT INTO body_blobs (sha, dict_id, payload) VALUES"
                " ('body-orphan', NULL, x'00'), ('body-unnamed', NULL, x'00')"
            )
            conn.execute(
                "INSERT INTO request_attempts (request_id, attempt, provider,"
                " model_ref, outcome) SELECT 'gone', 0, provider, model_ref, outcome"
                " FROM request_attempts LIMIT 1"
            )
            conn.execute(
                "INSERT INTO request_images (request_id, position, sha)"
                " VALUES ('gone', 0, 'img-orphan')"
            )
            conn.execute(
                "INSERT INTO image_blobs (sha, kind) VALUES ('img-orphan', 'image'),"
                " (NULL, 'image'), ('img-unnamed', 'image')"
            )
            conn.execute(
                "INSERT INTO request_media (request_id, direction, idx, sha256)"
                " VALUES ('gone', 'out', 0, 'media-orphan')"
            )
            conn.execute(
                "INSERT INTO media_blobs (sha256, mime, bytes, created_at, stored)"
                " VALUES ('media-orphan', 'image/png', 1, 1.0, 1)"
            )
    RequestLogStore(reference, max_rows=1_000).close()
    assert _reference_prune(reference, 1_000, {}) == 0
    reference_files = list(deleted_files)
    deleted_files.clear()

    store = RequestLogStore(new, max_rows=1_000)
    store.close()
    assert store.prune() == 0

    assert deleted_files == reference_files == [("media-orphan",)]
    new_rows = _dump(new)
    assert new_rows == _dump(reference)
    assert [row for row in new_rows["image_blobs"] if row[0] is None] == []
    assert len(new_rows["body_blobs"]) == len(_dump(template)["body_blobs"])


def test_a_rolled_back_pass_still_owes_its_sweeps(
    template: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, new = _pair(template, tmp_path)
    store = RequestLogStore(new, max_rows=1_000)
    store.close()

    def fail(*_args: Any) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store, "_sweep_orphans", fail)
    assert store.prune() == 0
    assert store._full_sweeps_owed == set(request_log_module._ORPHAN_SWEEP_TABLES)
