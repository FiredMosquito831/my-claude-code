"""The walker has to find the deepest await, and it must never find a value.

``Task.get_stack()`` on its own answers with one frame for a suspended task, so
every assertion here that names a frame *below* the task coroutine is an
assertion about the ``cr_await`` / ``ag_await`` walk that this module adds. The
four shapes tested are the four the 09-16 investigation named as candidates:
a lock, an event, a socket read and a sleep -- plus nested async generators,
which is what MCC's whole streaming path is made of.
"""

import asyncio
import time

import pytest

from my_claude_code.core.async_stacks import (
    deepest_frame,
    group_by_deepest_frame,
    package_relative,
    task_frames,
)

SECRET = "sk-test-ABCDEF-do-not-log-1234567890"


async def _settle() -> None:
    """Let a freshly created task reach its first suspension point."""

    for _ in range(6):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_a_task_parked_on_a_lock_names_acquire_not_only_itself() -> None:
    lock = asyncio.Lock()

    async def park() -> None:
        async with lock:
            await asyncio.sleep(3600)

    holder = asyncio.create_task(park(), name="holder")
    await _settle()
    waiter = asyncio.create_task(park(), name="waiter")
    await _settle()
    try:
        frames = task_frames(waiter)
        joined = "\n".join(frames)
        assert "park" in joined
        # The point of the module: without the ``cr_await`` walk this is the
        # only frame there is, and it says nothing about a lock.
        assert "acquire" in deepest_frame(waiter), frames
        assert "stdlib/asyncio/locks.py" in deepest_frame(waiter), frames
        # And the holder is somewhere else entirely, which is the distinction
        # the 09-16 report asked for: "8 tasks on Lock.acquire and 1 elsewhere".
        assert "acquire" not in deepest_frame(holder), task_frames(holder)
    finally:
        for task in (holder, waiter):
            task.cancel()
        await asyncio.gather(holder, waiter, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_task_parked_on_an_event_names_event_wait() -> None:
    event = asyncio.Event()

    async def park() -> None:
        await event.wait()

    task = asyncio.create_task(park(), name="event")
    await _settle()
    try:
        assert "wait" in deepest_frame(task)
        assert "stdlib/asyncio/locks.py" in deepest_frame(task)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_task_parked_on_a_sleep_names_sleep() -> None:
    async def park() -> None:
        await asyncio.sleep(3600)

    task = asyncio.create_task(park(), name="sleeper")
    await _settle()
    try:
        assert "sleep" in deepest_frame(task)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_task_parked_on_a_socket_read_names_the_read() -> None:
    """The shape of an upstream that accepted and then said nothing."""

    # The handler holds its connection open until the test releases it, and
    # the test releases it in ``finally`` -- ``wait_closed`` waits for live
    # handlers, so a handler that slept for an hour would hang the suite.
    release = asyncio.Event()

    async def handler(reader, writer) -> None:
        # Accept and say nothing at all. This is bucket (a) of the
        # investigation: "upstream silent after the HTTP head".
        await release.wait()
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    async def park() -> None:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            await reader.read(4096)
        finally:
            writer.close()

    task = asyncio.create_task(park(), name="reader")
    await _settle()
    await asyncio.sleep(0.05)
    try:
        frames = task_frames(task)
        joined = "\n".join(frames)
        assert "park" in joined, frames
        assert "read" in joined, frames
        assert "stdlib/asyncio" in joined, frames
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        release.set()
        server.close()
        await asyncio.wait_for(server.wait_closed(), 5.0)


@pytest.mark.asyncio
async def test_nested_async_generators_do_not_hide_the_deepest_frame() -> None:
    """``async_generator_asend`` breaks the chain; ``gc`` repairs it.

    Without the bridge the deepest frame is the *outermost* consumer, which is
    exactly wrong for MCC, whose streaming path is generators all the way down.
    """

    event = asyncio.Event()

    async def inner():
        yield "a"
        await event.wait()
        yield "b"

    async def outer(source):
        async for item in source:
            yield item

    async def consume() -> None:
        async for _ in outer(inner()):
            pass

    task = asyncio.create_task(consume(), name="agen")
    await _settle()
    try:
        frames = task_frames(task)
        joined = "\n".join(frames)
        assert "consume" in joined, frames
        assert "outer" in joined, frames
        assert "inner" in joined, frames
        assert "wait" in deepest_frame(task), frames
    finally:
        event.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_no_local_and_no_argument_value_ever_reaches_a_frame() -> None:
    """The privacy contract, asserted against a planted secret.

    The value is both a local *and* an argument of the parked coroutine, and
    it must appear in no frame, no name and no path.
    """

    event = asyncio.Event()

    async def park(api_key: str) -> None:
        held_locally = api_key + "-local"
        assert held_locally
        await event.wait()

    task = asyncio.create_task(park(SECRET), name="secretive")
    await _settle()
    try:
        rendered = "\n".join(task_frames(task))
        assert SECRET not in rendered
        assert "sk-" not in rendered
        summary = await group_by_deepest_frame([task])
        assert SECRET not in repr(summary)
    finally:
        event.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_nine_tasks_on_one_await_group_under_one_frame() -> None:
    """The summary that would have settled 09-16 in a single look."""

    lock = asyncio.Lock()

    async def park() -> None:
        async with lock:
            await asyncio.sleep(3600)

    tasks = [asyncio.create_task(park(), name=f"park-{i}") for i in range(10)]
    await _settle()
    await asyncio.sleep(0.05)
    try:
        summary = await group_by_deepest_frame(tasks)
        groups = {row["frame"]: row["tasks"] for row in summary["groups"]}
        biggest = max(groups.items(), key=lambda item: item[1])
        assert biggest[1] == 9, summary
        assert "acquire" in biggest[0], summary
        assert summary["total_tasks"] == 10
        assert summary["truncated"] is False
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_the_summary_says_when_it_truncated() -> None:
    async def park() -> None:
        await asyncio.sleep(3600)

    tasks = [asyncio.create_task(park()) for _ in range(20)]
    await _settle()
    try:
        summary = await group_by_deepest_frame(tasks, task_limit=5)
        assert summary["examined"] == 5
        assert summary["total_tasks"] == 20
        assert summary["truncated"] is True
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            r"C:\Users\someone\proj\.venv\Lib\site-packages\httpcore\_async\socks_proxy.py",
            "site-packages/httpcore/_async/socks_proxy.py",
        ),
        (
            "/home/someone/proj/src/my_claude_code/api/response_streams.py",
            "my_claude_code/api/response_streams.py",
        ),
        ("<frozen importlib._bootstrap>", "<frozen importlib._bootstrap>"),
        ("", "<unknown>"),
        # No anchor at all: the basename, never a guess that could carry a
        # home directory into a log somebody pastes into an issue.
        (r"D:\scratch\rig\fake_upstream.py", "fake_upstream.py"),
    ],
)
def test_paths_are_package_relative_and_never_name_a_person(
    raw: str, expected: str
) -> None:
    assert package_relative(raw) == expected


def test_a_real_module_path_is_rendered_without_a_home_directory() -> None:
    import my_claude_code.core.async_stacks as module

    rendered = package_relative(module.__file__ or "")
    assert rendered == "my_claude_code/core/async_stacks.py"


@pytest.mark.asyncio
async def test_building_a_stack_for_five_hundred_tasks_is_cheap() -> None:
    """A synchronous walk over the whole loop must not become the stall.

    The cap exists because this work is synchronous; the measurement is what
    justifies the cap's default rather than a guess.
    """

    event = asyncio.Event()

    async def park() -> None:
        await event.wait()

    tasks = [asyncio.create_task(park()) for _ in range(500)]
    await _settle()
    try:
        start = time.perf_counter()
        summary = await group_by_deepest_frame(tasks)
        elapsed = time.perf_counter() - start
        assert summary["examined"] == 500
        # Two orders of magnitude of headroom over what it measures here
        # (~5 ms), so a loaded CI runner cannot make this flaky.
        assert elapsed < 1.0, elapsed
    finally:
        event.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
