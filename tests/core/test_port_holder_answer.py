"""Asking the process on our port whether it is a live My Claude Code (7.70.0).

User decision R5 (2026-10-01): a start that finds a live My Claude Code server
ANSWERING on its port backs off instead of killing it; the kill is kept only
for a holder that does not answer. These tests pin the classification, the
probe ladder against real loopback holders, and the decision table.
"""

import json
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from my_claude_code.core.loop_health import BUSY_MARKER_HEADER, BUSY_MARKER_VALUE
from my_claude_code.core.port_holder_answer import (
    ANSWERING,
    HolderAnswer,
    HolderProbe,
    HolderSettlement,
    PortDecision,
    back_off_message,
    classify_health_answer,
    drain_wait_message,
    parse_probe_ladder,
    probe_holder,
    probe_timeouts,
    settle_port_holder,
    stop_wait_seconds,
)
from my_claude_code.core.server_pid import SERVER_PID_HEADER
from my_claude_code.core.startup_state import (
    STARTING_MARKER_HEADER,
    STARTING_MARKER_VALUE,
)
from my_claude_code.core.stop_deadline import (
    SHUTDOWN_MARKER_HEADER,
    SHUTDOWN_MARKER_VALUE,
)

HEALTHY_BODY = b'{"status":"healthy"}'


# --------------------------------------------------------------- classification


@pytest.mark.parametrize(
    ("status", "headers", "body", "expected"),
    [
        # 7.70.0 and later: the pid header alone says "MCC".
        (200, {SERVER_PID_HEADER: "4242"}, HEALTHY_BODY, HolderAnswer.HEALTHY),
        # 7.69.x and older: no MCC header at all, recognised by the body.
        (200, {"content-type": "application/json"}, HEALTHY_BODY, HolderAnswer.HEALTHY),
        (
            200,
            {BUSY_MARKER_HEADER: BUSY_MARKER_VALUE},
            b'{"status":"healthy","busy":true}',
            HolderAnswer.BUSY,
        ),
        (
            503,
            {STARTING_MARKER_HEADER: STARTING_MARKER_VALUE},
            b'{"status":"starting"}',
            HolderAnswer.STARTING,
        ),
        (
            503,
            {SHUTDOWN_MARKER_HEADER: SHUTDOWN_MARKER_VALUE},
            b"{}",
            HolderAnswer.DRAINING,
        ),
        # A stop requested during a slow start answers "going", never "coming".
        (
            503,
            {
                SHUTDOWN_MARKER_HEADER: SHUTDOWN_MARKER_VALUE,
                STARTING_MARKER_HEADER: STARTING_MARKER_VALUE,
            },
            b"{}",
            HolderAnswer.DRAINING,
        ),
        (200, {"content-type": "text/html"}, b"<html>hi</html>", HolderAnswer.NOT_MCC),
        (503, {"retry-after": "5"}, b"busy", HolderAnswer.NOT_MCC),
        (404, {}, b"not found", HolderAnswer.NOT_MCC),
        (200, {}, b'{"status":"ok"}', HolderAnswer.NOT_MCC),
    ],
)
def test_an_answer_is_classified_by_its_markers_and_body(
    status, headers, body, expected
) -> None:
    assert classify_health_answer(status, headers, body) is expected


def test_answering_is_healthy_busy_or_starting_and_never_draining() -> None:
    expected = {HolderAnswer.HEALTHY, HolderAnswer.BUSY, HolderAnswer.STARTING}
    assert set(ANSWERING) == expected
    assert HolderAnswer.DRAINING not in ANSWERING


# -------------------------------------------------------------------- the ladder


def test_the_ladder_is_parsed_with_the_status_documents_rules() -> None:
    assert parse_probe_ladder("5, 10,15") == [5.0, 10.0, 15.0]
    assert parse_probe_ladder("5,abc,-1,0,,7.5") == [5.0, 7.5]
    assert parse_probe_ladder("") == []
    assert parse_probe_ladder(None) == []


def test_one_timeout_per_try_with_the_last_rung_repeating() -> None:
    assert probe_timeouts([5.0, 10.0, 15.0], tries=3, fallback=1.5) == [5.0, 10.0, 15.0]
    assert probe_timeouts([5.0, 10.0], tries=4, fallback=1.5) == [5.0, 10.0, 10.0, 10.0]
    assert probe_timeouts([], tries=3, fallback=1.5) == [1.5, 1.5, 1.5]
    assert probe_timeouts([5.0], tries=0, fallback=1.5) == [5.0]


def test_the_shipped_ladder_gives_a_holder_thirty_seconds_at_most() -> None:
    from my_claude_code.config.constants import (
        DESKTOP_HEALTH_FAILURE_THRESHOLD_DEFAULT,
        DESKTOP_HEALTH_PROBE_TIMEOUT_DEFAULT,
        DESKTOP_HEALTH_PROBE_TIMEOUTS_DEFAULT,
    )

    timeouts = probe_timeouts(
        parse_probe_ladder(DESKTOP_HEALTH_PROBE_TIMEOUTS_DEFAULT),
        tries=DESKTOP_HEALTH_FAILURE_THRESHOLD_DEFAULT,
        fallback=DESKTOP_HEALTH_PROBE_TIMEOUT_DEFAULT,
    )
    assert timeouts == [5.0, 10.0, 15.0]
    assert sum(timeouts) == 30.0


def test_a_draining_holder_is_given_the_servers_own_stop_budget() -> None:
    # 20 s graceful + 3 s teardown margin + 1 s watchdog beat: the sum the tray,
    # the installer and ``--print-status`` already use.
    assert stop_wait_seconds(20.0) == 24.0


# ------------------------------------------------------------ real loopback holders


@contextmanager
def _http_holder(
    status: int,
    headers: dict[str, str],
    body: bytes,
    *,
    delay: float = 0.0,
) -> Iterator[int]:
    """A tiny HTTP server on an ephemeral loopback port, answering as told."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if delay:
                time.sleep(delay)
            try:
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                return

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@contextmanager
def _silent_holder() -> Iterator[int]:
    """A listener that completes handshakes and never answers a byte."""

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    try:
        yield int(sock.getsockname()[1])
    finally:
        sock.close()


@pytest.mark.local_serial
def test_a_real_healthy_holder_is_answering_and_names_its_pid() -> None:
    with _http_holder(
        200,
        {"Content-Type": "application/json", SERVER_PID_HEADER: "4242"},
        HEALTHY_BODY,
    ) as port:
        probe = probe_holder(
            f"http://127.0.0.1:{port}", [2.0, 2.0, 2.0], port_is_free=lambda: False
        )
    assert probe.answer is HolderAnswer.HEALTHY
    assert probe.pid == 4242
    assert probe.tries == 1


@pytest.mark.local_serial
def test_an_answer_on_the_third_rung_still_counts() -> None:
    """A holder that answers late but inside the ladder is a live server."""

    with _http_holder(200, {SERVER_PID_HEADER: "77"}, HEALTHY_BODY, delay=0.6) as port:
        probe = probe_holder(
            f"http://127.0.0.1:{port}", [0.2, 0.2, 5.0], port_is_free=lambda: False
        )
    assert probe.answer is HolderAnswer.HEALTHY
    assert probe.tries == 3
    assert probe.pid == 77


@pytest.mark.local_serial
def test_a_silent_holder_is_silent_after_the_whole_ladder() -> None:
    with _silent_holder() as port:
        started = time.monotonic()
        probe = probe_holder(
            f"http://127.0.0.1:{port}", [0.2, 0.2, 0.2], port_is_free=lambda: False
        )
        elapsed = time.monotonic() - started
    assert probe.answer is HolderAnswer.SILENT
    assert probe.tries == 3
    assert elapsed < 10.0


@pytest.mark.local_serial
def test_a_holder_that_leaves_while_asked_is_free() -> None:
    with _silent_holder() as port:
        probe = probe_holder(
            f"http://127.0.0.1:{port}", [0.2, 0.2, 0.2], port_is_free=lambda: True
        )
    assert probe.answer is HolderAnswer.FREE
    assert probe.tries == 1


@pytest.mark.local_serial
def test_the_probe_never_goes_through_a_proxy(monkeypatch) -> None:
    """A loopback probe routed to ``HTTP_PROXY`` would never reach the holder."""

    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("NO_PROXY", "")
    with _http_holder(200, {SERVER_PID_HEADER: "5"}, HEALTHY_BODY) as port:
        probe = probe_holder(
            f"http://127.0.0.1:{port}", [2.0], port_is_free=lambda: False
        )
    assert probe.answer is HolderAnswer.HEALTHY


# ------------------------------------------------------------- the decision table


def _probes(*answers: HolderAnswer):
    queue = [HolderProbe(answer, pid=4242, seconds=0.5, tries=1) for answer in answers]
    calls: list[int] = []

    def probe() -> HolderProbe:
        calls.append(1)
        return queue.pop(0)

    return probe, calls


def _never_frees(_seconds: float, _probe: HolderProbe) -> bool:
    return False


def _frees(_seconds: float, _probe: HolderProbe) -> bool:
    return True


@pytest.mark.parametrize(
    "answer", [HolderAnswer.HEALTHY, HolderAnswer.BUSY, HolderAnswer.STARTING]
)
def test_an_answering_holder_makes_the_start_back_off(answer) -> None:
    probe, calls = _probes(answer)
    settlement = settle_port_holder(
        probe=probe, wait_for_free=_never_frees, drain_wait_seconds=24.0
    )
    assert settlement.decision is PortDecision.BACK_OFF
    assert len(calls) == 1


@pytest.mark.parametrize("answer", [HolderAnswer.SILENT, HolderAnswer.NOT_MCC])
def test_a_silent_or_foreign_holder_goes_to_todays_rules(answer) -> None:
    probe, _ = _probes(answer)
    settlement = settle_port_holder(
        probe=probe, wait_for_free=_never_frees, drain_wait_seconds=24.0
    )
    assert settlement.decision is PortDecision.TAKE


def test_a_port_that_came_free_is_simply_bound() -> None:
    probe, _ = _probes(HolderAnswer.FREE)
    assert (
        settle_port_holder(
            probe=probe, wait_for_free=_never_frees, drain_wait_seconds=24.0
        ).decision
        is PortDecision.FREE
    )


def test_a_draining_holder_is_waited_out_with_the_stop_budget() -> None:
    waited: list[float] = []

    def wait(seconds: float, _probe: HolderProbe) -> bool:
        waited.append(seconds)
        return True

    probe, calls = _probes(HolderAnswer.DRAINING)
    settlement = settle_port_holder(
        probe=probe, wait_for_free=wait, drain_wait_seconds=24.0
    )
    assert settlement.decision is PortDecision.FREE
    assert settlement.waited_for_drain
    assert waited == [24.0]
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("second", "decision"),
    [
        (HolderAnswer.HEALTHY, PortDecision.BACK_OFF),
        (HolderAnswer.STARTING, PortDecision.BACK_OFF),
        # Still saying it is leaving after its whole budget: still never killed.
        (HolderAnswer.DRAINING, PortDecision.BACK_OFF),
        (HolderAnswer.FREE, PortDecision.FREE),
        # Silent past its own watchdog: today's rules, as for any silent holder.
        (HolderAnswer.SILENT, PortDecision.TAKE),
        (HolderAnswer.NOT_MCC, PortDecision.TAKE),
    ],
)
def test_after_the_drain_wait_the_holder_is_asked_once_more(second, decision) -> None:
    probe, calls = _probes(HolderAnswer.DRAINING, second)
    settlement = settle_port_holder(
        probe=probe, wait_for_free=_never_frees, drain_wait_seconds=24.0
    )
    assert settlement.decision is decision
    assert settlement.waited_for_drain
    assert len(calls) == 2


# --------------------------------------------------------------------- the words


def test_the_back_off_message_names_the_pid_and_how_to_replace_it() -> None:
    settlement = HolderSettlement(
        PortDecision.BACK_OFF, HolderProbe(HolderAnswer.HEALTHY, pid=4242, seconds=3.2)
    )
    windows = back_off_message(port=8082, settlement=settlement, pid=4242, windows=True)
    posix = back_off_message(port=8082, settlement=settlement, pid=4242, windows=False)
    for text in (windows, posix):
        assert "Port 8082 is already served by My Claude Code (pid 4242" in text
        assert "3.2 s" in text
        assert "nothing was stopped" in text
        assert "Ctrl+C" in text
        assert "start mcc-server again" in text
    assert "Task Manager" in windows
    assert "kill 4242" in posix


def test_the_back_off_message_without_a_pid_still_reads() -> None:
    settlement = HolderSettlement(
        PortDecision.BACK_OFF, HolderProbe(HolderAnswer.STARTING, seconds=0.1)
    )
    text = back_off_message(port=9, settlement=settlement, pid=None, windows=True)
    assert "pid unknown" in text
    assert "still starting" in text


def test_the_drain_wait_message_says_nothing_is_stopped() -> None:
    text = drain_wait_message(port=8082, pid=12, seconds=24.0)
    assert "pid 12" in text
    assert "24 s" in text
    assert "not stopped" in text


def test_a_classified_body_is_json_never_parsed_from_a_huge_answer() -> None:
    big = json.dumps({"status": "healthy", "pad": "x" * 10}).encode()
    assert classify_health_answer(200, {}, big) is HolderAnswer.HEALTHY
