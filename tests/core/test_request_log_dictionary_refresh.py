"""Per-kind compression dictionaries, relearned in the background (7.73.0).

A dictionary used to be trained once, on the writer thread, and never again; on
a real log it dated from 2026-08-09 and new prompts compressed 1.8-2.0x worse
than against a fresh one. Now each kind -- prompts, the rest of a body, wire
snapshots -- is relearned when its dictionary is older than the last 14 days of
traffic and 1,024 samples of it arrived in them: on a thread of its own, never
the writer's or the event loop's, keeping every older dictionary for good.
"""

import json
import sqlite3
import threading
import time
from compression import zstd
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from my_claude_code.core import request_log as request_log_module
from my_claude_code.core.request_log import (
    RequestLogStore,
    RequestRecord,
    RouteAttempt,
    RouteAttemptOutcome,
)

_DAY = 86_400.0
_AGE = request_log_module._DICT_REFRESH_AGE_SECONDS
_MIN = request_log_module._DICT_REFRESH_MIN_SAMPLES
_BOOTSTRAP = request_log_module._BODY_DICT_MIN_SAMPLES


def _prompt(index: int) -> str:
    return (
        "You are a coding agent. Follow the conventions of this repository. " * 60
        + f"\nTurn {index}: explain why test_{index} fails in module m{index % 37}."
    )


def _wire(index: int) -> str:
    return json.dumps(
        {
            "model": f"vendor/model-{index % 5}",
            "max_tokens": 16384,
            "messages": [{"role": "user", "chars": 100 + index % 50}] * 4,
            "_original_chars": 9000 + index,
        }
    )


def _record(index: int, ts: float, *, prefix: str = "r") -> RequestRecord:
    return RequestRecord(
        id=f"{prefix}{index:05d}",
        ts_epoch=ts,
        endpoint="/v1/messages",
        protocol="anthropic",
        requested_model="claude-sonnet-4-5",
        provider="nvidia_nim",
        resolved_model="test-model",
        stream=True,
        input_text=_prompt(index),
        output_text=f"Reply {index}: the fixture in m{index % 37} is stale. " * 30,
        tokens_in=10,
        tokens_out=20,
        duration_ms=120.0,
        status="success",
        attempts=(
            RouteAttempt(
                attempt=0,
                provider="nvidia_nim",
                model_ref="nvidia_nim/test-model",
                outcome=RouteAttemptOutcome.SUCCEEDED,
                duration_ms=100.0,
                wire_body=_wire(index),
            ),
        ),
    )


def _settle(store: RequestLogStore) -> None:
    """Wait for the store's trainer to end and the writer to store its results."""
    deadline = time.monotonic() + 180
    assert store._dictionaries_checked.wait(60), "the writer never started"
    trainer = store._trainer
    if trainer is not None:
        trainer.join(timeout=180)
        assert not trainer.is_alive()
    while store._trained:
        assert time.monotonic() < deadline, "a trained dictionary was never stored"
        time.sleep(0.05)


def _dictionaries(path: Path) -> dict[str, list[tuple[int, Any]]]:
    with closing(sqlite3.connect(path)) as conn:
        return {
            "body": [
                (int(row[0]), row[1])
                for row in conn.execute(
                    "SELECT id, kind FROM body_dictionaries ORDER BY id"
                )
            ],
            "wire": [
                (int(row[0]), None)
                for row in conn.execute("SELECT id FROM wire_dictionaries ORDER BY id")
            ],
        }


def _seed_dictionary(
    path: Path, table: str, created_at: float, kind: str | None = None
) -> int:
    samples = [(_prompt(i) + _wire(i)).encode() for i in range(300)]
    content = zstd.train_dict(samples, 4096).dict_content
    with closing(sqlite3.connect(path)) as conn, conn:
        if table == "wire_dictionaries":
            cursor = conn.execute(
                "INSERT INTO wire_dictionaries (created_at, content) VALUES (?, ?)",
                (created_at, content),
            )
        else:
            cursor = conn.execute(
                "INSERT INTO body_dictionaries (created_at, content, kind)"
                " VALUES (?, ?, ?)",
                (created_at, content, kind),
            )
    return int(cursor.lastrowid or 0)


def _fill(path: Path, count: int, ts: float, *, prefix: str = "r") -> None:
    store = RequestLogStore(path, max_rows=0)
    for index in range(count):
        store.enqueue(_record(index, ts + index * 0.001, prefix=prefix))
    store.close()


# ------------------------------------------------------------------ the rule


def _rule_store(tmp_path: Path) -> RequestLogStore:
    store = RequestLogStore(tmp_path / "requests.db", max_rows=0)
    _settle(store)
    store.close()
    return store


def test_the_rule_is_age_beyond_14_days_or_no_dictionary(tmp_path: Path) -> None:
    store = _rule_store(tmp_path)
    now = 2_000_000_000.0
    fresh = now - _AGE + 60
    stale = now - _AGE - 60
    store._kind_dicts = {"wire": (1, fresh), "prompt": (2, stale)}
    assert store._dictionaries_due(now) == [("prompt", _MIN), ("rest", _BOOTSTRAP)]
    store._kind_dicts = {"wire": (1, fresh), "prompt": (2, fresh), "rest": (3, fresh)}
    assert store._dictionaries_due(now) == []
    # A failure waits an hour before the same kind is tried again.
    store._kind_dicts = {"wire": (1, stale), "prompt": (2, fresh), "rest": (3, fresh)}
    store._dict_train_failed_at = {"wire": now - 60}
    assert store._dictionaries_due(now) == []
    store._dict_train_failed_at = {"wire": now - 3_601}
    assert store._dictionaries_due(now) == [("wire", _MIN)]
    # Bodies stored as plain text are not compressed, so nothing is learned.
    store._compress_bodies = False
    assert store._dictionaries_due(now) == []


def test_a_stale_dictionary_is_relearned_only_from_1024_samples_of_its_kind(
    tmp_path: Path,
) -> None:
    path = tmp_path / "requests.db"
    RequestLogStore(path, max_rows=0).close()
    now = time.time()
    legacy = _seed_dictionary(path, "body_dictionaries", now - _AGE - _DAY)
    wire = _seed_dictionary(path, "wire_dictionaries", now - _AGE - _DAY)
    # Old traffic does not count: only the last 14 days are learned from.
    _fill(path, 1_200, now - _AGE - 2 * _DAY, prefix="old")
    _fill(path, _MIN - 1, now - _DAY)
    store = RequestLogStore(path, max_rows=0)
    _settle(store)
    store.close()
    assert _dictionaries(path) == {"body": [(legacy, None)], "wire": [(wire, None)]}

    _fill(path, _MIN, now - _DAY, prefix="more")
    store = RequestLogStore(path, max_rows=0)
    _settle(store)
    store.close()
    found = _dictionaries(path)
    assert found["body"][0] == (legacy, None)
    assert sorted(kind for _, kind in found["body"][1:]) == ["prompt", "rest"]
    assert len(found["wire"]) == 2
    # And new rows use them.
    store = RequestLogStore(path, max_rows=0)
    store.enqueue(_record(99_999, time.time(), prefix="new"))
    store.close()
    with closing(sqlite3.connect(path)) as conn:
        prompt_dict = conn.execute(
            "SELECT b.dict_id FROM request_bodies r JOIN body_blobs b"
            " ON b.sha = r.input_sha WHERE r.request_id = 'new99999'"
        ).fetchone()[0]
        (wire_value,) = conn.execute(
            "SELECT wire_body FROM request_attempts WHERE request_id = 'new99999'"
        ).fetchone()
    assert prompt_dict == max(i for i, kind in found["body"] if kind == "prompt")
    assert request_log_module._read_varint(wire_value, 1)[0] == found["wire"][-1][0]


def test_training_runs_on_its_own_thread_never_the_writer_or_the_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    seen: list[str] = []
    real = request_log_module.zstd.train_dict

    def spy(samples: Any, size: int) -> Any:
        seen.append(threading.current_thread().name)
        return real(samples, size)

    monkeypatch.setattr(request_log_module.zstd, "train_dict", spy)
    path = tmp_path / "requests.db"
    _fill(path, _BOOTSTRAP + 10, time.time() - _DAY)

    async def serve() -> list[float]:
        """An event loop that keeps ticking while the store trains."""
        store = RequestLogStore(path, max_rows=0)
        gaps: list[float] = []
        deadline = time.monotonic() + 180
        while store._trainer is None or store._trainer.is_alive():
            assert time.monotonic() < deadline, "the trainer never started or ended"
            started = time.perf_counter()
            await asyncio.sleep(0.001)
            gaps.append(time.perf_counter() - started)
        _settle(store)
        store.close()
        return gaps

    asyncio.run(serve())
    assert seen, "nothing was trained"
    assert set(seen) == {"mcc-request-log-dictionary-trainer"}
    assert "mcc-request-log-writer" not in seen
    assert threading.main_thread().name not in seen


# ---------------------------------------------------------- kept for good


def test_named_dictionaries_survive_prune_and_a_whole_table_sweep(
    tmp_path: Path,
) -> None:
    path = tmp_path / "requests.db"
    RequestLogStore(path, max_rows=0).close()
    future = time.time() + 365 * _DAY  # fresh: no refresh while this runs
    body_ids = [
        _seed_dictionary(path, "body_dictionaries", future, kind)
        for kind in (None, "prompt", "rest")
    ]
    wire_ids = [_seed_dictionary(path, "wire_dictionaries", future)]
    _fill(path, 40, time.time() - _DAY)
    before = _dictionaries(path)

    store = RequestLogStore(path, max_rows=5)
    store.close()
    store._owe_full_sweeps(*request_log_module._ORPHAN_SWEEP_TABLES)
    assert store.prune() > 0
    store._owe_full_sweeps(*request_log_module._ORPHAN_SWEEP_TABLES)
    store.prune()
    assert _dictionaries(path) == before
    assert [i for i, _ in before["body"]] == body_ids
    assert [i for i, _ in before["wire"]] == wire_ids
    # Even with every row gone, nothing names them, and they still stay.
    store = RequestLogStore(path, max_rows=1)
    store.close()
    store.prune()
    assert _dictionaries(path) == before


def test_dictionary_ids_are_never_handed_out_twice(tmp_path: Path) -> None:
    path = tmp_path / "requests.db"
    RequestLogStore(path, max_rows=0).close()
    for table in ("body_dictionaries", "wire_dictionaries"):
        first = _seed_dictionary(path, table, 1.0)
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute(f"DELETE FROM {table} WHERE id = ?", (first,))
        assert _seed_dictionary(path, table, 1.0) > first


# ------------------------------------------------- failure and interruption


def test_a_failed_training_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(samples: Any, size: int) -> Any:
        raise zstd.ZstdError("training exploded")

    monkeypatch.setattr(request_log_module.zstd, "train_dict", broken)
    path = tmp_path / "requests.db"
    _fill(path, _BOOTSTRAP + 10, time.time() - _DAY)
    before = _dictionaries(path)
    store = RequestLogStore(path, max_rows=0)
    _settle(store)
    store.enqueue(_record(77_777, time.time(), prefix="x"))
    store.close()
    assert _dictionaries(path) == before == {"body": [], "wire": []}
    assert store._kind_dicts == {}
    assert set(store._dict_train_failed_at) == {"wire", "prompt", "rest"}
    detail = store.get_request("x77777")
    assert detail is not None
    assert detail["input_text"] == _prompt(77_777)
    assert detail["route_attempts"][0]["wire_body"] == json.loads(_wire(77_777))


def test_a_store_closed_mid_training_stores_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "requests.db"
    _fill(path, _BOOTSTRAP + 10, time.time() - _DAY)
    entered = threading.Event()
    release = threading.Event()
    real = request_log_module.zstd.train_dict

    def slow(samples: Any, size: int) -> Any:
        entered.set()
        release.wait(60)
        return real(samples, size)

    monkeypatch.setattr(request_log_module.zstd, "train_dict", slow)
    before = _dictionaries(path)
    store = RequestLogStore(path, max_rows=0)
    assert entered.wait(60)
    store.close()
    release.set()
    trainer = store._trainer
    assert trainer is not None
    trainer.join(timeout=60)
    assert _dictionaries(path) == before
    # The file is free and whole: a new store opens and trains it properly.
    monkeypatch.setattr(request_log_module.zstd, "train_dict", real)
    reopened = RequestLogStore(path, max_rows=0)
    _settle(reopened)
    reopened.close()
    assert len(_dictionaries(path)["wire"]) == 1


# -------------------------------------------- old blobs and newer dictionaries


def test_rows_written_with_dictionary_n_read_after_n_plus_1_and_n_plus_2(
    tmp_path: Path,
) -> None:
    path = tmp_path / "requests.db"
    RequestLogStore(path, max_rows=0).close()
    future = time.time() + 365 * _DAY
    generations = []
    for generation in range(3):
        body = _seed_dictionary(path, "body_dictionaries", future, "prompt")
        wire = _seed_dictionary(path, "wire_dictionaries", future)
        generations.append((body, wire))
        store = RequestLogStore(path, max_rows=0)
        assert store._dictionaries_checked.wait(60)
        assert store._kind_dicts["prompt"][0] == body  # a restart picks it up
        assert store._kind_dicts["wire"][0] == wire
        store.enqueue(_record(generation, time.time(), prefix=f"g{generation}-"))
        store.close()
    store = RequestLogStore(path, max_rows=0)
    store.close()
    with closing(sqlite3.connect(path)) as conn:
        for generation, (body, wire) in enumerate(generations):
            request_id = f"g{generation}-{generation:05d}"
            named_body = conn.execute(
                "SELECT b.dict_id FROM request_bodies r JOIN body_blobs b"
                " ON b.sha = r.input_sha WHERE r.request_id = ?",
                (request_id,),
            ).fetchone()[0]
            (value,) = conn.execute(
                "SELECT wire_body FROM request_attempts WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            assert named_body == body
            assert request_log_module._read_varint(value, 1)[0] == wire
            detail = store.get_request(request_id)
            assert detail is not None
            assert detail["input_text"] == _prompt(generation)
            assert detail["route_attempts"][0]["wire_body"] == json.loads(
                _wire(generation)
            )


def test_a_kind_falls_back_to_the_dictionary_from_before_kinds(
    tmp_path: Path,
) -> None:
    path = tmp_path / "requests.db"
    RequestLogStore(path, max_rows=0).close()
    future = time.time() + 365 * _DAY
    legacy = _seed_dictionary(path, "body_dictionaries", future)
    prompt = _seed_dictionary(path, "body_dictionaries", future, "prompt")
    store = RequestLogStore(path, max_rows=0)
    store.close()
    assert store._kind_dicts["prompt"][0] == prompt
    assert store._kind_dicts["rest"][0] == legacy
    assert "wire" not in store._kind_dicts
    assert store._active_dict_id == prompt  # what an older version would pick


def test_an_older_version_decodes_bodies_written_with_kind_dictionaries(
    tmp_path: Path,
) -> None:
    """7.72.2 looked a blob's dictionary up by id alone, ignoring kinds."""
    path = tmp_path / "requests.db"
    RequestLogStore(path, max_rows=0).close()
    future = time.time() + 365 * _DAY
    _seed_dictionary(path, "body_dictionaries", future)
    _seed_dictionary(path, "body_dictionaries", future, "prompt")
    _seed_dictionary(path, "body_dictionaries", future, "rest")
    store = RequestLogStore(path, max_rows=0)
    for index in range(5):
        store.enqueue(_record(index, time.time()))
    store.close()
    with closing(sqlite3.connect(path)) as conn:
        dictionaries = {
            int(row[0]): zstd.ZstdDict(bytes(row[1]))
            for row in conn.execute("SELECT id, content FROM body_dictionaries")
        }
        rows = conn.execute(
            "SELECT r.request_id, b.dict_id, b.payload FROM request_bodies r"
            " JOIN body_blobs b ON b.sha = r.input_sha"
        ).fetchall()
    assert {row[1] for row in rows} == {2}  # the prompt dictionary, not MAX(id)
    for request_id, dict_id, payload in rows:
        raw = zstd.decompress(payload, zstd_dict=dictionaries[dict_id])
        index = int(request_id[1:])
        assert json.loads(raw)["i"] == _prompt(index)
