"""The process-wide memory of spent and blocked exits (7.81.0)."""

import pytest

from my_claude_code.core import proxy_exit_memory
from my_claude_code.core.proxy_exit_memory import (
    BLOCKED,
    EXIT_MEMORY,
    MEDIA_EXIT_MEMORY,
    SPENT,
    ExitMemory,
    add_forget_listener,
    forget_exits,
)
from my_claude_code.core.proxy_rotation import (
    PROXY_HEALTH,
    PROXY_REACHABILITY,
    reset_proxy_health,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _clean():
    reset_proxy_health()
    yield
    reset_proxy_health()


def _memory(clock: _Clock, **kwargs) -> ExitMemory:
    return ExitMemory(clock=clock, wall=lambda: 1_700_000_000.0, **kwargs)


def test_a_remembered_exit_is_recalled_until_it_expires() -> None:
    clock = _Clock()
    memory = _memory(clock)

    record = memory.remember(
        "opencode",
        "cred",
        "203.0.113.7:1080",
        state=SPENT,
        seconds=60.0,
        reason="rate_limit",
        stated_wait=60.0,
        credential_label="…blic",
    )

    assert record is not None
    assert record.until_wall == 1_700_000_060.0
    assert memory.recall("opencode", "cred", "203.0.113.7:1080") == record
    assert memory.live("opencode", "cred") == {"203.0.113.7:1080": 60.0}
    clock.now += 60.0
    assert memory.recall("opencode", "cred", "203.0.113.7:1080") is None
    assert memory.live("opencode", "cred") == {}


def test_the_key_is_provider_credential_and_exit() -> None:
    clock = _Clock()
    memory = _memory(clock)
    memory.remember("p", "a", "x:1", state=BLOCKED, seconds=5, reason="country")

    assert memory.recall("p", "b", "x:1") is None
    assert memory.recall("q", "a", "x:1") is None
    assert memory.recall("p", "a", "y:1") is None
    assert memory.recall("p", "a", "x:1") is not None


def test_no_time_is_no_record_and_clears_an_old_one() -> None:
    memory = _memory(_Clock())
    memory.remember("p", "a", "x:1", state=SPENT, seconds=30, reason="r")

    assert memory.remember("p", "a", "x:1", state=SPENT, seconds=0, reason="r") is None
    assert memory.recall("p", "a", "x:1") is None


def test_an_unknown_state_is_refused() -> None:
    with pytest.raises(ValueError, match="not an exit memory state"):
        _memory(_Clock()).remember("p", "a", "x", state="dead", seconds=1, reason="")


def test_forget_one_exit_one_provider_or_some_labels() -> None:
    memory = _memory(_Clock())
    for label in ("x:1", "y:1"):
        memory.remember("p", "a", label, state=SPENT, seconds=30, reason="r")
    memory.remember("q", "a", "x:1", state=SPENT, seconds=30, reason="r")

    assert memory.forget_exit("p", "a", "x:1") is True
    assert memory.forget_exit("p", "a", "x:1") is False
    assert memory.forget("p", labels=["nope"]) == 0
    assert memory.forget("p") == 1
    assert [record.provider_id for record in memory.records()] == ["q"]


def test_the_table_is_bounded_oldest_first() -> None:
    memory = _memory(_Clock(), bound=3)
    for index in range(5):
        memory.remember("p", "a", f"x:{index}", state=SPENT, seconds=30, reason="r")

    assert [record.exit_label for record in memory.records()] == ["x:2", "x:3", "x:4"]


def test_forget_exits_clears_both_tables_the_cooldown_and_the_dead_exits() -> None:
    EXIT_MEMORY.remember("p", "a", "x:1", state=SPENT, seconds=30, reason="r")
    MEDIA_EXIT_MEMORY.remember("p", "a", "x:1", state=BLOCKED, seconds=30, reason="c")
    EXIT_MEMORY.remember("other", "a", "x:1", state=SPENT, seconds=30, reason="r")
    PROXY_REACHABILITY.note_failure("y:1", "ConnectError")
    PROXY_HEALTH.note_failure("p", "x:1", benched_for=30.0, reason="rate_limit")
    told: list[tuple[str, frozenset[str]]] = []

    def listener(provider_id: str, labels: frozenset[str]) -> None:
        told.append((provider_id, labels))

    add_forget_listener(listener)
    add_forget_listener(listener)
    try:
        dropped = forget_exits("p", ["x:1", "y:1", "direct"])
    finally:
        proxy_exit_memory._FORGET_LISTENERS.remove(listener)

    assert dropped == 2
    assert EXIT_MEMORY.records("p") == ()
    assert MEDIA_EXIT_MEMORY.records("p") == ()
    assert len(EXIT_MEMORY.records("other")) == 1
    assert not PROXY_REACHABILITY.is_unhealthy("y:1")
    assert PROXY_HEALTH.snapshot("p", "x:1")["state"] != "cooldown"
    assert told == [("p", frozenset({"x:1", "y:1", "direct"}))]


def test_a_failing_listener_never_fails_the_forget() -> None:
    def broken(provider_id: str, labels: frozenset[str]) -> None:
        raise RuntimeError("no")

    add_forget_listener(broken)
    try:
        assert forget_exits("p", ["x:1"]) == 0
    finally:
        proxy_exit_memory._FORGET_LISTENERS.remove(broken)
