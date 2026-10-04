"""The learned-facts flush and the event loop that records facts (7.72.1).

The debounced flush writes on a worker thread, and the loop that records and
forgets facts keeps running while it does. Before 7.72.1 the worker iterated
the live dictionary, so a fact learned mid-flush raised "dictionary changed
size during iteration" inside a task nobody awaited -- and the flush had
already cleared the dirty flag, so that write was simply never made. A write
that failed on disk cleared it the same way.

Each case drives the interleaving it is about on purpose rather than hoping a
sleep lines it up, except the last one, which is the bounded stress run the
deterministic cases are the explanation for.
"""

import asyncio
import json
import sys
import threading
import time
from pathlib import Path

import pytest
from loguru import logger

from my_claude_code.providers.recovery import store as store_module
from my_claude_code.providers.recovery.facts import FACT_OUTPUT_CAP, LearnedFact
from my_claude_code.providers.recovery.store import LearnedFactStore

STORE_LOGGER = "my_claude_code.providers.recovery.store"


def _models_on_disk(path: Path) -> list[str]:
    document = json.loads(path.read_text(encoding="utf-8"))
    return sorted(row["model_id"] for row in document["facts"])


async def _settled(store: LearnedFactStore, seconds: float = 10.0) -> asyncio.Task:
    """The store's debounce task once it has finished, whichever way it did."""

    deadline = time.monotonic() + seconds
    while True:
        task = store._flush_task
        if task is not None and task.done():
            return task
        assert time.monotonic() < deadline, "the flush never finished"
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_a_fact_learned_mid_flush_breaks_nothing_and_is_written_next(
    tmp_path, monkeypatch
) -> None:
    """The loop records a fact while a worker is half-way through rendering.

    Injected, not timed: the first row rendered off the loop thread hands the
    loop one ``record`` and waits for it to finish before rendering the rest.
    """

    path = tmp_path / "learned_facts.json"
    store = LearnedFactStore(path=path, flush_debounce_seconds=0.01)
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    learned = threading.Event()
    fired: list[int] = []
    original_as_row = LearnedFact.as_row

    def learn_on_the_loop() -> None:
        store.record("custom_x", "m3", FACT_OUTPUT_CAP, 1024)
        learned.set()

    def as_row_while_the_loop_learns(self: LearnedFact) -> dict:
        if not fired and threading.get_ident() != loop_thread:
            fired.append(threading.get_ident())
            loop.call_soon_threadsafe(learn_on_the_loop)
            assert learned.wait(5.0), "the loop never ran the injected record"
        return original_as_row(self)

    written: list[list[str]] = []
    original_write = store_module.write_json_document_atomically_if_changed

    def recording_write(target: Path, document: dict) -> bool:
        written.append(sorted(row["model_id"] for row in document["facts"]))
        return original_write(target, document)

    monkeypatch.setattr(LearnedFact, "as_row", as_row_while_the_loop_learns)
    monkeypatch.setattr(
        store_module, "write_json_document_atomically_if_changed", recording_write
    )

    store.record("custom_x", "m1", FACT_OUTPUT_CAP, 4096)
    store.record("custom_x", "m2", FACT_OUTPUT_CAP, 2048)
    task = await _settled(store)

    assert fired, "the hook never ran off the loop: the flush did not use a worker"
    assert task.exception() is None
    # The first write is exactly the snapshot the loop took -- not a mix of
    # before and after -- and the fact learned during it is the next write.
    assert written == [["m1", "m2"], ["m1", "m2", "m3"]]
    assert _models_on_disk(path) == ["m1", "m2", "m3"]


def test_a_failed_write_keeps_the_fact_for_the_next_flush(
    tmp_path, monkeypatch, caplog
) -> None:
    path = tmp_path / "learned_facts.json"
    store = LearnedFactStore(path=path, flush_debounce_seconds=0.0)
    store.record("custom_x", "m1", FACT_OUTPUT_CAP, 4096)
    calls: list[Path] = []
    original_write = store_module.write_json_document_atomically_if_changed

    def failing_once(target: Path, document: dict) -> bool:
        calls.append(target)
        if len(calls) == 1:
            raise PermissionError(13, "simulated: another process holds the file")
        return original_write(target, document)

    monkeypatch.setattr(
        store_module, "write_json_document_atomically_if_changed", failing_once
    )

    assert store.flush() is False
    assert not path.exists()
    # Still owed: the next flush writes the same fact rather than finding
    # nothing to do.
    assert store.flush() is True
    assert _models_on_disk(path) == ["m1"]
    warnings = [
        record
        for record in caplog.records
        if "LEARNED FACTS: cannot write" in record.getMessage()
    ]
    assert len(warnings) == 1, "one failed write, one log line"


@pytest.mark.asyncio
async def test_a_failed_debounced_write_ends_quietly_and_is_written_at_close(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "learned_facts.json"
    store = LearnedFactStore(path=path, flush_debounce_seconds=0.01)
    calls: list[Path] = []
    original_write = store_module.write_json_document_atomically_if_changed

    def failing_once(target: Path, document: dict) -> bool:
        calls.append(target)
        if len(calls) == 1:
            raise PermissionError(13, "simulated: another process holds the file")
        return original_write(target, document)

    monkeypatch.setattr(
        store_module, "write_json_document_atomically_if_changed", failing_once
    )

    store.record("custom_x", "m1", FACT_OUTPUT_CAP, 4096)
    task = await _settled(store)

    assert task.exception() is None
    assert not path.exists()
    await store.close()
    assert _models_on_disk(path) == ["m1"]


@pytest.mark.asyncio
async def test_two_seconds_of_learning_beside_flushing_never_breaks_a_flush(
    tmp_path,
) -> None:
    """The stress run: the loop churns facts while flushes run back to back.

    Bounded at two seconds and stopped at the first broken flush. The switch
    interval is lowered for the run so the worker and the loop trade the
    interpreter often, which is what makes two seconds enough.
    """

    path = tmp_path / "learned_facts.json"
    store = LearnedFactStore(path=path, flush_debounce_seconds=0.001)
    seeds = [f"seed{index:04d}" for index in range(2000)]
    for model in seeds:
        store.record("custom_x", model, FACT_OUTPUT_CAP, 4096)

    broken: list[BaseException] = []
    churned = 0
    previous_interval = sys.getswitchinterval()
    logger.disable(STORE_LOGGER)
    sys.setswitchinterval(1e-5)
    try:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            task = store._flush_task
            if task is not None and task.done() and not task.cancelled():
                exc = task.exception()
                if exc is not None:
                    broken.append(exc)
                    break
            # A fact in and out again: the dictionary changes size twice.
            store.record("custom_x", f"churn{churned}", FACT_OUTPUT_CAP, 1)
            store.forget("custom_x", f"churn{churned}")
            churned += 1
            await asyncio.sleep(0)
    finally:
        sys.setswitchinterval(previous_interval)
        logger.enable(STORE_LOGGER)

    await store.close()
    assert broken == [], f"a flush broke after {churned} churns: {broken[0]!r}"
    assert churned > 0
    assert _models_on_disk(path) == seeds
