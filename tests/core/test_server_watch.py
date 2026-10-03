"""How ``mcc-desktop`` decides the server is dead, and how often it looks (7.70.0).

Dead is decided by the operating system, never by a timeout (user decision R4):
a process that still holds its port is slow, and slow is never announced. A
dead server is announced once per outage. The probe runs every 30 s while the
server answers and every 5 s once it does not, exactly as fast as before.
"""

import pytest

from my_claude_code.core.server_watch import (
    DEAD_VERDICTS,
    NotificationRoute,
    PortFacts,
    ServerWatch,
    Verdict,
    WatchStep,
    classify_outage,
    dead_message,
    notification_route,
    recovered_message,
)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _watch(clock: Clock, *, threshold: int = 3) -> ServerWatch:
    return ServerWatch(
        threshold=threshold,
        healthy_interval=30.0,
        failing_interval=5.0,
        confirm_seconds=30.0,
        recheck_seconds=30.0,
        clock=clock,
        wall=lambda: 1_790_000_000.0,
    )


GONE = PortFacts(port_free=True, known_pid=4242, known_alive=False)
LOST = PortFacts(port_free=True, known_pid=4242, known_alive=True)
NOBODY = PortFacts(port_free=True)
SLOW = PortFacts(port_free=False, holder_pid=4242, known_pid=4242)
SLOW_OTHER_MCC = PortFacts(
    port_free=False, holder_pid=7, holder_is_mcc=True, known_pid=4242
)
UNKNOWN = PortFacts(
    port_free=False, holder_pid=None, holder_is_mcc=None, known_pid=4242
)
FOREIGN = PortFacts(
    port_free=False, holder_pid=99, holder_is_mcc=False, holder_image="nginx.exe"
)
UPDATING = PortFacts(port_free=True, known_pid=4242, known_alive=False, updating=True)


@pytest.mark.parametrize(
    ("facts", "verdict"),
    [
        (GONE, Verdict.PROCESS_GONE),
        (LOST, Verdict.LISTENER_LOST),
        # Unknown liveness counts as alive: never "gone" on a failed lookup.
        (
            PortFacts(port_free=True, known_pid=4242, known_alive=None),
            Verdict.LISTENER_LOST,
        ),
        (NOBODY, Verdict.NOT_LISTENING),
        (SLOW, Verdict.SLOW),
        (SLOW_OTHER_MCC, Verdict.SLOW),
        (UNKNOWN, Verdict.UNKNOWN),
        (FOREIGN, Verdict.FOREIGN),
        (UPDATING, Verdict.UPDATING),
    ],
)
def test_the_verdict_comes_from_the_os_facts(facts, verdict) -> None:
    assert classify_outage(facts) is verdict


def test_a_process_that_holds_its_port_is_never_dead() -> None:
    for facts in (SLOW, SLOW_OTHER_MCC, UNKNOWN, UPDATING):
        assert classify_outage(facts) not in DEAD_VERDICTS


# ------------------------------------------------------------------ cadence


def test_the_healthy_cadence_is_thirty_seconds_and_the_failing_one_five() -> None:
    clock = Clock()
    watch = _watch(clock)

    # Unknown (before the first probe) is fast.
    assert watch.next_interval() == 5.0
    watch.record_probe(True)
    assert watch.next_interval() == 30.0
    watch.record_probe(False)
    assert watch.next_interval() == 5.0
    watch.record_probe(True)
    assert watch.next_interval() == 30.0


def test_the_fast_cadence_is_never_slower_than_the_healthy_one() -> None:
    watch = ServerWatch(
        threshold=3,
        healthy_interval=2.0,
        failing_interval=5.0,
        confirm_seconds=30.0,
        recheck_seconds=30.0,
    )
    watch.record_probe(False)
    assert watch.next_interval() == 2.0


# -------------------------------------------------------- once per outage


def _fail_until_check(watch: ServerWatch, clock: Clock, *, step: float = 5.0) -> int:
    probes = 0
    while True:
        clock.now += step
        probes += 1
        if watch.record_probe(False) is WatchStep.CHECK:
            return probes
        assert probes < 1000


def test_a_gone_process_is_announced_once_however_long_it_stays_gone() -> None:
    clock = Clock()
    watch = _watch(clock)
    watch.record_probe(True)
    announced: list[Verdict] = []

    for _ in range(500):
        clock.now += 5.0
        if watch.record_probe(False) is WatchStep.CHECK:
            verdict = watch.record_facts(GONE)
            if verdict is not None:
                announced.append(verdict)

    assert announced == [Verdict.PROCESS_GONE]


def test_the_os_is_only_asked_after_the_threshold() -> None:
    clock = Clock()
    watch = _watch(clock)

    assert _fail_until_check(watch, clock) == 3


def test_a_slow_server_is_never_announced_and_the_os_is_asked_sparingly() -> None:
    """A thousand failed probes of a server that holds its port: silence."""

    clock = Clock()
    watch = _watch(clock)
    checks = 0
    for _ in range(1000):
        clock.now += 5.0
        if watch.record_probe(False) is WatchStep.CHECK:
            checks += 1
            assert watch.record_facts(SLOW) is None
    # 5000 s of failing probes, the OS asked at most once per 30 s.
    assert 0 < checks <= 5000 / 30 + 1


def test_an_unidentified_holder_is_patience_not_death() -> None:
    clock = Clock()
    watch = _watch(clock)
    for _ in range(200):
        clock.now += 5.0
        if watch.record_probe(False) is WatchStep.CHECK:
            assert watch.record_facts(UNKNOWN) is None


def test_an_update_is_never_announced_as_a_death() -> None:
    clock = Clock()
    watch = _watch(clock)
    for _ in range(200):
        clock.now += 5.0
        if watch.record_probe(False) is WatchStep.CHECK:
            assert watch.record_facts(UPDATING) is None


def test_a_lost_listener_needs_three_looks_over_thirty_seconds() -> None:
    clock = Clock()
    watch = _watch(clock)
    _fail_until_check(watch, clock)
    first = clock.now
    assert watch.record_facts(LOST) is None
    announced_at = None
    for _ in range(20):
        clock.now += 5.0
        assert watch.record_probe(False) is WatchStep.CHECK
        verdict = watch.record_facts(LOST)
        if verdict is not None:
            announced_at = clock.now
            assert verdict is Verdict.LISTENER_LOST
            break
    assert announced_at is not None
    assert announced_at - first >= 30.0
    assert announced_at - first < 35.0


def test_a_reload_that_rebinds_inside_the_confirmation_is_never_announced() -> None:
    """An in-process reload closes and re-binds its own listener."""

    clock = Clock()
    watch = _watch(clock)
    _fail_until_check(watch, clock)
    assert watch.record_facts(LOST) is None
    for _ in range(3):
        clock.now += 5.0
        assert watch.record_probe(False) is WatchStep.CHECK
        assert watch.record_facts(LOST) is None
    clock.now += 5.0
    assert watch.record_probe(True) is WatchStep.NOTHING


def test_a_slow_look_resets_a_pending_confirmation() -> None:
    clock = Clock()
    watch = _watch(clock)
    _fail_until_check(watch, clock)
    assert watch.record_facts(LOST) is None
    clock.now += 5.0
    watch.record_probe(False)
    assert watch.record_facts(SLOW) is None
    # The next confirmation starts over (and waits for the slow re-check).
    clock.now += 31.0
    assert watch.record_probe(False) is WatchStep.CHECK
    assert watch.record_facts(LOST) is None


def test_recovery_is_said_only_after_an_announced_death() -> None:
    clock = Clock()
    watch = _watch(clock)
    _fail_until_check(watch, clock)
    assert watch.record_facts(FOREIGN) is Verdict.FOREIGN
    clock.now += 5.0
    assert watch.record_probe(True) is WatchStep.RECOVERED
    # A second, unannounced blip recovers in silence.
    _fail_until_check(watch, clock)
    assert watch.record_facts(SLOW) is None
    assert watch.record_probe(True) is WatchStep.NOTHING


def test_the_known_pid_is_the_last_one_named() -> None:
    watch = _watch(Clock())
    assert watch.known_pid is None
    watch.note_pid(11)
    watch.note_pid(None)
    assert watch.known_pid == 11
    watch.note_pid(12)
    assert watch.known_pid == 12


# --------------------------------------------------------------------- words


def test_each_death_says_what_happened() -> None:
    since = 1_790_000_000.0
    gone = dead_message(Verdict.PROCESS_GONE, GONE, port=8082, since=since)
    lost = dead_message(Verdict.LISTENER_LOST, LOST, port=8082, since=since)
    nobody = dead_message(Verdict.NOT_LISTENING, NOBODY, port=8082, since=since)
    foreign = dead_message(Verdict.FOREIGN, FOREIGN, port=8082, since=since)
    for text in (gone, lost, nobody, foreign):
        assert text.startswith(
            "The My Claude Code server on port 8082 is not answering"
        )
        assert "(since " in text
    assert "process 4242 has exited" in gone
    assert "process 4242 is still running but no longer holds the port" in lost
    assert "nothing is listening on the port" in nobody
    assert "nginx.exe (pid 99), which is not My Claude Code" in foreign
    assert recovered_message(port=8082) == (
        "The My Claude Code server on port 8082 is answering again."
    )


# ------------------------------------------------------------------- routing


@pytest.mark.parametrize(
    ("tray", "app", "console", "route"),
    [
        (True, True, True, NotificationRoute.TRAY),
        (True, False, False, NotificationRoute.TRAY),
        (False, True, True, NotificationRoute.APP),
        (False, True, False, NotificationRoute.APP),
        (False, False, True, NotificationRoute.CONSOLE),
        (False, False, False, NotificationRoute.LOG_ONLY),
    ],
)
def test_a_notification_comes_from_the_app_when_it_runs_else_the_console(
    tray, app, console, route
) -> None:
    assert (
        notification_route(
            tray_can_notify=tray, desktop_app_running=app, console_available=console
        )
        is route
    )
