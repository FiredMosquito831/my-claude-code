"""The watchdog: what it reports, how often, and what it must never do.

The last of those is the one that matters most. Every fallback deadline on the
reporting operator's install is ``0`` -- wait for ever -- by their own
deliberate choice, and this release was built on the promise that the
instrument would not quietly become a policeman. So there is a test here that
runs a parked request with the watchdog on and with it off and asserts the
request finishes the same way, with the same bytes, either way.
"""

import asyncio
import json

import pytest

from my_claude_code.core import request_tasks
from my_claude_code.core.request_tasks import (
    PHASE_AWAITING_FIRST_BYTE,
    PHASE_BETWEEN_ATTEMPTS,
    PHASE_STREAMING_STOPPED,
    RequestProgress,
)
from my_claude_code.runtime.stall_watchdog import (
    REPEAT_MULTIPLIER,
    StallWatchdog,
)

SECRET = "sk-live-QP1SECRET-0987654321-do-not-log"


class _Clock:
    """A monotonic clock the test drives, so no test here sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


def _watchdog(tmp_path, *, stall=300.0, max_mb=5, retain=3) -> StallWatchdog:
    return StallWatchdog(
        stall_seconds=lambda: stall,
        interval_seconds=30,
        log_max_bytes=lambda: max_mb * 1024 * 1024,
        retain_files=lambda: retain,
    )


class _Progress:
    """A settable progress reader standing in for a RequestCapture."""

    def __init__(self) -> None:
        self.value = RequestProgress(
            phase=PHASE_AWAITING_FIRST_BYTE,
            attempt_index=0,
            provider="opencode",
            model_ref="opencode/muse-spark-1.3-contributor-free",
            key_label="sk-8...Kofx",
            proxy_label="173.249.24.121:1080",
        )

    def __call__(self) -> RequestProgress:
        return self.value


@pytest.fixture(autouse=True)
def _config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    from my_claude_code.config import paths

    paths.reset_config_dir_cache()
    yield
    paths.reset_config_dir_cache()


def _register(progress) -> request_tasks.RequestTaskEntry:
    entry = request_tasks.register(
        request_id="req_parked",
        endpoint="/v1/messages",
        protocol="anthropic",
        stream=True,
        harness="claude",
        requested_model="mcc/best",
        progress=progress,
    )
    assert entry is not None
    return entry


def test_a_still_request_is_reported_once_per_threshold_crossing(tmp_path) -> None:
    clock = _Clock()
    progress = _Progress()
    entry = _register(progress)
    entry.started_at_mono = clock.now
    entry.last_progress_mono = clock.now
    watchdog = _watchdog(tmp_path, stall=300.0)

    # Nothing before the threshold, however many passes there are.
    for _ in range(9):
        assert watchdog.sweep(now=clock.advance(30)) == []

    records = watchdog.sweep(now=clock.advance(30))
    assert len(records) == 1
    assert records[0]["request_id"] == "req_parked"
    assert records[0]["report_index"] == 1
    assert records[0]["threshold_s"] == 300.0

    # Silent again until five times the threshold, not every pass.
    for _ in range(39):
        assert watchdog.sweep(now=clock.advance(30)) == []
    second = watchdog.sweep(now=clock.advance(30))
    assert len(second) == 1
    assert second[0]["report_index"] == 2
    assert second[0]["threshold_s"] == 300.0 * REPEAT_MULTIPLIER
    assert second[0]["no_progress_for_s"] >= 1500


def test_progress_resets_the_clock_and_the_ladder(tmp_path) -> None:
    clock = _Clock()
    progress = _Progress()
    entry = _register(progress)
    entry.last_progress_mono = clock.now
    watchdog = _watchdog(tmp_path, stall=100.0)

    assert watchdog.sweep(now=clock.advance(50)) == []
    # One more character reached the client: that is progress.
    progress.value = RequestProgress(phase=PHASE_STREAMING_STOPPED, output_chars=7)
    assert watchdog.sweep(now=clock.advance(50)) == []
    assert watchdog.sweep(now=clock.advance(50)) == []
    records = watchdog.sweep(now=clock.advance(60))
    assert len(records) == 1
    assert records[0]["threshold_s"] == 100.0


def test_a_chunk_is_progress_even_with_the_request_log_off(tmp_path) -> None:
    """``_observe`` never runs with the log off; the chunk counter still does."""

    clock = _Clock()
    entry = _register(None)
    entry.last_progress_mono = clock.now
    watchdog = _watchdog(tmp_path, stall=100.0)

    assert watchdog.sweep(now=clock.advance(60)) == []
    entry.chunks += 1
    entry.last_chunk_mono = clock.advance(10)
    assert watchdog.sweep(now=clock.now) == []
    assert watchdog.sweep(now=clock.advance(60)) == []
    assert len(watchdog.sweep(now=clock.advance(60))) == 1


def test_zero_stops_the_reporting_and_nothing_else(tmp_path) -> None:
    clock = _Clock()
    entry = _register(_Progress())
    entry.last_progress_mono = clock.now
    watchdog = _watchdog(tmp_path, stall=0.0)
    for _ in range(50):
        assert watchdog.sweep(now=clock.advance(60)) == []
    assert request_tasks.count() == 1


@pytest.mark.parametrize(
    ("progress", "expected"),
    [
        (
            RequestProgress(phase=PHASE_AWAITING_FIRST_BYTE, provider="p", tries=0),
            PHASE_AWAITING_FIRST_BYTE,
        ),
        (
            RequestProgress(phase=PHASE_BETWEEN_ATTEMPTS, provider="p", tries=1),
            PHASE_BETWEEN_ATTEMPTS,
        ),
        (
            RequestProgress(phase=PHASE_STREAMING_STOPPED, ttft_ms=120.0),
            PHASE_STREAMING_STOPPED,
        ),
    ],
)
def test_the_record_says_which_phase_the_stall_is_in(
    tmp_path, progress, expected
) -> None:
    clock = _Clock()
    entry = _register(lambda: progress)
    entry.last_progress_mono = clock.now
    watchdog = _watchdog(tmp_path, stall=10.0)
    assert watchdog.sweep(now=clock.advance(5)) == []
    records = watchdog.sweep(now=clock.advance(20))
    assert records[0]["phase"] == expected


@pytest.mark.asyncio
async def test_the_record_carries_frames_and_never_a_secret(tmp_path) -> None:
    """The privacy contract, end to end, from a really parked task."""

    event = asyncio.Event()
    ready = asyncio.Event()
    progress = _Progress()

    async def serve(api_key: str) -> None:
        held_locally = api_key + "-copy"
        assert held_locally
        entry = _register(progress)
        entry.last_progress_mono = 0.0
        ready.set()
        await event.wait()

    task = asyncio.create_task(serve(SECRET), name="mcc-parked")
    await ready.wait()
    watchdog = _watchdog(tmp_path, stall=1.0)
    try:
        records = watchdog.sweep(now=1e6)
        assert len(records) == 1
        record = records[0]
        assert record["deepest_frame"] is not None
        assert "wait" in record["deepest_frame"]
        assert record["tasks"][0]["name"] == "mcc-parked"
        blob = json.dumps(record)
        assert SECRET not in blob
        assert "sk-live" not in blob
        # And no home directory reached the file either.
        assert "Users" not in blob
        assert "home/" not in blob
        for frame in record["tasks"][0]["frames"]:
            assert (
                frame.startswith(
                    ("my_claude_code/", "tests/", "stdlib/", "site-packages/")
                )
                or "/" not in frame
            ), frame
    finally:
        event.set()
        await task


def test_the_jsonl_is_written_rotated_and_pruned(tmp_path) -> None:
    from my_claude_code.runtime.stall_watchdog import stuck_log_path

    clock = _Clock()
    entry = _register(_Progress())
    entry.last_progress_mono = clock.now
    # A cap of one byte makes every append rotate, which is what makes the
    # bound testable without writing five megabytes.
    watchdog = StallWatchdog(
        stall_seconds=lambda: 1.0,
        interval_seconds=30,
        log_max_bytes=lambda: 1,
        retain_files=lambda: 2,
    )
    path = stuck_log_path()
    for _ in range(6):
        entry.next_threshold = 1.0
        entry.last_progress_mono = clock.now
        watchdog.sweep(now=clock.advance(10))
    assert path.is_file()
    rotated = sorted(path.parent.glob(f"{path.stem}.*{path.suffix}"))
    assert len(rotated) <= 2, rotated
    payload = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert payload["request_id"] == "req_parked"


def test_a_cap_of_zero_writes_no_file_at_all(tmp_path) -> None:
    from my_claude_code.runtime.stall_watchdog import stuck_log_path

    clock = _Clock()
    entry = _register(_Progress())
    entry.last_progress_mono = clock.now
    watchdog = _watchdog(tmp_path, stall=1.0, max_mb=0)
    watchdog.sweep(now=clock.advance(10))
    assert watchdog.records_written == 1
    assert not stuck_log_path().exists()


@pytest.mark.asyncio
async def test_the_watchdog_never_ends_cancels_or_alters_a_request() -> None:
    """The promise this whole release rests on, asserted both ways.

    The same parked request is run with the watchdog sweeping over it and with
    no watchdog at all. It must finish normally in both cases and yield byte
    identical output.
    """

    async def run(*, watched: bool) -> tuple[list[str], bool]:
        release = asyncio.Event()
        chunks: list[str] = []

        async def serve() -> bool:
            entry = _register(_Progress())
            entry.last_progress_mono = 0.0
            chunks.append("event: message_start\ndata: {}\n\n")
            await release.wait()
            chunks.append("event: message_stop\ndata: {}\n\n")
            request_tasks.unregister("req_parked")
            return True

        task = asyncio.create_task(serve())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        if watched:
            watchdog = StallWatchdog(
                stall_seconds=lambda: 0.001,
                interval_seconds=30,
                log_max_bytes=lambda: 0,
                retain_files=lambda: 0,
            )
            for _ in range(5):
                watchdog.sweep(now=1e6)
                await asyncio.sleep(0)
            assert watchdog.records_written >= 1
        release.set()
        finished = await task
        return chunks, finished

    watched_chunks, watched_ok = await run(watched=True)
    request_tasks.reset()
    plain_chunks, plain_ok = await run(watched=False)
    assert watched_ok is True
    assert plain_ok is True
    assert watched_chunks == plain_chunks
    assert not task_was_cancelled()


def task_was_cancelled() -> bool:
    """No task anywhere in these tests is cancelled by the watchdog.

    There is nothing in ``stall_watchdog`` that could cancel one -- the module
    contains no ``cancel`` call except the one that stops its own task -- and
    this is the assertion that says so if that ever changes.
    """

    import inspect

    from my_claude_code.runtime import stall_watchdog

    source = inspect.getsource(stall_watchdog)
    # The only cancel is the watchdog cancelling itself in ``close``.
    assert source.count("cancel()") == 1
    assert "_begin_finalize" not in source
    assert "wait_for" not in source
    return False


@pytest.mark.asyncio
async def test_close_settles_immediately(tmp_path) -> None:
    watchdog = _watchdog(tmp_path)
    watchdog.start()
    assert watchdog.task is not None
    await asyncio.wait_for(watchdog.close(), 2.0)
    assert watchdog.task is None
