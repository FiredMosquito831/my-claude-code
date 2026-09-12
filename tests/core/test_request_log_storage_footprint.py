"""What the log costs, which was invisible until it was four and a half gigabytes.

The user's decision was explicit: **no cap, no prune, no lowering of the
700,000-row default** -- show the size instead. So this is a readout and only a
readout, and the tests hold it to the two rules every readout in this codebase
follows: NULL means "not measured", never zero; and the number describes the
whole thing it claims to describe.
"""

import sqlite3
import time
from typing import Any

from my_claude_code.core.request_log import RequestLogStore, RequestRecord


def _record(request_id: str, **overrides: Any) -> RequestRecord:
    defaults: dict[str, Any] = {
        "id": request_id,
        "endpoint": "/v1/messages",
        "protocol": "anthropic",
        "provider": "nvidia_nim",
        "resolved_model": "test-model",
        "status": "success",
        "tokens_in": 10,
    }
    defaults.update(overrides)
    return RequestRecord(**defaults)


def test_the_footprint_counts_the_rows_that_are_kept(tmp_path) -> None:
    store = RequestLogStore(tmp_path / "requests.db", max_rows=10_000)
    try:
        for index in range(7):
            store.enqueue(_record(f"r{index}", ts_epoch=time.time() - index))
        store.close()

        footprint = store.storage_footprint()

        assert footprint["rows"] == 7
        assert footprint["path"].endswith("requests.db")
    finally:
        store.close()


def test_the_footprint_adds_up_every_file_the_log_occupies(tmp_path) -> None:
    """The write-ahead log is tens of megabytes on a busy install."""

    store = RequestLogStore(tmp_path / "requests.db", max_rows=10_000)
    try:
        store.enqueue(_record("r0"))
        store.close()

        footprint = store.storage_footprint()

        assert set(footprint["bytes_by_file"]) == {"database", "wal", "shm"}
        assert footprint["bytes"] == sum(footprint["bytes_by_file"].values())
        assert footprint["bytes_by_file"]["database"] > 0
        assert footprint["bytes"] >= footprint["bytes_by_file"]["database"]
    finally:
        store.close()


def test_an_absent_sidecar_contributes_nothing_rather_than_failing(tmp_path) -> None:
    store = RequestLogStore(tmp_path / "requests.db", max_rows=10)
    try:
        store.close()
        for suffix in ("-wal", "-shm"):
            sidecar = store.db_path.with_name(store.db_path.name + suffix)
            sidecar.unlink(missing_ok=True)

        footprint = store.storage_footprint()

        assert footprint["bytes_by_file"]["wal"] == 0
        assert footprint["bytes_by_file"]["shm"] == 0
        assert footprint["bytes"] > 0
    finally:
        store.close()


def test_a_log_that_cannot_be_counted_reports_null_not_zero(tmp_path) -> None:
    """NULL means not measured. Zero rows is a different, wrong claim."""

    store = RequestLogStore(tmp_path / "requests.db", max_rows=10)
    try:
        store.enqueue(_record("r0"))
        store.close()
        with sqlite3.connect(store.db_path) as conn:
            conn.execute("DROP TABLE requests")

        footprint = store.storage_footprint()

        assert footprint["rows"] is None
        assert footprint["bytes"] > 0
    finally:
        store.close()


def test_the_footprint_grows_with_the_log(tmp_path) -> None:
    store = RequestLogStore(tmp_path / "requests.db", max_rows=10_000)
    try:
        store.enqueue(_record("r0"))
        store.close()
        before = store.storage_footprint()

        again = RequestLogStore(tmp_path / "requests.db", max_rows=10_000)
        for index in range(200):
            again.enqueue(
                _record(f"big{index}", input_text="x" * 2_000, output_text="y" * 2_000)
            )
        again.close()

        after = again.storage_footprint()
        assert after["rows"] == before["rows"] + 200
        assert after["bytes"] > before["bytes"]
    finally:
        store.close()


def test_nothing_here_deletes_a_row(tmp_path) -> None:
    """A readout, not a pruner. The decision on record is: no capping."""

    store = RequestLogStore(tmp_path / "requests.db", max_rows=10_000)
    try:
        for index in range(20):
            store.enqueue(_record(f"r{index}"))
        store.close()

        for _ in range(5):
            store.storage_footprint()

        assert store.storage_footprint()["rows"] == 20
    finally:
        store.close()
