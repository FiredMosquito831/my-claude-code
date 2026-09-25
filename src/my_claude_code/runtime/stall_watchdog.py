"""The task that writes down where a request is stuck, before the log rotates.

On 2026-09-16 nine parallel requests parked for 47 minutes. Every candidate
mechanism that could be excluded from the database *was* excluded; the one
artefact that would have named the ``await`` holding them -- the server log for
that hour -- had rotated away before anyone looked, and the event has not
recurred in the fourteen days since. The operator's decision was to stop
guessing and build the instrument: capture the evidence the next time, rather
than ship a fix for a mechanism nobody has proven.

So this is a witness, not a policeman. **It never ends, cancels, retries or
alters a request.** It holds no reference that could keep one alive, it takes
no lock any request path takes, and the only thing it does on finding a stall
is write. Every fallback deadline on this install is ``0`` -- wait forever --
by the operator's deliberate choice, and nothing here second-guesses that.

What one pass costs when nothing is wrong: a dictionary snapshot, one cheap
counter read per in-flight request, and a tuple comparison. Stacks are built
only for a request that has already crossed the threshold. Measured overhead
with 100 in-flight requests is in ``tests/runtime/test_stall_watchdog_cost.py``.

**The rate limit.** A request that is stuck stays stuck, and a watchdog that
said so every thirty seconds for forty-seven minutes would produce ninety-four
identical records and teach the reader to ignore the file. One record is
written when the request first crosses ``REQUEST_WATCHDOG_STALL_SECONDS``, and
the next threshold is five times the last -- 300 s, 1500 s, 7500 s on the
shipped default, so the 09-16 park would have produced exactly two records with
a growing stack age between them. Any progress at all resets the ladder to the
first rung, because a request that moved and stopped again is a new fact.
"""

import asyncio
import json
import os
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from my_claude_code.config.paths import LOGS_DIRNAME, config_dir_path
from my_claude_code.core import request_tasks
from my_claude_code.core.async_stacks import DEFAULT_FRAME_LIMIT
from my_claude_code.core.stuck_requests import describe_entry

#: The file the records land in, beside ``server.log`` in the same directory.
STUCK_LOG_FILENAME = "stuck-requests.jsonl"

#: Each report threshold is this many times the last. Five keeps a 47-minute
#: park to two records and a day-long one to four.
REPEAT_MULTIPLIER = 5.0

#: A directory holding more rotated copies than this is more likely the wrong
#: directory than a retention problem, so the sweep refuses rather than guesses.
#: The same guard, and the same reasoning, as ``config/logging_config.py``.
_MAX_ROTATED_FILES = 100_000


def stuck_log_path() -> Path:
    """Where the records go. Resolved per call: the config dir can move."""

    return config_dir_path() / LOGS_DIRNAME / STUCK_LOG_FILENAME


class StallWatchdog:
    """One task, one sleep, and a tuple comparison per in-flight request."""

    def __init__(
        self,
        *,
        stall_seconds: Callable[[], float],
        interval_seconds: float,
        log_max_bytes: Callable[[], int],
        retain_files: Callable[[], int],
        frame_limit: int = DEFAULT_FRAME_LIMIT,
    ) -> None:
        # The threshold and the file cap are read per pass: an operator
        # reproducing a stall can drop the threshold to 5 s and see the next
        # pass use it. The interval is the sleep itself; a saved value reaches
        # it through ``set_interval`` and applies from the next pass.
        self._stall_seconds = stall_seconds
        self._log_max_bytes = log_max_bytes
        self._retain_files = retain_files
        self._interval = max(1.0, float(interval_seconds))
        self._frame_limit = frame_limit
        self._task: asyncio.Task[None] | None = None
        #: Records written by this process. Reported by the endpoint so a
        #: reader can tell "nothing was stuck" from "nothing was watching".
        self.records_written = 0

    @property
    def task(self) -> asyncio.Task[None] | None:
        return self._task

    @property
    def interval_seconds(self) -> float:
        return self._interval

    def set_interval(self, interval_seconds: float) -> None:
        """Sweep at a new interval from the next pass on.

        ``_run`` reads ``_interval`` for every sleep, so a saved
        ``REQUEST_WATCHDOG_INTERVAL_SECONDS`` needs no new task: the pass
        already asleep wakes on the old interval and every later one uses the
        new value. Nothing is cancelled and ``records_written`` is kept.
        """

        self._interval = max(1.0, float(interval_seconds))

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="mcc-stall-watchdog")

    async def close(self) -> None:
        """Stop, inside the 6.41.0 stop deadline, and leave nothing behind."""

        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                raise
            try:
                self.sweep()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A watchdog that can take the server down with it is worse
                # than no watchdog. One line, and the next pass tries again.
                logger.debug("WATCHDOG: a pass failed and was skipped: {}", exc)

    def sweep(self, *, now: float | None = None) -> list[dict[str, Any]]:
        """One pass. Returns the records it wrote, which is usually none."""

        moment = time.monotonic() if now is None else now
        written: list[dict[str, Any]] = []
        for entry in request_tasks.snapshot():
            record = self._examine(entry, moment)
            if record is not None:
                written.append(record)
        return written

    def _examine(
        self, entry: request_tasks.RequestTaskEntry, moment: float
    ) -> dict[str, Any] | None:
        progress = entry.read_progress()
        signature = None if progress is None else progress.signature()
        # A chunk delivered to the client is progress even when the request
        # log -- and therefore the capture's own observer -- is switched off,
        # which is why it is counted on the entry rather than in the capture.
        marker = (signature, entry.chunks)
        first_look = entry.last_signature is None
        stall = max(0.0, float(self._stall_seconds()))
        if entry.last_signature != marker:
            entry.last_signature = marker
            entry.reports = 0
            entry.next_threshold = stall
            if not first_look:
                # The first pass over a request is not evidence that it moved:
                # its clock has been running since it registered, and resetting
                # it here would make every request look fresh once per pass.
                entry.last_progress_mono = moment
                return None
        still_for = moment - max(entry.last_progress_mono, entry.last_chunk_mono or 0.0)
        if stall <= 0:
            return None
        threshold = entry.next_threshold or stall
        if still_for < threshold:
            return None
        record = describe_entry(
            entry,
            now=moment,
            frame_limit=self._frame_limit,
            no_progress_for=still_for,
        )
        entry.reports += 1
        entry.next_threshold = threshold * REPEAT_MULTIPLIER
        record["report_index"] = entry.reports
        record["threshold_s"] = threshold
        self._emit(record)
        return record

    def _emit(self, record: dict[str, Any]) -> None:
        self.records_written += 1
        deepest = record.get("deepest_frame") or "no frame could be read"
        # One WARNING line, naming the two things a reader needs before they
        # open anything: which request, and what it is waiting on.
        logger.warning(
            "WATCHDOG: request {} has made no progress for {:.0f}s ({}); "
            "deepest await {}. Full stack in logs/{}.",
            record.get("request_id"),
            float(record.get("no_progress_for_s") or 0.0),
            record.get("phase"),
            deepest,
            STUCK_LOG_FILENAME,
        )
        self._append(record)

    def _append(self, record: dict[str, Any]) -> None:
        max_bytes = max(0, int(self._log_max_bytes()))
        if max_bytes <= 0:
            return
        path = stuck_log_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._rotate_if_large(path, max_bytes)
            line = json.dumps(record, separators=(",", ":"), default=str)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError as exc:
            logger.debug("WATCHDOG: could not write {}: {}", path, exc)

    def _rotate_if_large(self, path: Path, max_bytes: int) -> None:
        try:
            if not path.is_file() or path.stat().st_size < max_bytes:
                return
        except OSError:
            return
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")
        target = path.with_name(f"{path.stem}.{stamp}{path.suffix}")
        counter = 1
        while target.exists():
            target = path.with_name(f"{path.stem}.{stamp}-{counter}{path.suffix}")
            counter += 1
        try:
            os.replace(path, target)
        except OSError:
            return
        self._sweep(path)

    def _sweep(self, path: Path) -> None:
        retain = max(0, int(self._retain_files()))
        if retain <= 0:
            return
        try:
            rotated = sorted(
                path.parent.glob(f"{path.stem}.*{path.suffix}"),
                key=lambda candidate: candidate.stat().st_mtime,
            )
        except OSError:
            return
        if len(rotated) >= _MAX_ROTATED_FILES:
            return
        excess = len(rotated) - retain
        for old in rotated[: max(0, excess)]:
            with suppress(OSError):
                old.unlink()


__all__ = ["REPEAT_MULTIPLIER", "STUCK_LOG_FILENAME", "StallWatchdog", "stuck_log_path"]
