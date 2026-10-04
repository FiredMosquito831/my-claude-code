"""REQUEST_LOG_COMPRESSION_LEVEL reaches the writer -- and the default writes what it always wrote.

Until 7.72.2 the writer passed a fixed level 9 to zstd, so the setting, its
dashboard card and ``retune`` changed nothing (a level-3 run on a real log wrote
byte-identical sizes). The level now applies to bodies written from the next
batch on; stored rows keep theirs, and reading never depends on it.
"""

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from my_claude_code.config.admin.manifest import FIELDS
from my_claude_code.core import request_log as request_log_module
from my_claude_code.core.request_log import RequestLogStore, RequestRecord

zstd = request_log_module.zstd

#: Bodies shaped like real traffic: a long, repetitive prompt that grows by a
#: turn, JSON-ish tool output, prose, and a short reply.
_CORPUS: list[tuple[str, str]] = [
    (
        "You are a coding agent. Follow the repository rules.\n"
        + "".join(
            f"user: turn {turn} -- please read src/module_{turn % 7}.py and fix the"
            f" failing test test_case_{turn}\nassistant: reading the file now.\n"
            for turn in range(depth)
        ),
        f"Done: {depth} turns handled.",
    )
    for depth in (5, 40, 120)
] + [
    (
        '{"tool_result": ['
        + ", ".join(
            f'{{"path": "src/pkg/file_{index}.py", "lines": {index * 13}}}'
            for index in range(300)
        )
        + "]}",
        "Listed 300 files.",
    ),
    ("Short question?", "Short answer."),
]


def _record(request_id: str, prompt: str, reply: str) -> RequestRecord:
    return RequestRecord(
        id=request_id,
        ts_epoch=1_790_000_000.0,
        endpoint="/v1/messages",
        protocol="anthropic",
        requested_model="claude-sonnet-4-5",
        provider="nvidia_nim",
        resolved_model="test-model",
        stream=True,
        input_text=prompt,
        output_text=reply,
        status="success",
    )


def _write(store: RequestLogStore, prefix: str) -> list[RequestRecord]:
    """One batch through the writer's own ``_flush``, as the writer thread runs it."""

    # Bodies are content-addressed: content already stored is never compressed
    # again, so each batch carries its own marker.
    records = [
        _record(f"{prefix}{index}", f"{prompt}\n[{prefix}]", f"{reply} [{prefix}]")
        for index, (prompt, reply) in enumerate(_CORPUS)
    ]
    conn = store._connect()
    try:
        store._flush(records, conn)
    finally:
        conn.close()
    return records


def _blobs(path: Path, prefix: str) -> dict[str, tuple[int | None, bytes]]:
    with closing(sqlite3.connect(path)) as conn:
        return {
            str(sha): (dict_id, bytes(payload))
            for sha, dict_id, payload in conn.execute(
                "SELECT b.sha, b.dict_id, b.payload FROM body_blobs b WHERE b.sha IN"
                " (SELECT sha FROM request_bodies WHERE request_id LIKE ?"
                " UNION SELECT input_sha FROM request_bodies WHERE request_id LIKE ?)",
                (prefix + "%", prefix + "%"),
            )
        }


def _expected(
    store: RequestLogStore, records: list[RequestRecord], level: int
) -> dict[str, tuple[int | None, bytes]]:
    """What the pre-7.72.2 writer wrote at level 9: ``zstd.compress`` of each
    packed body, with the active dictionary -- here at ``level``."""

    dict_id = store._active_dict_id
    expected: dict[str, tuple[int | None, bytes]] = {}
    for record in records:
        for packed in store._pack_record(record):
            if packed is None:
                continue
            expected[hashlib.sha256(packed).hexdigest()] = (
                dict_id,
                zstd.compress(
                    packed, level=level, zstd_dict=store._dictionary(dict_id)
                ),
            )
    return expected


@pytest.fixture(params=[False, True], ids=["no dictionary", "dictionary"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Any:
    path = tmp_path / "requests.db"
    if request.param:
        RequestLogStore(path, max_rows=0).close()
        samples = [
            f"{prompt} sample {index}".encode()
            for index in range(60)
            for prompt, _ in _CORPUS
        ]
        dictionary = zstd.train_dict(samples, 1024)
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute(
                "INSERT INTO body_dictionaries (created_at, content) VALUES (1.0, ?)",
                (dictionary.dict_content,),
            )
    store = RequestLogStore(path, max_rows=0)
    store.close()
    assert (store._active_dict_id is not None) is request.param
    return store


def test_the_default_level_writes_exactly_what_the_old_writer_wrote(
    store: RequestLogStore,
) -> None:
    """Golden: unset, the setting is 9, and every byte matches the fixed-9 writer."""

    assert store._compression_level == 9
    records = _write(store, "d")
    written = _blobs(store.db_path, "d")
    assert written == _expected(store, records, 9)
    assert len(written) == 2 * len(_CORPUS)  # each prompt and each reply


@pytest.mark.parametrize("level", [1, 3, 15, 19])
def test_a_configured_level_is_the_level_written(
    store: RequestLogStore, level: int
) -> None:
    store._compression_level = level
    records = _write(store, f"l{level}-")
    written = _blobs(store.db_path, f"l{level}-")
    assert written == _expected(store, records, level)
    # It reads back byte-identical, whatever the level.
    for record in records:
        row = store.get_request(record.id)
        assert row is not None
        assert row["input_text"] == record.input_text
        assert row["output_text"] == record.output_text
    # Not the fixed-9 bytes any more. Which way the size moves depends on the
    # input (on this small corpus without a dictionary, 1 and 3 come out
    # smaller than 9); the measured trade-off on a real log is in the notes.
    total = sum(len(payload) for _, payload in written.values())
    at_nine = sum(len(payload) for _, payload in _expected(store, records, 9).values())
    assert total != at_nine


def test_a_saved_level_applies_from_the_next_batch_without_a_restart(
    store: RequestLogStore,
) -> None:
    first = _write(store, "before-")
    store.retune(
        max_rows=0,
        text_max_chars=request_log_module.MAX_TEXT_CHARS,
        compression_level=1,
        queue_max_size=10_000,
        compress_bodies=True,
    )
    second = _write(store, "after-")

    assert _blobs(store.db_path, "before-") == _expected(store, first, 9)
    assert _blobs(store.db_path, "after-") == _expected(store, second, 1)
    # Rows written before the change are untouched by it and still read back.
    for record in first:
        row = store.get_request(record.id)
        assert row is not None
        assert row["input_text"] == record.input_text


def test_the_dashboard_says_it_applies_on_save_to_new_rows() -> None:
    field = next(
        field for field in FIELDS if field.key == "REQUEST_LOG_COMPRESSION_LEVEL"
    )
    assert field.restart_required is False
    assert field.default == str(request_log_module._BODY_COMPRESSION_LEVEL) == "9"
    description = field.description.lower()
    assert "already stored" in description
    assert "dictionary" in description
